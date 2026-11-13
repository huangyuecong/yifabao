"""Beijing-time days for people, UTC for storage.

Times are stored in UTC; "today" for a user in China starts at 00:00 Beijing time.
China has no daylight saving, so a fixed +8 offset is exact and needs no tz database.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

CHINA = timezone(timedelta(hours=8), "Asia/Shanghai")


def today() -> date:
    return datetime.now(CHINA).date()


def parse_day(value: str) -> date:
    """"today", "yesterday" or YYYY-MM-DD (Beijing time). Raises ValueError otherwise."""
    text = (value or "").strip().lower()
    if text in {"today", "今天"}:
        return today()
    if text in {"yesterday", "昨天"}:
        return today() - timedelta(days=1)
    return date.fromisoformat(text)


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """[start, end) of a Beijing day as naive UTC, matching how the database stores times."""
    start = datetime(day.year, day.month, day.day, tzinfo=CHINA).astimezone(UTC).replace(tzinfo=None)
    return start, start + timedelta(days=1)


def format_time(value: datetime | None) -> str:
    """'2026-11-13 14:05' in Beijing time; naive values are UTC."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(CHINA).strftime("%Y-%m-%d %H:%M")
