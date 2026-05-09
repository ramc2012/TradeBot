from __future__ import annotations

from datetime import datetime, timezone

from brokers.base import Tick
from market_data.data_router import DataRouter
from market_data.market_profile import MarketProfileBuilder


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

    builder.on_tick(
        Tick(
            symbol="NSE:NIFTY50-INDEX",
            ltp=24050.6,
            volume=100,
            timestamp=datetime(2026, 4, 11, 9, 15, tzinfo=timezone.utc),
        )
    )
    builder.on_tick(
        Tick(
            symbol="NSE:NIFTY50-INDEX",
            ltp=24062.4,
            volume=120,
            timestamp=datetime(2026, 4, 11, 9, 16),
        )
    )

    rows = builder.get_tick_rows("NSE:NIFTY50-INDEX")

    assert len(rows) == 2
    assert rows[0]["time"].endswith("+00:00")
    assert rows[1]["time"].endswith("+00:00")


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
