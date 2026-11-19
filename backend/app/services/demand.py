"""峰值需量证据与风险区间数据产品。

口径（两条路线都必须遵守）：
- 只能使用"采样时间"（measured_from / measured_to）归格；received_at 只用于判断
  该读数是否在本次数据截止点之前到达。
- 15 分钟平均需量只能在**完整覆盖**的结算格上确认；部分覆盖只给出"观测部分"的
  保守下界，绝不把部分读数按比例放大成完整格平均。
- 累计表回退（非单调）单列为未知，永远不产生负需量。
- 已确认峰值低于合同容量、但存在部分/无覆盖格时，只能给"尚未证实超限"；
  除非设备额定上限或明确登记的相邻读数变化率给出**保守上界**并仍低于合同，
  才可声明已证实未超限。
- 所有读数规范化、时间格覆盖、需量计算、合同阈值与报告发布都绑定同一个
  data_cutoff；截止点之后到达的读数只会形成新报告版本，并标出峰值结论变化。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    ContractCapacity,
    DemandMeterConfig,
    DemandReading,
    DemandReport,
    Meter,
    SimClock,
)
from ..timeutil import iso, utcnow

SLOT_SECONDS = 15 * 60
METHOD_GRID = "grid"
METHOD_INTEGRATION = "integration"
METHODS = (METHOD_GRID, METHOD_INTEGRATION)
METHOD_META = {
    METHOD_GRID: {
        "name": "15 分钟结算格归集",
        "description": (
            "读数先归一到固定 15 分钟格；只有被格内读数完整铺满、且没有跨格读数的格"
            "才计算平均需量。口径稳定、可与结算周期逐格核对，但边界采样不齐会留下更多未知格。"
        ),
    },
    METHOD_INTEGRATION: {
        "name": "带时间戳区间积分",
        "description": (
            "对带采样时段的高频读数做区间积分，跨格事件按重叠时长精确分摊；"
            "能利用不规则采样，但每个读数的代表时段必须可信，断档期间仍不可插值。"
        ),
    },
}

MODE_ENERGY = "energy_interval"
MODE_REGISTER = "register"
MODE_POWER = "power"
MODE_META = {
    MODE_ENERGY: "15 分钟区间电量（kWh）",
    MODE_REGISTER: "累计表码（kWh，瞬时抄见）",
    MODE_POWER: "区间平均功率（kW）",
}

STATUS_FULL = "full"
STATUS_PARTIAL = "partial"
STATUS_NONE = "none"

CONTRACT_EXCEEDED = "exceeded"
CONTRACT_NOT_EXCEEDED = "not_exceeded"
CONTRACT_NOT_PROVEN = "not_proven"
CONTRACT_NO_CAPACITY = "no_contract"

RISK_NONE = "none"
RISK_BOUNDED = "bounded"
RISK_UNBOUNDED = "unbounded"


# ---------------------------------------------------------------- inputs


def _slot_starts(ps: datetime, pe: datetime) -> list[datetime]:
    out: list[datetime] = []
    cur = ps
    while cur < pe:
        out.append(cur)
        cur += timedelta(seconds=SLOT_SECONDS)
    return out


def resolve_contract(
    db: Session, building_id: int, product_field: str, ps: datetime, pe: datetime
) -> ContractCapacity | None:
    rows = (
        db.execute(
            select(ContractCapacity)
            .where(
                ContractCapacity.building_id == building_id,
                ContractCapacity.product_field == product_field,
                ContractCapacity.valid_from < pe,
                (ContractCapacity.valid_to.is_(None))
                | (ContractCapacity.valid_to > ps),
            )
            .order_by(ContractCapacity.valid_from.desc())
        )
        .scalars()
        .all()
    )
    return rows[0] if rows else None


def meter_config(db: Session, meter: Meter) -> DemandMeterConfig | None:
    return (
        db.execute(
            select(DemandMeterConfig).where(DemandMeterConfig.meter_id == meter.id)
        )
        .scalars()
        .first()
    )


def _mode_of(meter: Meter, cfg: DemandMeterConfig | None) -> str:
    if cfg is not None:
        return cfg.mode
    return MODE_REGISTER if meter.cumulative else MODE_ENERGY


def load_readings(
    db: Session,
    meter: Meter,
    ps: datetime,
    pe: datetime,
    cutoff: datetime,
    *,
    streams: tuple[str, ...] = ("settlement",),
    margin: timedelta = timedelta(hours=3),
) -> tuple[list[DemandReading], int]:
    """Readings are filtered by *arrival* (cutoff) and positioned by *sampling time*.

    Each method consumes an explicitly declared stable source (`stream`), so a fine
    frequency feed and a settlement feed can coexist without cross-method mixing.
    Duplicates are grouped by (stream, kind, measured_from, measured_to): the latest
    arrival before the cutoff is authoritative; extra copies are counted, not summed.
    """
    rows = (
        db.execute(
            select(DemandReading)
            .where(
                DemandReading.meter_id == meter.id,
                DemandReading.stream.in_(streams),
                DemandReading.received_at <= cutoff,
                DemandReading.measured_to >= ps - margin,
                DemandReading.measured_from <= pe + margin,
            )
            .order_by(
                DemandReading.measured_from,
                DemandReading.measured_to,
                DemandReading.received_at,
                DemandReading.id,
            )
        )
        .scalars()
        .all()
    )
    groups: dict[tuple[str, str, datetime, datetime], DemandReading] = {}
    duplicates = 0
    for row in rows:
        key = (row.stream, row.kind, row.measured_from, row.measured_to)
        if key in groups:
            duplicates += 1
            prev = groups[key]
            if (row.received_at, row.id) >= (prev.received_at, prev.id):
                groups[key] = row
        else:
            groups[key] = row
    ordered = sorted(groups.values(), key=lambda r: (r.measured_from, r.measured_to))
    return ordered, duplicates


def _segments(
    meter: Meter, mode: str, readings: list[DemandReading]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Piecewise energy segments used by interval integration (and coverage math)."""
    mult = meter.multiplier or 1.0
    segments: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    if mode == MODE_REGISTER:
        regs = [r for r in readings if r.kind == "register"]
        for a, b in zip(regs, regs[1:]):
            dur = (b.measured_from - a.measured_from).total_seconds()
            if dur <= 0:
                invalid.append(
                    {
                        "kind": "zero_duration",
                        "from": a.measured_from,
                        "to": b.measured_from,
                        "detail": "相邻抄见时刻非单调，代表时段不可信",
                    }
                )
                continue
            if dur > SLOT_SECONDS:
                # A delta spanning more than one settlement cell only proves total
                # energy, not the 15-minute peak inside it: no interpolation.
                invalid.append(
                    {
                        "kind": "register_span",
                        "from": a.measured_from,
                        "to": b.measured_from,
                        "detail": (
                            "相邻抄见间隔超过一个 15 分钟结算格，"
                            "无法在不插值的前提下归因到单一格的峰值"
                        ),
                    }
                )
                continue
            delta = b.value - a.value
            if delta < -1e-9:
                invalid.append(
                    {
                        "kind": "rollback",
                        "from": a.measured_from,
                        "to": b.measured_from,
                        "detail": (
                            f"累计表读数回退 {a.value:.3f} → {b.value:.3f}；"
                            "负差不可计为需量，该时段列为未知"
                        ),
                    }
                )
                continue
            segments.append(
                {
                    "from": a.measured_from,
                    "to": b.measured_from,
                    "energy": delta * mult,
                    "source": "register_delta",
                }
            )
        return segments, invalid

    for r in readings:
        dur = (r.measured_to - r.measured_from).total_seconds()
        if dur <= 0:
            invalid.append(
                {
                    "kind": "zero_duration",
                    "from": r.measured_from,
                    "to": r.measured_to,
                    "detail": "读数代表时段长度为零，不可作为区间证据",
                }
            )
            continue
        if mode == MODE_POWER:
            energy = r.value * (dur / 3600.0) * mult
        else:
            energy = r.value * mult
        segments.append(
            {
                "from": r.measured_from,
                "to": r.measured_to,
                "energy": energy,
                "source": "interval_reading",
            }
        )
    return segments, invalid


def _overlap(a_from: datetime, a_to: datetime, b_from: datetime, b_to: datetime) -> float:
    start = max(a_from, b_from)
    end = min(a_to, b_to)
    return max(0.0, (end - start).total_seconds())


# ---------------------------------------------------------------- meter slots


def _grid_meter_slots(
    meter: Meter,
    mode: str,
    readings: list[DemandReading],
    segments: list[dict[str, Any]],
    invalid: list[dict[str, Any]],
    slots: list[datetime],
) -> list[dict[str, Any]]:
    mult = meter.multiplier or 1.0
    by_time = {
        r.measured_from: r for r in readings if r.kind == "register"
    }
    out: list[dict[str, Any]] = []
    for slot in slots:
        end = slot + timedelta(seconds=SLOT_SECONDS)
        if mode == MODE_REGISTER:
            start_read = by_time.get(slot)
            end_read = by_time.get(end)
            rollback = any(
                inv["kind"] == "rollback" and inv["from"] < end and inv["to"] > slot
                for inv in invalid
            )
            covered = sum(
                _overlap(s["from"], s["to"], slot, end) for s in segments
            )
            entry = {
                "status": STATUS_NONE,
                "demand_kw": None,
                "observed_kw": None,
                "covered_seconds": covered,
                "reasons": [],
                "upper_sources": [],
            }
            if rollback:
                entry["status"] = STATUS_PARTIAL
                entry["reasons"].append(
                    {
                        "kind": "rollback",
                        "detail": "累计表在格内回退，不能给出平均需量，也不产生负需量",
                    }
                )
            elif start_read is not None and end_read is not None:
                delta = (end_read.value - start_read.value) * mult
                if delta < -1e-9:
                    entry["status"] = STATUS_PARTIAL
                    entry["reasons"].append(
                        {"kind": "rollback", "detail": "格边界抄见回退，需量列为未知"}
                    )
                else:
                    entry["status"] = STATUS_FULL
                    entry["demand_kw"] = round(delta / (SLOT_SECONDS / 3600.0), 4)
                    entry["observed_kw"] = entry["demand_kw"]
                    entry["covered_seconds"] = SLOT_SECONDS
            elif covered > 0:
                entry["status"] = STATUS_PARTIAL
                entry["reasons"].append(
                    {
                        "kind": "register_no_boundary",
                        "detail": "缺少格边界抄见，无法在固定结算格上确认平均需量",
                    }
                )
                entry["observed_kw"] = round(
                    sum(
                        s["energy"]
                        * _overlap(s["from"], s["to"], slot, end)
                        / max(1.0, (s["to"] - s["from"]).total_seconds())
                        for s in segments
                    )
                    / (SLOT_SECONDS / 3600.0),
                    4,
                )
            else:
                entry["reasons"].append(
                    {"kind": "no_data", "detail": "该格没有任何已到达读数"}
                )
            out.append(entry)
            continue

        contained = 0.0
        energy = 0.0
        overlap = 0.0
        straddle = False
        for r in readings:
            if r.measured_to <= slot or r.measured_from >= end:
                continue
            dur = (r.measured_to - r.measured_from).total_seconds()
            if r.measured_from >= slot and r.measured_to <= end:
                contained += dur
                energy += (
                    r.value * (dur / 3600.0) * mult
                    if mode == MODE_POWER
                    else r.value * mult
                )
            else:
                # A reading whose representative period crosses a settlement boundary
                # cannot be attributed to one grid slot without assuming a profile.
                straddle = True
                overlap += _overlap(r.measured_from, r.measured_to, slot, end)
        entry = {
            "status": STATUS_NONE,
            "demand_kw": None,
            "observed_kw": None,
            "covered_seconds": contained,
            "reasons": [],
            "upper_sources": [],
        }
        if abs(contained - SLOT_SECONDS) < 1e-6 and not straddle:
            entry["status"] = STATUS_FULL
            entry["demand_kw"] = round(energy / (SLOT_SECONDS / 3600.0), 4)
            entry["observed_kw"] = entry["demand_kw"]
        elif contained > 0 or straddle:
            entry["status"] = STATUS_PARTIAL
            if straddle:
                entry["reasons"].append(
                    {
                        "kind": "boundary_straddle",
                        "detail": "存在跨格读数：按固定 15 分钟格归集时无法把能量分摊到单一格",
                    }
                )
            if contained < SLOT_SECONDS - 1e-6 and not straddle:
                entry["reasons"].append(
                    {
                        "kind": "incomplete_coverage",
                        "detail": f"格内仅覆盖 {int(contained)} 秒，未铺满 900 秒",
                    }
                )
            entry["observed_kw"] = round(
                energy / (SLOT_SECONDS / 3600.0), 4
            )  # observed portion only, never a 15-min average of the whole slot
        else:
            entry["reasons"].append(
                {"kind": "no_data", "detail": "该格没有任何已到达读数"}
            )
        out.append(entry)
    return out


def _integration_meter_slots(
    segments: list[dict[str, Any]],
    invalid: list[dict[str, Any]],
    slots: list[datetime],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for slot in slots:
        end = slot + timedelta(seconds=SLOT_SECONDS)
        energy = 0.0
        covered = 0.0
        for seg in segments:
            ov = _overlap(seg["from"], seg["to"], slot, end)
            if ov <= 0:
                continue
            dur = (seg["to"] - seg["from"]).total_seconds()
            energy += seg["energy"] * ov / dur
            covered += ov
        invalid_here = [
            inv for inv in invalid if inv["from"] < end and inv["to"] > slot
        ]
        entry = {
            "status": STATUS_NONE,
            "demand_kw": None,
            "observed_kw": round(energy / (SLOT_SECONDS / 3600.0), 4),
            "covered_seconds": round(covered, 1),
            "reasons": [],
            "upper_sources": [],
        }
        if invalid_here:
            # Evidence exists but cannot be trusted for a 15-minute average: this is
            # partial coverage, never "no data" and never a confirmed value.
            entry["status"] = STATUS_PARTIAL
            for inv in invalid_here:
                entry["reasons"].append({"kind": inv["kind"], "detail": inv["detail"]})
        elif covered >= SLOT_SECONDS - 1e-6:
            entry["status"] = STATUS_FULL
            entry["demand_kw"] = round(energy / (SLOT_SECONDS / 3600.0), 4)
        elif covered > 0:
            entry["status"] = STATUS_PARTIAL
            entry["reasons"].append(
                {
                    "kind": "incomplete_coverage",
                    "detail": (
                        f"区间积分仅覆盖 {int(covered)} 秒；断档期间不插值，"
                        "观测部分只作保守下界"
                    ),
                }
            )
        else:
            entry["reasons"].append(
                {"kind": "no_data", "detail": "该格没有任何可信区间证据"}
            )
        out.append(entry)
    return out


def _meter_upper(
    slot_index: int,
    slots: list[datetime],
    entries: list[dict[str, Any]],
    cfg: DemandMeterConfig | None,
    meter: Meter,
) -> tuple[float | None, list[dict[str, Any]]]:
    """Conservative physical upper bound for a non-full slot, with explicit provenance.

    Only two documented sources are allowed: the equipment rating, or a registered
    maximum credible step from a *full* adjacent slot. A historical maximum is never
    used as a generic argument that a gap cannot be higher.
    """
    entry = entries[slot_index]
    if entry["status"] == STATUS_FULL and entry["demand_kw"] is not None:
        return float(entry["demand_kw"]), [
            {
                "kind": "observed",
                "label": "该格已完整覆盖，直接采用 15 分钟平均需量",
                "value_kw": float(entry["demand_kw"]),
            }
        ]
    sources: list[dict[str, Any]] = []
    values: list[float] = []
    if cfg is not None and cfg.rated_kw is not None:
        values.append(float(cfg.rated_kw))
        sources.append(
            {
                "kind": "rated",
                "label": f"设备额定上限 {meter.code}",
                "value_kw": float(cfg.rated_kw),
            }
        )
    if cfg is not None and cfg.neighbor_step_kw is not None:
        step = float(cfg.neighbor_step_kw)
        nearest: tuple[int, float] | None = None
        for idx, other in enumerate(entries):
            if other["status"] != STATUS_FULL or other["demand_kw"] is None:
                continue
            dist = abs(idx - slot_index)
            bound = float(other["demand_kw"]) + step * dist
            if nearest is None or bound < nearest[1]:
                nearest = (idx, bound)
        if nearest is not None:
            values.append(nearest[1])
            sources.append(
                {
                    "kind": "neighbor_step",
                    "label": (
                        f"可信相邻格 + 每格最大变化 {step:g} kW"
                        f"（相邻格 {iso(slots[nearest[0]])}）"
                    ),
                    "value_kw": round(nearest[1], 4),
                    "step_kw": step,
                    "neighbor_slot": iso(slots[nearest[0]]),
                }
            )
    if not values:
        return None, sources
    chosen = min(values)
    return round(chosen, 4), sources


# ---------------------------------------------------------------- building report


def compute_demand_report(
    db: Session,
    building_code: str,
    method: str,
    ps: datetime,
    pe: datetime,
    cutoff: datetime,
    *,
    product_field: str = "electricity",
) -> dict[str, Any]:
    from ..models import Building  # local import keeps the module import-light

    building = (
        db.execute(select(Building).where(Building.code == building_code))
        .scalars()
        .first()
    )
    if building is None:
        raise ValueError(f"未知楼栋 {building_code}")
    if method not in METHODS:
        raise ValueError(f"未知口径 {method}")
    # Only settlement cells that have fully ended at the cutoff are due. Cells beyond
    # the cutoff are not missing evidence and are never treated as zero or unknown.
    slots = [
        s
        for s in _slot_starts(ps, pe)
        if s + timedelta(seconds=SLOT_SECONDS) <= cutoff
    ]
    # Only meters explicitly registered for peak-demand participation are used; the
    # ordinary sampling meters in the same building are a different instrument role.
    meters = (
        db.execute(
            select(Meter)
            .join(DemandMeterConfig, DemandMeterConfig.meter_id == Meter.id)
            .where(Meter.building_id == building.id)
            .order_by(Meter.code)
        )
        .scalars()
        .all()
    )
    contract = resolve_contract(db, building.id, product_field, ps, pe)

    meter_meta: list[dict[str, Any]] = []
    per_meter_entries: list[dict[str, Any]] = []
    duplicates_total = 0
    for meter in meters:
        cfg = meter_config(db, meter)
        mode = _mode_of(meter, cfg)
        stream = (
            (cfg.grid_stream if method == METHOD_GRID else cfg.integration_stream)
            if cfg is not None
            else "settlement"
        )
        readings, duplicates = load_readings(
            db, meter, ps, pe, cutoff, streams=(stream,)
        )
        duplicates_total += duplicates
        segments, invalid = _segments(meter, mode, readings)
        if method == METHOD_GRID:
            entries = _grid_meter_slots(meter, mode, readings, segments, invalid, slots)
        else:
            entries = _integration_meter_slots(segments, invalid, slots)
        upper_bounds: list[float | None] = []
        upper_sources: list[list[dict[str, Any]]] = []
        for idx, entry in enumerate(entries):
            bound, sources = _meter_upper(idx, slots, entries, cfg, meter)
            # a partial observation itself is a lower bound; the bound must not undercut it
            if entry["status"] != STATUS_FULL and bound is not None and entry["observed_kw"] is not None:
                bound = max(bound, float(entry["observed_kw"]))
            upper_bounds.append(bound)
            upper_sources.append(sources)
        per_meter_entries.append(
            {
                "meter": meter,
                "mode": mode,
                "cfg": cfg,
                "entries": entries,
                "upper_bounds": upper_bounds,
                "upper_sources": upper_sources,
                "invalid": invalid,
                "readings": readings,
                "duplicates": duplicates,
            }
        )
        meter_meta.append(
            {
                "meter_code": meter.code,
                "meter_name": meter.name,
                "mode": mode,
                "mode_label": MODE_META[mode],
                "stream": stream,
                "cumulative": meter.cumulative,
                "multiplier": meter.multiplier,
                "rating_kw": cfg.rated_kw if cfg else None,
                "neighbor_step_kw": cfg.neighbor_step_kw if cfg else None,
                "readings_used": len(readings),
                "duplicates": duplicates,
                "rollbacks": len([i for i in invalid if i["kind"] == "rollback"]),
                "zero_duration": len([i for i in invalid if i["kind"] == "zero_duration"]),
            }
        )

    if not per_meter_entries:
        return _empty_report(
            building, method, slots, ps, pe, cutoff, contract, meter_meta
        )

    building_slots: list[dict[str, Any]] = []
    for idx, slot in enumerate(slots):
        meter_parts: list[dict[str, Any]] = []
        for pm in per_meter_entries:
            e = pm["entries"][idx]
            meter_parts.append(
                {
                    "meter_code": pm["meter"].code,
                    "status": e["status"],
                    "demand_kw": e["demand_kw"],
                    "observed_kw": e["observed_kw"],
                    "covered_seconds": e["covered_seconds"],
                    "reasons": e["reasons"],
                    "upper_kw": pm["upper_bounds"][idx],
                    "upper_sources": pm["upper_sources"][idx],
                }
            )
        statuses = [p["status"] for p in meter_parts]
        if all(s == STATUS_FULL for s in statuses):
            status = STATUS_FULL
        elif all(s == STATUS_NONE for s in statuses):
            status = STATUS_NONE
        else:
            status = STATUS_PARTIAL
        demand = (
            round(sum(p["demand_kw"] for p in meter_parts), 4)
            if status == STATUS_FULL
            else None
        )
        observed = round(
            sum(float(p["observed_kw"] or 0.0) for p in meter_parts), 4
        )
        upper: float | None
        upper_src: list[dict[str, Any]] = []
        if status == STATUS_FULL:
            upper = demand
            upper_src = [
                {
                    "kind": "observed",
                    "label": "全部表计在该格完整覆盖",
                    "value_kw": demand,
                }
            ]
        else:
            total = 0.0
            bounded = True
            for pm, part in zip(per_meter_entries, meter_parts):
                if part["status"] == STATUS_FULL:
                    total += float(part["demand_kw"] or 0.0)
                elif part["upper_kw"] is None:
                    bounded = False
                    break
                else:
                    total += float(part["upper_kw"])
                    upper_src.extend(part["upper_sources"])
            upper = round(total, 4) if bounded else None
        lower = float(demand) if status == STATUS_FULL else observed
        building_slots.append(
            {
                "slot": iso(slot),
                "slot_end": iso(slot + timedelta(seconds=SLOT_SECONDS)),
                "status": status,
                "demand_kw": demand,
                "observed_kw": observed,
                "lower_kw": round(lower, 4),
                "upper_kw": upper,
                "upper_sources": upper_src,
                "contract_kw": contract.capacity_kw if contract else None,
                "above_contract": bool(
                    (demand is not None and contract is not None and demand > contract.capacity_kw)
                ),
                "meters": meter_parts,
            }
        )

    full = [s for s in building_slots if s["status"] == STATUS_FULL]
    partial = [s for s in building_slots if s["status"] == STATUS_PARTIAL]
    none = [s for s in building_slots if s["status"] == STATUS_NONE]
    unknown = partial + none

    confirmed = max(full, key=lambda s: s["demand_kw"]) if full else None
    confirmed_kw = float(confirmed["demand_kw"]) if confirmed else None
    peak_lower = max([s["lower_kw"] for s in building_slots] + ([0.0])) if building_slots else 0.0
    if confirmed_kw is not None:
        peak_lower = max(peak_lower, confirmed_kw)
    bounded = all(s["upper_kw"] is not None for s in building_slots)
    peak_upper = max((s["upper_kw"] for s in building_slots), default=None) if bounded else None

    capacity = contract.capacity_kw if contract else None
    candidates = [
        s
        for s in unknown
        if capacity is not None
        and (
            s["upper_kw"] is None
            or s["upper_kw"] > capacity
            or s["observed_kw"] > capacity
        )
    ]
    # Observed energy in a partially covered cell can only under-count the cell's true
    # average (loads are non-negative), so observed > contract already proves exceeded
    # even though the cell has no confirmed 15-minute average.
    lower_bound_exceeded = (
        capacity is not None
        and any(s["lower_kw"] > capacity for s in unknown)
    )

    if capacity is None:
        contract_status = CONTRACT_NO_CAPACITY
        contract_basis = "no_contract"
        risk = RISK_UNBOUNDED if unknown else RISK_NONE
    elif not building_slots:
        # No settlement cell has ended at the cutoff: there is no evidence at all, so
        # "not exceeded" would be an unsupported claim.
        contract_status = CONTRACT_NOT_PROVEN
        contract_basis = "no_due_cells"
        risk = RISK_UNBOUNDED
    elif confirmed_kw is not None and confirmed_kw > capacity:
        contract_status = CONTRACT_EXCEEDED
        contract_basis = "confirmed"
        risk = RISK_NONE if not unknown else (
            RISK_UNBOUNDED if peak_upper is None else RISK_BOUNDED
        )
    elif lower_bound_exceeded:
        contract_status = CONTRACT_EXCEEDED
        contract_basis = "observed_lower_bound"
        risk = RISK_UNBOUNDED if peak_upper is None else RISK_BOUNDED
    elif not unknown:
        contract_status = CONTRACT_NOT_EXCEEDED
        contract_basis = "full_coverage"
        risk = RISK_NONE
    elif peak_upper is not None and peak_upper <= capacity:
        contract_status = CONTRACT_NOT_EXCEEDED
        contract_basis = "physical_bound"
        risk = RISK_NONE
    else:
        contract_status = CONTRACT_NOT_PROVEN
        contract_basis = "unknown"
        risk = RISK_BOUNDED if peak_upper is not None else RISK_UNBOUNDED

    conclusion = _conclusion_text(
        contract_status, contract_basis, confirmed_kw, capacity, len(unknown)
    )

    duplicates = sum(pm["duplicates"] for pm in per_meter_entries)
    warnings: list[dict[str, Any]] = []
    rollbacks = sum(
        len([i for i in pm["invalid"] if i["kind"] == "rollback"])
        for pm in per_meter_entries
    )
    zero_dur = sum(
        len([i for i in pm["invalid"] if i["kind"] == "zero_duration"])
        for pm in per_meter_entries
    )
    if rollbacks:
        warnings.append(
            {
                "kind": "rollback",
                "detail": f"{rollbacks} 个累计表区间回退：列未知，未产生负需量",
            }
        )
    if zero_dur:
        warnings.append(
            {
                "kind": "zero_duration",
                "detail": f"{zero_dur} 个读数代表时段不可信（长度为零）",
            }
        )
    if duplicates:
        warnings.append(
            {
                "kind": "duplicate",
                "detail": f"{duplicates} 条重复报文：同一采样时段只保留一条代表读数，未累加",
            }
        )
    if any(
        r["kind"] == "boundary_straddle"
        for s in building_slots
        for m in s["meters"]
        for r in m["reasons"]
    ):
        warnings.append(
            {
                "kind": "boundary_straddle",
                "detail": "存在跨 15 分钟格边界的读数：固定格归集无法分摊，仅区间积分可用",
            }
        )

    return {
        "building_code": building.code,
        "building_name": building.name,
        "product_field": product_field,
        "method": method,
        "method_label": METHOD_META[method]["name"],
        "period_start": iso(ps),
        "period_end": iso(pe),
        "data_cutoff": iso(cutoff),
        "unit": "kW",
        "slot_minutes": 15,
        "resolution_seconds": SLOT_SECONDS,
        "contract": (
            {
                "capacity_kw": capacity,
                "source": contract.source,
                "valid_from": iso(contract.valid_from),
                "valid_to": iso(contract.valid_to),
                "note": contract.note,
            }
            if contract
            else None
        ),
        "confirmed_peak": {
            "demand_kw": confirmed_kw,
            "slot": confirmed["slot"] if confirmed else None,
            "slot_end": confirmed["slot_end"] if confirmed else None,
        },
        "risk_interval": {
            "lower_kw": round(peak_lower, 4),
            "confirmed_kw": confirmed_kw,
            "upper_kw": peak_upper,
            "unit": "kW",
            "explanation": _risk_explanation(confirmed_kw, peak_upper, capacity, len(unknown)),
        },
        "contract_status": contract_status,
        "contract_basis": contract_basis,
        "unknown_risk": risk,
        "conclusion": conclusion,
        "counts": {
            "slots": len(building_slots),
            "full": len(full),
            "partial": len(partial),
            "none": len(none),
            "unknown": len(unknown),
            "candidates": len(candidates),
            "duplicates": duplicates,
            "rollbacks": rollbacks,
            "zero_duration": zero_dur,
        },
        "slots": building_slots,
        "gap_slots": [
            {
                "slot": s["slot"],
                "slot_end": s["slot_end"],
                "status": s["status"],
                "observed_kw": s["observed_kw"],
                "lower_kw": s["lower_kw"],
                "upper_kw": s["upper_kw"],
                "reasons": sorted(
                    {r["kind"] for m in s["meters"] for r in m["reasons"]}
                ),
            }
            for s in unknown
        ],
        "candidate_peak_slots": [
            {
                "slot": s["slot"],
                "slot_end": s["slot_end"],
                "status": s["status"],
                "observed_kw": s["observed_kw"],
                "upper_kw": s["upper_kw"],
                "reason": (
                    "无覆盖且无保守上界，可能隐藏高于合同容量的峰值"
                    if s["upper_kw"] is None
                    else "保守上界高于合同容量，不能排除超限"
                ),
            }
            for s in candidates
        ],
        "peak_candidates": [
            {
                "slot": s["slot"],
                "demand_kw": s["demand_kw"],
                "rank": i + 1,
            }
            for i, s in enumerate(sorted(full, key=lambda x: -x["demand_kw"])[:5])
        ],
        "meters": meter_meta,
        "warnings": warnings,
        "inputs": {
            "normalization_cutoff": iso(cutoff),
            "uses": "sampling_time",
            "received_time_used_for": "arrival_cutoff_only",
            "contract_threshold_bound": True,
            "methods": list(METHODS),
        },
    }


def _conclusion_text(
    status: str,
    basis: str,
    confirmed: float | None,
    capacity: float | None,
    unknown: int,
) -> str:
    if status == CONTRACT_NO_CAPACITY:
        return "未登记合同容量：只给出已确认峰值与未知范围，不能判断合同状态"
    if status == CONTRACT_NOT_PROVEN and basis == "no_due_cells":
        return "尚无已结束的结算格：数据截止点太早，不能给出合同结论"
    if status == CONTRACT_EXCEEDED:
        if basis == "observed_lower_bound":
            return (
                f"已证实超限：未完整覆盖格的观测下界已高于合同容量 {capacity:.1f} kW"
                if capacity is not None
                else "已证实超限"
            )
        return (
            f"已证实超限：已确认峰值 {confirmed:.1f} kW 高于合同容量 {capacity:.1f} kW"
            if confirmed is not None and capacity is not None
            else "已证实超限"
        )
    if status == CONTRACT_NOT_EXCEEDED and basis == "full_coverage":
        return (
            f"已证实未超限：全部结算格完整覆盖，最高已确认格 "
            f"{confirmed:.1f} kW ≤ 合同容量 {capacity:.1f} kW"
            if confirmed is not None and capacity is not None
            else "已证实未超限"
        )
    if status == CONTRACT_NOT_EXCEEDED and basis == "physical_bound":
        return "已证实未超限：覆盖不足格的保守物理上界仍不高于合同容量"
    return (
        f"尚未证实超限：已确认峰值 {confirmed:.1f} kW 低于合同容量 "
        f"{capacity:.1f} kW，但仍有 {unknown} 个格未完整覆盖，未知范围内可能更高"
        if confirmed is not None and capacity is not None
        else f"尚未证实超限：仍有 {unknown} 个格未完整覆盖"
    )


def _risk_explanation(
    confirmed: float | None, upper: float | None, capacity: float | None, unknown: int
) -> str:
    if unknown == 0:
        return "所有结算格均已完整覆盖：真实峰值等于已确认峰值。"
    if upper is None:
        return (
            "存在未完整覆盖格且没有可用的保守物理上界：真实峰值只有下界"
            "（已确认峰值），上界未知；不能用历史最大值假定缺口不会更高。"
        )
    if capacity is not None and upper <= capacity:
        return (
            f"未完整覆盖格的保守上界 {upper:.1f} kW 仍不高于合同容量 "
            f"{capacity:.1f} kW，因此可证实未超限。"
        )
    return (
        f"真实峰值落在 [{confirmed:.1f}, {upper:.1f}] kW 之间："
        "上界来自设备额定上限/登记的相邻读数变化率，未知格可能更高。"
        if confirmed is not None
        else f"真实峰值上界 {upper:.1f} kW，来自显式登记的保守边界。"
    )


def _empty_report(
    building,
    method: str,
    slots: list[datetime],
    ps: datetime,
    pe: datetime,
    cutoff: datetime,
    contract: ContractCapacity | None,
    meter_meta: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "building_code": building.code,
        "building_name": building.name,
        "product_field": "electricity",
        "method": method,
        "method_label": METHOD_META[method]["name"],
        "period_start": iso(ps),
        "period_end": iso(pe),
        "data_cutoff": iso(cutoff),
        "unit": "kW",
        "slot_minutes": 15,
        "resolution_seconds": SLOT_SECONDS,
        "contract": (
            {"capacity_kw": contract.capacity_kw, "source": contract.source}
            if contract
            else None
        ),
        "confirmed_peak": {"demand_kw": None, "slot": None, "slot_end": None},
        "risk_interval": {
            "lower_kw": 0.0,
            "confirmed_kw": None,
            "upper_kw": None,
            "unit": "kW",
            "explanation": "没有参与峰值需量计算的表计或读数，结论未知。",
        },
        "contract_status": CONTRACT_NO_CAPACITY
        if contract is None
        else CONTRACT_NOT_PROVEN,
        "contract_basis": "no_data",
        "unknown_risk": RISK_UNBOUNDED,
        "conclusion": "没有参与峰值需量计算的表计或读数：不能给出合同结论",
        "counts": {
            "slots": len(slots),
            "full": 0,
            "partial": 0,
            "none": len(slots),
            "unknown": len(slots),
            "candidates": 0,
            "duplicates": 0,
            "rollbacks": 0,
            "zero_duration": 0,
        },
        "slots": [],
        "gap_slots": [],
        "candidate_peak_slots": [],
        "peak_candidates": [],
        "meters": meter_meta,
        "warnings": [{"kind": "no_data", "detail": "没有可用的峰值需量表计配置"}],
        "inputs": {
            "normalization_cutoff": iso(cutoff),
            "uses": "sampling_time",
            "received_time_used_for": "arrival_cutoff_only",
            "contract_threshold_bound": True,
            "methods": list(METHODS),
        },
    }


# ---------------------------------------------------------------- publication


def _signature(payload: dict[str, Any]) -> tuple:
    return (
        payload["contract_status"],
        payload["confirmed_peak"]["demand_kw"],
        payload["confirmed_peak"]["slot"],
        payload["risk_interval"]["upper_kw"],
        tuple(
            (s["slot"], s["status"], s["demand_kw"], s["upper_kw"])
            for s in payload["slots"]
        ),
    )


def _delta(prev: DemandReport | None, payload: dict[str, Any]) -> dict[str, Any] | None:
    if prev is None:
        return None
    prev_details = prev.details or {}
    prev_peak = (prev_details.get("confirmed_peak") or {}).get("demand_kw")
    prev_slot = (prev_details.get("confirmed_peak") or {}).get("slot")
    prev_status = prev.contract_status
    new_peak = payload["confirmed_peak"]["demand_kw"]
    new_slot = payload["confirmed_peak"]["slot"]
    changed = (
        prev_peak != new_peak
        or prev_slot != new_slot
        or prev_status != payload["contract_status"]
        or (prev_details.get("counts") or {}).get("unknown")
        != payload["counts"]["unknown"]
    )
    note_parts: list[str] = []
    if prev_slot != new_slot and new_slot:
        note_parts.append(f"峰值时间格由 {_short(prev_slot)} 变为 {_short(new_slot)}")
    if prev_peak != new_peak:
        note_parts.append(
            f"已确认峰值 {_num(prev_peak)} → {_num(new_peak)} kW"
        )
    if prev_status != payload["contract_status"]:
        note_parts.append(
            f"合同状态 {prev_status} → {payload['contract_status']}"
        )
    return {
        "previous_report_id": prev.id,
        "previous_revision": prev.revision,
        "previous_cutoff": iso(prev.data_cutoff),
        "current_cutoff": payload["data_cutoff"],
        "confirmed_peak_kw_before": prev_peak,
        "confirmed_peak_kw_after": new_peak,
        "peak_slot_before": prev_slot,
        "peak_slot_after": new_slot,
        "contract_status_before": prev_status,
        "contract_status_after": payload["contract_status"],
        "unknown_slots_before": (prev_details.get("counts") or {}).get("unknown"),
        "unknown_slots_after": payload["counts"]["unknown"],
        "conclusion_changed": changed,
        "summary": "；".join(note_parts) if note_parts else "结论未变化（迟到读数未改变峰值结论）",
    }


def _short(value: str | None) -> str:
    return value.replace("T", " ").replace("Z", "")[5:16] if value else "—"


def _num(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else "—"


def latest_report(
    db: Session,
    building_code: str,
    method: str,
    ps: datetime,
    pe: datetime,
    *,
    product_field: str = "electricity",
) -> DemandReport | None:
    return (
        db.execute(
            select(DemandReport)
            .where(
                DemandReport.building_code == building_code,
                DemandReport.product_field == product_field,
                DemandReport.method == method,
                DemandReport.period_start == ps,
                DemandReport.period_end == pe,
            )
            .order_by(DemandReport.revision.desc(), DemandReport.id.desc())
        )
        .scalars()
        .first()
    )


def publish_report(
    db: Session,
    building_code: str,
    method: str,
    ps: datetime,
    pe: datetime,
    cutoff: datetime,
    *,
    product_field: str = "electricity",
) -> DemandReport:
    payload = compute_demand_report(
        db, building_code, method, ps, pe, cutoff, product_field=product_field
    )
    prev = latest_report(db, building_code, method, ps, pe, product_field=product_field)
    signature = _signature(payload)
    if (
        prev is not None
        and prev.data_cutoff == cutoff
        and _signature(prev.details or {}) == signature
    ):
        prev.recompute_count = (prev.recompute_count or 1) + 1
        prev.computed_at = utcnow()
        prev.details = payload
        prev.counts = payload["counts"]
        prev.confirmed_peak_kw = payload["confirmed_peak"]["demand_kw"]
        prev.confirmed_peak_slot = _dt(payload["confirmed_peak"]["slot"])
        prev.lower_kw = payload["risk_interval"]["lower_kw"]
        prev.upper_kw = payload["risk_interval"]["upper_kw"]
        prev.conclusion = payload["conclusion"]
        prev.contract_status = payload["contract_status"]
        prev.unknown_risk = payload["unknown_risk"]
        db.flush()
        return prev

    delta = _delta(prev, payload)
    if delta is not None:
        payload["delta"] = delta
    revision = (prev.revision + 1) if prev else 0
    version = (prev.version + 1) if prev else 1
    report = DemandReport(
        building_code=building_code,
        building_name=payload["building_name"],
        product_field=product_field,
        method=method,
        period_start=ps,
        period_end=pe,
        data_cutoff=cutoff,
        revision=revision,
        version=version,
        conclusion=payload["conclusion"],
        contract_status=payload["contract_status"],
        unknown_risk=payload["unknown_risk"],
        confirmed_peak_kw=payload["confirmed_peak"]["demand_kw"],
        confirmed_peak_slot=_dt(payload["confirmed_peak"]["slot"]),
        contract_kw=(payload.get("contract") or {}).get("capacity_kw"),
        lower_kw=payload["risk_interval"]["lower_kw"],
        upper_kw=payload["risk_interval"]["upper_kw"],
        counts=payload["counts"],
        details=payload,
    )
    db.add(report)
    db.flush()
    return report


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.rstrip("Z"))


def publish_all(
    db: Session,
    ps: datetime,
    pe: datetime,
    cutoff: datetime,
    *,
    methods: tuple[str, ...] = METHODS,
    building_codes: list[str] | None = None,
) -> list[DemandReport]:
    from ..models import Building

    buildings = (
        db.execute(select(Building).order_by(Building.code)).scalars().all()
    )
    if building_codes:
        buildings = [b for b in buildings if b.code in set(building_codes)]
    out: list[DemandReport] = []
    for building in buildings:
        for method in methods:
            out.append(publish_report(db, building.code, method, ps, pe, cutoff))
    return out


def report_payload(report: DemandReport) -> dict[str, Any]:
    details = dict(report.details or {})
    details.update(
        {
            "id": report.id,
            "revision": report.revision,
            "version": report.version,
            "recompute_count": report.recompute_count,
            "data_cutoff": iso(report.data_cutoff),
            "computed_at": iso(report.computed_at),
            "contract_status": report.contract_status,
            "unknown_risk": report.unknown_risk,
            "conclusion": report.conclusion,
            "confirmed_peak_kw": report.confirmed_peak_kw,
        }
    )
    return details


def default_period(db: Session) -> tuple[datetime, datetime]:
    """默认报告期：模拟日的前一天（已结束的完整结算日）。"""
    clock = db.get(SimClock, 1)
    day = clock.day if clock is not None else "2026-11-10"
    end = datetime.fromisoformat(day + "T00:00:00")
    return end - timedelta(days=1), end
