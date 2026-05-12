from __future__ import annotations

from datetime import datetime, timedelta, timezone

from market_data.data_quality_agent import DataQualityAgent


def test_data_quality_ignores_empty_symbols_and_aggregates_by_symbol() -> None:
    agent = DataQualityAgent()
    now = datetime.now(timezone.utc)

    agent.record_tick(symbol="", source="fyers_tick", observed_at=now, last_value=1.0)
    agent.record_tick(
        symbol="NSE:NIFTY50-INDEX",
        source="upstox_tick",
        observed_at=now - timedelta(seconds=90),
        last_value=23800.0,
    )
    agent.record_tick(
        symbol="NSE:NIFTY50-INDEX",
        source="fyers_tick",
        observed_at=now,
        last_value=23805.0,
    )

    snapshot = agent.snapshot()

    assert snapshot["symbol_count"] == 1
    assert snapshot["entry_count"] == 2
    assert snapshot["overall"] == "healthy"
    assert snapshot["symbol_health"][0]["symbol"] == "NSE:NIFTY50-INDEX"
    assert snapshot["symbol_health"][0]["stale"] is False


def test_data_quality_marks_stale_nse_ticks_idle_outside_market_hours(monkeypatch) -> None:
    agent = DataQualityAgent()
    observed_at = datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc)
    snapshot_at = datetime(2026, 5, 12, 18, 30, tzinfo=timezone.utc)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return snapshot_at.replace(tzinfo=None)
            return snapshot_at.astimezone(tz)

    agent.record_tick(
        symbol="NSE:NIFTY50-INDEX",
        source="fyers_tick",
        observed_at=observed_at,
        last_value=24000.0,
    )
    monkeypatch.setattr("market_data.data_quality_agent.datetime", FixedDateTime)

    snapshot = agent.snapshot()

    assert snapshot["overall"] == "idle"
    assert snapshot["market_state"] == "nse_closed"
    assert snapshot["stale_count"] == 1
