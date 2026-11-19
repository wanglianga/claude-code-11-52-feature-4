from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import HealthEvaluation, Meter, ProductRun, Source
from ..timeutil import iso, utcnow

PRODUCT_REALTIME = "energy.realtime"
PRODUCT_DAILY = "energy.daily"
PRODUCT_META = {
    PRODUCT_REALTIME: {
        "name": "近实时能耗曲线",
        "description": "按数据截止点输出的暂定曲线，带截止点标记，可用于负荷调度",
        "provisional": True,
    },
    PRODUCT_DAILY: {
        "name": "核验日汇总",
        "description": "观察窗口完整重算、通过核验后的日汇总，用于结算与考核",
        "provisional": False,
    },
}


def _slot_status(evaluation: HealthEvaluation) -> dict[str, dict[str, Any]]:
    """Aggregate per-meter slot states into a per-window slot status map."""
    details = evaluation.details or {}
    slot_map: dict[str, dict[str, Any]] = {}
    for meter in details.get("meters", []):
        for st in meter.get("slot_states", []):
            slot = st["slot"]
            agg = slot_map.setdefault(
                slot,
                {
                    "slot": slot,
                    "covered": 0,
                    "maintenance": 0,
                    "missing": 0,
                    "pending": 0,
                    "beyond_cutoff": 0,
                    "meters": 0,
                },
            )
            agg["meters"] += 1
            agg[st["status"]] += 1
    for meter in details.get("meters", []):
        for v in meter.get("verification", []):
            agg = slot_map.get(v["slot"])
            if agg is not None:
                agg.setdefault("verification", 0)
                agg["verification"] += 1
                if v.get("slot_end"):
                    agg.setdefault("ends", {})[v["slot"]] = v["slot_end"]
    return slot_map


def _classify(slot_map: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    qualified: list[dict[str, Any]] = []
    excused: list[dict[str, Any]] = []

    def push(bucket: list[dict[str, Any]], slot: str) -> None:
        if bucket and bucket[-1]["to"] == slot:
            bucket[-1]["to"] = (datetime.fromisoformat(slot.rstrip("Z")) + timedelta(minutes=15)).isoformat() + "Z"
        else:
            bucket.append(
                {
                    "from": slot,
                    "to": (datetime.fromisoformat(slot.rstrip("Z")) + timedelta(minutes=15)).isoformat() + "Z",
                }
            )

    for slot in sorted(slot_map):
        agg = slot_map[slot]
        if (
            agg["missing"]
            or agg["pending"]
            or agg["beyond_cutoff"]
            or agg.get("verification")
        ):
            continue
        if agg["maintenance"] == agg["meters"]:
            push(excused, slot)
        elif agg["covered"] == agg["meters"]:
            push(qualified, slot)
    return qualified, excused


def _product_output(
    db: Session,
    source: Source,
    evaluation: HealthEvaluation,
    ws: datetime,
    we: datetime,
    cutoff: datetime,
) -> dict[str, Any]:
    details = evaluation.details or {}
    by_slot: dict[str, dict[str, float]] = {}
    slot_ends: dict[str, str] = {}
    building_totals: dict[str, dict[str, float]] = {}
    totals: dict[str, float] = {}
    provisional_from: str | None = None
    meters = {m.id: m for m in db.execute(select(Meter).where(Meter.source_id == source.id)).scalars()}

    for meter in details.get("meters", []):
        code = meter["building_code"]
        building_totals.setdefault(code, {})
        for st in meter.get("slot_states", []):
            if st.get("slot_end"):
                slot_ends[st["slot"]] = st["slot_end"]
        for slot, value in (meter.get("consumption") or {}).get("by_slot", {}).items():
            if value is None:
                continue
            by_slot.setdefault(slot, {})
            by_slot[slot][code] = round(by_slot[slot].get(code, 0.0) + value, 4)
            building_totals[code][slot] = round(building_totals[code].get(slot, 0.0) + value, 4)
            totals[slot] = round(totals.get(slot, 0.0) + value, 4)
        _ = meters

    series = [
        {
            "slot": slot,
            "value": totals[slot],
            "by_building": by_slot.get(slot, {}),
        }
        for slot in sorted(totals)
    ]
    if evaluation.data_cutoff < we:
        provisional_from = iso(evaluation.data_cutoff)
    if evaluation.details.get("counts", {}).get("beyond_cutoff"):
        provisional_from = iso(evaluation.data_cutoff)

    first = (
        min((datetime.fromisoformat(s["slot"].rstrip("Z")) for s in series), default=None)
    )
    last = max((datetime.fromisoformat(s["slot"].rstrip("Z")) for s in series), default=None)
    return {
        "unit": source.unit,
        "slot_ends": slot_ends,
        "window_start": iso(ws),
        "window_end": iso(we),
        "data_cutoff": iso(cutoff),
        "provisional_from": provisional_from,
        "series": series,
        "building_totals": {
            code: round(sum(v.values()), 4) for code, v in building_totals.items()
        },
        "total": round(sum(totals.values()), 4),
        "first_slot": iso(first),
        "last_slot": iso(last + timedelta(minutes=15)) if last else None,
        "coverage": {
            "num": evaluation.coverage_num,
            "den": evaluation.coverage_den,
        },
    }


def build_product(
    db: Session,
    source: Source,
    evaluation: HealthEvaluation,
    product_code: str,
    cutoff: datetime,
) -> ProductRun:
    ws, we = evaluation.window_start, evaluation.window_end
    slot_map = _slot_status(evaluation)
    qualified, excused = _classify(slot_map)
    output = _product_output(db, source, evaluation, ws, we, cutoff)

    details = evaluation.details or {}
    if product_code == PRODUCT_DAILY:
        if cutoff < we or details.get("pending"):
            status = "pending"
        elif evaluation.certified:
            status = "qualified"
        elif evaluation.status == "breached":
            status = "failed"
        else:
            status = "partial"
    else:
        provisional_slots = (evaluation.details or {}).get("counts", {}).get("beyond_cutoff", 0)
        cut = (evaluation.details or {}).get("verification", [])
        if not output["series"] and not provisional_slots:
            status = "unavailable"
        elif details.get("missing") or (cut and not evaluation.verification_ok):
            status = "partial"
        elif details.get("pending") or cutoff < we or provisional_slots:
            status = "provisional"
        else:
            status = "qualified"

    latest = (
        db.execute(
            select(ProductRun)
            .where(
                ProductRun.product_code == product_code,
                ProductRun.product_field == source.product_field,
                ProductRun.window_start == ws,
                ProductRun.window_end == we,
            )
            .order_by(ProductRun.revision.desc())
        )
        .scalars()
        .first()
    )
    signature = (
        status,
        [r["from"] + r["to"] for r in qualified],
        output["total"],
        [s["slot"] for s in output["series"]],
    )
    if latest is not None:
        prev_sig = (
            latest.status,
            [r["from"] + r["to"] for r in latest.qualified_ranges if r.get("kind") == "qualified"],
            (latest.output or {}).get("total"),
            [s["slot"] for s in (latest.output or {}).get("series", [])],
        )
        if prev_sig == signature:
            latest.data_cutoff = cutoff
            latest.computed_at = utcnow()
            latest.output = output
            latest.qualified_ranges = [
                {**r, "kind": "qualified"} for r in qualified
            ] + [{**r, "kind": "maintenance"} for r in excused]
            latest.evaluation_ids = [evaluation.id]
            db.flush()
            return latest

    revision = (latest.revision + 1) if latest else 0
    version = (latest.version + 1) if latest else 1
    run = ProductRun(
        product_code=product_code,
        product_field=source.product_field,
        window_start=ws,
        window_end=we,
        data_cutoff=cutoff,
        revision=revision,
        version=version,
        status=status,
        output=output,
        qualified_ranges=[{**r, "kind": "qualified"} for r in qualified]
        + [{**r, "kind": "maintenance"} for r in excused],
        evaluation_ids=[evaluation.id],
    )
    db.add(run)
    db.flush()
    return run


def latest_product(
    db: Session, product_code: str, product_field: str, ws: datetime, we: datetime
) -> ProductRun | None:
    return (
        db.execute(
            select(ProductRun)
            .where(
                ProductRun.product_code == product_code,
                ProductRun.product_field == product_field,
                ProductRun.window_start == ws,
                ProductRun.window_end == we,
            )
            .order_by(ProductRun.revision.desc())
        )
        .scalars()
        .first()
    )
