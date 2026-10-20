"""Completion checks for start-stamped intraday bars. Naive timestamps are UTC."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from market_data.candle_timeframes import interval_minutes


def completed_candles(
    rows: list[dict[str, Any]], interval: str, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    cutoff = now or datetime.now(timezone.utc)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    duration = timedelta(minutes=interval_minutes(interval))
    out = []
    for row in rows:
        try:
            start = datetime.fromisoformat(str(row["time"]).replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if start + duration <= cutoff:
                out.append(row)
        except (KeyError, TypeError, ValueError):
            continue
    return out
