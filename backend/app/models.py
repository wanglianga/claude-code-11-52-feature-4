from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .timeutil import utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(32), default="operator")
    password_hash: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SimClock(Base):
    __tablename__ = "sim_clock"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    now: Mapped[datetime] = mapped_column(DateTime)
    day: Mapped[str] = mapped_column(String(16))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Building(Base):
    __tablename__ = "buildings"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(64))


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    unit: Mapped[str] = mapped_column(String(32))
    product_field: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text, default="")

    meters: Mapped[list[Meter]] = relationship(back_populates="source")


class Meter(Base):
    __tablename__ = "meters"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    building_id: Mapped[int] = mapped_column(ForeignKey("buildings.id"))
    multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    cumulative: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="active")
    linked_from_meter_id: Mapped[int | None] = mapped_column(
        "linked_from_meter_id", ForeignKey("meters.id"), nullable=True
    )
    link_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")

    source: Mapped[Source] = relationship(back_populates="meters")
    building: Mapped[Building] = relationship()
    linked_from: Mapped[Meter | None] = relationship(
        "Meter",
        primaryjoin="Meter.linked_from_meter_id == Meter.id",
        remote_side="Meter.id",
        foreign_keys=[linked_from_meter_id],
        post_update=True,
    )


class MeterCommitment(Base):
    """采样承诺：有效安装期 + 计划频率 + 允许迟到 + 核验要求。"""

    __tablename__ = "meter_commitments"

    id: Mapped[int] = mapped_column(primary_key=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"), index=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cadence_minutes: Mapped[int] = mapped_column(Integer, default=15)
    allowed_lateness_minutes: Mapped[int] = mapped_column(Integer, default=30)
    certification_required: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    meter: Mapped[Meter] = relationship()


class MaintenanceWindow(Base):
    __tablename__ = "maintenance_windows"

    id: Mapped[int] = mapped_column(primary_key=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"), index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime)
    ends_at: Mapped[datetime] = mapped_column(DateTime)
    allowed_deduction: Mapped[bool] = mapped_column(Boolean, default=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="ops")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    meter: Mapped[Meter] = relationship()


class Sample(Base):
    __tablename__ = "samples"
    __table_args__ = (
        Index("ix_samples_meter_slot", "meter_id", "slot_start"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"))
    slot_start: Mapped[datetime] = mapped_column(DateTime)
    value: Mapped[float] = mapped_column(Float)
    received_at: Mapped[datetime] = mapped_column(DateTime)
    is_declared_zero: Mapped[bool] = mapped_column(Boolean, default=False)
    is_correction: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(32), default="meter")
    note: Mapped[str] = mapped_column(Text, default="")

    meter: Mapped[Meter] = relationship()


class HealthEvaluation(Base):
    __tablename__ = "health_evaluations"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "window_start", "window_end", "data_cutoff", "revision",
            name="uq_health_eval_key",
        ),
        Index("ix_health_eval_window", "window_start", "window_end"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    window_start: Mapped[datetime] = mapped_column(DateTime)
    window_end: Mapped[datetime] = mapped_column(DateTime)
    data_cutoff: Mapped[datetime] = mapped_column(DateTime)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32))
    freshness: Mapped[str] = mapped_column(String(32))
    coverage_num: Mapped[int] = mapped_column(Integer, default=0)
    coverage_den: Mapped[int] = mapped_column(Integer, default=0)
    coverage_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    verification_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    certified: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmable: Mapped[bool] = mapped_column(Boolean, default=False)
    max_lateness_minutes: Mapped[int] = mapped_column(Integer, default=0)
    recompute_count: Mapped[int] = mapped_column(Integer, default=1)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    source: Mapped[Source] = relationship()
    meters: Mapped[list[MeterEvaluation]] = relationship(
        back_populates="evaluation", cascade="all, delete-orphan"
    )


class MeterEvaluation(Base):
    __tablename__ = "meter_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    evaluation_id: Mapped[int] = mapped_column(
        ForeignKey("health_evaluations.id", ondelete="CASCADE"), index=True
    )
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"))
    building_code: Mapped[str] = mapped_column(String(32))
    expected: Mapped[int] = mapped_column(Integer, default=0)
    observed: Mapped[int] = mapped_column(Integer, default=0)
    missing: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    pending: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    late: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    maintenance: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    segments: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    verification: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    duplicate_slots: Mapped[int] = mapped_column(Integer, default=0)
    declared_zero_slots: Mapped[int] = mapped_column(Integer, default=0)
    consumption: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    evaluation: Mapped[HealthEvaluation] = relationship(back_populates="meters")
    meter: Mapped[Meter] = relationship()


class BreachFact(Base):
    """不可抹去的违约事实：即使事后补维护或补数据，事实保留并标记处置结果。"""

    __tablename__ = "breach_facts"
    __table_args__ = (
        UniqueConstraint("meter_id", "slot_start", "kind", name="uq_breach_meter_slot_kind"),
        Index("ix_breach_source", "source_id", "slot_start"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"))
    slot_start: Mapped[datetime] = mapped_column(DateTime)
    slot_end: Mapped[datetime] = mapped_column(DateTime)
    kind: Mapped[str] = mapped_column(String(32))  # missing | verification | late
    reason: Mapped[str] = mapped_column(String(128), default="")
    detected_at: Mapped[datetime] = mapped_column(DateTime)
    evaluation_id: Mapped[int | None] = mapped_column(ForeignKey("health_evaluations.id"), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by_sample_id: Mapped[int | None] = mapped_column(ForeignKey("samples.id"), nullable=True)
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id"), nullable=True)
    retroactive_maintenance_id: Mapped[int | None] = mapped_column(
        ForeignKey("maintenance_windows.id"), nullable=True
    )
    note: Mapped[str] = mapped_column(Text, default="")

    meter: Mapped[Meter] = relationship()


class ProductRun(Base):
    __tablename__ = "product_runs"
    __table_args__ = (
        UniqueConstraint(
            "product_code", "product_field", "window_start", "window_end",
            "data_cutoff", "revision", name="uq_product_run_key",
        ),
        Index("ix_product_window", "product_code", "product_field", "window_start"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_code: Mapped[str] = mapped_column(String(64))
    product_field: Mapped[str] = mapped_column(String(32))
    window_start: Mapped[datetime] = mapped_column(DateTime)
    window_end: Mapped[datetime] = mapped_column(DateTime)
    data_cutoff: Mapped[datetime] = mapped_column(DateTime)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32))
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    qualified_ranges: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    evaluation_ids: Mapped[list[int]] = mapped_column(JSONB, default=list)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Consumer(Base):
    __tablename__ = "consumers"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))  # property_dashboard | settlement | assessment
    description: Mapped[str] = mapped_column(Text, default="")
    tolerance_minutes: Mapped[int] = mapped_column(Integer, default=60)
    min_coverage: Mapped[float] = mapped_column(Float, default=0.8)
    max_gap_slots: Mapped[int] = mapped_column(Integer, default=0)
    require_certified: Mapped[bool] = mapped_column(Boolean, default=False)
    thresholds_version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    dependencies: Mapped[list[ConsumerDependency]] = relationship(
        back_populates="consumer", cascade="all, delete-orphan"
    )


class ConsumerDependency(Base):
    __tablename__ = "consumer_dependencies"

    id: Mapped[int] = mapped_column(primary_key=True)
    consumer_id: Mapped[int] = mapped_column(ForeignKey("consumers.id"), index=True)
    product_code: Mapped[str] = mapped_column(String(64))  # energy.realtime | energy.daily
    product_field: Mapped[str] = mapped_column(String(32))
    building_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")

    consumer: Mapped[Consumer] = relationship(back_populates="dependencies")


class ConsumerImpact(Base):
    __tablename__ = "consumer_impacts"
    __table_args__ = (
        UniqueConstraint(
            "consumer_id", "window_start", "window_end", "revision",
            name="uq_consumer_impact_window",
        ),
        Index("ix_impact_window", "window_start", "window_end"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    consumer_id: Mapped[int] = mapped_column(ForeignKey("consumers.id"))
    window_start: Mapped[datetime] = mapped_column(DateTime)
    window_end: Mapped[datetime] = mapped_column(DateTime)
    data_cutoff: Mapped[datetime] = mapped_column(DateTime)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32))  # ok | restricted | unavailable | waiting
    severity: Mapped[str] = mapped_column(String(32))
    affected_slots: Mapped[int] = mapped_column(Integer, default=0)
    ranges: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    facts: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    threshold_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    consumer: Mapped[Consumer] = relationship()


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    cause_category: Mapped[str] = mapped_column(String(32))  # data_gap | verification
    title: Mapped[str] = mapped_column(String(256))
    summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="open")
    opened_at_sim: Mapped[datetime] = mapped_column(DateTime)
    closed_at_sim: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_breach_at: Mapped[datetime] = mapped_column(DateTime)
    last_breach_at: Mapped[datetime] = mapped_column(DateTime)
    ranges: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    prior_incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id"), nullable=True)
    recovery_check_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    source: Mapped[Source] = relationship()
    events: Mapped[list[IncidentEvent]] = relationship(
        back_populates="incident", cascade="all, delete-orphan", order_by="IncidentEvent.id"
    )


class IncidentEvent(Base):
    __tablename__ = "incident_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    kind: Mapped[str] = mapped_column(String(48))
    at_sim: Mapped[datetime] = mapped_column(DateTime)
    message: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    incident: Mapped[Incident] = relationship(back_populates="events")


class RecoveryCheck(Base):
    __tablename__ = "recovery_checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("health_evaluations.id"))
    window_start: Mapped[datetime] = mapped_column(DateTime)
    window_end: Mapped[datetime] = mapped_column(DateTime)
    data_cutoff: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(32), default="completed")
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    incident: Mapped[Incident] = relationship()
    evaluation: Mapped[HealthEvaluation] = relationship()


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at_sim: Mapped[datetime] = mapped_column(DateTime)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(128), default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


# ---------------------------------------------------------------- peak demand


class ContractCapacity(Base):
    """合同容量：按楼栋/字段登记，报告只与它比较，不据此执行限电或计费。"""

    __tablename__ = "contract_capacities"
    __table_args__ = (Index("ix_contract_building", "building_id", "product_field"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    building_id: Mapped[int] = mapped_column(ForeignKey("buildings.id"))
    product_field: Mapped[str] = mapped_column(String(32), default="electricity")
    capacity_kw: Mapped[float] = mapped_column(Float)
    valid_from: Mapped[datetime] = mapped_column(DateTime)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(128), default="合同容量条款")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    building: Mapped[Building] = relationship()


class DemandMeterConfig(Base):
    """参与峰值需量计算的表计角色：计量模式、单位口径与物理上界来源。"""

    __tablename__ = "demand_meter_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"), unique=True)
    mode: Mapped[str] = mapped_column(String(32))  # energy_interval | register | power
    grid_stream: Mapped[str] = mapped_column(String(24), default="settlement")
    integration_stream: Mapped[str] = mapped_column(String(24), default="settlement")
    rated_kw: Mapped[float | None] = mapped_column(Float, nullable=True)
    neighbor_step_kw: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    meter: Mapped[Meter] = relationship()


class DemandReading(Base):
    """带采样时间的高频/15 分钟原始读数。

    measured_from/measured_to 是读数代表的采样时段（不是接收时间）；received_at 只
    用于数据截止点判断。register 模式 measured_from == measured_to == 表码抄见时刻。
    """

    __tablename__ = "demand_readings"
    __table_args__ = (
        Index("ix_demand_reading_meter", "meter_id", "measured_to"),
        Index("ix_demand_reading_received", "meter_id", "received_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"))
    kind: Mapped[str] = mapped_column(String(16))  # energy | register | power
    stream: Mapped[str] = mapped_column(String(24), default="settlement")
    measured_from: Mapped[datetime] = mapped_column(DateTime)
    measured_to: Mapped[datetime] = mapped_column(DateTime)
    value: Mapped[float] = mapped_column(Float)
    received_at: Mapped[datetime] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(32), default="meter")
    note: Mapped[str] = mapped_column(Text, default="")

    meter: Mapped[Meter] = relationship()


class DemandReport(Base):
    """按楼栋发布的峰值需量证据报告：一个数据截止点对应一个不可变版本。"""

    __tablename__ = "demand_reports"
    __table_args__ = (
        UniqueConstraint(
            "building_code", "product_field", "method", "period_start", "period_end",
            "data_cutoff", "revision", name="uq_demand_report_key",
        ),
        Index("ix_demand_report_lookup", "building_code", "method", "period_start"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    building_code: Mapped[str] = mapped_column(String(32))
    building_name: Mapped[str] = mapped_column(String(128), default="")
    product_field: Mapped[str] = mapped_column(String(32), default="electricity")
    method: Mapped[str] = mapped_column(String(24))  # grid | integration
    period_start: Mapped[datetime] = mapped_column(DateTime)
    period_end: Mapped[datetime] = mapped_column(DateTime)
    data_cutoff: Mapped[datetime] = mapped_column(DateTime)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    recompute_count: Mapped[int] = mapped_column(Integer, default=1)
    conclusion: Mapped[str] = mapped_column(Text, default="")
    contract_status: Mapped[str] = mapped_column(String(24))
    unknown_risk: Mapped[str] = mapped_column(String(16))
    confirmed_peak_kw: Mapped[float | None] = mapped_column(Float, nullable=True)
    confirmed_peak_slot: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    contract_kw: Mapped[float | None] = mapped_column(Float, nullable=True)
    lower_kw: Mapped[float] = mapped_column(Float, default=0.0)
    upper_kw: Mapped[float | None] = mapped_column(Float, nullable=True)
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
