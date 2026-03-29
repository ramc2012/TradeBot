"""Reusable technical-indicator helpers for option watchlists."""
from __future__ import annotations

import math
from typing import Any, Optional

from analysis.macd_engine import compute_macd


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


def latest_macd_rsi(closes: list[float]) -> dict[str, Any]:
    if len(closes) < 20:
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
