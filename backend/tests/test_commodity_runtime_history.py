import asyncio


def test_load_commodity_history_rows_prefers_active_rollover_contract(monkeypatch) -> None:
    from market_data import commodity_runtime_history as history
    import paper_engine.commodity_strategy_agent as commodity_agent_module

    calls: list[str] = []

    class FakeAgent:
        def get_symbols(self) -> list[str]:
            return ["MCX:NICKEL26JULFUT"]

        async def _active_futures_symbols(self) -> dict[str, str]:
            return {"MCX:NICKEL26JULFUT": "MCX:NICKEL26AUGFUT"}

        def get_selected_option_lookup_symbols(self) -> dict[str, str]:
            return {}

        async def _load_history(self, symbol: str, *, interval: str, lookback_days: int):
            calls.append(symbol)
            assert interval == "1minute"
            assert lookback_days == 2
            return [{"time": "2026-07-16T15:24:00+00:00", "close": 1643.4}]

    monkeypatch.setattr(commodity_agent_module, "CommodityStrategyAgent", FakeAgent)

    rows, selected_symbol = asyncio.run(
        history.load_commodity_history_rows(
            "NICKEL",
            interval="1minute",
            lookback_days=2,
            persist=False,
        )
    )

    assert rows
    assert selected_symbol == "MCX:NICKEL26AUGFUT"
    assert calls == ["MCX:NICKEL26AUGFUT"]
