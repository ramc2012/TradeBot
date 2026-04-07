"""Pure Python MACD computation (no external libraries)."""
from __future__ import annotations

from typing import Optional


# ── EMA ───────────────────────────────────────────────────────────────────────

def compute_ema(values: list[float], period: int) -> list[Optional[float]]:
    """
    Compute Exponential Moving Average (EMA) over a list of values.

    The first valid EMA is seeded with a simple average (SMA) of the first
    `period` values. Positions before enough data has accumulated are None.

    Parameters
    ----------
    values : list[float]
        Input price series (e.g., closes)
    period : int
        EMA period (e.g. 12, 26)

    Returns
    -------
    list[float | None]
        EMA values; None for indices < period - 1
    """
    if period <= 0:
        raise ValueError(f"EMA period must be positive, got {period}")

    n = len(values)
    result: list[Optional[float]] = [None] * n

    if n < period:
        return result

    # Seed: SMA of the first `period` values
    sma = sum(values[:period]) / period
    result[period - 1] = sma

    k = 2.0 / (period + 1)  # smoothing factor
    prev_ema = sma

    for i in range(period, n):
        ema = values[i] * k + prev_ema * (1.0 - k)
        result[i] = ema
        prev_ema = ema

    return result


# ── MACD ──────────────────────────────────────────────────────────────────────

def compute_macd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    """
    Compute MACD line, Signal line, and Histogram for a price series.

    MACD line   = EMA(fast) - EMA(slow)
    Signal line = EMA(MACD, signal_period)
    Histogram   = MACD - Signal

    Parameters
    ----------
    closes : list[float]
        Closing price series (chronological, oldest first)
    fast : int
        Fast EMA period (default 12)
    slow : int
        Slow EMA period (default 26)
    signal_period : int
        Signal EMA period (default 9)

    Returns
    -------
    tuple[list, list, list]
        (macd_line, signal_line, histogram) — all same length as closes.
        Values before sufficient data are None.
    """
    n = len(closes)
    macd_line: list[Optional[float]] = [None] * n
    signal_line: list[Optional[float]] = [None] * n
    histogram: list[Optional[float]] = [None] * n

    ema_fast = compute_ema(closes, fast)
    ema_slow = compute_ema(closes, slow)

    # MACD line: valid only where both EMAs are non-None (i.e. from index slow-1)
    macd_values: list[Optional[float]] = []
    for i in range(n):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_values.append(ema_fast[i] - ema_slow[i])  # type: ignore[operator]
        else:
            macd_values.append(None)
    macd_line = macd_values

    # Signal line: EMA of MACD line — extract non-None run and compute EMA
    # We compute an inline EMA over the first contiguous non-None suffix of macd_line
    # (MACD becomes valid at index slow-1 and stays valid thereafter)
    first_valid_macd = -1
    for i, v in enumerate(macd_line):
        if v is not None:
            first_valid_macd = i
            break

    if first_valid_macd == -1:
        # No valid MACD values at all
        return macd_line, signal_line, histogram

    # Extract the valid MACD subsequence
    valid_macd_values: list[float] = [
        macd_line[i] for i in range(first_valid_macd, n)  # type: ignore[misc]
    ]

    ema_signal_partial = compute_ema(valid_macd_values, signal_period)

    # Map back into full-length arrays
    for j, v in enumerate(ema_signal_partial):
        full_idx = first_valid_macd + j
        signal_line[full_idx] = v
        if macd_line[full_idx] is not None and v is not None:
            histogram[full_idx] = macd_line[full_idx] - v  # type: ignore[operator]

    return macd_line, signal_line, histogram


# ── Zero-line crossover detection ─────────────────────────────────────────────

def find_zero_crossovers(macd_line: list[Optional[float]]) -> list[int]:
    """
    Find indices where MACD crosses from negative (or zero) to positive
    (zero-line bullish crossover = buy signal).

    A crossover is detected when:
        macd_line[i-1] <= 0  AND  macd_line[i] > 0
    Both values must be non-None.

    Parameters
    ----------
    macd_line : list[float | None]
        MACD line values

    Returns
    -------
    list[int]
        Indices of zero-line buy crossovers
    """
    crossovers: list[int] = []
    prev_val: Optional[float] = None

    for i, val in enumerate(macd_line):
        if val is None:
            prev_val = None
            continue
        if prev_val is not None and prev_val <= 0.0 and val > 0.0:
            crossovers.append(i)
        prev_val = val

    return crossovers


# ── Trade analysis ────────────────────────────────────────────────────────────

def analyze_trade(candles: list[dict], entry_idx: int) -> dict:
    """
    Analyze a single MACD crossover trade from entry to end of candle series.

    Entry is the close of the crossover bar. The trade is notionally held until
    the last candle in the series (i.e., till expiry).

    Parameters
    ----------
    candles : list[dict]
        Candle list. Each dict must have keys: open, high, low, close, time.
        Candles must be chronological (oldest first).
    entry_idx : int
        Index of the crossover/entry bar in `candles`.

    Returns
    -------
    dict with keys:
        entry_time        : str — ISO timestamp of entry bar
        entry_price       : float — close at crossover bar
        max_price         : float — highest high from entry bar to last bar
        max_return_pct    : float — (max_price - entry_price) / entry_price * 100
        final_price       : float — close of last candle
        held_return_pct   : float — (final_price - entry_price) / entry_price * 100
        target_50pct_hit  : bool  — max_return_pct >= 50
        target_100pct_hit : bool  — max_return_pct >= 100
        bars_to_max       : int   — bars from entry to the bar with max_price
        bars_held         : int   — total bars from entry to end
        entry_idx         : int   — echoed back for reference
    """
    if entry_idx < 0 or entry_idx >= len(candles):
        raise IndexError(
            f"entry_idx {entry_idx} is out of range for candles list of length {len(candles)}"
        )

    entry_candle = candles[entry_idx]
    entry_price: float = float(entry_candle["close"])
    entry_time: str = str(entry_candle.get("time", ""))

    if entry_price <= 0.0:
        # Avoid division by zero for illiquid / zero-price options
        return {
            "entry_time": entry_time,
            "entry_price": entry_price,
            "max_price": 0.0,
            "max_return_pct": 0.0,
            "final_price": 0.0,
            "held_return_pct": 0.0,
            "target_50pct_hit": False,
            "target_100pct_hit": False,
            "bars_to_max": 0,
            "bars_held": 0,
            "entry_idx": entry_idx,
        }

    # Scan from entry bar to end
    max_price = entry_price
    max_bar_offset = 0
    final_price = entry_price

    for offset, candle in enumerate(candles[entry_idx:]):
        high = float(candle.get("high", candle.get("close", 0)))
        close = float(candle.get("close", 0))

        if high > max_price:
            max_price = high
            max_bar_offset = offset

        final_price = close  # keep updating until last candle

    bars_held = len(candles) - 1 - entry_idx  # bars after entry bar

    max_return_pct = (max_price - entry_price) / entry_price * 100.0
    held_return_pct = (final_price - entry_price) / entry_price * 100.0

    return {
        "entry_time": entry_time,
        "entry_price": round(entry_price, 4),
        "max_price": round(max_price, 4),
        "max_return_pct": round(max_return_pct, 4),
        "final_price": round(final_price, 4),
        "held_return_pct": round(held_return_pct, 4),
        "target_50pct_hit": max_return_pct >= 50.0,
        "target_100pct_hit": max_return_pct >= 100.0,
        "bars_to_max": max_bar_offset,
        "bars_held": bars_held,
        "entry_idx": entry_idx,
    }


def _build_exit_result(
    candles: list[dict],
    entry_idx: int,
    exit_idx: int,
    exit_price: float,
    strategy_name: str,
    exit_reason: str,
) -> dict:
    """Return a normalized exit summary for a simulated strategy."""
    entry_price = float(candles[entry_idx]["close"])
    exit_candle = candles[exit_idx]
    exit_time = str(exit_candle.get("time", ""))
    bars_held = max(exit_idx - entry_idx, 0)
    return_pct = 0.0
    if entry_price > 0:
        return_pct = (exit_price - entry_price) / entry_price * 100.0
    return {
        "strategy": strategy_name,
        "exit_reason": exit_reason,
        "exit_time": exit_time,
        "exit_price": round(exit_price, 4),
        "return_pct": round(return_pct, 4),
        "bars_held": bars_held,
    }


def simulate_hold_to_expiry(candles: list[dict], entry_idx: int) -> dict:
    """Exit at the close of the last available candle."""
    exit_idx = len(candles) - 1
    exit_price = float(candles[exit_idx].get("close", 0.0))
    return _build_exit_result(
        candles,
        entry_idx,
        exit_idx,
        exit_price,
        "hold_to_expiry",
        "expiry_close",
    )


def simulate_target_exit(
    candles: list[dict],
    entry_idx: int,
    target_pct: float,
) -> dict:
    """
    Exit at the first candle whose high reaches the configured percentage target.

    If the target is never reached, fall back to expiry close.
    """
    entry_price = float(candles[entry_idx]["close"])
    if entry_price <= 0:
        return _build_exit_result(
            candles,
            entry_idx,
            entry_idx,
            entry_price,
            f"target_{int(target_pct)}pct",
            "invalid_entry_price",
        )

    target_price = entry_price * (1.0 + target_pct / 100.0)
    for exit_idx in range(entry_idx + 1, len(candles)):
        high = float(candles[exit_idx].get("high", candles[exit_idx].get("close", 0.0)))
        if high >= target_price:
            return _build_exit_result(
                candles,
                entry_idx,
                exit_idx,
                target_price,
                f"target_{int(target_pct)}pct",
                f"target_{int(target_pct)}pct_hit",
            )

    return simulate_hold_to_expiry(candles, entry_idx) | {
        "strategy": f"target_{int(target_pct)}pct",
        "exit_reason": "expiry_close_target_not_hit",
    }


def simulate_trailing_exit(
    candles: list[dict],
    entry_idx: int,
    activation_pct: float,
    trail_drawdown_pct: float,
) -> dict:
    """
    Trailing-close exit:
      - activate only after price first reaches `activation_pct`
      - once active, exit on the first candle close that falls
        `trail_drawdown_pct` below the running peak close/high mix
      - otherwise exit at expiry close

    Using the bar close for the trail trigger avoids making unverifiable
    intrabar ordering assumptions from OHLC data.
    """
    entry_price = float(candles[entry_idx]["close"])
    if entry_price <= 0:
        return _build_exit_result(
            candles,
            entry_idx,
            entry_idx,
            entry_price,
            f"trail_after_{int(activation_pct)}pct_{int(trail_drawdown_pct)}pct",
            "invalid_entry_price",
        )

    activation_price = entry_price * (1.0 + activation_pct / 100.0)
    activated = False
    peak_price = entry_price
    strategy_name = (
        f"trail_after_{int(activation_pct)}pct_{int(trail_drawdown_pct)}pct"
    )

    for exit_idx in range(entry_idx + 1, len(candles)):
        candle = candles[exit_idx]
        high = float(candle.get("high", candle.get("close", 0.0)))
        close = float(candle.get("close", 0.0))
        if high > peak_price:
            peak_price = high
        if not activated and high >= activation_price:
            activated = True
        if activated and close <= peak_price * (1.0 - trail_drawdown_pct / 100.0):
            return _build_exit_result(
                candles,
                entry_idx,
                exit_idx,
                close,
                strategy_name,
                "trailing_close_break",
            )

    return simulate_hold_to_expiry(candles, entry_idx) | {
        "strategy": strategy_name,
        "exit_reason": "expiry_close_trail_not_hit",
    }


def simulate_exit_strategies(candles: list[dict], entry_idx: int) -> dict[str, dict]:
    """
    Simulate a compact set of practical exit styles for each entry.

    The comparison is intentionally simple and deterministic so it remains
    defensible on 30-minute OHLC data.
    """
    strategies: dict[str, dict] = {}
    strategies["hold_to_expiry"] = simulate_hold_to_expiry(candles, entry_idx)
    for target_pct in (10, 20, 30, 50, 75, 100):
        key = f"target_{target_pct}pct"
        strategies[key] = simulate_target_exit(candles, entry_idx, float(target_pct))
    for activation_pct, trail_drawdown_pct in ((20, 10), (30, 15), (50, 20)):
        key = f"trail_after_{activation_pct}pct_{trail_drawdown_pct}pct"
        strategies[key] = simulate_trailing_exit(
            candles,
            entry_idx,
            float(activation_pct),
            float(trail_drawdown_pct),
        )
    return strategies
