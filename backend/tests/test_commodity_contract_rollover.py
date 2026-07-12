from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

import paper_engine.commodity_strategy_agent as commodity_module
from market_data.upstox_commodity import select_active_mcx_future_contract


def _contract(root: str, expiry: date) -> dict:
    return {
        "exchange": "MCX",
        "segment": "MCX_FO",
        "instrument_type": "FUT",
        "instrument_key": f"MCX_FO|{root}|{expiry.isoformat()}",
        "underlying_symbol": root,
        "trading_symbol": f"{root} FUT {expiry:%d %b %y}".upper(),
        "expiry": int(datetime(expiry.year, expiry.month, expiry.day, tzinfo=timezone.utc).timestamp() * 1000),
        "lot_size": 1000,
    }


def test_front_contract_rolls_before_expiry_session() -> None:
    contracts = [
        _contract("ZINCMINI", date(2026, 7, 13)),
        _contract("ZINCMINI", date(2026, 8, 31)),
    ]

    before = select_active_mcx_future_contract(
        contracts,
        "MCX:ZINCMINI26JULFUT",
        session_date=date(2026, 7, 10),
    )
    expiry_session = select_active_mcx_future_contract(
        contracts,
        "MCX:ZINCMINI26JULFUT",
        session_date=date(2026, 7, 13),
    )

    assert before and before["symbol"] == "MCX:ZINCMINI26JULFUT"
    assert expiry_session and expiry_session["symbol"] == "MCX:ZINCMINI26AUGFUT"


def test_rollover_moves_open_position_and_preserves_risk_geometry(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", tmp_path / "commodity.json")
    monkeypatch.setattr(commodity_module, "load_runtime_state", lambda _key: (None, None))
    monkeypatch.setattr(commodity_module, "save_runtime_state", lambda _key, _payload: datetime.now(timezone.utc))
    monkeypatch.setattr(commodity_module, "record_paper_trade", lambda **_kwargs: None)

    audit_events: list[dict] = []

    async def fake_audit(**kwargs):
        audit_events.append(kwargs)

    monkeypatch.setattr(commodity_module, "record_audit_event", fake_audit)
    agent = commodity_module.CommodityStrategyAgent()
    old_symbol = "MCX:ZINCMINI26JUNFUT"
    position = commodity_module.CommodityPositionState(
        position_key=f"commodity_futures:{old_symbol}",
        symbol=old_symbol,
        live_symbol=old_symbol,
        underlying="ZINCMINI",
        strategy_key="commodity_futures",
        strategy_title="MP+OF Futures",
        instrument_type="FUT",
        action="BUY",
        qty=1000,
        lots=1,
        lot_size=1000,
        entry_price=365.0,
        current_price=376.0,
        stop_price=360.0,
        target_price=375.0,
        regime="trend_up",
        signal_reason="ib_break_up",
        atr=1.0,
        macd_value=None,
        mp_poc=365.0,
        mp_vah=366.0,
        mp_val=364.0,
        entered_at="2026-07-03T10:00:00+05:30",
        entry_bar_time="2026-07-03T10:00:00+05:30",
        contract_unit_label="1 MT mini contract",
        quote_unit_label="Rs / kg",
        display_name="Zinc Mini",
        initial_qty=1000,
        peak_price=376.0,
    )
    agent._runtime.positions[position.position_key] = position
    agent._runtime.portfolio._positions["seed"] = commodity_module.VirtualPosition(
        symbol=old_symbol,
        action="BUY",
        qty=1000,
        avg_price=365.0,
        current_price=376.0,
        instrument_type="FUT",
        opened_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
    )

    asyncio.run(
        agent._roll_futures_position(
            position.position_key,
            position,
            {
                "symbol": "MCX:ZINCMINI26JULFUT",
                "price": 380.0,
                "bar_time": "2026-07-13T09:00:00+05:30",
                "contract_expiry": "2026-07-31",
            },
        )
    )

    rolled = agent._runtime.positions["commodity_futures:MCX:ZINCMINI26JULFUT"]
    assert rolled.rollover_from_symbol == old_symbol
    assert rolled.rollover_count == 1
    assert rolled.stop_price == rolled.entry_price - 5.0
    assert rolled.target_price == rolled.entry_price + 10.0
    assert agent._runtime.rollover_events[0]["status"] == "completed"
    assert any(event["event_type"] == "contract_rollover" for event in audit_events)


def test_watchlist_stabilizer_uses_resolved_active_symbol(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", tmp_path / "commodity.json")
    monkeypatch.setattr(commodity_module, "load_runtime_state", lambda _key: (None, None))
    monkeypatch.setattr(commodity_module, "save_runtime_state", lambda _key, _payload: datetime.now(timezone.utc))
    agent = commodity_module.CommodityStrategyAgent()
    agent._symbols = ["MCX:ALUMINI26JUNFUT"]
    agent._active_contract_metadata = {
        "MCX:ALUMINI26JUNFUT": {"active_symbol": "MCX:ALUMINI26JULFUT"}
    }

    rows, retained = agent._stabilize_futures_watchlist(
        [{"symbol": "MCX:ALUMINI26JULFUT", "price": 340.0}],
        live_quotes={"MCX:ALUMINI26JULFUT": 340.0},
    )

    assert [row["symbol"] for row in rows] == ["MCX:ALUMINI26JULFUT"]
    assert retained == []


def test_closed_market_preparation_does_not_execute_rollover(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", tmp_path / "commodity.json")
    monkeypatch.setattr(commodity_module, "load_runtime_state", lambda _key: (None, None))
    monkeypatch.setattr(commodity_module, "save_runtime_state", lambda _key, _payload: datetime.now(timezone.utc))
    agent = commodity_module.CommodityStrategyAgent()
    called: list[bool] = []

    async def fake_active():
        return {"MCX:ZINCMINI26JUNFUT": "MCX:ZINCMINI26JULFUT"}

    async def fake_quotes(_adapter, _symbols):
        return {"MCX:ZINCMINI26JULFUT": 380.0}

    async def fake_analysis(symbol, _quote):
        return {"symbol": symbol, "price": 380.0, "bar_time": "2026-07-10T23:29:00+05:30"}

    async def fake_roll(_rows):
        called.append(True)

    monkeypatch.setattr(agent, "_active_futures_symbols", fake_active)
    monkeypatch.setattr(agent, "_safe_get_ltp", fake_quotes)
    monkeypatch.setattr(agent, "_analyze_futures_symbol", fake_analysis)
    monkeypatch.setattr(agent, "_reconcile_futures_rollovers", fake_roll)
    monkeypatch.setattr(commodity_module, "ensure_fyers_session", lambda **_kwargs: asyncio.sleep(0, result=True))
    monkeypatch.setattr(commodity_module, "ensure_upstox_session", lambda **_kwargs: asyncio.sleep(0, result=True))
    monkeypatch.setattr(commodity_module, "get_fyers_token_health", lambda **_kwargs: asyncio.sleep(0, result={"valid": True}))
    monkeypatch.setattr(commodity_module, "get_upstox_token_health", lambda **_kwargs: asyncio.sleep(0, result={"valid": True}))
    monkeypatch.setattr(agent, "_get_scan_adapter", lambda: asyncio.sleep(0, result=None))

    asyncio.run(agent._prepare_closed_market_state(datetime(2026, 7, 12, 10, 0, tzinfo=timezone.utc)))

    assert called == []
