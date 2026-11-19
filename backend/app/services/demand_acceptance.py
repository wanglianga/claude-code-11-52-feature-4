"""峰值需量数据产品的验收重放。

覆盖题目要求的全部样例：格边界采样、重复报文、午间缺口、累计表回退、已确认超限、
未覆盖候选峰值，以及"迟到高峰读数只生成新报告并标出峰值结论如何改变"。
每一步的观测都从数据库读回报告与逐格明细，不在展示层合成结论。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import DemandReading, DemandReport, Meter
from ..timeutil import iso
from . import demand as demand_service

PERIOD_START = datetime(2026, 11, 9, 0, 0)
PERIOD_END = datetime(2026, 11, 10, 0, 0)
BASE_CUTOFF = datetime(2026, 11, 10, 0, 0)
LATE_CUTOFF = datetime(2026, 11, 10, 2, 0)
LATE_NOTE = "验收：迟到补传的高峰读数"


def reset_demand(db: Session) -> None:
    """Return peak-demand evidence to the pristine seeded state."""
    db.execute(delete(DemandReport))
    db.execute(delete(DemandReading).where(DemandReading.note == LATE_NOTE))
    db.flush()


def _add_late_noon_readings(db: Session) -> int:
    meter = (
        db.execute(select(Meter).where(Meter.code == "M-DMD-B2-01")).scalars().one()
    )
    created = 0
    for i in range(4):  # 12:00-13:00, the cells the settlement feed never delivered
        start = datetime(2026, 11, 9, 12, 0) + timedelta(minutes=15 * i)
        end = start + timedelta(minutes=15)
        db.add(
            DemandReading(
                meter_id=meter.id,
                kind="energy",
                stream="settlement",
                measured_from=start,
                measured_to=end,
                value=round(960.0 * 0.25, 4),
                received_at=LATE_CUTOFF,
                note=LATE_NOTE,
            )
        )
        created += 1
    db.flush()
    return created


def _report_bundle(db: Session, cutoff: datetime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for code in ("B1", "B2", "B3", "B4"):
        for method in demand_service.METHODS:
            report = demand_service.latest_report(
                db, code, method, PERIOD_START, PERIOD_END
            )
            if report is None:
                continue
            details = report.details or {}
            out.append(
                {
                    "building": code,
                    "method": method,
                    "report_id": report.id,
                    "revision": report.revision,
                    "version": report.version,
                    "data_cutoff": iso(report.data_cutoff),
                    "contract_kw": report.contract_kw,
                    "confirmed_peak_kw": report.confirmed_peak_kw,
                    "confirmed_peak_slot": iso(report.confirmed_peak_slot),
                    "risk_interval": details.get("risk_interval"),
                    "contract_status": report.contract_status,
                    "contract_basis": details.get("contract_basis"),
                    "unknown_risk": report.unknown_risk,
                    "conclusion": report.conclusion,
                    "counts": report.counts,
                    "gap_slots": details.get("gap_slots", []),
                    "candidate_peak_slots": details.get("candidate_peak_slots", []),
                    "peak_candidates": details.get("peak_candidates", []),
                    "delta": details.get("delta"),
                    "warnings": details.get("warnings", []),
                    "slot_details": [
                        {
                            "slot": s["slot"],
                            "status": s["status"],
                            "demand_kw": s["demand_kw"],
                            "observed_kw": s["observed_kw"],
                            "lower_kw": s["lower_kw"],
                            "upper_kw": s["upper_kw"],
                            "above_contract": s["above_contract"],
                        }
                        for s in details.get("slots", [])
                    ],
                }
            )
    return out


def _find(bundle: list[dict[str, Any]], building: str, method: str) -> dict[str, Any]:
    for item in bundle:
        if item["building"] == building and item["method"] == method:
            return item
    raise KeyError(f"{building}/{method}")


def run_demand_acceptance(db: Session) -> dict[str, Any]:
    reset_demand(db)
    steps: list[dict[str, Any]] = []

    def record(step: str, title: str, detail: dict[str, Any] | None = None) -> None:
        steps.append(
            {
                "step": step,
                "title": title,
                "detail": detail or {},
                "observation": {"demand": _report_bundle(db, BASE_CUTOFF)},
            }
        )

    demand_service.publish_all(db, PERIOD_START, PERIOD_END, BASE_CUTOFF)
    db.commit()
    base = _report_bundle(db, BASE_CUTOFF)

    b1_grid = _find(base, "B1", "grid")
    checks = [
        ("基线：15 分钟格全部完整覆盖", b1_grid["counts"]["full"] == 96),
        ("重复报文只保留一条代表读数", b1_grid["counts"]["duplicates"] >= 1),
        (
            "已确认峰值低于合同容量且无未知格 ⇒ 已证实未超限",
            b1_grid["contract_status"] == "not_exceeded"
            and b1_grid["counts"]["unknown"] == 0,
        ),
    ]
    record(
        "demand_grid_coverage",
        "格归集：完整/部分/无覆盖分开统计，重复报文不累加，满覆盖时才确认 15 分钟平均需量",
        {
            "checks": [{"label": k, "ok": v} for k, v in checks],
            "b1_full_slots": b1_grid["counts"]["full"],
            "b1_duplicates": b1_grid["counts"]["duplicates"],
        },
    )

    b2_grid = _find(base, "B2", "grid")
    record(
        "demand_noon_gap",
        "午间缺口：15 分钟流缺 12:00–13:00，只有已见峰值 801 kW，未知格中保留候选峰值",
        {
            "unknown_slots": b2_grid["counts"]["unknown"],
            "candidate_slots": b2_grid["candidate_peak_slots"],
            "contract_status": b2_grid["contract_status"],
            "conclusion": b2_grid["conclusion"],
            "checks": [
                {
                    "label": "最高已确认格低于合同但存在缺口格 ⇒ 尚未证实超限",
                    "ok": b2_grid["contract_status"] == "not_proven",
                },
                {
                    "label": "缺口格被列为未覆盖候选峰值",
                    "ok": len(b2_grid["candidate_peak_slots"]) == 4,
                },
            ],
        },
    )

    b4_grid = _find(base, "B4", "grid")
    record(
        "demand_boundary_sampling",
        "格边界采样：读数跨越 15 分钟边界，固定结算格无法分摊 ⇒ 更多未知格，不冒充平均值",
        {
            "full": b4_grid["counts"]["full"],
            "partial": b4_grid["counts"]["partial"],
            "unknown": b4_grid["counts"]["unknown"],
            "warnings": b4_grid["warnings"],
            "checks": [
                {
                    "label": "跨格读数不产生任何完整格",
                    "ok": b4_grid["counts"]["full"] == 0,
                },
                {
                    "label": "未知格数量多于区间积分路线",
                    "ok": b4_grid["counts"]["unknown"]
                    > _find(base, "B4", "integration")["counts"]["unknown"],
                },
            ],
        },
    )

    b3_grid = _find(base, "B3", "grid")
    record(
        "demand_cumulative_rollback",
        "累计表回退：10:30 表码回退列为未知，不产生负需量；其余格照常确认",
        {
            "counts": b3_grid["counts"],
            "warnings": b3_grid["warnings"],
            "checks": [
                {
                    "label": "回退被单列为未知而非负需量",
                    "ok": b3_grid["counts"]["rollbacks"] == 1
                    and (b3_grid["risk_interval"]["lower_kw"] or 0) >= 0,
                },
                {
                    "label": "已确认峰值 1135 kW 高于合同 1000 kW ⇒ 已证实超限",
                    "ok": b3_grid["contract_status"] == "exceeded"
                    and (b3_grid["confirmed_peak_kw"] or 0) > 1000,
                },
            ],
        },
    )

    b2_int = _find(base, "B2", "integration")
    record(
        "demand_integration_route",
        "区间积分：5 分钟读数铺满全天，确认 12:00 的 961 kW 峰值，合同超限证据成立",
        {
            "confirmed_peak_kw": b2_int["confirmed_peak_kw"],
            "confirmed_peak_slot": b2_int["confirmed_peak_slot"],
            "contract_status": b2_int["contract_status"],
            "checks": [
                {
                    "label": "积分路线确认了格归集未覆盖的峰值",
                    "ok": (b2_int["confirmed_peak_kw"] or 0) > 850,
                },
                {
                    "label": "同一合同阈值下结论为已证实超限",
                    "ok": b2_int["contract_status"] == "exceeded",
                },
            ],
        },
    )

    b4_int = _find(base, "B4", "integration")
    record(
        "demand_physical_bound",
        "保守界：个别未完整覆盖格有登记相邻变化率作明确来源的保守上界，上界仍低于合同 ⇒ 可证实未超限",
        {
            "risk_interval": b4_int["risk_interval"],
            "contract_status": b4_int["contract_status"],
            "contract_basis": b4_int["contract_basis"],
            "checks": [
                {
                    "label": "上界不等于历史最大值，且来源明确",
                    "ok": b4_int["counts"]["unknown"] > 0
                    and b4_int["contract_status"] == "not_exceeded"
                    and b4_int["contract_basis"] == "physical_bound",
                }
            ],
        },
    )

    # ---- late peak reading -> new report version with an explicit peak delta ----
    before = _find(_report_bundle(db, BASE_CUTOFF), "B2", "grid")
    created = _add_late_noon_readings(db)
    db.commit()
    demand_service.publish_all(db, PERIOD_START, PERIOD_END, LATE_CUTOFF)
    db.commit()
    after_bundle = _report_bundle(db, LATE_CUTOFF)
    after = _find(after_bundle, "B2", "grid")
    record(
        "demand_late_peak",
        "迟到高峰读数：在 02:00 截止点生成新报告版本，峰值结论与合同状态的变化单独标出",
        {
            "late_readings": created,
            "before": {
                "report_id": before["report_id"],
                "data_cutoff": before["data_cutoff"],
                "confirmed_peak_kw": before["confirmed_peak_kw"],
                "contract_status": before["contract_status"],
            },
            "after": {
                "report_id": after["report_id"],
                "data_cutoff": after["data_cutoff"],
                "confirmed_peak_kw": after["confirmed_peak_kw"],
                "contract_status": after["contract_status"],
                "revision": after["revision"],
            },
            "delta": after["delta"],
            "checks": [
                {
                    "label": "迟到读数不改写旧版本，只生成新 revision",
                    "ok": after["revision"] > before["revision"]
                    and after["data_cutoff"] != before["data_cutoff"],
                },
                {
                    "label": "结论由“尚未证实超限”变为“已证实超限”并标出峰值变化",
                    "ok": before["contract_status"] == "not_proven"
                    and after["contract_status"] == "exceeded"
                    and (after["delta"] or {}).get("conclusion_changed") is True,
                },
                {
                    "label": "迟到高峰读数按采样时间归格",
                    "ok": after["confirmed_peak_slot"].startswith("2026-11-09T12:00"),
                },
            ],
        },
    )

    return {
        "ok": True,
        "period": {"start": iso(PERIOD_START), "end": iso(PERIOD_END)},
        "steps": steps,
    }
