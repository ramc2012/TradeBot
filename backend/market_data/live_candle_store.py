"""Persist live ticks and aggregate them into reusable intraday candles."""
from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median
from typing import Any, Optional

from loguru import logger
from sqlalchemy import text

from brokers.base import Tick
from db.database import AsyncSessionLocal
from market_data.candle_timeframes import CANDLE_INTERVALS_MINUTES, floor_timestamp
from market_data.commodity_contract_specs import extract_commodity_root
from market_data.symbols import DISPLAY_NAMES
from market_data.tick_sanity import validate_structural_tick
from market_data.upstox_commodity import resolve_upstox_mcx_future

try:  # WS-0.1a — reject counter; must never block ingest
    from core.metrics import record_reject as _record_reject
except Exception:  # pragma: no cover
    def _record_reject(*_a, **_k) -> None:  # type: ignore[misc]
        ...


def _option_row_passes_no_arb(row: dict[str, Any]) -> bool:
    """Ingest-time no-arbitrage guard for option candles. Rejects impossible
    prices at the SOURCE: a put can never be worth more than its strike, a call
    never more than spot. Fyers serves these for post-corporate-action zombie
    strikes — the INDIANB 820 PE @ 1298.8 (2026-06-11, > strike) that booked a
    +Rs19L phantom entered through exactly this live-candle path. The existing
    analysis/safe_candles guard is downstream (backtest load); this stops the bad
    row before it is ever persisted (and thus before any live mark reads it).
    Returns True (keep) on any parse ambiguity — never drops a legitimate row."""
    try:
        px = max(float(row.get("close") or 0.0), float(row.get("high") or 0.0))
        strike = float(row.get("strike") or 0.0)
        spot = float(row.get("underlying_price") or 0.0)
    except (TypeError, ValueError):
        return True
    otype = str(row.get("option_type") or "").upper()
    if px <= 0:
        return True  # zero/empty is a different validation concern, not no-arb
    if otype.startswith("P") and strike > 0 and px > strike * 1.02:
        return False
    if otype.startswith("C") and spot > 0 and px > spot * 1.05:
        return False
    return True


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
    # Broker tick volume (Fyers vol_traded_today / Upstox vtt) is session-
    # CUMULATIVE. Bars must store the per-bar delta or live rows clobber
    # per-bar broker-history rows at the same key with day totals.
    volume_open: int = 0
    cum_volume_last: int = 0
    dirty: bool = True


class LiveCandleStore:
    FLUSH_INTERVAL_SECONDS = 5.0
    BATCH_SIZE = 250
    # Ingest must never die or grow without bound when Postgres blips: the
    # queue is bounded (overflow drops oldest-first with a counter), failed
    # batches are retained for retry up to MAX_PENDING_TICKS, and flush
    # failures back off instead of killing the worker.
    QUEUE_MAXSIZE = 50_000
    MAX_PENDING_TICKS = 20_000
    FLUSH_BACKOFF_MAX_SECONDS = 30.0
    METADATA_NEGATIVE_TTL_SECONDS = 600.0
    # WS-0.1a ingest validation — magnitude guard for index spot (where the
    # documented cross-symbol contamination lands, e.g. NIFTY prints at 53k/75k vs
    # ~23k spot). The reference is a rolling median (robust to a minority of bad
    # prints); ±50% is far outside any legit index intraday move yet catches the
    # documented 2.3-3.3x contamination with margin. Options are exempt (premiums
    # legitimately move multiples). Conservative by design — watch
    # nomad_ingest_rejected_total{reason} before tightening.
    SPOT_DEVIATION_THRESHOLD = 0.5
    SPOT_REF_WINDOW = 30
    SPOT_REF_WARMUP = 5

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None
        self._queue: asyncio.Queue[Tick] = asyncio.Queue(maxsize=self.QUEUE_MAXSIZE)
        self._tick_batch: list[Tick] = []
        self._buckets: dict[tuple[str, str], _CandleBucket] = {}
        self._metadata_cache: dict[str, Optional[dict[str, Any]]] = {}
        self._metadata_none_at: dict[str, float] = {}
        self._latest_spot: dict[str, float] = {}
        self._spot_window: dict[str, deque] = {}  # WS-0.1a rolling spot reference
        # Persistence health — a dead flush path must be a visible fact.
        self._ticks_persisted = 0
        self._ticks_dropped = 0
        self._candle_rows_upserted = 0
        self._flush_failures = 0
        self._consecutive_flush_failures = 0
        self._last_flush_ok_at: Optional[datetime] = None
        self._last_flush_error: Optional[str] = None
        self._flush_retry_not_before = 0.0

    def status(self) -> dict[str, Any]:
        return {
            "worker_alive": bool(self._task and not self._task.done()),
            "ticks_persisted": self._ticks_persisted,
            "ticks_dropped": self._ticks_dropped,
            "candle_rows_upserted": self._candle_rows_upserted,
            "flush_failures": self._flush_failures,
            "consecutive_flush_failures": self._consecutive_flush_failures,
            "last_flush_ok_at": self._last_flush_ok_at.isoformat() if self._last_flush_ok_at else None,
            "last_flush_error": self._last_flush_error,
            "pending_queue": self._queue.qsize(),
            "pending_batch": len(self._tick_batch),
            "dirty_buckets": sum(1 for b in self._buckets.values() if b.dirty),
        }

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

    def _validate_tick(self, tick: Tick) -> bool:
        """WS-0.1a — drop corrupt ticks at the ingest boundary. Returns True to
        accept, False to drop.

        Structural checks apply to every symbol; a rolling-median magnitude guard
        applies to index spot only (where the documented cross-symbol contamination
        lands). Rejects increment nomad_ingest_rejected_total{reason} so the
        false-drop rate is observable before tightening.
        """
        try:
            ltp = float(tick.ltp)
        except (TypeError, ValueError):
            _record_reject("non_numeric_price")
            return False

        if not math.isfinite(ltp):
            _record_reject("nonpositive_price")
            return False

        reject_reason = validate_structural_tick(tick)
        if reject_reason:
            _record_reject(reject_reason)
            return False

        # Magnitude guard — index spot only (options legitimately move multiples).
        if tick.symbol in DISPLAY_NAMES:
            window = self._spot_window.setdefault(
                tick.symbol, deque(maxlen=self.SPOT_REF_WINDOW)
            )
            window.append(ltp)
            if len(window) >= self.SPOT_REF_WARMUP:
                ref = median(window)
                if ref > 0 and abs(ltp - ref) / ref > self.SPOT_DEVIATION_THRESHOLD:
                    _record_reject("spot_magnitude")
                    return False
        return True

    def on_tick(self, tick: Tick) -> None:
        if not self._loop or not self._loop.is_running():
            return
        if tick.timestamp is None:
            tick.timestamp = datetime.now(timezone.utc)
        if tick.timestamp.tzinfo is None:
            tick.timestamp = tick.timestamp.replace(tzinfo=timezone.utc)
        else:
            tick.timestamp = tick.timestamp.astimezone(timezone.utc)

        # WS-0.1a — reject corrupt prints at the boundary, before they pollute the
        # tick log or the candle buckets.
        if not self._validate_tick(tick):
            return

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop and running_loop is self._loop:
            self._enqueue_nowait(tick)
        else:
            # Never block the WS callback thread on a full queue.
            self._loop.call_soon_threadsafe(self._enqueue_nowait, tick)

    def _enqueue_nowait(self, tick: Tick) -> None:
        try:
            self._queue.put_nowait(tick)
        except asyncio.QueueFull:
            # Drop oldest-first so the freshest prices win when the flush path
            # is down; count it so the loss is observable.
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(tick)
            except asyncio.QueueFull:
                pass
            self._ticks_dropped += 1
            _record_reject("candle_store_queue_full")

    async def _worker(self) -> None:
        # One transient Postgres error must never kill tick/candle persistence
        # for the rest of the session — every flush is guarded and failures
        # back off while ticks keep buffering (bounded).
        while True:
            try:
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
                try:
                    await self._drain_queue()
                    await self._flush_pending(force=True)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[LiveCandleStore] final flush on shutdown failed: {exc}")
                raise
            except Exception as exc:  # noqa: BLE001
                # Belt-and-braces: _flush_pending contains its own guards, so
                # anything landing here is unexpected — log and keep serving.
                logger.error(f"[LiveCandleStore] worker iteration failed (continuing): {exc}")

    async def _drain_queue(self) -> None:
        while not self._queue.empty():
            tick = await self._queue.get()
            self._tick_batch.append(tick)
            self._update_buckets(tick)

    def _update_buckets(self, tick: Tick) -> None:
        timestamp = tick.timestamp or datetime.now(timezone.utc)
        if tick.symbol in DISPLAY_NAMES:
            self._latest_spot[DISPLAY_NAMES[tick.symbol]] = float(tick.ltp)

        cum_volume = int(tick.volume or 0)
        for minutes, interval in CANDLE_INTERVALS_MINUTES.items():
            bucket_start = floor_timestamp(timestamp, minutes)
            key = (tick.symbol, interval)
            bucket = self._buckets.get(key)
            if bucket is None or bucket.bucket_start != bucket_start:
                prev = bucket
                # Broker volume is session-cumulative: baseline the new bar at
                # the prior bar's last cumulative reading so bucket.volume is a
                # per-bar delta (mirrors chain_candle_builder). A lower reading
                # (new session / broker reset / reconnect) re-baselines.
                if prev is not None and 0 < prev.cum_volume_last <= cum_volume:
                    volume_open = prev.cum_volume_last
                else:
                    volume_open = cum_volume
                bucket = _CandleBucket(
                    symbol=tick.symbol,
                    interval=interval,
                    bucket_start=bucket_start,
                    open=float(tick.ltp),
                    high=float(tick.ltp),
                    low=float(tick.ltp),
                    close=float(tick.ltp),
                    volume=max(0, cum_volume - volume_open),
                    oi=int(tick.oi or 0),
                    updated_at=timestamp,
                    volume_open=volume_open,
                    cum_volume_last=cum_volume,
                )
                self._buckets[key] = bucket
                continue

            price = float(tick.ltp)
            bucket.high = max(bucket.high, price)
            bucket.low = min(bucket.low, price)
            bucket.close = price
            if cum_volume > 0:
                if cum_volume < bucket.volume_open:
                    # Cumulative counter went backwards mid-bar — re-baseline.
                    bucket.volume_open = cum_volume
                bucket.cum_volume_last = cum_volume
                bucket.volume = max(0, cum_volume - bucket.volume_open)
            bucket.oi = int(tick.oi or 0)
            bucket.updated_at = timestamp
            bucket.dirty = True

    async def _flush_pending(self, *, force: bool = False) -> None:
        # Failure backoff: while the DB is unhealthy, skip flush attempts (data
        # keeps buffering, bounded) instead of hammering it every 5s.
        if not force and time.monotonic() < self._flush_retry_not_before:
            self._trim_pending()
            return

        ok = True
        try:
            await self._persist_ticks()
        except Exception as exc:  # noqa: BLE001
            ok = False
            self._note_flush_failure("ticks", exc)
        try:
            await self._persist_candles(force=force)
        except Exception as exc:  # noqa: BLE001
            ok = False
            self._note_flush_failure("candles", exc)

        if ok:
            self._consecutive_flush_failures = 0
            self._flush_retry_not_before = 0.0
            self._last_flush_ok_at = datetime.now(timezone.utc)
            self._last_flush_error = None
        else:
            self._trim_pending()

    def _note_flush_failure(self, stage: str, exc: Exception) -> None:
        self._flush_failures += 1
        self._consecutive_flush_failures += 1
        self._last_flush_error = f"{stage}: {exc}"
        backoff = min(2.0 ** min(self._consecutive_flush_failures, 5), self.FLUSH_BACKOFF_MAX_SECONDS)
        self._flush_retry_not_before = time.monotonic() + backoff
        log = logger.error if self._consecutive_flush_failures in (1, 5) or self._consecutive_flush_failures % 60 == 0 else logger.debug
        log(
            f"[LiveCandleStore] {stage} flush failed "
            f"(#{self._consecutive_flush_failures}, retry in {backoff:.0f}s, "
            f"{len(self._tick_batch)} ticks buffered): {exc}"
        )

    def _trim_pending(self) -> None:
        overflow = len(self._tick_batch) - self.MAX_PENDING_TICKS
        if overflow > 0:
            del self._tick_batch[:overflow]
            self._ticks_dropped += overflow
            logger.warning(
                f"[LiveCandleStore] pending tick buffer capped — dropped {overflow} oldest ticks"
            )

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
        batch_len = len(self._tick_batch)
        if not payload:
            self._tick_batch.clear()
            return

        # The batch is cleared only AFTER a successful commit — a failing
        # flush must retain its ticks for the next attempt, not lose them.
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
        del self._tick_batch[:batch_len]
        self._ticks_persisted += len(payload)

    async def _persist_candles(self, *, force: bool = False) -> None:
        dirty_buckets = [
            bucket for bucket in self._buckets.values()
            if bucket.dirty or force
        ]
        if not dirty_buckets:
            return

        spot_rows: list[dict[str, Any]] = []
        option_rows: list[dict[str, Any]] = []
        option_buckets: list[_CandleBucket] = []
        # Buckets are marked clean only AFTER the commit succeeds — a failed
        # flush must leave them dirty for retry. Deliberately-dropped buckets
        # (phantom expiry, no-arb) are marked clean without persisting.
        flushed: list[_CandleBucket] = []

        for bucket in dirty_buckets:
            metadata = await self._resolve_symbol_metadata(bucket.symbol)
            if not metadata:
                continue

            if metadata["kind"] == "spot":
                flushed.append(bucket)
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

            # Reject phantom index contracts on an invalid expiry weekday (e.g. a
            # NIFTY 'Thursday' series that only exists on BSE) before they pollute
            # option_premium_candles. Stocks/non-index symbols pass through.
            from analysis.instruments import is_valid_index_expiry

            if not is_valid_index_expiry(metadata.get("underlying"), metadata.get("expiry")):
                logger.warning(
                    f"[live_candle_store] skipping phantom expiry {metadata.get('underlying')} "
                    f"{metadata.get('expiry')} (invalid index expiry weekday)"
                )
                flushed.append(bucket)
                continue
            option_buckets.append(bucket)
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

            # Option buckets are flushed whether their row survives the no-arb
            # filter (persisted) or not (deliberate drop, never retried).
            flushed.extend(option_buckets)
            if option_rows:
                # Drop no-arbitrage-violating rows before they are ever persisted.
                _kept = [r for r in option_rows if _option_row_passes_no_arb(r)]
                if len(_kept) != len(option_rows):
                    logger.warning(
                        "[live_candle_store] dropped {n} no-arb option rows at ingest",
                        n=len(option_rows) - len(_kept),
                    )
                option_rows = _kept

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

        for bucket in flushed:
            bucket.dirty = False
        self._candle_rows_upserted += len(spot_rows) + len(option_rows)

    async def _resolve_symbol_metadata(self, symbol: str) -> Optional[dict[str, Any]]:
        if symbol in self._metadata_cache:
            cached = self._metadata_cache[symbol]
            if cached is not None:
                return cached
            # Negative results expire so a contract added to the catalog
            # mid-session starts persisting without a restart.
            if time.monotonic() - self._metadata_none_at.get(symbol, 0.0) < self.METADATA_NEGATIVE_TTL_SECONDS:
                return None
            self._metadata_cache.pop(symbol, None)

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
            elif symbol.startswith("MCX:") and symbol.endswith("FUT"):
                resolved = await resolve_upstox_mcx_future(symbol)
                instrument_key = str((resolved or {}).get("instrument_key") or "")
                underlying = extract_commodity_root(symbol)
                if instrument_key and underlying:
                    metadata = {
                        "kind": "spot",
                        "underlying": underlying,
                        "instrument_key": instrument_key,
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
            self._metadata_none_at[symbol] = time.monotonic()
            logger.debug(f"[LiveCandleStore] No candle metadata mapping for {symbol}")
        return metadata


live_candle_store = LiveCandleStore()
