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
                "time": (start + timedelta(minutes=30 * index)).isoformat(),
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


def test_evaluate_commodity_signal_detects_bullish_breakout() -> None:
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0,
              110.0, 111.0, 112.0, 113.0, 114.0, 115.0, 116.0, 117.0, 118.0, 119.0,
              120.0, 121.0, 122.0, 130.0]
    signal = evaluate_commodity_signal(_build_candles(closes))

    assert signal["signal"] == "BUY"
    assert signal["reason"] == "bullish_breakout"
    assert signal["regime"] == "bullish"
    assert signal["atr"] is not None


def test_evaluate_commodity_signal_detects_bearish_breakdown() -> None:
    closes = [130.0, 129.0, 128.0, 127.0, 126.0, 125.0, 124.0, 123.0, 122.0, 121.0,
              120.0, 119.0, 118.0, 117.0, 116.0, 115.0, 114.0, 113.0, 112.0, 111.0,
              110.0, 109.0, 108.0, 100.0]
    signal = evaluate_commodity_signal(_build_candles(closes))

    assert signal["signal"] == "SELL"
    assert signal["reason"] == "bearish_breakdown"
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
    agent._runtime.watchlist = [  # type: ignore[attr-defined]
        {"symbol": "MCX:SILVERM26JUNFUT", "signal": "BUY", "price": 123.4}
    ]
    agent._runtime.positions["MCX:SILVERM26JUNFUT"] = commodity_module.CommodityPositionState(  # type: ignore[attr-defined]
        symbol="MCX:SILVERM26JUNFUT",
        action="BUY",
        qty=1,
        entry_price=120.0,
        current_price=123.4,
        stop_price=118.0,
        target_price=128.0,
        regime="bullish",
        signal_reason="bullish_breakout",
        atr=2.4,
        entered_at="2026-04-02T10:00:00+05:30",
        entry_bar_time="2026-04-02T09:30:00+05:30",
    )
    agent._runtime.orders.insert(0, {  # type: ignore[attr-defined]
        "time": "2026-04-02T10:00:00+05:30",
        "order_id": "ord-1",
        "symbol": "MCX:SILVERM26JUNFUT",
        "action": "BUY",
        "qty": 1,
        "order_type": "MARKET",
        "status": "FILLED",
        "fill_price": 120.0,
        "reason": "bullish_breakout",
    })
    agent._append_commentary("trade", "ENTRY MCX:SILVERM26JUNFUT BUY @120.00")  # type: ignore[attr-defined]
    agent._append_report()  # type: ignore[attr-defined]
    agent._persist_state()  # type: ignore[attr-defined]

    reloaded = CommodityStrategyAgent()

    assert reloaded.get_symbols() == ["MCX:SILVERM26JUNFUT", "MCX:NATURALGAS26MAYFUT"]
    assert reloaded.get_selected_option_expiries() == {"MCX:SILVERM26JUNFUT": "2099-05-26"}
    assert reloaded.get_status()["watchlist"][0]["symbol"] == "MCX:SILVERM26JUNFUT"
    assert reloaded.get_positions()[0]["symbol"] == "MCX:SILVERM26JUNFUT"
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
