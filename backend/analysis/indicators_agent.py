"""Single-owner Indicators agent.

Today the codebase computes MACD/EMA in at least five places under
`analysis/` (market_profile_analysis.py, timeframe_sweep.py,
expansion_strategy.py, staggered_exit_sweep.py, option_indicator_sweep.py),
plus inline in the strategy agents themselves. The canonical implementation
lives at `analysis.macd_engine`. This module is the single API the rest of
the codebase should call. It memoizes per-(symbol, timeframe, last_bar_time)
so repeated requests across strategy agents within the same scan cycle are
cheap.

Public surface intentionally narrow: MACD, EMA, RSI, ATR. Other indicators
can be added as needed but should land here, not as ad-hoc copies elsewhere.
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Optional, Sequence

from analysis.macd_engine import compute_ema, compute_macd


@dataclass(frozen=True)
class MACDResult:
    macd: list[Optional[float]]
    signal: list[Optional[float]]
    histogram: list[Optional[float]]


@dataclass(frozen=True)
class IndicatorContext:
    symbol: str
    timeframe: str
    last_bar_time: Optional[str]


def _close_atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int) -> list[Optional[float]]:
    n = len(closes)
    if n == 0 or len(highs) != n or len(lows) != n:
        return [None] * max(n, 0)
    trs: list[float] = []
    for i in range(n):
        if i == 0:
            trs.append(float(highs[i] - lows[i]))
        else:
            prev_close = float(closes[i - 1])
            high = float(highs[i])
            low = float(lows[i])
            trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    atr: list[Optional[float]] = [None] * n
    if n < period:
        return atr
    running = sum(trs[:period]) / period
    atr[period - 1] = running
    for i in range(period, n):
        running = ((running * (period - 1)) + trs[i]) / period
        atr[i] = running
    return atr


def _rsi(closes: Sequence[float], period: int) -> list[Optional[float]]:
    n = len(closes)
    if n < period + 1:
        return [None] * n
    deltas = [float(closes[i] - closes[i - 1]) for i in range(1, n)]
    gains = [max(d, 0.0) for d in deltas]
    losses = [-min(d, 0.0) for d in deltas]
    rsi: list[Optional[float]] = [None] * n
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0:
        rsi[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi[period] = 100.0 - (100.0 / (1.0 + rs))
    for i in range(period + 1, n):
        avg_gain = ((avg_gain * (period - 1)) + gains[i - 1]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i - 1]) / period
        if avg_loss == 0:
            rsi[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i] = 100.0 - (100.0 / (1.0 + rs))
    return rsi


class IndicatorsAgent:
    """Centralised, cached indicator computation."""

    def __init__(self, max_cache: int = 2048) -> None:
        self._lock = RLock()
        self._cache: dict[tuple[str, str, Optional[str], str, tuple], object] = {}
        self._max_cache = max_cache

    def _key(self, ctx: IndicatorContext, name: str, params: tuple) -> tuple[str, str, Optional[str], str, tuple]:
        return (ctx.symbol, ctx.timeframe, ctx.last_bar_time, name, params)

    def _store(self, key: tuple, value: object) -> None:
        with self._lock:
            if len(self._cache) >= self._max_cache:
                # crude FIFO eviction
                first_key = next(iter(self._cache))
                self._cache.pop(first_key, None)
            self._cache[key] = value

    def macd(
        self,
        *,
        ctx: IndicatorContext,
        closes: Sequence[float],
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> MACDResult:
        key = self._key(ctx, "macd", (fast, slow, signal))
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached  # type: ignore[return-value]
        macd_line, signal_line, histogram = compute_macd(list(closes), fast, slow, signal)
        result = MACDResult(macd=macd_line, signal=signal_line, histogram=histogram)
        self._store(key, result)
        return result

    def ema(
        self,
        *,
        ctx: IndicatorContext,
        closes: Sequence[float],
        period: int,
    ) -> list[Optional[float]]:
        key = self._key(ctx, "ema", (period,))
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached  # type: ignore[return-value]
        result = compute_ema(list(closes), period)
        self._store(key, result)
        return result

    def rsi(
        self,
        *,
        ctx: IndicatorContext,
        closes: Sequence[float],
        period: int = 14,
    ) -> list[Optional[float]]:
        key = self._key(ctx, "rsi", (period,))
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached  # type: ignore[return-value]
        result = _rsi(closes, period)
        self._store(key, result)
        return result

    def atr(
        self,
        *,
        ctx: IndicatorContext,
        highs: Sequence[float],
        lows: Sequence[float],
        closes: Sequence[float],
        period: int = 14,
    ) -> list[Optional[float]]:
        key = self._key(ctx, "atr", (period,))
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached  # type: ignore[return-value]
        result = _close_atr(highs, lows, closes, period)
        self._store(key, result)
        return result

    def invalidate(self, *, symbol: Optional[str] = None) -> None:
        with self._lock:
            if symbol is None:
                self._cache.clear()
                return
            keys_to_drop = [k for k in self._cache.keys() if k[0] == symbol]
            for k in keys_to_drop:
                self._cache.pop(k, None)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"size": len(self._cache), "capacity": self._max_cache}


indicators_agent = IndicatorsAgent()


__all__ = [
    "IndicatorsAgent",
    "IndicatorContext",
    "MACDResult",
    "indicators_agent",
]
