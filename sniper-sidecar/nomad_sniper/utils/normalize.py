"""Normalization helpers — the instrument-independence standard (contract §2).

Every model feature must be expressed in instrument-independent units: ATR-normalized
distance, rolling z-score, ratio, percent, or categorical. The functions here are the
canonical primitives for that conversion.

**Leak-free by construction.** Every function that consumes a time series accepts an
`as_of` / `decision_time` and uses only data *strictly before* it. `tests/test_normalize.py`
asserts this. Never pass a window that includes the decision bar itself.
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd

from nomad_sniper.utils.timeutil import ensure_ist


def atr_reference(
    bars: pd.DataFrame,
    as_of_date: date,
    *,
    window: int = 14,
) -> float | None:
    """Prior-close 14-session ATR in points, computed from sessions strictly before
    `as_of_date` (leak-free: today's bars never enter ATR_ref).

    ATR here is the simple mean of the daily True Range over the trailing `window`
    completed sessions. Returns None if fewer than 2 prior sessions exist.

    `bars` must be an IST-indexed minute-bar frame with high/low/close columns.
    """
    if bars.empty:
        return None
    prior = bars[bars.index.date < as_of_date]
    if prior.empty:
        return None

    daily = (
        prior.groupby(prior.index.date)
        .agg(high=("high", "max"), low=("low", "min"), close=("close", "last"))
    )
    if len(daily) < 2:
        return None

    daily["prev_close"] = daily["close"].shift(1)
    tr = pd.concat(
        [
            daily["high"] - daily["low"],
            (daily["high"] - daily["prev_close"]).abs(),
            (daily["low"] - daily["prev_close"]).abs(),
        ],
        axis=1,
    ).max(axis=1)
    tr = tr.dropna()
    if tr.empty:
        return None
    atr = float(tr.tail(window).mean())
    return atr if atr > 0 else None


def atr_normalize(value_points: float | None, atr_ref: float | None) -> float | None:
    """Convert a points quantity into ATR units. None-safe."""
    if value_points is None or atr_ref is None or atr_ref <= 0:
        return None
    return float(value_points) / float(atr_ref)


def rolling_tod_baseline(
    series: pd.Series,
    decision_time: datetime,
    *,
    lookback: int = 20,
) -> tuple[float | None, float | None]:
    """Same-time-of-day rolling baseline (mean, std), leak-free.

    For a value observed at `decision_time`, the baseline is built from the values at the
    *same clock minute* on the trailing `lookback` sessions strictly before today. This
    captures the strong intraday seasonality of volume / flow (e.g. 09:20 is always heavy).

    `series` must be an IST-indexed numeric Series. Returns (None, None) if there is not
    enough same-TOD history (< 3 points).
    """
    if series.empty:
        return None, None
    decision_time = ensure_ist(decision_time)
    today = decision_time.date()
    target_hm = (decision_time.hour, decision_time.minute)

    idx = series.index
    # Strictly-before-today, same hour:minute.
    same_tod = series[
        (np.array([d.date() for d in idx]) < today)
        & (np.array([(t.hour, t.minute) == target_hm for t in idx]))
    ]
    same_tod = same_tod.dropna()
    if len(same_tod) < 3:
        return None, None
    tail = same_tod.tail(lookback)
    mu = float(tail.mean())
    sigma = float(tail.std(ddof=1))
    return mu, sigma


def zscore(x: float | None, mu: float | None, sigma: float | None) -> float | None:
    """Standard z-score. None-safe; returns 0.0 when sigma is 0 (degenerate, no spread)."""
    if x is None or mu is None or sigma is None:
        return None
    if sigma == 0:
        return 0.0
    return (float(x) - float(mu)) / float(sigma)


def pct_change(value: float | None, base: float | None) -> float | None:
    """Percent of a base quantity (e.g. OI change as % of prior OI). None-safe."""
    if value is None or base is None or base == 0:
        return None
    return 100.0 * float(value) / float(base)


def safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    """Unitless ratio. None-safe; None when denominator is 0 or missing."""
    if numerator is None or denominator is None or denominator == 0:
        return None
    return float(numerator) / float(denominator)
