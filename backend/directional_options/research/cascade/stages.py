"""(C-cascade) Stage-1 / Stage-2 definitions — fixed A PRIORI from the owner's model.

Owner's model (verbatim): "first small time frame confirms a move - then as
direction builds higher time frame confirm - then sustained large move happens".

So the object under test is a SEQUENCE:
    stage-1  : the LOWER timeframe (30-minute) confirms direction, at a moment
               when the HIGHER timeframe has NOT yet confirmed;
    stage-2  : the HIGHER timeframe (daily) subsequently confirms the same
               direction, within a bounded window;
    outcome  : a sustained large directional move follows.

Everything here is a CAUSAL FILTER (same contract as
../setups_2d3d/features.py, proven by test_causality_cascade.py):
value at row i depends only on rows 0..i.

Timing convention (conservative, no peeking):
  * a 30m stage-1 condition evaluated on bar t is actionable at the OPEN of
    bar t+1 (same session);
  * a DAILY stage-2 condition evaluated on the daily bar of session s uses
    only bars of session s and earlier, and is actionable at the OPEN of the
    first 30m bar of session s+1 (the daily bar of s is not closed until
    15:30 IST of s).
  * "the higher timeframe has not yet confirmed" at a 30m bar inside session s
    is evaluated on the daily bar of session s-1 (the last CLOSED daily bar).

Definitions are fixed before any measurement. Alternates exist only so the
grid can be reported honestly with multiplicity applied; the PRIMARY pair is
the one the verdict is based on.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setups_2d3d"))
from features import adx, atr, ema, macd, rsi, sma  # noqa: E402


# =========================================================================
# DAILY (higher timeframe) — stage-2 candidate conditions
# =========================================================================

def add_daily_stage_features(d: pd.DataFrame) -> pd.DataFrame:
    """Daily features for ONE underlying, ascending by session.

    Input columns: s_open, s_high, s_low, s_close.
    """
    c, h, l = d["s_close"], d["s_high"], d["s_low"]
    out = d.copy()
    out["D_sma20"] = sma(c, 20)
    out["D_ema20"] = ema(c, 20)
    out["D_ema50"] = ema(c, 50)
    out["D_rsi14"] = rsi(c, 14)
    ml, ms, mh = macd(c)
    out["D_macd"], out["D_macd_sig"], out["D_macd_hist"] = ml, ms, mh
    a, pdi, mdi = adx(h, l, c, 14)
    out["D_adx14"], out["D_pdi"], out["D_mdi"] = a, pdi, mdi
    out["D_atr14"] = atr(h, l, c, 14)
    out["D_atr_pct"] = out["D_atr14"] / c
    out["D_adx_up"] = out["D_adx14"] > out["D_adx14"].shift(3)
    return out


def daily_state(d: pd.DataFrame, variant: str, side: int) -> pd.Series:
    """Boolean 'higher timeframe is confirmed in `side` direction' STATE.

    A stage-2 EVENT is a False->True transition of this state (computed by
    stage2_events), which is exactly the owner's "then the higher timeframe
    confirms".
    """
    s = float(side)
    if variant == "primary":
        # daily MACD histogram agrees + price on the right side of SMA20
        # + daily ADX above 20 and rising vs 3 sessions ago.
        return (
            (s * d["D_macd_hist"] > 0)
            & (s * (d["s_close"] - d["D_sma20"]) > 0)
            & (d["D_adx14"] > 20)
            & d["D_adx_up"]
        )
    if variant == "ma":
        # pure moving-average regime on the daily
        return (s * (d["D_ema20"] - d["D_ema50"]) > 0) & (s * (d["s_close"] - d["D_ema20"]) > 0)
    if variant == "di":
        # Wilder directional system on the daily
        return (d["D_adx14"] > 20) & (s * (d["D_pdi"] - d["D_mdi"]) > 0)
    raise ValueError(variant)


def stage2_events(daily: pd.DataFrame, variant: str, side: int) -> pd.DataFrame:
    """False->True transitions of the daily state, per underlying.

    Returns rows (underlying, sidx, session) at which the higher timeframe
    FRESHLY confirms `side`. The event is actionable from session sidx+1.
    """
    out = []
    for u, g in daily.groupby("underlying", sort=False):
        g = g.sort_values("sidx")
        st = daily_state(g, variant, side).fillna(False).to_numpy()
        prev = np.concatenate([[False], st[:-1]])
        m = st & ~prev
        if m.any():
            out.append(pd.DataFrame({
                "underlying": u,
                "sidx": g["sidx"].to_numpy()[m],
                "session": g["session"].to_numpy()[m],
            }))
    if not out:
        return pd.DataFrame(columns=["underlying", "sidx", "session"])
    return pd.concat(out, ignore_index=True)


# =========================================================================
# 30-MINUTE (lower timeframe) — stage-1 candidate conditions
# =========================================================================

def stage1_mask(x: pd.DataFrame, variant: str, side: int) -> pd.Series:
    """Boolean stage-1 trigger on the 30m decision bar.

    x carries the m_* intraday block from ../setups_2d3d/features.py
    (already lagged copies m_*_p = previous bar).
    """
    s = float(side)
    if variant == "primary":
        # fresh 30m MACD signal-line cross in `side`, with the 30m moving
        # averages already ordered that way and some directional energy.
        cross = (s * (x["m_macd"] - x["m_macd_sig"]) > 0) & (
            s * (x["m_macd_p"] - x["m_macd_sig_p"]) <= 0)
        return cross & (s * (x["m_ema20"] - x["m_ema50"]) > 0) & (x["m_adx14"] > 20)
    if variant == "ma":
        # fresh 30m EMA20/50 cross with directional energy
        return (
            (s * (x["m_ema20"] - x["m_ema50"]) > 0)
            & (s * (x["m_ema20_p"] - x["m_ema50_p"]) <= 0)
            & (x["m_adx14"] > 20)
        )
    if variant == "rsi":
        # fresh 30m RSI-50 cross with MACD histogram agreeing
        return (
            (s * (x["m_rsi14"] - 50) > 0)
            & (s * (x["m_rsi14_p"] - 50) <= 0)
            & (s * x["m_macd_hist"] > 0)
        )
    raise ValueError(variant)


S1_VARIANTS = ("primary", "ma", "rsi")
S2_VARIANTS = ("primary", "ma", "di")

# --- fixed a priori design constants -------------------------------------
S2_WINDOW_SESSIONS = 3      # higher TF must confirm within 3 sessions of stage-1
LARGE_TGT_ATR = 2.0         # "sustained large move" = +2.0 daily-ATR ...
LARGE_STP_ATR = 1.0         # ... reached before -1.0 daily-ATR against
LARGE_HORIZON_SESSIONS = 10  # ... within 10 sessions of the entry bar
EPISODE_GAP_SESSIONS = 3    # stage-1 fires <=3 sessions apart = ONE episode
