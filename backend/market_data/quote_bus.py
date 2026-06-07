"""Quote bus — the low-latency coalescer for terminal-grade streaming.

This is the heart of the event-driven tick fan-out. It taps the live Fyers WS
feed via ``data_router.register_global_callback`` (called synchronously on every
tick, possibly from the broker SDK's own thread), accumulates the *latest* value
per symbol (last-write-wins), and flushes ONE compact multi-symbol frame to the
Redis ``quotes:bus`` channel every ``flush_interval`` (default 150 ms).

Why coalesce instead of forwarding every raw tick:
  - A naive per-tick fan-out to N browser sockets is O(N × ticks) sends — a fast
    tape (multiple symbols × several prints/sec) produces a frame storm that janks
    React. One coalesced frame fanned to N sockets is O(N) per window.
  - Worst-case added latency is exactly one flush window (≤150 ms) — set against
    a glass-to-glass budget of <500 ms this is the deliberate, bounded cost.

Honesty contract: every coalesced quote carries ``"c": 1`` so the UI can label a
150 ms-batched frame as such and never present it as a raw exchange print.

The frame shape (short keys to keep frames small; nulls dropped):
    {"q": [ {"s": sym, "p": ltp, "b": bid, "a": ask, "bz": bidqty, "az": askqty,
             "v": vol, "oi": oi, "t": epoch_ms, "c": 1}, ... ], "ts": epoch_ms}
A connect-time snapshot frame additionally carries ``"snap": 1`` and includes the
last-known value for every symbol (so a freshly-opened grid paints instantly
instead of waiting for the next tick on each symbol).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from loguru import logger

from db.redis_client import get_redis
from market_data import data_router as market_data_router

QUOTES_BUS_CHANNEL = "quotes:bus"
DEFAULT_FLUSH_INTERVAL_SECONDS = 0.15


def _epoch_ms(ts: Any) -> int:
    """Best-effort datetime/ISO -> epoch milliseconds."""
    try:
        if isinstance(ts, datetime):
            dt = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        if isinstance(ts, str) and ts:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
    except Exception:  # noqa: BLE001
        pass
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _compact(tick: Any) -> Dict[str, Any]:
    """Build the compact, null-dropped quote dict from a Tick.

    Index symbols carry no book/volume/OI; those keys are simply omitted (the
    frontend must not render a missing bid/ask as 0 for an index row).
    """
    out: Dict[str, Any] = {
        "s": getattr(tick, "symbol", None),
        "p": getattr(tick, "ltp", None),
        "t": _epoch_ms(getattr(tick, "timestamp", None)),
        "c": 1,
    }
    # Optional fields — emit only when meaningfully present.
    for key, attr in (
        ("o", "open"), ("h", "high"), ("l", "low"), ("pc", "close"),
        ("b", "bid"), ("a", "ask"), ("bz", "bid_qty"), ("az", "ask_qty"),
        ("v", "volume"), ("oi", "oi"),
    ):
        val = getattr(tick, attr, None)
        if val:
            out[key] = val
    return out


class QuoteBus:
    """Coalesces raw ticks into bounded multi-symbol frames on the Redis bus."""

    def __init__(self, flush_interval: float = DEFAULT_FLUSH_INTERVAL_SECONDS):
        self._flush_interval = flush_interval
        # Changed-since-last-flush (cleared each flush) and the full last-value
        # map (retained, for connect-time snapshot replay).
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._latest: Dict[str, Dict[str, Any]] = {}
        self._task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False
        self._registered = False

    # ── ingest (runs synchronously on the tick thread — must be cheap, no await) ──
    def _on_tick(self, tick: Any) -> None:
        try:
            sym = getattr(tick, "symbol", None)
            if not sym:
                return
            quote = _compact(tick)
            # Dict item assignment is atomic under the GIL — safe without a lock
            # even when this fires from the Fyers SDK's non-async thread.
            self._pending[sym] = quote
            self._latest[sym] = quote
        except Exception as exc:  # noqa: BLE001 — never let a bad tick kill the feed
            logger.debug(f"[quote_bus] on_tick error: {exc}")

    # ── lifecycle ──
    async def start(self) -> None:
        if self._running:
            return
        self._loop = asyncio.get_running_loop()
        if not self._registered:
            market_data_router.register_global_callback(self._on_tick)
            self._registered = True
        self._running = True
        self._task = self._loop.create_task(self._flush_loop())
        logger.info(f"[quote_bus] started (flush {int(self._flush_interval * 1000)}ms)")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    # ── flush (runs on the main event loop) ──
    async def _flush_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._flush_interval)
                if not self._pending:
                    continue
                # Swap out the pending batch atomically (rebind, don't mutate).
                batch = self._pending
                self._pending = {}
                frame = json.dumps({
                    "q": list(batch.values()),
                    "ts": int(datetime.now(timezone.utc).timestamp() * 1000),
                })
                try:
                    redis = await get_redis()
                    await redis.publish(QUOTES_BUS_CHANNEL, frame)
                except Exception as exc:  # noqa: BLE001 — Redis blip must not kill the loop
                    logger.debug(f"[quote_bus] publish error: {exc}")
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[quote_bus] flush loop error: {exc}")

    # ── connect-time snapshot (paint the grid instantly on a new WS client) ──
    def snapshot_frame(self) -> str:
        """A single frame with the last-known value for every known symbol."""
        return json.dumps({
            "q": list(self._latest.values()),
            "ts": int(datetime.now(timezone.utc).timestamp() * 1000),
            "snap": 1,
        })


# Module-level singleton (one coalescer per process).
quote_bus = QuoteBus()
