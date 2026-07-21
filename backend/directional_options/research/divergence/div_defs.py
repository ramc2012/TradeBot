"""(D) DIVERGENCE STUDY, part 2 — element definitions, FIXED A PRIORI.

The owner's setup, verbatim: "PNB gave macd crossover with divergence on may 22.
Then made higher low on 8th july ... Multiple filters, trendlines, strength at
MACD crossover to be studied along with options positioning by participants.
Here we use hourly and daily timeframes."

Five computable elements are named here BEFORE any measurement is taken. Every
predicate is CAUSAL: its value at daily session index i depends only on daily
bars 0..i (and, for the hourly element, only on 30m bars that closed at or
before the stated decision bar). The prefix-invariance test
(div_test_causality.py, rtol 1e-12) proves it mechanically.

TIMING CONVENTION (identical to ../cascade/stages.py)
  * a DAILY condition evaluated on the bar of session s uses only sessions
    <= s and is actionable at the OPEN of session s+1 (the daily bar of s is
    not closed until 15:30 IST);
  * a fractal pivot at session p with right-width R is not knowable until the
    close of session p+R, so it is actionable at the OPEN of session p+R+1;
  * an HOURLY condition on the hourly bar ending at 30m bar t is actionable at
    the OPEN of 30m bar t+1.

Nothing here is tuned. Alternates exist only so the grid can be reported in
full with multiplicity applied; the PRIMARY definitions carry the verdict.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setups_2d3d"))
from features import atr, macd  # noqa: E402

# --------------------------------------------------------------------------
# fixed a-priori design constants
# --------------------------------------------------------------------------
PIV_L = 3                  # fractal pivot left width  (daily sessions)
PIV_R = 3                  # fractal pivot right width -> confirmation lag = 3
DIV_LOOKBACK = 40          # the two divergence pivots must lie inside 40 sessions
DIV_MIN_SEP = 5            # ... and be at least 5 sessions apart
DIV_MIN_PRICE = 0.001      # "lower low" means at least 0.1% lower (not a tie)
HL_WINDOW = 25             # higher low must confirm within 25 sessions of the cross
HL_MIN = 0.001             # "higher low" means at least 0.1% higher
TL_WINDOW = 10             # a trendline break counts if it happened <=10 sessions before
TL_MAX_SPAN = 60           # the two pivot highs defining the line span <=60 sessions
HR_WINDOW = 15             # hourly lead is searched over the 15 sessions before the cross

# outcome barriers (same numbers as the cascade study, so results are comparable)
TGT_ATR = 2.0
STP_ATR = 1.0
HORIZON_SESSIONS = 15      # widened from the cascade's 10: the established fact is
                           # that the median qualifying move takes 12 sessions
EPISODE_GAP_SESSIONS = 5   # triggers <=5 sessions apart on one name = ONE episode

SIDE = 1                   # this study is long-only: the owner's setup is a bullish
                           # divergence into a bullish crossover. The bearish mirror
                           # is reported as a symmetry check, not as the verdict.


# ==========================================================================
# causal fractal pivots
# ==========================================================================

def pivot_low_idx(low: np.ndarray, L: int = PIV_L, R: int = PIV_R) -> np.ndarray:
    """Boolean array: session i is a fractal pivot LOW.

    low[i] < low[i-L..i-1] and low[i] <= low[i+1..i+R]. Strict on the left,
    non-strict on the right, so a flat double bottom resolves to its FIRST bar
    (a deterministic, side-independent tie-break).

    NOT causal at i -- it is causal at i+R, which is why every consumer only
    ever reads pivots with i + R <= (current session).
    """
    n = len(low)
    out = np.zeros(n, bool)
    for i in range(L, n - R):
        v = low[i]
        if not np.isfinite(v):
            continue
        if np.all(low[i - L:i] > v) and np.all(low[i + 1:i + R + 1] >= v):
            out[i] = True
    return out


def pivot_high_idx(high: np.ndarray, L: int = PIV_L, R: int = PIV_R) -> np.ndarray:
    n = len(high)
    out = np.zeros(n, bool)
    for i in range(L, n - R):
        v = high[i]
        if not np.isfinite(v):
            continue
        if np.all(high[i - L:i] < v) and np.all(high[i + 1:i + R + 1] <= v):
            out[i] = True
    return out


# ==========================================================================
# per-underlying element panel
# ==========================================================================

def build_elements(d: pd.DataFrame) -> pd.DataFrame:
    """Add every element column for ONE underlying, ascending by session.

    Required input columns: s_open, s_high, s_low, s_close, s_vol,
    D_macd, D_macd_sig, D_macd_hist, D_atr14 (from ../cascade/stages.py).
    """
    d = d.reset_index(drop=True).copy()
    n = len(d)
    lo = d["s_low"].to_numpy(float)
    hi = d["s_high"].to_numpy(float)
    cl = d["s_close"].to_numpy(float)
    vol = d["s_vol"].to_numpy(float)
    md = d["D_macd"].to_numpy(float)
    sg = d["D_macd_sig"].to_numpy(float)
    hist = d["D_macd_hist"].to_numpy(float)
    a14 = d["D_atr14"].to_numpy(float)

    pl = pivot_low_idx(lo)
    ph = pivot_high_idx(hi)
    d["piv_low"] = pl
    d["piv_high"] = ph

    # ---------------- (a1) the bare MACD signal-line crossover -------------
    diff = md - sg
    prev = np.concatenate([[np.nan], diff[:-1]])
    cross = (diff > 0) & (prev <= 0) & np.isfinite(prev)
    d["cross"] = cross

    # ---------------- (a2) DIVERGENCE at the crossover ---------------------
    # At session s, take the two most recent pivot lows CONFIRMED by s
    # (pivot index p with p + PIV_R <= s). Bullish regular divergence:
    #   price makes a LOWER low   (low[p2] < low[p1] * (1 - DIV_MIN_PRICE))
    #   MACD  makes a HIGHER low  (macd[p2] > macd[p1])
    piv_idx = np.where(pl)[0]
    div = np.zeros(n, bool)
    div_price = np.full(n, np.nan)
    div_macd = np.full(n, np.nan)
    div_anchor = np.full(n, -1, np.int64)     # p2, the recent (lower) price low
    div_prior = np.full(n, -1, np.int64)      # p1
    for s in range(n):
        avail = piv_idx[piv_idx + PIV_R <= s]
        if len(avail) < 2:
            continue
        p1, p2 = int(avail[-2]), int(avail[-1])
        if (s - p2) > DIV_LOOKBACK or (p2 - p1) < DIV_MIN_SEP or (p2 - p1) > DIV_LOOKBACK:
            continue
        if not (np.isfinite(md[p1]) and np.isfinite(md[p2])):
            continue
        if lo[p2] < lo[p1] * (1.0 - DIV_MIN_PRICE) and md[p2] > md[p1]:
            div[s] = True
            div_price[s] = lo[p2] / lo[p1] - 1.0
            div_macd[s] = md[p2] - md[p1]
            div_anchor[s] = p2
            div_prior[s] = p1
    d["div"] = div
    d["div_price"] = div_price
    d["div_macd"] = div_macd
    d["div_anchor"] = div_anchor
    d["div_prior"] = div_prior

    # ALTERNATE definition (reported in the grid with multiplicity, never the
    # verdict): the most recent confirmed pivot low versus ANY earlier confirmed
    # pivot low inside the lookback, not only the immediately preceding one.
    # Same causality contract; strictly a superset of `div`.
    div_any = np.zeros(n, bool)
    div_any_macd = np.full(n, np.nan)
    div_any_anchor = np.full(n, -1, np.int64)
    for s in range(n):
        avail = piv_idx[piv_idx + PIV_R <= s]
        if len(avail) < 2:
            continue
        p2 = int(avail[-1])
        if (s - p2) > DIV_LOOKBACK:
            continue
        for p1 in avail[:-1][::-1]:
            p1 = int(p1)
            if (p2 - p1) < DIV_MIN_SEP or (p2 - p1) > DIV_LOOKBACK:
                continue
            if not (np.isfinite(md[p1]) and np.isfinite(md[p2])):
                continue
            if lo[p2] < lo[p1] * (1.0 - DIV_MIN_PRICE) and md[p2] > md[p1]:
                div_any[s] = True
                div_any_macd[s] = md[p2] - md[p1]
                div_any_anchor[s] = p2
                break
    d["div_any"] = div_any
    d["div_any_macd"] = div_any_macd
    d["div_any_anchor"] = div_any_anchor

    # ---------------- (b) CROSSOVER STRENGTH -------------------------------
    # Four measures, all evaluated on the cross session itself, all scale-free.
    with np.errstate(invalid="ignore", divide="ignore"):
        d["str_hist"] = hist / a14                       # ATR-normalised histogram
        slope = hist - np.concatenate([[np.nan], hist[:-1]])
        d["str_slope"] = slope / a14                     # histogram slope at the cross
        d["str_below0"] = -md / cl                       # how far below zero MACD crossed
        thr = cl - np.concatenate([[np.nan], cl[:-1]])
        d["str_thrust"] = thr / a14                      # ATR-normalised price thrust
        v20 = pd.Series(vol).rolling(20, min_periods=10).mean().to_numpy()
        vsd = pd.Series(vol).rolling(20, min_periods=10).std().to_numpy()
        d["str_volz"] = (vol - v20) / vsd                # volume z at the cross
    d["str_div_macd"] = div_macd / a14                   # divergence magnitude, ATR units

    # ---------------- (c) STRUCTURE: the higher low ------------------------
    # A confirmed pivot low p is a HIGHER LOW if low[p] exceeds the low of the
    # previous confirmed pivot low by >= HL_MIN. Actionable at open of p+R+1.
    hl_conf = np.zeros(n, bool)          # session at which a higher low is CONFIRMED
    hl_pivot = np.full(n, -1, np.int64)
    hl_lift = np.full(n, np.nan)
    for k in range(1, len(piv_idx)):
        p_prev, p = int(piv_idx[k - 1]), int(piv_idx[k])
        c = p + PIV_R
        if c >= n:
            continue
        if lo[p] > lo[p_prev] * (1.0 + HL_MIN):
            hl_conf[c] = True
            hl_pivot[c] = p
            hl_lift[c] = lo[p] / lo[p_prev] - 1.0
    d["hl_conf"] = hl_conf
    d["hl_pivot"] = hl_pivot
    d["hl_lift"] = hl_lift

    # ---------------- (d) TRENDLINE break ----------------------------------
    # Descending line through the two most recent CONFIRMED pivot highs
    # (h1 at i1, h2 at i2, i1<i2, high[i2] < high[i1]). Break at session t when
    # close[t] > line(t) and close[t-1] <= line(t-1). Fully objective.
    ph_idx = np.where(ph)[0]
    tl_break = np.zeros(n, bool)
    tl_slope = np.full(n, np.nan)
    line_prev = np.nan
    for t in range(1, n):
        avail = ph_idx[ph_idx + PIV_R <= t]
        if len(avail) < 2:
            line_prev = np.nan
            continue
        i1, i2 = int(avail[-2]), int(avail[-1])
        if (i2 - i1) > TL_MAX_SPAN or (t - i2) > TL_MAX_SPAN:
            line_prev = np.nan
            continue
        if not (hi[i2] < hi[i1]):
            line_prev = np.nan
            continue
        m = (hi[i2] - hi[i1]) / (i2 - i1)
        lt = hi[i1] + m * (t - i1)
        ltm1 = hi[i1] + m * (t - 1 - i1)
        if np.isfinite(cl[t]) and np.isfinite(cl[t - 1]) and cl[t] > lt and cl[t - 1] <= ltm1:
            tl_break[t] = True
            tl_slope[t] = m / cl[t]
        line_prev = lt
    d["tl_break"] = tl_break
    d["tl_slope"] = tl_slope
    # "a descending trendline was broken at s or in the previous TL_WINDOW sessions"
    d["tl_recent"] = (pd.Series(tl_break).rolling(TL_WINDOW + 1, min_periods=1)
                      .max().fillna(0).astype(bool).to_numpy())

    return d


# ==========================================================================
# hourly (element e)
# ==========================================================================

def hourly_from_30m(g: pd.DataFrame) -> pd.DataFrame:
    """Hourly bars for ONE underlying from its 30m tape.

    NSE 30m bars are aligned to 09:15 IST; hourly bars pair consecutive 30m
    bars WITHIN a session starting at 09:15 (bidx 0+1, 2+3, ...). The 13th bar
    of a session is an orphan half-hour and is kept as its own (short) bar so
    no information is discarded and no bar ever straddles a session boundary.
    The bar is stamped with the time of its LAST 30m bar, which is when it
    closes and therefore when it is knowable.
    """
    g = g.sort_values("time").copy()
    g["hgrp"] = g["session"].astype(str) + "#" + (g["bidx"] // 2).astype(str)
    h = (g.groupby("hgrp", sort=False)
           .agg(time=("time", "last"), session=("session", "last"),
                bidx=("bidx", "last"), open=("open", "first"), high=("high", "max"),
                low=("low", "min"), close=("close", "last"))
           .reset_index(drop=True)
           .sort_values("time").reset_index(drop=True))
    ml, ms, mh = macd(h["close"])
    h["H_macd"], h["H_macd_sig"], h["H_macd_hist"] = ml, ms, mh
    h["H_atr14"] = atr(h["high"], h["low"], h["close"], 14)
    dfl = (h["H_macd"] - h["H_macd_sig"])
    h["H_cross"] = (dfl > 0) & (dfl.shift(1) <= 0)
    return h


# ==========================================================================
# setup assembly -- the arms under test, named a priori
# ==========================================================================
# Every arm produces (underlying, trigger session index, entry session index).
# ENTRY IS ALWAYS THE OPEN OF THE SESSION AFTER THE LAST KNOWABLE EVENT.
ARMS = {
    # --- the baseline the cascade study already killed, re-run here so the
    #     divergence family is measured against it and not against nothing
    "cross":         ("cross",),
    # --- the owner's element (a)
    "cross_div":     ("cross", "div"),
    # --- (a)+(d)
    "cross_div_tl":  ("cross", "div", "tl_recent"),
    # --- (a)+(c): entry deferred to the confirmed higher low
    "cross_div_hl":  ("cross", "div", "HL"),
    # --- the FULL setup: (a)+(c)+(d)
    "full":          ("cross", "div", "tl_recent", "HL"),
    # --- ablations of the full setup (drop exactly one element)
    "abl_no_div":    ("cross", "tl_recent", "HL"),
    "abl_no_tl":     ("cross", "div", "HL"),
    "abl_no_hl":     ("cross", "div", "tl_recent"),
    "abl_no_cross":  ("div", "tl_recent", "HL"),
    # --- alternates (grid, multiplicity applied; never the verdict)
    "cross_divany":     ("cross", "div_any"),
    "cross_divany_hl":  ("cross", "div_any", "HL"),
    "full_any":         ("cross", "div_any", "tl_recent", "HL"),
}
PRIMARY_ARM = "cross_div"
FULL_ARM = "full"
