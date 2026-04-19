from __future__ import annotations

import asyncio
from datetime import date

from brokers.base import OptionChain, OptionChainEntry
from market_data.commodity_atm_watchlist import (
    CommodityATMWatchlistService,
    _extract_commodity_root,
    _normalize_commodity_symbols,
    _select_default_expiry,
)


class _FakeCommodityAdapter:
    def __init__(self) -> None:
        self._contracts = {
            "MCX:GOLD26JUNFUT": [{"expiry": "2099-04-30"}, {"expiry": "2099-05-27"}],
            "MCX:CRUDEOIL26MAYFUT": [{"expiry": "2099-04-16"}],
            "MCX:SILVERM26JUNFUT": [{"expiry": "2099-04-21"}, {"expiry": "2099-05-26"}],
        }
        self.chain_requests: list[tuple[str, str]] = []
        self.ltp_requests: list[list[str]] = []

    async def get_option_contracts(self, symbol: str) -> list[dict]:
        return list(self._contracts.get(symbol, []))

    async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        self.ltp_requests.append(list(symbols))
        payload: dict[str, float] = {}
        for symbol in symbols:
            if "GOLD" in symbol:
                payload[symbol] = 145600.0
            elif "SILVER" in symbol:
                payload[symbol] = 253400.0
            else:
                payload[symbol] = 10400.0
        return payload

    async def get_option_chain(self, symbol: str, expiry: str) -> OptionChain:
        self.chain_requests.append((symbol, expiry))
        return OptionChain(
            symbol=symbol,
            expiry=expiry,
            spot_price=145600.0 if "GOLD" in symbol else 10400.0,
            entries=[
                OptionChainEntry(
                    strike=145600.0 if "GOLD" in symbol else 10400.0,
                    option_type="CE",
                    ltp=120.0,
                    oi=100,
                    volume=200,
                    bid=119.5,
                    ask=120.5,
                    prev_close=100.0,
                    prev_oi=90,
                    instrument_key=f"{symbol.replace('FUT', '')}ATMCE",
                ),
                OptionChainEntry(
                    strike=145600.0 if "GOLD" in symbol else 10400.0,
                    option_type="PE",
                    ltp=118.0,
                    oi=110,
                    volume=180,
                    bid=117.5,
                    ask=118.5,
                    prev_close=105.0,
                    prev_oi=95,
                    instrument_key=f"{symbol.replace('FUT', '')}ATMPE",
                ),
            ],
        )


def test_normalize_commodity_symbols_filters_to_mcx() -> None:
    assert _normalize_commodity_symbols(
        ["mcx:gold26junfut", " MCX:GOLD26JUNFUT ", "NSE:NIFTY50-INDEX", ""]
    ) == ["MCX:GOLD26JUNFUT"]


def test_extract_commodity_root_parses_mcx_future_symbol() -> None:
    assert _extract_commodity_root("MCX:SILVERMIC26JUNFUT") == "SILVERM"


def test_select_default_expiry_prefers_nearest_future() -> None:
    assert _select_default_expiry(
        ["2099-04-16", "2099-04-30", "2099-05-27"],
        as_of=date(2099, 4, 20),
    ) == "2099-04-30"


def test_commodity_watchlist_uses_selected_mcx_expiry() -> None:
    service = CommodityATMWatchlistService()
    adapter = _FakeCommodityAdapter()

    async def _get_adapter():
        return adapter

    service._get_fyers_adapter = _get_adapter  # type: ignore[method-assign]

    payload = asyncio.run(
        service.get_watchlist(
            ["MCX:GOLD26JUNFUT", "MCX:CRUDEOIL26MAYFUT"],
            "2099-05-27",
        )
    )

    assert payload["expiry"] == "2099-05-27"
    assert payload["summary"]["total_rows"] == 1
    assert payload["rows"][0]["underlying"] == "GOLD"
    assert payload["rows"][0]["fyers_symbol"] == "MCX:GOLD26JUNFUT"
    assert payload["rows"][0]["lot_size"] == 10
    assert payload["rows"][0]["contract_unit_label"] == "100 gm contract"


def test_contract_catalog_marks_selected_and_active_expiries() -> None:
    service = CommodityATMWatchlistService()
    adapter = _FakeCommodityAdapter()

    async def _get_adapter():
        return adapter

    service._get_fyers_adapter = _get_adapter  # type: ignore[method-assign]

    payload = asyncio.run(
        service.get_contract_catalog(
            ["MCX:GOLD26JUNFUT", "MCX:CRUDEOIL26MAYFUT"],
            {"MCX:GOLD26JUNFUT": "2099-05-27"},
        )
    )

    gold = next(item for item in payload["contracts"] if item["symbol"] == "MCX:GOLD26JUNFUT")
    crude = next(item for item in payload["contracts"] if item["symbol"] == "MCX:CRUDEOIL26MAYFUT")

    assert gold["selected_expiry"] == "2099-05-27"
    assert gold["active_expiry"] == "2099-05-27"
    assert crude["selected_expiry"] is None
    assert crude["active_expiry"] == "2099-04-16"


def test_contract_catalog_resolves_silvermic_to_silverm_option_root() -> None:
    service = CommodityATMWatchlistService()
    adapter = _FakeCommodityAdapter()

    async def _get_adapter():
        return adapter

    service._get_fyers_adapter = _get_adapter  # type: ignore[method-assign]

    payload = asyncio.run(service.get_contract_catalog(["MCX:SILVERMIC26JUNFUT"]))

    silver = payload["contracts"][0]
    assert silver["symbol"] == "MCX:SILVERMIC26JUNFUT"
    assert silver["lookup_symbol"] == "MCX:SILVERM26JUNFUT"
    assert silver["has_options"] is True
    assert silver["active_expiry"] == "2099-04-21"
    assert silver["lot_size"] == 5
    assert "SILVERM26JUNFUT" in str(silver["detail"])


def test_watchlist_uses_alias_option_root_for_chain_build() -> None:
    service = CommodityATMWatchlistService()
    adapter = _FakeCommodityAdapter()

    async def _get_adapter():
        return adapter

    service._get_fyers_adapter = _get_adapter  # type: ignore[method-assign]

    payload = asyncio.run(service.get_watchlist(["MCX:SILVERMIC26JUNFUT"]))

    assert payload["summary"]["total_rows"] == 1
    assert payload["rows"][0]["lookup_symbol"] == "MCX:SILVERM26JUNFUT"
    assert adapter.chain_requests == [("MCX:SILVERM26JUNFUT", "2099-04-21")]


def test_contract_catalog_discovers_expiry_specific_mcx_lookup_symbols() -> None:
    service = CommodityATMWatchlistService()

    class _RollingMcxAdapter(_FakeCommodityAdapter):
        def __init__(self) -> None:
            super().__init__()
            self._contracts.update(
                {
                    "MCX:NATURALGAS26MAYFUT": [{"expiry": "2099-05-21"}],
                    "MCX:NATURALGAS26JUNFUT": [{"expiry": "2099-06-23"}],
                }
            )

    adapter = _RollingMcxAdapter()

    async def _get_adapter():
        return adapter

    service._get_fyers_adapter = _get_adapter  # type: ignore[method-assign]

    payload = asyncio.run(service.get_contract_catalog(["MCX:NATURALGAS26MAYFUT"]))

    contract = payload["contracts"][0]
    assert contract["symbol"] == "MCX:NATURALGAS26MAYFUT"
    assert contract["expiries"] == ["2099-05-21", "2099-06-23"]
    assert contract["active_expiry"] == "2099-05-21"
    assert contract["active_lookup_symbol"] == "MCX:NATURALGAS26MAYFUT"
    assert contract["expiry_mappings"] == [
        {"expiry": "2099-05-21", "lookup_symbol": "MCX:NATURALGAS26MAYFUT"},
        {"expiry": "2099-06-23", "lookup_symbol": "MCX:NATURALGAS26JUNFUT"},
    ]


def test_contract_catalog_scans_far_enough_for_later_mcx_future_rolls() -> None:
    service = CommodityATMWatchlistService()

    class _LongDatedMcxAdapter(_FakeCommodityAdapter):
        def __init__(self) -> None:
            super().__init__()
            self._contracts.update(
                {
                    "MCX:GOLD26JUNFUT": [{"expiry": "2099-06-30"}],
                    "MCX:GOLD26DECFUT": [{"expiry": "2099-12-31"}],
                    "MCX:GOLD27JANFUT": [{"expiry": "2100-01-29"}],
                }
            )

    adapter = _LongDatedMcxAdapter()

    async def _get_adapter():
        return adapter

    service._get_fyers_adapter = _get_adapter  # type: ignore[method-assign]

    payload = asyncio.run(service.get_contract_catalog(["MCX:GOLD26JUNFUT"]))

    contract = payload["contracts"][0]
    assert contract["expiries"] == ["2099-06-30", "2099-12-31", "2100-01-29"]
    assert contract["expiry_mappings"] == [
        {"expiry": "2099-06-30", "lookup_symbol": "MCX:GOLD26JUNFUT"},
        {"expiry": "2099-12-31", "lookup_symbol": "MCX:GOLD26DECFUT"},
        {"expiry": "2100-01-29", "lookup_symbol": "MCX:GOLD27JANFUT"},
    ]


def test_watchlist_uses_expiry_specific_lookup_symbol_when_selected_contract_rolls() -> None:
    service = CommodityATMWatchlistService()

    class _RollingMcxAdapter(_FakeCommodityAdapter):
        def __init__(self) -> None:
            super().__init__()
            self._contracts.update(
                {
                    "MCX:NATURALGAS26MAYFUT": [{"expiry": "2099-05-21"}],
                    "MCX:NATURALGAS26JUNFUT": [{"expiry": "2099-06-23"}],
                }
            )

        async def get_option_chain(self, symbol: str, expiry: str) -> OptionChain:
            self.chain_requests.append((symbol, expiry))
            return OptionChain(
                symbol=symbol,
                expiry=expiry,
                spot_price=282.0,
                entries=[
                    OptionChainEntry(
                        strike=280.0,
                        option_type="CE",
                        ltp=11.0,
                        oi=300,
                        volume=210,
                        bid=10.8,
                        ask=11.2,
                        prev_close=10.0,
                        prev_oi=260,
                        instrument_key=f"{symbol.replace('FUT', '')}ATMCE",
                    ),
                    OptionChainEntry(
                        strike=280.0,
                        option_type="PE",
                        ltp=10.5,
                        oi=280,
                        volume=205,
                        bid=10.3,
                        ask=10.7,
                        prev_close=9.8,
                        prev_oi=250,
                        instrument_key=f"{symbol.replace('FUT', '')}ATMPE",
                    ),
                ],
            )

    adapter = _RollingMcxAdapter()

    async def _get_adapter():
        return adapter

    service._get_fyers_adapter = _get_adapter  # type: ignore[method-assign]

    payload = asyncio.run(
        service.get_watchlist(
            ["MCX:NATURALGAS26MAYFUT"],
            {"MCX:NATURALGAS26MAYFUT": "2099-06-23"},
        )
    )

    assert payload["summary"]["total_rows"] == 1
    row = payload["rows"][0]
    assert row["active_expiry"] == "2099-06-23"
    assert row["lookup_symbol"] == "MCX:NATURALGAS26JUNFUT"
    assert adapter.chain_requests == [("MCX:NATURALGAS26JUNFUT", "2099-06-23")]


def test_pinned_selection_holds_until_selected_expiry(monkeypatch) -> None:
    service = CommodityATMWatchlistService()

    class _PinnedSelectionAdapter(_FakeCommodityAdapter):
        def __init__(self) -> None:
            super().__init__()
            self._contracts = {
                "MCX:NATURALGAS26JUNFUT": [{"expiry": "2099-06-23"}],
            }

        async def get_option_chain(self, symbol: str, expiry: str) -> OptionChain:
            self.chain_requests.append((symbol, expiry))
            return OptionChain(
                symbol=symbol,
                expiry=expiry,
                spot_price=282.0,
                entries=[
                    OptionChainEntry(
                        strike=280.0,
                        option_type="CE",
                        ltp=11.0,
                        oi=300,
                        volume=210,
                        bid=10.8,
                        ask=11.2,
                        instrument_key=f"{symbol.replace('FUT', '')}ATMCE",
                    ),
                    OptionChainEntry(
                        strike=280.0,
                        option_type="PE",
                        ltp=10.5,
                        oi=280,
                        volume=205,
                        bid=10.3,
                        ask=10.7,
                        instrument_key=f"{symbol.replace('FUT', '')}ATMPE",
                    ),
                ],
            )

    class _FrozenDate(date):
        @classmethod
        def today(cls) -> date:
            return cls(2099, 5, 20)

    adapter = _PinnedSelectionAdapter()

    async def _get_adapter():
        return adapter

    service._get_fyers_adapter = _get_adapter  # type: ignore[method-assign]
    monkeypatch.setattr("market_data.commodity_atm_watchlist.date", _FrozenDate)

    catalog = asyncio.run(
        service.get_contract_catalog(
            ["MCX:NATURALGAS26MAYFUT"],
            {"MCX:NATURALGAS26MAYFUT": "2099-05-21"},
            {"MCX:NATURALGAS26MAYFUT": "MCX:NATURALGAS26MAYFUT"},
        )
    )

    contract = catalog["contracts"][0]
    assert contract["selected_expiry"] == "2099-05-21"
    assert contract["active_expiry"] == "2099-05-21"
    assert contract["active_lookup_symbol"] == "MCX:NATURALGAS26MAYFUT"
    assert contract["selection_policy"] == "pinned_until_expiry"
    assert contract["selection_locked"] is True

    payload = asyncio.run(
        service.get_watchlist(
            ["MCX:NATURALGAS26MAYFUT"],
            {"MCX:NATURALGAS26MAYFUT": "2099-05-21"},
            {"MCX:NATURALGAS26MAYFUT": "MCX:NATURALGAS26MAYFUT"},
        )
    )

    row = payload["rows"][0]
    assert row["active_expiry"] == "2099-05-21"
    assert row["lookup_symbol"] == "MCX:NATURALGAS26MAYFUT"
    assert row["selection_policy"] == "pinned_until_expiry"
    assert row["selection_locked"] is True
    assert adapter.chain_requests == [("MCX:NATURALGAS26MAYFUT", "2099-05-21")]


def test_watchlist_prefers_live_lookup_quote_over_chain_spot_price() -> None:
    service = CommodityATMWatchlistService()

    class _StaleChainAdapter(_FakeCommodityAdapter):
        async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
            self.ltp_requests.append(list(symbols))
            return {"MCX:SILVERM26JUNFUT": 259408.0}

        async def get_option_chain(self, symbol: str, expiry: str) -> OptionChain:
            self.chain_requests.append((symbol, expiry))
            return OptionChain(
                symbol=symbol,
                expiry=expiry,
                spot_price=253400.0,
                entries=[
                    OptionChainEntry(
                        strike=259000.0,
                        option_type="CE",
                        ltp=8100.0,
                        oi=120,
                        volume=240,
                        bid=8090.0,
                        ask=8110.0,
                        prev_close=7900.0,
                        prev_oi=95,
                        instrument_key="MCX:SILVERM26APR259000CE",
                    ),
                    OptionChainEntry(
                        strike=259000.0,
                        option_type="PE",
                        ltp=7800.0,
                        oi=140,
                        volume=265,
                        bid=7790.0,
                        ask=7810.0,
                        prev_close=7600.0,
                        prev_oi=110,
                        instrument_key="MCX:SILVERM26APR259000PE",
                    ),
                ],
            )

    adapter = _StaleChainAdapter()

    async def _get_adapter():
        return adapter

    service._get_fyers_adapter = _get_adapter  # type: ignore[method-assign]

    payload = asyncio.run(service.get_watchlist(["MCX:SILVERM26JUNFUT"]))

    row = payload["rows"][0]
    assert row["spot_price"] == 259408.0
    assert row["atm_strike"] == 259000.0
    assert adapter.ltp_requests == [["MCX:SILVERM26JUNFUT"]]


def test_watchlist_prefers_upstox_lookup_quote_before_fyers() -> None:
    service = CommodityATMWatchlistService()

    class _Adapter(_FakeCommodityAdapter):
        async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
            self.ltp_requests.append(list(symbols))
            return {"MCX:CRUDEOIL26MAYFUT": 8607.0}

        async def get_option_chain(self, symbol: str, expiry: str) -> OptionChain:
            self.chain_requests.append((symbol, expiry))
            return OptionChain(
                symbol=symbol,
                expiry=expiry,
                spot_price=253400.0,
                entries=[
                    OptionChainEntry(
                        strike=259000.0,
                        option_type="CE",
                        ltp=8100.0,
                        oi=120,
                        volume=240,
                        bid=8090.0,
                        ask=8110.0,
                        instrument_key="MCX:SILVERM26APR259000CE",
                    ),
                    OptionChainEntry(
                        strike=259000.0,
                        option_type="PE",
                        ltp=7800.0,
                        oi=140,
                        volume=265,
                        bid=7790.0,
                        ask=7810.0,
                        instrument_key="MCX:SILVERM26APR259000PE",
                    ),
                ],
            )

    adapter = _Adapter()

    async def _get_adapter():
        return adapter

    async def _fake_upstox_quotes(symbols: list[str]) -> dict[str, float]:
        assert symbols == ["MCX:SILVERM26JUNFUT"]
        return {"MCX:SILVERM26JUNFUT": 259408.0}

    service._get_fyers_adapter = _get_adapter  # type: ignore[method-assign]

    from market_data import commodity_atm_watchlist as commodity_atm_module

    original_loader = commodity_atm_module.load_upstox_mcx_quotes
    commodity_atm_module.load_upstox_mcx_quotes = _fake_upstox_quotes  # type: ignore[assignment]
    try:
        payload = asyncio.run(service.get_watchlist(["MCX:SILVERM26JUNFUT"]))
    finally:
        commodity_atm_module.load_upstox_mcx_quotes = original_loader  # type: ignore[assignment]

    row = payload["rows"][0]
    assert row["spot_price"] == 259408.0
    assert row["live_source"] == "upstox"
    assert adapter.ltp_requests == []


def test_watchlist_prefers_nearest_liquid_contract_when_atm_is_thin() -> None:
    service = CommodityATMWatchlistService()

    class _ThinAtmAdapter(_FakeCommodityAdapter):
        async def get_option_chain(self, symbol: str, expiry: str) -> OptionChain:
            self.chain_requests.append((symbol, expiry))
            return OptionChain(
                symbol=symbol,
                expiry=expiry,
                spot_price=145600.0,
                entries=[
                    OptionChainEntry(
                        strike=145600.0,
                        option_type="CE",
                        ltp=118.0,
                        oi=12,
                        volume=4,
                        bid=117.0,
                        ask=121.0,
                        prev_close=100.0,
                        prev_oi=10,
                        instrument_key="MCX:GOLDATMCE",
                    ),
                    OptionChainEntry(
                        strike=145700.0,
                        option_type="CE",
                        ltp=126.0,
                        oi=220,
                        volume=180,
                        bid=125.5,
                        ask=126.5,
                        prev_close=112.0,
                        prev_oi=150,
                        instrument_key="MCX:GOLDLIQCE",
                    ),
                    OptionChainEntry(
                        strike=145600.0,
                        option_type="PE",
                        ltp=115.0,
                        oi=8,
                        volume=2,
                        bid=113.0,
                        ask=118.0,
                        prev_close=98.0,
                        prev_oi=7,
                        instrument_key="MCX:GOLDATMPE",
                    ),
                    OptionChainEntry(
                        strike=145500.0,
                        option_type="PE",
                        ltp=124.0,
                        oi=250,
                        volume=200,
                        bid=123.5,
                        ask=124.5,
                        prev_close=108.0,
                        prev_oi=180,
                        instrument_key="MCX:GOLDLIQPE",
                    ),
                ],
            )

    adapter = _ThinAtmAdapter()

    async def _get_adapter():
        return adapter

    service._get_fyers_adapter = _get_adapter  # type: ignore[method-assign]

    payload = asyncio.run(service.get_watchlist(["MCX:GOLD26JUNFUT"]))

    row = payload["rows"][0]
    assert row["atm_strike"] == 145600.0
    assert row["ce"]["instrument_key"] == "MCX:GOLDLIQCE"
    assert row["ce"]["selection_mode"] == "nearest_liquid"
    assert row["pe"]["instrument_key"] == "MCX:GOLDLIQPE"
    assert row["pe"]["selection_mode"] == "nearest_liquid"
