from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    Building,
    Consumer,
    ConsumerDependency,
    ContractCapacity,
    DemandMeterConfig,
    DemandReading,
    MaintenanceWindow,
    Meter,
    MeterCommitment,
    Sample,
    SimClock,
    Source,
    User,
)

SIM_DAY = "2026-11-10"
SIM_START = datetime(2026, 11, 10, 0, 0)


def _password_hash(raw: str) -> str:
    return hashlib.sha256(("slo-demo::" + raw).encode()).hexdigest()


def _ensure_users(db: Session) -> None:
    if db.execute(select(func.count()).select_from(User)).scalar_one() > 0:
        return
    db.add_all(
        [
            User(
                username="admin",
                display_name="平台管理员",
                role="admin",
                password_hash=_password_hash("admin123"),
            ),
            User(
                username="ops",
                display_name="值班调度",
                role="operator",
                password_hash=_password_hash("ops123"),
            ),
        ]
    )


def _ensure_catalog(db: Session) -> None:
    if db.execute(select(func.count()).select_from(Source)).scalar_one() > 0:
        return

    sources = {
        "electricity": Source(
            code="electricity",
            name="电力",
            unit="kWh",
            product_field="electricity",
            description="园区总表与楼栋分表，15 分钟累计表码",
        ),
        "water": Source(
            code="water",
            name="自来水",
            unit="m³",
            product_field="water",
            description="楼栋水表，15 分钟区间用量",
        ),
        "steam": Source(
            code="steam",
            name="蒸汽",
            unit="t",
            product_field="steam",
            description="生产蒸汽流量计，15 分钟累计表码",
        ),
        "pv": Source(
            code="pv",
            name="光伏",
            unit="kWh",
            product_field="pv",
            description="光伏逆变器，15 分钟区间发电量",
        ),
        "demand": Source(
            code="demand",
            name="峰值需量",
            unit="kW",
            product_field="demand",
            description="楼栋峰值需量计量点，独立的结算格/区间积分证据来源",
        ),
    }
    db.add_all(sources.values())
    db.flush()

    buildings = {
        "B1": Building(code="B1", name="研发楼", kind="office"),
        "B2": Building(code="B2", name="生产车间", kind="factory"),
        "B3": Building(code="B3", name="仓储中心", kind="warehouse"),
        "B4": Building(code="B4", name="综合办公楼", kind="office"),
    }
    db.add_all(buildings.values())
    db.flush()

    meter_defs = [
        ("M-ELE-B1-01", "研发楼 1# 进线", "electricity", "B1", True, 1.0),
        ("M-ELE-B1-02", "研发楼 2# 进线", "electricity", "B1", True, 1.0),
        ("M-ELE-B2-01", "生产车间动力进线", "electricity", "B2", True, 1.0),
        ("M-ELE-B3-01", "仓储中心进线", "electricity", "B3", True, 1.0),
        ("M-ELE-B4-01", "综合办公楼进线", "electricity", "B4", True, 1.0),
        ("M-WAT-B1-01", "研发楼水表", "water", "B1", False, 1.0),
        ("M-WAT-B2-01", "生产车间水表", "water", "B2", False, 1.0),
        ("M-WAT-B4-01", "综合办公楼水表", "water", "B4", False, 1.0),
        ("M-STM-B2-01", "生产车间蒸汽流量计", "steam", "B2", True, 1.0),
        ("M-STM-B3-01", "仓储蒸汽流量计", "steam", "B3", True, 1.0),
        ("M-PV-01", "光伏 1# 逆变器", "pv", "B2", False, 1.0),
    ]
    for code, name, src, bld, cumulative, mult in meter_defs:
        db.add(
            Meter(
                code=code,
                name=name,
                source_id=sources[src].id,
                building_id=buildings[bld].id,
                cumulative=cumulative,
                multiplier=mult,
            )
        )
    db.flush()

    for meter in db.execute(select(Meter)).scalars():
        db.add(
            MeterCommitment(
                meter_id=meter.id,
                valid_from=datetime(2026, 1, 1),
                cadence_minutes=15,
                allowed_lateness_minutes=30,
                note="标准 15 分钟采样承诺",
            )
        )

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

    consumers = {
        "property_dashboard": Consumer(
            code="property_dashboard",
            name="物业调度大屏",
            kind="property_dashboard",
            description="近实时负荷曲线，用于安排楼宇负荷；容忍暂定数据与短缺口",
            tolerance_minutes=60,
            min_coverage=0.5,
            max_gap_slots=40,
            require_certified=False,
        ),
        "settlement_billing": Consumer(
            code="settlement_billing",
            name="月度结算账单",
            kind="settlement",
            description="按核验日汇总出账，要求完整、按期到达且已核验",
            tolerance_minutes=30,
            min_coverage=0.98,
            max_gap_slots=0,
            require_certified=True,
        ),
        "energy_assessment": Consumer(
            code="energy_assessment",
            name="节能考核报表",
            kind="assessment",
            description="考核研发楼与生产车间单位能耗，依赖核验日汇总与光伏",
            tolerance_minutes=30,
            min_coverage=0.95,
            max_gap_slots=3,
            require_certified=True,
        ),
    }
    db.add_all(consumers.values())
    db.flush()

    rt, daily = "energy.realtime", "energy.daily"

    def dep(consumer: Consumer, product: str, field: str, building: str | None = None):
        db.add(
            ConsumerDependency(
                consumer_id=consumer.id,
                product_code=product,
                product_field=field,
                building_code=building,
                valid_from=datetime(2026, 1, 1),
                note="",
            )
        )

    for field in ("electricity", "water"):
        dep(consumers["property_dashboard"], rt, field)
    for field in ("electricity", "water", "steam"):
        dep(consumers["settlement_billing"], daily, field)
    for building in ("B1", "B2"):
        dep(consumers["energy_assessment"], daily, "electricity", building)
    dep(consumers["energy_assessment"], daily, "pv")
    db.flush()


def seed_if_empty(db: Session) -> None:
    _ensure_users(db)
    _ensure_catalog(db)
    db.flush()
    clock = db.get(SimClock, 1)
    if clock is not None:
        db.commit()
        return

    from .services.ingest import generate_samples
    from .services.simulator import run_checks

    start = SIM_START - timedelta(hours=12)
    clock = SimClock(id=1, now=SIM_START + timedelta(hours=6), day=SIM_DAY)
    db.add(clock)
    db.flush()
    generate_samples(db, start, clock.now)
    db.commit()
    run_checks(db, clock.now)
    db.commit()
    _ensure_demand_catalog(db)
    db.commit()


# ---------------------------------------------------------------- peak demand

DEMAND_DAY = "2026-11-09"
DEMAND_START = datetime(2026, 11, 9, 0, 0)
DEMAND_END = datetime(2026, 11, 10, 0, 0)

# Per-building 15-minute demand shapes in kW. The shapes deliberately reproduce every
# peak-evidence case the acceptance script must cover:
#   B1 grid     : peak at 14:00-14:15 (895 kW <= 900 contract, no gaps) -> proven safe
#   B2 grid     : morning peak 800 kW, noon 12:00-13:00 gap, unseen peak 13:00 candidate
#   B3 grid     : register rollback 10:30, confirmed 1150 > 1000 contract -> exceeded
#   B4 grid     : boundary-straddling readings, partial cells -> more unknown cells
#   B1 integration: same data integrated as 5-minute readings
#   B2 integration: high-frequency readings recover the noon peak exactly
DEMAND_PLAN = {
    # B1: clean settlement feed, duplicate report at 09:00, peak 14:00-15:00 = 890 kW.
    #     Contract 900 kW; every cell full -> "已证实未超限".
    "B1": {
        "contract_kw": 900.0,
        "mode": "energy_interval",
        "rated_kw": 1200.0,
        "neighbor_step_kw": 80.0,
        "shape": [
            (0, 8, 620), (8, 10, 700), (10, 12, 660), (12, 14, 640),
            (14, 15, 890), (15, 24, 630),
        ],
    },
    # B2: settlement feed missing 12:00-13:00; the hidden noon cell is 960 kW.
    #     Confirmed grid peak is the 800 kW morning cell < 850 contract, but the gap
    #     has no conservative bound below capacity -> "尚未证实超限" + candidate slots.
    #     Integration uses the complete 5-minute feed and confirms 960 kW > 850.
    "B2": {
        "contract_kw": 850.0,
        "mode": "energy_interval",
        "rated_kw": 1300.0,
        "neighbor_step_kw": None,
        "shape": [
            (0, 8, 545), (8, 10, 800), (10, 12, 700), (12, 13, 960),
            (13, 14, 760), (14, 16, 700), (16, 24, 600),
        ],
    },
    # B3: cumulative register; rollback at 10:30 and confirmed 1134 kW > 1000 contract.
    "B3": {
        "contract_kw": 1000.0,
        "mode": "register",
        "rated_kw": 1500.0,
        "neighbor_step_kw": None,
        "shape": [
            (0, 8, 905), (8, 10, 940), (10, 11, 930), (11, 13, 950),
            (13, 14, 1040), (14, 15, 1120), (15, 16, 1134), (16, 24, 990),
        ],
    },
    # B4: every settlement reading straddles the 15-minute boundary. Grid leaves all
    #     cells partial (more unknown cells); integration reconstructs full coverage
    #     and the adjacent-step bound proves the 700 kW contract was not exceeded.
    "B4": {
        "contract_kw": 700.0,
        "mode": "energy_interval",
        "rated_kw": 1050.0,
        "neighbor_step_kw": 45.0,
        "shape": [
            (0, 8, 480), (8, 10, 540), (10, 12, 500), (12, 13, 615),
            (13, 16, 560), (16, 17, 655), (17, 24, 520),
        ],
    },
}

ROLLBACK_SLOT_INDEX = 42  # 10:30-10:45 for the B3 register


def _demand_profile_kw(code: str, idx: int, plan: dict[str, Any]) -> float:
    """Deterministic 15-minute average demand in kW for slot index ``idx``."""
    hour = idx / 4.0
    kw = plan["shape"][-1][2]
    for start_h, end_h, level in plan["shape"]:
        if start_h <= hour < end_h:
            kw = level
            break
    # small deterministic ripple so individual slots remain distinguishable
    return round(kw + 1.5 * math.sin(idx * 0.9 + len(code)), 3)


def _ensure_demand_catalog(db: Session) -> None:
    if db.execute(select(func.count()).select_from(DemandReading)).scalar_one() > 0:
        return
    buildings = {b.code: b for b in db.execute(select(Building)).scalars().all()}
    demand_source = (
        db.execute(select(Source).where(Source.code == "demand")).scalars().first()
    )
    if demand_source is None:
        # databases seeded before this product shipped
        demand_source = Source(
            code="demand",
            name="峰值需量",
            unit="kW",
            product_field="demand",
            description="楼栋峰值需量计量点，独立的结算格/区间积分证据来源",
        )
        db.add(demand_source)
        db.flush()
    for code, plan in DEMAND_PLAN.items():
        building = buildings[code]
        meter = (
            db.execute(
                select(Meter).where(Meter.code == f"M-DMD-{code}-01")
            )
            .scalars()
            .first()
        )
        if meter is None:
            meter = Meter(
                code=f"M-DMD-{code}-01",
                name=f"{building.name} 峰值需量计量点",
                source_id=demand_source.id,
                building_id=building.id,
                multiplier=1.0,
                cumulative=plan["mode"] == "register",
                note="峰值需量证据数据产品：15 分钟结算格 / 区间积分",
            )
            db.add(meter)
            db.flush()
        elif meter.source_id != demand_source.id:
            meter.source_id = demand_source.id
        if (
            db.execute(
                select(DemandMeterConfig).where(DemandMeterConfig.meter_id == meter.id)
            )
            .scalars()
            .first()
            is None
        ):
            db.add(
                DemandMeterConfig(
                    meter_id=meter.id,
                    mode=plan["mode"],
                    grid_stream="settlement",
                    integration_stream=(
                        "fine" if code in ("B1", "B2") else "settlement"
                    ),
                    rated_kw=plan.get("rated_kw"),
                    neighbor_step_kw=plan.get("neighbor_step_kw"),
                    note="额定上限/相邻变化率仅作保守物理边界来源",
                )
            )
        if (
            db.execute(
                select(ContractCapacity).where(
                    ContractCapacity.building_id == building.id,
                    ContractCapacity.product_field == "electricity",
                )
            )
            .scalars()
            .first()
            is None
        ):
            db.add(
                ContractCapacity(
                    building_id=building.id,
                    product_field="electricity",
                    capacity_kw=plan["contract_kw"],
                    valid_from=datetime(2026, 1, 1),
                    source="园区供电合同附件二",
                    note="月度最高 15 分钟平均需量上限；仅用于容量复核",
                )
            )
        if (
            db.execute(
                select(DemandReading).where(DemandReading.meter_id == meter.id)
            )
            .scalars()
            .first()
            is None
        ):
            _generate_demand_readings(db, meter, code, plan)
    db.flush()


def _generate_demand_readings(
    db: Session, meter: Meter, code: str, plan: dict[str, Any]
) -> None:
    """Emit readings for the whole settlement day, with the planned evidence defects.

    measured_from/measured_to are sampling times; received_at only controls visibility
    at a data cutoff. Each method declares a stable stream: the settlement feed is what
    the 15-minute grid uses, the fine feed is what interval integration may use.
    """
    n_slots = int((DEMAND_END - DEMAND_START).total_seconds() // 900)
    profile = [_demand_profile_kw(code, i, plan) for i in range(n_slots)]

    def add(kind: str, frm: datetime, to: datetime, value: float, *, stream: str,
            note: str = "", received: datetime | None = None) -> None:
        db.add(
            DemandReading(
                meter_id=meter.id,
                kind=kind,
                stream=stream,
                measured_from=frm,
                measured_to=to,
                value=value,
                received_at=received or to,
                note=note,
            )
        )

    if plan["mode"] == "register":
        reading = 100000.0
        add("register", DEMAND_START, DEMAND_START, reading, stream="settlement")
        for i, kw in enumerate(profile):
            end = DEMAND_START + timedelta(minutes=15 * (i + 1))
            if code == "B3" and i == ROLLBACK_SLOT_INDEX:
                # cumulative rollback at 10:30: the register drops 5000 kWh
                reading -= 5000.0
            else:
                reading += kw * 0.25
            add("register", end, end, round(reading, 3), stream="settlement")
        return

    gap = (48, 52) if code == "B2" else None  # 12:00-13:00 settlement cells absent
    straddle = code == "B4"
    for i, kw in enumerate(profile):
        slot = DEMAND_START + timedelta(minutes=15 * i)
        end = slot + timedelta(minutes=15)
        if gap and gap[0] <= i < gap[1]:
            continue
        if straddle:
            frm, to = slot + timedelta(minutes=7), end + timedelta(minutes=7)
            value = kw * (to - frm).total_seconds() / 3600.0
        else:
            frm, to = slot, end
            value = kw * 0.25
        add("energy", frm, to, round(value, 4), stream="settlement")

    if code != "B4":
        # duplicate report for 09:00-09:15 (same sampling period, later arrival)
        slot = DEMAND_START + timedelta(minutes=15 * 36)
        add(
            "energy",
            slot,
            slot + timedelta(minutes=15),
            round(profile[36] * 0.25, 4),
            stream="settlement",
            note="重复报文",
            received=slot + timedelta(minutes=30),
        )

    if code in ("B1", "B2"):
        # 5-minute high-frequency feed: complete coverage of the settlement day, so
        # interval integration can see the shape the 15-minute feed may be missing.
        for i in range(int((DEMAND_END - DEMAND_START).total_seconds() // 300)):
            start = DEMAND_START + timedelta(minutes=5 * i)
            end = start + timedelta(minutes=5)
            kw = profile[i // 3]
            add("energy", start, end, round(kw / 12.0, 4), stream="fine")
    db.flush()
