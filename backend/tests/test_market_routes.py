from __future__ import annotations

from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.routers import market as market_router
from market_data.symbols import to_app_symbol, to_broker_symbol, to_fyers_symbol


def _build_client() -> TestClient:
    app = FastAPI()
    app.include_router(market_router.router)
    return TestClient(app)


def test_market_symbol_helpers_accept_display_and_fyers_aliases() -> None:
    assert to_app_symbol("NIFTY") == "NSE:NIFTY50-INDEX"
    assert to_app_symbol("NSE:NIFTYBANK-INDEX") == "NSE:BANKNIFTY-INDEX"
    assert to_broker_symbol("BANKNIFTY") == "NSE_INDEX|Nifty Bank"
    assert to_fyers_symbol("BANKNIFTY") == "NSE:NIFTYBANK-INDEX"


def test_ltp_route_normalizes_display_symbols(monkeypatch) -> None:
    class _FakeAdapter:
        broker_name = "upstox"

        async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
            assert symbols == ["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank"]
            return {
                "NSE_INDEX|Nifty 50": 24025.5,
                "NSE_INDEX|Nifty Bank": 55110.0,
            }

    async def _fake_get_market_adapter():
        return _FakeAdapter(), "upstox"

    monkeypatch.setattr(market_router, "_get_market_adapter", _fake_get_market_adapter)

    client = _build_client()
    response = client.post("/api/market/ltp", json={"symbols": ["NIFTY", "BANKNIFTY"]})

    assert response.status_code == 200
    assert response.json() == {
        "NSE:NIFTY50-INDEX": 24025.5,
        "NSE:BANKNIFTY-INDEX": 55110.0,
    }


def test_expiries_route_normalizes_display_symbol(monkeypatch) -> None:
    class _FakeAdapter:
        broker_name = "fyers"

        def __init__(self) -> None:
            self.requested_symbols: list[str] = []

        async def get_option_contracts(self, symbol: str) -> list[dict[str, str]]:
            self.requested_symbols.append(symbol)
            return [
                {"expiry": "2026-04-13"},
                {"expiry": "2026-04-21"},
            ]

    adapter = _FakeAdapter()

    async def _fake_get_market_adapter():
        return adapter, "fyers"

    monkeypatch.setattr(market_router, "_get_market_adapter", _fake_get_market_adapter)

    client = _build_client()
    response = client.get("/api/market/expiries/NIFTY")

    assert response.status_code == 200
    payload = response.json()
    assert payload["symbol"] == "NSE:NIFTY50-INDEX"
    assert payload["expiries"] == ["2026-04-13", "2026-04-21"]
    assert payload["default_expiry"] == "2026-04-13"
    assert adapter.requested_symbols == ["NSE:NIFTY50-INDEX", "NSE:NIFTY50-INDEX"]


def test_option_chain_route_normalizes_display_symbol(monkeypatch) -> None:
    class _FakeAdapter:
        broker_name = "fyers"

        def __init__(self) -> None:
            self.requested_symbols: list[str] = []

        async def get_option_contracts(self, symbol: str) -> list[dict[str, str]]:
            self.requested_symbols.append(symbol)
            return [{"expiry": "2026-04-13"}]

    adapter = _FakeAdapter()
    refreshed: list[tuple[str, str]] = []

    async def _fake_get_market_adapter():
        return adapter, "fyers"

    async def _fake_get_cached(symbol: str, expiry: str):
        if not refreshed:
            assert symbol == "NSE:NIFTY50-INDEX"
            assert expiry == "2026-04-13"
            return None
        return {
            "symbol": symbol,
            "expiry": expiry,
            "entries": [{"strike": 23850.0, "option_type": "CE", "ltp": 100.0, "oi": 10, "volume": 20}],
            "source": "fyers",
        }

    async def _fake_refresh(symbol: str, expiry: str):
        refreshed.append((symbol, expiry))

    monkeypatch.setattr(market_router, "_get_market_adapter", _fake_get_market_adapter)
    monkeypatch.setattr(market_router.option_chain_service, "set_broker", lambda _broker: None)
    monkeypatch.setattr(market_router.option_chain_service, "track", lambda _symbol, _expiry: None)
    monkeypatch.setattr(market_router.option_chain_service, "_refresh", _fake_refresh)
    monkeypatch.setattr(market_router.option_chain_service, "get_cached", _fake_get_cached)

    client = _build_client()
    response = client.get("/api/market/option-chain/NIFTY")

    assert response.status_code == 200
    payload = response.json()
    assert payload["symbol"] == "NSE:NIFTY50-INDEX"
    assert payload["expiry"] == "2026-04-13"
    assert len(payload["entries"]) == 1
    assert refreshed == [("NSE:NIFTY50-INDEX", "2026-04-13")]
    assert adapter.requested_symbols == ["NSE:NIFTY50-INDEX"]
