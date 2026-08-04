"""Data router — manages WebSocket feeds and publishes ticks to Redis."""
from __future__ import annotations
import asyncio
import json
import random
import threading
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from brokers.base import BrokerAdapter, Tick
from core.config import auction_of_book_symbols, settings
from core.trading_calendar import trading_calendar
from db.redis_client import get_redis
from market_data import index_band_guard
from market_data.symbols import (
    TICK_CAPTURE_APP_SYMBOLS,
    to_app_symbol,
    to_broker_symbol,
    to_fyers_symbol,
)
from market_data.tick_sanity import validate_structural_tick


IST = timezone(timedelta(hours=5, minutes=30))


# Redis last-value tick hot-cache. `tick:{symbol}` holds the most recent
# tick JSON so cross-process readers and late WS subscribers can fetch a
# live mark without the process-local _tick_buffer. TTL evicts symbols the
# feed has stopped sending (e.g. after an expiry roll or unsubscribe).
LATEST_TICK_KEY_PREFIX = "tick:"
LATEST_TICK_TTL_SECONDS = 300

# Plane-split subscription forwarding (2026-07-28). With LANESET=strategies the
# broker WS lives ONLY in the core plane, but the strategy lanes still call
# subscribe()/add_subscriptions() with the contracts they need — and before the
# split those calls were what put the symbols on the broker stream. Every
# downstream feed is subscription-driven (market_ticks rows via
# live_candle_store, tick:* last-value keys, ticks:* pub/sub), so silently
# dropping the calls starved flow-driven lanes: institutional-convergence read
# ZERO ticks all of 2026-07-28 (footprint source='insufficient_ticks',
# tick_age_ms=None) because nothing in the core plane ever subscribed its MCX
# contracts. The gated subscribe() now forwards wanted symbols to this Redis
# hash (symbol -> unix ts) and the core plane's feed watchdog absorbs fresh
# entries every ~30s. Entries older than the freshness window age out, so a
# contract stops being subscribed ~30 min after the last lane cycle that asked
# for it (e.g. after an expiry rollover).
WANTED_SYMBOLS_KEY = "laneset:wanted_symbols"
WANTED_SYMBOLS_FRESH_SECONDS = 1800.0
WANTED_SYMBOLS_TTL_SECONDS = 3600

# Redis P0 (2026-07-18): tick → Redis fan-out is COALESCED. The old path spawned
# one asyncio task per tick, each doing a publish + SET on its own pooled
# connection; during event-loop stalls thousands of tasks piled up and demanded
# the whole pool at once ("Too many connections", 5662/24h on 07-17). Now ticks
# land in a last-write-wins pending map and ONE pipeline per flush window writes
# every changed symbol (publish ticks:{sym} + SET tick:{sym}). 150ms matches the
# quote_bus coalesce window, so per-symbol subscribers and the hot-cache carry
# the same worst-case added latency the terminal tape already accepts — bounded,
# and far inside the 300s staleness budgets of every tick:* consumer.
TICK_FLUSH_INTERVAL_SECONDS = 0.15


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
        # Subscription replacement is a single-writer operation. Several
        # strategy/watchlist boot tasks add symbols concurrently; without a
        # lock they can each create a socket and overwrite ``_ws_client``,
        # leaving the older socket alive and still dispatching ticks.
        self._subscription_lock = asyncio.Lock()
        # Every socket callback captures the generation that created it.
        # Incrementing this before teardown immediately fences a socket whose
        # close hangs, so an abandoned SDK thread cannot contaminate the active
        # quote stream with stale topic-id mappings.
        self._ws_generation = 0
        self._subscribed_symbols: List[str] = []
        self._desired_primary_symbols: List[str] = []
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
        ] + [
            # Real order-flow book contracts (front-month futures / ATM options)
            # pinned onto the WS so their genuine bid/ask sizes + tape land in
            # market_ticks for the auction-intelligence order-flow path. Empty
            # unless AUCTION_OF_BOOK_SYMBOLS is configured → zero change to the
            # default index-only capture. (P1b, 2026-06-03.)
            to_app_symbol(s) for s in auction_of_book_symbols().values()
        ]
        self._callbacks: Dict[str, List[Callable[[Tick], None]]] = {}
        self._global_callbacks: List[Callable[[Tick], None]] = []
        self._tick_buffer: Dict[str, Tick] = {}  # latest tick per symbol
        # Redis P0: last-write-wins staging map for the coalesced Redis flusher
        # (see TICK_FLUSH_INTERVAL_SECONDS). Broker SDK callbacks stage ticks
        # from a NON-event-loop thread while _redis_flush_loop (on the loop
        # thread) swaps the map out to drain it. Individual dict writes are
        # atomic under the GIL, but the flusher's swap-then-iterate handoff is
        # NOT atomic against a concurrent insert — that race could drop a tick
        # or raise "dict changed size during iteration". _redis_ticks_lock makes
        # every stage/swap/clear on the map mutually exclusive so no write is
        # ever lost or interleaved with the swap. The lock is held only for the
        # O(1) map handoff, never across the Redis pipeline round-trip.
        self._redis_ticks_lock = threading.Lock()
        self._pending_redis_ticks: Dict[str, Tick] = {}
        self._redis_flush_task: Optional[asyncio.Task] = None
        self._depth_refs: Dict[str, int] = {}  # ref-counted DepthUpdate subscriptions
        self._tbt_client: Any = None  # Phase 6 — lazily-created TBT 50-level socket
        self._mock_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._source_policy: dict[str, Any] = {}
        self._reconnect_task: Optional[asyncio.Task] = None
        self._last_reconnect_attempt_at: Optional[datetime] = None
        # WS-1.3b adaptive reconnect: fast first retry, exponential backoff + jitter
        # on repeated failure (capped), reset on success. Replaces the old fixed 60s
        # blind window — transient drops now recover in seconds, while a sustained
        # broker outage is not hammered and parallel instances desynchronise.
        self._reconnect_base_seconds = 5.0
        self._reconnect_cap_seconds = 120.0
        self._reconnect_failures = 0
        # Required-feed watchdog: a periodic guard that re-subscribes any
        # required index symbol that dropped off the broker WS, and forces a
        # reconnect when a required symbol's last tick is older than the
        # staleness budget during market hours.
        self._watchdog_task: Optional[asyncio.Task] = None
        self._watchdog_interval_seconds = 30.0
        self._required_tick_stale_seconds = 45.0  # WS-1.3a: was 90s — faster dead-feed detection (RTH indices tick sub-second)
        # WS-1.4 (2026-07-24): post-resubscribe warm-up gate. A reconnect tears
        # the socket down, CLEARS _tick_buffer, then re-subscribes the FULL set
        # (~169 symbols live). A Fyers connect + that many subscriptions + first
        # frames can exceed the 30s watchdog interval, so _stale_required_symbols
        # reads the still-empty buffer of the BRAND-NEW socket as (N/N) stale and
        # the watchdog force-reconnects AGAIN before it can warm up — the
        # self-perpetuating storm (Thu 2026-07-23: 231 reconnects, feed dark
        # ~1h53m across the storm, ticks resumed the instant it stopped). This
        # gate makes _schedule_reconnect give a fresh resubscribe time to warm
        # its tick buffer before ANY trigger (watchdog / status poll / the new
        # socket-loss hook) can tear it down again — paced to at most one
        # reconnect per warm-up window during a sustained outage. Chosen > the
        # worst-case observed warm-up and > the 30s watchdog interval.
        self._last_resubscribe_at: Optional[datetime] = None
        self._post_resubscribe_warmup_seconds = 75.0

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
        # Phase-1 process split: the strategy plane must NEVER open the broker
        # WS — the core plane owns the ONE Fyers socket. This single public
        # chokepoint also covers add/remove_subscriptions, the required-feed
        # watchdog reopen, the auth-restore/token-callback resync
        # (api/routers/auth._sync_market_data_feed), and the option WS
        # subscription manager. get_live_mark's Redis tick:* fallback and all
        # REST paths stay fully live for the strategy plane. Inert when
        # LANESET=all (boots_core() is True).
        from core.laneset import boots_core

        if not boots_core():
            if not getattr(self, "_laneset_ws_gate_logged", False):
                self._laneset_ws_gate_logged = True
                logger.info(
                    "[DataRouter] LANESET=strategies — broker WS subscribe suppressed; "
                    f"wanted symbols forward to the core plane via Redis {WANTED_SYMBOLS_KEY}; "
                    "ticks/marks read back from Redis tick:* / market_ticks"
                )
            await self._forward_wanted_symbols(symbols)
            return
        async with self._subscription_lock:
            await self._subscribe_unlocked(symbols)

    async def _subscribe_unlocked(self, symbols: List[str]):
        if not self._broker:
            logger.warning("[DataRouter] No broker set — cannot subscribe")
            return
        # Merge required index streams + sticky extras (e.g. option contracts
        # pinned by the OptionWS subscription manager). Without this, a
        # spot-only resync from the broker-session refresh path would wipe a
        # previously-applied option subscription or drop a required index.
        self._desired_primary_symbols = list(dict.fromkeys(to_app_symbol(s) for s in symbols if s))
        full_set = self._compose_subscription_set(self._desired_primary_symbols)
        if not self._stream_window_open():
            await self._unsubscribe_unlocked()
            logger.debug("[DataRouter] Subscription deferred until the next NSE/MCX stream window")
            return
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
        await self._unsubscribe_unlocked()
        self._loop = asyncio.get_running_loop()
        self._subscribed_symbols = full_set
        generation = self._ws_generation

        def _on_tick_if_current(tick: Tick) -> None:
            if generation != self._ws_generation:
                return
            self._on_tick(tick)

        def _on_depth_if_current(depth: dict) -> None:
            if generation != self._ws_generation:
                return
            self._on_depth(depth)

        def _on_reconnect_if_current() -> None:
            if generation != self._ws_generation:
                return
            self._on_ws_reconnected()

        def _on_ws_lost_if_current() -> None:
            # Fires from the Fyers SDK's WS thread on a socket close/error.
            # This is the ONLY feed-recovery trigger during MCX-evening hours:
            # the required-feed watchdog's force-reconnect branch is gated to
            # NSE regular hours (09:15-15:30 IST) only, so a broker drop after
            # 15:30 previously had no recovery path and the feed went dark for
            # hours (Tue 2026-07-21, 7h14m silent). Fence to the socket
            # generation — a retired socket's late close must not reconnect the
            # live one — then hop to the loop thread to schedule the
            # router-owned reconnect (create_task is not thread-safe). The
            # scheduled reconnect is backoff- + warm-up-throttled, so repeated
            # close/error frames cannot storm.
            if generation != self._ws_generation:
                return
            loop = self._loop
            if loop is None or not loop.is_running():
                return
            loop.call_soon_threadsafe(self._schedule_reconnect)

        broker_name = getattr(self._broker, "broker_name", "")
        if broker_name == "fyers":
            broker_symbols = [to_fyers_symbol(symbol) for symbol in full_set]
        else:
            broker_symbols = [to_broker_symbol(symbol) for symbol in full_set]
        if broker_name == "fyers":
            # Fyers adapter accepts an optional depth callback for the 5-level
            # DepthUpdate ladder (subscribed incrementally per focused symbol),
            # plus a reconnect hook that invalidates the dedupe state below.
            self._ws_client = await self._broker.subscribe_websocket(
                broker_symbols,
                _on_tick_if_current,
                on_depth_callback=_on_depth_if_current,
                on_reconnect_callback=_on_reconnect_if_current,
                on_ws_lost=_on_ws_lost_if_current,
            )
        else:
            self._ws_client = await self._broker.subscribe_websocket(
                broker_symbols, _on_tick_if_current
            )
        self._ws_broker = self._broker
        # Re-arm any depth subscriptions that were active before a resubscribe.
        if self._depth_refs and broker_name == "fyers":
            for dsym in list(self._depth_refs):
                try:
                    self._ws_client.subscribe(symbols=[dsym], data_type="DepthUpdate")
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[DataRouter] depth re-arm failed for {dsym}: {exc}")
        # WS-1.4: mark when this (re)subscribe completed so _schedule_reconnect
        # can grant the fresh socket a warm-up window before it may be torn down
        # again — breaks the self-perpetuating reconnect storm.
        self._last_resubscribe_at = datetime.now(timezone.utc)
        logger.info(
            f"[DataRouter] Subscribed to {len(full_set)} symbols "
            f"(primary={len(symbols)} required={len(self._required_symbols)} sticky={len(sticky_extras)})"
        )

    def _on_ws_reconnected(self) -> None:
        """Called from the Fyers SDK's WS thread on every RE-connect.

        The SDK restores the socket but NOT the subscriptions. Clearing the
        dedupe state here makes the next periodic subscribe() (the broker-
        session refresh path calls it every ~20-30s) re-send the FULL set —
        required indices + sticky option extras — and re-arm depth refs via the
        normal subscribe flow. Without this, the dedupe gate skipped the
        resubscribe after the 2026-07-08 11:14 IST drop and the tape stayed
        blind for 4h16m. Attribute assignment is atomic (safe from the WS
        thread); loguru is thread-safe."""
        self._subscribed_symbols = []
        logger.warning(
            "[DataRouter] Fyers WS reconnected — subscription state cleared; "
            "full resubscribe will fire on the next subscribe() pass"
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
        primary = [s for s in self._subscribed_symbols if s not in self._sticky_extras] or list(self._desired_primary_symbols)
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
        primary = [s for s in self._subscribed_symbols if s not in self._sticky_extras and s not in drop] or list(self._desired_primary_symbols)
        await self.subscribe(primary)
        logger.info(f"[DataRouter] Removed {len(drop)} subscriptions")
        return len(drop)

    # ── Plane-split subscription forwarding ──────────────────────────────────

    async def _forward_wanted_symbols(self, symbols: List[str]) -> None:
        """LANESET=strategies: publish this plane's wanted symbols to Redis.

        Called from the gated subscribe() with whatever the lane asked for.
        Sticky extras ride along because add_subscriptions() pins new symbols
        there BEFORE routing through subscribe(), so an add-path call always
        reaches the hash even though the primary list it passes may not
        contain the new names. Best-effort: a Redis fault must never break a
        lane cycle.
        """
        wanted = [to_app_symbol(s) for s in symbols if s]
        wanted.extend(self._sticky_extras)
        wanted = [s for s in dict.fromkeys(wanted) if s]
        if not wanted:
            return
        try:
            redis = await get_redis()
            now = datetime.now(timezone.utc).timestamp()
            pipe = redis.pipeline(transaction=False)
            pipe.hset(WANTED_SYMBOLS_KEY, mapping={s: now for s in wanted})
            pipe.expire(WANTED_SYMBOLS_KEY, WANTED_SYMBOLS_TTL_SECONDS)
            await pipe.execute()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[DataRouter] wanted-symbol forward failed: {exc}")

    async def _absorb_forwarded_symbols(self) -> None:
        """Core plane: subscribe symbols the strategy plane asked for.

        Runs from the required-feed watchdog (~30s). Reads WANTED_SYMBOLS_KEY
        (symbol -> unix ts), deletes entries older than the freshness window
        (contract rolled / lane disabled), and add_subscriptions() the fresh
        ones not already streaming — which also pins them sticky so a broker
        auth resync cannot drop them.
        """
        try:
            redis = await get_redis()
            raw = await redis.hgetall(WANTED_SYMBOLS_KEY)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[DataRouter] wanted-symbol read failed: {exc}")
            return
        if not raw:
            return
        now = datetime.now(timezone.utc).timestamp()
        fresh: List[str] = []
        stale: List[str] = []
        for sym, ts in raw.items():
            name = sym.decode() if isinstance(sym, bytes) else str(sym)
            try:
                is_fresh = (now - float(ts)) <= WANTED_SYMBOLS_FRESH_SECONDS
            except (TypeError, ValueError):
                is_fresh = False
            (fresh if is_fresh else stale).append(name)
        if stale:
            try:
                await redis.hdel(WANTED_SYMBOLS_KEY, *stale)
            except Exception:  # noqa: BLE001
                pass
        new = [s for s in fresh if s not in self._subscribed_symbols]
        if not new:
            return
        added = await self.add_subscriptions(new)
        if added:
            logger.info(
                f"[DataRouter] Watchdog absorbed {added} strategy-plane symbol(s) "
                f"from {WANTED_SYMBOLS_KEY}: {', '.join(sorted(new)[:8])}"
                + ("…" if len(new) > 8 else "")
            )

    # ── Required-feed watchdog ───────────────────────────────────────────────

    @staticmethod
    def _is_index_market_open(now: Optional[datetime] = None) -> bool:
        """True while NSE index/derivative instruments stream (09:15–15:40 IST).

        Extended from 15:30 on 2026-08-03: NSE equity derivatives now trade
        until 15:40, and this gate governs the tick stream that feeds option
        marks. Leaving it at 15:30 silently dropped the final ten minutes of
        F&O trading — the closing prints — off the tape.
        """
        if now is None:
            now = datetime.now(IST)
        now_ist = now.astimezone(IST)
        if now_ist.weekday() >= 5:  # Saturday / Sunday
            return False
        minute_of_day = now_ist.hour * 60 + now_ist.minute
        return (9 * 60 + 15) <= minute_of_day <= (15 * 60 + 40)

    @staticmethod
    def _stream_window_open(now: Optional[datetime] = None) -> bool:
        """True from the 08:45 pre-open through either exchange's live session."""
        now_ist = (now or datetime.now(IST)).astimezone(IST)
        for exchange in ("NSE", "MCX"):
            if trading_calendar.is_exchange_open(exchange, now_ist):
                return True
            if (
                trading_calendar.has_exchange_session(exchange, now_ist.date())
                and time(8, 45) <= now_ist.time().replace(tzinfo=None) < time(9, 15)
            ):
                return True
        return False

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
        if not self._broker or not self._stream_window_open():
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
                    if not self._stream_window_open():
                        if self._ws_client is not None:
                            await self.unsubscribe()
                            logger.info("[DataRouter] Closed websocket outside NSE/MCX stream windows")
                        continue
                    if self._ws_client is None and self._broker is not None:
                        await self.subscribe(list(self._desired_primary_symbols))
                        continue
                    # Re-subscribe anything that fell off the broker WS.
                    restored = await self.ensure_required_subscriptions()
                    if restored:
                        logger.warning(
                            "[DataRouter] Watchdog re-subscribed missing required index symbols."
                        )
                    # Plane split: absorb the strategy plane's wanted symbols
                    # (its subscribe() is gated and forwards here via Redis).
                    await self._absorb_forwarded_symbols()
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
                            # WS-0.3a — alert once per stale episode (edge-triggered;
                            # reset on recovery). Flows via the audit→Telegram bridge.
                            if not getattr(self, "_stale_alerted", False):
                                self._stale_alerted = True
                                try:
                                    from agentic_rag.audit_agent import record_audit_event

                                    await record_audit_event(
                                        market="system",
                                        strategy_key="market_data_feed",
                                        event_type="feed_stale",
                                        actor="data_router_watchdog",
                                        severity="warning",
                                        message=(
                                            f"{len(stale)}/{len(self._required_symbols)} required index "
                                            f"feed(s) stale >{int(self._required_tick_stale_seconds)}s: "
                                            f"{', '.join(stale[:6])}"
                                        ),
                                    )
                                except Exception:
                                    pass
                        elif getattr(self, "_stale_alerted", False):
                            # Feed recovered — reset so the next episode re-alerts.
                            self._stale_alerted = False
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[DataRouter] Watchdog iteration failed: {exc}")
        except asyncio.CancelledError:
            pass

    async def unsubscribe(self):
        async with self._subscription_lock:
            await self._unsubscribe_unlocked()

    async def _unsubscribe_unlocked(self):
        # Fence callbacks before attempting a potentially blocking SDK close.
        self._ws_generation += 1
        retired_symbols = list(self._subscribed_symbols)
        # A quote accepted by the old generation must not remain visible after
        # replacement. This matters when the broker sent one cross-wired
        # snapshot before the socket was retired and the real instrument is
        # subsequently quiet: without clearing both caches, that bad mark stays
        # on screen for the full Redis TTL.
        self._tick_buffer.clear()
        # Also drop any staged-but-unflushed ticks: a retired symbol's pending
        # entry must not resurrect its tick:{symbol} key after the delete below.
        # Clear under the staging lock so we don't race a producer insert or the
        # flusher's swap.
        with self._redis_ticks_lock:
            self._pending_redis_ticks.clear()
        if retired_symbols:
            try:
                redis = await get_redis()
                await redis.delete(
                    *(f"{LATEST_TICK_KEY_PREFIX}{symbol}" for symbol in retired_symbols)
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[DataRouter] retired tick-cache cleanup failed: {exc}")
        client = self._ws_client
        self._ws_client = None
        self._ws_broker = None
        if client is None:
            return
        # The Fyers SDK socket method is close_connection() — there is NO .close()
        # (the old self._ws_client.close() raised AttributeError that the bare
        # except swallowed, so the socket was NEVER torn down and its
        # reconnect=True loop lived on as a ZOMBIE firing on_error on every drop —
        # this is the 1,683-error WS "flap" storm, multiple sockets accumulating
        # one-per-resubscribe). close_connection() sets restart_flag=False (the
        # reconnect loop is guarded by `if self.restart_flag`) then joins the
        # socket threads — those joins can BLOCK on a wedged socket (the
        # 2026-06-11 process freeze), so run it OFF the event loop with a deadline.
        def _teardown() -> None:
            try:
                # Stop the SDK reconnect loop FIRST, so even if the close hangs and
                # we abandon the thread, it can never reconnect (no zombie).
                setattr(client, "restart_flag", False)
            except Exception:
                pass
            close_fn = getattr(client, "close_connection", None) or getattr(client, "close", None)
            if close_fn is not None:
                close_fn()

        try:
            await asyncio.wait_for(asyncio.to_thread(_teardown), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("[DataRouter] ws close_connection exceeded 5s; abandoned (reconnect already stopped)")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[DataRouter] ws teardown error (ignored): {exc}")

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
        reject_reason = validate_structural_tick(tick)
        if reject_reason:
            logger.debug(f"[DataRouter] Dropped corrupt tick for {tick.symbol}: {reject_reason}")
            return
        # Cross-symbol contamination guard (WS-0.1b): a misrouted frame carries
        # another index's whole-frame OHLC under this symbol. Drop it here — the
        # single choke point upstream of the tick buffer (live marks), the Redis
        # hot-cache, every callback (market_ticks / MP builder) and the sector
        # tape — so a contaminated 57.8k print can never mark a 24k index.
        if index_band_guard.is_guarded(tick.symbol) and not index_band_guard.passes(
            tick.symbol, getattr(tick, "ltp", 0.0)
        ):
            logger.debug(
                f"[DataRouter] Dropped out-of-band tick for {tick.symbol}: ltp={getattr(tick, 'ltp', None)}"
            )
            return
        self._tick_buffer[tick.symbol] = tick
        # WS-0.2 instrumentation — tick throughput + age at ingest. Fully
        # isolated: a metrics fault must never disturb the tick hot path.
        try:
            from core.metrics import observe_tick

            _src = getattr(self._broker, "broker_name", None) or "unknown"
            _age = (
                (datetime.now(timezone.utc) - tick.timestamp).total_seconds()
                if tick.timestamp is not None
                else None
            )
            observe_tick(f"{_src}_tick", _age)
        except Exception:
            pass
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
        # Redis P0: NO per-tick task/publish — stage for the coalesced flusher.
        # Fyers websocket callbacks can arrive on a non-async thread; hold the
        # staging lock so this insert can never interleave with the flusher's
        # swap (last-write-wins per symbol within the 150ms window). The flusher
        # task is only ever (re)started on the event-loop thread.
        with self._redis_ticks_lock:
            self._pending_redis_ticks[tick.symbol] = tick
        if self._redis_flush_task is None or self._redis_flush_task.done():
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop and running_loop is self._loop:
                self._ensure_redis_flusher()
            elif self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._ensure_redis_flusher)

    def _ensure_redis_flusher(self) -> None:
        """Start the coalesced Redis tick flusher (event-loop thread only)."""
        if self._redis_flush_task is not None and not self._redis_flush_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._redis_flush_task = loop.create_task(self._redis_flush_loop())

    async def _redis_flush_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(TICK_FLUSH_INTERVAL_SECONDS)
                await self._flush_pending_ticks()
        except asyncio.CancelledError:
            pass

    async def _flush_pending_ticks(self) -> None:
        """Write every pending symbol to Redis in ONE pipeline round-trip.

        Per symbol (latest value wins within the window):
          (1) publish ticks:{symbol} — per-symbol pub/sub fan-out for live WS
              subscribers (payload schema unchanged from the per-tick era).
          (2) SET tick:{symbol} — last-value hot-cache. Unlike pub/sub this
              survives between ticks so late subscribers and *other processes*
              (workers, supervisors, the positions WS) can read the latest mark
              without the process-local _tick_buffer.
        """
        # Swap out the batch under the staging lock so a producer insert on the
        # broker-callback thread cannot land between the read and the rebind
        # (which would silently drop that tick) or mutate the map we are about
        # to iterate. Ticks arriving AFTER the swap go to the fresh map and ride
        # the next window; the lock is released before the Redis round-trip.
        with self._redis_ticks_lock:
            if not self._pending_redis_ticks:
                return
            batch = self._pending_redis_ticks
            self._pending_redis_ticks = {}
        try:
            redis = await get_redis()
            pipe = redis.pipeline(transaction=False)
            for symbol, tick in batch.items():
                payload = self._tick_payload(tick)
                pipe.publish(f"ticks:{symbol}", payload)
                pipe.set(
                    f"{LATEST_TICK_KEY_PREFIX}{symbol}",
                    payload,
                    ex=LATEST_TICK_TTL_SECONDS,
                )
            await pipe.execute()
        except Exception as e:
            logger.debug(f"[DataRouter] Redis tick flush error: {e}")

    def _tick_payload(self, tick: Tick) -> str:
        """Serialize a tick to the wire/cache JSON (schema is a consumer contract)."""
        timestamp = self._ensure_utc_timestamp(tick.timestamp)
        return json.dumps({
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

    # ── Depth (5-level DOM ladder) ───────────────────────────────────────────
    def _on_depth(self, depth: dict):
        """Depth callback (fires on the broker SDK thread) → schedule publish."""
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop and running_loop is self._loop:
            asyncio.create_task(self._publish_depth(depth))
        elif self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._publish_depth(depth), self._loop)

    async def _publish_depth(self, depth: dict):
        try:
            symbol = depth.get("symbol")
            if not symbol:
                return
            redis = await get_redis()
            ts = self._ensure_utc_timestamp(depth.get("timestamp"))
            payload = json.dumps({
                "symbol": symbol,
                "bids": depth.get("bids", []),
                "asks": depth.get("asks", []),
                "tbq": depth.get("tbq", 0),
                "tsq": depth.get("tsq", 0),
                "timestamp": ts.isoformat(),
            })
            await redis.publish(f"depth:{symbol}", payload)
        except Exception as e:
            logger.debug(f"[DataRouter] Redis depth publish error: {e}")

    async def subscribe_depth(self, broker_symbol: str) -> None:
        """Ref-counted DepthUpdate subscription on the live WS client.

        ``broker_symbol`` is the broker-native key (e.g. ``NSE:NIFTY...CE``) — the
        same key the tick feed publishes, so the depth:{symbol} channel aligns.
        """
        if not broker_symbol or self._ws_client is None:
            return
        n = self._depth_refs.get(broker_symbol, 0)
        self._depth_refs[broker_symbol] = n + 1
        if n == 0:
            # Phase 6: route through the TBT 50-level socket when enabled +
            # entitled; fall back to the 5-level DataSocket DepthUpdate on any error.
            if settings.FYERS_TBT_DEPTH_ENABLED and self._is_fyers_broker():
                if await self._tbt_subscribe(broker_symbol):
                    return
            try:
                sub = getattr(self._ws_client, "subscribe", None)
                if callable(sub):
                    sub(symbols=[broker_symbol], data_type="DepthUpdate")
                    logger.info(f"[DataRouter] depth subscribed (5-level): {broker_symbol}")
            except Exception as e:
                logger.debug(f"[DataRouter] depth subscribe failed for {broker_symbol}: {e}")

    async def unsubscribe_depth(self, broker_symbol: str) -> None:
        if not broker_symbol:
            return
        n = self._depth_refs.get(broker_symbol, 0)
        if n <= 1:
            self._depth_refs.pop(broker_symbol, None)
            if settings.FYERS_TBT_DEPTH_ENABLED and self._tbt_client is not None:
                try:
                    self._broker.tbt_unsubscribe(self._tbt_client, [broker_symbol])
                except Exception as e:
                    logger.debug(f"[DataRouter] TBT depth unsubscribe failed for {broker_symbol}: {e}")
            if self._ws_client is not None:
                try:
                    unsub = getattr(self._ws_client, "unsubscribe", None)
                    if callable(unsub):
                        unsub(symbols=[broker_symbol], data_type="DepthUpdate")
                except Exception as e:
                    logger.debug(f"[DataRouter] depth unsubscribe failed for {broker_symbol}: {e}")
        else:
            self._depth_refs[broker_symbol] = n - 1

    def _is_fyers_broker(self) -> bool:
        return getattr(self._broker, "broker_name", "") == "fyers"

    async def _tbt_subscribe(self, broker_symbol: str) -> bool:
        """Subscribe a symbol on the TBT 50-level socket (lazily created). Returns
        True on success; False to let the caller fall back to the 5-level path."""
        try:
            if self._tbt_client is None:
                self._tbt_client = await self._broker.subscribe_tbt_websocket(
                    [broker_symbol], self._on_depth
                )
                logger.info(f"[DataRouter] TBT depth socket opened; subscribed {broker_symbol}")
            else:
                self._broker.tbt_subscribe(self._tbt_client, [broker_symbol])
                logger.info(f"[DataRouter] TBT depth subscribed: {broker_symbol}")
            return True
        except Exception as e:
            logger.warning(f"[DataRouter] TBT depth unavailable for {broker_symbol} ({e}); using 5-level")
            self._tbt_client = None
            return False

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
        if (
            mode == "broker"
            and self._subscribed_symbols
            and not ws_connected
            and self._stream_window_open()
        ):
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

    def _current_reconnect_backoff(self) -> timedelta:
        """WS-1.3b: exponential backoff (base·2^failures, capped) + up to 50% jitter.
        ``_reconnect_failures`` is 0 after a success, so the first retry waits only
        ~base seconds; it grows only while reconnects keep failing."""
        raw = min(
            self._reconnect_base_seconds * (2 ** self._reconnect_failures),
            self._reconnect_cap_seconds,
        )
        return timedelta(seconds=raw + raw * 0.5 * random.random())

    def _schedule_reconnect(self) -> None:
        if not self._loop or not self._loop.is_running():
            return
        if self._reconnect_task and not self._reconnect_task.done():
            return
        now = datetime.now(timezone.utc)
        # WS-1.4: don't tear a freshly-resubscribed socket down before it has had
        # time to warm its tick buffer — otherwise the staleness check reads the
        # (still-empty) new buffer as stale and the reconnect self-perpetuates
        # into a storm (Thu 2026-07-23, 231 reconnects). During a genuine
        # sustained outage each attempt refreshes _last_resubscribe_at, so this
        # paces reconnects to one per warm-up window rather than one per watchdog
        # tick — it de-storms without ever permanently blocking recovery.
        if (
            self._last_resubscribe_at is not None
            and (now - self._last_resubscribe_at).total_seconds()
            < self._post_resubscribe_warmup_seconds
        ):
            return
        if (
            self._last_reconnect_attempt_at is not None
            and now - self._last_reconnect_attempt_at < self._current_reconnect_backoff()
        ):
            return
        self._last_reconnect_attempt_at = now
        self._reconnect_task = self._loop.create_task(self._reconnect_if_stale())

    async def _reconnect_if_stale(self) -> None:
        try:
            if not self._broker or not self._subscribed_symbols or not self._stream_window_open():
                return
            logger.warning("[DataRouter] Tick feed stale. Reconnecting websocket subscription.")
            # Use the normal serialized replacement path so reconnects get the
            # same stale-callback fence, depth hooks and broker bookkeeping as
            # watchlist-driven subscription changes.
            self._subscribed_symbols = []
            await self.subscribe(list(self._desired_primary_symbols))
            self._reconnect_failures = 0  # success → reset backoff to fast-retry
        except Exception as exc:
            self._reconnect_failures = min(self._reconnect_failures + 1, 8)  # escalate (cap exponent)
            logger.warning(
                f"[DataRouter] Websocket reconnect failed (failure streak={self._reconnect_failures}): {exc}"
            )
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
