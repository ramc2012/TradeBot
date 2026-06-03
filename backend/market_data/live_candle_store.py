"""Persist live ticks and aggregate them into reusable intraday candles."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger
from sqlalchemy import text

from brokers.base import Tick
from db.database import AsyncSessionLocal
from market_data.candle_timeframes import CANDLE_INTERVALS_MINUTES, floor_timestamp
from market_data.symbols import DISPLAY_NAMES


@dataclass
class _CandleBucket:
    symbol: str
    interval: str
    bucket_start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    oi: int
    updated_at: datetime
    dirty: bool = True


class LiveCandleStore:
    FLUSH_INTERVAL_SECONDS = 5.0
    BATCH_SIZE = 250

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None
        self._queue: asyncio.Queue[Tick] = asyncio.Queue()
        self._tick_batch: list[Tick] = []
        self._buckets: dict[tuple[str, str], _CandleBucket] = {}
        self._metadata_cache: dict[str, Optional[dict[str, Any]]] = {}
        self._latest_spot: dict[str, float] = {}

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._worker(), name="live-candle-store")

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    def on_tick(self, tick: Tick) -> None:
        if not self._loop or not self._loop.is_running():
            return
        if tick.timestamp is None:
            tick.timestamp = datetime.now(timezone.utc)
        if tick.timestamp.tzinfo is None:
            tick.timestamp = tick.timestamp.replace(tzinfo=timezone.utc)
        else:
            tick.timestamp = tick.timestamp.astimezone(timezone.utc)

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop and running_loop is self._loop:
            self._queue.put_nowait(tick)
        else:
            asyncio.run_coroutine_threadsafe(self._queue.put(tick), self._loop)

    async def _worker(self) -> None:
        try:
            while True:
                try:
                    tick = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=self.FLUSH_INTERVAL_SECONDS,
                    )
                except asyncio.TimeoutError:
                    await self._flush_pending()
                    continue

                self._tick_batch.append(tick)
                self._update_buckets(tick)
                if len(self._tick_batch) >= self.BATCH_SIZE:
                    await self._flush_pending()
        except asyncio.CancelledError:
            await self._drain_queue()
            await self._flush_pending(force=True)
            raise

    async def _drain_queue(self) -> None:
        while not self._queue.empty():
            tick = await self._queue.get()
            self._tick_batch.append(tick)
            self._update_buckets(tick)

    def _update_buckets(self, tick: Tick) -> None:
        timestamp = tick.timestamp or datetime.now(timezone.utc)
        if tick.symbol in DISPLAY_NAMES:
            self._latest_spot[DISPLAY_NAMES[tick.symbol]] = float(tick.ltp)

        for minutes, interval in CANDLE_INTERVALS_MINUTES.items():
            bucket_start = floor_timestamp(timestamp, minutes)
            key = (tick.symbol, interval)
            bucket = self._buckets.get(key)
            if bucket is None or bucket.bucket_start != bucket_start:
                bucket = _CandleBucket(
                    symbol=tick.symbol,
                    interval=interval,
                    bucket_start=bucket_start,
                    open=float(tick.ltp),
                    high=float(tick.ltp),
                    low=float(tick.ltp),
                    close=float(tick.ltp),
                    volume=int(tick.volume or 0),
                    oi=int(tick.oi or 0),
                    updated_at=timestamp,
                )
                self._buckets[key] = bucket
                continue

            price = float(tick.ltp)
            bucket.high = max(bucket.high, price)
            bucket.low = min(bucket.low, price)
            bucket.close = price
            bucket.volume = int(tick.volume or 0)
            bucket.oi = int(tick.oi or 0)
            bucket.updated_at = timestamp
            bucket.dirty = True

    async def _flush_pending(self, *, force: bool = False) -> None:
        await self._persist_ticks()
        await self._persist_candles(force=force)

    async def _persist_ticks(self) -> None:
        if not self._tick_batch:
            return

        payload = [
            {
                "time": tick.timestamp,
                "symbol": tick.symbol,
                "ltp": float(tick.ltp),
                "open": float(tick.open or tick.ltp),
                "high": float(tick.high or tick.ltp),
                "low": float(tick.low or tick.ltp),
                "close": float(tick.close or tick.ltp),
                "volume": int(tick.volume or 0),
                "oi": int(tick.oi or 0),
                "bid": float(tick.bid or 0.0),
                "ask": float(tick.ask or 0.0),
                "bid_qty": int(tick.bid_qty or 0),
                "ask_qty": int(tick.ask_qty or 0),
                "total_buy_qty": int(getattr(tick, "total_buy_qty", 0) or 0),
                "total_sell_qty": int(getattr(tick, "total_sell_qty", 0) or 0),
            }
            for tick in self._tick_batch
            if tick.timestamp is not None
        ]
        self._tick_batch.clear()
        if not payload:
            return

        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO market_ticks (
                        time, symbol, ltp, open, high, low, close, volume,
                        oi, bid, ask, bid_qty, ask_qty, total_buy_qty, total_sell_qty
                    ) VALUES (
                        :time, :symbol, :ltp, :open, :high, :low, :close, :volume,
                        :oi, :bid, :ask, :bid_qty, :ask_qty, :total_buy_qty, :total_sell_qty
                    )
                    """
                ),
                payload,
            )
            await session.commit()

    async def _persist_candles(self, *, force: bool = False) -> None:
        dirty_buckets = [
            bucket for bucket in self._buckets.values()
            if bucket.dirty or force
        ]
        if not dirty_buckets:
            return

        spot_rows: list[dict[str, Any]] = []
        option_rows: list[dict[str, Any]] = []

        for bucket in dirty_buckets:
            metadata = await self._resolve_symbol_metadata(bucket.symbol)
            if not metadata:
                continue
            bucket.dirty = False

            if metadata["kind"] == "spot":
                spot_rows.append(
                    {
                        "time": bucket.bucket_start,
                        "instrument_key": metadata["instrument_key"],
                        "underlying": metadata["underlying"],
                        "interval": bucket.interval,
                        "open": bucket.open,
                        "high": bucket.high,
                        "low": bucket.low,
                        "close": bucket.close,
                        "volume": bucket.volume,
                        "oi": bucket.oi,
                        "source": "live_tick",
                    }
                )
                continue

            option_rows.append(
                {
                    "time": bucket.bucket_start,
                    "instrument_key": metadata["instrument_key"],
                    "trading_symbol": metadata.get("trading_symbol"),
                    "underlying": metadata["underlying"],
                    "market": metadata.get("market", "NSE"),
                    "expiry": metadata["expiry"],
                    "strike": metadata["strike"],
                    "option_type": metadata["option_type"],
                    "interval": bucket.interval,
                    "open": bucket.open,
                    "high": bucket.high,
                    "low": bucket.low,
                    "close": bucket.close,
                    "volume": bucket.volume,
                    "oi": bucket.oi,
                    "iv": None,
                    "delta": None,
                    "gamma": None,
                    "theta": None,
                    "vega": None,
                    "underlying_price": self._latest_spot.get(metadata["underlying"]),
                    "source": "live_tick",
                }
            )

        async with AsyncSessionLocal() as session:
            if spot_rows:
                await session.execute(
                    text(
                        """
                        INSERT INTO underlying_spot_candles (
                            time, instrument_key, underlying, interval, open, high,
                            low, close, volume, oi, source, synced_at
                        ) VALUES (
                            :time, :instrument_key, :underlying, :interval, :open, :high,
                            :low, :close, :volume, :oi, :source, NOW()
                        )
                        ON CONFLICT (instrument_key, interval, time) DO UPDATE
                        SET open = EXCLUDED.open,
                            high = EXCLUDED.high,
                            low = EXCLUDED.low,
                            close = EXCLUDED.close,
                            volume = EXCLUDED.volume,
                            oi = EXCLUDED.oi,
                            source = EXCLUDED.source,
                            synced_at = NOW()
                        """
                    ),
                    spot_rows,
                )

            if option_rows:
                await session.execute(
                    text(
                        """
                        INSERT INTO option_premium_candles (
                            time, instrument_key, trading_symbol, underlying, market,
                            expiry, strike, option_type, interval, open, high, low,
                            close, volume, oi, iv, delta, gamma, theta, vega,
                            underlying_price, source, synced_at
                        ) VALUES (
                            :time, :instrument_key, :trading_symbol, :underlying, :market,
                            :expiry, :strike, :option_type, :interval, :open, :high, :low,
                            :close, :volume, :oi, :iv, :delta, :gamma, :theta, :vega,
                            :underlying_price, :source, NOW()
                        )
                        ON CONFLICT (instrument_key, interval, time) DO UPDATE
                        SET trading_symbol = EXCLUDED.trading_symbol,
                            underlying = EXCLUDED.underlying,
                            market = EXCLUDED.market,
                            expiry = EXCLUDED.expiry,
                            strike = EXCLUDED.strike,
                            option_type = EXCLUDED.option_type,
                            open = EXCLUDED.open,
                            high = EXCLUDED.high,
                            low = EXCLUDED.low,
                            close = EXCLUDED.close,
                            volume = EXCLUDED.volume,
                            oi = EXCLUDED.oi,
                            underlying_price = COALESCE(EXCLUDED.underlying_price, option_premium_candles.underlying_price),
                            source = EXCLUDED.source,
                            synced_at = NOW()
                        """
                    ),
                    option_rows,
                )

            await session.commit()

    async def _resolve_symbol_metadata(self, symbol: str) -> Optional[dict[str, Any]]:
        if symbol in self._metadata_cache:
            return self._metadata_cache[symbol]

        metadata: Optional[dict[str, Any]] = None
        async with AsyncSessionLocal() as session:
            if symbol in DISPLAY_NAMES:
                underlying = DISPLAY_NAMES[symbol]
                result = await session.execute(
                    text(
                        """
                        SELECT symbol, spot_instrument_key
                        FROM fo_underlying_catalog
                        WHERE symbol = :underlying
                        LIMIT 1
                        """
                    ),
                    {"underlying": underlying},
                )
                row = result.first()
                metadata = {
                    "kind": "spot",
                    "underlying": underlying,
                    "instrument_key": str(getattr(row, "spot_instrument_key", None) or symbol),
                }
            else:
                result = await session.execute(
                    text(
                        """
                        SELECT instrument_key, trading_symbol, underlying, expiry, strike, option_type, market
                        FROM fo_contract_catalog
                        WHERE instrument_key = :symbol OR trading_symbol = :symbol
                        LIMIT 1
                        """
                    ),
                    {"symbol": symbol},
                )
                row = result.first()
                if row:
                    metadata = {
                        "kind": "option",
                        "instrument_key": row.instrument_key,
                        "trading_symbol": row.trading_symbol,
                        "underlying": row.underlying,
                        "expiry": row.expiry,
                        "strike": float(row.strike) if row.strike is not None else None,
                        "option_type": row.option_type,
                        "market": row.market or "NSE",
                    }

        self._metadata_cache[symbol] = metadata
        if metadata is None:
            logger.debug(f"[LiveCandleStore] No candle metadata mapping for {symbol}")
        return metadata


live_candle_store = LiveCandleStore()
