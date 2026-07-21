"""(2) LTF ENTRY TIMERS — fixed A PRIORI, evaluated ONLY when the daily
regime state (prior-session close, `*_lag1`) is on.

Direction convention: every timer is defined for a LONG entry inside an UP
regime; inside a DOWN regime the mirrored condition fires a PE entry. The
timer fires at the CLOSE of the signal bar; the entry fill is that bar's
close (primary) with next-bar-open declared as the fill-lag sensitivity —
the cascade study showed a 1-bar fill lag can flip a marginal edge, so both
are always reported.

Timeframes: 30m native; 1h built by pairing 30m bars from the session open
(09:15 IST origin, label = first half's stamp, bar complete only when both
halves exist). ORB on the "1h" frame uses the first hour as the range.

T1  deep_macd  (the survivor construct, str_below0 family — inside an UP
    regime a deep-below-zero 30m MACD cross is a PULLBACK entry):
    MACD(12,26,9) line crosses ABOVE signal, macd_line < 0, and depth
    d = -macd_line/close >= DEEP_MIN. DEEP_MIN = 0.0015 primary;
    sensitivity {0.0010, 0.0015, 0.0025} (declared, not selected).
T2  pullback_anchor: prev close > EMA20, this bar's low <= EMA20, close >
    EMA20 (touch-and-reclaim of the LTF anchor from above). Session-VWAP
    anchor declared as the sensitivity variant; EMA20 is primary.
T3  orb: opening range = first bar of the frame (30m: 09:15-09:45). A later
    bar CLOSING above the OR high fires, once per session, only before
    13:00 IST (declared cutoff — afternoon breakouts are a different animal).
T4  macd_plain (CONTROL TIMER): MACD(12,26,9) cross above signal, no depth
    or sign requirement. Isolates what "deep" adds to T1.

CAUSALITY CONTRACT: every indicator value at bar t uses bars <= t only
(EMAs are recursive; VWAP is a running session sum; OR is the first bar).
Verified by prefix-invariance in test_causality.py at rtol 1e-12.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MACD_F, MACD_S, MACD_SIG = 12, 26, 9
DEEP_MIN = 0.0015            # primary; sensitivity {0.0010, 0.0015, 0.0025}
EMA_ANCHOR = 20
ORB_CUTOFF_IST_MIN = 13 * 60  # fire only before 13:00 IST
TIMERS = ("deep_macd", "pullback_anchor", "orb", "macd_plain")


def _ema(x: pd.Series, n: int) -> pd.Series:
    return x.ewm(span=n, adjust=False, min_periods=n).mean()


def add_session_cols(bars: pd.DataFrame) -> pd.DataFrame:
    """INPUT CONTRACT: one row per (underlying, time) — spot must already be
    deduped by option_read_layer.load_spot_csvs (the spot table carries
    cross-source duplicates). Duplicate timestamps are dropped defensively
    (keep first after a STABLE sort) so indicator paths are deterministic."""
    b = bars.sort_values("time", kind="mergesort").drop_duplicates(
        "time", keep="first").reset_index(drop=True).copy()
    ist = b["time"].dt.tz_convert("Asia/Kolkata")
    b["session"] = pd.to_datetime(ist.dt.date)
    b["ist_min"] = ist.dt.hour * 60 + ist.dt.minute
    return b


def to_hourly(b30: pd.DataFrame) -> pd.DataFrame:
    """Pair 30m bars into 1h bars from the session open. Complete pairs only.
    Label = the FIRST half's timestamp; the bar is known at the SECOND half's
    close, so downstream fills use time + 60min as the actionable stamp."""
    b = add_session_cols(b30)
    b["pair"] = b.groupby("session").cumcount() // 2
    g = b.groupby(["session", "pair"])
    h = pd.DataFrame({
        "time": g["time"].first(), "open": g["open"].first(),
        "high": g["high"].max(), "low": g["low"].min(),
        "close": g["close"].last(), "volume": g["volume"].sum(),
        "n": g["close"].size(), "ist_min": g["ist_min"].first(),
    }).reset_index()
    h = h[h["n"] == 2].drop(columns=["pair", "n"])
    h["session"] = pd.to_datetime(h["session"])
    return h.sort_values("time").reset_index(drop=True)


def timer_signals(bars: pd.DataFrame, deep_min: float = DEEP_MIN,
                  anchor: str = "ema") -> pd.DataFrame:
    """One underlying, one timeframe. Returns bars + boolean long-entry
    columns t_deep_macd, t_pullback_anchor, t_orb, t_macd_plain and the
    mirrored short columns (suffix _dn). Regime gating happens downstream:
    signal AND (regime_lag1 == +1) -> CE ; mirrored AND -1 -> PE."""
    b = add_session_cols(bars) if "session" not in bars.columns else \
        bars.sort_values("time", kind="mergesort").drop_duplicates(
            "time", keep="first").reset_index(drop=True).copy()
    c = b["close"].astype(float)

    macd = _ema(c, MACD_F) - _ema(c, MACD_S)
    sig = macd.ewm(span=MACD_SIG, adjust=False,
                   min_periods=MACD_SIG).mean()
    x_up = (macd > sig) & (macd.shift(1) <= sig.shift(1))
    x_dn = (macd < sig) & (macd.shift(1) >= sig.shift(1))
    depth_up = (-macd / c)                     # >0 when macd below zero
    depth_dn = (macd / c)
    b["t_macd_plain"] = x_up.fillna(False)
    b["t_macd_plain_dn"] = x_dn.fillna(False)
    b["t_deep_macd"] = (x_up & (macd < 0) & (depth_up >= deep_min)).fillna(False)
    b["t_deep_macd_dn"] = (x_dn & (macd > 0) & (depth_dn >= deep_min)).fillna(False)
    b["macd_depth"] = depth_up

    if anchor == "ema":
        a = _ema(c, EMA_ANCHOR)
    else:  # session vwap (running, causal)
        tp = (b["high"].astype(float) + b["low"].astype(float) + c) / 3.0
        v = b["volume"].astype(float).clip(lower=0)
        num = (tp * v).groupby(b["session"]).cumsum()
        den = v.groupby(b["session"]).cumsum()
        a = num / den.replace(0, np.nan)
    b["anchor"] = a
    prev_above = c.shift(1) > a.shift(1)
    prev_below = c.shift(1) < a.shift(1)
    b["t_pullback_anchor"] = (prev_above & (b["low"].astype(float) <= a)
                              & (c > a)).fillna(False)
    b["t_pullback_anchor_dn"] = (prev_below & (b["high"].astype(float) >= a)
                                 & (c < a)).fillna(False)

    # ORB: first bar of each session is the range; first later close beyond
    # it fires once, before the IST cutoff.
    or_hi = b.groupby("session")["high"].transform("first").astype(float)
    or_lo = b.groupby("session")["low"].transform("first").astype(float)
    is_first = b.groupby("session").cumcount() == 0
    brk_up = (~is_first) & (c > or_hi) & (b["ist_min"] < ORB_CUTOFF_IST_MIN)
    brk_dn = (~is_first) & (c < or_lo) & (b["ist_min"] < ORB_CUTOFF_IST_MIN)
    first_up = brk_up & (~brk_up.groupby(b["session"]).cummax().shift(1)
                         .fillna(False).astype(bool))
    first_dn = brk_dn & (~brk_dn.groupby(b["session"]).cummax().shift(1)
                         .fillna(False).astype(bool))
    b["t_orb"] = first_up.fillna(False)
    b["t_orb_dn"] = first_dn.fillna(False)
    return b
