from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.routers import auction_intelligence as auction_intelligence_router
from api.routers.auction_intelligence import router
from auction_intelligence.config import clone_default_config
from auction_intelligence.demo import build_demo_analysis, build_demo_validation_series
from auction_intelligence.live import (
    _build_quote_from_snapshot,
    _load_portfolio_snapshot,
    _normalize_portfolio_symbol,
    available_live_symbols,
)
from auction_intelligence.market_profile import MarketProfileEngine
from auction_intelligence.order_flow import OrderFlowEngine
from auction_intelligence.paper.service import PaperTradingService
from auction_intelligence.regime import RegimeEngine
from auction_intelligence.risk import RiskGovernor
from auction_intelligence.schemas import (
    AgentContext,
    DepthLevel,
    DepthSnapshot,
    MarketBar,
    MarketProfileSnapshot,
    OrderFlowSnapshot,
    PortfolioSnapshot,
    QuoteSnapshot,
    RegimeAssessment,
    SessionContext,
    TradePrint,
)
from auction_intelligence.agents.swing import SwingAgent
from auction_intelligence.service import AuctionIntelligenceService
from auction_intelligence.validation.engine import GateAValidator
from auction_intelligence.validation.gate_b import GateBValidator
from auction_intelligence.validation.gate_c import GateCValidator


def _make_bars(start: datetime, rows: list[tuple[float, float, float, float]]) -> list[MarketBar]:
    bars: list[MarketBar] = []
    for index, (open_, high, low, close) in enumerate(rows):
        bars.append(
            MarketBar(
                timestamp=start + timedelta(minutes=30 * index),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=1000 + (index * 100),
            )
        )
    return bars


def _make_trades(start: datetime) -> list[TradePrint]:
    return [
        TradePrint(timestamp=start + timedelta(seconds=10), price=105.1, quantity=40, aggressor_side="buy"),
        TradePrint(timestamp=start + timedelta(seconds=20), price=105.2, quantity=30, aggressor_side="buy"),
        TradePrint(timestamp=start + timedelta(seconds=30), price=105.0, quantity=10, aggressor_side="sell"),
        TradePrint(timestamp=start + timedelta(seconds=40), price=105.3, quantity=20, aggressor_side="buy"),
    ]


def test_market_profile_builds_tpo_and_comparative_metrics() -> None:
    config = clone_default_config()
    engine = MarketProfileEngine(config["market_profile"])
    start = datetime(2026, 4, 1, 9, 15)

    prior = engine.build_profile(
        "NIFTY FUT",
        _make_bars(start - timedelta(days=1), [(100, 101, 99.5, 100.5), (100.5, 101.5, 100, 101)]),
    )
    current = engine.build_profile(
        "NIFTY FUT",
        _make_bars(start, [(101, 102, 100.5, 101.8), (101.8, 103, 101.5, 102.8), (102.8, 104, 102.5, 103.8), (103.8, 105, 103.5, 104.8)]),
        prior_profile=prior,
    )

    assert current.period_count == 4
    assert current.initial_balance_high == 103
    assert current.initial_balance_low == 100.5
    assert current.range_extension_up == 2
    assert current.val <= current.poc <= current.vah
    assert current.value_area_overlap is not None
    assert current.poc_shift is not None


def test_order_flow_engine_computes_positive_imbalance_and_micro_price() -> None:
    config = clone_default_config()
    engine = OrderFlowEngine(config["order_flow"])
    start = datetime(2026, 4, 1, 10, 30)

    snapshot = engine.compute(
        quote=QuoteSnapshot(timestamp=start, bid=105.0, ask=105.5, bid_size=500, ask_size=250),
        trades=_make_trades(start),
        depth=DepthSnapshot(
            timestamp=start,
            bids=[DepthLevel(price=105.0, quantity=500), DepthLevel(price=104.5, quantity=450)],
            asks=[DepthLevel(price=105.5, quantity=250), DepthLevel(price=106.0, quantity=200)],
        ),
        tick_size=0.5,
    )

    assert snapshot.top_imbalance > 0
    assert snapshot.depth_imbalance > 0
    assert snapshot.delta > 0
    assert snapshot.micro_price > snapshot.mid_price
    assert snapshot.passive_fill_probability > 0


def test_regime_engine_classifies_higher_acceptance() -> None:
    config = clone_default_config()
    mp_engine = MarketProfileEngine(config["market_profile"])
    regime_engine = RegimeEngine(config["regime"])
    flow_engine = OrderFlowEngine(config["order_flow"])
    start = datetime(2026, 4, 1, 9, 15)

    prior = mp_engine.build_profile(
        "NIFTY FUT",
        _make_bars(start - timedelta(days=1), [(100, 101, 99.5, 100.5), (100.5, 101.5, 100, 101.0), (101.0, 101.5, 100.5, 101.2)]),
    )
    current = mp_engine.build_profile(
        "NIFTY FUT",
        _make_bars(start, [(101.5, 102.5, 101.0, 102.3), (102.3, 103.2, 102.0, 103.0), (103.0, 104.0, 102.8, 103.9), (103.9, 105.2, 103.8, 105.0)]),
        prior_profile=prior,
    )
    flow = flow_engine.compute(
        quote=QuoteSnapshot(timestamp=start, bid=105.0, ask=105.5, bid_size=500, ask_size=250),
        trades=_make_trades(start),
        tick_size=0.5,
    )
    regime = regime_engine.classify(current=current, prior=prior, order_flow=flow)

    assert regime.label in {"breakout_acceptance", "trend_continuation", "trend_day"}
    assert "LONG" in regime.allowed_directions


def test_service_produces_swing_execution_plan_and_journal(tmp_path) -> None:
    config = clone_default_config()
    config["paper_trading"]["journal_root"] = str(tmp_path)
    service = AuctionIntelligenceService(config)
    start = datetime(2026, 4, 1, 9, 15)

    bundle, journal_paths = service.analyze_and_record_paper(
        session=SessionContext(
            symbol="NIFTY FUT",
            session_date=date(2026, 4, 1),
            last_price=105.0,
            stale_data_seconds=0.0,
            minutes_to_close=180,
        ),
        bars=_make_bars(start, [(101.5, 102.5, 101.0, 102.3), (102.3, 103.2, 102.0, 103.0), (103.0, 104.0, 102.8, 103.9), (103.9, 105.2, 103.8, 105.0)]),
        prior_bars=_make_bars(start - timedelta(days=1), [(100, 101, 99.5, 100.5), (100.5, 101.5, 100, 101.0), (101.0, 101.5, 100.5, 101.2)]),
        quote=QuoteSnapshot(timestamp=start, bid=105.0, ask=105.5, bid_size=500, ask_size=250),
        trades=_make_trades(start),
        portfolio=PortfolioSnapshot(),
    )

    assert bundle.risk.allowed is True
    assert any(decision.agent_name == "swing" and decision.action != "FLAT" for decision in bundle.agent_decisions)
    assert bundle.execution_plan
    assert journal_paths
    assert tmp_path.joinpath("nifty_fut.jsonl").exists()


def test_swing_agent_uses_contract_aware_margin_sizing() -> None:
    config = clone_default_config()
    agent = SwingAgent(config["agents"]["swing"])

    decision = agent.evaluate(
        AgentContext(
            session=SessionContext(
                symbol="BANKNIFTY FUT",
                session_date=date(2026, 4, 2),
                last_price=52040.0,
            ),
            portfolio=PortfolioSnapshot(net_liquidation=1_000_000.0),
            current_profile=MarketProfileSnapshot(
                symbol="BANKNIFTY FUT",
                session_date="2026-04-02",
                period_minutes=30,
                tick_size=0.5,
                open_price=51910.0,
                high_price=52120.0,
                low_price=51780.0,
                close_price=52040.0,
                total_volume=100000.0,
                tpo_counts={52000.0: 3},
                tpo_letters={52000.0: "ABCD"},
                poc=51960.0,
                vah=52060.0,
                val=51880.0,
                initial_balance_high=52010.0,
                initial_balance_low=51810.0,
                initial_balance_range=200.0,
                day_range=340.0,
                range_extension_up=110.0,
                range_extension_down=0.0,
                single_prints=[],
                buying_tail=[],
                selling_tail=[],
                poor_high=False,
                poor_low=False,
                excess_high=0.0,
                excess_low=0.0,
                spike_direction="up",
                spike_price=None,
                period_count=4,
                sample_count=4,
            ),
            prior_profile=None,
            order_flow=OrderFlowSnapshot(
                spread=1.0,
                mid_price=52040.0,
                micro_price=52040.4,
                top_imbalance=0.3,
                depth_imbalance=0.25,
                aggressive_buy_volume=1000.0,
                aggressive_sell_volume=600.0,
                delta=400.0,
                cumulative_delta=1600.0,
                vwap=51980.0,
                vwap_drift=60.0,
                queue_pressure=0.2,
                volatility_burst=1.1,
                passive_fill_probability=0.55,
                aggressive_fill_probability=0.8,
                adverse_selection_risk=0.2,
                timing_confidence=0.6,
                execution_aggression="PASSIVE",
                micro_stop_distance=20.0,
            ),
            regime=RegimeAssessment(
                label="trend_continuation",
                confidence=0.8,
                allowed_directions=["LONG"],
                reasons=["Value migrated higher after acceptance above prior value."],
            ),
            config=config,
        )
    )

    assert decision.action == "LONG"
    assert decision.quantity >= 15
    assert decision.metadata["margin_fraction_per_lot"] == 0.18


def test_risk_governor_blocks_stale_and_loss_breach() -> None:
    config = clone_default_config()
    governor = RiskGovernor(config["risk"])
    decision_bundle = [
        service_decision
        for service_decision in AuctionIntelligenceService(config).analyze(
            session=SessionContext(
                symbol="NIFTY FUT",
                session_date=date(2026, 4, 1),
                last_price=105.0,
                stale_data_seconds=0.0,
                minutes_to_close=180,
            ),
            bars=_make_bars(datetime(2026, 4, 1, 9, 15), [(101.5, 102.5, 101.0, 102.3), (102.3, 103.2, 102.0, 103.0), (103.0, 104.0, 102.8, 103.9), (103.9, 105.2, 103.8, 105.0)]),
            prior_bars=_make_bars(datetime(2026, 3, 31, 9, 15), [(100, 101, 99.5, 100.5), (100.5, 101.5, 100, 101.0), (101.0, 101.5, 100.5, 101.2)]),
            quote=QuoteSnapshot(timestamp=datetime(2026, 4, 1, 10, 0), bid=105.0, ask=105.5, bid_size=500, ask_size=250),
            trades=_make_trades(datetime(2026, 4, 1, 10, 0)),
            portfolio=PortfolioSnapshot(),
        ).agent_decisions
    ]

    blocked = governor.evaluate(
        session=SessionContext(
            symbol="NIFTY FUT",
            session_date=date(2026, 4, 1),
            last_price=105.0,
            stale_data_seconds=15.0,
            minutes_to_close=180,
        ),
        portfolio=PortfolioSnapshot(daily_realized_pnl=-100000.0),
        decisions=decision_bundle,
    )

    assert blocked.allowed is False
    assert blocked.kill_switch is True
    assert "Market data is stale." in blocked.reasons
    assert "Daily loss limit breached." in blocked.reasons


def test_risk_governor_uses_margin_ratio_for_projected_futures_exposure() -> None:
    config = clone_default_config()
    governor = RiskGovernor({**config["risk"], "contract_specs": config["contract_specs"]})

    allowed = governor.evaluate(
        session=SessionContext(
            symbol="BANKNIFTY FUT",
            session_date=date(2026, 4, 2),
            last_price=52040.0,
            stale_data_seconds=0.0,
            minutes_to_close=180,
        ),
        portfolio=PortfolioSnapshot(
            net_liquidation=1_000_000.0,
            symbol_exposure={"BANKNIFTY FUT": 0.16},
            correlated_exposure=0.16,
        ),
        decisions=[
            SwingAgent(config["agents"]["swing"]).evaluate(
                AgentContext(
                    session=SessionContext(
                        symbol="BANKNIFTY FUT",
                        session_date=date(2026, 4, 2),
                        last_price=52040.0,
                    ),
                    portfolio=PortfolioSnapshot(net_liquidation=1_000_000.0),
                    current_profile=MarketProfileSnapshot(
                        symbol="BANKNIFTY FUT",
                        session_date="2026-04-02",
                        period_minutes=30,
                        tick_size=0.5,
                        open_price=51910.0,
                        high_price=52120.0,
                        low_price=51780.0,
                        close_price=52040.0,
                        total_volume=100000.0,
                        tpo_counts={52000.0: 3},
                        tpo_letters={52000.0: "ABCD"},
                        poc=51960.0,
                        vah=52060.0,
                        val=51880.0,
                        initial_balance_high=52010.0,
                        initial_balance_low=51810.0,
                        initial_balance_range=200.0,
                        day_range=340.0,
                        range_extension_up=110.0,
                        range_extension_down=0.0,
                        single_prints=[],
                        buying_tail=[],
                        selling_tail=[],
                        poor_high=False,
                        poor_low=False,
                        excess_high=0.0,
                        excess_low=0.0,
                        spike_direction="up",
                        spike_price=None,
                        period_count=4,
                        sample_count=4,
                    ),
                    prior_profile=None,
                    order_flow=OrderFlowSnapshot(
                        spread=1.0,
                        mid_price=52040.0,
                        micro_price=52040.4,
                        top_imbalance=0.3,
                        depth_imbalance=0.25,
                        aggressive_buy_volume=1000.0,
                        aggressive_sell_volume=600.0,
                        delta=400.0,
                        cumulative_delta=1600.0,
                        vwap=51980.0,
                        vwap_drift=60.0,
                        queue_pressure=0.2,
                        volatility_burst=1.1,
                        passive_fill_probability=0.55,
                        aggressive_fill_probability=0.8,
                        adverse_selection_risk=0.2,
                        timing_confidence=0.6,
                        execution_aggression="PASSIVE",
                        micro_stop_distance=20.0,
                    ),
                    regime=RegimeAssessment(
                        label="trend_continuation",
                        confidence=0.8,
                        allowed_directions=["LONG"],
                        reasons=["Value migrated higher after acceptance above prior value."],
                    ),
                    config=config,
                )
            )
        ],
    )

    assert allowed.allowed is False
    assert "BANKNIFTY FUT projected margin exposure would exceed cap." in allowed.reasons


def test_live_quote_builder_prefers_quote_override_for_live_session() -> None:
    start = datetime(2026, 4, 2, 11, 15)
    quote, source, stale_seconds = _build_quote_from_snapshot(
        [{"time": start.isoformat(), "open": 52000.0, "high": 52020.0, "low": 51980.0, "close": 52010.0, "volume": 1000.0}],
        None,
        quote_override={
            "timestamp": start.isoformat(),
            "bid": 52009.5,
            "ask": 52010.5,
            "bid_size": 210.0,
            "ask_size": 175.0,
            "last_price": 52010.0,
        },
        tick_size=0.5,
        snapshot_mode="live_session",
    )

    assert source == "rest_quote"
    assert quote["last_price"] == 52010.0
    assert quote["bid"] == 52009.5
    assert stale_seconds >= 0.0


def test_normalize_portfolio_symbol_maps_index_and_futures_aliases() -> None:
    assert _normalize_portfolio_symbol("NSE:BANKNIFTY26APRFUT", "FUTIDX") == "BANKNIFTY FUT"
    assert _normalize_portfolio_symbol("NSE:NIFTY50-INDEX", "INDEX") == "NIFTY INDEX"


def test_load_portfolio_snapshot_uses_margin_based_symbol_exposure(monkeypatch) -> None:
    class StubFunds:
        total_balance = 1_000_000.0
        available_cash = 1_000_000.0
        realized_pnl = 12_500.0

    class StubPosition:
        def __init__(self, symbol: str, instrument_type: str, qty: int, avg_price: float, ltp: float):
            self.symbol = symbol
            self.instrument_type = instrument_type
            self.qty = qty
            self.avg_price = avg_price
            self.ltp = ltp

    class StubAdapter:
        async def get_funds(self):
            return StubFunds()

        async def get_positions(self):
            return [
                StubPosition("NSE:BANKNIFTY26APRFUT", "FUTIDX", 15, 52000.0, 52100.0),
                StubPosition("NSE:NIFTY26APRFUT", "FUTIDX", 25, 22800.0, 22900.0),
            ]

    monkeypatch.setattr("auction_intelligence.live.get_active_adapter", lambda broker=None: StubAdapter())

    snapshot = asyncio.run(_load_portfolio_snapshot("BANKNIFTY FUT"))

    assert snapshot["daily_realized_pnl"] == 12500.0
    assert snapshot["open_positions"] == 2
    assert snapshot["symbol_exposure"]["BANKNIFTY FUT"] == 0.1407
    assert snapshot["symbol_exposure"]["NIFTY FUT"] == 0.1031
    assert snapshot["correlated_exposure"] == 0.2438


def test_demo_analysis_exposes_request_and_analysis_payload() -> None:
    demo = build_demo_analysis("NIFTY", "acceptance_up")

    assert demo["symbol_code"] == "NIFTY"
    assert demo["scenario"] == "acceptance_up"
    assert demo["request"]["session"]["symbol"] == "NIFTY FUT"
    assert demo["analysis"]["regime"]["label"] in {
        "breakout_acceptance",
        "trend_continuation",
        "trend_day",
    }
    assert demo["analysis"]["agent_decisions"]


def test_live_symbol_registry_includes_supported_index_symbols() -> None:
    assert {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}.issubset(set(available_live_symbols()))


def test_live_snapshot_rejects_unknown_symbol_with_client_error() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get("/api/auction-intelligence/live-snapshot", params={"symbol": "UNKNOWN"})

    assert response.status_code == 400
    assert "Unsupported live symbol" in response.json()["detail"]


def test_gate_a_validator_passes_on_consistent_session() -> None:
    config = clone_default_config()
    validator = GateAValidator(config)
    start = datetime(2026, 4, 1, 9, 15)

    report = validator.validate(
        session=SessionContext(
            symbol="NIFTY FUT",
            session_date=date(2026, 4, 1),
            last_price=105.0,
        ),
        bars=_make_bars(start, [(101.5, 102.5, 101.0, 102.3), (102.3, 103.2, 102.0, 103.0), (103.0, 104.0, 102.8, 103.9), (103.9, 105.2, 103.8, 105.0)]),
        prior_bars=_make_bars(start - timedelta(days=1), [(100, 101, 99.5, 100.5), (100.5, 101.5, 100, 101.0), (101.0, 101.5, 100.5, 101.2), (101.2, 101.8, 100.9, 101.5)]),
    )

    assert report.passed is True
    assert report.metrics["current_session_gaps"] == 0
    assert report.metrics["current_duplicate_timestamps"] == 0


def test_gate_a_validator_flags_duplicates_and_gaps() -> None:
    config = clone_default_config()
    validator = GateAValidator(config)
    session = SessionContext(
        symbol="NIFTY FUT",
        session_date=date(2026, 4, 1),
        last_price=105.0,
    )
    bars = [
        MarketBar(timestamp=datetime(2026, 4, 1, 9, 15), open=101, high=102, low=100.5, close=101.8, volume=1000),
        MarketBar(timestamp=datetime(2026, 4, 1, 10, 15), open=101.8, high=103, low=101.5, close=102.6, volume=1200),
        MarketBar(timestamp=datetime(2026, 4, 1, 10, 15), open=102.6, high=103.5, low=102.3, close=103.2, volume=1300),
        MarketBar(timestamp=datetime(2026, 4, 1, 10, 45), open=103.2, high=104.5, low=103.0, close=104.1, volume=1500),
    ]
    prior_bars = _make_bars(datetime(2026, 3, 31, 9, 15), [(100, 101, 99.5, 100.5), (100.5, 101.5, 100, 101.0), (101.0, 101.5, 100.5, 101.2), (101.2, 101.8, 100.9, 101.5)])

    report = validator.validate(session=session, bars=bars, prior_bars=prior_bars)

    assert report.passed is False
    assert report.metrics["current_duplicate_timestamps"] == 1
    assert report.metrics["current_session_gaps"] >= 1


def test_validate_gate_a_endpoint_returns_report(monkeypatch) -> None:
    monkeypatch.setattr(
        auction_intelligence_router._validation_store,
        "record_report",
        AsyncMock(return_value={"persisted": False, "error": "disabled_in_test"}),
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    start = datetime(2026, 4, 1, 9, 15)

    payload = {
        "session": {
            "symbol": "NIFTY FUT",
            "session_date": "2026-04-01",
            "last_price": 105.0,
            "stale_data_seconds": 0.0,
            "minutes_to_close": 180,
            "broker_connected": True,
        },
        "quote": {
            "timestamp": start.isoformat(),
            "bid": 105.0,
            "ask": 105.5,
            "bid_size": 500,
            "ask_size": 250,
        },
        "bars": [
            {"timestamp": (start + timedelta(minutes=30 * index)).isoformat(), "open": row[0], "high": row[1], "low": row[2], "close": row[3], "volume": 1000 + (index * 100)}
            for index, row in enumerate([(101.5, 102.5, 101.0, 102.3), (102.3, 103.2, 102.0, 103.0), (103.0, 104.0, 102.8, 103.9), (103.9, 105.2, 103.8, 105.0)])
        ],
        "prior_bars": [
            {"timestamp": (start - timedelta(days=1) + timedelta(minutes=30 * index)).isoformat(), "open": row[0], "high": row[1], "low": row[2], "close": row[3], "volume": 1000 + (index * 100)}
            for index, row in enumerate([(100, 101, 99.5, 100.5), (100.5, 101.5, 100, 101.0), (101.0, 101.5, 100.5, 101.2), (101.2, 101.8, 100.9, 101.5)])
        ],
        "trades": [],
        "portfolio": {},
    }

    response = client.post("/api/auction-intelligence/validate-gate-a", json=payload)

    assert response.status_code == 200
    report = response.json()
    assert report["gate"] == "gate_a"
    assert "checks" in report
    assert "metrics" in report


def test_demo_validation_series_builds_multi_session_history() -> None:
    payload = build_demo_validation_series("BANKNIFTY", "acceptance_up", session_count=6)

    assert payload["symbol_code"] == "BANKNIFTY"
    assert payload["source"] == "demo_series"
    assert len(payload["sessions"]) >= 6
    assert all(session["bars"] for session in payload["sessions"])


def test_gate_b_validator_generates_rule_metrics_from_demo_series() -> None:
    config = clone_default_config()
    validator = GateBValidator(config)
    payload = build_demo_validation_series("BANKNIFTY", "acceptance_up", session_count=6)
    sessions = [
        [
            MarketBar(
                timestamp=datetime.fromisoformat(item["timestamp"]),
                open=item["open"],
                high=item["high"],
                low=item["low"],
                close=item["close"],
                volume=item.get("volume", 0.0),
            )
            for item in session["bars"]
        ]
        for session in payload["sessions"]
    ]

    report = validator.validate(
        symbol="BANKNIFTY FUT",
        sessions=sessions,
        mode="demo",
        source=payload["source"],
    )

    assert report.gate == "gate_b"
    assert report.metrics["session_count"] >= 6
    assert "walk_forward_windows" in report.metrics
    assert "evaluated_trades" in report.metrics
    assert "setup_attribution" in report.metrics
    assert "flat_reason_attribution" in report.metrics
    assert "blocking_reason_attribution" in report.metrics
    assert report.artifacts
    assert any(artifact.artifact_type == "gate_b_session" for artifact in report.artifacts)
    skipped = next(
        (artifact for artifact in report.artifacts if artifact.artifact_type == "gate_b_session" and artifact.payload.get("status") == "skipped"),
        None,
    )
    assert skipped is not None
    assert "flat_reason" in skipped.payload
    assert "blocking_reasons" in skipped.payload
    assert any(check.key == "trade_count" for check in report.checks)


def test_validate_gate_b_demo_endpoint_returns_report(monkeypatch) -> None:
    monkeypatch.setattr(
        auction_intelligence_router._validation_store,
        "record_report",
        AsyncMock(return_value={"persisted": False, "error": "disabled_in_test"}),
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get(
        "/api/auction-intelligence/validate-gate-b",
        params={"symbol": "BANKNIFTY", "mode": "demo", "scenario": "acceptance_up", "session_limit": 6},
    )

    assert response.status_code == 200
    report = response.json()
    assert report["gate"] == "gate_b"
    assert report["series_metadata"]["symbol_code"] == "BANKNIFTY"
    assert report["metrics"]["session_count"] >= 6
    assert report["artifact_count"] >= 1
    assert report["artifacts_preview"]
    assert "flat_reason_attribution" in report["metrics"]


def test_validation_run_artifacts_endpoint_returns_persisted_artifacts(monkeypatch) -> None:
    monkeypatch.setattr(
        auction_intelligence_router._validation_store,
        "list_artifacts",
        AsyncMock(
            return_value=[
                {
                    "artifact_id": "artifact-1",
                    "run_id": "run-1",
                    "artifact_type": "gate_b_session",
                    "artifact_key": "2026-04-01",
                    "payload": {"status": "evaluated", "setup_name": "acceptance_continuation_long"},
                    "created_at": "2026-04-05T00:00:00Z",
                }
            ]
        ),
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get(
        "/api/auction-intelligence/validation-runs/run-1/artifacts",
        params={"artifact_type": "gate_b_session", "limit": 10},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == "run-1"
    assert payload["artifact_type"] == "gate_b_session"
    assert payload["count"] == 1
    assert payload["artifacts"][0]["payload"]["setup_name"] == "acceptance_continuation_long"


def test_gate_c_validator_passes_on_shadow_observation_window() -> None:
    validator = GateCValidator(clone_default_config())
    records = []
    for day_offset in range(20):
        session_date = date(2026, 3, 1) + timedelta(days=day_offset)
        records.append(
            {
                "signal_id": f"signal-{day_offset}",
                "recorded_at": datetime(2026, 3, 1, 10, 0) + timedelta(days=day_offset),
                "session_date": session_date.isoformat(),
                "symbol": "BANKNIFTY FUT",
                "source": "fyers_continuous_futures",
                "snapshot_mode": "live_session",
                "agent_name": "swing",
                "action": "LONG",
                "regime_label": "trend_continuation",
                "setup_name": "acceptance_continuation_long",
                "confidence": 0.72,
                "quantity": 15,
                "entry_price": 52000.0 + day_offset,
                "stop_price": 51880.0 + day_offset,
                "target_price": 52240.0 + day_offset,
                "tick_size": 0.5,
                "risk_allowed": True,
                "kill_switch_active": False,
                "simulated_fill_price": 52000.0 + day_offset,
                "observed_touch_price": 52000.5 + day_offset,
                "observed_fill_price": 52000.5 + day_offset,
                "fill_drift_ticks": 1.0,
                "stale_signal": False,
                "reconciliation_status": "matched",
                "mismatch_duration_seconds": 0.0,
                "kill_switch_tested": day_offset in {2, 15},
                "kill_switch_passed": day_offset in {2, 15},
                "dashboard_checked": day_offset == 0,
                "alerts_checked": day_offset == 1,
                "manual_override_tested": day_offset == 2,
                "metadata": {},
            }
        )

    report = validator.validate(symbol="BANKNIFTY FUT", records=records, session_limit=30)

    assert report.gate == "gate_c"
    assert report.passed is True
    assert report.metrics["session_count"] == 20
    assert report.metrics["fill_drift_median_ticks"] == 1.0
    assert report.metrics["successful_kill_switch_drills"] == 2


def test_shadow_record_live_endpoint_records_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(
        auction_intelligence_router,
        "build_live_analysis",
        AsyncMock(
            return_value={
                "symbol_code": "BANKNIFTY",
                "session_date": "2026-04-02",
                "request": {
                    "session": {
                        "symbol": "BANKNIFTY FUT",
                        "session_date": "2026-04-02",
                        "stale_data_seconds": 0.0,
                    },
                    "quote": {"bid": 52010.0, "ask": 52011.0},
                    "metadata": {
                        "history_source": "fyers_continuous_futures",
                        "snapshot_mode": "historical_replay",
                        "quote_source": "historical_bar_inference",
                        "history_symbol": "NSE:BANKNIFTY26APRFUT",
                    },
                },
                "analysis": {
                    "market_profile": {"tick_size": 0.5},
                    "regime": {"label": "trend_continuation"},
                    "risk": {"allowed": True, "kill_switch": False, "reasons": ["Risk checks passed."]},
                    "agent_decisions": [
                        {
                            "agent_name": "swing",
                            "action": "LONG",
                            "confidence": 0.7,
                            "entry_price": 52008.0,
                            "stop_price": 51950.0,
                            "target_price": 52120.0,
                            "quantity": 15,
                            "rationale": ["Acceptance above value."],
                            "metadata": {"setup_name": "acceptance_continuation_long"},
                        }
                    ],
                    "execution_plan": [
                        {"agent_name": "swing", "limit_price": 52008.5}
                    ],
                },
            }
        ),
    )
    monkeypatch.setattr(
        auction_intelligence_router._shadow_store,
        "record_records",
        AsyncMock(return_value={"persisted": True, "record_count": 1, "record_ids": ["shadow-1"]}),
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.post(
        "/api/auction-intelligence/shadow-record-live",
        params={"symbol": "BANKNIFTY"},
        json={"dashboard_checked": True, "alerts_checked": True, "manual_override_tested": True},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["symbol_code"] == "BANKNIFTY"
    assert payload["record_count"] == 1
    assert payload["storage"]["persisted"] is True
    assert payload["records_preview"][0]["fill_drift_ticks"] == 5.0


def test_shadow_records_endpoint_returns_persisted_records(monkeypatch) -> None:
    monkeypatch.setattr(
        auction_intelligence_router._shadow_store,
        "list_records",
        AsyncMock(
            return_value=[
                {
                    "record_id": "shadow-1",
                    "session_date": "2026-04-02",
                    "symbol": "BANKNIFTY FUT",
                    "agent_name": "swing",
                    "action": "LONG",
                }
            ]
        ),
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get("/api/auction-intelligence/shadow-records", params={"symbol": "BANKNIFTY", "limit": 10})

    assert response.status_code == 200
    payload = response.json()
    assert payload["symbol"] == "BANKNIFTY FUT"
    assert payload["count"] == 1
    assert payload["records"][0]["action"] == "LONG"


def test_shadow_backfill_endpoint_records_broker_history_snapshots(monkeypatch) -> None:
    monkeypatch.setattr(
        auction_intelligence_router,
        "build_shadow_backfill_snapshots",
        AsyncMock(
            return_value={
                "symbol_code": "BANKNIFTY",
                "source": "fyers_continuous_futures",
                "history_symbol": "NSE:BANKNIFTY26APRFUT",
                "snapshot_count": 2,
                "skipped_sessions": [{"session_date": "2026-03-12", "error": "missing_prior_session"}],
                "snapshots": [
                    {
                        "symbol_code": "BANKNIFTY",
                        "session_date": "2026-03-13",
                        "request": {
                            "session": {
                                "symbol": "BANKNIFTY FUT",
                                "session_date": "2026-03-13",
                                "stale_data_seconds": 0.0,
                            },
                            "quote": {"bid": 52010.0, "ask": 52011.0},
                            "metadata": {
                                "history_source": "fyers_continuous_futures",
                                "snapshot_mode": "historical_replay",
                                "snapshot_time": "2026-03-13T12:20:00+05:30",
                                "quote_source": "historical_bar_inference",
                                "history_symbol": "NSE:BANKNIFTY26APRFUT",
                            },
                        },
                        "analysis": {
                            "market_profile": {"tick_size": 0.5},
                            "regime": {"label": "trend_continuation"},
                            "risk": {"allowed": True, "kill_switch": False, "reasons": ["Risk checks passed."]},
                            "agent_decisions": [
                                {
                                    "agent_name": "swing",
                                    "action": "LONG",
                                    "confidence": 0.71,
                                    "entry_price": 52008.0,
                                    "stop_price": 51950.0,
                                    "target_price": 52120.0,
                                    "quantity": 15,
                                    "rationale": ["Acceptance above value."],
                                    "metadata": {"setup_name": "acceptance_continuation_long"},
                                }
                            ],
                            "execution_plan": [{"agent_name": "swing", "limit_price": 52008.5}],
                        },
                    }
                ],
            }
        ),
    )
    monkeypatch.setattr(
        auction_intelligence_router._shadow_store,
        "record_records",
        AsyncMock(return_value={"persisted": True, "record_count": 1, "record_ids": ["shadow-1"]}),
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.post(
        "/api/auction-intelligence/shadow-backfill",
        params={"symbol": "BANKNIFTY", "session_limit": 20, "lookback_days": 45},
        json={"dashboard_checked": True, "alerts_checked": True, "manual_override_tested": True},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["symbol_code"] == "BANKNIFTY"
    assert payload["snapshot_count"] == 2
    assert payload["record_count"] == 1
    assert payload["storage"]["persisted"] is True
    assert payload["skipped_sessions"][0]["session_date"] == "2026-03-12"


def test_validate_gate_c_endpoint_returns_report(monkeypatch) -> None:
    monkeypatch.setattr(
        auction_intelligence_router._shadow_store,
        "list_records",
        AsyncMock(
            return_value=[
                {
                    "signal_id": "shadow-1",
                    "recorded_at": "2026-03-01T10:00:00Z",
                    "session_date": "2026-03-01",
                    "symbol": "BANKNIFTY FUT",
                    "agent_name": "swing",
                    "action": "LONG",
                    "fill_drift_ticks": 1.0,
                    "tick_size": 0.5,
                    "stale_signal": False,
                    "reconciliation_status": "matched",
                    "mismatch_duration_seconds": 0.0,
                    "kill_switch_tested": True,
                    "kill_switch_passed": True,
                    "dashboard_checked": True,
                    "alerts_checked": True,
                    "manual_override_tested": True,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        auction_intelligence_router._validation_store,
        "record_report",
        AsyncMock(return_value={"persisted": False, "error": "disabled_in_test"}),
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get(
        "/api/auction-intelligence/validate-gate-c",
        params={"symbol": "BANKNIFTY", "session_limit": 30, "record_limit": 10},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["gate"] == "gate_c"
    assert payload["series_metadata"]["symbol"] == "BANKNIFTY FUT"
    assert "fill_drift_median_ticks" in payload["metrics"]
    assert "artifacts_preview" in payload


def test_latest_validation_run_endpoint_accepts_symbol_filter(monkeypatch) -> None:
    monkeypatch.setattr(
        auction_intelligence_router._validation_store,
        "latest_report",
        AsyncMock(
            return_value={
                "run_id": "run-1",
                "gate": "gate_c",
                "symbol": "BANKNIFTY",
                "passed": True,
                "score": 1.0,
                "created_at": "2026-04-05T00:00:00Z",
                "artifact_counts": {},
                "report": {},
            }
        ),
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get("/api/auction-intelligence/validation-runs/latest", params={"gate": "gate_c", "symbol": "BANKNIFTY"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["symbol"] == "BANKNIFTY"
    assert payload["gate"] == "gate_c"


def test_canary_readiness_endpoint_reports_gate_status(monkeypatch) -> None:
    async def latest_report_stub(*, gate=None, symbol=None):
        if gate == "gate_b":
            return {"gate": "gate_b", "symbol": symbol, "passed": True, "score": 1.0}
        if gate == "gate_c":
            return {"gate": "gate_c", "symbol": symbol, "passed": True, "score": 1.0}
        return None

    monkeypatch.setattr(auction_intelligence_router._validation_store, "latest_report", latest_report_stub)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get("/api/auction-intelligence/canary-readiness", params={"symbol": "BANKNIFTY"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["symbol"] == "BANKNIFTY"
    assert payload["ready"] is True
    assert payload["blockers"] == []
    assert payload["requirements"]["manual_approval_required"] is True


def test_paper_service_resolves_relative_root_under_backend_runtime() -> None:
    service = PaperTradingService("runtime/auction_intelligence")

    assert service.writer.root == Path(__file__).resolve().parents[1] / "runtime" / "auction_intelligence"
