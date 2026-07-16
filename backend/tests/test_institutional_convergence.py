from __future__ import annotations

import asyncio
from dataclasses import dataclass
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
from institutional_convergence.engine import (
    _aligned_tick_cvd,
    _freshness_limit_ms,
    _risk_plan,
    _structural_setup,
    build_footprint,
    evaluate_rules,
    lots_for_risk,
    tick_clock_drift_ms,
)
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


def test_open_cycle_offloads_rule_evaluation_from_event_loop(monkeypatch, tmp_path) -> None:
    service = InstitutionalConvergenceService(state_file=tmp_path / "state.json")
    now = datetime(2026, 7, 13, 10, 0, tzinfo=IST)
    calls: list[str] = []

    monkeypatch.setattr(service_module, "_now_ist", lambda: now)
    monkeypatch.setattr(service, "build_universe", lambda: asyncio.sleep(0, result={"indices": ["NIFTY"], "stocks": []}))
    monkeypatch.setattr(service_module, "_load_india_vix", lambda: asyncio.sleep(0, result=13.0))
    monkeypatch.setattr(
        service_module,
        "_load_rule_inputs",
        lambda symbol, futures_symbol, current_now: asyncio.sleep(
            0,
            result={
                "current_bars": [{"time": now, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 100}] * 4,
                "prior_bars": [{"time": now - timedelta(days=1), "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 100}] * 4,
                "history_bars": [{"time": now - timedelta(days=2), "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 100}] * 8,
                "ticks": [],
                "options": {},
                "lot_size": 50,
                "tick_size": 0.5,
                "clock_drift_ms": 10.0,
            },
        ),
    )

    async def _fake_to_thread(func, /, *args, **kwargs):
        calls.append(func.__name__)
        return func(*args, **kwargs)

    monkeypatch.setattr(service_module.asyncio, "to_thread", _fake_to_thread)
    monkeypatch.setattr(service_module.market_data_router, "add_subscriptions", lambda symbols: asyncio.sleep(0, result=None))
    monkeypatch.setattr(service_module.convergence_paper_book, "sync", lambda results, current_now: {"open_count": 0, "open_positions": [], "closed_positions": []})
    monkeypatch.setattr(service, "_save_state", lambda payload: None)

    import core.config as config_module
    import data.index_futures_backfill as backfill_module

    monkeypatch.setattr(config_module, "auction_front_month_book_symbols", lambda current_date: {"NSE:NIFTY50-INDEX": "NSE:NIFTY24JULFUT"})
    monkeypatch.setattr(backfill_module, "fyers_front_month_symbol", lambda symbol, current_date: "NSE:NIFTY24JULFUT")
    monkeypatch.setattr(backfill_module, "month_code_for_front_contract", lambda current_date, symbol: "24JUL")
    def _evaluate_rules(**kwargs):
        return {"symbol": kwargs["symbol"], "status": "actionable_paper", "action": "LONG"}

    monkeypatch.setattr(service_module, "evaluate_rules", _evaluate_rules)

    payload = asyncio.run(service.run_cycle())

    assert payload["status"] == "ok"
    assert payload["actionable_count"] == 1
    assert calls == ["_evaluate_rules", "<lambda>"]


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


def test_three_minute_lane_uses_adaptive_tick_freshness() -> None:
    base = datetime(2026, 7, 13, 10, 0, tzinfo=IST)
    ticks = [{"time": base + timedelta(seconds=index * 2)} for index in range(10)]

    assert _freshness_limit_ms(ticks) == 45_000.0


def test_tick_drift_cancels_shared_flush_lag_on_a_fresh_tape() -> None:
    """Repro of 2026-07-15 21:18 IST tick_fresh 5/8: the batched tick flush ran
    ~50s behind the wall clock while every MCX root ticked seconds-fresh.
    Wall-clock drift blamed the symbols; pipeline-referenced drift must not."""
    wall_now = datetime(2026, 7, 15, 21, 18, 0, tzinfo=IST)
    pipeline_last = wall_now - timedelta(seconds=50)  # newest visible tick anywhere
    fresh_root_last = pipeline_last - timedelta(seconds=4)  # e.g. COPPER, 4s behind GOLD

    drift = tick_clock_drift_ms(fresh_root_last, pipeline_last, wall_now)

    assert drift == 4_000.0  # tape staleness only — not 54s of shared write lag


def test_tick_drift_still_flags_a_genuinely_sparse_symbol() -> None:
    wall_now = datetime(2026, 7, 15, 21, 18, 0, tzinfo=IST)
    pipeline_last = wall_now - timedelta(seconds=10)
    sparse_last = pipeline_last - timedelta(seconds=95)  # e.g. NICKEL, tape truly quiet

    drift = tick_clock_drift_ms(sparse_last, pipeline_last, wall_now)

    assert drift == 95_000.0  # above the 90s adaptive cap -> gate blocks


def test_tick_drift_falls_back_to_wall_clock_when_the_pipeline_stalls() -> None:
    """If the WHOLE store goes quiet the pipeline reference must not make every
    symbol pass with ~0 drift — a dead feed still blocks."""
    wall_now = datetime(2026, 7, 15, 21, 18, 0, tzinfo=IST)
    pipeline_last = wall_now - timedelta(seconds=300)
    last_tick = pipeline_last  # this symbol owns the newest (stale) tick

    drift = tick_clock_drift_ms(last_tick, pipeline_last, wall_now)

    assert drift == 300_000.0  # wall-clock fallback -> tick_fresh fails


def test_tick_drift_handles_missing_ticks_and_missing_pipeline() -> None:
    wall_now = datetime(2026, 7, 15, 21, 18, 0, tzinfo=IST)

    assert tick_clock_drift_ms(None, wall_now, wall_now) is None
    # No pipeline reference at all (empty store) -> wall clock is the reference.
    assert tick_clock_drift_ms(wall_now - timedelta(seconds=7), None, wall_now) == 7_000.0
    # A symbol owning the newest tick can never be negative-drift.
    assert tick_clock_drift_ms(wall_now, wall_now - timedelta(seconds=1), wall_now) == 0.0


def test_commodity_loader_measures_drift_against_the_pipeline_reference(monkeypatch) -> None:
    """_load_rule_inputs must compute clock_drift_ms from the newest tick the
    shared flush pipeline produced (root tick vs store-wide tick), not the wall
    clock — otherwise flush lag masquerades as tape staleness."""
    from institutional_convergence import commodity as commodity_module

    wall_anchor = datetime.now(timezone.utc)
    root_last = wall_anchor - timedelta(seconds=60)
    pipeline_last = root_last + timedelta(seconds=46)

    class _Rows:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _Result:
        def __init__(self, rows=None, scalar=None):
            self._rows = rows or []
            self._scalar = scalar

        def mappings(self):
            return _Rows(self._rows)

        def scalar(self):
            return self._scalar

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, statement, params=None):
            sql = str(statement)
            if "underlying_spot_candles" in sql:
                return _Result(rows=[])
            if "option_premium_candles" in sql:
                return _Result(rows=[])
            if "market_ticks" in sql and params and "symbol" in params:
                return _Result(rows=[{"time": root_last, "ltp": 100.0, "bid": 99.9,
                                      "ask": 100.1, "bid_qty": 1, "ask_qty": 1, "volume": 10}])
            if "market_ticks" in sql:
                return _Result(scalar=pipeline_last)
            raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(commodity_module, "AsyncSessionLocal", _Session)

    inputs = asyncio.run(
        commodity_module._load_rule_inputs(
            "GOLD", "MCX:GOLD26AUGFUT", datetime.now(IST)
        )
    )

    assert inputs["clock_drift_ms"] == 46_000.0  # tape gap only, no flush lag


def test_structural_reclaim_stays_armed_for_five_bars_then_expires() -> None:
    base = datetime(2026, 7, 13, 10, 0, tzinfo=IST)
    reclaim = {"time": base, "open": 99.5, "high": 100.5, "low": 99.0, "close": 100.2}
    follow = [
        {"time": base + timedelta(minutes=3 * index), "open": 100.3, "high": 100.7, "low": 100.3, "close": 100.5}
        for index in range(1, 6)
    ]

    armed = _structural_setup([reclaim, *follow[:4]], [100.0], "LONG", 0.2, active_window_bars=5)
    expired = _structural_setup([reclaim, *follow], [100.0], "LONG", 0.2, active_window_bars=5)

    assert armed["state"] == "ARMED"
    assert armed["age_bars"] == 4
    assert expired["state"] == "EXPIRED"


def test_risk_plan_rejects_a_chased_entry() -> None:
    plan = _risk_plan(
        direction="LONG",
        entry=102.0,
        setup={"active": True, "level": 100.0, "extreme": 99.0},
        atr_value=1.0,
        targets=[108.0],
        max_chase_atr=0.5,
        min_reward_risk=1.5,
    )

    assert plan["not_chasing"] is False
    assert plan["reward_risk_valid"] is True


def test_two_of_three_confirmations_can_enter_at_reduced_risk(monkeypatch) -> None:
    from institutional_convergence import engine as engine_module

    @dataclass
    class Profile:
        val: float
        vah: float
        poc: float
        initial_balance_range: float = 2.0
        initial_balance_high: float = 104.0
        initial_balance_low: float = 100.0

    now = datetime(2026, 7, 13, 10, 30, tzinfo=IST)
    bars = [
        {"time": now - timedelta(minutes=15 - 3 * index), "open": 100.5, "high": 101.0, "low": 100.0, "close": 100.5, "volume": 100}
        for index in range(5)
    ]
    bars[-1] = {"time": now - timedelta(minutes=3), "open": 99.6, "high": 100.4, "low": 99.0, "close": 100.2, "volume": 100}
    prior_profile = Profile(val=100.0, vah=110.0, poc=106.0)
    current_profile = Profile(val=99.0, vah=104.0, poc=103.0, initial_balance_high=100.0, initial_balance_low=99.0)
    monkeypatch.setattr(engine_module, "_profile", lambda symbol, rows, tick_size, prior=None: prior_profile if prior is None else current_profile)
    monkeypatch.setattr(engine_module, "volume_node_density", lambda rows, bins: [])
    monkeypatch.setattr(engine_module, "hvn_lvn", lambda histogram: {"hvn": [], "lvn": []})
    monkeypatch.setattr(engine_module, "build_footprint", lambda ticks, tick_size: {"bars": [], "tick_count": len(ticks), "source": "market_ticks"})
    cvd = [0.0, 10.0, 20.0, 40.0]
    series = [{"time": row["time"].isoformat(), "cvd": value, "close": row["close"]} for row, value in zip(bars[-4:], cvd)]
    monkeypatch.setattr(engine_module, "_aligned_tick_cvd", lambda current, footprint: (current[-4:], cvd, series))
    monkeypatch.setattr(engine_module, "cvd_divergence", lambda candles, values, lookback=20: None)
    ticks = [{"time": now - timedelta(seconds=20 - index)} for index in range(8)]

    result = evaluate_rules(
        symbol="TEST", current_bars=bars, prior_bars=bars, history_bars=bars,
        ticks=ticks, options={}, vix=14.0, lot_size=10, tick_size=0.1,
        clock_drift_ms=20_000.0, now=now, directional_bias="bullish",
    )

    assert result["action"] == "LONG", result
    assert result["confirmation_count"] == 2
    assert result["long_confirmations"] == {
        "cvd_confirmation": True,
        "buying_footprint_3x_recent": False,
        "price_reclaim": True,
    }
    assert result["risk"]["risk_fraction"] == 0.005
    assert result["setup_state"] == "CONFIRMED"


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

    # One adverse CVD observation is noise and must not kill the runner.
    signal["spot"] = 111.0
    signal["cvd"]["series"] = [{"cvd": 1}, {"cvd": 3}, {"cvd": 2}]
    held = book.sync([signal], now.replace(minute=36))
    assert held["open_count"] == 1

    # Two consecutive adverse observations confirm the reversal.
    signal["cvd"]["series"] = [{"cvd": 3}, {"cvd": 2}, {"cvd": 1}]
    closed = book.sync([signal], now.replace(minute=39))
    assert closed["open_count"] == 0
    assert closed["closed_positions"][-1]["exit_reason"] == "cvd_reversal"


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


def test_paper_book_consumes_setup_once_and_does_not_reenter(tmp_path) -> None:
    book = ConvergencePaperBook(tmp_path / "paper.json")
    now = datetime(2026, 7, 13, 10, 30, tzinfo=IST)
    signal = {
        "symbol": "NIFTY", "status": "actionable_paper", "action": "LONG", "spot": 100.0,
        "long_setup": {"bar_time": "2026-07-13T10:27:00+05:30"},
        "risk": {"entry": 100.0, "stop": 99.0, "target1": 101.0, "target2_long": 103.0, "lot_size": 50, "risk_fraction": 0.005},
        "cvd": {"series": [{"cvd": 1}, {"cvd": 2}, {"cvd": 3}]},
    }

    assert book.sync([signal], now)["open_count"] == 1
    signal["spot"] = 98.5
    stopped = book.sync([signal], now + timedelta(minutes=1))
    assert stopped["open_count"] == 0
    signal["spot"] = 100.0
    assert book.sync([signal], now + timedelta(minutes=2))["open_count"] == 0


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


# ── Commodity (MCX) variant ────────────────────────────────────────────────


def test_commodity_aggregate_bars_one_to_three_minute() -> None:
    from institutional_convergence.commodity import aggregate_bars

    base = datetime(2026, 7, 13, 9, 0, tzinfo=IST)
    rows = [
        {"time": base, "open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5, "volume": 10},
        {"time": base.replace(minute=1), "open": 100.5, "high": 102.0, "low": 100.0, "close": 101.5, "volume": 20},
        {"time": base.replace(minute=2), "open": 101.5, "high": 101.8, "low": 100.8, "close": 101.0, "volume": 15},
        {"time": base.replace(minute=3), "open": 101.0, "high": 101.2, "low": 100.5, "close": 100.7, "volume": 5},
    ]

    bars = aggregate_bars(rows, minutes=3)

    assert len(bars) == 2
    first = bars[0]
    assert first["open"] == 100.0
    assert first["high"] == 102.0
    assert first["low"] == 99.5
    assert first["close"] == 101.0
    assert first["volume"] == 45
    assert bars[1]["open"] == 101.0


def test_commodity_engine_disables_nse_session_gates() -> None:
    """At NSE noon with no VIX, the commodity variant's data gates must pass
    (they are NSE-session concepts) while the NSE defaults block both."""
    from institutional_convergence.engine import evaluate_rules

    noon = datetime(2026, 7, 13, 12, 15, tzinfo=IST)
    bars = [
        {"time": noon.replace(hour=9, minute=15 + 3 * i), "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 100}
        for i in range(6)
    ]

    nse = evaluate_rules(
        symbol="GOLD", current_bars=bars, prior_bars=bars, history_bars=bars,
        ticks=[], options={}, vix=None, lot_size=10, tick_size=1.0,
        clock_drift_ms=100.0, now=noon,
    )
    commodity = evaluate_rules(
        symbol="GOLD", current_bars=bars, prior_bars=bars, history_bars=bars,
        ticks=[], options={}, vix=None, lot_size=10, tick_size=1.0,
        clock_drift_ms=100.0, now=noon,
        noon_quarantine=False, require_vix=False, kind="commodity",
    )

    assert nse["long_gates"]["outside_noon_quarantine"] is False
    assert nse["long_gates"]["vix_available"] is False
    assert commodity["long_gates"]["outside_noon_quarantine"] is True
    assert commodity["long_gates"]["vix_available"] is True
    assert commodity["kind"] == "commodity"
    # Honest-data gates stay identical: no real ticks -> no signal either way.
    assert commodity["long_gates"]["real_tick_cvd"] is False
    assert commodity["action"] == "FLAT"


def test_commodity_paper_book_uses_evening_squareoff(tmp_path) -> None:
    from datetime import time as dt_time

    book = ConvergencePaperBook(tmp_path / "paper.json", squareoff=dt_time(23, 15), entry_quarantine=None)
    signal = {
        "symbol": "GOLD", "status": "actionable_paper", "action": "LONG", "spot": 100.0,
        "risk": {"entry": 100.0, "stop": 90.0, "target1": 110.0, "target2_long": None, "lot_size": 10, "risk_fraction": 0.01},
        "cvd": {"series": [{"cvd": 1}, {"cvd": 2}]},
    }
    # NSE noon does NOT quarantine the MCX book, and 16:00 (past the NSE
    # square-off) does NOT close the evening-session position.
    opened = book.sync([signal], datetime(2026, 7, 13, 12, 0, tzinfo=IST))
    assert opened["open_count"] == 1
    held = book.sync([signal], datetime(2026, 7, 13, 16, 0, tzinfo=IST))
    assert held["open_count"] == 1
    # 23:20 crosses the MCX square-off boundary.
    squared = book.sync([signal], datetime(2026, 7, 13, 23, 20, tzinfo=IST))
    assert squared["open_count"] == 0
    assert squared["closed_positions"][-1]["exit_reason"] == "intraday_squareoff"


def test_paper_book_squares_off_positions_whose_symbol_left_the_universe(tmp_path) -> None:
    """A symbol that rotates out of the universe stops producing marks — its
    position must still square off at the boundary instead of living forever."""
    book = ConvergencePaperBook(tmp_path / "paper.json")
    signal = {
        "symbol": "NIFTY", "status": "actionable_paper", "action": "LONG", "spot": 100.0,
        "risk": {"entry": 100.0, "stop": 90.0, "target1": 110.0, "target2_long": None, "lot_size": 50, "risk_fraction": 0.01},
        "cvd": {"series": [{"cvd": 1}, {"cvd": 2}]},
    }
    opened = book.sync([signal], datetime(2026, 7, 13, 10, 30, tzinfo=IST))
    assert opened["open_count"] == 1

    # Symbol absent from results after universe rotation; square-off time hits.
    squared = book.sync([], datetime(2026, 7, 13, 15, 26, tzinfo=IST))
    assert squared["open_count"] == 0
    assert squared["closed_positions"][-1]["exit_reason"] == "intraday_squareoff_stale_mark"


def test_commodity_closed_cycle_does_not_build(monkeypatch) -> None:
    import asyncio as _asyncio

    from institutional_convergence import commodity as commodity_module

    calls = {"universe": 0}

    async def _fail_universe():
        calls["universe"] += 1
        raise AssertionError("universe should not build while MCX is closed")

    monkeypatch.setattr(commodity_module.commodity_convergence_service, "build_universe", _fail_universe)
    monkeypatch.setattr(
        commodity_module,
        "_now_ist",
        lambda: datetime(2026, 7, 12, 3, 0, tzinfo=IST),  # Sunday, MCX closed
    )

    payload = _asyncio.run(commodity_module.commodity_convergence_service.run_cycle())

    assert payload["status"] == "market_closed"
    assert calls["universe"] == 0


def test_commodity_configured_roots_canonicalize_and_dedupe(monkeypatch) -> None:
    from core.config import settings as app_settings
    from institutional_convergence.commodity import configured_roots

    monkeypatch.setattr(
        app_settings,
        "INSTITUTIONAL_CONVERGENCE_COMMODITY_SYMBOLS",
        "gold, MCX:CRUDEOIL26JULFUT, gold, natgas",
        raising=False,
    )

    roots = configured_roots()

    assert roots[0] == "GOLD"
    assert "CRUDEOIL" in roots
    assert len([r for r in roots if r == "GOLD"]) == 1
