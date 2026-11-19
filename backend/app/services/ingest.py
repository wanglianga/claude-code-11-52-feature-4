from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import MaintenanceWindow, Meter, MeterCommitment, Sample
from ..timeutil import ceil_to, slots_between

ANCHOR = datetime(2026, 1, 1)


def _hash01(*parts: object) -> float:
    raw = "|".join(str(p) for p in parts).encode()
    return int(hashlib.sha256(raw).hexdigest()[:8], 16) / 0xFFFFFFFF


def meter_reading(meter: Meter, slot: datetime) -> tuple[float, bool]:
    """Deterministic meter emission. Cumulative meters are monotonically non-decreasing:
    the step per slot dominates every other term so resets only happen when a sample is
    explicitly replaced (acceptance scenario), not from noise."""
    index = int((slot - ANCHOR).total_seconds() // 900)
    hour = slot.hour + slot.minute / 60
    noise = _hash01(meter.code + "@" + slot.isoformat())
    field = meter.source.product_field
    if field == "electricity":
        base = 10000 + index * 28 + 120 * math.sin((hour / 24) * 2 * math.pi)
        return round(base + 0.8 * noise, 3), False
    if field == "water":
        return round(2.5 + 4 * noise, 3), False
    if field == "steam":
        base = 500 + index * 0.9 + 4 * math.sin((hour / 24) * 2 * math.pi)
        return round(base + 0.05 * noise, 3), False
    if field == "pv":
        if 7 <= hour < 18:
            daylight = math.sin(((hour - 7) / 11) * math.pi)
            return round(60 * daylight * (0.6 + 0.4 * noise), 3), False
        return 0.0, True
    return 0.0, False


def active_commitments(
    db: Session, meter_id: int, start: datetime, end: datetime
) -> list[MeterCommitment]:
    return (
        db.execute(
            select(MeterCommitment)
            .where(
                MeterCommitment.meter_id == meter_id,
                MeterCommitment.valid_from < end,
                (MeterCommitment.valid_to.is_(None)) | (MeterCommitment.valid_to > start),
            )
            .order_by(MeterCommitment.valid_from)
        )
        .scalars()
        .all()
    )


def generate_samples(
    db: Session, start: datetime, end: datetime, *, commit: bool = True
) -> int:
    """Meters emit samples slot by slot as simulated time passes."""
    if start >= end:
        return 0
    meters = db.execute(select(Meter).order_by(Meter.code)).scalars().all()
    created = 0
    for meter in meters:
        existing = {
            row
            for row in db.execute(
                select(Sample.slot_start).where(
                    Sample.meter_id == meter.id,
                    Sample.slot_start >= start,
                    Sample.slot_start < end,
                )
            ).scalars()
        }
        maintenance = (
            db.execute(
                select(MaintenanceWindow).where(
                    MaintenanceWindow.meter_id == meter.id,
                    MaintenanceWindow.allowed_deduction.is_(True),
                    MaintenanceWindow.ends_at > start,
                    MaintenanceWindow.starts_at < end,
                )
            )
            .scalars()
            .all()
        )
        commitments = active_commitments(db, meter.id, start, end) or []
        if not commitments:
            continue
        for c in commitments:
            seg_start = max(start, c.valid_from)
            seg_end = min(end, c.valid_to or end)
            for slot in slots_between(seg_start, seg_end, c.cadence_minutes):
                if slot in existing:
                    continue
                if any(m.starts_at <= slot < m.ends_at for m in maintenance):
                    continue
                value, declared_zero = meter_reading(meter, slot)
                db.add(
                    Sample(
                        meter_id=meter.id,
                        slot_start=slot,
                        value=value,
                        received_at=slot,
                        is_declared_zero=declared_zero,
                    )
                )
                existing.add(slot)
                created += 1
    if commit:
        db.flush()
    return created
