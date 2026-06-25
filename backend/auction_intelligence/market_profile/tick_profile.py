"""Tick-based Market Profile for indices.

The TPO :class:`MarketProfileEngine` builds a *time* profile from 30-minute
bars — it only advances when a bar closes. An index, however, has a dense LTP
tick stream (``market_ticks``) with no order book. This module builds a
**tick / volume profile** straight from those ticks: each tick's price is
bucketed onto the ``tick_size`` ladder and we accumulate a tick count (plus
traded volume where the feed carries it) per bucket, then derive POC and the
value area with the *same* 70% expansion the TPO engine uses. The result
develops continuously (every tick), so an index gets a fine-grained,
intra-period auction read instead of waiting for the next 30-minute close.

This is intentionally decoupled from the broker order book: it works on the
index LTP ticks we already capture, and slots in alongside the bar TPO
profile rather than replacing it.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import floor
from typing import Any, Optional, Sequence


def round_to_tick(price: float, tick_size: float) -> float:
    """Round a price to the nearest point on the tick_size ladder."""
    if tick_size <= 0:
        return round(price, 2)
    return round(floor(price / tick_size + 0.5) * tick_size, 6)


def _value_area(
    prices: list[float],
    counts: dict[float, int],
    poc: float,
    value_area_pct: float,
) -> tuple[float, float]:
    """70% value area by symmetric expansion from POC — same algorithm as
    ``MarketProfileEngine._value_area`` but over a tick/volume histogram.
    Returns ``(vah, val)`` (prices ascending → upper, lower)."""
    total = sum(counts.values())
    target = max(int(total * value_area_pct), 1)
    poc_index = prices.index(poc)
    lower = upper = poc_index
    covered = counts[poc]
    while covered < target and (lower > 0 or upper < len(prices) - 1):
        next_low = counts[prices[lower - 1]] if lower > 0 else -1
        next_high = counts[prices[upper + 1]] if upper < len(prices) - 1 else -1
        if next_high > next_low:
            upper += 1
            covered += next_high
        elif next_low > next_high:
            lower -= 1
            covered += next_low
        else:
            if upper < len(prices) - 1:
                upper += 1
                covered += max(next_high, 0)
            if covered < target and lower > 0:
                lower -= 1
                covered += max(next_low, 0)
    return prices[upper], prices[lower]


@dataclass
class TickProfile:
    symbol: str
    tick_size: float
    value_area_pct: float
    poc: float
    vah: float
    val: float
    high_price: float
    low_price: float
    last_price: float
    total_ticks: int
    total_volume: float
    # price → {"ticks": int, "volume": float}
    histogram: dict[float, dict[str, float]]
    first_time: Optional[str] = None
    last_time: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "tick_profile",
            "symbol": self.symbol,
            "tick_size": self.tick_size,
            "value_area_pct": self.value_area_pct,
            "poc": round(self.poc, 2),
            "vah": round(self.vah, 2),
            "val": round(self.val, 2),
            "high_price": round(self.high_price, 2),
            "low_price": round(self.low_price, 2),
            "last_price": round(self.last_price, 2),
            "total_ticks": self.total_ticks,
            "total_volume": round(self.total_volume, 2),
            "first_time": self.first_time,
            "last_time": self.last_time,
            # JSON needs string keys; keep ticks + volume per price level.
            "histogram": {
                f"{price:.2f}": {
                    "ticks": int(cell["ticks"]),
                    "volume": round(float(cell["volume"]), 2),
                }
                for price, cell in sorted(self.histogram.items())
            },
        }


def _tick_price(row: dict[str, Any], price_key: str) -> float:
    for key in (price_key, "ltp", "last_price", "close", "price"):
        value = row.get(key)
        if value is not None:
            try:
                price = float(value)
            except (TypeError, ValueError):
                continue
            if price > 0:
                return price
    return 0.0


def _tick_time(row: dict[str, Any]) -> Optional[str]:
    value = row.get("time") or row.get("timestamp")
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def build_tick_profile(
    ticks: Sequence[dict[str, Any]],
    *,
    symbol: str,
    tick_size: float,
    value_area_pct: float = 0.70,
    price_key: str = "ltp",
    min_ticks: int = 30,
) -> Optional[TickProfile]:
    """Build a tick/volume profile from a sequence of tick rows.

    Each row needs a price (``ltp``/``last_price``/``close``). Traded volume
    per tick is taken as the forward delta of a cumulative ``volume`` field
    when it is present and monotonic; indices typically report no per-tick
    volume, in which case the profile is a pure tick-frequency distribution
    (still a valid POC/value-area read). Returns ``None`` when there aren't
    at least ``min_ticks`` usable ticks.
    """
    counts: dict[float, int] = {}
    volume_at: dict[float, float] = {}
    high = float("-inf")
    low = float("inf")
    last = 0.0
    total_ticks = 0
    total_volume = 0.0
    prev_cum_volume: Optional[float] = None
    first_time: Optional[str] = None
    last_time: Optional[str] = None

    for row in ticks:
        price = _tick_price(row, price_key)
        if price <= 0:
            continue
        bucket = round_to_tick(price, tick_size)

        # Per-tick traded volume = positive delta of cumulative volume, when
        # the feed supplies it; otherwise 0 (tick-frequency profile).
        tick_vol = 0.0
        raw_vol = row.get("volume")
        if raw_vol is not None:
            try:
                cum = float(raw_vol)
                if prev_cum_volume is not None and cum >= prev_cum_volume:
                    tick_vol = cum - prev_cum_volume
                prev_cum_volume = cum
            except (TypeError, ValueError):
                pass

        counts[bucket] = counts.get(bucket, 0) + 1
        volume_at[bucket] = volume_at.get(bucket, 0.0) + tick_vol
        total_ticks += 1
        total_volume += tick_vol
        last = price
        high = max(high, price)
        low = min(low, price)
        if first_time is None:
            first_time = _tick_time(row)
        last_time = _tick_time(row) or last_time

    if total_ticks < max(min_ticks, 1) or not counts:
        return None

    return profile_from_histogram(
        symbol=symbol,
        tick_size=tick_size,
        value_area_pct=value_area_pct,
        counts=counts,
        volume_at=volume_at,
        high=high,
        low=low,
        last=last,
        total_ticks=total_ticks,
        total_volume=total_volume,
        first_time=first_time,
        last_time=last_time,
    )


def profile_from_histogram(
    *,
    symbol: str,
    tick_size: float,
    counts: dict[float, int],
    volume_at: Optional[dict[float, float]] = None,
    high: float,
    low: float,
    last: float,
    total_ticks: int,
    total_volume: float = 0.0,
    value_area_pct: float = 0.70,
    first_time: Optional[str] = None,
    last_time: Optional[str] = None,
    min_ticks: int = 30,
) -> Optional[TickProfile]:
    """Build a :class:`TickProfile` from a pre-aggregated price→tick-count
    histogram. Lets the caller compute the histogram cheaply (e.g. a SQL
    ``GROUP BY`` over a price bucket) instead of streaming every raw tick.
    """
    counts = {round(float(p), 6): int(c) for p, c in counts.items() if c}
    if total_ticks < max(min_ticks, 1) or not counts:
        return None
    volume_at = {round(float(p), 6): float(v) for p, v in (volume_at or {}).items()}
    prices = sorted(counts)
    max_count = max(counts.values())
    midpoint = (high + low) / 2.0
    poc = min(
        (p for p in prices if counts[p] == max_count),
        key=lambda p: abs(p - midpoint),
    )
    vah, val = _value_area(prices, counts, poc, value_area_pct)
    return TickProfile(
        symbol=symbol,
        tick_size=tick_size,
        value_area_pct=value_area_pct,
        poc=poc,
        vah=vah,
        val=val,
        high_price=high,
        low_price=low,
        last_price=last,
        total_ticks=total_ticks,
        total_volume=total_volume,
        histogram={p: {"ticks": counts[p], "volume": volume_at.get(p, 0.0)} for p in prices},
        first_time=first_time,
        last_time=last_time,
    )


__all__ = ["TickProfile", "build_tick_profile", "profile_from_histogram", "round_to_tick"]
