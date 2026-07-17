from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import paper_engine.commodity_strategy_agent as commodity_module
from paper_engine.commodity_strategy_agent import CommodityStrategyAgent


UTC = timezone.utc


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "commodity_strategy.json"
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", config_path)

    store: dict[str, object | None] = {
        "payload": commodity_module._default_saved_state(),
        "updated_at": None,
    }

    def _load_saved_state():
        payload = copy.deepcopy(store["payload"])
        updated_at = store["updated_at"]
        return payload, updated_at

    def _load_saved_state_from_database():
        return None, None

    def _save_state(payload: dict):
        store["payload"] = copy.deepcopy(payload)
        store["updated_at"] = datetime.now(UTC)
        return store["updated_at"]

    monkeypatch.setattr(commodity_module, "_load_saved_state", _load_saved_state)
    monkeypatch.setattr(commodity_module, "_load_saved_state_from_database", _load_saved_state_from_database)
    monkeypatch.setattr(commodity_module, "_save_state", _save_state)
    monkeypatch.setattr(commodity_module.option_history_service, "reset_health", lambda: None)
    monkeypatch.setattr(
        commodity_module.option_history_service,
        "get_health_snapshot",
        lambda: {"failure_count": 0, "success_count": 0, "brokers": {}},
    )


def test_load_history_uses_local_store_when_broker_fetch_times_out(monkeypatch) -> None:
    async def fake_resolve(_symbol: str):
        return {}

    async def fake_fetch(**_kwargs):
        raise asyncio.TimeoutError

    fallback_rows = [
        {
            "time": "2026-07-14T16:53:00+05:30",
            "open": 141000.0,
            "high": 141120.0,
            "low": 140980.0,
            "close": 141015.0,
            "volume": 10,
            "oi": 0,
        }
    ]

    async def fake_local_history(self, symbol: str, *, interval: str, lookback_days: int):
        assert symbol == "MCX:GOLD26AUGFUT"
        assert interval == "1minute"
        assert lookback_days == 1
        return list(fallback_rows)

    monkeypatch.setattr(commodity_module, "resolve_upstox_mcx_future", fake_resolve)
    monkeypatch.setattr(commodity_module.option_history_service, "_fetch_broker_candles", fake_fetch)
    monkeypatch.setattr(CommodityStrategyAgent, "_load_history_from_store", fake_local_history)

    agent = CommodityStrategyAgent()
    rows = asyncio.run(agent._load_history("MCX:GOLD26AUGFUT", interval="1minute", lookback_days=1))

    assert rows == fallback_rows


def test_load_history_prefers_fresh_local_store_during_live_session(monkeypatch) -> None:
    async def fake_resolve(_symbol: str):
        return {}

    async def fake_fetch(**_kwargs):
        raise AssertionError("broker fetch should be skipped when the local store is fresh")

    fresh_rows = [
        {
            "time": (datetime.now(commodity_module.IST) - timedelta(minutes=1)).isoformat(),
            "open": 141000.0,
            "high": 141120.0,
            "low": 140980.0,
            "close": 141015.0,
            "volume": 10,
            "oi": 0,
        }
    ]

    async def fake_local_history(self, symbol: str, *, interval: str, lookback_days: int):
        assert symbol == "MCX:GOLD26AUGFUT"
        assert interval == "1minute"
        assert lookback_days == 1
        return list(fresh_rows)

    monkeypatch.setattr(commodity_module, "resolve_upstox_mcx_future", fake_resolve)
    monkeypatch.setattr(commodity_module, "_in_commodity_hours", lambda _now=None: True)
    monkeypatch.setattr(commodity_module.option_history_service, "_fetch_broker_candles", fake_fetch)
    monkeypatch.setattr(CommodityStrategyAgent, "_load_history_from_store", fake_local_history)

    agent = CommodityStrategyAgent()
    rows = asyncio.run(agent._load_history("MCX:GOLD26AUGFUT", interval="1minute", lookback_days=1))

    assert rows == fresh_rows


def test_run_once_retains_previous_row_when_symbol_scan_times_out(monkeypatch) -> None:
    async def fake_session_ok(*_args, **_kwargs):
        return True

    async def fake_health(*_args, **_kwargs):
        return {"valid": True, "status": "valid_session"}

    async def fake_scan_adapter(self):
        return None

    async def fake_active_symbols(self):
        return {
            "MCX:GOLD26AUGFUT": "MCX:GOLD26AUGFUT",
            "MCX:NICKEL26JULFUT": "MCX:NICKEL26JULFUT",
        }

    async def fake_ltp(self, _adapter, symbols: list[str]):
        return {symbol: 100.0 for symbol in symbols}

    async def fake_manage_positions(self, _adapter, futures_rows, option_rows, option_quote_map=None):
        return None

    async def fake_analyze(self, symbol: str, live_ltp: float | None):
        if symbol == "MCX:NICKEL26JULFUT":
            await asyncio.sleep(0.05)
        return {
            "symbol": symbol,
            "underlying": "GOLD" if "GOLD" in symbol else "NICKEL",
            "display_name": "Gold" if "GOLD" in symbol else "Nickel",
            "price": float(live_ltp or 100.0),
            "previous_close": 99.0,
            "change": 1.0,
            "change_pct": 1.01,
            "atr": 1.0,
            "atr_15m": 2.0,
            "bar_time": "2026-07-14T16:53:00+05:30",
            "reason": "no_trigger",
            "signal": None,
            "candidate_signal": None,
            "entry_style": "fresh_cross",
            "regime": "balance",
            "mp_status": "ready",
            "mp_periods": 8,
        }

    monkeypatch.setattr(commodity_module, "ensure_fyers_session", fake_session_ok)
    monkeypatch.setattr(commodity_module, "ensure_upstox_session", fake_session_ok)
    monkeypatch.setattr(commodity_module, "get_fyers_token_health", fake_health)
    monkeypatch.setattr(commodity_module, "get_upstox_token_health", fake_health)
    monkeypatch.setattr(commodity_module, "_in_commodity_hours", lambda _started_at: True)
    monkeypatch.setattr(commodity_module, "DEFAULT_COMMODITY_SYMBOL_SCAN_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(CommodityStrategyAgent, "_get_scan_adapter", fake_scan_adapter)
    monkeypatch.setattr(CommodityStrategyAgent, "_active_futures_symbols", fake_active_symbols)
    monkeypatch.setattr(CommodityStrategyAgent, "_safe_get_ltp", fake_ltp)
    monkeypatch.setattr(CommodityStrategyAgent, "_manage_positions", fake_manage_positions)
    monkeypatch.setattr(CommodityStrategyAgent, "_analyze_futures_symbol", fake_analyze)
    monkeypatch.setattr(CommodityStrategyAgent, "_audit_futures_watchlist", lambda self, rows: None)

    agent = CommodityStrategyAgent()
    agent.update_symbols(["MCX:GOLD26AUGFUT", "MCX:NICKEL26JULFUT"])
    agent._runtime.futures_watchlist = [
        {
            "symbol": "MCX:NICKEL26JULFUT",
            "underlying": "NICKEL",
            "display_name": "Nickel",
            "price": 1590.4,
            "previous_close": 1589.0,
            "change": 1.4,
            "change_pct": 0.09,
            "atr": 1.0,
            "atr_15m": 2.0,
            "bar_time": "2026-07-14T16:46:00+05:30",
            "reason": "no_trigger",
            "signal": None,
            "candidate_signal": None,
            "entry_style": "fresh_cross",
            "regime": "balance",
            "mp_status": "ready",
            "mp_periods": 8,
            "signal_validation": "waiting_trigger",
        }
    ]

    status = asyncio.run(agent.run_once(force=False))

    retained_row = next(row for row in status["futures_watchlist"] if row["symbol"] == "MCX:NICKEL26JULFUT")
    fresh_row = next(row for row in status["futures_watchlist"] if row["symbol"] == "MCX:GOLD26AUGFUT")
    refreshed = agent.get_status(refresh=True)

    assert status["last_run_at"] is not None
    assert retained_row["runtime_retained"] is True
    assert fresh_row.get("runtime_retained") is not True
    assert "retained 1 futures rows" in status["last_message"]
    assert refreshed["last_run_at"] == status["last_run_at"]
    assert refreshed["last_message"] == status["last_message"]


def test_scan_timeout_scales_with_symbol_count(monkeypatch) -> None:
    monkeypatch.setattr(commodity_module, "DEFAULT_COMMODITY_SCAN_TIMEOUT_SECONDS", 120)
    monkeypatch.setattr(commodity_module, "DEFAULT_COMMODITY_SYMBOL_SCAN_TIMEOUT_SECONDS", 12)
    monkeypatch.setattr(commodity_module, "DEFAULT_COMMODITY_SCAN_TIMEOUT_HEADROOM_SECONDS", 60)

    agent = CommodityStrategyAgent()
    agent.update_symbols(
        [
            "MCX:GOLD26AUGFUT",
            "MCX:SILVERM26AUGFUT",
            "MCX:CRUDEOIL26JULFUT",
            "MCX:NATURALGAS26JULFUT",
            "MCX:COPPER26JULFUT",
            "MCX:ALUMINI26JULFUT",
            "MCX:ZINCMINI26JULFUT",
            "MCX:NICKEL26JULFUT",
        ]
    )

    assert agent._scan_timeout_seconds() == 156

    agent.update_symbols(["MCX:GOLD26AUGFUT"])
    assert agent._scan_timeout_seconds() == 120


def test_get_status_does_not_refresh_store_while_scan_running() -> None:
    agent = CommodityStrategyAgent()
    agent.update_symbols(["MCX:GOLD26AUGFUT"])
    agent._last_run_at = "2026-07-16T14:57:02+05:30"
    agent._last_message = "Commodity agent running continuously."
    agent._running = True

    stale_state = commodity_module._default_saved_state()
    stale_state["control"]["last_run_at"] = "2026-07-16T13:52:42+05:30"
    stale_state["control"]["last_message"] = "Stale saved state"
    commodity_module._save_state(stale_state)

    status = agent.get_status(refresh=True)

    assert status["last_run_at"] == "2026-07-16T14:57:02+05:30"
    assert status["last_message"] == "Commodity agent running continuously."
