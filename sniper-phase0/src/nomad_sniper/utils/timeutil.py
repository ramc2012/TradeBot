"""Timezone handling. NSE trading is in IST (Asia/Kolkata, UTC+5:30).

Hard rule from CLAUDE.md: all timestamps in storage and code are timezone-aware IST.
Naive datetimes are a bug. The functions below enforce that.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pandas as pd
import pytz

IST = pytz.timezone("Asia/Kolkata")

# NSE F&O session — pre-open at 09:00, continuous from 09:15 to 15:30
NSE_SESSION_OPEN = time(9, 15)
NSE_SESSION_CLOSE = time(15, 30)
NSE_PREOPEN = time(9, 0)


def now_ist() -> datetime:
    """Current time in IST, timezone-aware."""
    return datetime.now(IST)


def ensure_ist(ts: datetime | pd.Timestamp) -> datetime:
    """Ensure a timestamp is IST-aware. Raises on naive timestamps.

    Naive datetimes silently coerced to UTC are the #1 source of bugs in NSE
    data pipelines. We refuse them.
    """
    if isinstance(ts, pd.Timestamp):
        ts = ts.to_pydatetime()
    if ts.tzinfo is None:
        raise ValueError(
            f"Naive datetime {ts!r} not allowed. Localize to IST explicitly:"
            f" `IST.localize(dt)` or `dt.tz_localize('Asia/Kolkata')`."
        )
    return ts.astimezone(IST)


def is_session_open(ts: datetime) -> bool:
    """True if `ts` falls inside the continuous NSE session window."""
    ts = ensure_ist(ts)
    t = ts.time()
    return NSE_SESSION_OPEN <= t <= NSE_SESSION_CLOSE


def session_start(d: date) -> datetime:
    """09:15 IST on the given date."""
    return IST.localize(datetime.combine(d, NSE_SESSION_OPEN))


def session_end(d: date) -> datetime:
    """15:30 IST on the given date."""
    return IST.localize(datetime.combine(d, NSE_SESSION_CLOSE))


def tpo_period_index(ts: datetime, tpo_minutes: int = 3) -> int:
    """Which TPO period (0-indexed) does this timestamp fall into?

    NSE session is 375 minutes (09:15–15:30). With 3-min TPOs that's 125 periods.
    With 30-min TPOs it's 12 (the classic Steidlmayer A-L convention).
    """
    ts = ensure_ist(ts)
    start = session_start(ts.date())
    if ts < start:
        return -1
    delta = ts - start
    return int(delta.total_seconds() // (tpo_minutes * 60))


def hour_block(ts: datetime) -> int:
    """Which clock-hour block of the session (0..6 for 09:15–15:30)."""
    ts = ensure_ist(ts)
    return (ts.hour * 60 + ts.minute - (9 * 60 + 15)) // 60


def floor_to_minute(ts: datetime, minutes: int = 1) -> datetime:
    """Floor a timestamp to the nearest N-minute boundary, IST-preserving."""
    ts = ensure_ist(ts)
    seconds_since_epoch = int(ts.timestamp())
    floored = seconds_since_epoch - (seconds_since_epoch % (minutes * 60))
    return datetime.fromtimestamp(floored, tz=IST)


def tod_bucket_key(ts: datetime) -> int:
    """Stable same-time-of-day key (minutes since 09:15), for trailing baselines."""
    ts = ensure_ist(ts)
    return (ts.hour * 60 + ts.minute) - (NSE_SESSION_OPEN.hour * 60 + NSE_SESSION_OPEN.minute)


def decision_grid(
    session_date: date,
    *,
    grid_minutes: int = 5,
    start: time = time(9, 30),
    end: time = time(15, 0),
) -> list[datetime]:
    """IST decision-grid timestamps for one session (spec section 3)."""
    points = []
    t = IST.localize(datetime.combine(session_date, start))
    end_dt = IST.localize(datetime.combine(session_date, end))
    step = timedelta(minutes=grid_minutes)
    while t <= end_dt:
        points.append(t)
        t += step
    return points


def trading_days_between(start: date, end: date) -> list[date]:
    """Mon-Fri only. Does NOT exclude NSE holidays — Phase 0 fix later if needed."""
    days = []
    d = start
    one = timedelta(days=1)
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += one
    return days
