from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data import fno_analytics


def test_build_fno_analytics_normalizes_nse_and_mcx_contracts(monkeypatch) -> None:
    expiry = date.today().replace(day=min(date.today().day, 20))
    if expiry <= date.today():
        expiry = date.today().replace(day=min(date.today().day + 7, 28))
    rows = [
        {
            "market": "NSE",
            "instrument_key": "NSE_FO|1",
            "trading_symbol": "NIFTY26MAY23700CE",
            "underlying": "NIFTY",
            "expiry": expiry,
            "strike": 23700,
            "option_type": "CE",
            "lot_size": 75,
            "tick_size": 0.05,
            "freeze_quantity": 1800,
            "sync_status": "complete",
            "updated_at": datetime.now(timezone.utc),
        },
        {
            "market": "NSE",
            "instrument_key": "NSE_FO|2",
            "trading_symbol": "RELIANCE26MAY1400PE",
            "underlying": "RELIANCE",
            "expiry": expiry,
            "strike": 1400,
            "option_type": "PE",
            "lot_size": 500,
            "tick_size": 0.05,
            "freeze_quantity": 10000,
            "sync_status": "complete",
            "updated_at": datetime.now(timezone.utc),
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

        async def execute(self, *_args, **_kwargs):
            return _FakeResult()

    class _FakeCommodityAgent:
        def get_symbols(self):
            return ["MCX:CRUDEOIL26JUNFUT"]

        def get_selected_option_expiries(self):
            return {"MCX:CRUDEOIL26JUNFUT": expiry.isoformat()}

        def get_selected_option_lookup_symbols(self):
            return {}

    class _FakeCommodityWatchlistService:
        def get_cached_contract_catalog(self, *_args, **_kwargs):
            return {
                "contracts": [
                    {
                        "symbol": "MCX:CRUDEOIL26JUNFUT",
                        "underlying": "CRUDEOIL",
                        "active_expiry": expiry.isoformat(),
                        "has_options": True,
                        "lot_size": 100,
                    }
                ],
                "summary": {"total_symbols": 1, "contracts_ready": 1, "active_selections": 1},
                "source": "test",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        def get_cached_watchlist(self, *_args, **_kwargs):
            return {
                "expiry": expiry.isoformat(),
                "rows": [
                    {
                        "symbol": "CRUDEOIL",
                        "underlying": "CRUDEOIL",
                        "expiry": expiry.isoformat(),
                        "atm_strike": 8300,
                        "lot_size": 100,
                        "ce": {"strike": 8300, "ltp": 120, "oi": 1000, "volume": 200},
                        "pe": {"strike": 8300, "ltp": 90, "oi": 1200, "volume": 180},
                    }
                ],
                "summary": {"total_rows": 1, "ce_ready": 1, "pe_ready": 1},
                "source": "test",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

    monkeypatch.setattr(fno_analytics, "AsyncSessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(fno_analytics, "commodity_strategy_agent", _FakeCommodityAgent())
    monkeypatch.setattr(fno_analytics, "commodity_atm_watchlist_service", _FakeCommodityWatchlistService())

    payload = asyncio.run(
        fno_analytics.build_fno_analytics(
            fno_360={
                "status": "ready",
                "latest_time": datetime.now(timezone.utc).isoformat(),
                "market": {"total_underlyings": 2, "pcr_oi": 1.1, "average_iv": 0.2},
                "breadth": {"advancers": 1, "decliners": 1, "unchanged": 0},
                "buildup_counts": {"bullish_long_buildup": 1},
                "analytics": {"oi_change_contracts": [], "volatility_watch": []},
                "top_volume": [],
                "top_oi": [],
            },
            limit=5,
        )
    )

    assert payload["status"] == "ready"
    assert payload["nse"]["contract_master"]["summary"]["total_contracts"] == 2
    assert payload["nse"]["contract_master"]["sample"][0]["contract_id"].startswith("NSE:FO:OPTIDX:NIFTY")
    assert payload["nse"]["risk"]["stock_physical_settlement_contracts"] == 1
    assert payload["mcx"]["contract_master"]["summary"]["total_contracts"] == 3
    assert payload["mcx"]["contract_master"]["summary"]["option_contracts"] == 2
    assert payload["mcx"]["contract_master"]["sample"][1]["is_devolvement_applicable"] is True
    assert payload["quality_checks"][0]["status"] == "ok"
    assert payload["stage_status"][0]["status"] == "ready"
    assert payload["research"]["modules"][0]["key"] == "contract_master"
    assert payload["research"]["answer_cards"][0]["label"] == "What is happening?"
    assert any(source["key"] == "mcx_atm_watchlist" for source in payload["research"]["sources"])
