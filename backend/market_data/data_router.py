"""Data router — manages WebSocket feeds and publishes ticks to Redis."""
from __future__ import annotations
import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from brokers.base import BrokerAdapter, Tick
from db.redis_client import get_redis
from market_data.symbols import to_app_symbol, to_broker_symbol, to_fyers_symbol


class DataRouter:
    """
    Selects the active broker's WebSocket feed.
    Publishes each tick to Redis channel ticks:{symbol}
    and calls registered local callbacks.
    """

    def __init__(self):
        self._broker: Optional[BrokerAdapter] = None
        self._ws_client: Any = None
        self._subscribed_symbols: List[str] = []
        self._callbacks: Dict[str, List[Callable[[Tick], None]]] = {}
        self._global_callbacks: List[Callable[[Tick], None]] = []
        self._tick_buffer: Dict[str, Tick] = {}  # latest tick per symbol
        self._mock_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_broker(self, broker: BrokerAdapter):
        self._broker = broker

    def register_callback(self, symbol: str, callback: Callable[[Tick], None]):
        self._callbacks.setdefault(symbol, []).append(callback)

    def register_global_callback(self, callback: Callable[[Tick], None]):
        if callback not in self._global_callbacks:
            self._global_callbacks.append(callback)

    async def subscribe(self, symbols: List[str]):
        if not self._broker:
            logger.warning("[DataRouter] No broker set — cannot subscribe")
            return
        await self.stop_mock_feed()
        await self.unsubscribe()
        self._loop = asyncio.get_running_loop()
        self._subscribed_symbols = symbols
        broker_name = getattr(self._broker, "broker_name", "")
        if broker_name == "fyers":
            broker_symbols = [to_fyers_symbol(symbol) for symbol in symbols]
        else:
            broker_symbols = [to_broker_symbol(symbol) for symbol in symbols]
        self._ws_client = await self._broker.subscribe_websocket(
            broker_symbols, self._on_tick
        )
        logger.info(f"[DataRouter] Subscribed to {len(symbols)} symbols")

    async def unsubscribe(self):
        if self._ws_client:
            try:
                self._ws_client.close()
            except Exception:
                pass
        self._ws_client = None

    async def stop_mock_feed(self):
        if self._mock_task and not self._mock_task.done():
            self._mock_task.cancel()
            try:
                await self._mock_task
            except asyncio.CancelledError:
                pass
        self._mock_task = None

    def _on_tick(self, tick: Tick):
        """Handle an incoming tick synchronously, publish to Redis async."""
        tick.symbol = to_app_symbol(tick.symbol)
        tick.timestamp = self._ensure_utc_timestamp(tick.timestamp)
        self._tick_buffer[tick.symbol] = tick
        # Dispatch to local callbacks
        for cb in self._callbacks.get(tick.symbol, []):
            try:
                cb(tick)
            except Exception as e:
                logger.error(f"[DataRouter] Callback error for {tick.symbol}: {e}")
        for cb in self._global_callbacks:
            try:
                cb(tick)
            except Exception as e:
                logger.error(f"[DataRouter] Global callback error for {tick.symbol}: {e}")
        # Fyers websocket callbacks can arrive on a non-async thread.
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop and running_loop is self._loop:
            asyncio.create_task(self._publish_tick(tick))
        elif self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._publish_tick(tick), self._loop)

    async def _publish_tick(self, tick: Tick):
        try:
            redis = await get_redis()
            timestamp = self._ensure_utc_timestamp(tick.timestamp)
            payload = json.dumps({
                "symbol": tick.symbol,
                "ltp": tick.ltp,
                "open": tick.open,
                "high": tick.high,
                "low": tick.low,
                "close": tick.close,
                "volume": tick.volume,
                "oi": tick.oi,
                "bid": tick.bid,
                "ask": tick.ask,
                "timestamp": timestamp.isoformat(),
            })
            await redis.publish(f"ticks:{tick.symbol}", payload)
        except Exception as e:
            logger.debug(f"[DataRouter] Redis publish error: {e}")

    def get_latest_tick(self, symbol: str) -> Optional[Tick]:
        return self._tick_buffer.get(symbol)

    def get_ltp(self, symbol: str) -> float:
        tick = self._tick_buffer.get(symbol)
        return tick.ltp if tick else 0.0

    def get_status(self) -> dict[str, Any]:
        broker_name = getattr(self._broker, "broker_name", None) if self._broker else None
        mock_running = bool(self._mock_task and not self._mock_task.done())
        last_tick_times = [
            self._ensure_utc_timestamp(tick.timestamp)
            for tick in self._tick_buffer.values()
            if tick.timestamp is not None
        ]
        last_tick_at = max(last_tick_times) if last_tick_times else None
        callback_count = sum(len(callbacks) for callbacks in self._callbacks.values())

        if mock_running:
            mode = "mock"
        elif broker_name:
            mode = "broker"
        else:
            mode = "idle"

        return {
            "mode": mode,
            "broker": broker_name,
            "subscribed_symbols": list(self._subscribed_symbols),
            "subscribed_symbol_count": len(self._subscribed_symbols),
            "tick_buffer_size": len(self._tick_buffer),
            "callback_count": callback_count,
            "ws_connected": bool(self._ws_client),
            "mock_running": mock_running,
            "last_tick_at": last_tick_at.isoformat() if last_tick_at else None,
        }

    # ── Mock tick feed for testing ───────────────────────────────────────────

    async def start_mock_feed(self, symbols: List[str], interval_secs: float = 1.0):
        """Generate synthetic ticks for paper trading without a real broker."""
        import random
        self._mock_task = asyncio.current_task()
        prices = {s: 100.0 for s in symbols}
        logger.info(f"[DataRouter] Starting mock feed for {symbols}")
        try:
            while True:
                for sym in symbols:
                    price = prices[sym]
                    change = random.uniform(-0.5, 0.5)
                    prices[sym] = max(1.0, price + change)
                    tick = Tick(
                        symbol=sym,
                        ltp=round(prices[sym], 2),
                        open=round(price, 2),
                        high=round(prices[sym] * 1.002, 2),
                        low=round(prices[sym] * 0.998, 2),
                        close=round(price, 2),
                        volume=random.randint(100, 10000),
                        timestamp=datetime.now(timezone.utc),
                    )
                    self._on_tick(tick)
                await asyncio.sleep(interval_secs)
        finally:
            self._mock_task = None

    @staticmethod
    def _ensure_utc_timestamp(value: Optional[datetime]) -> datetime:
        if value is None:
            return datetime.now(timezone.utc)
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


# ── Singleton ────────────────────────────────────────────────────────────────
data_router = DataRouter()
