"""Pure indicator math for MACD Refined.

Kept dependency-light (numpy/pandas only) so the same functions are used by
the backtest, the live evaluator, and unit tests.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# 30-min bars per year: NSE session 09:15–15:30 = 375 min = 12.5 bars/day × 252.
BARS_PER_YEAR_30MIN: float = 3150.0


def compute_macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD on a price/premium series. Returns (macd, signal, histogram)."""
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    histogram = macd - signal_line
    return macd, signal_line, histogram


def zero_cross_up(macd: pd.Series) -> pd.Series:
    """Boolean series: True on bars where MACD crosses from <0 to >0."""
    prev = macd.shift(1)
    return (prev < 0) & (macd > 0)


def realized_vol_annualized(close: pd.Series, window_bars: int, bars_per_year: float) -> float:
    """Annualised realised volatility from log returns of a close series.

    `bars_per_year` lets callers pass the right scaling for 30-min bars
    (≈ 12.5 bars/day × 252 ≈ 3150) vs daily bars (252)."""
    if close is None or len(close) < 3:
        return 0.0
    rets = np.log(close.astype(float)).diff().dropna()
    if window_bars and len(rets) > window_bars:
        rets = rets.iloc[-window_bars:]
    if len(rets) < 2:
        return 0.0
    std = float(rets.std(ddof=1))
    return std * float(np.sqrt(max(bars_per_year, 1.0)))


def iv_rank(current_iv: float, history: pd.Series | np.ndarray | list) -> float | None:
    """IV-rank = position of `current_iv` within the [min, max] of a trailing
    IV history (spec §5). Returns a value in [0, 1], or None if undefined.

    Uses the min/max-range definition (IV-rank), not the percentile-rank
    definition (IV-percentile) — the spec calls for IV-rank < 0.30.
    """
    if current_iv is None or current_iv <= 0:
        return None
    arr = np.asarray(list(history), dtype=float)
    arr = arr[np.isfinite(arr) & (arr > 0)]
    if arr.size < 5:
        return None
    lo = float(arr.min())
    hi = float(arr.max())
    if hi - lo < 1e-9:
        return None
    return float(np.clip((float(current_iv) - lo) / (hi - lo), 0.0, 1.0))


def turnover_rupees(volume: float, premium: float, multiplier: float = 1.0) -> float:
    """Per-bar option turnover in ₹ (spec §2: turnover = volume × premium).

    `multiplier` is 1.0 when `volume` is already in shares (the verified case),
    or the contract lot size when `volume` is reported in lots/contracts.
    """
    try:
        return max(float(volume), 0.0) * max(float(premium), 0.0) * max(float(multiplier), 1.0)
    except (TypeError, ValueError):
        return 0.0


def daily_turnover_series(frame: pd.DataFrame, *, multiplier: float = 1.0) -> pd.Series:
    """Collapse an intraday (time, close, volume) frame to a per-session
    turnover series in ₹, indexed by session date.

    Turnover per bar = volume × close; summed within each calendar day.
    """
    if frame is None or frame.empty:
        return pd.Series(dtype="float64")
    work = frame.loc[:, ["time", "close", "volume"]].copy()
    work["time"] = pd.to_datetime(work["time"], errors="coerce")
    work = work.dropna(subset=["time"])
    work["turnover"] = (
        pd.to_numeric(work["close"], errors="coerce").fillna(0.0)
        * pd.to_numeric(work["volume"], errors="coerce").fillna(0.0)
        * max(float(multiplier), 1.0)
    )
    return work.groupby(work["time"].dt.date)["turnover"].sum()


def trailing_baseline_turnover(
    daily: pd.Series,
    *,
    as_of_date,
    sessions: int,
) -> float:
    """Median daily turnover over the `sessions` sessions strictly BEFORE
    `as_of_date` (no lookahead). 0.0 if insufficient history."""
    if daily is None or daily.empty:
        return 0.0
    prior = daily[[d < as_of_date for d in daily.index]]
    if prior.empty:
        return 0.0
    window = prior.iloc[-int(max(sessions, 1)):]
    if window.empty:
        return 0.0
    return float(window.median())
