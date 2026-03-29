"""Market Profile builder — TPO profiles from 3-minute candles."""
from __future__ import annotations
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from loguru import logger

from brokers.base import Tick
from db.redis_client import get_redis


TPO_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
PRICE_STEP = 0.5     # price granularity for TPO (0.5 for index options)
CANDLE_MINS = 3
PERIODS_PER_HOUR = 60 // CANDLE_MINS  # 20


@dataclass
class TPOCandle:
    open: float
    high: float
    low: float
    close: float
    volume: int
    timestamp: datetime
    tpo_letter: str = ""


@dataclass
class MarketProfileResult:
    symbol: str
    timeframe: str         # "daily" | "hourly"
    date: str
    poc: float             # Point of Control
    vah: float             # Value Area High
    val: float             # Value Area Low
    ib_high: float         # Initial Balance High (first hour)
    ib_low: float          # Initial Balance Low
    tpo_data: dict         # { price_level: [tpo_letters] }
    single_prints: List[float] = field(default_factory=list)
    poor_high: bool = False
    poor_low: bool = False


class MarketProfileBuilder:
    """Accumulates ticks into candles, builds TPO profiles."""

    VA_PERCENTAGE = 0.70   # 70% of TPO count in value area

    def __init__(self):
        self._candles: Dict[str, List[TPOCandle]] = {}   # symbol → candles
        self._current_candle: Dict[str, Optional[TPOCandle]] = {}
        self._candle_start: Dict[str, Optional[datetime]] = {}

    def on_tick(self, tick: Tick):
        sym = tick.symbol
        ts = tick.timestamp or datetime.utcnow()
        candle_start = self._get_candle_start(ts)

        if sym not in self._current_candle or self._candle_start.get(sym) != candle_start:
            # Save current candle if exists
            if sym in self._current_candle and self._current_candle[sym]:
                self._finalize_candle(sym)
            # Start new candle
            self._current_candle[sym] = TPOCandle(
                open=tick.ltp, high=tick.ltp, low=tick.ltp, close=tick.ltp,
                volume=tick.volume, timestamp=candle_start,
            )
            self._candle_start[sym] = candle_start
        else:
            candle = self._current_candle[sym]
            if candle:
                candle.high = max(candle.high, tick.ltp)
                candle.low = min(candle.low, tick.ltp)
                candle.close = tick.ltp
                candle.volume += tick.volume

    def _get_candle_start(self, ts: datetime) -> datetime:
        minute = (ts.minute // CANDLE_MINS) * CANDLE_MINS
        return ts.replace(minute=minute, second=0, microsecond=0)

    def _finalize_candle(self, sym: str):
        candle = self._current_candle.get(sym)
        if candle:
            idx = len(self._candles.get(sym, []))
            candle.tpo_letter = TPO_CHARS[idx % len(TPO_CHARS)]
            self._candles.setdefault(sym, []).append(candle)

    def build_daily_profile(self, symbol: str) -> Optional[MarketProfileResult]:
        candles = self._candles_with_current(symbol)
        if not candles:
            return None
        return self._build_profile(symbol, candles, "daily")

    def build_hourly_profile(self, symbol: str) -> Optional[MarketProfileResult]:
        """Use last 20 candles (1 hour at 3-min intervals)."""
        candles = self._candles_with_current(symbol)
        if not candles:
            return None
        recent = candles[-PERIODS_PER_HOUR:]
        return self._build_profile(symbol, recent, "hourly")

    def _candles_with_current(self, symbol: str) -> List[TPOCandle]:
        candles = list(self._candles.get(symbol, []))
        current = self._current_candle.get(symbol)
        if current:
            candles.append(current)
        return candles

    def _build_profile(
        self, symbol: str, candles: List[TPOCandle], timeframe: str
    ) -> MarketProfileResult:
        # Assign TPO letters per price level
        tpo_map: Dict[float, List[str]] = defaultdict(list)
        for candle in candles:
            price = candle.low
            step = PRICE_STEP
            while price <= candle.high:
                rounded = round(round(price / step) * step, 2)
                tpo_map[rounded].append(candle.tpo_letter)
                price += step

        if not tpo_map:
            return None

        # POC = price with most TPO letters
        poc = max(tpo_map, key=lambda p: len(tpo_map[p]))

        # Value area: 70% of total TPOs around POC
        total_tpos = sum(len(v) for v in tpo_map.values())
        target = total_tpos * self.VA_PERCENTAGE
        sorted_prices = sorted(tpo_map.keys())
        poc_idx = sorted_prices.index(poc)

        accumulated = len(tpo_map[poc])
        lo_idx = poc_idx
        hi_idx = poc_idx

        while accumulated < target and (lo_idx > 0 or hi_idx < len(sorted_prices) - 1):
            lo_add = len(tpo_map[sorted_prices[lo_idx - 1]]) if lo_idx > 0 else 0
            hi_add = len(tpo_map[sorted_prices[hi_idx + 1]]) if hi_idx < len(sorted_prices) - 1 else 0
            if lo_add >= hi_add and lo_idx > 0:
                lo_idx -= 1
                accumulated += lo_add
            elif hi_idx < len(sorted_prices) - 1:
                hi_idx += 1
                accumulated += hi_add
            else:
                break

        vah = sorted_prices[hi_idx]
        val = sorted_prices[lo_idx]

        # Initial Balance (first 2 candles = first 6 minutes, traditionally first hour)
        ib_candles = candles[:20]  # first 20 × 3-min = first hour
        ib_high = max(c.high for c in ib_candles) if ib_candles else poc
        ib_low = min(c.low for c in ib_candles) if ib_candles else poc

        # Single prints — price levels with only 1 TPO letter
        single_prints = [p for p, letters in tpo_map.items() if len(letters) == 1]

        # Poor high/low — last 2+ TPOs at extreme
        price_sorted = sorted(tpo_map.keys())
        poor_high = len(tpo_map.get(price_sorted[-1], [])) >= 2
        poor_low = len(tpo_map.get(price_sorted[0], [])) >= 2

        return MarketProfileResult(
            symbol=symbol,
            timeframe=timeframe,
            date=datetime.utcnow().strftime("%Y-%m-%d"),
            poc=poc,
            vah=vah,
            val=val,
            ib_high=ib_high,
            ib_low=ib_low,
            tpo_data={str(k): v for k, v in tpo_map.items()},
            single_prints=single_prints,
            poor_high=poor_high,
            poor_low=poor_low,
        )

    async def store_profile(self, profile: MarketProfileResult):
        """Cache profile in Redis."""
        redis = await get_redis()
        key = f"mp:{profile.symbol}:{profile.timeframe}"
        await redis.set(
            key,
            json.dumps({
                "symbol": profile.symbol,
                "timeframe": profile.timeframe,
                "date": profile.date,
                "poc": profile.poc,
                "vah": profile.vah,
                "val": profile.val,
                "ib_high": profile.ib_high,
                "ib_low": profile.ib_low,
                "tpo_data": profile.tpo_data,
                "single_prints": profile.single_prints,
                "poor_high": profile.poor_high,
                "poor_low": profile.poor_low,
            }),
            ex=3600,
        )

    async def get_cached_profile(self, symbol: str, timeframe: str) -> Optional[dict]:
        redis = await get_redis()
        raw = await redis.get(f"mp:{symbol}:{timeframe}")
        return json.loads(raw) if raw else None


market_profile_builder = MarketProfileBuilder()
