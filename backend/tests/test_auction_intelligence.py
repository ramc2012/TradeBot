from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auction_intelligence.config import clone_default_config
from auction_intelligence.market_profile import MarketProfileEngine
from auction_intelligence.order_flow import OrderFlowEngine
from auction_intelligence.regime import RegimeEngine
from auction_intelligence.risk import RiskGovernor
from auction_intelligence.schemas import (
    DepthLevel,
    DepthSnapshot,
    MarketBar,
    PortfolioSnapshot,
    QuoteSnapshot,
    SessionContext,
    TradePrint,
)
from auction_intelligence.service import AuctionIntelligenceService


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
