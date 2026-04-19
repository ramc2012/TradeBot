from __future__ import annotations

import pytest

from brokers.fyers import FyersAdapter


@pytest.mark.asyncio
async def test_get_option_chain_prefers_future_price_over_option_ltp(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = FyersAdapter()

    async def fake_get_data_json(_path: str, _params: dict | None = None) -> dict:
        return {
            "data": {
                "expiryData": [{"date": "21-04-2026", "expiry": "1776729600"}],
                "optionsChain": [
                    {
                        "option_type": "CE",
                        "strike_price": 252000,
                        "ltp": 253045,
                        "fp": 258900,
                        "oi": 11,
                        "volume": 37,
                    },
                    {
                        "option_type": "PE",
                        "strike_price": 252000,
                        "ltp": 7200,
                        "fp": 258900,
                        "oi": 5,
                        "volume": 28,
                    },
                ],
            }
        }

    monkeypatch.setattr(adapter, "_get_data_json", fake_get_data_json)

    chain = await adapter.get_option_chain("MCX:SILVERM26JUNFUT", "2026-04-21")

    assert chain.spot_price == 258900.0


@pytest.mark.asyncio
async def test_get_option_chain_falls_back_to_live_quote_when_chain_has_no_underlying_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FyersAdapter()

    async def fake_get_data_json(_path: str, _params: dict | None = None) -> dict:
        return {
            "data": {
                "expiryData": [{"date": "16-04-2026", "expiry": "1776297600"}],
                "optionsChain": [
                    {
                        "option_type": "CE",
                        "strike_price": 8600,
                        "ltp": 280.4,
                        "oi": 2054,
                        "volume": 16978,
                    },
                ],
            }
        }

    async def fake_get_ltp(symbols: list[str]) -> dict[str, float]:
        assert symbols == ["MCX:CRUDEOIL26APRFUT"]
        return {"MCX:CRUDEOIL26APRFUT": 8607.0}

    monkeypatch.setattr(adapter, "_get_data_json", fake_get_data_json)
    monkeypatch.setattr(adapter, "get_ltp", fake_get_ltp)

    chain = await adapter.get_option_chain("MCX:CRUDEOIL26APRFUT", "2026-04-16")

    assert chain.spot_price == 8607.0
