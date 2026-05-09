from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import paper_engine.commodity_strategy_agent as commodity_module
from paper_engine.commodity_strategy_agent import (
    CommodityStrategyAgent,
    _normalize_symbols,
    evaluate_commodity_signal,
)


UTC = timezone.utc


@pytest.fixture(autouse=True)
def _fake_runtime_state_store(monkeypatch):
    store: dict[str, object | None] = {
        "payload": None,
        "updated_at": None,
    }

    def _load_runtime_state(_state_key: str):
        return copy.deepcopy(store["payload"]), store["updated_at"]

    def _save_runtime_state(_state_key: str, payload: dict):
        store["payload"] = copy.deepcopy(payload)
        store["updated_at"] = datetime.now(UTC)
        return store["updated_at"]

    monkeypatch.setattr(commodity_module, "load_runtime_state", _load_runtime_state)
    monkeypatch.setattr(commodity_module, "save_runtime_state", _save_runtime_state)
    return store


def _build_candles(closes: list[float]) -> list[dict]:
    start = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)
    candles: list[dict] = []
    for index, close in enumerate(closes):
        candles.append(
            {
                "time": (start + timedelta(minutes=15 * index)).isoformat(),
                "open": close - 1.0,
                "high": close + 2.0,
                "low": close - 2.0,
                "close": close,
                "volume": 1000 + index,
            }
        )
    return candles


def test_normalize_symbols_deduplicates_and_requires_exchange_prefix() -> None:
    assert _normalize_symbols(
        ["mcx:gold26junfut", " MCX:SILVERMIC26JUNFUT ", "INVALID", "", "nse:nifty50-index"]
    ) == ["MCX:GOLD26JUNFUT", "MCX:SILVERM26JUNFUT", "NSE:NIFTY50-INDEX"]


def test_evaluate_commodity_signal_detects_bullish_macd_zero_cross() -> None:
    closes = [120.0 - (index * 1.1) for index in range(38)] + [78.0, 182.0]
    signal = evaluate_commodity_signal(_build_candles(closes))

    assert signal["signal"] == "BUY"
    assert signal["reason"] == "macd_zero_cross_up"
    assert signal["regime"] == "bullish"
    assert signal["macd"] is not None
    assert signal["atr"] is not None


def test_evaluate_commodity_signal_detects_bearish_macd_zero_cross() -> None:
    closes = [100.0 + (index * 4.0) for index in range(20)] + [176.0 - ((index + 1) * 2.0) for index in range(20)]
    signal = evaluate_commodity_signal(_build_candles(closes))

    assert signal["signal"] == "SELL"
    assert signal["reason"] == "macd_zero_cross_down"
    assert signal["regime"] == "bearish"


def test_evaluate_commodity_signal_reports_insufficient_data() -> None:
    signal = evaluate_commodity_signal(_build_candles([100.0] * 10))

    assert signal["signal"] is None
    assert signal["reason"] == "insufficient_data"


def test_commodity_options_pe_cross_does_not_require_ce_quadrant(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "commodity_strategy.json"
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", config_path)

    ce_closes = [180.0 - (index * 1.5) for index in range(40)]
    pe_closes = [100.0 + (index * 4.0) for index in range(20)] + [
        176.0 - ((index + 1) * 2.0) for index in range(20)
    ]

    async def fake_load_candles(**kwargs):
        option_type = kwargs["option_type"]
        return _build_candles(ce_closes if option_type == "CE" else pe_closes)

    monkeypatch.setattr(commodity_module.option_history_service, "load_candles", fake_load_candles)

    agent = CommodityStrategyAgent()
    row = {
        "symbol": "MCX:GOLD26JUNFUT",
        "underlying": "GOLD",
        "expiry": "2099-06-26",
        "ce": {
            "instrument_key": "MCX_FO|GOLD_CE",
            "trading_symbol": "GOLD CE",
            "strike": 152000.0,
            "option_type": "CE",
            "ltp": 900.0,
            "is_liquid": True,
        },
        "pe": {
            "instrument_key": "MCX_FO|GOLD_PE",
            "trading_symbol": "GOLD PE",
            "strike": 152000.0,
            "option_type": "PE",
            "ltp": 850.0,
            "is_liquid": True,
        },
    }

    analyzed = asyncio.run(agent._analyze_option_row(row))

    assert analyzed is not None
    assert analyzed["regime"] == "dead_zone"
    assert analyzed["signal_side"] == "PE"
    assert analyzed["signal_reason"] == "pe_macd_zero_cross"
    assert analyzed["pe_cross"] is True


def test_filter_closed_interval_rows_drops_incomplete_tail_bar() -> None:
    start = datetime(2026, 4, 16, 9, 15, tzinfo=UTC)
    candles = [
        {"time": (start + timedelta(minutes=15 * index)).isoformat(), "close": 100.0 + index}
        for index in range(3)
    ]

    filtered = commodity_module._filter_closed_interval_rows(  # type: ignore[attr-defined]
        candles,
        interval="15minute",
        now=start + timedelta(minutes=37),
    )

    assert len(filtered) == 2
    assert filtered[-1]["time"] == (start + timedelta(minutes=15)).isoformat()


def test_commodity_symbols_persist_to_config_file(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "commodity_strategy.json"
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", config_path)

    agent = CommodityStrategyAgent()
    agent.update_symbols(
        ["MCX:GOLD26JUNFUT", "MCX:CRUDEOIL26MAYFUT"],
        selected_option_expiries={"MCX:GOLD26JUNFUT": "2099-05-27"},
    )

    reloaded = CommodityStrategyAgent()

    assert config_path.exists()
    assert reloaded.get_symbols() == ["MCX:GOLD26JUNFUT", "MCX:CRUDEOIL26MAYFUT"]
    assert reloaded.get_selected_option_expiries() == {"MCX:GOLD26JUNFUT": "2099-05-27"}


def test_commodity_runtime_persists_orders_positions_and_commentary(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "commodity_strategy.json"
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", config_path)

    agent = CommodityStrategyAgent()
    agent.update_symbols(
        ["MCX:SILVERMIC26JUNFUT", "MCX:NATURALGAS26MAYFUT"],
        selected_option_expiries={"MCX:SILVERM26JUNFUT": "2099-05-26"},
    )
    agent._runtime.futures_watchlist = [  # type: ignore[attr-defined]
        {"symbol": "MCX:SILVERM26JUNFUT", "signal": "BUY", "price": 123.4}
    ]
    agent._runtime.positions["MCX:SILVERM26JUNFUT"] = commodity_module.CommodityPositionState(  # type: ignore[attr-defined]
        position_key="MCX:SILVERM26JUNFUT",
        symbol="MCX:SILVERM26JUNFUT",
        live_symbol="MCX:SILVERM26JUNFUT",
        underlying="SILVERM",
        strategy_key="commodity_futures",
        strategy_title="Strategy 2 · Futures",
        instrument_type="FUT",
        action="BUY",
        qty=5,
        lots=1,
        lot_size=5,
        entry_price=120.0,
        current_price=123.4,
        stop_price=118.0,
        target_price=128.0,
        regime="bullish",
        signal_reason="macd_zero_cross_up_mp_trend_up",
        atr=2.4,
        macd_value=0.32,
        mp_poc=121.0,
        mp_vah=124.0,
        mp_val=118.0,
        entered_at="2026-04-02T10:00:00+05:30",
        entry_bar_time="2026-04-02T09:30:00+05:30",
        contract_unit_label="5 kg contract",
        quote_unit_label="Rs / kg",
        display_name="Silver Mini",
        initial_qty=5,
        peak_price=123.4,
    )
    agent._runtime.orders.insert(0, {  # type: ignore[attr-defined]
        "time": "2026-04-02T10:00:00+05:30",
        "order_id": "ord-1",
        "symbol": "MCX:SILVERM26JUNFUT",
        "action": "BUY",
        "qty": 5,
        "lots": 1,
        "lot_size": 5,
        "order_type": "MARKET",
        "status": "FILLED",
        "fill_price": 120.0,
        "reason": "macd_zero_cross_up_mp_trend_up",
        "flow": "entry",
    })
    agent._append_commentary("trade", "ENTRY MCX:SILVERM26JUNFUT BUY @120.00")  # type: ignore[attr-defined]
    agent._append_report()  # type: ignore[attr-defined]
    agent._persist_state()  # type: ignore[attr-defined]

    reloaded = CommodityStrategyAgent()

    assert reloaded.get_symbols() == ["MCX:SILVERM26JUNFUT", "MCX:NATURALGAS26MAYFUT"]
    assert reloaded.get_selected_option_expiries() == {"MCX:SILVERM26JUNFUT": "2099-05-26"}
    assert reloaded.get_status()["watchlist"][0]["symbol"] == "MCX:SILVERM26JUNFUT"
    assert reloaded.get_positions()[0]["symbol"] == "MCX:SILVERM26JUNFUT"
    assert reloaded.get_positions()[0]["lot_size"] == 5
    assert reloaded.get_positions()[0]["lots"] == 1
    assert reloaded.get_orders()[0]["order_id"] == "ord-1"
    assert reloaded.get_reports()[0]["tracked_symbols"] == 2
    assert reloaded.get_status()["commentary"][0]["message"] == "ENTRY MCX:SILVERM26JUNFUT BUY @120.00"


def test_commodity_symbols_refresh_from_runtime_store_across_instances(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "commodity_strategy.json"
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", config_path)

    writer = CommodityStrategyAgent()
    reader = CommodityStrategyAgent()

    writer.update_symbols(
        ["MCX:GOLD26JUNFUT", "MCX:CRUDEOIL26MAYFUT"],
        selected_option_expiries={"MCX:GOLD26JUNFUT": "2099-05-27"},
    )

    assert reader.get_symbols() == ["MCX:GOLD26JUNFUT", "MCX:CRUDEOIL26MAYFUT"]
    assert reader.get_selected_option_expiries() == {"MCX:GOLD26JUNFUT": "2099-05-27"}


def test_selected_option_setup_persists_lookup_symbol(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "commodity_strategy.json"
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", config_path)

    async def _fake_catalog(symbols, selected_option_expiries=None, selected_option_lookup_symbols=None):
        return {
            "contracts": [
                {
                    "symbol": "MCX:GOLD26JUNFUT",
                    "expiry_mappings": [
                        {"expiry": "2099-05-27", "lookup_symbol": "MCX:GOLD26AUGFUT"},
                    ],
                    "active_lookup_symbol": "MCX:GOLD26AUGFUT",
                    "lookup_symbol": "MCX:GOLD26JUNFUT",
                    "default_lookup_symbol": "MCX:GOLD26JUNFUT",
                }
            ]
        }

    monkeypatch.setattr(
        commodity_module.commodity_atm_watchlist_service,
        "get_contract_catalog",
        _fake_catalog,
    )

    agent = CommodityStrategyAgent()
    agent.update_symbols(["MCX:GOLD26JUNFUT"])

    result = asyncio.run(
        agent.update_selected_option_expiries({"MCX:GOLD26JUNFUT": "2099-05-27"})
    )

    reloaded = CommodityStrategyAgent()

    assert result["selected_option_expiries"] == {"MCX:GOLD26JUNFUT": "2099-05-27"}
    assert result["selected_option_lookup_symbols"] == {"MCX:GOLD26JUNFUT": "MCX:GOLD26AUGFUT"}
    assert reloaded.get_selected_option_expiries() == {"MCX:GOLD26JUNFUT": "2099-05-27"}
    assert reloaded.get_selected_option_lookup_symbols() == {"MCX:GOLD26JUNFUT": "MCX:GOLD26AUGFUT"}


def test_legacy_selected_option_setup_backfills_lookup_symbol(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "commodity_strategy.json"
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", config_path)

    async def _fake_catalog(symbols, selected_option_expiries=None, selected_option_lookup_symbols=None):
        return {
            "contracts": [
                {
                    "symbol": "MCX:GOLD26JUNFUT",
                    "expiry_mappings": [
                        {"expiry": "2099-05-27", "lookup_symbol": "MCX:GOLD26AUGFUT"},
                    ],
                    "active_lookup_symbol": "MCX:GOLD26AUGFUT",
                    "lookup_symbol": "MCX:GOLD26JUNFUT",
                    "default_lookup_symbol": "MCX:GOLD26JUNFUT",
                }
            ]
        }

    monkeypatch.setattr(
        commodity_module.commodity_atm_watchlist_service,
        "get_contract_catalog",
        _fake_catalog,
    )

    legacy_agent = CommodityStrategyAgent()
    legacy_agent.update_symbols(
        ["MCX:GOLD26JUNFUT"],
        selected_option_expiries={"MCX:GOLD26JUNFUT": "2099-05-27"},
    )

    result = asyncio.run(legacy_agent.ensure_selected_option_setup_locks())
    reloaded = CommodityStrategyAgent()

    assert result == {"MCX:GOLD26JUNFUT": "MCX:GOLD26AUGFUT"}
    assert reloaded.get_selected_option_lookup_symbols() == {"MCX:GOLD26JUNFUT": "MCX:GOLD26AUGFUT"}


def test_commodity_symbol_save_preserves_latest_expiry_map_on_stale_instance(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "commodity_strategy.json"
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", config_path)

    fresh = CommodityStrategyAgent()
    stale = CommodityStrategyAgent()

    fresh.update_symbols(
        ["MCX:GOLD26JUNFUT"],
        selected_option_expiries={"MCX:GOLD26JUNFUT": "2099-05-27"},
    )

    result = stale.update_symbols(["MCX:GOLD26JUNFUT", "MCX:CRUDEOIL26MAYFUT"])

    assert result["symbols"] == ["MCX:GOLD26JUNFUT", "MCX:CRUDEOIL26MAYFUT"]
    assert result["selected_option_expiries"] == {"MCX:GOLD26JUNFUT": "2099-05-27"}


def test_commodity_agent_runs_automatically_until_kill_then_requires_restart(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "commodity_strategy.json"
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", config_path)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        agent = CommodityStrategyAgent()

        async def _idle_loop() -> None:
            await asyncio.Future()

        agent._loop = _idle_loop  # type: ignore[method-assign]

        loop.run_until_complete(agent.start())
        assert agent.get_status()["auto_run_enabled"] is True
        assert agent.get_status()["loop_active"] is True
        assert agent.get_status()["start_required"] is False

        activated = loop.run_until_complete(agent.set_kill_switch(True))
        assert activated["kill_switch_active"] is True
        assert activated["loop_active"] is False
        assert activated["start_required"] is True

        released = loop.run_until_complete(agent.set_kill_switch(False))
        assert released["kill_switch_active"] is False
        assert released["loop_active"] is False
        assert released["start_required"] is True

        restarted = loop.run_until_complete(agent.start_loop())
        assert restarted["loop_active"] is True
        assert restarted["start_required"] is False

        loop.run_until_complete(agent.stop())
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def test_commodity_watchlist_reports_signal_validation_and_strategy_metadata(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "commodity_strategy.json"
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", config_path)

    agent = CommodityStrategyAgent()
    agent.update_symbols(["MCX:GOLD26JUNFUT"])
    agent._runtime.futures_watchlist = agent._decorate_futures_rows(  # type: ignore[attr-defined]
        [
            {
                "symbol": "MCX:GOLD26JUNFUT",
                "underlying": "GOLD",
                "signal": "BUY",
                "raw_signal": "BUY",
                "reason": "macd_zero_cross_up_mp_trend_up",
                "regime": "bullish",
                "mp_status": "ready",
                "mp_direction": "BUY",
                "mp_day_type": "trend_up",
                "mp_reason": "trend_up",
                "signal_validation_detail": "15-minute MACD cross matches the current MP gate.",
                "price": 152000.0,
                "atr": 350.0,
                "mp_poc": 151850.0,
                "mp_vah": 152120.0,
                "mp_val": 151620.0,
                "mp_ib_high": 152050.0,
                "mp_ib_low": 151700.0,
                "bar_time": "2026-04-09T10:30:00+05:30",
            }
        ]
    )

    status = agent.get_status()

    assert status["strategy_agents"][0]["key"] == "commodity_futures"
    assert status["strategy_agents"][1]["key"] == "commodity_options"
    assert status["strategies"][0]["key"] == "commodity_futures"
    assert status["strategies"][1]["key"] == "commodity_options"
    assert status["strategies"][0]["timeframe"] == "15minute"
    assert status["strategies"][1]["timeframe"] == "30minute"
    assert status["config"]["lots_per_trade"] == 1
    assert status["watchlist"][0]["lot_size"] == 10
    assert status["watchlist"][0]["default_qty"] == 10
    assert status["watchlist"][0]["signal_validation"] == "ready"


def test_commodity_run_once_stops_on_invalid_fyers_session(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "commodity_strategy.json"
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", config_path)
    monkeypatch.setattr(commodity_module, "_in_commodity_hours", lambda _: True)

    async def fake_fyers_health(*, force: bool = False):
        return {
            "connected": False,
            "valid": False,
            "status": "expired_reconnect_required",
            "message": "Saved Fyers access token is invalid.",
        }

    agent = CommodityStrategyAgent()
    agent.update_symbols(["MCX:GOLD26JUNFUT"])
    monkeypatch.setattr(commodity_module, "get_fyers_token_health", fake_fyers_health)

    status = asyncio.run(agent.run_once(force=False))

    assert status["last_error"] is not None
    assert "No valid Fyers session is available for the commodity scan." in status["last_message"]
    assert status["data_health"]["fyers_token_health"]["valid"] is False


def test_commodity_run_once_retains_previous_watchlists_on_transient_scan_gap(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "commodity_strategy.json"
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", config_path)
    monkeypatch.setattr(commodity_module, "_in_commodity_hours", lambda _: True)

    async def fake_fyers_health(*, force: bool = False):
        return {
            "connected": True,
            "valid": True,
            "status": "valid_session",
            "message": "ok",
        }

    async def fake_ensure_fyers_session(*, force_validate: bool = False):
        return True

    class _FakeAdapter:
        pass

    async def fake_get_fyers_adapter(self):
        return _FakeAdapter()

    async def fake_get_ltp(self, adapter, symbols):
        return {"MCX:GOLD26JUNFUT": 101.5}

    async def fake_analyze_futures_symbol(self, symbol, live_ltp):
        return None

    async def fake_build_option_watchlist(self):
        return []

    async def fake_manage_positions(self, adapter, futures_rows, option_rows, option_quote_map=None):
        return None

    class _FakeDescriptor:
        def __init__(self, key: str):
            self.key = key
            self.title = key
            self.timeframe = "test"
            self.instrument_scope = "test"
            self.execution_mode = "paper_execution"
            self.position_cap = 0

    class _FakeLane:
        def __init__(self, key: str):
            self.descriptor = _FakeDescriptor(key)

        def ready_signals(self):
            return 0

        def open_positions(self):
            return 0

        def build_status_payload(self):
            return {
                "key": self.descriptor.key,
                "title": self.descriptor.title,
                "timeframe": self.descriptor.timeframe,
                "instrument_scope": self.descriptor.instrument_scope,
                "execution_mode": self.descriptor.execution_mode,
                "position_cap": self.descriptor.position_cap,
                "tracked_symbols": 0,
                "open_positions": 0,
                "ready_signals": 0,
            }

        async def run_entries(self, rows):
            return None

    monkeypatch.setattr(commodity_module, "get_fyers_token_health", fake_fyers_health)
    monkeypatch.setattr(commodity_module, "ensure_fyers_session", fake_ensure_fyers_session)
    monkeypatch.setattr(CommodityStrategyAgent, "_get_fyers_adapter", fake_get_fyers_adapter)
    monkeypatch.setattr(CommodityStrategyAgent, "_safe_get_ltp", fake_get_ltp)
    monkeypatch.setattr(CommodityStrategyAgent, "_analyze_futures_symbol", fake_analyze_futures_symbol)
    monkeypatch.setattr(CommodityStrategyAgent, "_build_option_watchlist", fake_build_option_watchlist)
    monkeypatch.setattr(CommodityStrategyAgent, "_manage_positions", fake_manage_positions)
    monkeypatch.setattr(
        CommodityStrategyAgent,
        "_strategy_agents",
        lambda self: [_FakeLane("commodity_futures"), _FakeLane("commodity_options")],
    )
    monkeypatch.setattr(
        commodity_module.option_history_service,
        "get_health_snapshot",
        lambda: {
            "failure_count": 1,
            "success_count": 0,
            "brokers": {"fyers": {"failure": 1, "last_detail": "Fyers data API error 429: request limit reached"}},
        },
    )

    agent = CommodityStrategyAgent()
    agent.update_symbols(
        ["MCX:GOLD26JUNFUT"],
        selected_option_expiries={"MCX:GOLD26JUNFUT": "2099-05-27"},
    )
    agent._selected_option_lookup_symbols = {"MCX:GOLD26JUNFUT": "MCX:GOLD26JUNFUT"}  # type: ignore[attr-defined]
    agent._runtime.futures_watchlist = [  # type: ignore[attr-defined]
        {
            "symbol": "MCX:GOLD26JUNFUT",
            "underlying": "GOLD",
            "price": 100.0,
            "previous_close": 98.0,
            "signal_validation": "waiting_cross",
        }
    ]
    agent._runtime.option_watchlist = [  # type: ignore[attr-defined]
        {
            "symbol": "MCX:GOLD26JUNFUT",
            "selected_expiry": "2099-05-27",
            "active_expiry": "2099-05-27",
            "selected_lookup_symbol": "MCX:GOLD26JUNFUT",
            "lookup_symbol": "MCX:GOLD26JUNFUT",
            "signal_validation": "warming_up",
        }
    ]

    status = asyncio.run(agent.run_once(force=False))

    assert len(status["futures_watchlist"]) == 1
    assert len(status["option_watchlist"]) == 1
    assert status["futures_watchlist"][0]["runtime_retained"] is True
    assert status["option_watchlist"][0]["runtime_retained"] is True
    assert status["futures_watchlist"][0]["price"] == 101.5
    assert "Reused the last good snapshot" in status["last_message"]


def test_load_history_prefers_resolved_upstox_future_before_fyers_symbol(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "commodity_strategy.json"
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", config_path)

    async def fake_resolve(symbol: str):
        assert symbol == "MCX:GOLD26JUNFUT"
        return {"instrument_key": "MCX_FO|123"}

    requests: list[tuple[str, str]] = []

    async def fake_fetch(*, instrument_key, from_date, to_date, interval):
        requests.append((instrument_key, interval))
        if instrument_key == "MCX_FO|123":
            return [
                {
                    "time": "2026-04-16T09:15:00+05:30",
                    "open": 1.0,
                    "high": 2.0,
                    "low": 0.5,
                    "close": 1.5,
                    "volume": 10,
                }
            ]
        return []

    monkeypatch.setattr(commodity_module, "resolve_upstox_mcx_future", fake_resolve)
    monkeypatch.setattr(commodity_module.option_history_service, "_fetch_broker_candles", fake_fetch)

    agent = CommodityStrategyAgent()
    rows = asyncio.run(agent._load_history("MCX:GOLD26JUNFUT", interval="15minute", lookback_days=1))

    assert len(rows) == 1
    assert requests[0] == ("MCX_FO|123", "1minute")
    assert all(instrument_key != "MCX:GOLD26JUNFUT" for instrument_key, _ in requests)


def test_load_history_falls_back_to_fyers_symbol_when_upstox_rows_are_missing(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "commodity_strategy.json"
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", config_path)

    async def fake_resolve(_symbol: str):
        return {"instrument_key": "MCX_FO|123"}

    requests: list[tuple[str, str]] = []

    async def fake_fetch(*, instrument_key, from_date, to_date, interval):
        requests.append((instrument_key, interval))
        if instrument_key == "MCX:GOLD26JUNFUT":
            return [
                {
                    "time": "2026-04-16T09:15:00+05:30",
                    "open": 1.0,
                    "high": 2.0,
                    "low": 0.5,
                    "close": 1.5,
                    "volume": 10,
                }
            ]
        return []

    monkeypatch.setattr(commodity_module, "resolve_upstox_mcx_future", fake_resolve)
    monkeypatch.setattr(commodity_module.option_history_service, "_fetch_broker_candles", fake_fetch)

    agent = CommodityStrategyAgent()
    rows = asyncio.run(agent._load_history("MCX:GOLD26JUNFUT", interval="30minute", lookback_days=1))

    assert len(rows) == 1
    assert requests[:2] == [("MCX_FO|123", "30minute"), ("MCX:GOLD26JUNFUT", "30minute")]


def test_safe_get_ltp_prefers_upstox_quotes_then_fyers_fallback(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "commodity_strategy.json"
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", config_path)

    async def fake_upstox_quotes(symbols: list[str]) -> dict[str, float]:
        assert symbols == ["MCX:GOLD26JUNFUT", "MCX:CRUDEOIL26MAYFUT"]
        return {"MCX:GOLD26JUNFUT": 101.5}

    class _Adapter:
        def __init__(self) -> None:
            self.requests: list[list[str]] = []

        async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
            self.requests.append(list(symbols))
            return {"MCX:CRUDEOIL26MAYFUT": 86.2}

    monkeypatch.setattr(commodity_module, "load_upstox_mcx_quotes", fake_upstox_quotes)

    agent = CommodityStrategyAgent()
    adapter = _Adapter()

    quotes = asyncio.run(agent._safe_get_ltp(adapter, ["MCX:GOLD26JUNFUT", "MCX:CRUDEOIL26MAYFUT"]))

    assert quotes == {"MCX:GOLD26JUNFUT": 101.5, "MCX:CRUDEOIL26MAYFUT": 86.2}
    assert adapter.requests == [["MCX:CRUDEOIL26MAYFUT"]]


def test_safe_get_ltp_backs_off_after_rate_limit(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "commodity_strategy.json"
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", config_path)

    async def fake_upstox_quotes(symbols: list[str]) -> dict[str, float]:
        return {}

    class _Adapter:
        def __init__(self) -> None:
            self.requests: list[list[str]] = []

        async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
            self.requests.append(list(symbols))
            raise RuntimeError("Fyers data API error 429: request limit reached")

    monkeypatch.setattr(commodity_module, "load_upstox_mcx_quotes", fake_upstox_quotes)

    agent = CommodityStrategyAgent()
    adapter = _Adapter()

    first = asyncio.run(agent._safe_get_ltp(adapter, ["MCX:GOLD26JUNFUT"]))
    second = asyncio.run(agent._safe_get_ltp(adapter, ["MCX:GOLD26JUNFUT"]))

    assert first == {}
    assert second == {}
    assert adapter.requests == [["MCX:GOLD26JUNFUT"]]


def test_analyze_futures_symbol_uses_continuation_signal_when_mp_aligns(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "commodity_strategy.json"
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", config_path)

    async def fake_load_history(self, symbol: str, *, interval: str, lookback_days: int = 0):
        assert symbol == "MCX:CRUDEOIL26MAYFUT"
        assert interval == "15minute"
        return _build_candles([8700.0 + (index * 2.0) for index in range(40)])

    def fake_evaluate(*args, **kwargs):
        return {
            "signal": None,
            "reason": "no_cross",
            "regime": "bearish",
            "latest_close": 8650.0,
            "previous_close": 8680.0,
            "macd": -18.2,
            "macd_signal": -16.5,
            "macd_histogram": -1.7,
            "atr": 72.0,
            "bar_time": "2026-04-16T19:00:00+05:30",
            "recent_cross_signal": "SELL",
            "recent_cross_bars_ago": 2,
            "continuation_signal": "SELL",
            "continuation_reason": "macd_continuation_breakdown_down",
        }

    monkeypatch.setattr(CommodityStrategyAgent, "_load_history", fake_load_history)
    monkeypatch.setattr(commodity_module, "evaluate_commodity_signal", fake_evaluate)
    monkeypatch.setattr(
        commodity_module,
        "_latest_session_rows",
        lambda candles: (list(candles), datetime(2026, 4, 16, 19, 0, tzinfo=UTC).date()),
    )
    monkeypatch.setattr(CommodityStrategyAgent, "_build_market_profile", lambda self, symbol, rows: object())
    monkeypatch.setattr(
        CommodityStrategyAgent,
        "_classify_market_profile",
        lambda self, **kwargs: ("SELL", "trend_down", "mp_trend_down"),
    )

    agent = CommodityStrategyAgent()

    row = asyncio.run(agent._analyze_futures_symbol("MCX:CRUDEOIL26MAYFUT", 8648.0))

    assert row is not None
    assert row["signal"] == "SELL"
    assert row["entry_style"] == "continuation"
    assert row["reason"] == "macd_continuation_breakdown_down_mp_trend_down"
    assert "continuation" in row["signal_validation_detail"]


def test_futures_reversal_exit_waits_for_min_hold_bars(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "commodity_strategy.json"
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", config_path)

    agent = CommodityStrategyAgent()
    position = commodity_module.CommodityPositionState(
        position_key="commodity_futures:MCX:NATURALGAS26MAYFUT",
        symbol="MCX:NATURALGAS26MAYFUT",
        live_symbol="MCX:NATURALGAS26MAYFUT",
        underlying="NATURALGAS",
        strategy_key="commodity_futures",
        strategy_title="Strategy 2 · Futures",
        instrument_type="FUT",
        action="BUY",
        qty=1250,
        lots=1,
        lot_size=1250,
        entry_price=245.0,
        current_price=245.0,
        stop_price=242.0,
        target_price=251.0,
        regime="bullish",
        signal_reason="macd_zero_cross_up_mp_trend_up",
        atr=2.5,
        macd_value=0.8,
        mp_poc=244.5,
        mp_vah=246.2,
        mp_val=243.8,
        entered_at="2026-04-16T09:31:00+05:30",
        entry_bar_time="2026-04-16T09:30:00+05:30",
        contract_unit_label="1250 MMBtu",
        quote_unit_label="Rs / MMBtu",
        display_name="Natural Gas",
        initial_qty=1250,
        peak_price=245.0,
        entry_style="fresh_cross",
        last_reviewed_bar_time="2026-04-16T09:30:00+05:30",
    )
    agent._runtime.positions[position.position_key] = position  # type: ignore[attr-defined]

    early_row = {
        "symbol": "MCX:NATURALGAS26MAYFUT",
        "price": 246.8,
        "raw_signal": "SELL",
        "mp_direction": "SELL",
        "bar_time": "2026-04-16T10:15:00+05:30",
        "macd": -0.3,
        "mp_poc": 245.0,
        "mp_vah": 246.5,
        "mp_val": 244.0,
    }

    asyncio.run(agent._manage_positions(object(), [early_row], []))

    assert position.position_key in agent._runtime.positions  # type: ignore[attr-defined]
    assert agent.get_orders() == []

    mature_row = dict(early_row)
    mature_row["bar_time"] = "2026-04-16T10:30:00+05:30"
    asyncio.run(agent._manage_positions(object(), [mature_row], []))

    assert position.position_key not in agent._runtime.positions  # type: ignore[attr-defined]
    assert agent.get_orders()[0]["reason"] == "macd_reversal"


def test_futures_target_hit_arms_trailing_runner_instead_of_full_exit(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "commodity_strategy.json"
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", config_path)

    agent = CommodityStrategyAgent()
    position = commodity_module.CommodityPositionState(
        position_key="commodity_futures:MCX:CRUDEOIL26MAYFUT",
        symbol="MCX:CRUDEOIL26MAYFUT",
        live_symbol="MCX:CRUDEOIL26MAYFUT",
        underlying="CRUDEOIL",
        strategy_key="commodity_futures",
        strategy_title="Strategy 2 · Futures",
        instrument_type="FUT",
        action="BUY",
        qty=100,
        lots=1,
        lot_size=100,
        entry_price=100.0,
        current_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        regime="bullish",
        signal_reason="macd_zero_cross_up_mp_trend_up",
        atr=2.5,
        macd_value=0.5,
        mp_poc=99.5,
        mp_vah=101.0,
        mp_val=98.8,
        entered_at="2026-04-16T10:01:00+05:30",
        entry_bar_time="2026-04-16T10:00:00+05:30",
        contract_unit_label="100 barrel contract",
        quote_unit_label="Rs / barrel",
        display_name="Crude Oil",
        initial_qty=100,
        peak_price=100.0,
        entry_style="fresh_cross",
        last_reviewed_bar_time="2026-04-16T10:00:00+05:30",
    )
    agent._runtime.positions[position.position_key] = position  # type: ignore[attr-defined]

    target_row = {
        "symbol": "MCX:CRUDEOIL26MAYFUT",
        "price": 111.0,
        "raw_signal": None,
        "mp_direction": "BUY",
        "bar_time": "2026-04-16T11:00:00+05:30",
        "macd": 0.9,
        "mp_poc": 108.5,
        "mp_vah": 110.5,
        "mp_val": 107.8,
    }

    asyncio.run(agent._manage_positions(object(), [target_row], []))

    current = agent._runtime.positions[position.position_key]  # type: ignore[attr-defined]
    assert current.target_reached is True
    assert current.stop_price == pytest.approx(106.0)
    assert agent.get_orders() == []

    trail_exit_row = dict(target_row)
    trail_exit_row["price"] = 105.5
    asyncio.run(agent._manage_positions(object(), [trail_exit_row], []))

    assert position.position_key not in agent._runtime.positions  # type: ignore[attr-defined]
    assert agent.get_orders()[0]["reason"] == "trail_stop"


def test_overlay_live_option_quotes_prefers_direct_ltp_for_trade_symbol() -> None:
    rows = [
        {
            "symbol": "MCX:NATURALGAS26MAYFUT",
            "trade_symbol": "MCX:NATURALGAS26APR250CE",
            "trade_price": 4.95,
            "ce_symbol": "MCX:NATURALGAS26APR250CE",
            "ce_trade_price": 4.95,
            "pe_symbol": "MCX:NATURALGAS26APR250PE",
            "pe_trade_price": 3.10,
            "ce": {"strike": 250.0},
            "pe": {"strike": 250.0},
        }
    ]

    enriched = CommodityStrategyAgent._overlay_live_option_quotes(
        rows,
        {"MCX:NATURALGAS26APR250CE": 8.0},
    )

    assert enriched[0]["trade_price"] == pytest.approx(8.0)
    assert enriched[0]["trade_price_source"] == "direct_ltp"
    assert enriched[0]["ce_trade_price"] == pytest.approx(8.0)
    assert enriched[0]["ce"]["live_ltp"] == pytest.approx(8.0)
    assert enriched[0]["ce"]["price_source"] == "direct_ltp"


def test_option_exit_uses_direct_quote_over_stale_watchlist_price(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "commodity_strategy.json"
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", config_path)

    agent = CommodityStrategyAgent()
    position = commodity_module.CommodityPositionState(
        position_key="commodity_options:MCX:NATURALGAS26APR250CE",
        symbol="MCX:NATURALGAS26MAYFUT",
        live_symbol="MCX:NATURALGAS26APR250CE",
        underlying="NATURALGAS",
        strategy_key="commodity_options",
        strategy_title="Strategy 1 · Options",
        instrument_type="OPT",
        action="BUY",
        qty=11250,
        lots=9,
        lot_size=1250,
        entry_price=6.65,
        current_price=6.65,
        stop_price=4.99,
        target_price=9.50,
        regime="bullish",
        signal_reason="option_signal",
        atr=None,
        macd_value=0.32,
        mp_poc=None,
        mp_vah=None,
        mp_val=None,
        entered_at="2026-04-20T09:00:00+05:30",
        entry_bar_time="2026-04-20T09:00:00+05:30",
        contract_unit_label="1250 MMBtu",
        quote_unit_label="Rs / MMBtu",
        display_name="Natural Gas",
        initial_qty=11250,
        peak_price=6.65,
        expiry="2026-05-29",
        strike=250.0,
        option_type="CE",
    )
    agent._runtime.positions[position.position_key] = position  # type: ignore[attr-defined]

    option_row = {
        "symbol": "MCX:NATURALGAS26MAYFUT",
        "ce_symbol": "MCX:NATURALGAS26APR250CE",
        "ce_trade_price": 4.95,
        "pe_symbol": "MCX:NATURALGAS26APR250PE",
        "pe_trade_price": 3.10,
        "regime": "bullish",
        "ce": {"macd": 0.45},
        "pe": {"macd": -0.35},
    }

    asyncio.run(
        agent._manage_positions(
            object(),
            [],
            [option_row],
            option_quote_map={"MCX:NATURALGAS26APR250CE": 8.0},
        )
    )

    assert position.position_key in agent._runtime.positions  # type: ignore[attr-defined]
    assert position.current_price == pytest.approx(8.0)
    assert agent.get_orders() == []


def test_signal_audit_persists_across_instances(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "commodity_strategy.json"
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", config_path)

    agent = CommodityStrategyAgent()
    agent._runtime.signal_audit = [  # type: ignore[attr-defined]
        {
            "audit_key": "commodity_futures:MCX:GOLD26JUNFUT:2026-04-16T14:30:00+05:30:mp_conflict:SELL:continuation",
            "time": "2026-04-16T14:31:00+05:30",
            "lane": "commodity_futures",
            "symbol": "MCX:GOLD26JUNFUT",
            "underlying": "GOLD",
            "bar_time": "2026-04-16T14:30:00+05:30",
            "entry_style": "continuation",
            "signal": None,
            "raw_signal": None,
            "continuation_signal": "SELL",
            "recent_cross_signal": "SELL",
            "recent_cross_bars_ago": 3,
            "mp_direction": "BUY",
            "mp_day_type": "balance",
            "validation": "mp_conflict",
            "detail": "15-minute continuation fired SELL, but MP gate is BUY.",
            "price": 153420.0,
            "regime": "bearish",
            "runtime_retained": False,
        }
    ]
    agent._persist_state()  # type: ignore[attr-defined]

    reloaded = CommodityStrategyAgent()
    status = reloaded.get_status()

    assert len(status["signal_audit"]) == 1
    assert status["signal_audit"][0]["symbol"] == "MCX:GOLD26JUNFUT"
    assert status["signal_audit"][0]["validation"] == "mp_conflict"
    assert "audit_key" not in status["signal_audit"][0]
