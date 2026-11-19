"""峰值需量证据数据产品 API。

产品只输出"可证实 / 未知"范围供容量复核，不执行限电、不生成计费结论。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    Building,
    ContractCapacity,
    DemandMeterConfig,
    DemandReading,
    DemandReport,
    Meter,
)
from ..services import demand as demand_service
from ..services.simulator import get_clock
from ..timeutil import iso, utcnow

router = APIRouter(prefix="/api", tags=["demand"])


def _parse_dt(value: str, field: str = "时间") -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", ""))
    except ValueError as exc:
        raise HTTPException(400, f"{field} 格式无效: {value}") from exc


def _period(
    db: Session, start: str | None, end: str | None
) -> tuple[datetime, datetime, datetime]:
    clock = get_clock(db)
    ps, pe = demand_service.default_period(db)
    if start:
        ps = _parse_dt(start, "报告期开始")
    if end:
        pe = _parse_dt(end, "报告期结束")
    if ps >= pe:
        raise HTTPException(400, "报告期开始必须早于结束")
    cutoff = min(clock.now, pe) if (start or end) else clock.now
    if cutoff <= ps:
        raise HTTPException(409, "数据截止点早于报告期开始，没有可用证据")
    return ps, pe, cutoff


@router.get("/demand/meta", tags=["demand"])
def demand_meta(db: Session = Depends(get_db)) -> dict[str, Any]:
    clock = get_clock(db)
    ps, pe = demand_service.default_period(db)
    buildings = db.execute(select(Building).order_by(Building.code)).scalars().all()
    contracts = db.execute(select(ContractCapacity)).scalars().all()
    by_building = {c.building_id: c for c in contracts if c.product_field == "electricity"}
    configs = {
        c.meter_id: c for c in db.execute(select(DemandMeterConfig)).scalars().all()
    }
    meters = db.execute(select(Meter).order_by(Meter.code)).scalars().all()
    out_meters = []
    for meter in meters:
        cfg = configs.get(meter.id)
        if cfg is None:
            continue
        out_meters.append(
            {
                "meter_id": meter.id,
                "meter_code": meter.code,
                "meter_name": meter.name,
                "building_code": meter.building.code,
                "mode": cfg.mode,
                "mode_label": demand_service.MODE_META[cfg.mode],
                "cumulative": meter.cumulative,
                "multiplier": meter.multiplier,
                "rated_kw": cfg.rated_kw,
                "neighbor_step_kw": cfg.neighbor_step_kw,
            }
        )
    return {
        "clock": {"now": iso(clock.now), "day": clock.day},
        "default_period": {"start": iso(ps), "end": iso(pe)},
        "slot_minutes": 15,
        "methods": [
            {"method": key, **meta} for key, meta in demand_service.METHOD_META.items()
        ],
        "buildings": [
            {
                "code": b.code,
                "name": b.name,
                "contract_kw": by_building[b.id].capacity_kw
                if b.id in by_building
                else None,
                "contract_source": by_building[b.id].source
                if b.id in by_building
                else None,
            }
            for b in buildings
        ],
        "meters": out_meters,
        "status_labels": {
            "full": "完整覆盖（可确认 15 分钟平均需量）",
            "partial": "部分覆盖（只有观测部分，下界不可当平均值）",
            "none": "无覆盖（未知格）",
        },
        "contract_status_labels": {
            "exceeded": "已证实超限",
            "not_exceeded": "已证实未超限",
            "not_proven": "尚未证实超限",
            "no_contract": "未登记合同容量",
        },
        "unknown_risk_labels": {
            "none": "无未知风险",
            "bounded": "未知范围有保守上界",
            "unbounded": "未知范围无上界",
        },
    }


@router.get("/demand/reports", tags=["demand"])
def list_reports(
    db: Session = Depends(get_db),
    start: str | None = None,
    end: str | None = None,
    method: str | None = None,
    latest_only: bool = True,
    recompute: bool = True,
) -> dict[str, Any]:
    ps, pe, cutoff = _period(db, start, end)
    if method and method not in demand_service.METHODS:
        raise HTTPException(400, f"未知口径 {method}")
    methods = (method,) if method else demand_service.METHODS
    if recompute:
        demand_service.publish_all(db, ps, pe, cutoff, methods=methods)
        db.commit()
    buildings = db.execute(select(Building).order_by(Building.code)).scalars().all()
    reports: list[dict[str, Any]] = []
    for building in buildings:
        for m in methods:
            latest = demand_service.latest_report(db, building.code, m, ps, pe)
            if latest is None:
                continue
            chain = (
                db.execute(
                    select(DemandReport)
                    .where(
                        DemandReport.building_code == building.code,
                        DemandReport.method == m,
                        DemandReport.period_start == ps,
                        DemandReport.period_end == pe,
                    )
                    .order_by(DemandReport.revision)
                )
                .scalars()
                .all()
            )
            if latest_only:
                chain = [latest]
            for report in chain:
                summary = _summary(report)
                summary["versions"] = [
                    {
                        "id": r.id,
                        "revision": r.revision,
                        "version": r.version,
                        "data_cutoff": iso(r.data_cutoff),
                        "contract_status": r.contract_status,
                        "confirmed_peak_kw": r.confirmed_peak_kw,
                        "conclusion": r.conclusion,
                    }
                    for r in (
                        chain
                        if latest_only
                        else db.execute(
                            select(DemandReport)
                            .where(
                                DemandReport.building_code == building.code,
                                DemandReport.method == m,
                                DemandReport.period_start == ps,
                                DemandReport.period_end == pe,
                            )
                            .order_by(DemandReport.revision)
                        )
                        .scalars()
                        .all()
                    )
                ]
                reports.append(summary)
    return {
        "period": {"start": iso(ps), "end": iso(pe)},
        "data_cutoff": iso(cutoff),
        "reports": reports,
    }


def _summary(report: DemandReport) -> dict[str, Any]:
    details = report.details or {}
    return {
        "id": report.id,
        "building_code": report.building_code,
        "building_name": report.building_name,
        "method": report.method,
        "method_label": demand_service.METHOD_META[report.method]["name"],
        "period_start": iso(report.period_start),
        "period_end": iso(report.period_end),
        "data_cutoff": iso(report.data_cutoff),
        "revision": report.revision,
        "version": report.version,
        "recompute_count": report.recompute_count,
        "contract_status": report.contract_status,
        "contract_basis": details.get("contract_basis"),
        "unknown_risk": report.unknown_risk,
        "conclusion": report.conclusion,
        "confirmed_peak_kw": report.confirmed_peak_kw,
        "confirmed_peak_slot": iso(report.confirmed_peak_slot),
        "contract_kw": report.contract_kw,
        "risk_interval": details.get("risk_interval"),
        "counts": report.counts,
        "delta": details.get("delta"),
        "warnings": details.get("warnings", []),
        "gap_slots": details.get("gap_slots", []),
        "candidate_peak_slots": details.get("candidate_peak_slots", []),
        "peak_candidates": details.get("peak_candidates", []),
    }


@router.get("/demand/reports/{report_id}", tags=["demand"])
def get_report(report_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    report = db.get(DemandReport, report_id)
    if report is None:
        raise HTTPException(404, "峰值需量报告不存在")
    payload = demand_service.report_payload(report)
    chain = (
        db.execute(
            select(DemandReport)
            .where(
                DemandReport.building_code == report.building_code,
                DemandReport.method == report.method,
                DemandReport.period_start == report.period_start,
                DemandReport.period_end == report.period_end,
            )
            .order_by(DemandReport.revision)
        )
        .scalars()
        .all()
    )
    payload["versions"] = [
        {
            "id": r.id,
            "revision": r.revision,
            "version": r.version,
            "data_cutoff": iso(r.data_cutoff),
            "contract_status": r.contract_status,
            "confirmed_peak_kw": r.confirmed_peak_kw,
            "lower_kw": r.lower_kw,
            "upper_kw": r.upper_kw,
            "conclusion": r.conclusion,
            "computed_at": iso(r.computed_at),
            "recompute_count": r.recompute_count,
            "delta": (r.details or {}).get("delta"),
        }
        for r in chain
    ]
    return payload


@router.post("/demand/reports", tags=["demand"])
def publish(
    body: dict[str, Any] | None = None, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Publish a report version at an explicit data cutoff.

    Reading normalization, cell coverage, demand math, the contract threshold and this
    publication all bind to the same cutoff. A cutoff that moves forward creates a new
    version and records how the peak conclusion changed.
    """
    body = body or {}
    ps, pe, cutoff = _period(db, body.get("start"), body.get("end"))
    if body.get("cutoff"):
        cutoff = _parse_dt(body["cutoff"], "数据截止点")
    if cutoff <= ps:
        raise HTTPException(409, "数据截止点早于报告期开始，没有可用证据")
    method = body.get("method")
    if method and method not in demand_service.METHODS:
        raise HTTPException(400, f"未知口径 {method}")
    methods = (method,) if method else demand_service.METHODS
    codes = body.get("buildings")
    reports = demand_service.publish_all(
        db, ps, pe, cutoff, methods=methods, building_codes=codes
    )
    db.commit()
    return {
        "period": {"start": iso(ps), "end": iso(pe)},
        "data_cutoff": iso(cutoff),
        "reports": [_summary(r) for r in reports],
    }


@router.get("/demand/acceptance/steps", tags=["demand"])
def demand_acceptance_steps() -> list[dict[str, Any]]:
    return [
        {
            "key": "demand_grid_coverage",
            "title": "格归集：完整/部分/无覆盖分开，重复报文不累加",
        },
        {"key": "demand_noon_gap", "title": "午间缺口：未覆盖候选峰值，尚未证实超限"},
        {"key": "demand_boundary_sampling", "title": "格边界采样：跨格读数留下更多未知格"},
        {"key": "demand_cumulative_rollback", "title": "累计表回退：未知而非负需量"},
        {"key": "demand_integration_route", "title": "区间积分：确认格归集未覆盖的峰值"},
        {"key": "demand_physical_bound", "title": "保守界：有明确来源的上界才可证实未超限"},
        {"key": "demand_late_peak", "title": "迟到高峰读数：新报告版本与峰值结论变化"},
    ]


@router.post("/demand/acceptance/reset", tags=["demand"])
def reset_demand_acceptance(db: Session = Depends(get_db)) -> dict[str, Any]:
    from ..services.demand_acceptance import reset_demand

    reset_demand(db)
    db.commit()
    return {"ok": True}


@router.post("/demand/acceptance/run", tags=["demand"])
def run_demand_acceptance_endpoint(
    body: dict[str, Any] | None = None, db: Session = Depends(get_db)
) -> dict[str, Any]:
    from ..services.demand_acceptance import run_demand_acceptance

    return run_demand_acceptance(db)


@router.get("/demand/readings", tags=["demand"])
def list_readings(
    db: Session = Depends(get_db),
    meter: str | None = None,
    building: str | None = None,
    limit: int = Query(200, ge=1, le=2000),
) -> list[dict[str, Any]]:
    stmt = select(DemandReading).order_by(
        DemandReading.measured_from.desc(), DemandReading.id.desc()
    ).limit(limit)
    if meter:
        row = (
            db.execute(select(Meter).where(Meter.code == meter)).scalars().first()
        )
        if row is None:
            raise HTTPException(404, "表计不存在")
        stmt = stmt.where(DemandReading.meter_id == row.id)
    elif building:
        b = (
            db.execute(select(Building).where(Building.code == building))
            .scalars()
            .first()
        )
        if b is None:
            raise HTTPException(404, "楼栋不存在")
        meter_ids = [
            m.id
            for m in db.execute(
                select(Meter).where(Meter.building_id == b.id)
            )
            .scalars()
            .all()
        ]
        stmt = stmt.where(DemandReading.meter_id.in_(meter_ids or [-1]))
    rows = db.execute(stmt).scalars().all()
    return [
        {
            "id": r.id,
            "meter_id": r.meter_id,
            "meter_code": r.meter.code,
            "building_code": r.meter.building.code,
            "kind": r.kind,
            "measured_from": iso(r.measured_from),
            "measured_to": iso(r.measured_to),
            "value": r.value,
            "received_at": iso(r.received_at),
            "note": r.note,
        }
        for r in rows
    ]
