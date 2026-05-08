from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
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


def test_fno_360_statistics_aggregate_persisted_watchlist_snapshots(monkeypatch) -> None:
    snapshot_time = datetime(2026, 5, 7, 9, 30, tzinfo=timezone.utc)
    rows = [
        {
            "time": snapshot_time,
            "underlying": "RELIANCE",
            "kind": "stock",
            "expiry": date(2026, 5, 28),
            "strike": 1400,
            "option_type": "CE",
            "underlying_price": 1412.5,
            "ltp": 25.0,
            "change_pct": 1.2,
            "oi": 1000,
            "oi_change": 120,
            "oi_change_pct": 13.6,
            "volume": 600,
            "iv": 0.22,
        },
        {
            "time": snapshot_time,
            "underlying": "RELIANCE",
            "kind": "stock",
            "expiry": date(2026, 5, 28),
            "strike": 1400,
            "option_type": "PE",
            "underlying_price": 1412.5,
            "ltp": 18.0,
            "change_pct": 0.4,
            "oi": 500,
            "oi_change": 20,
            "oi_change_pct": 4.2,
            "volume": 300,
            "iv": 0.2,
        },
        {
            "time": snapshot_time,
            "underlying": "NIFTY",
            "kind": "index",
            "expiry": date(2026, 5, 28),
            "strike": 24000,
            "option_type": "CE",
            "underlying_price": 24020.0,
            "ltp": 140.0,
            "change_pct": -0.8,
            "oi": 2000,
            "oi_change": -50,
            "oi_change_pct": -2.4,
            "volume": 1200,
            "iv": 0.16,
        },
        {
            "time": snapshot_time,
            "underlying": "NIFTY",
            "kind": "index",
            "expiry": date(2026, 5, 28),
            "strike": 24000,
            "option_type": "PE",
            "underlying_price": 24020.0,
            "ltp": 110.0,
            "change_pct": -1.0,
            "oi": 3000,
            "oi_change": 150,
            "oi_change_pct": 5.3,
            "volume": 1800,
            "iv": 0.18,
        },
    ]

    class _FakeResult:
        def mappings(self):
            return self

        def all(self):
            return rows

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def scalar(self, *_args, **_kwargs):
            return snapshot_time

        async def execute(self, *_args, **_kwargs):
            return _FakeResult()

    monkeypatch.setattr(market_router, "AsyncSessionLocal", lambda: _FakeSession())

    payload = asyncio.run(market_router._fno_360_statistics(limit=5))

    assert payload["status"] == "ready"
    assert payload["market"]["total_underlyings"] == 2
    assert payload["market"]["stock_underlyings"] == 1
    assert payload["market"]["index_underlyings"] == 1
    assert payload["market"]["pcr_oi"] == 1.167
    assert payload["market"]["pcr_volume"] == 1.167
    assert payload["breadth"] == {"advancers": 1, "decliners": 1, "unchanged": 0}
    assert payload["buildup_counts"]["bullish_long_buildup"] == 1
    assert payload["buildup_counts"]["bearish_short_buildup"] == 1
    assert payload["top_volume"][0]["symbol"] == "NIFTY"
    assert payload["top_oi"][0]["symbol"] == "NIFTY"
    assert payload["analytics"]["index_watch"][0]["symbol"] == "NIFTY"
    assert payload["analytics"]["market_bias"]["label"] == "balanced"
    assert payload["analytics"]["market_bias"]["long"] == 1
    assert payload["analytics"]["market_bias"]["short"] == 1
    assert payload["analytics"]["momentum_distribution"]["zero_to_2"] == 1
    assert payload["analytics"]["momentum_distribution"]["minus_2_to_0"] == 1
    assert payload["analytics"]["active_options"][0]["symbol"] == "NIFTY"
    assert payload["analytics"]["active_options"][0]["side"] == "PE"
    assert payload["analytics"]["futures_gainers"][0]["symbol"] == "RELIANCE"
    assert payload["analytics"]["oi_concentration"]["largest_oi_symbol"] == "NIFTY"


def test_fno_360_statistics_rejects_stale_snapshot_rows(monkeypatch) -> None:
    rows = [
        {
            "time": datetime(2026, 4, 24, 6, 26, tzinfo=timezone.utc),
            "underlying": "VEDL",
            "kind": "stock",
            "expiry": date(2026, 5, 26),
            "strike": 710,
            "option_type": "CE",
            "underlying_price": 711.0,
            "ltp": 17.0,
            "change_pct": -74.24,
            "oi": 65550,
            "oi_change": 60950,
            "oi_change_pct": 1325.0,
            "volume": 98900,
            "iv": 0.1714,
        },
        {
            "time": datetime(2026, 4, 24, 6, 27, tzinfo=timezone.utc),
            "underlying": "VEDL",
            "kind": "stock",
            "expiry": date(2026, 5, 26),
            "strike": 710,
            "option_type": "PE",
            "underlying_price": 711.0,
            "ltp": 14.6,
            "change_pct": 126.36,
            "oi": 532450,
            "oi_change": 85100,
            "oi_change_pct": 19.02,
            "volume": 395600,
            "iv": 0.2032,
        },
    ]

    class _FakeResult:
        def mappings(self):
            return self

        def all(self):
            return rows

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def scalar(self, *_args, **_kwargs):
            return max(row["time"] for row in rows)

        async def execute(self, *_args, **_kwargs):
            return _FakeResult()

    monkeypatch.setattr(market_router, "AsyncSessionLocal", lambda: _FakeSession())

    payload = asyncio.run(market_router._fno_360_statistics(limit=5))

    assert payload["status"] == "stale"
    assert payload["latest_time"].startswith("2026-04-24T06:27:00")
    assert payload["market"]["total_underlyings"] == 0
    assert payload["analytics"] == {}
    assert payload["top_gainers"] == []


def test_market_intelligence_context_includes_fno_360(monkeypatch) -> None:
    async def _fake_sector_payload():
        return {"top_sectors": [], "lagging_sectors": []}

    async def _fake_macro_overview(refresh: bool = False):
        assert refresh is False
        return {"market_read": {"headline": "test"}}

    async def _fake_fno_360():
        return {"status": "ready", "market": {"total_underlyings": 2}}

    monkeypatch.setattr(market_router.india_live_sector_service, "market_intelligence_payload", _fake_sector_payload)
    monkeypatch.setattr(market_router.macro_research_service, "overview", _fake_macro_overview)
    monkeypatch.setattr(market_router, "_fno_360_statistics", _fake_fno_360)

    client = _build_client()
    response = client.get("/api/market/intelligence-context")

    assert response.status_code == 200
    payload = response.json()
    assert payload["fno_360"]["status"] == "ready"
    assert payload["fno_360"]["market"]["total_underlyings"] == 2
    assert payload["market_read"]["headline"] == "test"


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

    async def _no_db_snapshot(_market_symbol):
        return None

    monkeypatch.setattr(market_router, "_get_market_adapter", _fake_get_market_adapter)
    monkeypatch.setattr(market_router, "_latest_market_tick_snapshot", _no_db_snapshot)

    client = _build_client()
    response = client.post("/api/market/ltp", json={"symbols": ["NIFTY", "BANKNIFTY"]})

    assert response.status_code == 200
    assert response.json() == {
        "NSE:NIFTY50-INDEX": 24025.5,
        "NSE:BANKNIFTY-INDEX": 55110.0,
    }


def test_ltp_route_ignores_implausible_mock_index_prices(monkeypatch) -> None:
    class _FakeAdapter:
        broker_name = "upstox"

        async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
            return {symbol: 100.0 for symbol in symbols}

    async def _fake_get_market_adapter():
        return _FakeAdapter(), "upstox"

    async def _fake_latest_market_tick_snapshot(market_symbol) -> market_router.LatestTickSnapshot | None:
        prices = {
            "NSE:NIFTY50-INDEX": 24025.5,
            "NSE:BANKNIFTY-INDEX": 55110.0,
        }
        price = prices.get(market_symbol.app_symbol)
        if not price:
            return None
        return market_router.LatestTickSnapshot(
            symbol=market_symbol.app_symbol,
            ltp=price,
            open=price,
            high=price,
            low=price,
            close=price,
            timestamp=datetime.now(timezone.utc).isoformat(),
            source="underlying_spot_candles",
            stale=False,
            stale_seconds=0.0,
        )

    monkeypatch.setattr(market_router, "_get_market_adapter", _fake_get_market_adapter)
    monkeypatch.setattr(market_router, "_latest_market_tick_snapshot", _fake_latest_market_tick_snapshot)
    monkeypatch.setattr(market_router.data_router, "get_ltp", lambda _symbol: 99.0)

    client = _build_client()
    response = client.post("/api/market/ltp", json={"symbols": ["NIFTY", "BANKNIFTY"]})

    assert response.status_code == 200
    assert response.json() == {
        "NSE:NIFTY50-INDEX": 24025.5,
        "NSE:BANKNIFTY-INDEX": 55110.0,
    }


def test_latest_ticks_route_reports_database_source(monkeypatch) -> None:
    async def _fake_get_market_adapter():
        return None, "none"

    async def _fake_latest_market_tick_snapshot(market_symbol) -> market_router.LatestTickSnapshot:
        return market_router.LatestTickSnapshot(
            symbol=market_symbol.app_symbol,
            ltp=24025.5,
            open=23980.0,
            high=24040.0,
            low=23950.0,
            close=23990.0,
            volume=100,
            oi=0,
            timestamp=datetime.now(timezone.utc).isoformat(),
            source="market_ticks",
            stale=False,
            stale_seconds=0.0,
        )

    monkeypatch.setattr(market_router, "_get_market_adapter", _fake_get_market_adapter)
    monkeypatch.setattr(market_router, "_latest_market_tick_snapshot", _fake_latest_market_tick_snapshot)
    monkeypatch.setattr(market_router.data_router, "get_ltp", lambda _symbol: 0.0)

    client = _build_client()
    response = client.post("/api/market/latest-ticks", json={"symbols": ["NIFTY"]})

    assert response.status_code == 200
    payload = response.json()["NSE:NIFTY50-INDEX"]
    assert payload["ltp"] == 24025.5
    assert payload["close"] == 23990.0
    assert payload["source"] == "market_ticks"
    assert payload["stale"] is False


def test_latest_ticks_route_times_out_slow_live_ltp_and_uses_database(monkeypatch) -> None:
    class _SlowAdapter:
        broker_name = "upstox"

        async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
            await asyncio.sleep(10)
            return {symbol: 24000.0 for symbol in symbols}

    async def _fake_get_market_adapter():
        return _SlowAdapter(), "upstox"

    async def _fake_latest_market_tick_snapshot(market_symbol) -> market_router.LatestTickSnapshot:
        return market_router.LatestTickSnapshot(
            symbol=market_symbol.app_symbol,
            ltp=24025.5,
            open=23980.0,
            high=24040.0,
            low=23950.0,
            close=23990.0,
            volume=100,
            oi=0,
            timestamp=datetime.now(timezone.utc).isoformat(),
            source="market_ticks",
            stale=False,
            stale_seconds=0.0,
        )

    monkeypatch.setattr(market_router, "_LATEST_TICKS_LIVE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(market_router, "_get_market_adapter", _fake_get_market_adapter)
    monkeypatch.setattr(market_router, "_latest_market_tick_snapshot", _fake_latest_market_tick_snapshot)
    monkeypatch.setattr(market_router.data_router, "get_ltp", lambda _symbol: 0.0)

    client = _build_client()
    response = client.post("/api/market/latest-ticks", json={"symbols": ["NIFTY"]})

    assert response.status_code == 200
    payload = response.json()["NSE:NIFTY50-INDEX"]
    assert payload["ltp"] == 24025.5
    assert payload["source"] == "market_ticks"


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

    async def _fake_local_option_expiries(_symbol: str) -> list[str]:
        return []

    monkeypatch.setattr(market_router, "_get_market_adapter", _fake_get_market_adapter)
    monkeypatch.setattr(market_router, "_local_option_expiries", _fake_local_option_expiries)

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

    async def _fake_local_option_expiries(_symbol: str) -> list[str]:
        return []

    monkeypatch.setattr(market_router, "_get_market_adapter", _fake_get_market_adapter)
    monkeypatch.setattr(market_router, "_local_option_expiries", _fake_local_option_expiries)
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
