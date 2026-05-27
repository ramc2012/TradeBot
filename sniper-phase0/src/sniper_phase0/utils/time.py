from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)


def to_ist(ts: pd.Timestamp | datetime) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        return ts.tz_localize(IST)
    return ts.tz_convert(IST)


def is_in_session(ts: pd.Timestamp) -> bool:
    ts = to_ist(ts)
    return SESSION_OPEN <= ts.time() <= SESSION_CLOSE


def minutes_into_session(ts: pd.Timestamp) -> int:
    ts = to_ist(ts)
    open_dt = datetime.combine(ts.date(), SESSION_OPEN, tzinfo=IST)
    return max(0, int((ts - open_dt).total_seconds() // 60))


def session_bounds(ts: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp]:
    ts = to_ist(ts)
    open_dt = pd.Timestamp(datetime.combine(ts.date(), SESSION_OPEN, tzinfo=IST))
    close_dt = pd.Timestamp(datetime.combine(ts.date(), SESSION_CLOSE, tzinfo=IST))
    return open_dt, close_dt


def walk_forward_splits(
    start: str,
    end: str,
    train_months: int,
    validate_months: int,
    test_months: int,
    step_months: int,
) -> list[tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    """Yield (train_start, train_end, val_start, val_end, test_start, test_end) windows.

    Half-open on the right: train_end is exclusive.
    """
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    splits = []
    cursor = start_ts
    while True:
        train_end = cursor + pd.DateOffset(months=train_months)
        val_end = train_end + pd.DateOffset(months=validate_months)
        test_end = val_end + pd.DateOffset(months=test_months)
        if test_end > end_ts + timedelta(days=1):
            break
        splits.append((cursor, train_end, train_end, val_end, val_end, test_end))
        cursor = cursor + pd.DateOffset(months=step_months)
    return splits
