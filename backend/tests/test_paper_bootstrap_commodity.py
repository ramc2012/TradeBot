from __future__ import annotations

import asyncio

from core.paper_bootstrap import _prewarm_commodity_watchlists


def test_commodity_prewarm_does_not_require_removed_options_lock_api() -> None:
    calls: list[tuple[str, list[str]]] = []

    class Agent:
        def get_symbols(self):
            return ["MCX:GOLD26AUGFUT"]

        def get_selected_option_expiries(self):
            return {}

        def get_selected_option_lookup_symbols(self):
            return {}

    class Service:
        async def get_contract_catalog(self, symbols, expiries, lookup):
            calls.append(("catalog", symbols))
            return {}

        async def get_watchlist(self, symbols, expiries, lookup, selected):
            calls.append(("watchlist", symbols))
            return {}

    asyncio.run(
        _prewarm_commodity_watchlists(
            commodity_strategy_agent=Agent(),
            commodity_atm_watchlist_service=Service(),
        )
    )

    assert calls == [
        ("catalog", ["MCX:GOLD26AUGFUT"]),
        ("watchlist", ["MCX:GOLD26AUGFUT"]),
    ]
