from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paper_engine.commodity_strategy_agent as commodity_module
from paper_engine.commodity_strategy_agent import (
    CommodityStrategyAgent,
    _normalize_symbols,
    evaluate_commodity_signal,
)


UTC = timezone.utc


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
