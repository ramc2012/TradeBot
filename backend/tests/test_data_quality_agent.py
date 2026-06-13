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


def test_data_quality_assesses_direct_observation_timestamp() -> None:
    agent = DataQualityAgent()
    now = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)

    fresh = agent.assess_observation(
        symbol="NSE_FO|12345",
        source="option_history_5m",
        observed_at=now - timedelta(minutes=5),
        now=now,
    )
    stale = agent.assess_observation(
        symbol="NSE_FO|12345",
        source="option_history_5m",
        observed_at=now - timedelta(minutes=30),
        now=now,
    )

    assert fresh.stale is False
    assert stale.stale is True
    assert "beyond" in str(stale.reason)


def test_record_tick_rejects_purely_numeric_symbols():
    """A bare number ("22190", "2245", "23390.5") is a strike/token leaked into
    the symbol field — it must never enter the ledger (it bloated ~1,900 junk
    'stale' entries in the data_health payload)."""
    from market_data.data_quality_agent import DataQualityAgent

    agent = DataQualityAgent()
    for junk in ("22190", "2245", "23390.5", "  19500 ", ""):
        agent.record_tick(symbol=junk, source="fyers_tick", last_value=123.4)
    snap = agent.snapshot() if hasattr(agent, "snapshot") else None
    # No numeric-only key should exist in the ledger.
    keys = list(getattr(agent, "_ledger", {}).keys())
    assert all(any(c.isalpha() for c in sym) for sym, _src in keys), keys
    # A real symbol IS recorded.
    agent.record_tick(symbol="NSE:NIFTY50-INDEX", source="fyers_tick", last_value=23100.0)
    keys2 = [sym for sym, _src in getattr(agent, "_ledger", {}).keys()]
    assert "NSE:NIFTY50-INDEX" in keys2
