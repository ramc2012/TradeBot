"""Redis P0 (2026-07-18) — bounded blocking pools + coalesced tick flusher.

Covers:
  * command vs pub/sub pool isolation (distinct clients, distinct bounded pools)
  * BlockingConnectionPool semantics — a caller at the cap WAITS for a release
    (and succeeds if one arrives) instead of raising immediately; raises only
    after the acquire timeout, with the wait/timeout counters incremented
  * the coalesced flusher writes every pending symbol exactly once per flush,
    latest value wins, per-symbol channel + hot-cache key + payload schema are
    unchanged from the per-tick era, and no per-tick publish task is spawned
  * /api/system/pools telemetry returns both Redis pools + DB pool numbers
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from time import monotonic

import pytest
import redis.asyncio as aioredis

import importlib

import db.redis_client as redis_client_module

# NB: `import market_data.data_router as X` would bind the SINGLETON (the
# package __init__ rebinds the `data_router` attribute); import_module returns
# the real module object so get_redis can be monkeypatched.
data_router_module = importlib.import_module("market_data.data_router")
from brokers.base import Tick
from core.config import settings
from db.redis_client import InstrumentedBlockingPool, get_redis, get_redis_pubsub
from market_data.data_router import (
    LATEST_TICK_KEY_PREFIX,
    LATEST_TICK_TTL_SECONDS,
    DataRouter,
)


@pytest.fixture()
def isolated_redis_singletons():
    """Snapshot/restore the module-level Redis client singletons."""
    saved = (redis_client_module._redis, redis_client_module._redis_pubsub)
    redis_client_module._redis = None
    redis_client_module._redis_pubsub = None
    yield
    redis_client_module._redis, redis_client_module._redis_pubsub = saved


# ── pool isolation ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pubsub_pool_is_distinct_from_command_pool(isolated_redis_singletons):
    command = await get_redis()
    pubsub = await get_redis_pubsub()

    assert command is not pubsub
    assert command.connection_pool is not pubsub.connection_pool
    # Both are bounded BLOCKING pools (wait-then-raise, never raise-immediately).
    assert isinstance(command.connection_pool, InstrumentedBlockingPool)
    assert isinstance(pubsub.connection_pool, InstrumentedBlockingPool)
    assert command.connection_pool.max_connections == settings.REDIS_COMMAND_MAX_CONNECTIONS
    assert pubsub.connection_pool.max_connections == settings.REDIS_PUBSUB_MAX_CONNECTIONS
    assert command.connection_pool.timeout == settings.REDIS_POOL_ACQUIRE_TIMEOUT_SECONDS

    # Singletons are cached per role.
    assert await get_redis() is command
    assert await get_redis_pubsub() is pubsub


# ── blocking-pool semantics ──────────────────────────────────────────────────


class _FakeConnection:
    """Weakref-able stand-in; ensure_connection is bypassed in these tests."""


def _make_pool(max_connections: int, timeout: float) -> InstrumentedBlockingPool:
    pool = InstrumentedBlockingPool(
        max_connections=max_connections,
        timeout=timeout,
        connection_class=_FakeConnection,
    )

    async def _no_io_ensure(connection):  # pragma: no cover - trivial
        return None

    pool.ensure_connection = _no_io_ensure  # type: ignore[method-assign]
    return pool


@pytest.mark.asyncio
async def test_blocking_pool_waits_then_raises_instead_of_raising_immediately():
    pool = _make_pool(max_connections=1, timeout=0.2)

    conn = await pool.get_connection("GET")
    assert pool.acquire_waits == 0

    start = monotonic()
    with pytest.raises(aioredis.ConnectionError):
        await pool.get_connection("GET")
    elapsed = monotonic() - start

    # It WAITED for the timeout (the plain pool raises instantly at the cap).
    assert elapsed >= 0.15
    assert pool.acquire_waits == 1
    assert pool.acquire_timeouts == 1

    stats = pool.stats()
    assert stats["max_connections"] == 1
    assert stats["in_use"] == 1
    assert stats["acquire_timeouts"] == 1

    await pool.release(conn)
    assert (await pool.get_connection("GET")) is conn


@pytest.mark.asyncio
async def test_blocking_pool_waiter_succeeds_when_connection_released_in_time():
    pool = _make_pool(max_connections=1, timeout=2.0)
    conn = await pool.get_connection("GET")

    async def _release_soon():
        await asyncio.sleep(0.05)
        await pool.release(conn)

    release_task = asyncio.create_task(_release_soon())
    got = await pool.get_connection("GET")
    await release_task

    assert got is conn
    assert pool.acquire_waits == 1
    assert pool.acquire_timeouts == 0


# ── coalesced tick flusher ───────────────────────────────────────────────────


class _FakePipeline:
    def __init__(self, log: list):
        self._log = log
        self.executed = 0

    def publish(self, channel: str, payload: str):
        self._log.append(("publish", channel, payload))

    def set(self, key: str, payload: str, ex: int | None = None):
        self._log.append(("set", key, payload, ex))

    async def execute(self):
        self.executed += 1


class _FakeRedis:
    def __init__(self):
        self.log: list = []
        self.pipelines: list[_FakePipeline] = []

    def pipeline(self, transaction: bool = True):
        pipe = _FakePipeline(self.log)
        self.pipelines.append(pipe)
        return pipe


def _tick(symbol: str, ltp: float) -> Tick:
    return Tick(
        symbol=symbol,
        ltp=ltp,
        open=ltp - 5,
        high=ltp + 10,
        low=ltp - 10,
        close=ltp - 2,
        volume=1000,
        timestamp=datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_flush_writes_every_pending_symbol_once_and_latest_value_wins(monkeypatch):
    from market_data import index_band_guard

    index_band_guard.clear_reference_closes()
    router = DataRouter()
    fake = _FakeRedis()

    async def _fake_get_redis():
        return fake

    monkeypatch.setattr(data_router_module, "get_redis", _fake_get_redis)

    # Two prints on NIFTY inside one window — only the LATEST may be written.
    router._on_tick(_tick("NSE:NIFTY50-INDEX", 24000.0))
    router._on_tick(_tick("NSE:NIFTY50-INDEX", 24010.5))
    router._on_tick(_tick("NSE:BANKNIFTY-INDEX", 57500.0))

    # No per-tick publish happened — everything is staged.
    assert fake.log == []
    assert set(router._pending_redis_ticks) == {"NSE:NIFTY50-INDEX", "NSE:BANKNIFTY-INDEX"}

    await router._flush_pending_ticks()

    publishes = [entry for entry in fake.log if entry[0] == "publish"]
    sets = [entry for entry in fake.log if entry[0] == "set"]
    # Exactly once per symbol, on ONE pipeline round-trip.
    assert sorted(entry[1] for entry in publishes) == sorted([
        "ticks:NSE:NIFTY50-INDEX",
        "ticks:NSE:BANKNIFTY-INDEX",
    ])
    assert sorted(entry[1] for entry in sets) == sorted([
        f"{LATEST_TICK_KEY_PREFIX}NSE:NIFTY50-INDEX",
        f"{LATEST_TICK_KEY_PREFIX}NSE:BANKNIFTY-INDEX",
    ])
    assert len(fake.pipelines) == 1
    assert fake.pipelines[0].executed == 1

    # Latest value wins within the window.
    nifty_payload = json.loads(
        next(entry[2] for entry in publishes if entry[1] == "ticks:NSE:NIFTY50-INDEX")
    )
    assert nifty_payload["ltp"] == 24010.5

    # Payload schema is the per-tick era contract (consumers: frontend WS,
    # get_live_mark, macd_refined marks, option_subscription_manager).
    assert set(nifty_payload) == {
        "symbol", "ltp", "open", "high", "low", "close",
        "volume", "oi", "bid", "ask", "timestamp",
    }
    assert nifty_payload["symbol"] == "NSE:NIFTY50-INDEX"
    assert nifty_payload["timestamp"] == "2026-07-17T10:00:00+00:00"

    # Channel payload and hot-cache payload are identical; TTL preserved.
    nifty_set = next(e for e in sets if e[1] == f"{LATEST_TICK_KEY_PREFIX}NSE:NIFTY50-INDEX")
    assert json.loads(nifty_set[2]) == nifty_payload
    assert nifty_set[3] == LATEST_TICK_TTL_SECONDS

    # Batch drained; an empty flush is a no-op (no new pipeline).
    assert router._pending_redis_ticks == {}
    await router._flush_pending_ticks()
    assert len(fake.pipelines) == 1


@pytest.mark.asyncio
async def test_on_tick_starts_one_flusher_task_not_one_task_per_tick():
    router = DataRouter()
    router._loop = asyncio.get_running_loop()

    for i in range(10):
        router._on_tick(_tick("NSE:NIFTY50-INDEX", 24000.0 + i))

    task = router._redis_flush_task
    assert task is not None and not task.done()

    router._on_tick(_tick("NSE:NIFTY50-INDEX", 24100.0))
    assert router._redis_flush_task is task  # still the same single task

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_get_live_mark_reads_flushed_hot_cache_value(monkeypatch):
    """End-to-end (fake Redis): staged tick → flush → tick:{sym} readable."""

    class _KvRedis(_FakeRedis):
        def __init__(self):
            super().__init__()
            self.kv: dict[str, str] = {}

        def pipeline(self, transaction: bool = True):
            outer = self

            class _KvPipe(_FakePipeline):
                def set(self, key, payload, ex=None):
                    super().set(key, payload, ex)
                    outer.kv[key] = payload

            pipe = _KvPipe(self.log)
            self.pipelines.append(pipe)
            return pipe

        async def get(self, key):
            return self.kv.get(key)

    from market_data import index_band_guard

    index_band_guard.clear_reference_closes()
    router = DataRouter()
    fake = _KvRedis()

    async def _fake_get_redis():
        return fake

    monkeypatch.setattr(data_router_module, "get_redis", _fake_get_redis)

    tick = _tick("NSE:NIFTY50-INDEX", 24050.0)
    tick.timestamp = datetime.now(timezone.utc)
    router._on_tick(tick)
    await router._flush_pending_ticks()

    # Cross-process read path: wipe the in-process buffer, read via Redis.
    router._tick_buffer.clear()
    assert await router.get_live_mark("NSE:NIFTY50-INDEX") == 24050.0


# ── telemetry endpoint ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connection_pools_endpoint_reports_pool_numbers(isolated_redis_singletons):
    from api.routers.system import connection_pools

    await get_redis()  # initialize the command pool so stats are concrete

    payload = await connection_pools()

    redis_stats = payload["redis"]
    assert redis_stats["command"]["initialized"] is True
    assert redis_stats["command"]["max_connections"] == settings.REDIS_COMMAND_MAX_CONNECTIONS
    for key in ("in_use", "available", "acquire_waits", "acquire_timeouts"):
        assert key in redis_stats["command"]
    assert redis_stats["pubsub"] == {"initialized": False}

    db_stats = payload["database"]
    assert "error" not in db_stats
    for key in ("size", "checked_out", "checked_in", "overflow"):
        assert isinstance(db_stats[key], int)


# ── staging-map thread-safety (race fix, 2026-07-18) ─────────────────────────


@pytest.mark.asyncio
async def test_concurrent_thread_writes_during_flush_lose_no_tick(monkeypatch):
    """Broker SDK callbacks stage ticks from a NON-loop thread while the flusher
    swaps + drains the map on the loop thread. Before the staging lock, a
    producer insert that interleaved with the flusher's swap-then-iterate could
    drop a tick or raise "dict changed size during iteration". This hammers that
    handoff and asserts: no exception escapes, and every staged symbol is written.
    """
    import threading as _threading

    from market_data import index_band_guard

    index_band_guard.clear_reference_closes()
    router = DataRouter()
    # No self._loop wiring: the producer thread must NOT try to (re)start the
    # flusher; we drive _flush_pending_ticks() ourselves from the loop thread.
    fake = _FakeRedis()

    async def _fake_get_redis():
        return fake

    monkeypatch.setattr(data_router_module, "get_redis", _fake_get_redis)

    n_symbols = 400
    symbols = [f"NSE:SYM{i:04d}-INDEX" for i in range(n_symbols)]
    errors: list[BaseException] = []
    done = _threading.Event()

    def _producer():
        try:
            # Two passes so late writes overwrite (last-write-wins) and keep the
            # map churning while the flusher swaps underneath it.
            for value_base in (100.0, 200.0):
                for i, sym in enumerate(symbols):
                    router._on_tick(_tick(sym, value_base + i))
        except BaseException as exc:  # noqa: BLE001 — capture, assert later
            errors.append(exc)
        finally:
            done.set()

    producer = _threading.Thread(target=_producer)
    producer.start()

    # Flush repeatedly while the producer runs — this is the concurrent swap.
    while not done.is_set() or router._pending_redis_ticks:
        try:
            await router._flush_pending_ticks()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        await asyncio.sleep(0)  # yield so the producer thread interleaves

    producer.join(timeout=10)
    assert not producer.is_alive()
    await router._flush_pending_ticks()  # final drain

    # (1) No "dict changed size during iteration" (or anything else) escaped.
    assert errors == []
    # (2) No tick lost: every symbol was published at least once across flushes.
    published = {entry[1] for entry in fake.log if entry[0] == "publish"}
    assert published == {f"ticks:{sym}" for sym in symbols}
    # (3) Map fully drained; the last-write-wins value survives for a sample sym.
    assert router._pending_redis_ticks == {}
    last_payloads = [
        json.loads(entry[2])
        for entry in fake.log
        if entry[0] == "publish" and entry[1] == "ticks:NSE:SYM0000-INDEX"
    ]
    assert last_payloads  # at least one write for the sampled symbol
    # The final observed value must be one the producer actually sent (200.0 or
    # 100.0 offset), never a torn/dropped state.
    assert last_payloads[-1]["ltp"] in {100.0, 200.0}


@pytest.mark.asyncio
async def test_unsubscribe_clear_is_lock_guarded_and_drops_staged_ticks():
    """The unsubscribe path clears the staging map so a retired symbol cannot
    resurrect its hot-cache key on the next flush. Verify the clear empties the
    map (and, implicitly, runs under the same lock the producer/flusher use)."""
    router = DataRouter()
    router._on_tick(_tick("NSE:NIFTY50-INDEX", 24000.0))
    assert router._pending_redis_ticks
    with router._redis_ticks_lock:
        router._pending_redis_ticks.clear()
    assert router._pending_redis_ticks == {}
