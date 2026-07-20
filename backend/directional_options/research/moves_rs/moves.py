"""(B) Move definition + segmentation engine.

THE DEFINITION (fixed a priori, before any result was looked at)
---------------------------------------------------------------
The owner's model is: lower TF confirms -> higher TF confirms -> sustained
large move -> matures -> consolidation. So a "move" must be (i) directional,
(ii) sustained across sessions, (iii) large relative to the stock's OWN
normal daily range (otherwise the statistic is just "which stocks are
volatile"), and (iv) it must have a stated END.

We segment each stock's daily CLOSE series with an ATR-thresholded zigzag:

  NOISE FILTER  (fixed, NOT swept): a leg reverses when close retraces
      NOISE_K = 1.0 x ATR14 from the leg's running extreme, with the ATR
      frozen at the extreme's own bar. 1 ATR is the smallest retracement that
      is not, definitionally, a single average day's noise. This parameter is
      declared fixed; it is not tuned.

  SIGNIFICANCE BAR (reported at 3 levels, K in {2, 3, 4}): a confirmed leg is
      a MOVE at level K iff its close-to-close excursion is >= K x ATR14 as
      measured at the leg's START bar (causal: the ATR is known before the
      move begins). Legs that fail the bar are CONSOLIDATION.

  END CONDITION: the leg ends at its running favourable extreme; that extreme
      is CONFIRMED (i.e. knowable in real time) only on the later bar where
      price has retraced 1 ATR from it. Both dates are recorded, and every
      tradeability statement uses the confirm date, never the extreme date.

Why ATR-normalised and not a fixed %: a fixed % bar counts moves almost
entirely in proportion to a stock's volatility, so "which stocks move most"
degenerates into "which stocks are most volatile" -- a question we already
know the answer to and which is not a selection edge. ATR-normalisation asks
the harder and more useful question: which stocks TREND relative to their own
noise. A fixed-% robustness pass is run alongside (see analyse.py).

CAUSALITY: ATR14 at bar t uses bars <= t only; leg confirmation at bar t uses
closes <= t only. Verified by prefix-invariance in test_causality.py.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

NOISE_K = 1.0          # reversal threshold in ATRs -- FIXED, not swept
ATR_N = 14
SIG_LEVELS = (2.0, 3.0, 4.0)


def wilder_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = ATR_N) -> np.ndarray:
    """Causal Wilder ATR. atr[t] uses bars 0..t only; NaN until seeded."""
    m = len(close)
    tr = np.empty(m)
    tr[0] = high[0] - low[0]
    for i in range(1, m):
        pc = close[i - 1]
        tr[i] = max(high[i] - low[i], abs(high[i] - pc), abs(low[i] - pc))
    atr = np.full(m, np.nan)
    if m < n:
        return atr
    atr[n - 1] = tr[:n].mean()
    for i in range(n, m):
        atr[i] = (atr[i - 1] * (n - 1) + tr[i]) / n
    return atr


@dataclass
class Leg:
    underlying: str
    direction: int          # +1 up, -1 down
    start_i: int
    end_i: int              # bar of the favourable extreme
    confirm_i: int          # bar on which the extreme became knowable
    start_date: pd.Timestamp
    end_date: pd.Timestamp
    confirm_date: pd.Timestamp
    start_price: float
    end_price: float
    atr0: float             # ATR at start bar -- the normaliser
    atr_mult: float         # |end-start| / atr0
    ret: float              # signed, in the leg's own direction
    duration: int           # sessions from start bar to extreme bar
    lag: int                # confirm_i - end_i (sessions of un-capturable tail)


def segment(df: pd.DataFrame, name: str, noise_k: float = NOISE_K) -> list[Leg]:
    """ATR-zigzag segmentation of one stock's daily bars.

    df: columns session, open, high, low, close -- sorted ascending, unique.
    Returns only CONFIRMED legs; the final in-formation leg is dropped.
    """
    h = df["high"].to_numpy(float)
    lo = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    dates = df["session"].to_numpy()
    atr = wilder_atr(h, lo, c)

    ok = np.where(np.isfinite(atr) & (atr > 0))[0]
    if len(ok) < 5:
        return []
    i0 = int(ok[0])

    legs: list[Leg] = []
    pivot_i = i0
    state = 0               # 0 undetermined, +1 up-leg, -1 down-leg
    hi_i = lo_i = i0

    def emit(a: int, b: int, conf: int, d: int) -> None:
        a0 = atr[a]
        if not np.isfinite(a0) or a0 <= 0:
            return
        legs.append(Leg(
            underlying=name, direction=d, start_i=a, end_i=b, confirm_i=conf,
            start_date=pd.Timestamp(dates[a]), end_date=pd.Timestamp(dates[b]),
            confirm_date=pd.Timestamp(dates[conf]),
            start_price=float(c[a]), end_price=float(c[b]), atr0=float(a0),
            atr_mult=float(abs(c[b] - c[a]) / a0),
            ret=float(d * (c[b] / c[a] - 1.0)),
            duration=int(b - a), lag=int(conf - b),
        ))

    for t in range(i0 + 1, len(c)):
        if c[t] > c[hi_i]:
            hi_i = t
        if c[t] < c[lo_i]:
            lo_i = t

        if state == 0:
            up_thr = c[lo_i] + noise_k * atr[lo_i]
            dn_thr = c[hi_i] - noise_k * atr[hi_i]
            up = c[t] >= up_thr
            dn = c[t] <= dn_thr
            if up and dn:               # deterministic tie-break: older extreme wins
                up = lo_i <= hi_i
                dn = not up
            if up:
                state, pivot_i = 1, lo_i
                hi_i = t
            elif dn:
                state, pivot_i = -1, hi_i
                lo_i = t
        elif state == 1:
            if c[t] <= c[hi_i] - noise_k * atr[hi_i]:
                emit(pivot_i, hi_i, t, +1)
                pivot_i, state = hi_i, -1
                lo_i = t
        else:
            if c[t] >= c[lo_i] + noise_k * atr[lo_i]:
                emit(pivot_i, lo_i, t, -1)
                pivot_i, state = lo_i, 1
                hi_i = t

    return legs


def segment_all(daily: pd.DataFrame, noise_k: float = NOISE_K) -> pd.DataFrame:
    out = []
    for name, g in daily.groupby("underlying", sort=True):
        g = g.sort_values("session").reset_index(drop=True)
        out.extend(asdict(l) for l in segment(g, name, noise_k))
    legs = pd.DataFrame(out)
    if len(legs):
        for k in SIG_LEVELS:
            legs[f"sig_{k:g}"] = legs["atr_mult"] >= k
    return legs
