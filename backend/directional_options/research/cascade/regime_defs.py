"""Regime definitions for study (1) — fixed A PRIORI, before any result was seen.

Two independent operationalisations of the owner's "move vs consolidation".
Both are stated here once and are NOT swept; the only variation reported
anywhere is a pre-declared sensitivity grid around each single parameter.

A. CAUSAL CLASSIFIER — Wilder ADX(14), threshold 25.
   Chosen because (i) it is the canonical published trend-strength gauge with a
   threshold Wilder himself fixed at 25 in 1978 — we did not fit it to this
   data; (ii) it is one of the tools the owner named; (iii) it is strictly
   recursive, so the label at session t uses only sessions <= t and can be
   acted on at the next open.  Sessions with ADX >= 25 are "trending", ADX < 25
   is "consolidating".  Direction comes from +DI vs -DI.
   Sensitivity grid (declared, not selected): threshold in {20, 25, 30}.

A2. CAUSAL CROSS-CHECK — Kaufman efficiency ratio over 20 sessions, cut 0.5.
   Declared alongside A (both are pure trailing-window statistics on the same
   bars).  0.5 is a natural constant, not a fitted one: it asks whether price
   retained at least half of the distance it actually travelled.  It is here
   because the ADX verdict turns out to be threshold-sensitive and a second
   unrelated construction is the honest way to show that.
   Sensitivity grid (declared): cut in {0.3, 0.5, 0.7}.

B. DESCRIPTIVE SWING DECOMPOSITION — directional change with a per-instrument
   reversal threshold theta = 3 x median(daily true range / close).
   Chosen because the premise is about MOVE SIZE and DURATION, which needs the
   moves themselves delineated, and because "worth trading with options" has to
   mean more than one average daily range: 3 ATRs is the smallest round
   multiple that clears round-trip option cost on a 2-3 session hold (the honest
   carry/cost floor established earlier is -5..-6% of premium).  This one is
   EX-POST by construction (a swing is only known once it reverses) and is used
   for description ONLY -- never as an entry rule.
   Sensitivity grid (declared, not selected): multiplier in {2, 3, 4}.

Nothing below was re-chosen after seeing an outcome.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

ADX_N = 14
ADX_TREND = 25.0
THETA_MULT = 3.0


# --------------------------------------------------------------------------
# A. causal Wilder ADX
# --------------------------------------------------------------------------
def wilder_adx(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = ADX_N):
    """Return (atr, plus_di, minus_di, adx), all causal: element t uses bars <= t.

    Pure Wilder recursion, no lookahead, no centred windows.
    """
    m = len(h)
    tr = np.full(m, np.nan)
    pdm = np.zeros(m)
    ndm = np.zeros(m)
    for t in range(1, m):
        pc = c[t - 1]
        tr[t] = max(h[t] - l[t], abs(h[t] - pc), abs(l[t] - pc))
        up = h[t] - h[t - 1]
        dn = l[t - 1] - l[t]
        pdm[t] = up if (up > dn and up > 0) else 0.0
        ndm[t] = dn if (dn > up and dn > 0) else 0.0

    atr = np.full(m, np.nan)
    sp = np.full(m, np.nan)
    sn = np.full(m, np.nan)
    if m < n + 1:
        return atr, np.full(m, np.nan), np.full(m, np.nan), np.full(m, np.nan)
    atr[n] = np.nansum(tr[1:n + 1])
    sp[n] = pdm[1:n + 1].sum()
    sn[n] = ndm[1:n + 1].sum()
    for t in range(n + 1, m):
        atr[t] = atr[t - 1] - atr[t - 1] / n + tr[t]
        sp[t] = sp[t - 1] - sp[t - 1] / n + pdm[t]
        sn[t] = sn[t - 1] - sn[t - 1] / n + ndm[t]

    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100.0 * sp / atr
        ndi = 100.0 * sn / atr
        dx = 100.0 * np.abs(pdi - ndi) / (pdi + ndi)
    dx[~np.isfinite(dx)] = np.nan

    adx = np.full(m, np.nan)
    first = 2 * n  # first bar with n DX values available (indices n..2n-1)
    if m > first:
        seed = dx[n:first]
        if np.isfinite(seed).all():
            adx[first - 1] = seed.mean()
            for t in range(first, m):
                adx[t] = (adx[t - 1] * (n - 1) + dx[t]) / n
    return atr / n, pdi, ndi, adx


def add_causal_features(d: pd.DataFrame) -> pd.DataFrame:
    """Attach ATR14/DI/ADX per underlying. Element t depends only on rows <= t."""
    out = []
    for u, g in d.groupby("series_id", sort=False):
        g = g.sort_values("sidx").copy()
        atr, pdi, ndi, adx = wilder_adx(
            g["h"].to_numpy(float), g["l"].to_numpy(float), g["c"].to_numpy(float)
        )
        g["atr14"] = atr
        g["atr_pct"] = atr / g["c"].to_numpy(float)
        g["plus_di"] = pdi
        g["minus_di"] = ndi
        g["adx"] = adx
        g["er20"] = efficiency_ratio(g["c"].to_numpy(float), 20)
        out.append(g)
    return pd.concat(out, ignore_index=True)


def efficiency_ratio(c: np.ndarray, n: int = 20) -> np.ndarray:
    """Kaufman efficiency ratio over a trailing n-session window. Causal.

    ER_t = |c_t - c_{t-n}| / sum_{i=t-n+1..t} |c_i - c_{i-1}|  in [0, 1].
    ER >= 0.5 means price kept at least half of everything it travelled --
    the natural, unfitted cut for "this is a move, not chop".
    """
    m = len(c)
    out = np.full(m, np.nan)
    dif = np.abs(np.diff(c, prepend=c[0]))
    cs = np.cumsum(dif)
    for t in range(n, m):
        denom = cs[t] - cs[t - n]
        if denom > 0:
            out[t] = abs(c[t] - c[t - n]) / denom
    return out


def label_adx_regime(d: pd.DataFrame, thresh: float = ADX_TREND) -> pd.Series:
    """'trending' / 'consolidating' / NaN during warm-up. Causal."""
    lab = pd.Series(np.where(d["adx"] >= thresh, "trending", "consolidating"), index=d.index)
    lab[d["adx"].isna()] = np.nan
    return lab


# --------------------------------------------------------------------------
# B. descriptive directional-change swing decomposition (EX-POST)
# --------------------------------------------------------------------------
@dataclass
class Swing:
    series_id: str
    mclass: str
    start_sidx: int
    end_sidx: int
    start_session: object
    end_session: object
    direction: int          # +1 up, -1 down
    size: float             # signed pct move pivot -> pivot
    duration: int           # sessions from start pivot to end pivot


def swings(g: pd.DataFrame, theta: float) -> list[Swing]:
    """Directional-change decomposition of the close series at reversal theta.

    Alternating pivot-to-pivot swings; a swing closes only when price retraces
    `theta` from its running extreme (hence EX-POST).  The final unconfirmed
    leg is dropped.
    """
    c = g["c"].to_numpy(float)
    sidx = g["sidx"].to_numpy(int)
    sess = g["session"].to_numpy()
    u = g["series_id"].iloc[0]
    mc = g["mclass"].iloc[0]
    if len(c) < 3:
        return []
    out: list[Swing] = []
    mode = 1
    piv = 0
    ext, ext_i = c[0], 0
    for t in range(1, len(c)):
        if mode == 1:
            if c[t] > ext:
                ext, ext_i = c[t], t
            elif c[t] <= ext * (1.0 - theta):
                out.append(Swing(u, mc, sidx[piv], sidx[ext_i], sess[piv], sess[ext_i], 1,
                                 c[ext_i] / c[piv] - 1.0, sidx[ext_i] - sidx[piv]))
                mode, piv = -1, ext_i
                ext, ext_i = c[t], t
        else:
            if c[t] < ext:
                ext, ext_i = c[t], t
            elif c[t] >= ext * (1.0 + theta):
                out.append(Swing(u, mc, sidx[piv], sidx[ext_i], sess[piv], sess[ext_i], -1,
                                 c[ext_i] / c[piv] - 1.0, sidx[ext_i] - sidx[piv]))
                mode, piv = 1, ext_i
                ext, ext_i = c[t], t
    return out


def instrument_theta(g: pd.DataFrame, mult: float = THETA_MULT) -> float:
    """theta = mult x median(daily true range / close). Uses the whole sample
    (descriptive statistic, explicitly ex-post)."""
    tr = (g["h"] - g["l"]) / g["c"]
    return float(mult * tr.median())


def all_swings(d: pd.DataFrame, mult: float = THETA_MULT) -> pd.DataFrame:
    rows: list[Swing] = []
    thetas = {}
    for u, g in d.groupby("series_id", sort=False):
        g = g.sort_values("sidx")
        th = instrument_theta(g, mult)
        thetas[u] = th
        rows.extend(swings(g, th))
    sw = pd.DataFrame([r.__dict__ for r in rows])
    if not sw.empty:
        sw["theta"] = sw["series_id"].map(thetas)
        sw["abs_size"] = sw["size"].abs()
        sw["qualifies"] = sw["abs_size"] >= sw["theta"]
    return sw
