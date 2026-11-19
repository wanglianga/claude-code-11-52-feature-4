from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    BreachFact,
    ConsumerImpact,
    HealthEvaluation,
    Incident,
    IncidentEvent,
    Meter,
    RecoveryCheck,
    Source,
)
from ..timeutil import iso, utcnow
from .health import latest_evaluation
from .products import PRODUCT_DAILY, PRODUCT_REALTIME, latest_product

OPEN_STATUSES = ("open", "investigating", "recovering", "reopened")


def _add_event(
    inc: Incident, kind: str, at_sim: datetime, message: str, payload: dict[str, Any] | None = None
) -> None:
    inc.events.append(
        IncidentEvent(kind=kind, at_sim=at_sim, message=message, payload=payload or {})
    )


def _facts_for_window(
    db: Session, source_id: int, ws: datetime, we: datetime
) -> list[BreachFact]:
    return (
        db.execute(
            select(BreachFact)
            .where(
                BreachFact.source_id == source_id,
                BreachFact.slot_start >= ws,
                BreachFact.slot_start < we,
            )
            .order_by(BreachFact.slot_start, BreachFact.meter_id)
        )
        .scalars()
        .all()
    )


def _ranges_from_slots(
    slots: set[datetime], ends: dict[datetime, datetime] | None = None
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for slot in sorted(slots):
        end = (ends or {}).get(slot) or slot + timedelta(minutes=15)
        if out and out[-1]["_end"] == slot:
            out[-1]["to"] = iso(end)
            out[-1]["_end"] = end
            out[-1]["slots"].append(iso(slot))
        else:
            out.append(
                {
                    "from": iso(slot),
                    "to": iso(end),
                    "_end": end,
                    "slots": [iso(slot)],
                }
            )
    for r in out:
        r.pop("_end", None)
    return out


def _merge_ranges(
    ranges: list[dict[str, Any]],
    extra: list[dict[str, Any]],
    ends: dict[datetime, datetime] | None = None,
) -> list[dict[str, Any]]:
    slots: set[datetime] = set()
    end_map = dict(ends or {})
    for r in ranges + extra:
        for s in r.get("slots", []):
            slots.add(datetime.fromisoformat(s.rstrip("Z")))
        if r.get("from") and r.get("to"):
            end_map.setdefault(
                datetime.fromisoformat(r["from"].rstrip("Z")),
                datetime.fromisoformat(r["to"].rstrip("Z")),
            )
        if not r.get("slots") and r.get("from") and r.get("to"):
            cur = datetime.fromisoformat(r["from"].rstrip("Z"))
            end = datetime.fromisoformat(r["to"].rstrip("Z"))
            while cur < end:
                slots.add(cur)
                cur += timedelta(minutes=15)
    return _ranges_from_slots(slots, end_map)


def _next_code(db: Session, source: Source, at_sim: datetime) -> str:
    day = at_sim.strftime("%Y%m%d")
    prefix = f"INC-{day}-{source.code[:3].upper()}-"
    existing = (
        db.execute(select(Incident.code).where(Incident.code.like(prefix + "%")))
        .scalars()
        .all()
    )
    seq = len(existing) + 1
    code = f"{prefix}{seq:03d}"
    while code in existing:
        seq += 1
        code = f"{prefix}{seq:03d}"
    return code


def reconcile_incidents(
    db: Session,
    source: Source,
    evaluation: HealthEvaluation,
    at_sim: datetime,
    actor: str = "scheduler",
) -> list[Incident]:
    """Open / extend / reopen incidents from the latest evaluation facts.

    Key invariant: a closed incident is never silently extended. New breaches after
    closure create a new interval that links back to the prior incident.
    """
    ws, we = evaluation.window_start, evaluation.window_end
    facts = _facts_for_window(db, source.id, ws, we)
    open_facts = [f for f in facts if f.resolved_at is None]
    touched: list[Incident] = []

    active = (
        db.execute(
            select(Incident)
            .where(
                Incident.source_id == source.id,
                Incident.status.in_(OPEN_STATUSES),
            )
            .order_by(Incident.id.desc())
        )
        .scalars()
        .all()
    )
    active_inc = active[0] if active else None

    if not open_facts:
        if active_inc is not None:
            pending_eval = evaluation.status == "pending" or bool(
                (evaluation.details or {}).get("pending")
            )
            if not pending_eval:
                # An incident may only become "recovering" when every fact in *its own*
                # ranges (across all windows) is resolved — not merely because this
                # particular window happens to be clean.
                own_slots = [
                    datetime.fromisoformat(s.rstrip("Z"))
                    for r in (active_inc.ranges or [])
                    for s in r.get("slots", [])
                ]
                overlaps = any(ws <= s < we for s in own_slots)
                unresolved_own = []
                if overlaps and own_slots:
                    unresolved_own = (
                        db.execute(
                            select(BreachFact).where(
                                BreachFact.incident_id == active_inc.id,
                                BreachFact.resolved_at.is_(None),
                            )
                        )
                        .scalars()
                        .all()
                    )
                if (
                    overlaps
                    and not unresolved_own
                    and evaluation.coverage_den
                    and evaluation.coverage_num == evaluation.coverage_den
                    and evaluation.verification_ok
                ):
                    if active_inc.status != "recovering":
                        active_inc.status = "recovering"
                        active_inc.updated_at = utcnow()
                        _add_event(
                            active_inc,
                            "recovery_candidate",
                            at_sim,
                            "事故区间内事实全部解除，等待关闭前恢复评估",
                            {
                                "evaluation_id": evaluation.id,
                                "revision": evaluation.revision,
                                "coverage_num": evaluation.coverage_num,
                                "coverage_den": evaluation.coverage_den,
                            },
                        )
                        touched.append(active_inc)
        return touched

    # group by cause so missing-data and verification incidents stay distinct
    for cause in sorted({f.kind for f in open_facts}):
        # "bridge" verification gaps are a downstream consequence of missing slots and
        # are already covered by the data-gap incident, so they do not open a new one.
        if cause == "verification" and all(
            f.reason == "bridge" for f in open_facts if f.kind == "verification"
        ) and any(f.kind == "missing" for f in open_facts):
            continue
        cause_facts = [
            f
            for f in open_facts
            if f.kind == cause
            and not (cause == "verification" and f.reason == "bridge")
        ]
        if not cause_facts:
            continue
        slots = {f.slot_start for f in cause_facts}
        ends = {f.slot_start: f.slot_end for f in cause_facts}
        meters = sorted({f.meter_id for f in cause_facts})
        ranges = _ranges_from_slots(slots, ends)
        first = min(slots)
        last = max(slots)

        incident = (
            db.execute(
                select(Incident)
                .where(
                    Incident.source_id == source.id,
                    Incident.status.in_(OPEN_STATUSES),
                    Incident.cause_category
                    == ("data_gap" if cause == "missing" else "verification"),
                )
                .order_by(Incident.id.desc())
            )
            .scalars()
            .first()
        )

        evidence = [
            {
                "kind": "breach_fact",
                "fact_id": f.id,
                "meter_id": f.meter_id,
                "meter_code": next(
                    (m.code for m in db.execute(select(Meter).where(Meter.id == f.meter_id)).scalars()),
                    str(f.meter_id),
                ),
                "slot": iso(f.slot_start),
                "reason": f.reason,
                "note": f.note,
            }
            for f in cause_facts[:200]
        ]
        window_evidence = {
            "kind": "evaluation",
            "evaluation_id": evaluation.id,
            "revision": evaluation.revision,
            "window_start": iso(ws),
            "window_end": iso(we),
            "data_cutoff": iso(evaluation.data_cutoff),
            "coverage_num": evaluation.coverage_num,
            "coverage_den": evaluation.coverage_den,
            "verification_ok": evaluation.verification_ok,
        }

        if incident is None:
            prior = (
                db.execute(
                    select(Incident)
                    .where(
                        Incident.source_id == source.id,
                        Incident.status.in_(("closed",)),
                        Incident.cause_category
                        == ("data_gap" if cause == "missing" else "verification"),
                    )
                    .order_by(Incident.id.desc())
                )
                .scalars()
                .first()
            )
            seq = len(
                db.execute(select(Incident).where(Incident.source_id == source.id))
                .scalars()
                .all()
            ) + 1
            incident = Incident(
                code=_next_code(db, source, at_sim),
                source_id=source.id,
                cause_category="data_gap" if cause == "missing" else "verification",
                title=(
                    f"{source.name} · 缺失数据"
                    if cause == "missing"
                    else f"{source.name} · 核验缺口"
                ),
                summary=(
                    f"{len(meters)} 个表计在 {len(slots)} 个槽位出现"
                    + ("采样缺失" if cause == "missing" else "核验缺口（回零/换表/跨缺口）")
                ),
                status="reopened" if prior else "open",
                opened_at_sim=at_sim,
                first_breach_at=first,
                last_breach_at=last,
                ranges=ranges,
                evidence=evidence + [window_evidence],
                prior_incident_id=prior.id if prior else None,
            )
            db.add(incident)
            db.flush()
            if prior:
                _add_event(
                    incident,
                    "reopened_as_new_incident",
                    at_sim,
                    f"相同原因在事故 {prior.code} 关闭后再次出现，建立新区间并关联既往事故",
                    {"prior_incident_code": prior.code, "prior_closed_at": iso(prior.closed_at_sim)},
                )
                _add_event(
                    prior,
                    "recurrence_linked",
                    at_sim,
                    f"关闭后再次缺失，已建立后续事故 {incident.code}",
                    {"new_incident_code": incident.code},
                )
            _add_event(
                incident,
                "opened",
                at_sim,
                "评估发现未解决缺口，事故打开",
                {
                    "window_start": iso(ws),
                    "window_end": iso(we),
                    "ranges": ranges,
                    "meters": meters,
                },
            )
            for f in cause_facts:
                f.incident_id = incident.id
            touched.append(incident)
        else:
            new_slots = {f.slot_start for f in cause_facts}
            prev_slots = {
                datetime.fromisoformat(s.rstrip("Z"))
                for r in incident.ranges
                for s in r.get("slots", [])
            }
            incident.ranges = _merge_ranges(incident.ranges, ranges, ends)
            incident.evidence = evidence + [window_evidence]
            incident.first_breach_at = min(incident.first_breach_at, first)
            incident.last_breach_at = max(incident.last_breach_at, last)
            incident.updated_at = utcnow()
            for f in cause_facts:
                if f.incident_id is None:
                    f.incident_id = incident.id
            if incident.status == "recovering":
                incident.status = "open"
                _add_event(
                    incident,
                    "reopened",
                    at_sim,
                    "关闭前复查发现新的未解决缺口，事故重新打开，旧恢复结论作废",
                    {
                        "evaluation_id": evaluation.id,
                        "new_slots": sorted(iso(s) for s in new_slots - prev_slots)[:20],
                    },
                )
            else:
                _add_event(
                    incident,
                    "extended",
                    at_sim,
                    "新增缺口区间，事故范围扩展",
                    {
                        "evaluation_id": evaluation.id,
                        "added_slots": sorted(iso(s) for s in new_slots - prev_slots)[:20],
                    },
                )
            touched.append(incident)

    return touched


def run_recovery_check(
    db: Session,
    incident: Incident,
    evaluation: HealthEvaluation,
    at_sim: datetime,
    actor: str,
) -> RecoveryCheck:
    """Full-window re-evaluation evidence. Only this can support closing an incident."""
    facts = _facts_for_window(db, incident.source_id, evaluation.window_start, evaluation.window_end)
    unresolved = [f for f in facts if f.resolved_at is None]
    resolved = [f for f in facts if f.resolved_at is not None]
    details = evaluation.details or {}
    check = RecoveryCheck(
        incident_id=incident.id,
        evaluation_id=evaluation.id,
        window_start=evaluation.window_start,
        window_end=evaluation.window_end,
        data_cutoff=evaluation.data_cutoff,
        status="completed",
        evidence={
            "evaluation_id": evaluation.id,
            "revision": evaluation.revision,
            "coverage_num": evaluation.coverage_num,
            "coverage_den": evaluation.coverage_den,
            "verification_ok": evaluation.verification_ok,
            "certified": evaluation.certified,
            "pending_slots": len(details.get("pending", [])),
            "missing_slots": len(details.get("missing", [])),
            "unresolved_facts": len(unresolved),
            "resolved_facts": len(resolved),
            "facts": [
                {
                    "fact_id": f.id,
                    "meter_id": f.meter_id,
                    "slot": iso(f.slot_start),
                    "kind": f.kind,
                    "resolved_at": iso(f.resolved_at),
                    "resolved_by_sample_id": f.resolved_by_sample_id,
                    "retroactive_maintenance_id": f.retroactive_maintenance_id,
                }
                for f in facts[:400]
            ],
        },
    )
    db.add(check)
    db.flush()
    incident.status = "recovering"
    incident.updated_at = utcnow()
    _add_event(
        incident,
        "recovery_check_completed",
        at_sim,
        "完成窗口完整重算恢复评估",
        {
            "check_id": check.id,
            "evaluation_id": evaluation.id,
            "unresolved_facts": len(unresolved),
            "pending_slots": len(details.get("pending", [])),
            "missing_slots": len(details.get("missing", [])),
        },
    )
    return check


def attempt_close(
    db: Session,
    incident: Incident,
    at_sim: datetime,
    actor: str,
    note: str = "",
) -> dict[str, Any]:
    """Close only with a completed recovery check AND no new unresolved gap."""
    if incident.status == "closed":
        return {"ok": False, "error": "事故已关闭", "incident_id": incident.id}
    check = (
        db.execute(
            select(RecoveryCheck)
            .where(RecoveryCheck.incident_id == incident.id)
            .order_by(RecoveryCheck.id.desc())
        )
        .scalars()
        .first()
    )
    if check is None:
        return {
            "ok": False,
            "error": "缺少已完成的恢复评估，不能关闭事故",
            "incident_id": incident.id,
        }

    current = latest_evaluation(db, incident.source_id, check.window_start, check.window_end)
    if current is None:
        return {"ok": False, "error": "无法读取最新评估", "incident_id": incident.id}

    # A new gap may appear in *any* window covered by this incident, not only the one
    # the recovery check ran on. Check the check window plus every slot in the
    # incident's recorded ranges across all windows.
    facts = _facts_for_window(db, incident.source_id, check.window_start, check.window_end)
    range_slots: set[datetime] = set()
    for r in incident.ranges or []:
        for s in r.get("slots", []):
            range_slots.add(datetime.fromisoformat(s.rstrip("Z")))
    if range_slots:
        spanned = (
            db.execute(
                select(BreachFact).where(
                    BreachFact.source_id == incident.source_id,
                    BreachFact.slot_start >= min(range_slots),
                    BreachFact.slot_start <= max(range_slots),
                )
            )
            .scalars()
            .all()
        )
        merged: dict[int, BreachFact] = {f.id: f for f in facts}
        for f in spanned:
            merged[f.id] = f
        facts = list(merged.values())
    open_facts = [f for f in facts if f.resolved_at is None]
    if open_facts:
        # A new gap appeared after the recovery check: reopen instead of closing.
        incident.status = "open"
        incident.updated_at = utcnow()
        _add_event(
            incident,
            "close_blocked_new_gap",
            at_sim,
            "关闭前复查发现新的未解决缺口，拒绝关闭并重新打开事故",
            {
                "open_facts": [
                    {"fact_id": f.id, "slot": iso(f.slot_start), "meter_id": f.meter_id}
                    for f in open_facts[:50]
                ]
            },
        )
        return {
            "ok": False,
            "error": "关闭前复查发现新的未解决缺口，事故已重新打开",
            "incident_id": incident.id,
            "status": incident.status,
        }

    stale = (
        current.id != check.evaluation_id
        and (
            (current.details or {}).get("counts", {}).get("missing", 0) > 0
            or not current.verification_ok
        )
    )
    if stale:
        incident.status = "reopened"
        incident.updated_at = utcnow()
        _add_event(
            incident,
            "close_blocked_stale_check",
            at_sim,
            "恢复评估后质量事实再次变化，旧恢复结论不能沿用",
            {"current_evaluation_id": current.id, "check_evaluation_id": check.evaluation_id},
        )
        return {
            "ok": False,
            "error": "恢复评估已过期，请重新执行恢复评估",
            "incident_id": incident.id,
            "status": incident.status,
        }

    incident.status = "closed"
    incident.closed_at_sim = at_sim
    incident.recovery_check_id = check.id
    incident.updated_at = utcnow()
    _add_event(
        incident,
        "closed",
        at_sim,
        f"事故关闭：恢复评估 #{check.id} 通过，窗口内无未解决缺口。{note}".strip(),
        {"check_id": check.id, "actor": actor},
    )
    return {"ok": True, "incident_id": incident.id, "status": "closed", "check_id": check.id}


def sync_impacts_into_incidents(
    db: Session,
    source: Source,
    evaluation: HealthEvaluation,
    at_sim: datetime,
) -> None:
    """Attach downstream consumer impact to open incidents for traceability."""
    incs = (
        db.execute(
            select(Incident).where(
                Incident.source_id == source.id, Incident.status.in_(OPEN_STATUSES)
            )
        )
        .scalars()
        .all()
    )
    if not incs:
        return
    impacts = (
        db.execute(
            select(ConsumerImpact).where(
                ConsumerImpact.window_start == evaluation.window_start,
                ConsumerImpact.window_end == evaluation.window_end,
            )
        )
        .scalars()
        .all()
    )
    latest: dict[int, ConsumerImpact] = {}
    for imp in impacts:
        if imp.consumer_id not in latest or imp.revision >= latest[imp.consumer_id].revision:
            latest[imp.consumer_id] = imp
    affected = [
        {
            "consumer_id": imp.consumer_id,
            "status": imp.status,
            "severity": imp.severity,
            "affected_slots": imp.affected_slots,
            "ranges": imp.ranges,
        }
        for imp in latest.values()
        if imp.status != "ok"
    ]
    for inc in incs:
        inc.evidence = [
            e for e in (inc.evidence or []) if e.get("kind") != "consumer_impact"
        ] + [{"kind": "consumer_impact", "at": iso(at_sim), "impacts": affected}]
        inc.updated_at = utcnow()
        if affected:
            _add_event(
                inc,
                "consumer_impact",
                at_sim,
                f"{len(affected)} 个消费者受到不同阈值下的影响",
                {"impacts": affected},
            )
    db.flush()
