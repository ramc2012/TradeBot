"""Daily-bar loader for the Gann higher-order path.

Why this exists
---------------
``GannTPDeltaService.live_snapshot`` passed ``lookback_sessions`` straight
through as ``lookback_days`` to the INTRADAY spot loader, so asking for a
"1day" frame meant pulling a deep 1-minute frame and resampling it.  The API
router capped that at 180 days, which put the ceiling on reachable daily
history at ~120 bars — nowhere near enough for calendar cycles, and an
enormous read for a tiny result.

The 30-minute store already holds the depth (NIFTY/BANKNIFTY back to
2021-06-21, ~1,250 daily sessions).  This module reads THAT, bounded, and
resamples to IST daily bars.

Database politeness
-------------------
Postgres has been OOM-killed twice today.  Every query here:

* bounds ``time`` DIRECTLY with literal UTC timestamps — never ``time::date``,
  never ``date_trunc(time, ...)``, never a bind-parameter interval.  Wrapping
  the partitioning column in a function defeats chunk exclusion and turns a
  bounded read into a ~1,300-chunk scan;
* bounds ``underlying`` and ``interval`` too, so the composite index does the
  work;
* is issued PER INSTRUMENT.  Never scan across the universe in one statement —
  iterate, and release each frame before loading the next.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

import pandas as pd

IST = timezone(timedelta(hours=5, minutes=30))

#: Source interval resampled up to daily.  30minute is the deepest broadly
#: populated series (225 symbols); 1minute is deeper per bar but far heavier
#: and covers fewer symbols.
SOURCE_INTERVAL = "30minute"

_SELECT = """
SELECT time, open, high, low, close, volume, oi
FROM underlying_spot_candles
WHERE underlying = $1
  AND interval = $2
  AND time >= $3::timestamptz
  AND time <  $4::timestamptz
ORDER BY time
"""


def utc_literal(moment: datetime) -> str:
    """Render a UTC bound the way the PG query rule requires."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00")


def window_bounds(*, sessions: int, as_of: datetime | None = None) -> tuple[datetime, datetime]:
    """Calendar bounds wide enough to contain ``sessions`` trading days.

    NSE trades ~250 sessions a year and MCX ~250 as well, so 1.55 calendar
    days per session with a 15-day cushion is a safe, tight envelope.  Being
    tight matters: every extra day is extra chunks.
    """
    end = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc) + timedelta(days=1)
    span_days = int(max(int(sessions), 1) * 1.55) + 15
    return end - timedelta(days=span_days), end


#: Maximum wick, as a fraction of a source bar's own close, that is treated as
#: real.  A 30-minute index bar does not travel 8 % from its own close; when it
#: appears to, the tape has been contaminated.
MAX_WICK_FRACTION = 0.08


def sanitize_wicks(frame: pd.DataFrame, band: float = MAX_WICK_FRACTION) -> tuple[pd.DataFrame, int]:
    """Clamp impossible intrabar excursions before aggregating to daily.

    This matters more here than almost anywhere else in the repo: a Gann anchor
    IS an extreme, so one corrupt print becomes the origin of the fan, the
    Square of Nine and every cycle count derived from it.  Verified case, live
    data: NIFTY 30-minute bar 2026-07-08 04:00Z carries ``high=27094.30`` with
    ``low=24207.30`` and ``close=24251.30`` — a +11.7 % wick on a bar whose own
    body spans 0.2 %.  That single print made 27094.30 the swing-high anchor for
    the whole daily frame.  It is the known Fyers cross-symbol tick
    contamination (see the 2026-07-20 finding) leaking into the candle store.

    The clamp does not invent data.  A wick beyond ``band`` is replaced by the
    bar's own body extreme, which is the most conservative value consistent
    with the bar actually having traded.  The count of clamped bars is returned
    so the caller can report it rather than silently cleaning.
    """
    if frame is None or frame.empty:
        return frame, 0
    out = frame.copy()
    close = pd.to_numeric(out["close"], errors="coerce")
    open_ = pd.to_numeric(out["open"], errors="coerce")
    high = pd.to_numeric(out["high"], errors="coerce")
    low = pd.to_numeric(out["low"], errors="coerce")
    body_high = pd.concat([open_, close], axis=1).max(axis=1)
    body_low = pd.concat([open_, close], axis=1).min(axis=1)
    ceiling = close * (1.0 + float(band))
    floor = close * (1.0 - float(band))
    high_bad = high > ceiling
    low_bad = low < floor
    out.loc[high_bad, "high"] = body_high[high_bad]
    out.loc[low_bad, "low"] = body_low[low_bad]
    return out, int((high_bad | low_bad).sum())


def resample_to_daily(rows: Iterable[dict[str, Any]] | pd.DataFrame) -> pd.DataFrame:
    """Intraday rows -> IST daily OHLCV bars.

    The bar ``time`` is the IST *session date* at midnight, tz-naive, which is
    what the feature engine and the cycle mapper both expect.  The incomplete
    current session is NOT dropped here — callers that need only completed
    bars must trim, exactly as ``data.build_feature_frame`` does intraday.
    """
    frame = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(list(rows))
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume", "oi"])
    frame = frame.copy()
    times = pd.to_datetime(frame["time"], utc=True, errors="coerce")
    frame = frame.loc[times.notna()].copy()
    if frame.empty:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume", "oi"])
    times = pd.to_datetime(frame["time"], utc=True, errors="coerce")
    frame["session"] = times.dt.tz_convert(IST).dt.normalize().dt.tz_localize(None)
    for column in ("open", "high", "low", "close", "volume", "oi"):
        if column not in frame.columns:
            frame[column] = 0.0
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame, _clamped = sanitize_wicks(frame)
    grouped = (
        frame.sort_values("time")
        .groupby("session", as_index=False)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
            oi=("oi", "last"),
        )
        .rename(columns={"session": "time"})
    )
    grouped = grouped.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    return grouped


def daily_session_dates(frame: pd.DataFrame) -> list[date]:
    if frame is None or frame.empty:
        return []
    return [pd.Timestamp(value).date() for value in frame["time"]]


async def fetch_daily_frame(
    connection: Any,
    underlying: str,
    *,
    sessions: int = 400,
    as_of: datetime | None = None,
    interval: str = SOURCE_INTERVAL,
) -> pd.DataFrame:
    """One bounded, single-instrument read -> daily bars.

    ``connection`` is anything exposing asyncpg's ``fetch(sql, *args)``.
    """
    start, end = window_bounds(sessions=sessions, as_of=as_of)
    rows = await connection.fetch(
        _SELECT,
        str(underlying).upper(),
        str(interval),
        start,
        end,
    )
    daily = resample_to_daily([dict(row) for row in rows])
    if len(daily.index) > int(sessions):
        daily = daily.tail(int(sessions)).reset_index(drop=True)
    return daily


async def fetch_daily_frames(
    connection: Any,
    underlyings: Sequence[str],
    *,
    sessions: int = 400,
    as_of: datetime | None = None,
    on_result: Any = None,
) -> dict[str, pd.DataFrame]:
    """Iterate instruments one at a time — never one wide scan.

    ``on_result(underlying, frame)`` is invoked per instrument so a caller can
    consume and DISCARD each frame instead of holding the whole universe in
    memory; when it is supplied nothing is accumulated.
    """
    out: dict[str, pd.DataFrame] = {}
    for symbol in underlyings:
        frame = await fetch_daily_frame(connection, symbol, sessions=sessions, as_of=as_of)
        if on_result is not None:
            maybe = on_result(symbol, frame)
            if hasattr(maybe, "__await__"):
                await maybe
        else:
            out[symbol] = frame
    return out


__all__ = [
    "IST",
    "SOURCE_INTERVAL",
    "MAX_WICK_FRACTION",
    "sanitize_wicks",
    "utc_literal",
    "window_bounds",
    "resample_to_daily",
    "daily_session_dates",
    "fetch_daily_frame",
    "fetch_daily_frames",
]
