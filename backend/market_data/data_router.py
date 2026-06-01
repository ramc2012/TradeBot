"""Data router — manages WebSocket feeds and publishes ticks to Redis."""
from __future__ import annotations
import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from brokers.base import BrokerAdapter, Tick
from db.redis_client import get_redis
from market_data.symbols import (
    TICK_CAPTURE_APP_SYMBOLS,
    to_app_symbol,
    to_broker_symbol,
    to_fyers_symbol,
)


IST = timezone(timedelta(hours=5, minutes=30))


# Redis last-value tick hot-cache. `tick:{symbol}` holds the most recent
# tick JSON so cross-process readers and late WS subscribers can fetch a
# live mark without the process-local _tick_buffer. TTL evicts symbols the
# feed has stopped sending (e.g. after an expiry roll or unsubscribe).
LATEST_TICK_KEY_PREFIX = "tick:"
LATEST_TICK_TTL_SECONDS = 300


class DataRouter:
    """
    Selects the active broker's WebSocket feed.
    Publishes each tick to Redis channel ticks:{symbol}
    and calls registered local callbacks.
    """

    def __init__(self):
        self._broker: Optional[BrokerAdapter] = None
        self._ws_client: Any = None
        self._ws_broker: Optional[BrokerAdapter] = None
        self._subscribed_symbols: List[str] = []
        # Sticky extras — symbols that the OptionWS subscription manager
        # (or any future per-strategy subscriber) pins onto the broker
        # WS regardless of who calls subscribe() afterward. Without this
        # set, a spot-only resync from auth / _sync_market_data_feed
        # would wipe a previously-applied option subscription.
        self._sticky_extras: set[str] = set()
        # Required symbols — the MP-critical index streams that must stay on
        # every broker subscription so market_ticks never loses NIFTY /
        # BANKNIFTY / SENSEX. Defaults to the capture set; set_required_symbols
        # can override. These are always merged into subscribe()'s broker call.
        self._required_symbols: List[str] = [
            to_app_symbol(s) for s in TICK_CAPTURE_APP_SYMBOLS
        ]
        self._callbacks: Dict[str, List[Callable[[Tick], None]]] = {}
        self._global_callbacks: List[Callable[[Tick], None]] = []
        self._tick_buffer: Dict[str, Tick] = {}  # latest tick per symbol
        self._mock_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._source_policy: dict[str, Any] = {}
        self._reconnect_task: Optional[asyncio.Task] = None
        self._last_reconnect_attempt_at: Optional[datetime] = None
        self._reconnect_backoff = timedelta(seconds=60)
        # Required-feed watchdog: a periodic guard that re-subscribes any
        # required index symbol that dropped off the broker WS, and forces a
        # reconnect when a required symbol's last tick is older than the
        # staleness budget during market hours.
        self._watchdog_task: Optional[asyncio.Task] = None
        self._watchdog_interval_seconds = 30.0
        self._required_tick_stale_seconds = 90.0

    def set_broker(self, broker: BrokerAdapter):
        self._broker = broker

    def set_source_policy(self, policy: dict[str, Any]):
        self._source_policy = dict(policy or {})

    def register_callback(self, symbol: str, callback: Callable[[Tick], None]):
        self._callbacks.setdefault(symbol, []).append(callback)

    def register_global_callback(self, callback: Callable[[Tick], None]):
        if callback not in self._global_callbacks:
            self._global_callbacks.append(callback)

    def set_required_symbols(self, symbols: List[str]) -> None:
        """Set the symbols that must stay on every broker subscription.

        Normalized to app symbols and de-duplicated. These are always merged
        into ``subscribe()``'s broker call so the MP-critical index streams
        never drop off market_ticks.
        """
        seen: set[str] = set()
        required: List[str] = []
        for symbol in symbols:
            app = to_app_symbol(symbol)
            if app and app not in seen:
                seen.add(app)
                required.append(app)
        self._required_symbols = required

    def _compose_subscription_set(self, symbols: List[str]) -> List[str]:
        """Merge the caller's symbols with required + sticky extras.

        Order is primary → required → sticky; de-duplicated via to_app_symbol
        so the returned list is the canonical app-symbol subscription set.
        """
        seen: set[str] = set()
        out: List[str] = []
        for symbol in [
            *symbols,
            *self._required_symbols,
            *getattr(self, "_sticky_extras", set()),
        ]:
            app = to_app_symbol(symbol)
            if app and app not in seen:
                seen.add(app)
                out.append(app)
        return out

    async def subscribe(self, symbols: List[str]):
        if not self._broker:
            logger.warning("[DataRouter] No broker set — cannot subscribe")
            return
        # Merge required index streams + sticky extras (e.g. option contracts
        # pinned by the OptionWS subscription manager). Without this, a
        # spot-only resync from the broker-session refresh path would wipe a
        # previously-applied option subscription or drop a required index.
        full_set = self._compose_subscription_set(symbols)
        sticky_extras = [s for s in full_set if s in self._sticky_extras]
        if (
            self._ws_client is not None
            and self._ws_broker is self._broker
            and set(full_set) == set(self._subscribed_symbols)
        ):
            logger.debug(
                f"[DataRouter] Subscription already active for {len(full_set)} symbols; skipping resubscribe"
            )
            return
        await self.stop_mock_feed()
        await self.unsubscribe()
        self._loop = asyncio.get_running_loop()
        self._subscribed_symbols = full_set
        broker_name = getattr(self._broker, "broker_name", "")
        if broker_name == "fyers":
            broker_symbols = [to_fyers_symbol(symbol) for symbol in full_set]
        else:
            broker_symbols = [to_broker_symbol(symbol) for symbol in full_set]
        self._ws_client = await self._broker.subscribe_websocket(
            broker_symbols, self._on_tick
        )
        self._ws_broker = self._broker
        logger.info(
            f"[DataRouter] Subscribed to {len(full_set)} symbols "
            f"(primary={len(symbols)} required={len(self._required_symbols)} sticky={len(sticky_extras)})"
        )

    async def add_subscriptions(self, symbols: List[str]) -> int:
        """Append symbols to the WebSocket subscription (idempotent).

        Each added symbol is also pinned into _sticky_extras so a later
        spot-only resync from the broker auth flow doesn't drop them.
        Returns the count of newly added symbols.
        """
        new = [s for s in symbols if s and s not in self._subscribed_symbols]
        if not new:
            return 0
        for s in new:
            self._sticky_extras.add(s)
        # subscribe() will re-merge sticky_extras into the broker call.
        # Pass the current PRIMARY symbol set as the explicit list so
        # subscribe doesn't accidentally drop spots when the caller is
        # an add path. We compute primary as anything currently
        # subscribed that isn't a sticky extra.
        primary = [s for s in self._subscribed_symbols if s not in self._sticky_extras]
        await self.subscribe(primary)
        logger.info(f"[DataRouter] Added {len(new)} sticky subscriptions")
        return len(new)

    async def remove_subscriptions(self, symbols: List[str]) -> int:
        """Drop symbols from the subscription (idempotent).

        Un-sticks each symbol so it can be removed cleanly.
        """
        drop = {s for s in symbols if s in self._sticky_extras or s in self._subscribed_symbols}
        if not drop:
            return 0
        for s in drop:
            self._sticky_extras.discard(s)
        primary = [s for s in self._subscribed_symbols if s not in self._sticky_extras and s not in drop]
        await self.subscribe(primary)
        logger.info(f"[DataRouter] Removed {len(drop)} subscriptions")
        return len(drop)

    # ── Required-feed watchdog ───────────────────────────────────────────────

    @staticmethod
    def _is_index_market_open(now: Optional[datetime] = None) -> bool:
        """True during NSE index regular session (Mon–Fri, 09:15–15:30 IST)."""
        if now is None:
            now = datetime.now(IST)
        now_ist = now.astimezone(IST)
        if now_ist.weekday() >= 5:  # Saturday / Sunday
            return False
        minute_of_day = now_ist.hour * 60 + now_ist.minute
        return (9 * 60 + 15) <= minute_of_day <= (15 * 60 + 30)

    def _missing_required_symbols(self) -> List[str]:
        subscribed = set(self._subscribed_symbols)
        return [s for s in self._required_symbols if s not in subscribed]

    def _stale_required_symbols(self) -> List[str]:
        """Required symbols whose last tick is older than the staleness budget."""
        now = datetime.now(timezone.utc)
        stale: List[str] = []
        for symbol in self._required_symbols:
            tick = self._tick_buffer.get(symbol)
            if tick is None or tick.timestamp is None:
                stale.append(symbol)
                continue
            age = (now - self._ensure_utc_timestamp(tick.timestamp)).total_seconds()
            if age > self._required_tick_stale_seconds:
                stale.append(symbol)
        return stale

    async def ensure_required_subscriptions(self) -> bool:
        """Re-subscribe if any required index symbol dropped off the WS.

        Returns True if a re-subscribe was issued. Idempotent and cheap when
        nothing is missing.
        """
        if not self._broker:
            return False
        if not self._missing_required_symbols():
            return False
        # Re-issue subscribe with the current primary (non-sticky) set;
        # subscribe() re-merges the required symbols and sticky extras.
        primary = [s for s in self._subscribed_symbols if s not in self._sticky_extras]
        await self.subscribe(primary)
        return True

    async def start_required_feed_watchdog(self) -> None:
        """Start the periodic required-feed guard (idempotent)."""
        if self._watchdog_task is not None and not self._watchdog_task.done():
            return
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._watchdog_task = self._loop.create_task(self._required_feed_watchdog_loop())
        logger.info(
            f"[DataRouter] Required-feed watchdog started "
            f"({len(self._required_symbols)} symbols, "
            f"{int(self._watchdog_interval_seconds)}s interval, "
            f"{int(self._required_tick_stale_seconds)}s stale budget)"
        )

    async def stop_required_feed_watchdog(self) -> None:
        task = self._watchdog_task
        self._watchdog_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _required_feed_watchdog_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._watchdog_interval_seconds)
                try:
                    # Re-subscribe anything that fell off the broker WS.
                    restored = await self.ensure_required_subscriptions()
                    if restored:
                        logger.warning(
                            "[DataRouter] Watchdog re-subscribed missing required index symbols."
                        )
                    # During market hours, force a reconnect if required
                    # streams have gone stale even though they're "subscribed".
                    if self._is_index_market_open() and self._ws_client is not None:
                        stale = self._stale_required_symbols()
                        if stale:
                            logger.warning(
                                f"[DataRouter] Watchdog: required symbols stale "
                                f"({len(stale)}/{len(self._required_symbols)}); forcing reconnect."
                            )
                            self._schedule_reconnect()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[DataRouter] Watchdog iteration failed: {exc}")
        except asyncio.CancelledError:
            pass

    async def unsubscribe(self):
        if self._ws_client:
            try:
                self._ws_client.close()
            except Exception:
                pass
        self._ws_client = None
        self._ws_broker = None

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
        # Notify the DataQualityAgent so strategy agents can short-circuit
        # on stale data. Producers are encouraged to feed this agent on every
        # observed tick or quote.
        try:
            from market_data.data_quality_agent import data_quality_agent

            broker_name = (
                getattr(self._broker, "broker_name", "unknown")
                if self._broker
                else "unknown"
            )
            data_quality_agent.record_tick(
                symbol=tick.symbol,
                source=f"{broker_name or 'unknown'}_tick",
                observed_at=tick.timestamp,
                last_value=float(
                    getattr(tick, "ltp", None)
                    or getattr(tick, "price", None)
                    or 0.0
                ) or None,
            )
        except Exception:  # noqa: BLE001
            pass
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
            # (1) pub/sub fan-out for live WS subscribers (fire-and-forget).
            await redis.publish(f"ticks:{tick.symbol}", payload)
            # (2) last-value hot-cache. Unlike pub/sub, this survives between
            # ticks so late subscribers and *other processes* (workers,
            # supervisors, the positions WS) can read the latest mark without
            # depending on the process-local _tick_buffer.
            await redis.set(
                f"{LATEST_TICK_KEY_PREFIX}{tick.symbol}",
                payload,
                ex=LATEST_TICK_TTL_SECONDS,
            )
        except Exception as e:
            logger.debug(f"[DataRouter] Redis publish error: {e}")

    def get_latest_tick(self, symbol: str) -> Optional[Tick]:
        return self._tick_buffer.get(symbol)

    def get_ltp(self, symbol: str) -> float:
        tick = self._tick_buffer.get(symbol)
        return tick.ltp if tick else 0.0

    async def get_live_mark(
        self,
        symbol: str,
        *,
        max_age_seconds: float = 30.0,
    ) -> Optional[float]:
        """Return the freshest LTP for ``symbol`` across process boundaries.

        Order: in-process ``_tick_buffer`` (instant) → Redis
        ``tick:{symbol}`` last-value key (cross-process). Returns ``None``
        when no tick is available or the freshest one is older than
        ``max_age_seconds`` (so a dead feed never marks positions with a
        stale price masquerading as live).
        """
        tick = self._tick_buffer.get(symbol)
        if tick is not None and getattr(tick, "ltp", None):
            ts = self._ensure_utc_timestamp(tick.timestamp)
            if (datetime.now(timezone.utc) - ts).total_seconds() <= max_age_seconds:
                return float(tick.ltp)
        try:
            redis = await get_redis()
            raw = await redis.get(f"{LATEST_TICK_KEY_PREFIX}{symbol}")
            if raw:
                data = json.loads(raw)
                ltp = data.get("ltp")
                ts_raw = data.get("timestamp")
                if ltp and ts_raw:
                    ts = datetime.fromisoformat(ts_raw)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - ts).total_seconds() <= max_age_seconds:
                        return float(ltp)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[DataRouter] get_live_mark redis read failed for {symbol}: {exc}")
        return None

    def get_status(self) -> dict[str, Any]:
        source_policy = dict(self._source_policy)
        if not source_policy:
            from market_data.source_policy import source_policy_snapshot

            source_policy = source_policy_snapshot()
        broker_name = getattr(self._broker, "broker_name", None) if self._broker else None
        mock_running = bool(self._mock_task and not self._mock_task.done())
        last_tick_times = [
            self._ensure_utc_timestamp(tick.timestamp)
            for tick in self._tick_buffer.values()
            if tick.timestamp is not None
        ]
        last_tick_at = max(last_tick_times) if last_tick_times else None
        last_tick_age_seconds = None
        if last_tick_at is not None:
            last_tick_age_seconds = max(
                0.0,
                (datetime.now(timezone.utc) - last_tick_at).total_seconds(),
            )
        callback_count = sum(len(callbacks) for callbacks in self._callbacks.values())

        if mock_running:
            mode = "mock"
        elif broker_name:
            mode = "broker"
        else:
            mode = "idle"

        ws_connected = bool(self._ws_client) and (
            last_tick_age_seconds is None or last_tick_age_seconds <= 30.0
        )
        if mode == "broker" and self._subscribed_symbols and not ws_connected:
            self._schedule_reconnect()

        return {
            "mode": mode,
            "broker": broker_name,
            "subscribed_symbols": list(self._subscribed_symbols),
            "subscribed_symbol_count": len(self._subscribed_symbols),
            "required_symbols": list(self._required_symbols),
            "watchdog_active": bool(self._watchdog_task and not self._watchdog_task.done()),
            "tick_buffer_size": len(self._tick_buffer),
            "callback_count": callback_count,
            "ws_connected": ws_connected,
            "ws_client_present": bool(self._ws_client),
            "mock_running": mock_running,
            "last_tick_at": last_tick_at.isoformat() if last_tick_at else None,
            "last_tick_age_seconds": last_tick_age_seconds,
            "source_policy": source_policy,
        }

    def _schedule_reconnect(self) -> None:
        if not self._loop or not self._loop.is_running():
            return
        if self._reconnect_task and not self._reconnect_task.done():
            return
        now = datetime.now(timezone.utc)
        if (
            self._last_reconnect_attempt_at is not None
            and now - self._last_reconnect_attempt_at < self._reconnect_backoff
        ):
            return
        self._last_reconnect_attempt_at = now
        self._reconnect_task = self._loop.create_task(self._reconnect_if_stale())

    async def _reconnect_if_stale(self) -> None:
        try:
            if not self._broker or not self._subscribed_symbols:
                return
            logger.warning("[DataRouter] Tick feed stale. Reconnecting websocket subscription.")
            await self.unsubscribe()
            broker_name = getattr(self._broker, "broker_name", "")
            if broker_name == "fyers":
                broker_symbols = [to_fyers_symbol(symbol) for symbol in self._subscribed_symbols]
            else:
                broker_symbols = [to_broker_symbol(symbol) for symbol in self._subscribed_symbols]
            self._ws_client = await self._broker.subscribe_websocket(broker_symbols, self._on_tick)
        except Exception as exc:
            logger.warning(f"[DataRouter] Websocket reconnect failed: {exc}")
        finally:
            self._reconnect_task = None

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
