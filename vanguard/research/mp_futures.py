"""Market Profile on the FUTURES series -- real volume, tradeable prints.

WHY THIS EXISTS. Every profile so far was built on the spot index, which has two
defects the whole 2026-08-28 research pass kept tripping over: the index carries
ZERO volume (forcing TPO-only profiles and making VWAP/absorption impossible),
and the index is not tradeable -- the overnight-gap edge, the one finding that
survived everything, is UNVERIFIED on the instrument that would actually carry
it. This module closes both gaps for the window where futures data exists.

THE DATA, and its honest limits. index_futures_candles now holds Upstox
expired-contract candles: 24 monthly contracts per symbol, 2024-07/08 onward.
FIVE YEARS WAS REQUESTED AND DOES NOT EXIST AT THE BROKER -- the expired API
starts mid-2024, so every futures result rests on ~25 months and must not be
quietly compared against 5-year spot numbers.

CONTINUOUS STITCHING. One session's bars come from the FRONT contract: the
nearest unexpired expiry with data that session. No back-adjustment is applied
-- rollover gaps are real cash flows to a holder, and for session-level profile
work each session lives entirely inside one contract anyway. The expiry used is
carried on every row so a rollover session can be identified and, where a
roll-day matters (overnight holds across expiry), excluded explicitly.

    from research.mp_futures import load_futures_sessions, load_futures_bars
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.mp_auction import add_context, sessions

FUT_BAR_SQL = """
WITH ranked AS (
    SELECT underlying,
           (time AT TIME ZONE 'Asia/Kolkata') AS ts,
           date(time AT TIME ZONE 'Asia/Kolkata') AS dt,
           expiry, open, high, low, close, volume,
           -- front contract per session: nearest expiry not yet passed
           ROW_NUMBER() OVER (
               PARTITION BY underlying, time
               ORDER BY (expiry < date(time AT TIME ZONE 'Asia/Kolkata')),
                        expiry NULLS LAST
           ) AS rn
    FROM index_futures_candles
    WHERE interval = %(interval)s AND time >= %(start)s
      AND underlying = ANY(%(names)s)
      AND open IS NOT NULL AND close IS NOT NULL
)
SELECT underlying, ts, dt, expiry, open, high, low, close, volume
FROM ranked WHERE rn = 1
ORDER BY underlying, ts
"""


def load_futures_bars(connection, names: list[str], start,
                      interval: str = "30minute") -> pd.DataFrame:
    bars = pd.read_sql(FUT_BAR_SQL, connection,
                       params={"start": start, "names": names,
                               "interval": interval})
    bars["ts"] = pd.to_datetime(bars["ts"])
    bars["dt"] = pd.to_datetime(bars["dt"])
    for c in ("open", "high", "low", "close", "volume"):
        bars[c] = pd.to_numeric(bars[c], errors="coerce")
    # session bars only -- futures rows can carry pre-open prints
    tod = bars["ts"].dt.time
    bars = bars[(tod >= pd.Timestamp("09:15").time())
                & (tod <= pd.Timestamp("15:15").time())]
    return bars.reset_index(drop=True)


def load_futures_sessions(connection, names: list[str], start,
                          interval: str = "30minute") -> pd.DataFrame:
    """The mp_auction session frame, built from futures bars.

    Adds per-session VOLUME truth the spot frame never had, plus the expiry the
    session traded and a roll flag (the session whose front expiry differs from
    the previous session's -- exclude these from overnight holds)."""
    bars = load_futures_bars(connection, names, start, interval)
    if bars.empty:
        return bars
    s = add_context(sessions(bars))
    exp = (bars.groupby(["underlying", "dt"])["expiry"].first()
           .rename("front_expiry").reset_index())
    s = s.merge(exp, on=["underlying", "dt"], how="left")
    s["roll_session"] = (s.groupby("underlying")["front_expiry"]
                         .transform(lambda x: x != x.shift(1)))
    s.loc[s.groupby("underlying").head(1).index, "roll_session"] = False
    # session VWAP from the bars -- REAL volume, unlike the index
    pv = bars.assign(pv=((bars["high"] + bars["low"] + bars["close"]) / 3)
                     * bars["volume"])
    vw = (pv.groupby(["underlying", "dt"])
          .agg(vol=("volume", "sum"), pvs=("pv", "sum")).reset_index())
    vw["vwap"] = vw["pvs"] / vw["vol"].replace(0, np.nan)
    s = s.merge(vw[["underlying", "dt", "vol", "vwap"]],
                on=["underlying", "dt"], how="left")
    s["close_vs_vwap"] = (s["close"] / s["vwap"] - 1.0) * 100
    return s
