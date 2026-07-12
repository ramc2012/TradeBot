from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers import institutional_convergence as router_module
from institutional_convergence import service as service_module
from institutional_convergence.service import (
    InstitutionalConvergenceService,
    _select_rule_sessions,
    evaluate_index_snapshot,
    select_diversified_stocks,
)
from institutional_convergence.engine import _aligned_tick_cvd, build_footprint, lots_for_risk
from institutional_convergence.paper import ConvergencePaperBook
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


def test_footprint_detects_three_to_one_buying_imbalance() -> None:
    base = datetime(2026, 7, 13, 9, 18, tzinfo=IST)
    ticks = [
        {"time": base, "ltp": 100.0, "bid": 99.5, "ask": 100.0, "volume": 100},
        {"time": base.replace(second=10), "ltp": 100.0, "bid": 99.5, "ask": 100.0, "volume": 400},
        {"time": base.replace(second=20), "ltp": 99.5, "bid": 99.5, "ask": 100.0, "volume": 450},
    ]

    footprint = build_footprint(ticks, 0.5)

    level = next(row for row in footprint["bars"][0]["levels"] if row["price"] == 100.0)
    assert level["buy_ratio"] >= 3.0


def test_risk_sizing_uses_one_percent_cap() -> None:
    assert lots_for_risk(1_000_000, 0.01, 20, 50) == 10
    assert lots_for_risk(1_000_000, 0.005, 20, 50) == 5


def test_paper_book_opens_and_moves_stop_to_break_even(tmp_path) -> None:
    book = ConvergencePaperBook(tmp_path / "paper.json")
    now = datetime(2026, 7, 13, 10, 30, tzinfo=IST)
    signal = {
        "symbol": "NIFTY", "status": "actionable_paper", "action": "LONG", "spot": 100.0,
        "risk": {"entry": 100.0, "stop": 90.0, "target1": 110.0, "target2_long": 120.0, "lot_size": 50, "risk_fraction": 0.01},
        "cvd": {"series": [{"cvd": 1}, {"cvd": 2}]},
    }
    opened = book.sync([signal], now)
    assert opened["open_count"] == 1
    assert opened["open_positions"][0]["lots"] == 20

    signal["spot"] = 110.0
    marked = book.sync([signal], now.replace(minute=33))
    position = marked["open_positions"][0]
    assert position["target1_done"] is True
    assert position["lots"] == 10
    assert position["stop"] == 100.0


def test_paper_book_locks_after_two_losses(tmp_path) -> None:
    book = ConvergencePaperBook(tmp_path / "paper.json")
    today = "2026-07-13"
    book._save({
        "initial_capital": 1_000_000,
        "open_positions": [],
        "closed_positions": [
            {"session_date": today, "realized_pnl": -1000},
            {"session_date": today, "realized_pnl": -1000},
        ],
    })

    summary = book.sync([], datetime(2026, 7, 13, 11, 0, tzinfo=IST))

    assert summary["circuit_breaker"]["locked"] is True


def test_rule_sessions_ignore_weekend_and_after_hours_contamination() -> None:
    now = datetime(2026, 7, 12, 14, 0, tzinfo=IST)

    def rows(start: datetime, count: int):
        return [{"time": start + timedelta(minutes=3 * index)} for index in range(count)]

    bars = [
        *rows(datetime(2026, 7, 9, 9, 15, tzinfo=IST), 100),
        *rows(datetime(2026, 7, 10, 9, 15, tzinfo=IST), 126),
        *rows(datetime(2026, 7, 10, 18, 0, tzinfo=IST), 20),
        *rows(datetime(2026, 7, 12, 11, 0, tzinfo=IST), 6),
    ]

    current, prior, history = _select_rule_sessions(bars, now)

    assert current == []
    assert len(prior) == 126
    assert prior[0]["time"].date().isoformat() == "2026-07-10"
    assert len(history) == 226


def test_rule_sessions_accept_partial_current_session_after_four_bars() -> None:
    now = datetime(2026, 7, 13, 9, 30, tzinfo=IST)
    prior = [{"time": datetime(2026, 7, 10, 9, 15, tzinfo=IST) + timedelta(minutes=3 * index)} for index in range(126)]
    current_rows = [{"time": datetime(2026, 7, 13, 9, 15, tzinfo=IST) + timedelta(minutes=3 * index)} for index in range(5)]

    current, selected_prior, history = _select_rule_sessions([*prior, *current_rows], now)

    assert current == current_rows
    assert selected_prior == prior
    assert history == prior


def test_tick_cvd_alignment_drops_unmatched_buckets() -> None:
    bars = [
        {"time": datetime(2026, 7, 13, 9, 15, tzinfo=IST), "close": 100.0},
        {"time": datetime(2026, 7, 13, 9, 21, tzinfo=IST), "close": 102.0},
    ]
    footprint = [
        {"time": datetime(2026, 7, 13, 3, 45, tzinfo=timezone.utc).isoformat(), "cumulative_delta": 10},
        {"time": datetime(2026, 7, 13, 3, 48, tzinfo=timezone.utc).isoformat(), "cumulative_delta": 20},
        {"time": datetime(2026, 7, 13, 3, 51, tzinfo=timezone.utc).isoformat(), "cumulative_delta": 30},
    ]

    aligned, cvd, series = _aligned_tick_cvd(bars, footprint)

    assert aligned == bars
    assert cvd == [10.0, 30.0]
    assert [row["close"] for row in series] == [100.0, 102.0]
