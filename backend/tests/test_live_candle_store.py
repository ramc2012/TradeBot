from __future__ import annotations

from datetime import datetime, timezone

import pytest

from market_data.live_candle_store import LiveCandleStore, _CandleBucket


class _FakeSession:
    def __init__(self, statements: list[tuple[str, object]]) -> None:
        self._statements = statements

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def execute(self, stmt, params=None):
        self._statements.append((str(stmt), params))
        return None

    async def commit(self) -> None:
        return None


@pytest.mark.asyncio
async def test_live_candle_store_persists_spot_and_option_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    store = LiveCandleStore()
    statements: list[tuple[str, object]] = []

    monkeypatch.setattr(
        "market_data.live_candle_store.AsyncSessionLocal",
        lambda: _FakeSession(statements),
    )

    async def _resolve(symbol: str):
        if symbol == "NSE:NIFTY50-INDEX":
            return {
                "kind": "spot",
                "underlying": "NIFTY",
                "instrument_key": "NSE_INDEX|Nifty 50",
            }
        return {
            "kind": "option",
            "instrument_key": "NSE:TEST26APR24000CE",
            "trading_symbol": "TEST26APR24000CE",
            "underlying": "NIFTY",
            # Post-2025-09-01 migration NSE indices expire Tuesday; a Thursday expiry
            # (2026-04-30) is now rejected by the phantom-expiry guard in
            # live_candle_store (is_valid_index_expiry), so use a valid Tuesday.
            "expiry": "2026-04-28",
            "strike": 24000.0,
            "option_type": "CE",
            "market": "NSE",
        }

    monkeypatch.setattr(store, "_resolve_symbol_metadata", _resolve)

    now = datetime(2026, 4, 11, 9, 15, tzinfo=timezone.utc)
    store._latest_spot["NIFTY"] = 24050.0
    store._buckets[("NSE:NIFTY50-INDEX", "1minute")] = _CandleBucket(
        symbol="NSE:NIFTY50-INDEX",
        interval="1minute",
        bucket_start=now,
        open=24010.0,
        high=24060.0,
        low=24000.0,
        close=24050.0,
        volume=100,
        oi=0,
        updated_at=now,
    )
    store._buckets[("NSE:TEST26APR24000CE", "5minute")] = _CandleBucket(
        symbol="NSE:TEST26APR24000CE",
        interval="5minute",
        bucket_start=now,
        open=100.0,
        high=105.0,
        low=99.0,
        close=103.0,
        volume=50,
        oi=10,
        updated_at=now,
    )

    await store._persist_candles()

    assert any("underlying_spot_candles" in sql for sql, _ in statements)
    assert any("option_premium_candles" in sql for sql, _ in statements)

    spot_payload = next(params for sql, params in statements if "underlying_spot_candles" in sql)
    option_payload = next(params for sql, params in statements if "option_premium_candles" in sql)

    assert isinstance(spot_payload, list)
    assert isinstance(option_payload, list)
    assert spot_payload[0]["interval"] == "1minute"
    assert option_payload[0]["interval"] == "5minute"
    assert option_payload[0]["underlying_price"] == 24050.0


@pytest.mark.asyncio
async def test_resolve_symbol_metadata_maps_mcx_future_to_spot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LiveCandleStore()

    async def _resolve_future(symbol: str):
        assert symbol == "MCX:GOLD26AUGFUT"
        return {
            "instrument_key": "MCX_FO|466583",
            "trading_symbol": "GOLD FUT 05 AUG 26",
            "expiry": "2026-08-05",
        }

    monkeypatch.setattr(
        "market_data.live_candle_store.resolve_upstox_mcx_future",
        _resolve_future,
    )

    metadata = await store._resolve_symbol_metadata("MCX:GOLD26AUGFUT")

    assert metadata == {
        "kind": "spot",
        "underlying": "GOLD",
        "instrument_key": "MCX_FO|466583",
    }


class _FailingThenOkSession:
    """Fails the first N commits, then succeeds — simulates a transient DB blip."""

    calls = {"failures_left": 0, "statements": []}

    def __init__(self) -> None:
        self._stmts: list[tuple[str, object]] = []

    async def __aenter__(self) -> "_FailingThenOkSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def execute(self, stmt, params=None):
        self._stmts.append((str(stmt), params))
        return None

    async def commit(self) -> None:
        if self.calls["failures_left"] > 0:
            self.calls["failures_left"] -= 1
            raise RuntimeError("connection reset by peer")
        self.calls["statements"].extend(self._stmts)


def _tick(symbol: str, ltp: float, volume: int, ts: datetime):
    from brokers.base import Tick

    return Tick(symbol=symbol, ltp=ltp, volume=volume, timestamp=ts)


@pytest.mark.asyncio
async def test_flush_failure_retains_ticks_and_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    """P0 fix: a transient DB error must not lose the batch or kill persistence."""
    store = LiveCandleStore()
    _FailingThenOkSession.calls = {"failures_left": 1, "statements": []}
    monkeypatch.setattr(
        "market_data.live_candle_store.AsyncSessionLocal", _FailingThenOkSession
    )

    now = datetime(2026, 4, 11, 9, 15, 30, tzinfo=timezone.utc)
    store._tick_batch.append(_tick("NSE:NIFTY50-INDEX", 24000.0, 0, now))

    # First flush: commit raises -> ticks retained, failure recorded, no raise.
    await store._flush_pending()
    assert len(store._tick_batch) == 1
    assert store._consecutive_flush_failures >= 1
    assert store.status()["last_flush_error"] is not None

    # Second flush (force bypasses the backoff window): succeeds, batch drains.
    await store._flush_pending(force=True)
    assert store._tick_batch == []
    assert store._consecutive_flush_failures == 0
    assert store._ticks_persisted == 1
    assert any("market_ticks" in sql for sql, _ in _FailingThenOkSession.calls["statements"])


@pytest.mark.asyncio
async def test_candle_buckets_stay_dirty_on_failed_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    store = LiveCandleStore()
    _FailingThenOkSession.calls = {"failures_left": 1, "statements": []}
    monkeypatch.setattr(
        "market_data.live_candle_store.AsyncSessionLocal", _FailingThenOkSession
    )

    async def _resolve(symbol: str):
        return {"kind": "spot", "underlying": "NIFTY", "instrument_key": "NSE_INDEX|Nifty 50"}

    monkeypatch.setattr(store, "_resolve_symbol_metadata", _resolve)

    now = datetime(2026, 4, 11, 9, 15, tzinfo=timezone.utc)
    bucket = _CandleBucket(
        symbol="NSE:NIFTY50-INDEX", interval="1minute", bucket_start=now,
        open=1.0, high=2.0, low=1.0, close=2.0, volume=10, oi=0, updated_at=now,
    )
    store._buckets[("NSE:NIFTY50-INDEX", "1minute")] = bucket

    await store._flush_pending()
    assert bucket.dirty is True  # failed commit must leave it for retry

    await store._flush_pending(force=True)
    assert bucket.dirty is False


def test_cumulative_volume_converted_to_per_bar_delta() -> None:
    """P1 fix: broker vtt/vol_traded_today is session-cumulative; bars store deltas."""
    store = LiveCandleStore()
    t0 = datetime(2026, 4, 11, 9, 15, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 4, 11, 9, 15, 30, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 11, 9, 16, 5, tzinfo=timezone.utc)  # next 1m bucket

    store._update_buckets(_tick("NSE:TESTCE", 100.0, 50_000, t0))
    store._update_buckets(_tick("NSE:TESTCE", 101.0, 50_900, t1))
    b1 = store._buckets[("NSE:TESTCE", "1minute")]
    assert b1.volume == 900  # first tick baselines; intra-bar delta only

    store._update_buckets(_tick("NSE:TESTCE", 102.0, 51_400, t2))
    b2 = store._buckets[("NSE:TESTCE", "1minute")]
    assert b2.volume == 500  # 51,400 - 50,900 carried from prior bar's last cum

    # Counter reset (new session / reconnect) re-baselines instead of going huge.
    t3 = datetime(2026, 4, 11, 9, 17, 2, tzinfo=timezone.utc)
    store._update_buckets(_tick("NSE:TESTCE", 103.0, 200, t3))
    b3 = store._buckets[("NSE:TESTCE", "1minute")]
    assert b3.volume == 0


def test_queue_overflow_drops_with_counter() -> None:
    store = LiveCandleStore()
    store.QUEUE_MAXSIZE = 2  # not used post-init; rebuild queue small
    import asyncio as _asyncio

    store._queue = _asyncio.Queue(maxsize=2)
    now = datetime(2026, 4, 11, 9, 15, tzinfo=timezone.utc)
    for i in range(4):
        store._enqueue_nowait(_tick("NSE:TESTCE", 100.0 + i, 0, now))
    assert store._queue.qsize() == 2
    assert store._ticks_dropped == 2
    # Freshest ticks won (oldest dropped first).
    kept = [store._queue.get_nowait().ltp for _ in range(2)]
    assert kept == [102.0, 103.0]
