from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers import institutional_convergence as router_module
from institutional_convergence import service as service_module
from institutional_convergence.service import (
    InstitutionalConvergenceService,
    evaluate_index_snapshot,
    select_diversified_stocks,
)
from paper_engine.base_strategy_agent import IST


def test_select_diversified_stocks_keeps_one_name_per_sector() -> None:
    payload = {
        "watchlist": [
            {"instrument": "MIDCPNIFTY", "sector_code": "UNCLASSIFIED", "directional_bias": "bullish", "composite_alpha_score": 99},
            {"instrument": "A", "sector_code": "BANK", "directional_bias": "bullish", "composite_alpha_score": 90},
            {"instrument": "B", "sector_code": "BANK", "directional_bias": "bullish", "composite_alpha_score": 89},
            {"instrument": "C", "sector_code": "IT", "directional_bias": "bearish", "composite_alpha_score": 88},
            {"instrument": "D", "sector_code": "AUTO", "directional_bias": "neutral", "composite_alpha_score": 95},
        ]
    }

    rows = select_diversified_stocks(payload, limit=10)

    assert [row["symbol"] for row in rows] == ["A", "C"]
    assert len({row["sector"] for row in rows}) == len(rows)


def test_index_snapshot_requires_real_book_and_all_convergence_gates() -> None:
    snapshot = {
        "request": {
            "session": {"last_price": 100.0},
            "metadata": {
                "order_flow_source": "tick_reconstruction_book",
                "order_flow_book_active": True,
                "order_flow_book_symbol": "NSE:NIFTY26JULFUT",
            },
        },
        "analysis": {
            "market_profile": {"close_price": 100, "val": 100, "vah": 104, "poc": 102, "initial_balance_range": 4},
            "order_flow": {"cumulative_delta": 200, "book_pressure": 0.3, "volatility_burst": 1.8},
            "ntm_volx": {"directional_bias": "LONG", "net_pressure": 0.2},
        },
    }

    result = evaluate_index_snapshot("NIFTY", snapshot)

    assert result["status"] == "actionable_shadow"
    assert result["action"] == "LONG"
    assert all(result["gates"].values())


def test_index_snapshot_blocks_bar_inference() -> None:
    snapshot = {
        "request": {"session": {"last_price": 100}, "metadata": {"order_flow_source": "bar_inference"}},
        "analysis": {
            "market_profile": {"val": 100, "initial_balance_range": 4},
            "order_flow": {"cumulative_delta": 200, "book_pressure": 0.3, "volatility_burst": 1.8},
            "ntm_volx": {"directional_bias": "LONG"},
        },
    }

    result = evaluate_index_snapshot("NIFTY", snapshot)

    assert result["status"] == "blocked"
    assert "real_book_data" in result["blocked_reasons"]


def test_closed_cycle_does_not_build_or_persist(monkeypatch, tmp_path) -> None:
    service = InstitutionalConvergenceService(state_file=tmp_path / "state.json")
    monkeypatch.setattr(service_module, "_now_ist", lambda: datetime(2026, 7, 12, 10, 0, tzinfo=IST))

    payload = asyncio.run(service.run_cycle())

    assert payload["status"] == "market_closed"
    assert not service.state_file.exists()


def test_status_route_exposes_lane(monkeypatch) -> None:
    async def _status():
        return {"key": "institutional_convergence", "mode": "shadow"}

    monkeypatch.setattr(router_module.institutional_convergence_service, "status", _status)
    app = FastAPI()
    app.include_router(router_module.router)

    response = TestClient(app).get("/api/institutional-convergence/status")

    assert response.status_code == 200
    assert response.json()["key"] == "institutional_convergence"
