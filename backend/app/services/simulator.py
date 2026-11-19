from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    AuditLog,
    Consumer,
    MaintenanceWindow,
    Meter,
    MeterCommitment,
    Sample,
    SimClock,
    Source,
)
from ..timeutil import SLOT, iso, utcnow, window_floor, windows_of_day, WINDOW
from .health import evaluate_window, latest_evaluation
from .impact import compute_consumer_impact
from .incidents import reconcile_incidents, sync_impacts_into_incidents
from .ingest import generate_samples
from .products import PRODUCT_DAILY, PRODUCT_REALTIME, build_product


def get_clock(db: Session) -> SimClock:
    clock = db.get(SimClock, 1)
    if clock is None:
        raise RuntimeError("时钟未初始化")
    return clock


def set_clock(db: Session, now: datetime, *, day: str | None = None) -> SimClock:
    clock = get_clock(db)
    clock.now = now
    if day:
        clock.day = day
    clock.revision += 1
    clock.updated_at = utcnow()
    db.flush()
    return clock


def windows_upto(day: str, now: datetime) -> list[datetime]:
    return [w for w in windows_of_day(day) if w + WINDOW <= now]


def project_window(
    db: Session,
    source: Source,
    ws: datetime,
    we: datetime,
    cutoff: datetime,
    *,
    evaluate: bool = True,
    include_impacts: bool = False,
) -> dict[str, Any]:
    evaluation = (
        evaluate_window(db, source, ws, we, cutoff) if evaluate else None
    )
    if evaluation is None:
        return {}
    rt = build_product(db, source, evaluation, PRODUCT_REALTIME, cutoff)
    daily = build_product(db, source, evaluation, PRODUCT_DAILY, cutoff)
    incidents = reconcile_incidents(db, source, evaluation, cutoff)
    impacts: list[Any] = []
    if include_impacts:
        consumers = db.execute(select(Consumer).order_by(Consumer.id)).scalars().all()
        impacts = [compute_consumer_impact(db, c, ws, we, cutoff) for c in consumers]
        sync_impacts_into_incidents(db, source, evaluation, cutoff)
    db.flush()
    return {
        "evaluation": {
            "id": evaluation.id,
            "revision": evaluation.revision,
            "status": evaluation.status,
            "coverage_num": evaluation.coverage_num,
            "coverage_den": evaluation.coverage_den,
            "certified": evaluation.certified,
            "recompute_count": evaluation.recompute_count,
        },
        "realtime_product": {"id": rt.id, "status": rt.status, "version": rt.version},
        "daily_product": {"id": daily.id, "status": daily.status, "version": daily.version},
        "impacts": [
            {
                "consumer": i.consumer.code if hasattr(i, "consumer") else None,
                "status": i.status,
                "affected_slots": i.affected_slots,
            }
            for i in impacts
        ],
        "incidents": [
            {"id": i.id, "code": i.code, "status": i.status} for i in incidents
        ],
    }


def run_checks(
    db: Session, cutoff: datetime | None = None, *, day: str | None = None
) -> dict[str, Any]:
    """Scheduled check: full recompute of every completed window for the day.

    Per window: evaluate every source, build both products, then evaluate consumer
    impact once (after all sources exist) and reconcile incidents.
    """
    clock = get_clock(db)
    cutoff = cutoff or clock.now
    active_day = day or clock.day
    sources = db.execute(select(Source).order_by(Source.id)).scalars().all()
    consumers = db.execute(select(Consumer).order_by(Consumer.id)).scalars().all()
    results: list[dict[str, Any]] = []
    completed = windows_upto(active_day, cutoff)
    running = None
    candidate = window_floor(cutoff)
    if candidate.date() == cutoff.date() and candidate + WINDOW > cutoff and candidate not in completed:
        running = candidate
    for ws in completed:
        we = ws + WINDOW
        for source in sources:
            res = project_window(db, source, ws, we, cutoff)
            results.append({"source": source.code, "window_start": iso(ws), "window_end": iso(we), **res})
        for consumer in consumers:
            compute_consumer_impact(db, consumer, ws, we, cutoff)
        for source in sources:
            evaluation = latest_evaluation(db, source.id, ws, we)
            if evaluation is not None:
                sync_impacts_into_incidents(db, source, evaluation, cutoff)
        db.flush()

    if running is not None:
        # The running window has no confirmed product; it only feeds the provisional
        # realtime curve with an explicit data cutoff. Daily summary stays pending.
        for source in sources:
            res = project_window(db, source, running, running + WINDOW, cutoff)
            results.append(
                {"source": source.code, "window_start": iso(running), "window_end": iso(running + WINDOW), **res}
            )
        for consumer in consumers:
            compute_consumer_impact(db, consumer, running, running + WINDOW, cutoff)
        db.flush()
    db.commit()
    return {"cutoff": iso(cutoff), "windows": results}


def advance_clock(db: Session, minutes: int, *, run: bool = True) -> dict[str, Any]:
    clock = get_clock(db)
    previous = clock.now
    new_now = previous + timedelta(minutes=minutes)
    set_clock(db, new_now)
    db.flush()
    # meters keep emitting as simulated time passes
    created = generate_samples(db, previous, new_now)
    db.add(
        AuditLog(
            at_sim=new_now,
            actor="simulator",
            action="clock_advance",
            target=f"+{minutes}m",
            detail={"to": iso(new_now), "samples_generated": created},
        )
    )
    db.commit()
    if run:
        return run_checks(db, new_now)
    return {"now": iso(new_now), "windows": [], "samples_generated": created}


def go_to(db: Session, target: datetime, *, run: bool = True) -> dict[str, Any]:
    clock = get_clock(db)
    previous = clock.now
    set_clock(db, target)
    db.flush()
    created = generate_samples(db, previous, target) if target > previous else 0
    db.add(
        AuditLog(
            at_sim=target,
            actor="simulator",
            action="clock_set",
            target=iso(target),
            detail={"from": iso(previous), "samples_generated": created},
        )
    )
    db.commit()
    if run:
        return run_checks(db, target)
    return {"now": iso(target), "windows": [], "samples_generated": created}


def ingest_sample(
    db: Session,
    meter: Meter,
    slot_start: datetime,
    value: float,
    *,
    is_declared_zero: bool = False,
    is_correction: bool = False,
    note: str = "",
    actor: str = "ingest",
) -> dict[str, Any]:
    """Arrival event: insert a sample and incrementally re-evaluate affected windows.

    Re-evaluation is still a full recompute of each affected window — the event only
    selects *which* windows are recomputed, it never patches a slot in place.
    """
    clock = get_clock(db)
    sample = Sample(
        meter_id=meter.id,
        slot_start=slot_start,
        value=value,
        received_at=clock.now,
        is_declared_zero=is_declared_zero,
        is_correction=is_correction,
        source="meter",
        note=note,
    )
    db.add(sample)
    db.flush()
    db.add(
        AuditLog(
            at_sim=clock.now,
            actor=actor,
            action="sample_arrived",
            target=meter.code,
            detail={
                "slot_start": iso(slot_start),
                "value": value,
                "declared_zero": is_declared_zero,
                "correction": is_correction,
                "sample_id": sample.id,
            },
        )
    )
    result = recompute_windows_for_slot(db, slot_start, clock.now)
    db.commit()
    return {"sample_id": sample.id, **result}


def recompute_windows_for_slot(
    db: Session, slot_start: datetime, cutoff: datetime
) -> dict[str, Any]:
    """Arrival-triggered incremental recompute: the event only selects which windows are
    rebuilt; each affected window is still fully recomputed (and consumer impact and
    incidents are re-derived from it, so a curve refresh can never outrun completeness)."""
    day = slot_start.date().isoformat()
    out: list[dict[str, Any]] = []
    sources = db.execute(select(Source).order_by(Source.id)).scalars().all()
    consumers = db.execute(select(Consumer).order_by(Consumer.id)).scalars().all()
    for ws in windows_of_day(day):
        if not (ws <= slot_start < ws + WINDOW):
            continue
        if ws + WINDOW > cutoff:
            continue
        for source in sources:
            res = project_window(db, source, ws, ws + WINDOW, cutoff)
            out.append({"source": source.code, "window_start": iso(ws), **res})
        for consumer in consumers:
            compute_consumer_impact(db, consumer, ws, ws + WINDOW, cutoff)
        for source in sources:
            evaluation = latest_evaluation(db, source.id, ws, ws + WINDOW)
            if evaluation is not None:
                sync_impacts_into_incidents(db, source, evaluation, cutoff)
        db.flush()
    db.commit()
    return {"recomputed": out}


def reevaluate_day(db: Session, *, day: str | None = None, cutoff: datetime | None = None) -> dict[str, Any]:
    clock = get_clock(db)
    return run_checks(db, cutoff or clock.now, day=day or clock.day)


def add_maintenance(
    db: Session,
    meter: Meter,
    starts_at: datetime,
    ends_at: datetime,
    *,
    allowed_deduction: bool = True,
    reason: str = "",
    recorded_at: datetime | None = None,
    actor: str = "ops",
) -> dict[str, Any]:
    clock = get_clock(db)
    mw = MaintenanceWindow(
        meter_id=meter.id,
        starts_at=starts_at,
        ends_at=ends_at,
        allowed_deduction=allowed_deduction,
        recorded_at=recorded_at or clock.now,
        reason=reason,
        created_by=actor,
    )
    db.add(mw)
    db.flush()
    db.add(
        AuditLog(
            at_sim=clock.now,
            actor=actor,
            action="maintenance_recorded",
            target=meter.code,
            detail={
                "starts_at": iso(starts_at),
                "ends_at": iso(ends_at),
                "recorded_at": iso(mw.recorded_at),
                "allowed_deduction": allowed_deduction,
                "maintenance_id": mw.id,
            },
        )
    )
    db.commit()
    result = reevaluate_day(db)
    return {"maintenance_id": mw.id, **result}


def add_commitment(
    db: Session,
    meter: Meter,
    valid_from: datetime,
    *,
    cadence_minutes: int,
    allowed_lateness_minutes: int = 30,
    note: str = "",
) -> dict[str, Any]:
    previous = (
        db.execute(
            select(MeterCommitment)
            .where(
                MeterCommitment.meter_id == meter.id,
                MeterCommitment.valid_from < valid_from,
                (MeterCommitment.valid_to.is_(None))
                | (MeterCommitment.valid_to > valid_from),
            )
            .order_by(MeterCommitment.valid_from.desc())
        )
        .scalars()
        .first()
    )
    if previous is not None:
        previous.valid_to = valid_from
    c = MeterCommitment(
        meter_id=meter.id,
        valid_from=valid_from,
        cadence_minutes=cadence_minutes,
        allowed_lateness_minutes=allowed_lateness_minutes,
        note=note,
    )
    db.add(c)
    db.flush()
    db.add(
        AuditLog(
            at_sim=get_clock(db).now,
            actor="registry",
            action="commitment_change",
            target=meter.code,
            detail={
                "valid_from": iso(valid_from),
                "cadence_minutes": cadence_minutes,
                "previous_commitment_id": previous.id if previous else None,
            },
        )
    )
    db.commit()
    return {"commitment_id": c.id, **reevaluate_day(db)}


def sim_action(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Generic simulator entrypoint used by the UI and acceptance scripts."""
    clock = get_clock(db)
    action = payload.get("action")

    def meter_by_code(code: str) -> Meter:
        meter = (
            db.execute(select(Meter).where(Meter.code == code)).scalars().first()
        )
        if meter is None:
            raise ValueError(f"未知表计 {code}")
        return meter

    def parse_dt(key: str) -> datetime:
        raw = payload.get(key)
        if raw is None:
            raise ValueError(f"缺少参数 {key}")
        return datetime.fromisoformat(str(raw).replace("Z", ""))

    if action == "advance":
        return advance_clock(db, int(payload.get("minutes", 15)))
    if action == "goto":
        return go_to(db, parse_dt("at"))
    if action == "recheck":
        return reevaluate_day(db)
    if action == "delay":
        meter = meter_by_code(payload["meter"])
        start = parse_dt("from")
        end = parse_dt("to")
        minutes = int(payload.get("minutes", 120))
        rows = (
            db.execute(
                select(Sample).where(
                    Sample.meter_id == meter.id,
                    Sample.slot_start >= start,
                    Sample.slot_start < end,
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.received_at = row.received_at + timedelta(minutes=minutes)
        db.commit()
        db.add(
            AuditLog(
                at_sim=clock.now,
                actor="simulator",
                action="delay_arrival",
                target=meter.code,
                detail={"from": iso(start), "to": iso(end), "minutes": minutes},
            )
        )
        db.commit()
        return {"affected_samples": len(rows), **reevaluate_day(db)}
    if action == "drop":
        meter = meter_by_code(payload["meter"])
        start = parse_dt("from")
        end = parse_dt("to")
        rows = (
            db.execute(
                select(Sample).where(
                    Sample.meter_id == meter.id,
                    Sample.slot_start >= start,
                    Sample.slot_start < end,
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            db.delete(row)
        db.add(
            AuditLog(
                at_sim=clock.now,
                actor="simulator",
                action="drop_samples",
                target=meter.code,
                detail={"from": iso(start), "to": iso(end), "count": len(rows)},
            )
        )
        db.commit()
        return {"dropped": len(rows), **reevaluate_day(db)}
    if action == "backfill":
        meter = meter_by_code(payload["meter"])
        slot = parse_dt("slot")
        value = float(payload["value"])
        return ingest_sample(
            db,
            meter,
            slot,
            value,
            is_correction=True,
            note=payload.get("note", "迟到补传"),
            actor="backfill",
        )
    if action == "ingest":
        meter = meter_by_code(payload["meter"])
        slot = parse_dt("slot")
        value = float(payload["value"])
        return ingest_sample(
            db,
            meter,
            slot,
            value,
            is_declared_zero=bool(payload.get("declared_zero", False)),
            is_correction=bool(payload.get("correction", False)),
            note=payload.get("note", ""),
        )
    if action == "maintenance":
        meter = meter_by_code(payload["meter"])
        return add_maintenance(
            db,
            meter,
            parse_dt("from"),
            parse_dt("to"),
            allowed_deduction=bool(payload.get("allowed_deduction", True)),
            reason=payload.get("reason", "计划维护"),
            recorded_at=parse_dt("recorded_at") if payload.get("recorded_at") else None,
        )
    if action == "frequency":
        meter = meter_by_code(payload["meter"])
        return add_commitment(
            db,
            meter,
            parse_dt("from"),
            cadence_minutes=int(payload["cadence_minutes"]),
            allowed_lateness_minutes=int(payload.get("allowed_lateness_minutes", 30)),
            note=payload.get("note", "换频"),
        )
    raise ValueError(f"未知动作 {action}")
