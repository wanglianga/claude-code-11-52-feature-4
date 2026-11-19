from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import (
    AuditLog,
    BreachFact,
    Consumer,
    ConsumerImpact,
    HealthEvaluation,
    Incident,
    IncidentEvent,
    MaintenanceWindow,
    Meter,
    MeterCommitment,
    MeterEvaluation,
    ProductRun,
    RecoveryCheck,
    Sample,
    SimClock,
    Source,
)
from ..timeutil import WINDOW, iso, windows_of_day
from .health import latest_evaluation
from .impact import compute_consumer_impact
from .incidents import attempt_close, run_recovery_check
from .ingest import generate_samples, meter_reading
from .simulator import (
    add_commitment,
    advance_clock,
    get_clock,
    go_to,
    run_checks,
)

DAY = "2026-11-10"
BASE_NOW = datetime(2026, 11, 10, 6, 0)
W0 = datetime(2026, 11, 10, 0, 0)
W1 = datetime(2026, 11, 10, 12, 0)


# ------------------------------------------------------------------ reset


def reset_demo_state(db: Session) -> None:
    """Return the demo to a clean reproducible baseline before replaying acceptance."""
    from .demand_acceptance import reset_demand

    reset_demand(db)
    for model in (
        ConsumerImpact,
        RecoveryCheck,
        IncidentEvent,
        BreachFact,
        Incident,
        ProductRun,
        MeterEvaluation,
        HealthEvaluation,
        AuditLog,
        Sample,
        MaintenanceWindow,
        MeterCommitment,
    ):
        db.execute(delete(model))
    db.flush()

    for meter in db.execute(select(Meter)).scalars():
        # Peak-demand meters are an independent instrument role with their own
        # evidence base; they deliberately do not join the SLO sampling commitments.
        if meter.source.code == "demand":
            continue
        db.add(
            MeterCommitment(
                meter_id=meter.id,
                valid_from=datetime(2026, 1, 1),
                cadence_minutes=15,
                allowed_lateness_minutes=30,
                note="标准 15 分钟采样承诺",
            )
        )
    db.flush()

    b3 = db.execute(select(Meter).where(Meter.code == "M-ELE-B3-01")).scalars().one()
    db.add(
        MaintenanceWindow(
            meter_id=b3.id,
            starts_at=datetime(2026, 11, 10, 2, 0),
            ends_at=datetime(2026, 11, 10, 3, 0),
            allowed_deduction=True,
            recorded_at=datetime(2026, 11, 9, 18, 0),
            reason="计划停电检修（提前登记）",
            created_by="ops",
        )
    )
    clock = db.get(SimClock, 1)
    if clock is None:
        clock = SimClock(id=1, day=DAY)
        db.add(clock)
    clock.now = BASE_NOW
    clock.day = DAY
    clock.revision = (clock.revision or 0) + 1
    db.flush()

    generate_samples(db, datetime(2026, 11, 9, 18, 0), BASE_NOW)
    db.commit()
    run_checks(db, BASE_NOW)


# ------------------------------------------------------------------ helpers


def _meter(db: Session, code: str) -> Meter:
    return db.execute(select(Meter).where(Meter.code == code)).scalars().one()


def _drop_samples(db: Session, meter: Meter, start: datetime, end: datetime) -> int:
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
    db.flush()
    return len(rows)


def _shift_arrivals(
    db: Session, meter: Meter, start: datetime, end: datetime, minutes: int
) -> int:
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
    db.flush()
    return len(rows)


def _arrive(
    db: Session,
    meter: Meter,
    slot: datetime,
    value: float | None = None,
    *,
    correction: bool = False,
    note: str = "",
) -> None:
    if value is None:
        value, _ = meter_reading(meter, slot)
    db.add(
        Sample(
            meter_id=meter.id,
            slot_start=slot,
            value=value,
            received_at=get_clock(db).now,
            is_correction=correction,
            note=note,
        )
    )
    db.flush()


def _current_incident(db: Session, source_id: int) -> Incident | None:
    return (
        db.execute(
            select(Incident)
            .where(Incident.source_id == source_id)
            .order_by(Incident.id.desc())
        )
        .scalars()
        .first()
    )


def observe(db: Session, label: str) -> dict[str, Any]:
    """Read the state back from the database — no values are synthesised here."""
    clock = get_clock(db)
    windows: list[dict[str, Any]] = []
    for ws in windows_of_day(DAY):
        if ws + WINDOW > clock.now:
            continue
        item: dict[str, Any] = {
            "window_start": iso(ws),
            "window_end": iso(ws + WINDOW),
            "sources": [],
        }
        for source in db.execute(select(Source).order_by(Source.id)).scalars():
            ev = latest_evaluation(db, source.id, ws, ws + WINDOW)
            if ev is None:
                continue
            details = ev.details or {}
            item["sources"].append(
                {
                    "source": source.code,
                    "evaluation_id": ev.id,
                    "revision": ev.revision,
                    "status": ev.status,
                    "freshness": ev.freshness,
                    "coverage_num": ev.coverage_num,
                    "coverage_den": ev.coverage_den,
                    "certified": ev.certified,
                    "recompute_count": ev.recompute_count,
                    "counts": details.get("counts"),
                    "buildings": details.get("building_summary"),
                    "meters": [
                        {
                            "meter_code": m["meter_code"],
                            "building_code": m["building_code"],
                            "cumulative": m["cumulative"],
                            "expected": m["expected"],
                            "observed": m["observed"],
                            "segments": m["segments"],
                            "maintenance_slots": len(m["maintenance"]),
                            "late_slots": len(m["late"]),
                            "duplicate_slots": m["duplicate_slots"],
                            "declared_zero_slots": m["declared_zero_slots"],
                            "verification": m["verification"][:6],
                        }
                        for m in details.get("meters", [])
                    ],
                }
            )
        item["consumers"] = []
        for consumer in db.execute(select(Consumer).order_by(Consumer.id)).scalars():
            imp = (
                db.execute(
                    select(ConsumerImpact)
                    .where(
                        ConsumerImpact.consumer_id == consumer.id,
                        ConsumerImpact.window_start == ws,
                        ConsumerImpact.window_end == ws + WINDOW,
                    )
                    .order_by(ConsumerImpact.revision.desc())
                )
                .scalars()
                .first()
            )
            if imp is None:
                continue
            item["consumers"].append(
                {
                    "consumer": consumer.code,
                    "kind": consumer.kind,
                    "impact_id": imp.id,
                    "status": imp.status,
                    "severity": imp.severity,
                    "affected_slots": imp.affected_slots,
                    "thresholds": imp.threshold_snapshot,
                    "ranges": imp.ranges,
                    "affected_buildings": (imp.facts or {}).get("affected_buildings"),
                    "dependencies": [
                        {
                            "product_code": d["product_code"],
                            "product_field": d["product_field"],
                            "building_code": d["building_code"],
                            "status": d["status"],
                            "affected_slots": d["affected_slots"],
                            "per_building": d.get("per_building"),
                        }
                        for d in (imp.facts or {}).get("dependencies", [])
                    ],
                }
            )
        windows.append(item)

    incidents = []
    for inc in db.execute(select(Incident).order_by(Incident.id)).scalars():
        incidents.append(
            {
                "id": inc.id,
                "code": inc.code,
                "status": inc.status,
                "cause": inc.cause_category,
                "ranges": inc.ranges,
                "prior_incident_id": inc.prior_incident_id,
                "recovery_check_id": inc.recovery_check_id,
                "events": [
                    {"kind": e.kind, "at": iso(e.at_sim), "message": e.message}
                    for e in inc.events
                ],
            }
        )
    return {"label": label, "at": iso(clock.now), "windows": windows, "incidents": incidents}


# ------------------------------------------------------------------ narrative


def run_acceptance(db: Session) -> dict[str, Any]:
    """Replay the acceptance narrative from actual simulated meter arrivals.

    Order of events follows the acceptance script: planned maintenance, frequency
    change, a single delayed meter, late backfill, duplicate/declared-zero arrivals,
    cumulative reset, dependency branch, recovery, a gap before close, repair and
    closure, then a recurrence that must open a *new* linked incident.
    """
    reset_demo_state(db)
    steps: list[dict[str, Any]] = []

    def record(
        step: str, title: str, detail: dict[str, Any] | None = None
    ) -> None:
        steps.append(
            {
                "step": step,
                "title": title,
                "detail": detail or {},
                "observation": observe(db, title),
            }
        )

    # 1. baseline --------------------------------------------------------------
    record(
        "baseline",
        "基线：已完成窗口完整重算，覆盖 100% 且已核验",
        {"window": iso(W0)},
    )

    # 2. planned maintenance (pre-registered, deduction allowed) --------------
    record(
        "planned_maintenance",
        "计划维护：仓储中心 02:00–03:00 提前登记，仅扣除规则允许的 4 个槽位",
        {"meter": "M-ELE-B3-01"},
    )

    # 3. frequency change -> denominator split --------------------------------
    water = _meter(db, "M-WAT-B1-01")
    add_commitment(
        db,
        water,
        datetime(2026, 11, 10, 2, 0),
        cadence_minutes=30,
        allowed_lateness_minutes=30,
        note="验收：02:00 起改为 30 分钟频率",
    )
    record(
        "frequency_change",
        "换频：研发楼水表 02:00 起改为 30 分钟，窗口按前后两段分别计算分母",
        {"meter": water.code},
    )

    # 4. tolerance split: a 45-minute arrival is fine for property (60 min), late for
    #    settlement (30 min) — the same raw fact produces different verdicts.
    b4 = _meter(db, "M-ELE-B4-01")
    # 05:00-05:15 samples are dropped at a 06:00 cutoff: 45 minutes overdue. Property
    # (60 min tolerance) still has no impact; settlement (30 min) already does.
    b4_dropped = _drop_samples(
        db, b4, datetime(2026, 11, 10, 5, 0), datetime(2026, 11, 10, 5, 15)
    )
    db.commit()
    run_checks(db, get_clock(db).now)
    record(
        "tolerance_split",
        "容忍差异：综合办公楼 05:00–05:15 槽位在 06:00 截止时已超时 45 分钟，物业（容忍 60）无影响，结算（容忍 30）已受影响",
        {"meter": b4.code, "dropped": b4_dropped, "overdue_minutes": 45},
    )
    _arrive(db, b4, datetime(2026, 11, 10, 5, 0), correction=True, note="恢复该槽位")
    db.commit()
    run_checks(db, get_clock(db).now)

    # 5. single meter delayed 120 minutes -------------------------------------
    elec = _meter(db, "M-ELE-B2-01")
    shifted = _shift_arrivals(
        db, elec, datetime(2026, 11, 10, 3, 0), datetime(2026, 11, 10, 5, 0), 120
    )
    db.commit()
    run_checks(db, get_clock(db).now)
    record(
        "single_meter_delayed",
        "单表计延迟：生产车间动力进线 03:00–05:00 样本晚到 120 分钟，超出允许迟到 30 分钟",
        {"meter": elec.code, "shifted_samples": shifted, "delay_minutes": 120},
    )

    # 6. missing interval -> breach + incident --------------------------------
    dropped = _drop_samples(
        db, elec, datetime(2026, 11, 10, 5, 0), datetime(2026, 11, 10, 6, 0)
    )
    db.commit()
    run_checks(db, get_clock(db).now)
    inc = _current_incident(db, elec.source_id)
    record(
        "missing_interval",
        "缺失区间：05:00–06:00 四个槽位未到达，事故打开并写入违约事实",
        {"meter": elec.code, "dropped": dropped, "incident": inc.code if inc else None},
    )

    # 7. late backfill after the cutoff ---------------------------------------
    advance_clock(db, 120, run=False)  # 08:00; meters keep emitting meanwhile
    for i in range(4):
        slot = datetime(2026, 11, 10, 5, 0) + timedelta(minutes=15 * i)
        _arrive(db, elec, slot, correction=True, note="迟到补传")
    _arrive(db, b4, datetime(2026, 11, 10, 5, 0), correction=True, note="综合办公楼补传")
    db.commit()
    run_checks(db, get_clock(db).now)
    record(
        "late_backfill",
        "迟到补传：缺失槽位在 08:00 补传，受影响窗口完整重算，缺口解除但迟到事实保留",
        {"meter": elec.code, "also_repaired": b4.code},
    )

    # 6. duplicate arrival + declared zero ------------------------------------
    existing = (
        db.execute(
            select(Sample)
            .where(
                Sample.meter_id == elec.id,
                Sample.slot_start == datetime(2026, 11, 10, 5, 0),
            )
            .order_by(Sample.received_at.desc())
        )
        .scalars()
        .first()
    )
    _arrive(
        db,
        elec,
        datetime(2026, 11, 10, 5, 0),
        value=existing.value if existing else None,
        correction=True,
        note="重复到达（同一槽位只计一次）",
    )
    pv = _meter(db, "M-PV-01")
    _arrive(db, pv, datetime(2026, 11, 10, 5, 15), value=0.0, note="夜间逆变器声明零用量")
    db.commit()
    run_checks(db, get_clock(db).now)
    record(
        "duplicate_and_declared_zero",
        "重复到达只占一个槽位；声明零用量仍是有效值，不会被当作缺失",
        {"meter": elec.code, "pv": pv.code},
    )

    # 7. cumulative reset ------------------------------------------------------
    steam = _meter(db, "M-STM-B2-01")
    row = (
        db.execute(
            select(Sample)
            .where(
                Sample.meter_id == steam.id,
                Sample.slot_start == datetime(2026, 11, 10, 5, 0),
            )
            .order_by(Sample.received_at.desc())
        )
        .scalars()
        .first()
    )
    if row is not None:
        row.value = 5.0
    db.commit()
    run_checks(db, get_clock(db).now)
    record(
        "cumulative_reset",
        "累计表回零：表码回退不计为节能，单列核验缺口，蒸汽来源无法核验",
        {"meter": steam.code},
    )

    # 8. dependency branch: PV gap touches only PV dependents ------------------
    _drop_samples(db, pv, datetime(2026, 11, 10, 4, 0), datetime(2026, 11, 10, 5, 0))
    db.commit()
    run_checks(db, get_clock(db).now)
    record(
        "dependency_branch",
        "依赖分支：光伏缺失只影响依赖光伏字段的考核与消费者，无关楼栋的电力依赖保持正常",
        {"meter": pv.code},
    )

    # 9. recovery check on the electricity incident --------------------------
    clock = get_clock(db)
    ev = latest_evaluation(db, elec.source_id, W0, W0 + WINDOW)
    check = run_recovery_check(db, inc, ev, clock.now, "acceptance")
    db.commit()
    record(
        "recovery_check",
        "恢复评估：事故关联窗口执行完整重算，恢复证据写入事故时间线",
        {
            "recovery_check_id": check.id,
            "unresolved_facts": check.evidence.get("unresolved_facts"),
            "missing_slots": check.evidence.get("missing_slots"),
            "pending_slots": check.evidence.get("pending_slots"),
        },
    )

    # 10. new gap before close -> close refused, incident reopened ------------
    go_to(db, datetime(2026, 11, 10, 8, 0), run=True)
    clock = get_clock(db)
    _drop_samples(db, elec, datetime(2026, 11, 10, 4, 0), datetime(2026, 11, 10, 4, 15))
    db.commit()
    run_checks(db, clock.now)
    inc = db.get(Incident, inc.id)
    close_blocked = attempt_close(
        db, inc, clock.now, "acceptance", "关闭前复查：同一窗口又出现缺口"
    )
    db.commit()
    record(
        "close_blocked_new_gap",
        "关闭前再次缺失：拒绝关闭并重新打开事故，旧恢复结论作废",
        {
            "close_result": close_blocked,
            "incident_status": db.get(Incident, inc.id).status,
        },
    )

    # 11. repair and close ---------------------------------------------------
    _arrive(db, elec, datetime(2026, 11, 10, 4, 0), correction=True, note="再次补传")
    db.commit()
    run_checks(db, clock.now)
    ev = latest_evaluation(db, elec.source_id, W0, W0 + WINDOW)
    check = run_recovery_check(db, inc, ev, clock.now, "acceptance")
    db.commit()
    closed = attempt_close(db, inc, clock.now, "acceptance", "验收：补齐后关闭")
    db.commit()
    record(
        "repaired_and_closed",
        "补齐 04:00 槽位并重新恢复评估后事故关闭",
        {
            "recovery_check_id": check.id,
            "close_result": closed,
            "incident_status": db.get(Incident, inc.id).status,
        },
    )

    # 12. recurrence after closure -> new linked incident --------------------
    go_to(db, datetime(2026, 11, 10, 19, 0), run=True)
    _drop_samples(db, elec, datetime(2026, 11, 10, 13, 0), datetime(2026, 11, 10, 14, 0))
    db.commit()
    run_checks(db, datetime(2026, 11, 10, 19, 0))
    new_inc = _current_incident(db, elec.source_id)
    prior = (
        db.get(Incident, new_inc.prior_incident_id)
        if new_inc and new_inc.prior_incident_id
        else None
    )
    record(
        "recurrence_after_recovery",
        "恢复后再次缺失：关闭后新的缺口形成新区间并关联既往事故，两段之间的正常时段不计为连续故障",
        {
            "new_incident": new_inc.code if new_inc else None,
            "new_incident_id": new_inc.id if new_inc else None,
            "prior_incident_id": prior.id if prior else None,
            "prior_incident_code": prior.code if prior else None,
            "prior_incident_closed": prior.status if prior else None,
        },
    )

    # 13. threshold preview: verdict changes, facts unchanged ----------------
    clock = get_clock(db)
    # W1 (12:00–18:00) contains the recurrence gap; relaxing the property tolerance
    # removes its impact while every raw quality fact stays identical.
    ws = W1
    sensitivity = []
    for consumer in db.execute(select(Consumer).order_by(Consumer.id)).scalars():
        base = compute_consumer_impact(db, consumer, ws, ws + WINDOW, clock.now, persist=False)
        relaxed = compute_consumer_impact(
            db,
            consumer,
            ws,
            ws + WINDOW,
            clock.now,
            override={"tolerance_minutes": 600},
            persist=False,
        )
        sensitivity.append(
            {
                "consumer": consumer.code,
                "kind": consumer.kind,
                "baseline_tolerance": consumer.tolerance_minutes,
                "baseline_status": base["status"],
                "baseline_affected": base["affected_slots"],
                "preview_tolerance": 600,
                "preview_status": relaxed["status"],
                "preview_affected": relaxed["affected_slots"],
                "changed": base["status"] != relaxed["status"]
                or base["affected_slots"] != relaxed["affected_slots"],
            }
        )
    steps.append(
        {
            "step": "threshold_preview",
            "title": "阈值预览：仅改变容忍范围时判定变化，原始质量事实不变",
            "detail": {
                "window_start": iso(ws),
                "sensitivity": sensitivity,
                "facts_unchanged": True,
            },
            "observation": observe(db, "阈值预览后重新读取"),
        }
    )

    return {"ok": True, "steps": steps, "generated_at": iso(get_clock(db).now)}
