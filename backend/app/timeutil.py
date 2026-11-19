from __future__ import annotations

from datetime import datetime, timedelta, timezone

SLOT_MINUTES = 15
WINDOW_HOURS = 6
SLOT = timedelta(minutes=SLOT_MINUTES)
WINDOW = timedelta(hours=WINDOW_HOURS)

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def floor_to(dt: datetime, delta: timedelta) -> datetime:
    total = int(delta.total_seconds())
    epoch = int(EPOCH.replace(tzinfo=None).timestamp())
    secs = int(dt.replace(tzinfo=None).timestamp())
    return datetime.fromtimestamp(epoch + ((secs - epoch) // total) * total, tz=timezone.utc).replace(tzinfo=None)


def ceil_to(dt: datetime, delta: timedelta) -> datetime:
    floored = floor_to(dt, delta)
    if floored == dt.replace(tzinfo=None):
        return floored
    return floored + delta


def slot_floor(dt: datetime) -> datetime:
    return floor_to(dt, SLOT)


def window_floor(dt: datetime) -> datetime:
    return floor_to(dt, WINDOW)


def slots_between(start: datetime, end: datetime, cadence_minutes: int = SLOT_MINUTES) -> list[datetime]:
    """Slots whose [start, start+cadence) lies fully inside [start, end)."""
    cadence = timedelta(minutes=cadence_minutes)
    cur = ceil_to(start, cadence)
    out: list[datetime] = []
    while cur + cadence <= end:
        out.append(cur)
        cur += cadence
    return out


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt is not None else None


def business_date_of(dt: datetime) -> str:
    return dt.date().isoformat()


def day_bounds(day: str) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(day + "T00:00:00")
    return start, start + timedelta(days=1)


def windows_of_day(day: str) -> list[datetime]:
    start, end = day_bounds(day)
    out: list[datetime] = []
    cur = start
    while cur < end:
        out.append(cur)
        cur += WINDOW
    return out


def fmt(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else None
