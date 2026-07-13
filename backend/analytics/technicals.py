"""Reusable technical-indicator helpers for option watchlists."""
from __future__ import annotations

import math
from typing import Any, Optional

from analysis.macd_engine import compute_ema, compute_macd


def compute_rsi(closes: list[float], period: int = 14) -> list[Optional[float]]:
    if len(closes) < period + 1:
        return [None] * len(closes)

    gains = [0.0] * len(closes)
    losses = [0.0] * len(closes)
    for index in range(1, len(closes)):
        change = closes[index] - closes[index - 1]
        gains[index] = max(change, 0.0)
        losses[index] = abs(min(change, 0.0))

    avg_gain = sum(gains[1: period + 1]) / period
    avg_loss = sum(losses[1: period + 1]) / period
    values: list[Optional[float]] = [None] * len(closes)

    rs = avg_gain / avg_loss if avg_loss else float("inf")
    values[period] = 100.0 - (100.0 / (1.0 + rs))

    for index in range(period + 1, len(closes)):
        avg_gain = ((avg_gain * (period - 1)) + gains[index]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[index]) / period
        rs = avg_gain / avg_loss if avg_loss else float("inf")
        values[index] = 100.0 - (100.0 / (1.0 + rs))

    return values


def compute_roc(closes: list[float], period: int = 9) -> list[Optional[float]]:
    values: list[Optional[float]] = [None] * len(closes)
    if period <= 0:
        return values
    for index in range(period, len(closes)):
        previous = closes[index - period]
        if previous == 0:
            continue
        values[index] = ((closes[index] / previous) - 1.0) * 100.0
    return values


def compute_ema_cross(
    closes: list[float],
    fast_period: int = 9,
    slow_period: int = 21,
) -> tuple[list[Optional[float]], list[Optional[float]]]:
    return compute_ema(closes, fast_period), compute_ema(closes, slow_period)


def compute_cci(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 20,
) -> list[Optional[float]]:
    length = min(len(highs), len(lows), len(closes))
    values: list[Optional[float]] = [None] * length
    if period <= 0 or length < period:
        return values

    typical_prices = [
        (float(highs[index]) + float(lows[index]) + float(closes[index])) / 3.0
        for index in range(length)
    ]
    for index in range(period - 1, length):
        window = typical_prices[index - period + 1 : index + 1]
        sma = sum(window) / period
        mean_deviation = sum(abs(value - sma) for value in window) / period
        if mean_deviation == 0:
            values[index] = 0.0
            continue
        values[index] = (typical_prices[index] - sma) / (0.015 * mean_deviation)
    return values


def compute_adx(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    length = min(len(highs), len(lows), len(closes))
    adx: list[Optional[float]] = [None] * length
    plus_di: list[Optional[float]] = [None] * length
    minus_di: list[Optional[float]] = [None] * length
    if period <= 0 or length <= period:
        return adx, plus_di, minus_di

    true_range = [0.0] * length
    plus_dm = [0.0] * length
    minus_dm = [0.0] * length

    for index in range(1, length):
        high = float(highs[index])
        low = float(lows[index])
        prev_high = float(highs[index - 1])
        prev_low = float(lows[index - 1])
        prev_close = float(closes[index - 1])

        up_move = high - prev_high
        down_move = prev_low - low
        plus_dm[index] = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm[index] = down_move if down_move > up_move and down_move > 0 else 0.0
        true_range[index] = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )

    tr_sum = sum(true_range[1 : period + 1])
    plus_dm_sum = sum(plus_dm[1 : period + 1])
    minus_dm_sum = sum(minus_dm[1 : period + 1])
    dx_values: list[Optional[float]] = [None] * length

    for index in range(period, length):
        if index > period:
            tr_sum = tr_sum - (tr_sum / period) + true_range[index]
            plus_dm_sum = plus_dm_sum - (plus_dm_sum / period) + plus_dm[index]
            minus_dm_sum = minus_dm_sum - (minus_dm_sum / period) + minus_dm[index]

        if tr_sum <= 0:
            continue

        plus_value = (plus_dm_sum / tr_sum) * 100.0
        minus_value = (minus_dm_sum / tr_sum) * 100.0
        plus_di[index] = plus_value
        minus_di[index] = minus_value
        denominator = plus_value + minus_value
        if denominator == 0:
            dx_values[index] = 0.0
        else:
            dx_values[index] = (abs(plus_value - minus_value) / denominator) * 100.0

    first_adx_index = (period * 2) - 1
    if first_adx_index >= length:
        return adx, plus_di, minus_di

    seed = [value for value in dx_values[period : first_adx_index + 1] if value is not None]
    if len(seed) < period:
        return adx, plus_di, minus_di

    adx[first_adx_index] = sum(seed) / period
    for index in range(first_adx_index + 1, length):
        if dx_values[index] is None or adx[index - 1] is None:
            continue
        adx[index] = ((adx[index - 1] * (period - 1)) + dx_values[index]) / period

    return adx, plus_di, minus_di


MACD_MIN_BARS = 26 + 9


def latest_macd_rsi(closes: list[float]) -> dict[str, Any]:
    # MACD(12,26,9) needs the slow EMA plus signal-line warm-up.  Returning a
    # value after only 20 closes allowed fresh-contract startup artefacts to be
    # consumed as real S1 zero-crosses.
    if len(closes) < MACD_MIN_BARS:
        return {
            "macd": None,
            "macd_signal": None,
            "macd_histogram": None,
            "rsi": None,
        }

    macd_line, signal_line, histogram = compute_macd(closes)
    rsi_values = compute_rsi(closes)

    def round_or_none(value: Optional[float], digits: int = 4) -> Optional[float]:
        if value is None:
            return None
        numeric = float(value)
        if not math.isfinite(numeric):
            return None
        return round(numeric, digits)

    return {
        "macd": round_or_none(macd_line[-1]),
        "macd_signal": round_or_none(signal_line[-1]),
        "macd_histogram": round_or_none(histogram[-1]),
        "rsi": round_or_none(rsi_values[-1], 2),
    }
