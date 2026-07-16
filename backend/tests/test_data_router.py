from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone

import pytest

from brokers.base import Tick
from market_data.data_router import DataRouter
from market_data.market_profile import MarketProfileBuilder
from market_data.symbols import TICK_CAPTURE_APP_SYMBOLS


class _FakeWs:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeBroker:
    def __init__(self, broker_name: str = "fyers") -> None:
        self.broker_name = broker_name
        self.subscribe_calls: list[list[str]] = []

    async def subscribe_websocket(self, symbols, callback, on_depth_callback=None, **kwargs):
        # The fyers branch of DataRouter now also passes on_depth_callback (added in
        # commit 0d8c65a2). Accept it (and any future kwargs) so the fake matches the
        # real adapter signature (brokers/fyers.py subscribe_websocket).
        self.subscribe_calls.append(list(symbols))
        return _FakeWs()


def test_data_router_normalizes_naive_tick_timestamps_to_utc() -> None:
    router = DataRouter()
    router._on_tick(
        Tick(
            symbol="NSE:NIFTY50-INDEX",
            ltp=24050.6,
            timestamp=datetime(2026, 4, 11, 9, 15),
        )
    )

    stored = router.get_latest_tick("NSE:NIFTY50-INDEX")

    assert stored is not None
    assert stored.timestamp is not None
    assert stored.timestamp.tzinfo == timezone.utc
    assert stored.timestamp.isoformat() == "2026-04-11T09:15:00+00:00"


def test_market_profile_builder_accepts_timezone_aware_ticks_without_type_errors() -> None:
    builder = MarketProfileBuilder()
    now = datetime.now(timezone.utc).replace(microsecond=0)

    builder.on_tick(
        Tick(
            symbol="NSE:NIFTY50-INDEX",
            ltp=24050.6,
            volume=100,
            timestamp=now,
        )
    )
    builder.on_tick(
        Tick(
            symbol="NSE:NIFTY50-INDEX",
            ltp=24062.4,
            volume=120,
            timestamp=(now + timedelta(minutes=1)).replace(tzinfo=None),
        )
    )

    rows = builder.get_tick_rows("NSE:NIFTY50-INDEX")

    assert len(rows) == 2
    assert rows[0]["time"].endswith("+00:00")
    assert rows[1]["time"].endswith("+00:00")


def test_market_profile_builder_snapshots_live_deque_before_iterating() -> None:
    class _ExplodingDeque(deque):
        def __iter__(self):
            raise RuntimeError("deque mutated during iteration")

        def copy(self):
            return deque(deque.__iter__(self))

    builder = MarketProfileBuilder()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    live_ticks = _ExplodingDeque(
        [
            Tick(symbol="NSE:NIFTY50-INDEX", ltp=24050.6, volume=100, timestamp=now),
            Tick(symbol="NSE:NIFTY50-INDEX", ltp=24062.4, volume=120, timestamp=now + timedelta(minutes=1)),
        ]
    )
    builder._ticks["NSE:NIFTY50-INDEX"] = live_ticks

    rows = builder.get_tick_rows("NSE:NIFTY50-INDEX")

    assert len(rows) == 2
    assert rows[0]["close"] == pytest.approx(24050.6)
    assert rows[1]["close"] == pytest.approx(24062.4)


def test_data_router_global_callbacks_receive_ticks() -> None:
    router = DataRouter()
    captured: list[Tick] = []

    router.register_global_callback(captured.append)
    router._on_tick(
        Tick(
            symbol="NSE:NIFTY50-INDEX",
            ltp=24075.4,
            timestamp=datetime(2026, 4, 11, 9, 15, tzinfo=timezone.utc),
        )
    )

    assert len(captured) == 1
    assert captured[0].symbol == "NSE:NIFTY50-INDEX"
    assert captured[0].timestamp is not None


def test_data_router_drops_crosswired_book_tick_before_callbacks() -> None:
    router = DataRouter()
    captured: list[Tick] = []

    router.register_global_callback(captured.append)
    router._on_tick(
        Tick(
            symbol="MCX:CRUDEOIL26JULFUT",
            ltp=24217.95,
            bid=6967.0,
            ask=6969.0,
            timestamp=datetime(2026, 7, 13, 9, 15, tzinfo=timezone.utc),
        )
    )

    assert captured == []
    assert router.get_latest_tick("MCX:CRUDEOIL26JULFUT") is None


def test_data_router_status_schedules_reconnect_for_stale_broker_feed(monkeypatch) -> None:
    router = DataRouter()
    scheduled: list[bool] = []

    monkeypatch.setattr(router, "_schedule_reconnect", lambda: scheduled.append(True))
    monkeypatch.setattr(router, "_stream_window_open", lambda now=None: True)
    router._broker = type("Broker", (), {"broker_name": "fyers"})()
    router._ws_client = object()
    router._subscribed_symbols = ["NSE:NIFTY50-INDEX"]
    router._tick_buffer["NSE:NIFTY50-INDEX"] = Tick(
        symbol="NSE:NIFTY50-INDEX",
        ltp=24050.6,
        timestamp=datetime(2026, 4, 11, 9, 15, tzinfo=timezone.utc),
    )

    status = router.get_status()

    assert status["ws_connected"] is False
    assert scheduled == [True]


def test_data_router_status_skips_reconnect_for_stale_feed_off_hours(monkeypatch) -> None:
    router = DataRouter()
    scheduled: list[bool] = []

    monkeypatch.setattr(router, "_schedule_reconnect", lambda: scheduled.append(True))
    monkeypatch.setattr(router, "_stream_window_open", lambda now=None: False)
    router._broker = type("Broker", (), {"broker_name": "fyers"})()
    router._ws_client = object()
    router._subscribed_symbols = ["NSE:NIFTY50-INDEX"]
    router._tick_buffer["NSE:NIFTY50-INDEX"] = Tick(
        symbol="NSE:NIFTY50-INDEX",
        ltp=24050.6,
        timestamp=datetime(2026, 4, 11, 9, 15, tzinfo=timezone.utc),
    )

    status = router.get_status()

    assert status["ws_connected"] is False
    assert scheduled == []


def test_data_router_reconnect_schedule_is_rate_limited() -> None:
    class DoneTask:
        def done(self) -> bool:
            return True

    class FakeLoop:
        def __init__(self) -> None:
            self.created = 0

        def is_running(self) -> bool:
            return True

        def create_task(self, coroutine):
            self.created += 1
            coroutine.close()
            return DoneTask()

    loop = FakeLoop()
    router = DataRouter()
    router._loop = loop  # type: ignore[assignment]

    router._schedule_reconnect()
    router._schedule_reconnect()

    assert loop.created == 1


@pytest.mark.asyncio
async def test_data_router_keeps_required_index_capture_symbols_on_fyers_subscribe() -> None:
    router = DataRouter()
    router._stream_window_open = lambda now=None: True  # type: ignore[method-assign]
    broker = _FakeBroker("fyers")
    router.set_broker(broker)  # type: ignore[arg-type]

    await router.subscribe(["NSE:FINNIFTY-INDEX"])

    assert set(TICK_CAPTURE_APP_SYMBOLS).issubset(set(router._subscribed_symbols))
    assert "NSE:NIFTY50-INDEX" in broker.subscribe_calls[-1]
    assert "NSE:NIFTYBANK-INDEX" in broker.subscribe_calls[-1]
    assert "BSE:SENSEX-INDEX" in broker.subscribe_calls[-1]
    assert router.get_status()["required_symbols"] == list(TICK_CAPTURE_APP_SYMBOLS)


@pytest.mark.asyncio
async def test_data_router_required_symbols_survive_sticky_option_resubscribe() -> None:
    router = DataRouter()
    router._stream_window_open = lambda now=None: True  # type: ignore[method-assign]
    broker = _FakeBroker("upstox")
    router.set_broker(broker)  # type: ignore[arg-type]

    await router.subscribe(["NSE:FINNIFTY-INDEX"])
    await router.add_subscriptions(["NSE:ABC26JUN100CE"])

    assert set(TICK_CAPTURE_APP_SYMBOLS).issubset(set(router._subscribed_symbols))
    assert "NSE:ABC26JUN100CE" in router._subscribed_symbols
    assert "NSE_INDEX|Nifty 50" in broker.subscribe_calls[-1]
    assert "NSE_INDEX|Nifty Bank" in broker.subscribe_calls[-1]
    assert "BSE_INDEX|SENSEX" in broker.subscribe_calls[-1]


@pytest.mark.asyncio
async def test_data_router_ensure_required_subscriptions_restores_missing_symbols() -> None:
    router = DataRouter()
    router._stream_window_open = lambda now=None: True  # type: ignore[method-assign]
    broker = _FakeBroker("fyers")
    router.set_broker(broker)  # type: ignore[arg-type]
    router._subscribed_symbols = ["NSE:FINNIFTY-INDEX"]

    await router.ensure_required_subscriptions()

    assert set(TICK_CAPTURE_APP_SYMBOLS).issubset(set(router._subscribed_symbols))
    assert broker.subscribe_calls


def test_data_router_index_market_hours_gate() -> None:
    monday_open_ist = datetime(2026, 6, 1, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    saturday_open_ist = datetime(2026, 5, 30, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    assert DataRouter._is_index_market_open(monday_open_ist) is True
    assert DataRouter._is_index_market_open(saturday_open_ist) is False


@pytest.mark.asyncio
async def test_data_router_defers_websocket_outside_exchange_windows() -> None:
    router = DataRouter()
    broker = _FakeBroker("fyers")
    router.set_broker(broker)  # type: ignore[arg-type]
    router._stream_window_open = lambda now=None: False  # type: ignore[method-assign]

    await router.subscribe(["NSE:FINNIFTY-INDEX"])

    assert broker.subscribe_calls == []
    assert router._desired_primary_symbols == ["NSE:FINNIFTY-INDEX"]


def test_data_router_drops_out_of_band_index_tick() -> None:
    from market_data import index_band_guard

    index_band_guard.clear_reference_closes()
    router = DataRouter()
    # A whole-frame NIFTY value (~24k) misrouted under BANKNIFTY must never reach
    # the tick buffer (live marks) — BANKNIFTY's absolute floor is 28k.
    router._on_tick(Tick(symbol="NSE:BANKNIFTY-INDEX", ltp=24089.0))
    assert "NSE:BANKNIFTY-INDEX" not in router._tick_buffer
    assert router.get_ltp("NSE:BANKNIFTY-INDEX") == 0.0
    # The real level is accepted.
    router._on_tick(Tick(symbol="NSE:BANKNIFTY-INDEX", ltp=57582.0))
    assert router.get_ltp("NSE:BANKNIFTY-INDEX") == 57582.0
    # REALTY 908 into every index is also rejected (sector coverage).
    router._on_tick(Tick(symbol="NSE:NIFTY50-INDEX", ltp=906.0))
    assert "NSE:NIFTY50-INDEX" not in router._tick_buffer
