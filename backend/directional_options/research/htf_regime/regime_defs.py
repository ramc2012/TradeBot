"""(1) DAILY REGIME AS A STANDING STATE — definitions fixed A PRIORI.

The owner's model: the daily timeframe supplies a confirmed-trend STATE (not
a fresh transition event); the LTF only times entries inside it. These
definitions are frozen before any option-level outcome is measured. The only
variation anywhere is the pre-declared sensitivity grid next to each
parameter; nothing here is re-chosen after seeing a result.

CAUSALITY CONTRACT: the state that governs session t is the state computed
from daily bars up to and including session t-1's close (`state_lag1`).
Every consumer must read `state_lag1`, never `state`.

R1 (PRIMARY — the owner's stated construct, MA stack):
    UP:   close > SMA20 > SMA50  AND  SMA20 rising (SMA20[t] > SMA20[t-3])
    DOWN: close < SMA20 < SMA50  AND  SMA20 falling
    Chosen as primary because it operationalises the owner's own words
    ("confirmed uptrend in daily timeframe") with the most canonical
    published construct, and it was fixed here before measurement.
    Sensitivity (declared, not selected): rising-lookback in {1, 3, 5}.

R2 (INDEPENDENT CROSS-CHECK — Wilder trend gauge):
    UP:   ADX(14) > 20 AND +DI > -DI      DOWN: ADX(14) > 20 AND -DI > +DI
    Reuses the exact wilder_adx recursion already causality-verified in
    cascade/regime_defs.py. Threshold 20 fixed by the study brief.
    Sensitivity (declared): threshold in {18, 20, 25}.

AGE: sessions since the current state began (age=1 on the first governed
session). Reported by buckets {1-5, 6-12, 13-20, >20} — DESCRIPTIVE ONLY,
because the measured median regime run is 12-18 sessions and late entries
are hypothesised to die. Age is not a tunable entry parameter in this pass.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

SMA_FAST, SMA_SLOW = 20, 50
RISE_LB = 3                      # primary; sensitivity {1, 3, 5}
ADX_N, ADX_THR = 14, 20.0        # sensitivity thr {18, 20, 25}


def wilder_adx(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = ADX_N):
    """Causal Wilder ADX — identical recursion to cascade/regime_defs.py."""
    m = len(h)
    tr = np.full(m, np.nan)
    pdm = np.zeros(m)
    ndm = np.zeros(m)
    for t in range(1, m):
        tr[t] = max(h[t] - l[t], abs(h[t] - c[t - 1]), abs(l[t] - c[t - 1]))
        up, dn = h[t] - h[t - 1], l[t - 1] - l[t]
        if up > dn and up > 0:
            pdm[t] = up
        if dn > up and dn > 0:
            ndm[t] = dn
    atr = np.full(m, np.nan)
    spdm = np.full(m, np.nan)
    sndm = np.full(m, np.nan)
    if m > n:
        atr[n] = np.nanmean(tr[1:n + 1])
        spdm[n] = np.nansum(pdm[1:n + 1])
        sndm[n] = np.nansum(ndm[1:n + 1])
        for t in range(n + 1, m):
            atr[t] = (atr[t - 1] * (n - 1) + tr[t]) / n
            spdm[t] = spdm[t - 1] - spdm[t - 1] / n + pdm[t]
            sndm[t] = sndm[t - 1] - sndm[t - 1] / n + ndm[t]
    with np.errstate(invalid="ignore", divide="ignore"):
        pdi = 100.0 * spdm / (atr * n)
        ndi = 100.0 * sndm / (atr * n)
        dx = 100.0 * np.abs(pdi - ndi) / (pdi + ndi)
    adx = np.full(m, np.nan)
    if m > 2 * n:
        adx[2 * n] = np.nanmean(dx[n + 1:2 * n + 1])
        for t in range(2 * n + 1, m):
            adx[t] = (adx[t - 1] * (n - 1) + dx[t]) / n
    return atr, pdi, ndi, adx


def _age(state: pd.Series) -> pd.Series:
    """Sessions since the current (non-zero carried) state value began."""
    grp = (state != state.shift(1)).cumsum()
    return state.groupby(grp).cumcount() + 1


def daily_regimes(daily: pd.DataFrame, rise_lb: int = RISE_LB,
                  adx_thr: float = ADX_THR) -> pd.DataFrame:
    """daily: one underlying, sessions ascending, columns
    session high low close. Returns per session:
       r1_state, r2_state  in {+1 up, -1 down, 0 none}   (as of THIS close)
       r1_lag1, r2_lag1    the state governing THIS session (prior close)
       r1_age_lag1, r2_age_lag1  age of the governing state, in sessions
    """
    d = daily.sort_values("session").reset_index(drop=True).copy()
    c = d["close"].astype(float)
    s20 = c.rolling(SMA_FAST, min_periods=SMA_FAST).mean()
    s50 = c.rolling(SMA_SLOW, min_periods=SMA_SLOW).mean()
    rising = s20 > s20.shift(rise_lb)
    falling = s20 < s20.shift(rise_lb)
    up1 = (c > s20) & (s20 > s50) & rising
    dn1 = (c < s20) & (s20 < s50) & falling
    d["r1_state"] = np.where(up1, 1, np.where(dn1, -1, 0))

    _, pdi, ndi, adx = wilder_adx(d["high"].to_numpy(float),
                                  d["low"].to_numpy(float), c.to_numpy(float))
    with np.errstate(invalid="ignore"):
        up2 = (adx > adx_thr) & (pdi > ndi)
        dn2 = (adx > adx_thr) & (ndi > pdi)
    d["r2_state"] = np.where(up2, 1, np.where(dn2, -1, 0))
    d["adx"] = adx

    for r in ("r1", "r2"):
        d[f"{r}_age"] = _age(d[f"{r}_state"])
        d[f"{r}_lag1"] = d[f"{r}_state"].shift(1).fillna(0).astype(int)
        d[f"{r}_age_lag1"] = d[f"{r}_age"].shift(1)
    return d


def resample_daily(spot30m: pd.DataFrame) -> pd.DataFrame:
    """30m bars (one underlying) -> daily session bars. Sessions are IST
    dates; a session's close is its last 30m bar. Purely backward-looking.
    INPUT CONTRACT: deduped spot (one row per time) — see load_spot_csvs."""
    s = spot30m.sort_values("time", kind="mergesort").drop_duplicates(
        "time", keep="first").copy()
    ist = s["time"].dt.tz_convert("Asia/Kolkata")
    s["session"] = ist.dt.date
    g = s.groupby("session")
    d = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(),
        "low": g["low"].min(), "close": g["close"].last(),
        "volume": g["volume"].sum(), "n_bars": g["close"].size(),
    }).reset_index()
    d["session"] = pd.to_datetime(d["session"])
    return d
