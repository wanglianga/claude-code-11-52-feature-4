from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    AuditLog,
    BreachFact,
    IncidentEvent,
    Building,
    Consumer,
    ConsumerDependency,
    ConsumerImpact,
    HealthEvaluation,
    Incident,
    MaintenanceWindow,
    Meter,
    MeterCommitment,
    ProductRun,
    RecoveryCheck,
    Sample,
    SimClock,
    Source,
    User,
)
from ..services import simulator
from ..services.health import evaluation_at_cutoff, evaluate_window, latest_evaluation
from ..services.impact import compute_consumer_impact
from ..services.incidents import (
    attempt_close,
    reconcile_incidents,
    run_recovery_check,
    sync_impacts_into_incidents,
)
from ..services.products import PRODUCT_DAILY, PRODUCT_META, PRODUCT_REALTIME, latest_product
from ..timeutil import WINDOW, iso, slot_floor, utcnow, window_floor, windows_of_day

router = APIRouter(prefix="/api")


def _password_hash(raw: str) -> str:
    return hashlib.sha256(("slo-demo::" + raw).encode()).hexdigest()


def _parse_dt(value: str, field: str = "时间") -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", ""))
    except ValueError as exc:
        raise HTTPException(400, f"{field} 格式无效: {value}") from exc


def clock_payload(db: Session) -> dict[str, Any]:
    clock = simulator.get_clock(db)
    return {
        "now": iso(clock.now),
        "day": clock.day,
        "revision": clock.revision,
        "windows": [iso(w) for w in windows_of_day(clock.day) if w + WINDOW <= clock.now],
        "window_hours": int(WINDOW.total_seconds() // 3600),
    }


# ------------------------------------------------------------------ auth


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/auth/login", tags=["meta"])
def login(body: LoginIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    user = (
        db.execute(select(User).where(User.username == body.username)).scalars().first()
    )
    if user is None or user.password_hash != _password_hash(body.password):
        raise HTTPException(401, "用户名或密码错误")
    return {
        "token": f"demo-{user.username}-{user.role}",
        "user": {
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
        },
    }


# ------------------------------------------------------------------ meta


@router.get("/meta", tags=["meta"])
def meta(db: Session = Depends(get_db)) -> dict[str, Any]:
    buildings = db.execute(select(Building).order_by(Building.code)).scalars().all()
    sources = db.execute(select(Source).order_by(Source.id)).scalars().all()
    consumers = db.execute(select(Consumer).order_by(Consumer.id)).scalars().all()
    return {
        "clock": clock_payload(db),
        "buildings": [
            {"code": b.code, "name": b.name, "kind": b.kind} for b in buildings
        ],
        "sources": [
            {
                "code": s.code,
                "name": s.name,
                "unit": s.unit,
                "product_field": s.product_field,
            }
            for s in sources
        ],
        "products": PRODUCT_META,
        "consumers": [
            {
                "code": c.code,
                "name": c.name,
                "kind": c.kind,
                "description": c.description,
                "tolerance_minutes": c.tolerance_minutes,
                "min_coverage": c.min_coverage,
                "max_gap_slots": c.max_gap_slots,
                "require_certified": c.require_certified,
            }
            for c in consumers
        ],
    }


# ------------------------------------------------------------------ registry


@router.get("/sources", tags=["registry"])
def list_sources(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    out = []
    for source in db.execute(select(Source).order_by(Source.id)).scalars():
        commitments: list[dict[str, Any]] = []
        meters_out = []
        for meter in sorted(source.meters, key=lambda m: m.code):
            rows = (
                db.execute(
                    select(MeterCommitment)
                    .where(MeterCommitment.meter_id == meter.id)
                    .order_by(MeterCommitment.valid_from)
                )
                .scalars()
                .all()
            )
            for c in rows:
                commitments.append(
                    {
                        "commitment_id": c.id,
                        "meter_id": meter.id,
                        "meter_code": meter.code,
                        "valid_from": iso(c.valid_from),
                        "valid_to": iso(c.valid_to),
                        "cadence_minutes": c.cadence_minutes,
                        "allowed_lateness_minutes": c.allowed_lateness_minutes,
                        "certification_required": c.certification_required,
                        "note": c.note,
                    }
                )
            maint = (
                db.execute(
                    select(MaintenanceWindow)
                    .where(MaintenanceWindow.meter_id == meter.id)
                    .order_by(MaintenanceWindow.starts_at)
                )
                .scalars()
                .all()
            )
            active = next(
                (
                    c
                    for c in rows
                    if c.valid_from <= utcnow() and (c.valid_to is None or c.valid_to > utcnow())
                ),
                rows[-1] if rows else None,
            )
            meters_out.append(
                {
                    "id": meter.id,
                    "code": meter.code,
                    "name": meter.name,
                    "building_code": meter.building.code,
                    "building_name": meter.building.name,
                    "cumulative": meter.cumulative,
                    "multiplier": meter.multiplier,
                    "status": meter.status,
                    "linked_from_meter_id": meter.linked_from_meter_id,
                    "link_ratio": meter.link_ratio,
                    "cadence_minutes": active.cadence_minutes if active else None,
                    "allowed_lateness_minutes": (
                        active.allowed_lateness_minutes if active else None
                    ),
                    "valid_from": iso(active.valid_from) if active else None,
                    "valid_to": iso(active.valid_to) if active else None,
                    "maintenance": [
                        {
                            "id": m.id,
                            "starts_at": iso(m.starts_at),
                            "ends_at": iso(m.ends_at),
                            "allowed_deduction": m.allowed_deduction,
                            "recorded_at": iso(m.recorded_at),
                            "reason": m.reason,
                        }
                        for m in maint
                    ],
                }
            )
        product_dependencies = []
        for consumer in db.execute(select(Consumer).order_by(Consumer.id)).scalars():
            for dep in consumer.dependencies:
                if dep.product_field != source.product_field:
                    continue
                product_dependencies.append(
                    {
                        "product_code": dep.product_code,
                        "product_name": PRODUCT_META.get(dep.product_code, {}).get(
                            "name", dep.product_code
                        ),
                        "consumer_code": consumer.code,
                        "consumer_name": consumer.name,
                        "building_code": dep.building_code,
                        "valid_from": iso(dep.valid_from),
                        "valid_to": iso(dep.valid_to),
                    }
                )
        out.append(
            {
                "id": source.id,
                "code": source.code,
                "name": source.name,
                "unit": source.unit,
                "product_field": source.product_field,
                "description": source.description,
                "meters": meters_out,
                "commitments": commitments,
                "product_dependencies": product_dependencies,
            }
        )
    return out


@router.post("/sources/{source_code}/maintenance", tags=["registry"])
def create_maintenance(
    source_code: str, body: dict[str, Any], db: Session = Depends(get_db)
) -> dict[str, Any]:
    meter = (
        db.execute(select(Meter).where(Meter.code == body.get("meter")))
        .scalars()
        .first()
    )
    if meter is None or meter.source.code != source_code:
        raise HTTPException(404, "表计不存在于该数据来源")
    result = simulator.add_maintenance(
        db,
        meter,
        _parse_dt(body["from"], "开始时间"),
        _parse_dt(body["to"], "结束时间"),
        allowed_deduction=bool(body.get("allowed_deduction", True)),
        reason=body.get("reason", "计划维护"),
        recorded_at=_parse_dt(body["recorded_at"], "登记时间") if body.get("recorded_at") else None,
    )
    return result


@router.post("/sources/{source_code}/commitments", tags=["registry"])
def create_commitment(
    source_code: str, body: dict[str, Any], db: Session = Depends(get_db)
) -> dict[str, Any]:
    meter = (
        db.execute(select(Meter).where(Meter.code == body.get("meter")))
        .scalars()
        .first()
    )
    if meter is None or meter.source.code != source_code:
        raise HTTPException(404, "表计不存在于该数据来源")
    return simulator.add_commitment(
        db,
        meter,
        _parse_dt(body["valid_from"], "生效时间"),
        cadence_minutes=int(body["cadence_minutes"]),
        allowed_lateness_minutes=int(body.get("allowed_lateness_minutes", 30)),
        note=body.get("note", ""),
    )


# ------------------------------------------------------------------ health


@router.get("/health/evaluations", tags=["health"])
def list_evaluations(
    db: Session = Depends(get_db),
    day: str | None = None,
    source: str | None = None,
    latest_only: bool = True,
) -> list[dict[str, Any]]:
    clock = simulator.get_clock(db)
    target_day = day or clock.day
    ws_list = windows_of_day(target_day)
    out = []
    for ws in ws_list:
        stmt = select(HealthEvaluation).where(
            HealthEvaluation.window_start == ws,
            HealthEvaluation.window_end == ws + WINDOW,
        )
        if source:
            src = (
                db.execute(select(Source).where(Source.code == source)).scalars().first()
            )
            if src is None:
                raise HTTPException(404, "数据来源不存在")
            stmt = stmt.where(HealthEvaluation.source_id == src.id)
        rows = db.execute(stmt.order_by(HealthEvaluation.source_id, HealthEvaluation.revision)).scalars().all()
        by_source: dict[int, list[HealthEvaluation]] = {}
        for ev in rows:
            by_source.setdefault(ev.source_id, []).append(ev)
        for evs in by_source.values():
            if latest_only:
                evs = [evs[-1]]
            for ev in evs:
                # A window that has not had a single slot come due yet carries no
                # evidence; skip it rather than showing a meaningless 0/0.
                if ev.coverage_den == 0 and ev.status == "pending":
                    continue
                out.append(_eval_summary(db, ev))
    return out


def _eval_summary(db: Session, ev: HealthEvaluation) -> dict[str, Any]:
    source = db.get(Source, ev.source_id)
    details = ev.details or {}
    return {
        "id": ev.id,
        "source_code": source.code if source else None,
        "source_name": source.name if source else None,
        "unit": source.unit if source else None,
        "product_field": source.product_field if source else None,
        "window_start": iso(ev.window_start),
        "window_end": iso(ev.window_end),
        "data_cutoff": iso(ev.data_cutoff),
        "revision": ev.revision,
        "status": ev.status,
        "freshness": ev.freshness,
        "coverage_num": ev.coverage_num,
        "coverage_den": ev.coverage_den,
        "coverage": round(ev.coverage_num / ev.coverage_den, 4) if ev.coverage_den else None,
        "coverage_ok": ev.coverage_ok,
        "verification_ok": ev.verification_ok,
        "certified": ev.certified,
        "confirmable": ev.confirmable,
        "recompute_count": ev.recompute_count,
        "max_lateness_minutes": ev.max_lateness_minutes,
        "counts": details.get("counts", {}),
        "freshness_minutes": details.get("freshness_minutes"),
        "buildings": details.get("building_summary", []),
    }


@router.get("/health/evaluations/{evaluation_id}", tags=["health"])
def get_evaluation(evaluation_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    ev = db.get(HealthEvaluation, evaluation_id)
    if ev is None:
        raise HTTPException(404, "评估不存在")
    data = _eval_summary(db, ev)
    data["details"] = ev.details
    chain = (
        db.execute(
            select(HealthEvaluation)
            .where(
                HealthEvaluation.source_id == ev.source_id,
                HealthEvaluation.window_start == ev.window_start,
                HealthEvaluation.window_end == ev.window_end,
            )
            .order_by(HealthEvaluation.revision)
        )
        .scalars()
        .all()
    )
    data["revisions"] = [
        {
            "id": e.id,
            "revision": e.revision,
            "data_cutoff": iso(e.data_cutoff),
            "status": e.status,
            "coverage_num": e.coverage_num,
            "coverage_den": e.coverage_den,
            "certified": e.certified,
            "computed_at": iso(e.computed_at),
            "recompute_count": e.recompute_count,
        }
        for e in chain
    ]
    facts = (
        db.execute(
            select(BreachFact)
            .where(
                BreachFact.source_id == ev.source_id,
                BreachFact.slot_start >= ev.window_start,
                BreachFact.slot_start < ev.window_end,
            )
            .order_by(BreachFact.slot_start)
        )
        .scalars()
        .all()
    )
    data["facts"] = [
        {
            "id": f.id,
            "meter_id": f.meter_id,
            "slot": iso(f.slot_start),
            "kind": f.kind,
            "reason": f.reason,
            "detected_at": iso(f.detected_at),
            "resolved_at": iso(f.resolved_at),
            "retroactive_maintenance_id": f.retroactive_maintenance_id,
            "incident_id": f.incident_id,
            "note": f.note,
        }
        for f in facts
    ]
    return data


@router.post("/health/reevaluate", tags=["health"])
def reevaluate(body: dict[str, Any] | None = None, db: Session = Depends(get_db)) -> dict[str, Any]:
    body = body or {}
    return simulator.reevaluate_day(db, day=body.get("day"), cutoff=None)


@router.get("/health/evaluations/{evaluation_id}/at-cutoff", tags=["health"])
def evaluation_snapshot(
    evaluation_id: int, cutoff: str = Query(...), db: Session = Depends(get_db)
) -> dict[str, Any]:
    ev = db.get(HealthEvaluation, evaluation_id)
    if ev is None:
        raise HTTPException(404, "评估不存在")
    snap = evaluation_at_cutoff(
        db, ev.source_id, ev.window_start, ev.window_end, _parse_dt(cutoff, "截止点")
    )
    if snap is None:
        return {"found": False}
    return {"found": True, "evaluation": _eval_summary(db, snap), "details": snap.details}


# ------------------------------------------------------------------ products


@router.get("/products", tags=["products"])
def list_products(
    db: Session = Depends(get_db), day: str | None = None
) -> dict[str, Any]:
    clock = simulator.get_clock(db)
    target_day = day or clock.day
    sources = db.execute(select(Source).order_by(Source.id)).scalars().all()
    out: list[dict[str, Any]] = []
    for ws in windows_of_day(target_day):
        for code, meta in PRODUCT_META.items():
            for source in sources:
                run = latest_product(db, code, source.product_field, ws, ws + WINDOW)
                if run is None:
                    continue
                # Nothing due yet in a running window: expose no product version.
                if not (run.output or {}).get("series") and not (run.output or {}).get(
                    "slot_ends"
                ):
                    continue
                out.append(
                    {
                        "id": run.id,
                        "product_code": code,
                        "product_name": meta["name"],
                        "product_field": source.product_field,
                        "source_name": source.name,
                        "window_start": iso(run.window_start),
                        "window_end": iso(run.window_end),
                        "data_cutoff": iso(run.data_cutoff),
                        "version": run.version,
                        "revision": run.revision,
                        "status": run.status,
                        "qualified_ranges": run.qualified_ranges,
                        "total": (run.output or {}).get("total"),
                        "unit": (run.output or {}).get("unit"),
                        "provisional_from": (run.output or {}).get("provisional_from"),
                    }
                )
    return {"clock": clock_payload(db), "runs": out}


@router.get("/products/{run_id}", tags=["products"])
def get_product(run_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.get(ProductRun, run_id)
    if run is None:
        raise HTTPException(404, "产品运行不存在")
    chain = (
        db.execute(
            select(ProductRun)
            .where(
                ProductRun.product_code == run.product_code,
                ProductRun.product_field == run.product_field,
                ProductRun.window_start == run.window_start,
                ProductRun.window_end == run.window_end,
            )
            .order_by(ProductRun.revision)
        )
        .scalars()
        .all()
    )
    source = (
        db.execute(select(Source).where(Source.product_field == run.product_field))
        .scalars()
        .first()
    )
    refs = []
    for consumer in db.execute(select(Consumer).order_by(Consumer.id)).scalars():
        for dep in consumer.dependencies:
            if dep.product_code != run.product_code or dep.product_field != run.product_field:
                continue
            if dep.valid_from >= run.window_end or (
                dep.valid_to and dep.valid_to <= run.window_start
            ):
                continue
            impact = (
                db.execute(
                    select(ConsumerImpact)
                    .where(
                        ConsumerImpact.consumer_id == consumer.id,
                        ConsumerImpact.window_start == run.window_start,
                        ConsumerImpact.window_end == run.window_end,
                    )
                    .order_by(ConsumerImpact.revision.desc())
                )
                .scalars()
                .first()
            )
            refs.append(
                {
                    "consumer_code": consumer.code,
                    "consumer_name": consumer.name,
                    "kind": consumer.kind,
                    "building_code": dep.building_code,
                    "impact_status": impact.status if impact else "waiting",
                    "tolerance_minutes": consumer.tolerance_minutes,
                    "require_certified": consumer.require_certified,
                }
            )
    return {
        "id": run.id,
        "product_code": run.product_code,
        "product_name": PRODUCT_META.get(run.product_code, {}).get("name", run.product_code),
        "product_field": run.product_field,
        "source_name": source.name if source else None,
        "window_start": iso(run.window_start),
        "window_end": iso(run.window_end),
        "data_cutoff": iso(run.data_cutoff),
        "version": run.version,
        "revision": run.revision,
        "status": run.status,
        "output": run.output,
        "qualified_ranges": run.qualified_ranges,
        "evaluation_ids": run.evaluation_ids,
        "computed_at": iso(run.computed_at),
        "versions": [
            {
                "id": r.id,
                "version": r.version,
                "revision": r.revision,
                "status": r.status,
                "data_cutoff": iso(r.data_cutoff),
                "total": (r.output or {}).get("total"),
            }
            for r in chain
        ],
        "consumers": refs,
    }


# ------------------------------------------------------------------ impact


@router.get("/consumers", tags=["impact"])
def list_consumers(
    db: Session = Depends(get_db), day: str | None = None
) -> list[dict[str, Any]]:
    clock = simulator.get_clock(db)
    target_day = day or clock.day
    consumers = db.execute(select(Consumer).order_by(Consumer.id)).scalars().all()
    out = []
    for consumer in consumers:
        windows = []
        for ws in windows_of_day(target_day):
            impact = (
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
            if impact:
                windows.append(_impact_summary(consumer, impact))
        out.append(
            {
                "code": consumer.code,
                "name": consumer.name,
                "kind": consumer.kind,
                "description": consumer.description,
                "thresholds": {
                    "tolerance_minutes": consumer.tolerance_minutes,
                    "min_coverage": consumer.min_coverage,
                    "max_gap_slots": consumer.max_gap_slots,
                    "require_certified": consumer.require_certified,
                },
                "dependencies": [
                    {
                        "product_code": d.product_code,
                        "product_field": d.product_field,
                        "building_code": d.building_code,
                        "valid_from": iso(d.valid_from),
                        "valid_to": iso(d.valid_to),
                    }
                    for d in consumer.dependencies
                ],
                "windows": windows,
            }
        )
    return out


def _impact_summary(consumer: Consumer, impact: ConsumerImpact) -> dict[str, Any]:
    facts = impact.facts or {}
    return {
        "id": impact.id,
        "consumer_code": consumer.code,
        "consumer_name": consumer.name,
        "kind": consumer.kind,
        "window_start": iso(impact.window_start),
        "window_end": iso(impact.window_end),
        "data_cutoff": iso(impact.data_cutoff),
        "revision": impact.revision,
        "status": impact.status,
        "severity": impact.severity,
        "affected_slots": impact.affected_slots,
        "coverage": facts.get("coverage"),
        "den": facts.get("den"),
        "affected_buildings": facts.get("affected_buildings", []),
        "ranges": impact.ranges,
        "thresholds": impact.threshold_snapshot,
        "reason_count": len(impact.reasons),
    }


@router.get("/consumers/{code}", tags=["impact"])
def get_consumer(
    code: str,
    db: Session = Depends(get_db),
    day: str | None = None,
    window_start: str | None = None,
) -> dict[str, Any]:
    consumer = (
        db.execute(select(Consumer).where(Consumer.code == code)).scalars().first()
    )
    if consumer is None:
        raise HTTPException(404, "消费者不存在")
    clock = simulator.get_clock(db)
    target_day = day or clock.day
    ws_list = (
        [window_floor(_parse_dt(window_start, "窗口开始"))]
        if window_start
        else windows_of_day(target_day)
    )
    windows = []
    for ws in ws_list:
        impact = (
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
        if impact is None:
            continue
        item = _impact_summary(consumer, impact)
        item["reasons"] = impact.reasons
        item["dependencies"] = (impact.facts or {}).get("dependencies", [])
        windows.append(item)
    return {
        "code": consumer.code,
        "name": consumer.name,
        "kind": consumer.kind,
        "description": consumer.description,
        "thresholds": {
            "tolerance_minutes": consumer.tolerance_minutes,
            "min_coverage": consumer.min_coverage,
            "max_gap_slots": consumer.max_gap_slots,
            "require_certified": consumer.require_certified,
        },
        "dependencies": [
            {
                "product_code": d.product_code,
                "product_field": d.product_field,
                "building_code": d.building_code,
            }
            for d in consumer.dependencies
        ],
        "windows": windows,
    }


@router.post("/consumers/{code}/preview", tags=["impact"])
def preview_consumer(
    code: str, body: dict[str, Any], db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Threshold preview: raw quality facts stay fixed, only the verdict may change."""
    consumer = (
        db.execute(select(Consumer).where(Consumer.code == code)).scalars().first()
    )
    if consumer is None:
        raise HTTPException(404, "消费者不存在")
    window_start = body.get("window_start")
    if not window_start:
        clock = simulator.get_clock(db)
        completed = [w for w in windows_of_day(clock.day) if w + WINDOW <= clock.now]
        ws = completed[-1] if completed else clock.now - WINDOW
    else:
        ws = _parse_dt(window_start, "窗口开始")
    override = {
        "tolerance_minutes": body.get("tolerance_minutes"),
        "min_coverage": body.get("min_coverage"),
        "max_gap_slots": body.get("max_gap_slots"),
        "require_certified": body.get("require_certified"),
    }
    current = compute_consumer_impact(
        db, consumer, ws, ws + WINDOW, ws + WINDOW, persist=False
    )
    preview = compute_consumer_impact(
        db,
        consumer,
        ws,
        ws + WINDOW,
        ws + WINDOW,
        override=override,
        persist=False,
    )
    changed_deps = []
    cur_by_dep = {d["dependency_id"]: d for d in current["dependencies"]}
    for dep in preview["dependencies"]:
        prev = cur_by_dep.get(dep["dependency_id"], {})
        if prev.get("status") != dep.get("status"):
            changed_deps.append(
                {
                    "dependency_id": dep["dependency_id"],
                    "product_code": dep["product_code"],
                    "product_field": dep["product_field"],
                    "building_code": dep["building_code"],
                    "before": prev.get("status"),
                    "after": dep.get("status"),
                    "affected_slots_before": prev.get("affected_slots"),
                    "affected_slots_after": dep.get("affected_slots"),
                }
            )
    return {
        "window_start": iso(ws),
        "window_end": iso(ws + WINDOW),
        "current": current,
        "preview": preview,
        "changed": current["status"] != preview["status"]
        or any(c["before"] != c["after"] for c in changed_deps),
        "changed_dependencies": changed_deps,
        "facts_unchanged": True,
    }


# ------------------------------------------------------------------ incidents


def _incident_payload(db: Session, inc: Incident, detail: bool = False) -> dict[str, Any]:
    source = db.get(Source, inc.source_id)
    data = {
        "id": inc.id,
        "code": inc.code,
        "source_code": source.code if source else None,
        "source_name": source.name if source else None,
        "cause_category": inc.cause_category,
        "title": inc.title,
        "summary": inc.summary,
        "status": inc.status,
        "opened_at": iso(inc.opened_at_sim),
        "closed_at": iso(inc.closed_at_sim),
        "first_breach_at": iso(inc.first_breach_at),
        "last_breach_at": iso(inc.last_breach_at),
        "ranges": inc.ranges,
        "prior_incident_id": inc.prior_incident_id,
        "recovery_check_id": inc.recovery_check_id,
        "event_count": len(inc.events),
    }
    if detail:
        data["events"] = [
            {
                "id": e.id,
                "kind": e.kind,
                "at": iso(e.at_sim),
                "message": e.message,
                "payload": e.payload,
            }
            for e in inc.events
        ]
        data["evidence"] = inc.evidence
        checks = (
            db.execute(
                select(RecoveryCheck)
                .where(RecoveryCheck.incident_id == inc.id)
                .order_by(RecoveryCheck.id)
            )
            .scalars()
            .all()
        )
        data["recovery_checks"] = [
            {
                "id": c.id,
                "evaluation_id": c.evaluation_id,
                "window_start": iso(c.window_start),
                "window_end": iso(c.window_end),
                "data_cutoff": iso(c.data_cutoff),
                "status": c.status,
                "evidence": c.evidence,
                "created_at": iso(c.created_at),
            }
            for c in checks
        ]
        facts = (
            db.execute(
                select(BreachFact)
                .where(BreachFact.incident_id == inc.id)
                .order_by(BreachFact.slot_start)
            )
            .scalars()
            .all()
        )
        data["facts"] = [
            {
                "id": f.id,
                "meter_id": f.meter_id,
                "slot": iso(f.slot_start),
                "kind": f.kind,
                "resolved_at": iso(f.resolved_at),
                "retroactive_maintenance_id": f.retroactive_maintenance_id,
            }
            for f in facts
        ]
    return data


@router.get("/incidents", tags=["incidents"])
def list_incidents(
    db: Session = Depends(get_db), status: str | None = None
) -> list[dict[str, Any]]:
    stmt = select(Incident).order_by(Incident.id.desc())
    if status:
        stmt = stmt.where(Incident.status == status)
    return [_incident_payload(db, inc) for inc in db.execute(stmt).scalars()]


@router.get("/incidents/{incident_id}", tags=["incidents"])
def get_incident(incident_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    inc = db.get(Incident, incident_id)
    if inc is None:
        raise HTTPException(404, "事故不存在")
    return _incident_payload(db, inc, detail=True)


def _default_window(db: Session) -> tuple[datetime, datetime]:
    clock = simulator.get_clock(db)
    completed = [w for w in windows_of_day(clock.day) if w + WINDOW <= clock.now]
    ws = completed[-1] if completed else clock.now - WINDOW
    return ws, ws + WINDOW


@router.post("/incidents/{incident_id}/recovery-check", tags=["incidents"])
def recovery_check(
    incident_id: int, body: dict[str, Any] | None, db: Session = Depends(get_db)
) -> dict[str, Any]:
    inc = db.get(Incident, incident_id)
    if inc is None:
        raise HTTPException(404, "事故不存在")
    if inc.status == "closed":
        raise HTTPException(409, "事故已关闭，如需复查请由新事故处理")
    body = body or {}
    clock = simulator.get_clock(db)
    if body.get("window_start"):
        ws = window_floor(_parse_dt(body["window_start"], "窗口开始"))
    else:
        ws = _default_window(db)[0]
    cutoff = _parse_dt(body["cutoff"], "截止点") if body.get("cutoff") else clock.now
    ev = latest_evaluation(db, inc.source_id, ws, ws + WINDOW)
    if ev is None or ev.data_cutoff < cutoff:
        source = db.get(Source, inc.source_id)
        ev = evaluate_window(db, source, ws, ws + WINDOW, cutoff)
    check = run_recovery_check(db, inc, ev, clock.now, body.get("actor", "ops"))
    db.commit()
    return {
        "recovery_check_id": check.id,
        "evidence": check.evidence,
        "incident": _incident_payload(db, inc, detail=True),
    }


@router.post("/incidents/{incident_id}/close", tags=["incidents"])
def close_incident(
    incident_id: int, body: dict[str, Any] | None, db: Session = Depends(get_db)
) -> dict[str, Any]:
    inc = db.get(Incident, incident_id)
    if inc is None:
        raise HTTPException(404, "事故不存在")
    body = body or {}
    clock = simulator.get_clock(db)
    result = attempt_close(
        db, inc, clock.now, body.get("actor", "ops"), body.get("note", "")
    )
    db.commit()
    return {**result, "incident": _incident_payload(db, inc, detail=True)}


@router.post("/incidents/{incident_id}/events", tags=["incidents"])
def add_incident_note(
    incident_id: int, body: dict[str, Any], db: Session = Depends(get_db)
) -> dict[str, Any]:
    inc = db.get(Incident, incident_id)
    if inc is None:
        raise HTTPException(404, "事故不存在")
    clock = simulator.get_clock(db)
    inc.events.append(
        IncidentEvent(
            incident_id=inc.id,
            kind="note",
            at_sim=clock.now,
            message=body.get("message", ""),
            payload={"actor": body.get("actor", "ops")},
        )
    )
    db.add(
        AuditLog(
            at_sim=clock.now,
            actor=body.get("actor", "ops"),
            action="incident_note",
            target=inc.code,
            detail={"message": body.get("message", "")},
        )
    )
    db.commit()
    return _incident_payload(db, inc, detail=True)


# ------------------------------------------------------------------ simulator


@router.post("/sim/action", tags=["simulator"])
def sim_action(body: dict[str, Any], db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        result = simulator.sim_action(db, body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"result": result, "clock": clock_payload(db)}


class ClockIn(BaseModel):
    minutes: int | None = None
    at: str | None = None
    run: bool = True


@router.post("/sim/clock", tags=["simulator"])
def sim_clock(body: ClockIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    if body.at:
        result = simulator.go_to(db, _parse_dt(body.at, "时间"), run=body.run)
    elif body.minutes is not None:
        result = simulator.advance_clock(db, body.minutes, run=body.run)
    else:
        raise HTTPException(400, "需要 minutes 或 at")
    return {"result": result, "clock": clock_payload(db)}


@router.get("/sim/timeline", tags=["simulator"])
def sim_timeline(db: Session = Depends(get_db), limit: int = 100) -> list[dict[str, Any]]:
    rows = (
        db.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))
        .scalars()
        .all()
    )
    return [
        {
            "id": r.id,
            "at": iso(r.at_sim),
            "actor": r.actor,
            "action": r.action,
            "target": r.target,
            "detail": r.detail,
        }
        for r in rows
    ]


@router.get("/samples", tags=["simulator"])
def list_samples(
    db: Session = Depends(get_db),
    meter: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    stmt = select(Sample).order_by(Sample.slot_start.desc(), Sample.id.desc()).limit(limit)
    if meter:
        m = (db.execute(select(Meter).where(Meter.code == meter)).scalars().first())
        if m is None:
            raise HTTPException(404, "表计不存在")
        stmt = stmt.where(Sample.meter_id == m.id)
    rows = db.execute(stmt).scalars().all()
    return [
        {
            "id": s.id,
            "meter_id": s.meter_id,
            "slot_start": iso(s.slot_start),
            "value": s.value,
            "received_at": iso(s.received_at),
            "declared_zero": s.is_declared_zero,
            "correction": s.is_correction,
            "note": s.note,
        }
        for s in rows
    ]


# ------------------------------------------------------------------ acceptance


@router.get("/acceptance/steps", tags=["acceptance"])
def acceptance_steps() -> list[dict[str, Any]]:
    """The narrative the replay executes; titles double as the UI storyline."""
    return [
        {"key": "baseline", "title": "基线：已完成窗口完整重算", "kind": "scheduled_check"},
        {"key": "planned_maintenance", "title": "计划维护：只扣除规则允许的时段", "kind": "maintenance"},
        {"key": "frequency_change", "title": "换频：前后两段分别计算分母", "kind": "commitment"},
        {"key": "tolerance_split", "title": "同一超时槽位：物业可用、结算受限", "kind": "impact"},
        {"key": "single_meter_delayed", "title": "单表计延迟 120 分钟", "kind": "arrival"},
        {"key": "missing_interval", "title": "缺失区间：事故打开并记录违约事实", "kind": "arrival"},
        {"key": "late_backfill", "title": "迟到补传：受影响窗口完整重算", "kind": "arrival"},
        {"key": "duplicate_and_declared_zero", "title": "重复到达只占一槽；声明零用量有效", "kind": "arrival"},
        {"key": "cumulative_reset", "title": "累计表回零：负差不计为节能", "kind": "verification"},
        {"key": "dependency_branch", "title": "依赖分支：缺失不牵连无关楼栋", "kind": "impact"},
        {"key": "recovery_check", "title": "恢复评估：完整重算后给出恢复证据", "kind": "recovery"},
        {"key": "close_blocked_new_gap", "title": "关闭前再次缺失：事故重新打开", "kind": "incident"},
        {"key": "repaired_and_closed", "title": "补齐并重新恢复评估后关闭", "kind": "incident"},
        {"key": "recurrence_after_recovery", "title": "恢复后再次缺失：新区间关联既往事故", "kind": "incident"},
        {"key": "threshold_preview", "title": "阈值预览：判定变化、事实不变", "kind": "preview"},
    ]



@router.post("/acceptance/run", tags=["acceptance"])
def run_acceptance_endpoint(
    body: dict[str, Any] | None, db: Session = Depends(get_db)
) -> dict[str, Any]:
    from ..services.acceptance import run_acceptance

    return run_acceptance(db)


@router.post("/acceptance/reset", tags=["acceptance"])
def reset_acceptance(db: Session = Depends(get_db)) -> dict[str, Any]:
    from ..services.acceptance import reset_demo_state
    from ..services.simulator import get_clock

    reset_demo_state(db)
    clock = get_clock(db)
    return {"ok": True, "now": iso(clock.now), "day": clock.day}
