"""Market Profile builder for live ticks and historical candle windows."""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from brokers.base import Tick
from db.redis_client import get_redis


TPO_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
PRICE_STEP = 0.5
CANDLE_MINS = 3
PERIODS_PER_HOUR = 60 // CANDLE_MINS
MAX_TICKS_PER_SYMBOL = 6000


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
    timeframe: str
    date: str
    poc: float
    vah: float
    val: float
    ib_high: float
    ib_low: float
    tpo_data: dict
    single_prints: List[float] = field(default_factory=list)
    poor_high: bool = False
    poor_low: bool = False
    source_interval: str = ""
    sample_count: int = 0
    coverage_start: Optional[str] = None
    coverage_end: Optional[str] = None


class MarketProfileBuilder:
    """Accumulates live ticks and builds TPO profiles from arbitrary OHLC rows."""

    VA_PERCENTAGE = 0.70

    def __init__(self):
        self._candles: Dict[str, List[TPOCandle]] = {}
        self._current_candle: Dict[str, Optional[TPOCandle]] = {}
        self._candle_start: Dict[str, Optional[datetime]] = {}
        self._ticks: Dict[str, List[Tick]] = {}

    def on_tick(self, tick: Tick):
        sym = tick.symbol
        ts = self._ensure_utc_timestamp(tick.timestamp)
        self._ticks.setdefault(sym, []).append(
            Tick(
                symbol=sym,
                ltp=tick.ltp,
                open=tick.open,
                high=tick.high,
                low=tick.low,
                close=tick.close,
                volume=tick.volume,
                oi=tick.oi,
                bid=tick.bid,
                ask=tick.ask,
                bid_qty=tick.bid_qty,
                ask_qty=tick.ask_qty,
                timestamp=ts,
            )
        )
        self._trim_ticks(sym)

        candle_start = self._get_candle_start(ts)
        if sym not in self._current_candle or self._candle_start.get(sym) != candle_start:
            if sym in self._current_candle and self._current_candle[sym]:
                self._finalize_candle(sym)
            self._current_candle[sym] = TPOCandle(
                open=tick.ltp,
                high=tick.ltp,
                low=tick.ltp,
                close=tick.ltp,
                volume=tick.volume,
                timestamp=candle_start,
            )
            self._candle_start[sym] = candle_start
            return

        candle = self._current_candle[sym]
        if candle:
            candle.high = max(candle.high, tick.ltp)
            candle.low = min(candle.low, tick.ltp)
            candle.close = tick.ltp
            candle.volume += tick.volume

    def build_daily_profile(self, symbol: str) -> Optional[MarketProfileResult]:
        tick_rows = self.get_tick_rows(symbol)
        if not tick_rows:
            return None
        return self.build_profile_from_rows(symbol, tick_rows, "day", "tick")

    def build_hourly_profile(self, symbol: str) -> Optional[MarketProfileResult]:
        candles = self._candles_with_current(symbol)
        if not candles:
            return None
        recent = candles[-PERIODS_PER_HOUR:]
        return self._build_profile(symbol, recent, "hourly", "3minute")

    def build_profile_from_rows(
        self,
        symbol: str,
        rows: List[dict],
        timeframe: str,
        source_interval: str,
    ) -> Optional[MarketProfileResult]:
        candles = self.rows_to_candles(rows)
        if not candles:
            return None
        return self._build_profile(symbol, candles, timeframe, source_interval)

    def get_tick_rows(self, symbol: str) -> List[dict]:
        rows: List[dict] = []
        for tick in self._ticks.get(symbol, []):
            ts = self._ensure_utc_timestamp(tick.timestamp)
            rows.append(
                {
                    "time": ts.isoformat(),
                    "open": tick.ltp,
                    "high": tick.ltp,
                    "low": tick.ltp,
                    "close": tick.ltp,
                    "volume": tick.volume,
                }
            )
        return rows

    def get_three_minute_rows(self, symbol: str) -> List[dict]:
        return [
            {
                "time": candle.timestamp.isoformat(),
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
            }
            for candle in self._candles_with_current(symbol)
        ]

    def rows_to_candles(self, rows: List[dict]) -> List[TPOCandle]:
        candles: List[TPOCandle] = []
        for idx, row in enumerate(rows):
            ts_val = row.get("time") or row.get("timestamp")
            if not ts_val:
                continue
            try:
                ts = self._ensure_utc_timestamp(
                    datetime.fromisoformat(str(ts_val).replace("Z", "+00:00"))
                )
            except ValueError:
                continue
            candles.append(
                TPOCandle(
                    open=float(row.get("open", row.get("close", 0.0)) or 0.0),
                    high=float(row.get("high", row.get("close", 0.0)) or 0.0),
                    low=float(row.get("low", row.get("close", 0.0)) or 0.0),
                    close=float(row.get("close", 0.0) or 0.0),
                    volume=int(float(row.get("volume", 0) or 0)),
                    timestamp=ts,
                    tpo_letter=TPO_CHARS[idx % len(TPO_CHARS)],
                )
            )
        return candles

    def aggregate_rows(self, rows: List[dict], interval_minutes: int) -> List[dict]:
        if not rows:
            return []

        aggregated: List[dict] = []
        bucket_start: Optional[datetime] = None
        bucket: Optional[dict] = None

        for row in sorted(rows, key=lambda item: str(item.get("time") or item.get("timestamp"))):
            ts = self._ensure_utc_timestamp(
                datetime.fromisoformat(str(row.get("time") or row.get("timestamp")).replace("Z", "+00:00"))
            )
            rounded_minute = (ts.minute // interval_minutes) * interval_minutes
            current_start = ts.replace(minute=rounded_minute, second=0, microsecond=0)

            if bucket_start != current_start:
                if bucket is not None:
                    aggregated.append(bucket)
                bucket_start = current_start
                bucket = {
                    "time": current_start.isoformat(),
                    "open": float(row.get("open", row.get("close", 0.0)) or 0.0),
                    "high": float(row.get("high", row.get("close", 0.0)) or 0.0),
                    "low": float(row.get("low", row.get("close", 0.0)) or 0.0),
                    "close": float(row.get("close", 0.0) or 0.0),
                    "volume": int(float(row.get("volume", 0) or 0)),
                }
                continue

            if bucket is None:
                continue

            bucket["high"] = max(float(bucket["high"]), float(row.get("high", row.get("close", 0.0)) or 0.0))
            bucket["low"] = min(float(bucket["low"]), float(row.get("low", row.get("close", 0.0)) or 0.0))
            bucket["close"] = float(row.get("close", 0.0) or 0.0)
            bucket["volume"] = int(bucket["volume"]) + int(float(row.get("volume", 0) or 0))

        if bucket is not None:
            aggregated.append(bucket)
        return aggregated

    async def store_profile(self, profile: MarketProfileResult):
        # 1. Redis cache (fast read, 5-min TTL)
        redis = await get_redis()
        key = f"mp:{profile.symbol}:{profile.timeframe}"
        profile_dict = {
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
            "source_interval": profile.source_interval,
            "sample_count": profile.sample_count,
            "coverage_start": profile.coverage_start,
            "coverage_end": profile.coverage_end,
        }
        await redis.set(key, json.dumps(profile_dict), ex=300)

        # 2. DB persistence (long-term storage for analysis)
        try:
            from db.database import AsyncSessionLocal
            from sqlalchemy import text
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("""
                        INSERT INTO market_profiles (time, symbol, timeframe, poc, vah, val, ib_high, ib_low, tpo_data)
                        VALUES (NOW(), :symbol, :timeframe, :poc, :vah, :val, :ib_high, :ib_low, :tpo_data::jsonb)
                    """),
                    {
                        "symbol": profile.symbol,
                        "timeframe": profile.timeframe,
                        "poc": profile.poc,
                        "vah": profile.vah,
                        "val": profile.val,
                        "ib_high": profile.ib_high,
                        "ib_low": profile.ib_low,
                        "tpo_data": json.dumps(profile_dict),
                    },
                )
                await session.commit()
        except Exception:
            pass  # Non-fatal: Redis cache is primary, DB is for analysis

    async def get_cached_profile(self, symbol: str, timeframe: str) -> Optional[dict]:
        redis = await get_redis()
        raw = await redis.get(f"mp:{symbol}:{timeframe}")
        return json.loads(raw) if raw else None

    def _get_candle_start(self, ts: datetime) -> datetime:
        minute = (ts.minute // CANDLE_MINS) * CANDLE_MINS
        return ts.replace(minute=minute, second=0, microsecond=0)

    def _finalize_candle(self, sym: str):
        candle = self._current_candle.get(sym)
        if candle:
            idx = len(self._candles.get(sym, []))
            candle.tpo_letter = TPO_CHARS[idx % len(TPO_CHARS)]
            self._candles.setdefault(sym, []).append(candle)
            self._trim_candles(sym)

    def _candles_with_current(self, symbol: str) -> List[TPOCandle]:
        candles = list(self._candles.get(symbol, []))
        current = self._current_candle.get(symbol)
        if current:
            candles.append(current)
        return candles

    def _trim_ticks(self, sym: str):
        ticks = self._ticks.get(sym, [])
        if len(ticks) > MAX_TICKS_PER_SYMBOL:
            self._ticks[sym] = ticks[-MAX_TICKS_PER_SYMBOL:]
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=2)
        self._ticks[sym] = [tick for tick in ticks if self._ensure_utc_timestamp(tick.timestamp) >= cutoff]

    def _trim_candles(self, sym: str):
        candles = self._candles.get(sym, [])
        cutoff = datetime.now(timezone.utc) - timedelta(days=40)
        self._candles[sym] = [c for c in candles if self._ensure_utc_timestamp(c.timestamp) >= cutoff][-2000:]

    def _build_profile(
        self,
        symbol: str,
        candles: List[TPOCandle],
        timeframe: str,
        source_interval: str,
    ) -> Optional[MarketProfileResult]:
        if not candles:
            return None

        tpo_map: Dict[float, List[str]] = defaultdict(list)
        for idx, candle in enumerate(candles):
            if not candle.tpo_letter:
                candle.tpo_letter = TPO_CHARS[idx % len(TPO_CHARS)]
            price = candle.low
            while price <= candle.high:
                rounded = round(round(price / PRICE_STEP) * PRICE_STEP, 2)
                tpo_map[rounded].append(candle.tpo_letter)
                price += PRICE_STEP

        if not tpo_map:
            return None

        sorted_prices = sorted(tpo_map.keys())
        poc = max(sorted_prices, key=lambda price: len(tpo_map[price]))
        total_tpos = sum(len(values) for values in tpo_map.values())
        target = total_tpos * self.VA_PERCENTAGE
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
        ib_window = min(PERIODS_PER_HOUR, len(candles))
        ib_candles = candles[:ib_window]
        ib_high = max(candle.high for candle in ib_candles) if ib_candles else poc
        ib_low = min(candle.low for candle in ib_candles) if ib_candles else poc
        single_prints = [price for price, letters in tpo_map.items() if len(letters) == 1]
        poor_high = len(tpo_map.get(sorted_prices[-1], [])) >= 2
        poor_low = len(tpo_map.get(sorted_prices[0], [])) >= 2
        coverage_start = candles[0].timestamp.isoformat()
        coverage_end = candles[-1].timestamp.isoformat()

        return MarketProfileResult(
            symbol=symbol,
            timeframe=timeframe,
            date=datetime.utcnow().strftime("%Y-%m-%d"),
            poc=poc,
            vah=vah,
            val=val,
            ib_high=ib_high,
            ib_low=ib_low,
            tpo_data={str(price): letters for price, letters in tpo_map.items()},
            single_prints=single_prints,
            poor_high=poor_high,
            poor_low=poor_low,
            source_interval=source_interval,
            sample_count=len(candles),
            coverage_start=coverage_start,
            coverage_end=coverage_end,
        )

    @staticmethod
    def _ensure_utc_timestamp(value: Optional[datetime]) -> datetime:
        if value is None:
            return datetime.now(timezone.utc)
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


market_profile_builder = MarketProfileBuilder()
