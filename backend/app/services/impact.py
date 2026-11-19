from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Consumer,
    ConsumerDependency,
    ConsumerImpact,
    HealthEvaluation,
    ProductRun,
    Source,
)
from ..timeutil import iso, utcnow
from .products import PRODUCT_REALTIME, latest_product


def _slot_facts(evaluation: HealthEvaluation) -> dict[str, dict[str, Any]]:
    """Raw quality facts per (building, slot). Consumer thresholds are applied later and
    never mutate these facts."""
    out: dict[str, dict[str, Any]] = {}
    for meter in (evaluation.details or {}).get("meters", []):
        building = meter["building_code"]
        for st in meter.get("slot_states", []):
            key = f"{building}|{st['slot']}"
            entry = out.setdefault(
                key,
                {
                    "building": building,
                    "slot": st["slot"],
                    "slot_end": st.get("slot_end"),
                    "cadence_minutes": st.get("cadence_minutes"),
                    "status": st["status"],
                    "lateness": 0.0,
                    "verification": [],
                    "meters": [],
                },
            )
            entry["meters"].append(meter["meter_code"])
            if st["status"] == "covered":
                entry["lateness"] = max(entry["lateness"], st.get("lateness_minutes") or 0.0)
            elif st["status"] in ("missing", "pending", "beyond_cutoff"):
                # a building slot is only "covered" if every meter under it arrived
                if entry["status"] in ("covered", "maintenance", "verification"):
                    entry["status"] = st["status"]
            elif st["status"] == "maintenance":
                if entry["status"] in ("covered", "verification"):
                    entry["status"] = "maintenance"
        for v in meter.get("verification", []):
            key = f"{building}|{v['slot']}"
            entry = out.setdefault(
                key,
                {
                    "building": building,
                    "slot": v["slot"],
                    "slot_end": v.get("slot_end"),
                    "cadence_minutes": None,
                    "status": "verification",
                    "lateness": 0.0,
                    "verification": [],
                    "meters": [],
                },
            )
            entry["verification"].append(v)
            if entry["status"] not in ("missing", "pending"):
                entry["status"] = "verification"
    return out


def _merge(ranges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge overlapping/touching ranges so one physical gap is counted once even when
    the consumer depends on several affected products."""
    if not ranges:
        return []
    norm = sorted(
        [
            (
                datetime.fromisoformat(r["from"].rstrip("Z")),
                datetime.fromisoformat(r["to"].rstrip("Z")),
                r,
            )
            for r in ranges
        ],
        key=lambda x: (x[0], x[1]),
    )
    merged: list[dict[str, Any]] = []
    for start, end, r in norm:
        if merged and start <= datetime.fromisoformat(merged[-1]["to"].rstrip("Z")):
            cur_end = datetime.fromisoformat(merged[-1]["to"].rstrip("Z"))
            if end > cur_end:
                merged[-1]["to"] = end.isoformat() + "Z"
            merged[-1]["sources"] = sorted(
                set(merged[-1]["sources"] + r.get("sources", []))
            )
            merged[-1]["slots"] = sorted(set(merged[-1]["slots"] + r.get("slots", [])))
            merged[-1]["buildings"] = sorted(
                set(merged[-1].get("buildings", []) + r.get("buildings", []))
            )
        else:
            merged.append(
                {
                    "from": start.isoformat() + "Z",
                    "to": end.isoformat() + "Z",
                    "sources": list(r.get("sources", [])),
                    "slots": list(r.get("slots", [])),
                    "buildings": list(r.get("buildings", [])),
                }
            )
    return merged


def _rules(consumer: Consumer, override: dict[str, Any] | None) -> dict[str, Any]:
    rules = {
        "tolerance_minutes": consumer.tolerance_minutes,
        "min_coverage": consumer.min_coverage,
        "max_gap_slots": consumer.max_gap_slots,
        "require_certified": consumer.require_certified,
    }
    if override:
        for k in list(rules):
            if override.get(k) is not None:
                rules[k] = override[k]
    return rules


def _evaluate_dependency(
    dep: ConsumerDependency,
    rules: dict[str, Any],
    products: dict[tuple[str, str], ProductRun | None],
    evaluations: dict[tuple[int, str], HealthEvaluation | None],
    slot_fact_maps: dict[tuple[int, str], dict[str, dict[str, Any]]],
    sources: dict[str, Source],
    *,
    consumer_kind: str,
) -> dict[str, Any]:
    product = products.get((dep.product_code, dep.product_field))
    if product is None:
        return {
            "dependency_id": dep.id,
            "product_code": dep.product_code,
            "product_field": dep.product_field,
            "building_code": dep.building_code,
            "status": "waiting",
            "affected_slots": 0,
            "ranges": [],
            "reasons": [
                {
                    "kind": "missing_product",
                    "detail": f"窗口内没有 {dep.product_code}/{dep.product_field} 的运行版本",
                }
            ],
            "facts": {},
        }

    source = sources.get(dep.product_field)
    evaluation = evaluations.get((source.id, dep.product_field)) if source else None
    slot_map = slot_fact_maps.get((source.id, dep.product_field), {}) if source else {}
    if evaluation is None:
        return {
            "dependency_id": dep.id,
            "product_code": dep.product_code,
            "product_field": dep.product_field,
            "building_code": dep.building_code,
            "status": "waiting",
            "affected_slots": 0,
            "ranges": [],
            "reasons": [{"kind": "no_evaluation", "detail": "窗口尚未评估"}],
            "facts": {},
            "product_version": product.version,
        }

    tolerance = rules["tolerance_minutes"]
    require_certified = rules["require_certified"]
    is_realtime = dep.product_code == PRODUCT_REALTIME
    # Realtime consumers tolerate provisional display; settlement/assessment do not.
    counts_pending = not is_realtime

    affected_slots: set[str] = set()
    reasons: list[dict[str, Any]] = []
    by_building: dict[str, int] = {}
    raw_ranges: list[dict[str, Any]] = []

    scoped = [
        f
        for f in slot_map.values()
        if not dep.building_code or f["building"] == dep.building_code
    ]
    cutoff_dt = evaluation.data_cutoff
    for fact in scoped:
        status = fact["status"]
        if status in ("maintenance", "beyond_cutoff"):
            # Not due yet and not excused-by-rule: neither a gap nor a zero.
            continue
        slot_start = datetime.fromisoformat(fact["slot"].rstrip("Z"))
        slot_end = (
            datetime.fromisoformat(fact["slot_end"].rstrip("Z"))
            if fact.get("slot_end")
            else slot_start + timedelta(minutes=15)
        )
        # One raw fact produces different verdicts because each consumer compares it
        # with its own tolerance; the fact itself is never rewritten.
        if status == "verification":
            if is_realtime and not require_certified:
                continue
            kind = "verification_gap"
            detail = fact["verification"][0]["detail"]
        elif status == "covered":
            # Data has arrived: the consumer's product contains it. A historical late
            # arrival remains an SLO breach fact, but no longer blocks consumption.
            continue
        else:
            overdue = (cutoff_dt - slot_end).total_seconds() / 60
            if overdue <= tolerance:
                continue
            kind = "missing_beyond_tolerance" if status == "missing" else "pending_beyond_tolerance"
            detail = (
                f"槽位结束后 {overdue:.0f} 分钟仍未取得数据，"
                f"超过该消费者容忍 {tolerance} 分钟"
            )
        affected_slots.add(fact["slot"])
        by_building[fact["building"]] = by_building.get(fact["building"], 0) + 1
        raw_ranges.append(
            {
                "from": iso(slot_start),
                "to": iso(slot_end),
                "sources": [f"{dep.product_field}/{fact['building']}/{kind}"],
                "slots": [fact["slot"]],
                "buildings": [fact["building"]],
            }
        )
        if len(reasons) < 80:
            reasons.append(
                {
                    "kind": kind,
                    "slot": fact["slot"],
                    "building": fact["building"],
                    "product_code": dep.product_code,
                    "product_field": dep.product_field,
                    "product_version": product.version,
                    "meters": sorted(set(fact["meters"])),
                    "detail": detail,
                    "tolerance_minutes": tolerance,
                    "lateness_minutes": fact["lateness"],
                }
            )

    ranges = _merge(raw_ranges)
    denominator = len(scoped)
    coverage = ((denominator - len(affected_slots)) / denominator) if denominator else 0.0

    per_building: dict[str, dict[str, Any]] = {}
    for fact in scoped:
        b = per_building.setdefault(
            fact["building"], {"building": fact["building"], "den": 0, "affected": 0}
        )
        b["den"] += 1
        if fact["slot"] in affected_slots:
            b["affected"] += 1
    for b in per_building.values():
        b["coverage"] = round((b["den"] - b["affected"]) / b["den"], 4) if b["den"] else 1.0
        if b["affected"] == 0:
            b["status"] = "ok"
        elif b["coverage"] >= rules["min_coverage"]:
            b["status"] = "restricted"
        else:
            b["status"] = "unavailable"

    # daily downstream: trace back to the source interval that broke it
    if not is_realtime and affected_slots:
        last_slot = max(affected_slots)
        last_end = None
        for f in scoped:
            if f["slot"] == last_slot and f.get("slot_end"):
                last_end = datetime.fromisoformat(f["slot_end"].rstrip("Z"))
                break
        reasons.insert(
            0,
            {
                "kind": "source_interval",
                "product_code": dep.product_code,
                "product_field": dep.product_field,
                "from": min(affected_slots),
                "to": iso(
                    last_end
                    or datetime.fromisoformat(last_slot.rstrip("Z")) + timedelta(minutes=15)
                ),
                "detail": "下游日汇总失效，追溯到上述源字段/楼栋缺失区间",
            },
        )

    # Structural quality is judged only inside this dependency's own scope: a gap in
    # B2 must not mark a B1-only dependency unusable.
    scoped_missing = any(f["status"] == "missing" for f in scoped)
    scoped_pending = any(f["status"] == "pending" for f in scoped)
    scoped_verification = any(f["verification"] for f in scoped)
    # Certification is judged per dependency scope: a gap in B4 must not decertify a
    # B1/B2-only dependency. A scope is certified only if every slot in it arrived,
    # none of it is still provisional, verification passed, and nothing arrived after
    # its commitment (late arrivals void the certified version).
    scoped_late = [
        x
        for m in (evaluation.details or {}).get("meters", [])
        if not dep.building_code or m["building_code"] == dep.building_code
        for x in m.get("late", [])
    ]
    dep_certified = bool(
        scoped
        and evaluation.data_cutoff >= evaluation.window_end
        and all(f["status"] in ("covered", "maintenance") for f in scoped)
        and not scoped_missing
        and not scoped_pending
        and not scoped_verification
        and not scoped_late
    )
    has_structural = any(
        r["kind"] in ("missing_beyond_tolerance", "verification_gap")
        for r in reasons
    )
    if is_realtime:
        if not affected_slots:
            status = "ok"
        elif coverage >= rules["min_coverage"] and not has_structural:
            status = "restricted"
        elif coverage >= rules["min_coverage"]:
            status = "restricted"
        else:
            status = "unavailable"
    else:
        if affected_slots and (has_structural or coverage < rules["min_coverage"]):
            status = "unavailable"
        elif affected_slots:
            status = "restricted"
        elif scoped_pending:
            status = "waiting"
        elif require_certified and not dep_certified:
            status = "unavailable"
            reasons.insert(
                0,
                {
                    "kind": "not_certified",
                    "detail": "窗口尚未取得核验资格（存在未解决核验或覆盖条件），该消费者要求已核验数据",
                    "product_code": dep.product_code,
                    "product_field": dep.product_field,
                },
            )
        else:
            status = "ok"

    if rules["max_gap_slots"]:
        for r in ranges:
            if len(r["slots"]) > rules["max_gap_slots"]:
                if status in ("restricted", "ok"):
                    status = "unavailable"
                reasons.insert(
                    0,
                    {
                        "kind": "gap_exceeds",
                        "detail": (
                            f"连续缺口 {len(r['slots'])} 槽超过该消费者允许的 "
                            f"{rules['max_gap_slots']} 槽"
                        ),
                        "from": r["from"],
                        "to": r["to"],
                    },
                )
                break

    return {
        "dependency_id": dep.id,
        "product_code": dep.product_code,
        "product_field": dep.product_field,
        "building_code": dep.building_code,
        "product_version": product.version,
        "product_status": product.status,
        "product_provisional": bool((product.output or {}).get("provisional_from")),
        "status": status,
        "affected_slots": len(affected_slots),
        "coverage": round(coverage, 4),
        "ranges": ranges,
        "reasons": reasons,
        "by_building": by_building,
        "per_building": list(per_building.values()),
        "facts": {
            "window_den": denominator,
            "window_affected": len(affected_slots),
            "evaluation_id": evaluation.id,
            "evaluation_revision": evaluation.revision,
            "data_cutoff": iso(evaluation.data_cutoff),
            "certified": evaluation.certified,
            "dep_certified": dep_certified,
            "verification_ok": evaluation.verification_ok,
            "counts": evaluation.details.get("counts", {}),
        },
        "rules": rules,
    }


def compute_consumer_impact(
    db: Session,
    consumer: Consumer,
    ws: datetime,
    we: datetime,
    cutoff: datetime,
    *,
    override: dict[str, Any] | None = None,
    persist: bool = True,
) -> ConsumerImpact | dict[str, Any]:
    rules = _rules(consumer, override)
    deps = [
        d
        for d in consumer.dependencies
        if d.valid_from < we and (d.valid_to is None or d.valid_to > ws)
    ]
    sources = {s.product_field: s for s in db.execute(select(Source)).scalars().all()}
    pairs = {(d.product_code, d.product_field) for d in deps}
    products = {p: latest_product(db, p[0], p[1], ws, we) for p in pairs}
    evaluations: dict[tuple[int, str], HealthEvaluation | None] = {}
    slot_fact_maps: dict[tuple[int, str], dict[str, dict[str, Any]]] = {}
    for field in {d.product_field for d in deps}:
        source = sources.get(field)
        if not source:
            continue
        ev = (
            db.execute(
                select(HealthEvaluation)
                .where(
                    HealthEvaluation.source_id == source.id,
                    HealthEvaluation.window_start == ws,
                    HealthEvaluation.window_end == we,
                )
                .order_by(HealthEvaluation.revision.desc())
            )
            .scalars()
            .first()
        )
        evaluations[(source.id, field)] = ev
        slot_fact_maps[(source.id, field)] = _slot_facts(ev) if ev else {}

    results = [
        _evaluate_dependency(
            d,
            rules,
            products,
            evaluations,
            slot_fact_maps,
            sources,
            consumer_kind=consumer.kind,
        )
        for d in deps
    ]

    merged_ranges = _merge([r for res in results for r in res.get("ranges", [])])
    all_slots: set[str] = set()
    for res in results:
        all_slots.update(
            s for r in res.get("ranges", []) for s in r.get("slots", [])
        )
    affected_buildings = sorted({b for res in results for b in res.get("by_building", {})})
    total_affected = len(all_slots)
    raw_den = sum(res.get("facts", {}).get("window_den", 0) for res in results) or 0
    coverage = (raw_den - total_affected) / raw_den if raw_den else 0.0

    reasons: list[dict[str, Any]] = []
    seen_intervals: set[tuple[str, str, str]] = set()
    for res in results:
        for reason in res.get("reasons", []):
            if reason["kind"] == "source_interval":
                key = (reason["product_field"], reason["from"], reason["to"])
                if key in seen_intervals:
                    continue
                seen_intervals.add(key)
            reasons.append(
                {
                    **reason,
                    "dependency_id": res["dependency_id"],
                    "building_code": res["building_code"],
                }
            )

    statuses = [res["status"] for res in results]
    if not results:
        status = "waiting"
    elif any(s == "unavailable" for s in statuses):
        status = "unavailable"
    elif any(s == "restricted" for s in statuses):
        status = "restricted"
    elif any(s == "waiting" for s in statuses):
        status = "waiting"
    else:
        status = "ok"

    severity = {"ok": "none", "restricted": "degraded", "unavailable": "blocked", "waiting": "pending"}[status]
    if status == "restricted" and coverage < 0.5:
        severity = "blocked"

    payload = {
        "consumer_code": consumer.code,
        "consumer_name": consumer.name,
        "kind": consumer.kind,
        "window_start": iso(ws),
        "window_end": iso(we),
        "data_cutoff": iso(cutoff),
        "status": status,
        "severity": severity,
        "affected_slots": total_affected,
        "coverage": round(coverage, 4),
        "affected_buildings": affected_buildings,
        "ranges": merged_ranges,
        "reasons": reasons,
        "dependencies": results,
        "threshold_snapshot": rules,
        "facts": {"den": raw_den, "affected": total_affected},
    }

    if not persist:
        return payload

    latest = (
        db.execute(
            select(ConsumerImpact)
            .where(
                ConsumerImpact.consumer_id == consumer.id,
                ConsumerImpact.window_start == ws,
                ConsumerImpact.window_end == we,
            )
            .order_by(ConsumerImpact.revision.desc())
        )
        .scalars()
        .first()
    )
    if latest is not None:
        same = (
            latest.status == status
            and latest.affected_slots == total_affected
            and (latest.threshold_snapshot or {}) == rules
            and [(x["from"], x["to"]) for x in latest.ranges]
            == [(x["from"], x["to"]) for x in merged_ranges]
            and [(x.get("kind"), x.get("slot")) for x in latest.reasons]
            == [(x.get("kind"), x.get("slot")) for x in reasons]
        )
        if same:
            latest.data_cutoff = cutoff
            latest.computed_at = utcnow()
            latest.facts = {
                **payload["facts"],
                "dependencies": results,
                "affected_buildings": affected_buildings,
                "coverage": payload["coverage"],
            }
            db.flush()
            return latest

    revision = (latest.revision + 1) if latest else 0
    impact = ConsumerImpact(
        consumer_id=consumer.id,
        window_start=ws,
        window_end=we,
        data_cutoff=cutoff,
        revision=revision,
        status=status,
        severity=severity,
        affected_slots=total_affected,
        ranges=merged_ranges,
        reasons=reasons,
        facts={
            **payload["facts"],
            "dependencies": results,
            "affected_buildings": affected_buildings,
            "coverage": payload["coverage"],
        },
        threshold_snapshot=rules,
    )
    db.add(impact)
    db.flush()
    return impact
