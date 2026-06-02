"""Normalization helpers — the instrument-independence layer (spec §3).

Every model feature must be expressed in instrument-independent units. These helpers provide
the four sanctioned normalizers: ATR units, profile-width units, rolling z-score, and
percentile. All are leak-free by construction: callers pass only data available strictly
before the decision time.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd


def atr_reference(bars: pd.DataFrame, as_of_date: date, window: int = 14) -> float | None:
    """14-session ATR of the underlying in points, computed on sessions strictly before
    `as_of_date`. This is the master normalizer that makes instruments and epochs comparable.

    Returns None if there is not enough prior history.
    """
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
    atr = float(tr.tail(window).mean())
    return atr if atr > 0 else None


def in_atr(value_points: float | None, atr_ref: float | None) -> float | None:
    """Convert a points quantity to ATR units. None-safe."""
    if value_points is None or atr_ref is None or atr_ref <= 0:
        return None
    return value_points / atr_ref


def in_profile_width(value_points: float | None, profile_width: float | None) -> float | None:
    """Convert a points quantity to profile-width units. None-safe."""
    if value_points is None or profile_width is None or profile_width <= 0:
        return None
    return value_points / profile_width


def rolling_tod_baseline(
    bars: pd.DataFrame,
    decision_time,
    column: str,
    *,
    tod_key,
    lookback_sessions: int = 20,
) -> tuple[float, float] | None:
    """Mean and std of `column` for the same time-of-day bucket over the trailing
    `lookback_sessions`, using only sessions strictly before the decision date.

    `tod_key(ts) -> hashable` buckets timestamps by time of day. Leak-free.
    """
    d = decision_time.date()
    prior = bars[bars.index.date < d]
    if prior.empty:
        return None
    target_key = tod_key(decision_time)
    keys = prior.index.map(tod_key)
    same_tod = prior[keys == target_key]
    if same_tod.empty:
        return None
    # restrict to the most recent lookback_sessions distinct dates
    recent_dates = sorted({ts.date() for ts in same_tod.index})[-lookback_sessions:]
    sub = same_tod[[ts.date() in set(recent_dates) for ts in same_tod.index]]
    vals = sub[column].astype(float).dropna()
    if len(vals) < 3:
        return None
    return float(vals.mean()), float(vals.std(ddof=1) or 1.0)


def zscore(x: float | None, baseline: tuple[float, float] | None) -> float | None:
    """Z-score x against a (mu, sigma) baseline. None-safe."""
    if x is None or baseline is None:
        return None
    mu, sigma = baseline
    if sigma == 0:
        return 0.0
    return (x - mu) / sigma


def percentile_rank(x: float | None, history: pd.Series | None) -> float | None:
    """Percentile (0-100) of x within a historical distribution. None-safe."""
    if x is None or history is None or len(history) < 5:
        return None
    h = history.dropna().astype(float)
    if h.empty:
        return None
    return float((h < x).mean() * 100.0)
