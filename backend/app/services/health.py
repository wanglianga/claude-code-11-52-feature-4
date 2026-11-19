from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    BreachFact,
    HealthEvaluation,
    MaintenanceWindow,
    Meter,
    MeterCommitment,
    MeterEvaluation,
    Sample,
    Source,
)
from ..timeutil import SLOT, iso, slots_between, utcnow


# ---------------------------------------------------------------- commitments


def commitment_segments(
    db: Session, meter: Meter, ws: datetime, we: datetime
) -> list[dict[str, Any]]:
    rows = (
        db.execute(
            select(MeterCommitment)
            .where(
                MeterCommitment.meter_id == meter.id,
                MeterCommitment.valid_from < we,
                (MeterCommitment.valid_to.is_(None)) | (MeterCommitment.valid_to > ws),
            )
            .order_by(MeterCommitment.valid_from)
        )
        .scalars()
        .all()
    )
    out: list[dict[str, Any]] = []
    for c in rows:
        seg_start = max(ws, c.valid_from)
        seg_end = min(we, c.valid_to or we)
        if seg_start >= seg_end:
            continue
        # Align slots to the cadence grid starting at the segment start. A frequency
        # change therefore splits the denominator into independent segments.
        slots = slots_between(seg_start, seg_end, c.cadence_minutes)
        maint = (
            db.execute(
                select(MaintenanceWindow).where(
                    MaintenanceWindow.meter_id == meter.id,
                    MaintenanceWindow.starts_at < seg_end,
                    MaintenanceWindow.ends_at > seg_start,
                )
            )
            .scalars()
            .all()
        )
        out.append(
            {
                "commitment_id": c.id,
                "segment_start": seg_start,
                "segment_end": seg_end,
                "cadence_minutes": c.cadence_minutes,
                "allowed_lateness_minutes": c.allowed_lateness_minutes,
                "certification_required": c.certification_required,
                "slots": slots,
                "maintenance": maint,
            }
        )
    return out


def _deducted_maintenance(
    slots: list[datetime], maint: list[MaintenanceWindow], cadence: int
) -> dict[datetime, MaintenanceWindow]:
    """Maintenance only removes slots it fully covers and only when the rule allows it."""
    out: dict[datetime, MaintenanceWindow] = {}
    for slot in slots:
        end = slot + timedelta(minutes=cadence)
        for m in maint:
            if m.allowed_deduction and m.starts_at <= slot and m.ends_at >= end:
                out[slot] = m
                break
    return out


def _load_samples(
    db: Session, meter: Meter, ws: datetime, we: datetime, cutoff: datetime
) -> tuple[dict[datetime, list[Sample]], list[Sample]]:
    rows = (
        db.execute(
            select(Sample)
            .where(
                Sample.meter_id == meter.id,
                Sample.slot_start < we,
                Sample.received_at <= cutoff,
            )
            .order_by(Sample.slot_start, Sample.received_at, Sample.id)
        )
        .scalars()
        .all()
    )
    in_window: dict[datetime, list[Sample]] = {}
    before: list[Sample] = []
    for s in rows:
        if s.slot_start >= ws:
            in_window.setdefault(s.slot_start, []).append(s)
        else:
            before.append(s)
    return in_window, before


def _prior_reading(
    db: Session, meter: Meter, before: datetime, cutoff: datetime
) -> tuple[Sample | None, str]:
    """Latest known reading to anchor a cumulative meter, following meter replacement links."""
    own = (
        db.execute(
            select(Sample)
            .where(
                Sample.meter_id == meter.id,
                Sample.slot_start < before,
                Sample.received_at <= cutoff,
            )
            .order_by(Sample.slot_start.desc(), Sample.received_at.desc())
        )
        .scalars()
        .first()
    )
    if own is not None:
        return own, "own"
    if meter.linked_from_meter_id:
        old = (
            db.execute(
                select(Sample)
                .where(
                    Sample.meter_id == meter.linked_from_meter_id,
                    Sample.slot_start < before,
                    Sample.received_at <= cutoff,
                )
                .order_by(Sample.slot_start.desc(), Sample.received_at.desc())
            )
            .scalars()
            .first()
        )
        if old is not None:
            return old, "linked"
    return None, "none"


# ---------------------------------------------------------------- meter


def evaluate_meter(
    db: Session,
    meter: Meter,
    ws: datetime,
    we: datetime,
    cutoff: datetime,
    source: Source,
) -> dict[str, Any]:
    segments = commitment_segments(db, meter, ws, we)
    by_slot, _before = _load_samples(db, meter, ws, we, cutoff)

    expected: list[datetime] = []
    deducted: dict[datetime, tuple[MaintenanceWindow, bool]] = {}
    slot_cadence: dict[datetime, int] = {}
    slot_lateness: dict[datetime, int] = {}
    seg_summary: list[dict[str, Any]] = []

    for seg in segments:
        cadence = seg["cadence_minutes"]
        lateness = seg["allowed_lateness_minutes"]
        deduct_map = _deducted_maintenance(seg["slots"], seg["maintenance"], cadence)
        deducted_count = 0
        denom = 0
        for slot in seg["slots"]:
            if slot in slot_cadence:
                continue
            slot_cadence[slot] = cadence
            slot_lateness[slot] = lateness
            if slot in deduct_map:
                m = deduct_map[slot]
                retro = m.recorded_at > slot + timedelta(minutes=cadence + lateness)
                deducted[slot] = (m, retro)
                deducted_count += 1
            else:
                expected.append(slot)
                denom += 1
        seg_summary.append(
            {
                "commitment_id": seg["commitment_id"],
                "start": iso(seg["segment_start"]),
                "end": iso(seg["segment_end"]),
                "cadence_minutes": cadence,
                "allowed_lateness_minutes": lateness,
                "slots_expected": len(seg["slots"]),
                "slots_deducted": deducted_count,
                "denominator": denom,
            }
        )

    expected.sort()

    covered: dict[datetime, Sample] = {}
    duplicates = 0
    declared_zero = 0
    late: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    beyond_cutoff: list[datetime] = []
    settled_slots: list[datetime] = []

    for slot in expected:
        samples = by_slot.get(slot, [])
        if len(samples) > 1:
            duplicates += 1
        end = slot + timedelta(minutes=slot_cadence[slot])
        if not samples:
            deadline = end + timedelta(minutes=slot_lateness[slot])
            entry = {
                "slot": iso(slot),
                "slot_end": iso(end),
                "deadline": iso(deadline),
                "age_minutes": round((cutoff - end).total_seconds() / 60, 1),
            }
            # Slots whose sampling period has not ended at the cutoff are simply not
            # due yet: they are neither missing nor part of the denominator, and they
            # never count as zero.
            if cutoff < end:
                beyond_cutoff.append(slot)
                continue
            settled_slots.append(slot)
            if cutoff < deadline:
                pending.append(entry)
            else:
                missing.append(entry)
            continue
        settled_slots.append(slot)
        chosen = samples[-1]
        covered[slot] = chosen
        if chosen.is_declared_zero:
            declared_zero += 1
        lateness_min = (chosen.received_at - end).total_seconds() / 60
        if lateness_min > slot_lateness[slot]:
            late.append(
                {
                    "slot": iso(slot),
                    "minutes_late": round(lateness_min, 1),
                    "allowed_lateness_minutes": slot_lateness[slot],
                    "received_at": iso(chosen.received_at),
                    "declared_zero": chosen.is_declared_zero,
                }
            )

    # ---- verification and consumption -------------------------------------
    verification: list[dict[str, Any]] = []
    by_slot_value: dict[str, float | None] = {}
    missing_slots = {datetime.fromisoformat(m["slot"].rstrip("Z")) for m in missing}
    covered_slots = sorted(covered)
    last_reading: Sample | None = None
    last_kind = "none"

    for slot in covered_slots:
        sample = covered[slot]
        cadence = slot_cadence[slot]
        slot_end = slot + timedelta(minutes=cadence)
        consumption: float | None = None

        if not meter.cumulative:
            consumption = sample.value * (meter.multiplier or 1.0)
        else:
            if last_reading is None:
                prior, kind = _prior_reading(db, meter, slot, cutoff)
            else:
                prior, kind = last_reading, last_kind

            if prior is None:
                if last_reading is None:
                    verification.append(
                        {
                            "slot": iso(slot),
                            "slot_end": iso(slot_end),
                            "kind": "baseline",
                            "detail": "累计表缺少可衔接的基线读数，首槽用量无法核验",
                        }
                    )
                last_reading, last_kind = sample, "own"
                by_slot_value[iso(slot)] = None
                continue

            if prior.meter_id == meter.id:
                delta = sample.value - prior.value
                if delta < 0:
                    verification.append(
                        {
                            "slot": iso(slot),
                            "slot_end": iso(slot_end),
                            "kind": "reset",
                            "detail": (
                                f"累计表读数回退：{prior.value:.3f} → {sample.value:.3f}，"
                                "负差不可计为节能"
                            ),
                        }
                    )
                else:
                    gap = any(
                        prior.slot_start < ms < slot for ms in missing_slots
                    )
                    if gap:
                        verification.append(
                            {
                                "slot": iso(slot),
                                "kind": "bridge",
                                "detail": "跨过未覆盖槽位的表码差无法归因，单列核验缺口",
                            }
                        )
                    else:
                        consumption = delta * (meter.multiplier or 1.0)
            else:
                # new meter taking over: only the first reading can be bridged by ratio
                expected_read = prior.value * (meter.link_ratio or 1.0)
                base = abs(expected_read) if abs(expected_read) > 1e-9 else 1.0
                if abs(sample.value - expected_read) / base > 0.05:
                    verification.append(
                        {
                            "slot": iso(slot),
                            "slot_end": iso(slot_end),
                            "kind": "ratio",
                            "detail": (
                                f"换表倍率无法衔接：旧表末读数 {prior.value:.3f} × "
                                f"{meter.link_ratio} = {expected_read:.3f}，"
                                f"新表首读数 {sample.value:.3f}"
                            ),
                        }
                    )
                else:
                    consumption = (sample.value - expected_read) * (meter.multiplier or 1.0)
                last_reading, last_kind = sample, "own"

        if meter.cumulative:
            last_reading, last_kind = sample, "own"
        elif last_reading is None:
            last_reading = sample
        by_slot_value[iso(slot)] = round(consumption, 4) if consumption is not None else None

    slot_states: list[dict[str, Any]] = []
    pending_slots = {datetime.fromisoformat(p["slot"].rstrip("Z")) for p in pending}
    beyond_set = set(beyond_cutoff)
    for slot in sorted(set(expected) | set(deducted)):
        if slot in deducted:
            m, retro = deducted[slot]
            slot_states.append(
                {
                    "slot": iso(slot),
                    "slot_end": iso(slot + timedelta(minutes=slot_cadence.get(slot, 15))),
                    "cadence_minutes": slot_cadence.get(slot, 15),
                    "status": "maintenance",
                    "maintenance_id": m.id,
                    "retroactive": retro,
                    "allowed_deduction": m.allowed_deduction,
                }
            )
        elif slot in covered:
            s = covered[slot]
            slot_states.append(
                {
                    "slot": iso(slot),
                    "slot_end": iso(slot + timedelta(minutes=slot_cadence.get(slot, 15))),
                    "cadence_minutes": slot_cadence.get(slot, 15),
                    "status": "covered",
                    "value": round(s.value, 4),
                    "declared_zero": s.is_declared_zero,
                    "received_at": iso(s.received_at),
                    "sample_id": s.id,
                    "lateness_minutes": round(
                        (
                            s.received_at
                            - (slot + timedelta(minutes=slot_cadence.get(slot, 15)))
                        ).total_seconds()
                        / 60,
                        1,
                    ),
                }
            )
        elif slot in pending_slots:
            slot_states.append(
                {
                    "slot": iso(slot),
                    "slot_end": iso(slot + timedelta(minutes=slot_cadence.get(slot, 15))),
                    "cadence_minutes": slot_cadence.get(slot, 15),
                    "status": "pending",
                }
            )
        elif slot in beyond_set:
            slot_states.append(
                {
                    "slot": iso(slot),
                    "slot_end": iso(slot + timedelta(minutes=slot_cadence.get(slot, 15))),
                    "cadence_minutes": slot_cadence.get(slot, 15),
                    "status": "beyond_cutoff",
                }
            )
        else:
            slot_states.append(
                {
                    "slot": iso(slot),
                    "slot_end": iso(slot + timedelta(minutes=slot_cadence.get(slot, 15))),
                    "cadence_minutes": slot_cadence.get(slot, 15),
                    "status": "missing",
                }
            )

    settled_set = set(settled_slots)
    for seg in seg_summary:
        seg_start = datetime.fromisoformat(seg["start"].rstrip("Z"))
        seg_end = datetime.fromisoformat(seg["end"].rstrip("Z"))
        seg["denominator"] = len(
            [
                s
                for s in expected
                if seg_start <= s < seg_end and s in settled_set
            ]
        )
        seg["denominator_scheduled"] = len(
            [s for s in expected if seg_start <= s < seg_end]
        )

    total = sum(v for v in by_slot_value.values() if v is not None)
    # Denominators only cover slots that were due at the cutoff; slots beyond it are
    # not evidence of anything yet.
    due = len([slot for slot in expected if slot in settled_set])
    return {
        "meter_id": meter.id,
        "meter_code": meter.code,
        "meter_name": meter.name,
        "building_code": meter.building.code,
        "building_name": meter.building.name,
        "unit": source.unit,
        "cumulative": meter.cumulative,
        "multiplier": meter.multiplier,
        "expected": due,
        "scheduled": len(expected),
        "observed": len(covered),
        "due": due,
        "beyond_cutoff": [iso(s) for s in beyond_cutoff],
        "missing": missing,
        "pending": pending,
        "late": late,
        "maintenance": [
            {
                "slot": iso(slot),
                "maintenance_id": m.id,
                "retroactive": retro,
                "reason": m.reason,
                "recorded_at": iso(m.recorded_at),
                "allowed_deduction": m.allowed_deduction,
            }
            for slot, (m, retro) in sorted(deducted.items())
        ],
        "segments": seg_summary,
        "verification": verification,
        "duplicate_slots": duplicates,
        "declared_zero_slots": declared_zero,
        "consumption": {
            "unit": source.unit,
            "total": round(total, 4),
            "by_slot": by_slot_value,
            "verified_slots": len([v for v in by_slot_value.values() if v is not None]),
        },
        "slot_states": slot_states,
    }


# ---------------------------------------------------------------- window


def _canonical(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": payload["status"],
        "coverage_num": payload["coverage_num"],
        "coverage_den": payload["coverage_den"],
        "verification_ok": payload["verification_ok"],
        "certified": payload["certified"],
        "meters": [
            {
                "meter_id": m["meter_id"],
                "expected": m["expected"],
                "observed": m["observed"],
                "due": m["due"],
                "missing": [x["slot"] for x in m["missing"]],
                "pending": [x["slot"] for x in m["pending"]],
                "late": [(x["slot"], x["minutes_late"]) for x in m["late"]],
                "maintenance": [
                    (x["slot"], x["maintenance_id"], x["retroactive"])
                    for x in m["maintenance"]
                ],
                "verification": [(x["slot"], x["kind"]) for x in m["verification"]],
                "duplicates": m["duplicate_slots"],
                "zero": m["declared_zero_slots"],
                "total": m["consumption"]["total"],
            }
            for m in payload["meters"]
        ],
    }


def _building_summary(meter_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for m in meter_results:
        key = m["building_code"]
        if key not in seen:
            seen[key] = {
                "building_code": key,
                "building_name": m["building_name"],
                "expected": 0,
                "observed": 0,
                "missing": 0,
                "pending": 0,
                "verification": 0,
                "meters": [],
            }
            order.append(key)
        b = seen[key]
        b["expected"] += m["expected"]
        b["observed"] += m["observed"]
        b["missing"] += len(m["missing"])
        b["pending"] += len(m["pending"])
        b["verification"] += len(m["verification"])
        b["meters"].append(m["meter_code"])
    return [seen[k] for k in order]


def evaluate_window(
    db: Session,
    source: Source,
    ws: datetime,
    we: datetime,
    cutoff: datetime,
    *,
    persist: bool = True,
) -> HealthEvaluation | dict[str, Any]:
    meters = (
        db.execute(select(Meter).where(Meter.source_id == source.id).order_by(Meter.code))
        .scalars()
        .all()
    )
    meter_results = [evaluate_meter(db, m, ws, we, cutoff, source) for m in meters]

    # "expected" on a meter already excludes maintenance-deducted and beyond-cutoff
    # slots, so the window denominator is the sum of per-meter settled expectations.
    coverage_den = sum(m["expected"] for m in meter_results)
    coverage_num = sum(m["observed"] for m in meter_results)
    due_den = sum(m["due"] for m in meter_results)
    scheduled_den = sum(m["scheduled"] for m in meter_results)
    beyond = [x for m in meter_results for x in m["beyond_cutoff"]]
    missing = [x for m in meter_results for x in m["missing"]]
    pending = [x for m in meter_results for x in m["pending"]]
    late = [x for m in meter_results for x in m["late"]]
    verification = [x for m in meter_results for x in m["verification"]]
    verification_ok = len(verification) == 0

    # Health is computed from freshness + coverage + verification together; it never
    # depends on whether collection tasks are alive or whether an incident was closed.
    complete = coverage_den > 0 and coverage_num == coverage_den and due_den == coverage_den
    punctual = len(late) == 0
    if missing or not verification_ok:
        status = "breached"
    elif pending or cutoff < we:
        status = "pending"
    elif not complete:
        status = "partial"
    elif not punctual:
        status = "degraded"
    else:
        status = "healthy"

    certified = bool(
        complete
        and punctual
        and verification_ok
        and cutoff >= we
        and coverage_den > 0
        and scheduled_den == coverage_den
    )
    confirmable = bool(
        complete and punctual and verification_ok and cutoff >= we and scheduled_den == coverage_den
    )
    if pending:
        freshness = "lagging"
    elif missing:
        freshness = "stale"
    elif late:
        freshness = "late_arrival"
    else:
        freshness = "current"

    last_seen: datetime | None = None
    for meter in meters:
        row = (
            db.execute(
                select(Sample.received_at)
                .where(
                    Sample.meter_id == meter.id,
                    Sample.received_at <= cutoff,
                    Sample.slot_start < we,
                )
                .order_by(Sample.received_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if row and (last_seen is None or row > last_seen):
            last_seen = row
    freshness_minutes = (
        round((cutoff - last_seen).total_seconds() / 60, 1) if last_seen else None
    )
    max_lateness = max((x["minutes_late"] for x in late), default=0)

    payload: dict[str, Any] = {
        "status": status,
        "freshness": freshness,
        "freshness_minutes": freshness_minutes,
        "coverage_num": coverage_num,
        "coverage_den": coverage_den,
        "due_num": coverage_num,
        "due_den": due_den,
        "scheduled_den": scheduled_den,
        "beyond_cutoff_count": len(beyond),
        "coverage": round(coverage_num / coverage_den, 4) if coverage_den else None,
        "coverage_ok": complete,
        "verification_ok": verification_ok,
        "certified": certified,
        "confirmable": confirmable,
        "max_lateness_minutes": round(max_lateness),
        "counts": {
            "missing": len(missing),
            "pending": len(pending),
            "late": len(late),
            "verification": len(verification),
            "maintenance": sum(len(m["maintenance"]) for m in meter_results),
            "duplicates": sum(m["duplicate_slots"] for m in meter_results),
            "declared_zero": sum(m["declared_zero_slots"] for m in meter_results),
            "beyond_cutoff": len(beyond),
        },
        "meters": meter_results,
        "missing": missing,
        "pending": pending,
        "late": late,
        "verification": verification,
        "building_summary": _building_summary(meter_results),
    }

    if not persist:
        return payload

    latest = latest_evaluation(db, source.id, ws, we)
    if latest is not None and _canonical(latest.details or {}) == _canonical(payload):
        latest.data_cutoff = cutoff
        latest.computed_at = utcnow()
        latest.recompute_count = (latest.recompute_count or 1) + 1
        latest.details = payload
        latest.freshness = freshness
        latest.status = status
        latest.coverage_num = coverage_num
        latest.coverage_den = coverage_den
        latest.coverage_ok = complete
        latest.verification_ok = verification_ok
        latest.certified = certified
        latest.confirmable = confirmable
        latest.max_lateness_minutes = round(max_lateness)
        db.flush()
        ev = latest
    else:
        revision = (latest.revision + 1) if latest else 0
        ev = HealthEvaluation(
            source_id=source.id,
            window_start=ws,
            window_end=we,
            data_cutoff=cutoff,
            revision=revision,
            status=status,
            freshness=freshness,
            coverage_num=coverage_num,
            coverage_den=coverage_den,
            coverage_ok=complete,
            verification_ok=verification_ok,
            certified=certified,
            confirmable=confirmable,
            max_lateness_minutes=round(max_lateness),
            details=payload,
        )
        db.add(ev)
        db.flush()
        # Replace stale per-meter rows for this evaluation revision only; older
        # revisions stay appended for traceability.
        for m in meter_results:
            db.add(
                MeterEvaluation(
                    evaluation_id=ev.id,
                    meter_id=m["meter_id"],
                    building_code=m["building_code"],
                    expected=m["expected"],
                    observed=m["observed"],
                    missing=m["missing"],
                    pending=m["pending"],
                    late=m["late"],
                    maintenance=m["maintenance"],
                    segments=m["segments"],
                    verification=m["verification"],
                    duplicate_slots=m["duplicate_slots"],
                    declared_zero_slots=m["declared_zero_slots"],
                    consumption=m["consumption"],
                )
            )
        db.flush()

    _record_facts(db, source, ev, meter_results, cutoff)
    return ev


def latest_evaluation(
    db: Session, source_id: int, ws: datetime, we: datetime
) -> HealthEvaluation | None:
    return (
        db.execute(
            select(HealthEvaluation)
            .where(
                HealthEvaluation.source_id == source_id,
                HealthEvaluation.window_start == ws,
                HealthEvaluation.window_end == we,
            )
            .order_by(HealthEvaluation.revision.desc())
        )
        .scalars()
        .first()
    )


def evaluation_at_cutoff(
    db: Session, source_id: int, ws: datetime, we: datetime, cutoff: datetime
) -> HealthEvaluation | None:
    return (
        db.execute(
            select(HealthEvaluation)
            .where(
                HealthEvaluation.source_id == source_id,
                HealthEvaluation.window_start == ws,
                HealthEvaluation.window_end == we,
                HealthEvaluation.data_cutoff <= cutoff,
            )
            .order_by(HealthEvaluation.revision.desc())
        )
        .scalars()
        .first()
    )


def _slot_end_from_states(meter_result: dict[str, Any], slot: datetime, default) -> datetime:
    for st in meter_result.get("slot_states", []):
        if st["slot"] == iso(slot):
            cadence = st.get("cadence_minutes") or 15
            return slot + timedelta(minutes=cadence)
    return slot + default


def _record_facts(
    db: Session,
    source: Source,
    ev: HealthEvaluation,
    meter_results: list[dict[str, Any]],
    cutoff: datetime,
) -> None:
    """Breach facts are append-only. A late sample or a maintenance window resolves a
    fact but never erases it; retroactive maintenance is annotated as such."""
    facts = {
        (f.meter_id, f.slot_start, f.kind): f
        for f in db.execute(
            select(BreachFact).where(
                BreachFact.source_id == source.id,
                BreachFact.slot_start >= ev.window_start,
                BreachFact.slot_start < ev.window_end,
            )
        )
        .scalars()
        .all()
    }
    for m in meter_results:
        for miss in m["missing"]:
            slot = datetime.fromisoformat(miss["slot"].rstrip("Z"))
            key = (m["meter_id"], slot, "missing")
            fact = facts.get(key)
            if fact is None:
                facts[key] = BreachFact(
                    source_id=source.id,
                    meter_id=m["meter_id"],
                    slot_start=slot,
                    slot_end=_slot_end_from_states(m, slot, SLOT),
                    kind="missing",
                    reason="expected sample not received before deadline",
                    detected_at=cutoff,
                    evaluation_id=ev.id,
                )
                db.add(facts[key])
            elif fact.resolved_at is None:
                fact.evaluation_id = ev.id

        # Late-beyond-commitment arrivals are recorded as facts too: the arrival already
        # resolves them, but the fact itself is never erased.
        for lt in m["late"]:
            slot = datetime.fromisoformat(lt["slot"].rstrip("Z"))
            key = (m["meter_id"], slot, "late")
            fact = facts.get(key)
            if fact is None:
                facts[key] = BreachFact(
                    source_id=source.id,
                    meter_id=m["meter_id"],
                    slot_start=slot,
                    slot_end=_slot_end_from_states(m, slot, SLOT),
                    kind="late",
                    reason=f"arrived {lt['minutes_late']:.0f} min late, allowed {lt['allowed_lateness_minutes']} min",
                    detected_at=cutoff,
                    resolved_at=cutoff,
                    evaluation_id=ev.id,
                )
                db.add(facts[key])
            else:
                fact.evaluation_id = ev.id

        for v in m["verification"]:
            slot = datetime.fromisoformat(v["slot"].rstrip("Z"))
            key = (m["meter_id"], slot, "verification")
            fact = facts.get(key)
            if fact is None:
                facts[key] = BreachFact(
                    source_id=source.id,
                    meter_id=m["meter_id"],
                    slot_start=slot,
                    slot_end=_slot_end_from_states(m, slot, SLOT),
                    kind="verification",
                    reason=v["kind"],
                    detected_at=cutoff,
                    evaluation_id=ev.id,
                    note=v["detail"],
                )
                db.add(facts[key])
            elif fact.resolved_at is None:
                fact.evaluation_id = ev.id

        covered = {
            datetime.fromisoformat(st["slot"].rstrip("Z")): st
            for st in m["slot_states"]
            if st["status"] == "covered"
        }
        still_missing = {
            datetime.fromisoformat(x["slot"].rstrip("Z")) for x in m["missing"]
        }
        still_verified = {
            datetime.fromisoformat(x["slot"].rstrip("Z")) for x in m["verification"]
        }
        maint_by_slot = {
            datetime.fromisoformat(x["slot"].rstrip("Z")): x for x in m["maintenance"]
        }

        for (meter_id, slot, kind), fact in facts.items():
            if meter_id != m["meter_id"]:
                continue
            if kind == "missing" and fact.resolved_at is None:
                if slot in covered:
                    fact.resolved_at = cutoff
                    fact.resolved_by_sample_id = covered[slot].get("sample_id")
                elif slot in maint_by_slot:
                    mt = maint_by_slot[slot]
                    fact.resolved_at = cutoff
                    fact.retroactive_maintenance_id = mt["maintenance_id"]
                    if mt["retroactive"]:
                        fact.note = (
                            (fact.note + " | " if fact.note else "")
                            + "事后补录维护窗口：扣除分母，但不撤销当时已发生的违约事实"
                        )
            if kind == "verification" and fact.resolved_at is None:
                if slot not in still_verified and slot in covered:
                    fact.resolved_at = cutoff
                    fact.resolved_by_sample_id = covered[slot].get("sample_id")
    db.flush()
