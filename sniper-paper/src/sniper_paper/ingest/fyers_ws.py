"""Fyers WebSocket ingest.

Subscribes to the configured futures symbols (NIFTY, SENSEX, CRUDE), forwards
every tick into:
  - a Redis pub/sub channel (for live feature service fan-out)
  - a TimescaleDB insert queue (for durable storage)

The Fyers SDK is callback-based, not asyncio-native. We bridge with a queue.
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis

from sniper_paper.common.logging import get_logger
from sniper_paper.common.settings import Secrets, Settings

log = get_logger(__name__)


class FyersIngest:
    """Spans a background thread for the Fyers SDK + an asyncio consumer loop."""

    def __init__(self, settings: Settings, secrets: Secrets):
        self.settings = settings
        self.secrets = secrets
        self._raw_queue: queue.Queue[dict] = queue.Queue(maxsize=10000)
        self._fyers_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._redis: aioredis.Redis | None = None

    # ─── Bridge: SDK thread → asyncio queue ────────────────────────
    def _on_message(self, message: Any) -> None:
        """Called by the Fyers SDK in its own thread."""
        try:
            self._raw_queue.put_nowait({"raw": message, "received_at": datetime.now(timezone.utc)})
        except queue.Full:
            log.warning("Tick queue full, dropping message")

    def _on_open(self) -> None:
        symbols = [i.near_month_symbol for i in self.settings.instruments]
        log.info("Fyers WS open; subscribing to %s", symbols)
        self._socket.subscribe(symbols=symbols, data_type="SymbolUpdate")

    def _on_close(self, message: Any) -> None:
        log.warning("Fyers WS closed: %s", message)

    def _on_error(self, message: Any) -> None:
        log.error("Fyers WS error: %s", message)

    def _run_fyers_socket(self) -> None:
        try:
            from fyers_apiv3.FyersWebsocket import data_ws  # noqa
        except ImportError:
            log.error("fyers-apiv3 not installed; ingest will not produce real ticks")
            return

        access_token = self.secrets.fyers.get("access_token") or ""
        if not access_token:
            log.error("FYERS access_token empty; cannot connect WS. Populate secrets.yaml.")
            return

        self._socket = data_ws.FyersDataSocket(
            access_token=access_token,
            log_path="",
            litemode=self.settings.fyers.websocket_lite_mode,
            write_to_file=False,
            reconnect=True,
            on_connect=self._on_open,
            on_close=self._on_close,
            on_error=self._on_error,
            on_message=self._on_message,
        )
        self._socket.connect()
        # connect() is blocking-ish; thread stays alive until socket closes.

    # ─── Public: start / stop ──────────────────────────────────────
    async def start(self) -> None:
        self._redis = aioredis.from_url(self.settings.redis_url(), decode_responses=False)
        self._fyers_thread = threading.Thread(target=self._run_fyers_socket, daemon=True)
        self._fyers_thread.start()
        log.info("Fyers ingest started")

    async def stop(self) -> None:
        self._stop.set()
        if self._redis is not None:
            await self._redis.close()

    # ─── Public: async consumer for downstream ─────────────────────
    async def stream(self):
        """Async iterator yielding normalised tick dicts."""
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                raw = await loop.run_in_executor(None, self._raw_queue.get, True, 1.0)
            except queue.Empty:
                continue
            tick = self._normalise(raw)
            if tick is None:
                continue
            yield tick

    def _normalise(self, raw: dict) -> dict | None:
        """Map a Fyers message to our internal tick schema."""
        msg = raw.get("raw") or {}
        # Fyers SymbolUpdate carries fields like 'symbol', 'ltp', 'last_traded_qty',
        # 'bid_price1'..'bid_price5', 'ask_price1'..'ask_price5', 'bid_size1'..,
        # 'ask_size1'.., 'OI'. Field names occasionally vary; be tolerant.
        symbol = msg.get("symbol") or msg.get("Symbol")
        ltp = msg.get("ltp") or msg.get("LTP") or msg.get("last_price")
        if not symbol or ltp is None:
            return None
        try:
            instrument = self.settings.instrument_by_symbol(symbol).name
        except KeyError:
            return None

        return {
            "ts": raw["received_at"],
            "symbol": symbol,
            "instrument": instrument,
            "ltp": float(ltp),
            "last_qty": int(msg.get("last_traded_qty", msg.get("ltq", 0)) or 0),
            "bid_px_1": _f(msg.get("bid_price1") or msg.get("bid_price")),
            "ask_px_1": _f(msg.get("ask_price1") or msg.get("ask_price")),
            "bid_qty_1": _i(msg.get("bid_size1") or msg.get("bid_qty")),
            "ask_qty_1": _i(msg.get("ask_size1") or msg.get("ask_qty")),
            "oi": _i(msg.get("OI") or msg.get("oi")),
            "raw": msg,
        }

    # ─── Public: publish a tick to Redis ───────────────────────────
    async def publish(self, tick: dict) -> None:
        if self._redis is None:
            return
        payload = json.dumps({**tick, "ts": tick["ts"].isoformat(), "raw": None}).encode()
        await self._redis.publish(self.settings.redis.tick_channel, payload)


def _f(x) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _i(x) -> int | None:
    try:
        return int(x) if x is not None else None
    except (TypeError, ValueError):
        return None
