"""Shared candle interval definitions for live persistence and backfill policy."""
from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timedelta


CANDLE_INTERVALS_MINUTES: "OrderedDict[int, str]" = OrderedDict(
    [
        (1, "1minute"),
        (3, "3minute"),
        (5, "5minute"),
        (15, "15minute"),
        (30, "30minute"),
    ]
)

BACKFILL_RETENTION_DAYS: dict[str, int] = {
    "1minute": 90,    # 3 months
    "3minute": 180,   # 6 months
    "5minute": 365,   # 1 year
    "15minute": 1095, # 3 years
    "30minute": 1095, # 3 years
}


def interval_name(minutes: int) -> str:
    return CANDLE_INTERVALS_MINUTES[minutes]


def interval_minutes(interval: str) -> int:
    normalized = str(interval or "30minute")
    for minutes, label in CANDLE_INTERVALS_MINUTES.items():
        if label == normalized:
            return minutes
    raise KeyError(f"Unsupported interval: {interval}")


def floor_timestamp(timestamp: datetime, minutes: int) -> datetime:
    return timestamp.replace(
        minute=(timestamp.minute // minutes) * minutes,
        second=0,
        microsecond=0,
    )


def retention_start(interval: str, end_date: date | None = None) -> date:
    reference = end_date or date.today()
    days = BACKFILL_RETENTION_DAYS.get(str(interval or "30minute"), 365)
    return reference - timedelta(days=days)
