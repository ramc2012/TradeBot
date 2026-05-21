from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
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
    _build_quote_history_from_ticks,
    _build_trade_prints_from_ticks,
    _build_quote_from_snapshot,
    build_live_analysis,
    _fetch_recent_minute_rows,
    _group_rows_by_session,
    _load_portfolio_snapshot,
    _normalize_portfolio_symbol,
    available_live_symbols,
)
from auction_intelligence.market_profile import MarketProfileEngine
from auction_intelligence.order_flow import OrderFlowEngine
from auction_intelligence.options import OptionStrategyMapper
from auction_intelligence.paper.service import PaperTradingService
from auction_intelligence.regime import RegimeEngine
from auction_intelligence.risk import RiskGovernor
from auction_intelligence.schemas import (
    AgentDecision,
    AgentContext,
    AnalysisBundle,
    DepthLevel,
    DepthSnapshot,
    NTMVolXLevel,
    NTMVolXSnapshot,
    RiskDecision,
    MarketBar,
    MarketProfileSnapshot,
    OrderFlowSnapshot,
    PortfolioSnapshot,
    QuoteSnapshot,
    RegimeAssessment,
    SessionContext,
    TradePrint,
    ExecutionInstruction,
)
from auction_intelligence.agents.positional import PositionalAgent
from auction_intelligence.agents.scalp import ScalpAgent
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


def _make_profile_snapshot(**overrides) -> MarketProfileSnapshot:
    base = {
        "symbol": "NIFTY FUT",
        "session_date": "2026-04-02",
        "period_minutes": 30,
        "tick_size": 0.5,
        "open_price": 22480.0,
        "high_price": 22560.0,
        "low_price": 22410.0,
        "close_price": 22520.0,
        "total_volume": 100000.0,
        "tpo_counts": {22500.0: 3},
        "tpo_letters": {22500.0: "ABCD"},
        "poc": 22495.0,
        "vah": 22525.0,
        "val": 22455.0,
        "initial_balance_high": 22505.0,
        "initial_balance_low": 22445.0,
        "initial_balance_range": 60.0,
        "day_range": 150.0,
        "range_extension_up": 55.0,
        "range_extension_down": 35.0,
        "single_prints": [],
        "buying_tail": [],
        "selling_tail": [],
        "poor_high": False,
        "poor_low": False,
        "excess_high": 0.0,
        "excess_low": 0.0,
        "spike_direction": "none",
        "spike_price": None,
        "period_count": 4,
        "sample_count": 4,
        "value_area_overlap": 0.4,
        "poc_shift": 20.0,
        "value_migration": 25.0,
        "prior_poc_untouched": True,
        "bracket_state": "expanding",
    }
    base.update(overrides)
    return MarketProfileSnapshot(**base)


def _make_order_flow_snapshot(**overrides) -> OrderFlowSnapshot:
    base = {
        "spread": 1.0,
        "mid_price": 22520.0,
        "micro_price": 22520.4,
        "top_imbalance": 0.2,
        "depth_imbalance": 0.15,
        "aggressive_buy_volume": 800.0,
        "aggressive_sell_volume": 400.0,
        "delta": 400.0,
        "cumulative_delta": 1200.0,
        "vwap": 22500.0,
        "vwap_drift": 20.0,
        "queue_pressure": 0.18,
        "volatility_burst": 1.0,
        "passive_fill_probability": 0.58,
        "aggressive_fill_probability": 0.78,
        "adverse_selection_risk": 0.22,
        "timing_confidence": 0.68,
        "execution_aggression": "PASSIVE",
        "micro_stop_distance": 12.0,
        "trade_imbalance": 0.32,
        "order_flow_imbalance": 0.24,
        "book_pressure": 0.22,
        "micro_price_offset_bps": 0.6,
        "trade_intensity_per_minute": 3.2,
        "quote_repricing_rate": 8.0,
        "toxicity_score": 0.3,
    }
    base.update(overrides)
    return OrderFlowSnapshot(**base)


def _make_ntm_volx_snapshot(**overrides) -> NTMVolXSnapshot:
    base = {
        "underlying": "NIFTY",
        "expiry": "2026-04-09",
        "spot_price": 22520.0,
        "atm_strike": 22500.0,
        "dominant_side": "CALLS",
        "directional_bias": "LONG",
        "regime": "calls_control",
        "vxr": 1.82,
        "call_pressure": 1_800_000.0,
        "put_pressure": 990_000.0,
        "net_pressure": 0.29,
        "call_volume": 52_000.0,
        "put_volume": 33_000.0,
        "call_notional": 4_200_000.0,
        "put_notional": 2_250_000.0,
        "call_oi_change": 3_100.0,
        "put_oi_change": 1_200.0,
        "call_wall_strike": 22550.0,
        "put_wall_strike": 22450.0,
        "pair_count": 5,
        "notes": ["Calls control at 1.82x across 5 NTM pairs."],
        "pressure_ladder": [
            NTMVolXLevel(
                strike=22450.0,
                distance_from_spot=70.0,
                distance_from_spot_pct=0.0031,
                call_volume=9_000.0,
                put_volume=8_400.0,
                call_notional=720_000.0,
                put_notional=560_000.0,
                call_oi_change=420.0,
                put_oi_change=520.0,
                call_pressure=210_000.0,
                put_pressure=260_000.0,
                net_pressure=-0.1064,
            ),
            NTMVolXLevel(
                strike=22500.0,
                distance_from_spot=20.0,
                distance_from_spot_pct=0.0009,
                call_volume=12_000.0,
                put_volume=8_700.0,
                call_notional=1_100_000.0,
                put_notional=610_000.0,
                call_oi_change=900.0,
                put_oi_change=220.0,
                call_pressure=540_000.0,
                put_pressure=220_000.0,
                net_pressure=0.4211,
            ),
            NTMVolXLevel(
                strike=22550.0,
                distance_from_spot=30.0,
                distance_from_spot_pct=0.0013,
                call_volume=11_500.0,
                put_volume=7_100.0,
                call_notional=980_000.0,
                put_notional=430_000.0,
                call_oi_change=1_050.0,
                put_oi_change=180.0,
                call_pressure=620_000.0,
                put_pressure=150_000.0,
                net_pressure=0.6104,
            ),
        ],
    }
    base.update(overrides)
    return NTMVolXSnapshot(**base)


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
    quote_history = [
        QuoteSnapshot(
            timestamp=start + timedelta(seconds=index * 5),
            bid=105.0 + (0.05 * index),
            ask=105.5 + (0.05 * index),
            bid_size=500 + (index * 20),
            ask_size=250 - (index * 10),
        )
        for index in range(4)
    ]

    snapshot = engine.compute(
        quote=quote_history[-1],
        trades=_make_trades(start),
        depth=DepthSnapshot(
            timestamp=start,
            bids=[DepthLevel(price=105.0, quantity=500), DepthLevel(price=104.5, quantity=450)],
            asks=[DepthLevel(price=105.5, quantity=250), DepthLevel(price=106.0, quantity=200)],
        ),
        tick_size=0.5,
        quote_history=quote_history,
    )

    assert snapshot.top_imbalance > 0
    assert snapshot.depth_imbalance > 0
    assert snapshot.delta > 0
    assert snapshot.micro_price > snapshot.mid_price
    assert snapshot.trade_imbalance > 0
    assert snapshot.order_flow_imbalance > 0
    assert snapshot.book_pressure > 0
    assert snapshot.quote_repricing_rate > 0
    assert snapshot.toxicity_score >= 0
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
    config["agents"]["swing"]["enable_acceptance_continuation_long"] = True
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


def test_swing_agent_trend_pullback_long_is_actionable() -> None:
    config = clone_default_config()
    agent = SwingAgent(config["agents"]["swing"])
    context = AgentContext(
        session=SessionContext(
            symbol="NIFTY FUT",
            session_date=date(2026, 4, 16),
            last_price=24218.45,
            stale_data_seconds=0.0,
            minutes_to_close=140,
        ),
        portfolio=PortfolioSnapshot(net_liquidation=1_000_000.0),
        current_profile=_make_profile_snapshot(
            open_price=24163.8,
            close_price=24218.45,
            high_price=24260.0,
            low_price=24145.8,
            poc=24235.0,
            vah=24252.5,
            val=24188.5,
            initial_balance_high=24280.9,
            initial_balance_low=24145.8,
            initial_balance_range=135.1,
            day_range=114.2,
            range_extension_up=10.0,
            range_extension_down=4.0,
            value_area_overlap=0.0,
            poc_shift=400.5,
            value_migration=420.25,
            bracket_state="expanding",
        ),
        prior_profile=_make_profile_snapshot(
            session_date="2026-04-15",
            open_price=23810.0,
            close_price=23834.5,
            high_price=23912.0,
            low_price=23688.0,
            poc=23834.5,
            vah=23907.5,
            val=23693.0,
            initial_balance_high=23880.0,
            initial_balance_low=23750.0,
            value_area_overlap=0.32,
            poc_shift=24.0,
        ),
        order_flow=_make_order_flow_snapshot(
            mid_price=24218.45,
            delta=1.0,
            trade_imbalance=0.0435,
            order_flow_imbalance=0.0,
            book_pressure=0.1125,
            timing_confidence=0.3938,
            toxicity_score=0.0265,
        ),
        regime=RegimeAssessment(
            label="trend_continuation",
            confidence=0.8,
            allowed_directions=["LONG"],
            reasons=["Value migrated higher after acceptance above prior value."],
            scorecard={},
        ),
        config=config,
        ntm_volx=None,
    )

    decision = agent.evaluate(context)

    assert decision.action == "LONG"
    assert decision.metadata["setup_name"] == "trend_pullback_long"
    assert decision.confidence >= config["agents"]["swing"]["min_confidence"]


def test_swing_agent_options_buy_proxy_sizing_keeps_one_lot_with_small_paper_book() -> None:
    config = clone_default_config()
    agent = SwingAgent(config["agents"]["swing"])
    context = AgentContext(
        session=SessionContext(
            symbol="NIFTY FUT",
            session_date=date(2026, 4, 16),
            last_price=24218.45,
            stale_data_seconds=0.0,
            minutes_to_close=140,
        ),
        portfolio=PortfolioSnapshot(net_liquidation=100_000.0),
        current_profile=_make_profile_snapshot(
            open_price=24163.8,
            close_price=24218.45,
            high_price=24260.0,
            low_price=24145.8,
            poc=24235.0,
            vah=24252.5,
            val=24188.5,
            initial_balance_high=24280.9,
            initial_balance_low=24145.8,
            initial_balance_range=135.1,
            day_range=114.2,
            range_extension_up=10.0,
            range_extension_down=4.0,
            value_area_overlap=0.0,
            poc_shift=400.5,
            value_migration=420.25,
            bracket_state="expanding",
        ),
        prior_profile=_make_profile_snapshot(
            session_date="2026-04-15",
            open_price=23810.0,
            close_price=23834.5,
            high_price=23912.0,
            low_price=23688.0,
            poc=23834.5,
            vah=23907.5,
            val=23693.0,
            initial_balance_high=23880.0,
            initial_balance_low=23750.0,
            value_area_overlap=0.32,
            poc_shift=24.0,
        ),
        order_flow=_make_order_flow_snapshot(
            mid_price=24218.45,
            delta=1.0,
            trade_imbalance=0.0435,
            order_flow_imbalance=0.0,
            book_pressure=0.1125,
            timing_confidence=0.3938,
            toxicity_score=0.0265,
        ),
        regime=RegimeAssessment(
            label="trend_continuation",
            confidence=0.8,
            allowed_directions=["LONG"],
            reasons=["Value migrated higher after acceptance above prior value."],
            scorecard={},
        ),
        config=config,
        ntm_volx=None,
    )

    decision = agent.evaluate(context)

    assert decision.action == "LONG"
    assert decision.quantity == 65


class _FakeOptionAdapter:
    def __init__(self, *, option_chain, expiries, contracts):
        self._option_chain = option_chain
        self._expiries = expiries
        self._contracts = contracts

    async def get_option_contracts(self, _symbol: str, expiry: str | None = None):
        if expiry:
            return [row for row in self._contracts if str(row.get("expiry")) == expiry]
        return list(self._expiries)

    async def get_option_chain(self, _symbol: str, _expiry: str):
        return self._option_chain


def test_option_mapper_selects_atm_put_for_scalp_short(monkeypatch) -> None:
    config = clone_default_config()
    mapper = OptionStrategyMapper(config["options_mapping"], config["contract_specs"])
    expiry = "2026-04-09"
    adapter = _FakeOptionAdapter(
        expiries=[{"expiry": expiry}, {"expiry": "2026-04-16"}],
        contracts=[
            {"instrument_key": "PE22450", "trading_symbol": "NIFTY22450PE", "strike_price": 22450.0, "instrument_type": "PE", "expiry": expiry, "lot_size": 75},
            {"instrument_key": "PE22500", "trading_symbol": "NIFTY22500PE", "strike_price": 22500.0, "instrument_type": "PE", "expiry": expiry, "lot_size": 75},
            {"instrument_key": "PE22550", "trading_symbol": "NIFTY22550PE", "strike_price": 22550.0, "instrument_type": "PE", "expiry": expiry, "lot_size": 75},
        ],
        option_chain=type("Chain", (), {
            "spot_price": 22496.0,
            "entries": [
                type("Entry", (), {"strike": 22450.0, "option_type": "PE", "ltp": 82.0, "oi": 20000, "volume": 18000, "bid": 81.0, "ask": 82.0, "delta": -0.62, "instrument_key": "PE22450"})(),
                type("Entry", (), {"strike": 22500.0, "option_type": "PE", "ltp": 61.0, "oi": 26000, "volume": 22000, "bid": 60.5, "ask": 61.0, "delta": -0.50, "instrument_key": "PE22500"})(),
                type("Entry", (), {"strike": 22550.0, "option_type": "PE", "ltp": 43.0, "oi": 14000, "volume": 12000, "bid": 42.5, "ask": 43.0, "delta": -0.38, "instrument_key": "PE22550"})(),
            ],
        })(),
    )

    async def _fake_load_candles(**_kwargs):
        return [{"close": 58.0 + (index * 0.1)} for index in range(40)]

    async def _fake_resolve_lot_size(**_kwargs):
        return 75

    monkeypatch.setattr("auction_intelligence.options.mapper.get_active_adapter", lambda broker=None: adapter if broker == "upstox" else None)
    monkeypatch.setattr("auction_intelligence.options.mapper.ensure_upstox_session", lambda: asyncio.sleep(0, result=False))
    monkeypatch.setattr("auction_intelligence.options.mapper.ensure_fyers_session", lambda: asyncio.sleep(0, result=False))
    monkeypatch.setattr("auction_intelligence.options.mapper.option_history_service.load_candles", _fake_load_candles)
    monkeypatch.setattr("auction_intelligence.options.mapper.option_history_service.resolve_lot_size", _fake_resolve_lot_size)

    plans = asyncio.run(
        mapper.map_execution_plan(
            session=SessionContext(symbol="NIFTY FUT", session_date=date(2026, 4, 9), last_price=22496.0, minutes_to_close=180),
            decisions=[
                AgentDecision(
                    agent_name="scalp",
                    action="SHORT",
                    confidence=0.73,
                    entry_price=22496.0,
                    stop_price=22512.0,
                    target_price=22470.0,
                    quantity=75,
                    sleeve_fraction=0.2,
                    rationale=["Responsive sell near upper value."],
                    metadata={"setup_name": "responsive_sell_short"},
                )
            ],
            execution_plan=[
                ExecutionInstruction(
                    agent_name="scalp",
                    symbol="NIFTY FUT",
                    action="SHORT",
                    style="PASSIVE",
                    order_type="LIMIT",
                    limit_price=22496.0,
                    slices=2,
                    cancel_after_seconds=30,
                    rationale=["Base execution."],
                    quantity=75,
                )
            ],
        )
    )

    assert len(plans) == 1
    assert plans[0].option_type == "PE"
    assert plans[0].strike == 22500.0
    assert plans[0].moneyness == "ATM"
    assert plans[0].broker_action == "BUY"
    assert plans[0].quantity == 75


def test_option_mapper_sizes_quantity_from_selected_contract_lot(monkeypatch) -> None:
    config = clone_default_config()
    config["contract_specs"]["NIFTY"]["lot_size"] = 65
    mapper = OptionStrategyMapper(config["options_mapping"], config["contract_specs"])
    expiry = "2026-04-09"
    adapter = _FakeOptionAdapter(
        expiries=[{"expiry": expiry}],
        contracts=[
            {"instrument_key": "PE22500", "trading_symbol": "NIFTY22500PE", "strike_price": 22500.0, "instrument_type": "PE", "expiry": expiry, "lot_size": 20},
        ],
        option_chain=type("Chain", (), {
            "spot_price": 22496.0,
            "entries": [
                type("Entry", (), {"strike": 22500.0, "option_type": "PE", "ltp": 61.0, "oi": 26000, "volume": 22000, "bid": 60.5, "ask": 61.0, "delta": -0.50, "instrument_key": "PE22500"})(),
            ],
        })(),
    )

    async def _fake_load_candles(**_kwargs):
        return [{"close": 58.0 + (index * 0.1)} for index in range(40)]

    async def _fake_resolve_lot_size(**_kwargs):
        return 20

    monkeypatch.setattr("auction_intelligence.options.mapper.get_active_adapter", lambda broker=None: adapter if broker == "upstox" else None)
    monkeypatch.setattr("auction_intelligence.options.mapper.ensure_upstox_session", lambda: asyncio.sleep(0, result=False))
    monkeypatch.setattr("auction_intelligence.options.mapper.ensure_fyers_session", lambda: asyncio.sleep(0, result=False))
    monkeypatch.setattr("auction_intelligence.options.mapper.option_history_service.load_candles", _fake_load_candles)
    monkeypatch.setattr("auction_intelligence.options.mapper.option_history_service.resolve_lot_size", _fake_resolve_lot_size)

    plans = asyncio.run(
        mapper.map_execution_plan(
            session=SessionContext(symbol="NIFTY FUT", session_date=date(2026, 4, 9), last_price=22496.0, minutes_to_close=180),
            decisions=[
                AgentDecision(
                    agent_name="scalp",
                    action="SHORT",
                    confidence=0.73,
                    entry_price=22496.0,
                    stop_price=22512.0,
                    target_price=22470.0,
                    quantity=65,
                    sleeve_fraction=0.2,
                    rationale=["Responsive sell near upper value."],
                    metadata={"setup_name": "responsive_sell_short"},
                )
            ],
            execution_plan=[
                ExecutionInstruction(
                    agent_name="scalp",
                    symbol="NIFTY FUT",
                    action="SHORT",
                    style="PASSIVE",
                    order_type="LIMIT",
                    limit_price=22496.0,
                    slices=2,
                    cancel_after_seconds=30,
                    rationale=["Base execution."],
                    quantity=65,
                )
            ],
        )
    )

    assert len(plans) == 1
    assert plans[0].lot_size == 20
    assert plans[0].quantity == 60


def test_option_mapper_select_expiry_accepts_string_session_date() -> None:
    config = clone_default_config()
    mapper = OptionStrategyMapper(config["options_mapping"], config["contract_specs"])

    expiry = mapper._select_expiry(
        expiries=[date(2026, 4, 9), date(2026, 4, 16)],
        session=SessionContext(  # type: ignore[arg-type]
            symbol="NIFTY FUT",
            session_date="2026-04-09",
            last_price=22496.0,
            minutes_to_close=180,
        ),
        agent_name="scalp",
    )

    assert expiry == date(2026, 4, 9)


def test_option_mapper_prefers_itm_call_for_high_confidence_positional_signal(monkeypatch) -> None:
    config = clone_default_config()
    mapper = OptionStrategyMapper(config["options_mapping"], config["contract_specs"])
    expiry = "2026-04-16"
    adapter = _FakeOptionAdapter(
        expiries=[{"expiry": "2026-04-09"}, {"expiry": expiry}],
        contracts=[
            {"instrument_key": "CE22450", "trading_symbol": "NIFTY22450CE", "strike_price": 22450.0, "instrument_type": "CE", "expiry": expiry, "lot_size": 75},
            {"instrument_key": "CE22500", "trading_symbol": "NIFTY22500CE", "strike_price": 22500.0, "instrument_type": "CE", "expiry": expiry, "lot_size": 75},
            {"instrument_key": "CE22550", "trading_symbol": "NIFTY22550CE", "strike_price": 22550.0, "instrument_type": "CE", "expiry": expiry, "lot_size": 75},
        ],
        option_chain=type("Chain", (), {
            "spot_price": 22498.0,
            "entries": [
                type("Entry", (), {"strike": 22450.0, "option_type": "CE", "ltp": 116.0, "oi": 28000, "volume": 24000, "bid": 115.0, "ask": 116.0, "delta": 0.68, "instrument_key": "CE22450"})(),
                type("Entry", (), {"strike": 22500.0, "option_type": "CE", "ltp": 82.0, "oi": 25000, "volume": 22000, "bid": 81.5, "ask": 82.0, "delta": 0.54, "instrument_key": "CE22500"})(),
                type("Entry", (), {"strike": 22550.0, "option_type": "CE", "ltp": 57.0, "oi": 15000, "volume": 14000, "bid": 56.5, "ask": 57.0, "delta": 0.41, "instrument_key": "CE22550"})(),
            ],
        })(),
    )

    async def _fake_load_candles(**_kwargs):
        return [{"close": 78.0 + (index * 0.25)} for index in range(45)]

    async def _fake_resolve_lot_size(**_kwargs):
        return 75

    monkeypatch.setattr("auction_intelligence.options.mapper.get_active_adapter", lambda broker=None: adapter if broker == "upstox" else None)
    monkeypatch.setattr("auction_intelligence.options.mapper.ensure_upstox_session", lambda: asyncio.sleep(0, result=False))
    monkeypatch.setattr("auction_intelligence.options.mapper.ensure_fyers_session", lambda: asyncio.sleep(0, result=False))
    monkeypatch.setattr("auction_intelligence.options.mapper.option_history_service.load_candles", _fake_load_candles)
    monkeypatch.setattr("auction_intelligence.options.mapper.option_history_service.resolve_lot_size", _fake_resolve_lot_size)

    plans = asyncio.run(
        mapper.map_execution_plan(
            session=SessionContext(symbol="NIFTY FUT", session_date=date(2026, 4, 9), last_price=22498.0, minutes_to_close=240),
            decisions=[
                AgentDecision(
                    agent_name="positional",
                    action="LONG",
                    confidence=0.82,
                    entry_price=22498.0,
                    stop_price=22440.0,
                    target_price=22640.0,
                    quantity=150,
                    sleeve_fraction=0.45,
                    rationale=["Gap continuation through prior value."],
                    metadata={"setup_name": "gap_continuation_long"},
                )
            ],
            execution_plan=[
                ExecutionInstruction(
                    agent_name="positional",
                    symbol="NIFTY FUT",
                    action="LONG",
                    style="PASSIVE",
                    order_type="LIMIT",
                    limit_price=22498.0,
                    slices=2,
                    cancel_after_seconds=30,
                    rationale=["Base execution."],
                    quantity=150,
                )
            ],
        )
    )

    assert len(plans) == 1
    assert plans[0].option_type == "CE"
    assert plans[0].strike == 22450.0
    assert plans[0].moneyness == "ITM1"
    assert plans[0].days_to_expiry == 7


def test_option_mapper_rejects_overpriced_contracts(monkeypatch) -> None:
    config = clone_default_config()
    config["options_mapping"]["enable_relaxed_fallback"] = False
    mapper = OptionStrategyMapper(config["options_mapping"], config["contract_specs"])
    expiry = "2026-04-16"
    adapter = _FakeOptionAdapter(
        expiries=[{"expiry": expiry}],
        contracts=[
            {"instrument_key": "CE22450", "trading_symbol": "NIFTY22450CE", "strike_price": 22450.0, "instrument_type": "CE", "expiry": expiry, "lot_size": 75},
            {"instrument_key": "CE22500", "trading_symbol": "NIFTY22500CE", "strike_price": 22500.0, "instrument_type": "CE", "expiry": expiry, "lot_size": 75},
        ],
        option_chain=type("Chain", (), {
            "spot_price": 22498.0,
            "entries": [
                type("Entry", (), {"strike": 22450.0, "option_type": "CE", "ltp": 640.0, "oi": 28000, "volume": 24000, "bid": 639.0, "ask": 640.0, "delta": 0.68, "instrument_key": "CE22450"})(),
                type("Entry", (), {"strike": 22500.0, "option_type": "CE", "ltp": 560.0, "oi": 25000, "volume": 22000, "bid": 559.0, "ask": 560.0, "delta": 0.54, "instrument_key": "CE22500"})(),
            ],
        })(),
    )

    async def _fake_load_candles(**_kwargs):
        return [{"close": 500.0 + index} for index in range(30)]

    async def _fake_resolve_lot_size(**_kwargs):
        return 75

    monkeypatch.setattr("auction_intelligence.options.mapper.get_active_adapter", lambda broker=None: adapter if broker == "upstox" else None)
    monkeypatch.setattr("auction_intelligence.options.mapper.ensure_upstox_session", lambda: asyncio.sleep(0, result=False))
    monkeypatch.setattr("auction_intelligence.options.mapper.ensure_fyers_session", lambda: asyncio.sleep(0, result=False))
    monkeypatch.setattr("auction_intelligence.options.mapper.option_history_service.load_candles", _fake_load_candles)
    monkeypatch.setattr("auction_intelligence.options.mapper.option_history_service.resolve_lot_size", _fake_resolve_lot_size)

    plans = asyncio.run(
        mapper.map_execution_plan(
            session=SessionContext(symbol="NIFTY FUT", session_date=date(2026, 4, 9), last_price=22498.0, minutes_to_close=240),
            decisions=[
                AgentDecision(
                    agent_name="swing",
                    action="LONG",
                    confidence=0.74,
                    entry_price=22498.0,
                    stop_price=22460.0,
                    target_price=22580.0,
                    quantity=75,
                    sleeve_fraction=0.35,
                    rationale=["Acceptance continuation."],
                    metadata={"setup_name": "acceptance_continuation_long"},
                )
            ],
            execution_plan=[
                ExecutionInstruction(
                    agent_name="swing",
                    symbol="NIFTY FUT",
                    action="LONG",
                    style="PASSIVE",
                    order_type="LIMIT",
                    limit_price=22498.0,
                    slices=2,
                    cancel_after_seconds=30,
                    rationale=["Base execution."],
                    quantity=75,
                )
            ],
        )
    )

    assert plans == []


def test_option_mapper_relaxed_fallback_allows_paper_trade_when_primary_filters_fail(monkeypatch) -> None:
    config = clone_default_config()
    mapper = OptionStrategyMapper(config["options_mapping"], config["contract_specs"])
    expiry = "2026-04-16"
    adapter = _FakeOptionAdapter(
        expiries=[{"expiry": expiry}],
        contracts=[
            {"instrument_key": "CE22500", "trading_symbol": "NIFTY22500CE", "strike_price": 22500.0, "instrument_type": "CE", "expiry": expiry, "lot_size": 75},
        ],
        option_chain=type("Chain", (), {
            "spot_price": 22498.0,
            "entries": [
                type("Entry", (), {"strike": 22500.0, "option_type": "CE", "ltp": 620.0, "oi": 0, "volume": 0, "bid": 612.0, "ask": 620.0, "delta": 0.54, "instrument_key": "CE22500"})(),
            ],
        })(),
    )

    async def _fake_load_candles(**_kwargs):
        return [{"close": 700.0 - index} for index in range(30)]

    async def _fake_resolve_lot_size(**_kwargs):
        return 75

    monkeypatch.setattr("auction_intelligence.options.mapper.get_active_adapter", lambda broker=None: adapter if broker == "upstox" else None)
    monkeypatch.setattr("auction_intelligence.options.mapper.ensure_upstox_session", lambda: asyncio.sleep(0, result=False))
    monkeypatch.setattr("auction_intelligence.options.mapper.ensure_fyers_session", lambda: asyncio.sleep(0, result=False))
    monkeypatch.setattr("auction_intelligence.options.mapper.option_history_service.load_candles", _fake_load_candles)
    monkeypatch.setattr("auction_intelligence.options.mapper.option_history_service.resolve_lot_size", _fake_resolve_lot_size)

    plans = asyncio.run(
        mapper.map_execution_plan(
            session=SessionContext(symbol="NIFTY FUT", session_date=date(2026, 4, 9), last_price=22498.0, minutes_to_close=240),
            decisions=[
                AgentDecision(
                    agent_name="swing",
                    action="LONG",
                    confidence=0.74,
                    entry_price=22498.0,
                    stop_price=22460.0,
                    target_price=22580.0,
                    quantity=75,
                    sleeve_fraction=0.35,
                    rationale=["Acceptance continuation."],
                    metadata={"setup_name": "acceptance_continuation_long"},
                )
            ],
            execution_plan=[
                ExecutionInstruction(
                    agent_name="swing",
                    symbol="NIFTY FUT",
                    action="LONG",
                    style="PASSIVE",
                    order_type="LIMIT",
                    limit_price=22498.0,
                    slices=2,
                    cancel_after_seconds=30,
                    rationale=["Base execution."],
                    quantity=75,
                )
            ],
        )
    )

    assert len(plans) == 1
    assert plans[0].premium == 620.0
    assert any("relaxed paper-trade fallback" in reason for reason in plans[0].rationale)


def test_option_mapper_can_fall_back_to_local_atm_watchlist_chain(monkeypatch) -> None:
    config = clone_default_config()
    mapper = OptionStrategyMapper(config["options_mapping"], config["contract_specs"])

    async def _fake_cached_chain(symbol: str, expiry: str):
        return None

    async def _fake_watchlist(*, expiry=None, symbols=None, live_refresh=False):
        return {
            "rows": [
                {
                    "underlying": "NIFTY",
                    "expiry": "2026-04-16",
                    "spot_price": 22498.0,
                    "atm_strike": 22500.0,
                    "ce": {
                        "strike": 22500.0,
                        "ltp": 82.0,
                        "oi": 24000,
                        "volume": 20000,
                        "delta": 0.59,
                        "instrument_key": "CE22500",
                    },
                    "pe": {
                        "strike": 22500.0,
                        "ltp": 79.0,
                        "oi": 18000,
                        "volume": 16000,
                        "delta": -0.41,
                        "instrument_key": "PE22500",
                    },
                }
            ]
        }

    monkeypatch.setattr("auction_intelligence.options.mapper.option_chain_service.get_cached", _fake_cached_chain)
    monkeypatch.setattr("auction_intelligence.options.mapper.atm_watchlist_service.get_watchlist", _fake_watchlist)
    monkeypatch.setattr("auction_intelligence.options.mapper.settings.PAPER_TRADING_ONLY", True)

    chain = asyncio.run(
        mapper._load_chain(
            app_symbol="NSE:NIFTY50-INDEX",
            expiry=date(2026, 4, 16),
            upstox_adapter=None,
            fyers_adapter=None,
        )
    )

    assert chain is not None
    assert chain.spot_price == 22498.0
    assert {entry.option_type for entry in chain.entries} == {"CE", "PE"}


def test_service_ntm_volx_boosts_aligned_signal() -> None:
    service = AuctionIntelligenceService(clone_default_config())
    decision = AgentDecision(
        agent_name="swing",
        action="LONG",
        confidence=0.68,
        entry_price=22520.0,
        stop_price=22480.0,
        target_price=22610.0,
        quantity=75,
        sleeve_fraction=0.35,
        rationale=["Auction is accepting above value."],
        metadata={"setup_name": "acceptance_continuation_long"},
    )

    adjusted = service._apply_ntm_volx_overlay([decision], _make_ntm_volx_snapshot())

    assert adjusted[0].confidence > decision.confidence
    assert adjusted[0].metadata["ntm_alignment"] == "aligned"
    assert "NTM VolX confirms" in adjusted[0].rationale[-1]


def test_service_ntm_volx_blocks_weak_counter_bias_signal() -> None:
    service = AuctionIntelligenceService(clone_default_config())
    decision = AgentDecision(
        agent_name="swing",
        action="SHORT",
        confidence=0.66,
        entry_price=22520.0,
        stop_price=22560.0,
        target_price=22430.0,
        quantity=75,
        sleeve_fraction=0.35,
        rationale=["Fade looked attractive near the upper extreme."],
        metadata={"setup_name": "auction_rejection_short"},
    )

    adjusted = service._apply_ntm_volx_overlay(
        [decision],
        _make_ntm_volx_snapshot(vxr=2.8, net_pressure=0.41, regime="calls_extreme"),
    )

    assert adjusted[0].action == "FLAT"
    assert adjusted[0].quantity == 0
    assert adjusted[0].metadata["flat_reason"] == "ntm_volx_conflict"
    assert adjusted[0].metadata["ntm_alignment"] == "blocked"


def test_option_mapper_ntm_volx_can_shift_selection_toward_atm_pressure(monkeypatch) -> None:
    config = clone_default_config()
    mapper = OptionStrategyMapper(config["options_mapping"], config["contract_specs"])
    expiry = "2026-04-16"
    adapter = _FakeOptionAdapter(
        expiries=[{"expiry": expiry}],
        contracts=[
            {"instrument_key": "CE22450", "trading_symbol": "NIFTY22450CE", "strike_price": 22450.0, "instrument_type": "CE", "expiry": expiry, "lot_size": 75},
            {"instrument_key": "CE22500", "trading_symbol": "NIFTY22500CE", "strike_price": 22500.0, "instrument_type": "CE", "expiry": expiry, "lot_size": 75},
        ],
        option_chain=type("Chain", (), {
            "spot_price": 22498.0,
            "entries": [
                type("Entry", (), {"strike": 22450.0, "option_type": "CE", "ltp": 116.0, "oi": 21000, "volume": 18000, "bid": 115.0, "ask": 116.0, "delta": 0.61, "instrument_key": "CE22450"})(),
                type("Entry", (), {"strike": 22500.0, "option_type": "CE", "ltp": 82.0, "oi": 24000, "volume": 20000, "bid": 81.5, "ask": 82.0, "delta": 0.59, "instrument_key": "CE22500"})(),
            ],
        })(),
    )

    async def _fake_load_candles(**_kwargs):
        return [{"close": 60.0 + (index * 0.15)} for index in range(45)]

    async def _fake_resolve_lot_size(**_kwargs):
        return 75

    monkeypatch.setattr("auction_intelligence.options.mapper.get_active_adapter", lambda broker=None: adapter if broker == "upstox" else None)
    monkeypatch.setattr("auction_intelligence.options.mapper.ensure_upstox_session", lambda: asyncio.sleep(0, result=False))
    monkeypatch.setattr("auction_intelligence.options.mapper.ensure_fyers_session", lambda: asyncio.sleep(0, result=False))
    monkeypatch.setattr("auction_intelligence.options.mapper.option_history_service.load_candles", _fake_load_candles)
    monkeypatch.setattr("auction_intelligence.options.mapper.option_history_service.resolve_lot_size", _fake_resolve_lot_size)

    common_kwargs = {
        "session": SessionContext(symbol="NIFTY FUT", session_date=date(2026, 4, 9), last_price=22498.0, minutes_to_close=240),
        "decisions": [
            AgentDecision(
                agent_name="swing",
                action="LONG",
                confidence=0.82,
                entry_price=22498.0,
                stop_price=22450.0,
                target_price=22590.0,
                quantity=75,
                sleeve_fraction=0.35,
                rationale=["Acceptance continuation."],
                metadata={"setup_name": "acceptance_continuation_long"},
            )
        ],
        "execution_plan": [
            ExecutionInstruction(
                agent_name="swing",
                symbol="NIFTY FUT",
                action="LONG",
                style="PASSIVE",
                order_type="LIMIT",
                limit_price=22498.0,
                slices=2,
                cancel_after_seconds=30,
                rationale=["Base execution."],
                quantity=75,
            )
        ],
    }

    baseline = asyncio.run(mapper.map_execution_plan(**common_kwargs))
    with_ntm = asyncio.run(
        mapper.map_execution_plan(
            **common_kwargs,
            ntm_volx=_make_ntm_volx_snapshot(
                pressure_ladder=[
                    NTMVolXLevel(
                        strike=22450.0,
                        distance_from_spot=48.0,
                        distance_from_spot_pct=0.0021,
                        call_volume=18_000.0,
                        put_volume=12_000.0,
                        call_notional=2_000_000.0,
                        put_notional=1_000_000.0,
                        call_oi_change=600.0,
                        put_oi_change=200.0,
                        call_pressure=120_000.0,
                        put_pressure=80_000.0,
                        net_pressure=0.2,
                    ),
                    NTMVolXLevel(
                        strike=22500.0,
                        distance_from_spot=2.0,
                        distance_from_spot_pct=0.0001,
                        call_volume=24_000.0,
                        put_volume=8_000.0,
                        call_notional=2_400_000.0,
                        put_notional=600_000.0,
                        call_oi_change=1_200.0,
                        put_oi_change=120.0,
                        call_pressure=780_000.0,
                        put_pressure=90_000.0,
                        net_pressure=0.7931,
                    ),
                ],
                call_pressure=900_000.0,
                put_pressure=170_000.0,
                vxr=5.29,
                net_pressure=0.6822,
            ),
        )
    )

    assert baseline[0].strike == 22450.0
    assert with_ntm[0].strike == 22500.0
    assert any("NTM VolX" in reason for reason in with_ntm[0].rationale)


def test_service_records_option_paper_proposal(tmp_path, monkeypatch) -> None:
    config = clone_default_config()
    config["agents"]["swing"]["enable_acceptance_continuation_long"] = True
    config["paper_trading"]["journal_root"] = str(tmp_path)
    service = AuctionIntelligenceService(config)

    async def _fake_map_execution_plan(*, session, decisions, execution_plan, ntm_volx=None):
        return [
            replace(
                execution_plan[0],
                symbol="NIFTY22500CE",
                quantity=75,
                broker_action="BUY",
                underlying_symbol="NIFTY",
                instrument_type="CE",
                expiry="2026-04-16",
                strike=22500.0,
                option_type="CE",
                instrument_key="CE22500",
                trading_symbol="NIFTY22500CE",
                lot_size=75,
                premium=82.0,
                spot_price=22500.0,
                moneyness="ATM",
                expiry_kind="weekly",
                days_to_expiry=7,
                selection_reason="swing LONG mapped to CE ATM",
            )
        ]

    monkeypatch.setattr(service.options, "map_execution_plan", _fake_map_execution_plan)
    monkeypatch.setattr(service.options, "build_ntm_volx", AsyncMock(return_value=None))

    bundle, journal_paths, paper_positions = asyncio.run(
        service.analyze_and_record_option_paper(
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
        )
    )

    assert bundle.execution_plan
    assert bundle.execution_plan[0].option_type == "CE"
    assert journal_paths
    assert paper_positions["open_count"] == 1
    journal_row = json.loads(tmp_path.joinpath("nifty_fut.jsonl").read_text().splitlines()[0])
    assert journal_row["option_type"] == "CE"
    assert journal_row["broker_action"] == "BUY"
    assert journal_row["selection_reason"] == "swing LONG mapped to CE ATM"


def test_paper_position_book_closes_open_position_on_flat_signal(tmp_path, monkeypatch) -> None:
    service = PaperTradingService(str(tmp_path))

    async def _fake_latest_option_candle(**kwargs):
        return [{"close": 92.0}]

    monkeypatch.setattr("auction_intelligence.paper.book.option_history_service.load_candles", _fake_latest_option_candle)

    open_bundle = AnalysisBundle(
        config_scope={},
        market_profile=_make_profile_snapshot(symbol="NIFTY FUT", close_price=22510.0),
        prior_market_profile=None,
        order_flow=_make_order_flow_snapshot(),
        regime=RegimeAssessment(label="trend_up", confidence=0.76, allowed_directions=["LONG"], reasons=["Trend day."]),
        agent_decisions=[
            AgentDecision(
                agent_name="swing",
                action="LONG",
                confidence=0.78,
                entry_price=82.0,
                stop_price=70.0,
                target_price=118.0,
                quantity=75,
                sleeve_fraction=0.35,
                rationale=["Acceptance continuation long."],
            )
        ],
        risk=RiskDecision(allowed=True, kill_switch=False, max_size_multiplier=1.0, reasons=[]),
        execution_plan=[
            ExecutionInstruction(
                agent_name="swing",
                symbol="NIFTY22500CE",
                action="LONG",
                style="PASSIVE",
                order_type="LIMIT",
                limit_price=82.0,
                slices=2,
                cancel_after_seconds=30,
                rationale=["Mapped to ATM CE."],
                quantity=75,
                broker_action="BUY",
                underlying_symbol="NIFTY",
                instrument_type="CE",
                expiry="2026-04-16",
                strike=22500.0,
                option_type="CE",
                instrument_key="NIFTY|CE22500",
                trading_symbol="NIFTY22500CE",
                lot_size=75,
                premium=82.0,
                spot_price=22510.0,
                moneyness="ATM",
                expiry_kind="weekly",
                days_to_expiry=6,
                selection_reason="swing LONG mapped to CE ATM",
            )
        ],
    )

    open_summary = asyncio.run(service.sync_positions(open_bundle))
    assert open_summary["open_count"] == 1

    flat_bundle = AnalysisBundle(
        config_scope={},
        market_profile=_make_profile_snapshot(symbol="NIFTY FUT", close_price=22540.0),
        prior_market_profile=None,
        order_flow=_make_order_flow_snapshot(),
        regime=RegimeAssessment(label="balance", confidence=0.44, allowed_directions=[], reasons=["No edge."]),
        agent_decisions=[
            AgentDecision(
                agent_name="swing",
                action="FLAT",
                confidence=0.0,
                entry_price=None,
                stop_price=None,
                target_price=None,
                quantity=0,
                sleeve_fraction=0.0,
                rationale=["No continuation edge."],
                metadata={"flat_reason": "no_follow_through"},
            )
        ],
        risk=RiskDecision(allowed=True, kill_switch=False, max_size_multiplier=1.0, reasons=[]),
        execution_plan=[],
    )

    close_summary = asyncio.run(service.sync_positions(flat_bundle))
    state = asyncio.run(service.book.list_positions(symbol="NIFTY"))

    assert close_summary["open_count"] == 0
    assert close_summary["closed_count"] == 1
    assert state["open_positions"] == []
    assert state["closed_positions"][0]["close_reason"] == "flat_signal"
    assert state["closed_positions"][0]["realized_pnl"] == 750.0


def test_swing_agent_uses_contract_aware_margin_sizing() -> None:
    config = clone_default_config()
    config["agents"]["swing"]["enable_acceptance_continuation_long"] = True
    config["mvp_scope"]["instrument_type"] = "futures"
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
                trade_imbalance=0.25,
                order_flow_imbalance=0.22,
                book_pressure=0.24,
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


def test_regime_engine_classifies_neutral_extreme_when_prior_value_is_probed_both_sides() -> None:
    config = clone_default_config()
    regime_engine = RegimeEngine(config["regime"])
    prior = _make_profile_snapshot(
        high_price=22510.0,
        low_price=22440.0,
        close_price=22480.0,
        poc=22478.0,
        vah=22498.0,
        val=22458.0,
        value_area_overlap=None,
        poc_shift=None,
        value_migration=None,
        prior_poc_untouched=None,
    )
    current = _make_profile_snapshot(
        high_price=22545.0,
        low_price=22405.0,
        close_price=22486.0,
        vah=22506.0,
        val=22452.0,
        value_area_overlap=0.62,
        poc_shift=4.0,
        value_migration=1.5,
    )

    regime = regime_engine.classify(
        current=current,
        prior=prior,
        order_flow=_make_order_flow_snapshot(delta=120.0, trade_imbalance=0.08, book_pressure=0.04),
    )

    assert regime.label == "neutral_extreme"
    assert sorted(regime.allowed_directions) == ["LONG", "SHORT"]


def test_swing_agent_trades_eighty_percent_rule_long() -> None:
    config = clone_default_config()
    config["agents"]["swing"]["enable_eighty_percent_rule"] = True
    agent = SwingAgent(config["agents"]["swing"])
    prior = _make_profile_snapshot(
        high_price=22520.0,
        low_price=22440.0,
        close_price=22485.0,
        poc=22478.0,
        vah=22498.0,
        val=22458.0,
        value_area_overlap=None,
        poc_shift=None,
        value_migration=None,
        prior_poc_untouched=None,
    )
    current = _make_profile_snapshot(
        open_price=22428.0,
        close_price=22472.0,
        high_price=22488.0,
        low_price=22418.0,
        poc=22468.0,
        vah=22490.0,
        val=22452.0,
        initial_balance_high=22482.0,
        initial_balance_low=22428.0,
        value_area_overlap=0.58,
        poc_shift=-10.0,
        value_migration=-8.0,
    )

    decision = agent.evaluate(
        AgentContext(
            session=SessionContext(symbol="NIFTY FUT", session_date=date(2026, 4, 2), last_price=22472.0),
            portfolio=PortfolioSnapshot(net_liquidation=1_000_000.0),
            current_profile=current,
            prior_profile=prior,
            order_flow=_make_order_flow_snapshot(
                mid_price=22472.0,
                micro_price=22472.2,
                delta=320.0,
                trade_imbalance=0.42,
                order_flow_imbalance=0.28,
                book_pressure=0.46,
                timing_confidence=0.86,
            ),
            regime=RegimeAssessment(
                label="developing_balance",
                confidence=0.68,
                allowed_directions=["LONG", "SHORT"],
                reasons=["Auction re-entered value from below."],
            ),
            config=config,
        )
    )

    assert decision.action == "LONG"
    assert decision.metadata["setup_name"] == "eighty_percent_rule_long"


def test_positional_agent_trades_balance_rotation_long() -> None:
    config = clone_default_config()
    config["agents"]["positional"]["enable_balance_rotation"] = True
    agent = PositionalAgent(config["agents"]["positional"])
    prior = _make_profile_snapshot(
        high_price=22510.0,
        low_price=22420.0,
        close_price=22470.0,
        poc=22472.0,
        vah=22492.0,
        val=22452.0,
        value_area_overlap=None,
        poc_shift=None,
        value_migration=None,
        prior_poc_untouched=None,
    )
    current = _make_profile_snapshot(
        close_price=22460.0,
        low_price=22418.0,
        high_price=22508.0,
        val=22452.0,
        vah=22502.0,
        poc=22478.0,
        poor_low=True,
        excess_low=3.0,
        value_area_overlap=0.74,
        poc_shift=2.0,
        value_migration=1.5,
        prior_poc_untouched=False,
    )

    decision = agent.evaluate(
        AgentContext(
            session=SessionContext(symbol="NIFTY FUT", session_date=date(2026, 4, 2), last_price=22460.0),
            portfolio=PortfolioSnapshot(net_liquidation=1_000_000.0),
            current_profile=current,
            prior_profile=prior,
            order_flow=_make_order_flow_snapshot(
                mid_price=22460.0,
                micro_price=22460.4,
                delta=260.0,
                trade_imbalance=0.36,
                order_flow_imbalance=0.24,
                book_pressure=0.42,
                timing_confidence=0.86,
            ),
            regime=RegimeAssessment(
                label="rotational_day",
                confidence=0.74,
                allowed_directions=["LONG", "SHORT"],
                reasons=["Auction rotated through both sides of the initial balance."],
            ),
            config=config,
        )
    )

    assert decision.action == "LONG"
    assert decision.metadata["setup_name"] == "balance_rotation_long"


def test_scalp_agent_trades_responsive_sell_at_upper_value() -> None:
    config = clone_default_config()
    agent = ScalpAgent(config["agents"]["scalp"])

    decision = agent.evaluate(
        AgentContext(
            session=SessionContext(symbol="NIFTY FUT", session_date=date(2026, 4, 2), last_price=22518.0),
            portfolio=PortfolioSnapshot(net_liquidation=2_500_000.0),
            current_profile=_make_profile_snapshot(
                close_price=22518.0,
                high_price=22528.0,
                low_price=22460.0,
                vah=22522.0,
                val=22462.0,
                poor_high=True,
                excess_high=3.0,
                value_area_overlap=0.76,
                poc_shift=0.0,
                value_migration=0.0,
            ),
            prior_profile=_make_profile_snapshot(),
                order_flow=_make_order_flow_snapshot(
                    mid_price=22518.0,
                    micro_price=22517.6,
                    delta=-240.0,
                    trade_imbalance=-0.42,
                    order_flow_imbalance=-0.32,
                    book_pressure=-0.54,
                    timing_confidence=0.92,
                ),
            regime=RegimeAssessment(
                label="balance",
                confidence=0.72,
                allowed_directions=["LONG", "SHORT"],
                reasons=["Value overlap remains high and directional extension is muted."],
            ),
            config=config,
        )
    )

    assert decision.action == "SHORT"
    assert decision.metadata["setup_name"] == "responsive_sell_short"


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
    config["agents"]["swing"]["enable_acceptance_continuation_long"] = True
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
                        trade_imbalance=0.25,
                        order_flow_imbalance=0.22,
                        book_pressure=0.24,
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


def test_group_rows_by_session_keeps_partial_live_session_when_requested(monkeypatch) -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    fixed_now = datetime(2026, 4, 16, 11, 30, tzinfo=ist)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr("auction_intelligence.live.datetime", _FixedDateTime)

    rows = [
        {
            "time": (datetime(2026, 4, 16, 3, 45, tzinfo=timezone.utc) + timedelta(minutes=index)).isoformat(),
            "open": 24200.0 + index,
            "high": 24201.0 + index,
            "low": 24199.0 + index,
            "close": 24200.5 + index,
            "volume": 1000 + index,
        }
        for index in range(130)
    ]

    dropped = _group_rows_by_session(rows)
    kept = _group_rows_by_session(rows, allow_partial_live_session=True)

    assert date(2026, 4, 16) not in dropped
    assert date(2026, 4, 16) in kept
    assert len(kept[date(2026, 4, 16)]) == 130


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
    assert {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"}.issubset(set(available_live_symbols()))


def test_tick_reconstruction_builds_quote_history_and_signed_trade_prints() -> None:
    base = datetime(2026, 4, 11, 10, 0)
    tick_rows = [
        {
            "timestamp": base,
            "ltp": 24100.0,
            "bid": 24099.5,
            "ask": 24100.5,
            "bid_qty": 120.0,
            "ask_qty": 100.0,
            "volume": 1000.0,
            "oi": 0.0,
        },
        {
            "timestamp": base + timedelta(seconds=2),
            "ltp": 24100.5,
            "bid": 24100.0,
            "ask": 24101.0,
            "bid_qty": 140.0,
            "ask_qty": 90.0,
            "volume": 1080.0,
            "oi": 0.0,
        },
        {
            "timestamp": base + timedelta(seconds=4),
            "ltp": 24099.5,
            "bid": 24099.0,
            "ask": 24100.0,
            "bid_qty": 110.0,
            "ask_qty": 150.0,
            "volume": 1145.0,
            "oi": 0.0,
        },
    ]

    quote_history = _build_quote_history_from_ticks(tick_rows, tick_size=0.5)
    trades = _build_trade_prints_from_ticks(tick_rows, tick_size=0.5)

    assert len(quote_history) == 3
    assert quote_history[-1]["last_price"] == 24099.5
    assert len(trades) == 2
    assert trades[0]["aggressor_side"] == "buy"
    assert trades[0]["quantity"] == 80.0
    assert trades[1]["aggressor_side"] == "sell"
    assert trades[1]["quantity"] == 65.0


def test_fetch_recent_minute_rows_prefers_persisted_spot_candles(monkeypatch) -> None:
    class _FakeResult:
        def mappings(self):
            return self

        def all(self):
            return [
                {
                    "time": datetime(2026, 4, 11, 9, 15),
                    "open": 24100.0,
                    "high": 24110.0,
                    "low": 24095.0,
                    "close": 24105.0,
                    "volume": 1200,
                },
                {
                    "time": datetime(2026, 4, 11, 9, 16),
                    "open": 24105.0,
                    "high": 24115.0,
                    "low": 24100.0,
                    "close": 24112.0,
                    "volume": 900,
                },
            ]

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, *_args, **_kwargs):
            return _FakeResult()

    monkeypatch.setattr("auction_intelligence.live.AsyncSessionLocal", lambda: _FakeSession())
    monkeypatch.setattr("auction_intelligence.live.get_active_adapter", lambda _broker=None: None)
    monkeypatch.setattr("auction_intelligence.live.ensure_fyers_session", AsyncMock(return_value=False))
    monkeypatch.setattr("auction_intelligence.live.ensure_upstox_session", AsyncMock(return_value=False))
    monkeypatch.setattr("auction_intelligence.live.get_broker_token", lambda _broker: "")

    rows, source, history_symbol = asyncio.run(_fetch_recent_minute_rows("NIFTY", lookback_days=2))

    assert len(rows) == 2
    assert rows[0]["close"] == 24105.0
    assert source == "timescaledb_spot_1minute"
    assert history_symbol == "NSE:NIFTY50-INDEX"


def test_live_snapshot_rejects_unknown_symbol_with_client_error() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get("/api/auction-intelligence/live-snapshot", params={"symbol": "UNKNOWN"})

    assert response.status_code == 400
    assert "Unsupported live symbol" in response.json()["detail"]


def test_build_live_analysis_blocks_live_execution_when_tick_order_flow_is_missing(monkeypatch) -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    fixed_now = datetime(2026, 4, 20, 11, 30, tzinfo=ist)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    def _minute_rows(session_start_utc: datetime, *, count: int, base: float) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for index in range(count):
            open_price = base + (index * 0.8)
            close_price = open_price + (0.6 if index % 2 == 0 else -0.2)
            candle_time = session_start_utc + timedelta(minutes=index)
            rows.append(
                {
                    "time": candle_time.isoformat(),
                    "open": round(open_price, 2),
                    "high": round(max(open_price, close_price) + 0.4, 2),
                    "low": round(min(open_price, close_price) - 0.4, 2),
                    "close": round(close_price, 2),
                    "volume": 1200 + index,
                }
            )
        return rows

    prior_rows = _minute_rows(datetime(2026, 4, 17, 3, 45, tzinfo=timezone.utc), count=220, base=24100.0)
    current_rows = _minute_rows(datetime(2026, 4, 20, 3, 45, tzinfo=timezone.utc), count=136, base=24220.0)

    async def _fake_recent_rows(symbol_code: str, *, lookback_days: int = 7, allow_live_broker_refresh: bool = True):
        assert symbol_code == "NIFTY"
        return prior_rows + current_rows, "timescaledb_spot_1minute", "NSE:NIFTY50-INDEX"

    async def _fake_tick_rows(*_args, **_kwargs):
        return []

    async def _fake_portfolio_snapshot(*_args, **_kwargs):
        return {}

    async def _fake_analyze_with_options(self, **kwargs):
        return AnalysisBundle(
            config_scope={"instrument_type": "options_buy"},
            market_profile=_make_profile_snapshot(symbol="NIFTY INDEX", session_date="2026-04-20"),
            prior_market_profile=_make_profile_snapshot(symbol="NIFTY INDEX", session_date="2026-04-17"),
            order_flow=_make_order_flow_snapshot(),
            regime=RegimeAssessment(
                label="no_trade",
                confidence=0.0,
                allowed_directions=["FLAT"],
                reasons=["tick_order_flow_unavailable"],
            ),
            agent_decisions=[],
            risk=RiskDecision(
                allowed=False,
                kill_switch=True,
                max_size_multiplier=0.0,
                reasons=["Broker connectivity unavailable."],
            ),
            execution_plan=[],
            ntm_volx=None,
        )

    monkeypatch.setattr("auction_intelligence.live.datetime", _FixedDateTime)
    monkeypatch.setattr("auction_intelligence.live._fetch_recent_minute_rows", _fake_recent_rows)
    monkeypatch.setattr("auction_intelligence.live._fetch_recent_tick_rows", _fake_tick_rows)
    monkeypatch.setattr("auction_intelligence.live._load_portfolio_snapshot", _fake_portfolio_snapshot)
    monkeypatch.setattr("auction_intelligence.live.market_data_router.get_latest_tick", lambda _symbol: None)
    monkeypatch.setattr(AuctionIntelligenceService, "analyze_with_options", _fake_analyze_with_options)

    payload = asyncio.run(build_live_analysis("NIFTY"))

    assert payload["data_status"]["execution_ready"] is False
    assert payload["data_status"]["order_flow_source"] == "bar_inference"
    assert payload["data_status"]["degraded_reason"] == "tick_order_flow_unavailable"
    assert payload["request"]["session"]["broker_connected"] is False
    assert payload["request"]["session"]["stale_data_seconds"] > clone_default_config()["risk"]["stale_data_seconds"]


def test_fetch_recent_minute_rows_prefers_broker_history_when_persisted_rows_are_incomplete(monkeypatch) -> None:
    class _FakeResult:
        def mappings(self):
            return self

        def all(self):
            return [
                {
                    "time": datetime(2026, 4, 11, 9, 15),
                    "open": 24100.0,
                    "high": 24110.0,
                    "low": 24095.0,
                    "close": 24105.0,
                    "volume": 1200,
                },
                {
                    "time": datetime(2026, 4, 11, 9, 16),
                    "open": 24105.0,
                    "high": 24115.0,
                    "low": 24100.0,
                    "close": 24112.0,
                    "volume": 900,
                },
            ]

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, *_args, **_kwargs):
            return _FakeResult()

    def _broker_rows() -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for session_date in (date(2026, 4, 10), date(2026, 4, 11)):
            start = datetime.combine(session_date, datetime.min.time()).replace(hour=9, minute=15)
            for minute in range(375):
                candle_time = start + timedelta(minutes=minute)
                rows.append(
                    {
                        "time": candle_time.isoformat() + "+05:30",
                        "open": 24100.0 + minute,
                        "high": 24101.0 + minute,
                        "low": 24099.0 + minute,
                        "close": 24100.5 + minute,
                        "volume": 1000 + minute,
                    }
                )
        return rows

    class _FakeFyers:
        async def get_historical_candles(self, *_args, **_kwargs):
            return _broker_rows()

    monkeypatch.setattr("auction_intelligence.live.AsyncSessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(
        "auction_intelligence.live.get_active_adapter",
        lambda broker=None: _FakeFyers() if broker == "fyers" else None,
    )
    monkeypatch.setattr("auction_intelligence.live.ensure_fyers_session", AsyncMock(return_value=False))
    monkeypatch.setattr("auction_intelligence.live.ensure_upstox_session", AsyncMock(return_value=False))
    monkeypatch.setattr("auction_intelligence.live.get_broker_token", lambda _broker: "")

    rows, source, history_symbol = asyncio.run(_fetch_recent_minute_rows("NIFTY", lookback_days=7))

    assert len(rows) == 750
    assert source == "fyers_continuous_futures"
    assert history_symbol.startswith("NSE:NIFTY")


def test_fetch_recent_minute_rows_skips_broker_history_when_live_refresh_is_disabled(monkeypatch) -> None:
    class _FakeResult:
        def mappings(self):
            return self

        def all(self):
            return [
                {
                    "time": datetime(2026, 4, 11, 9, 15),
                    "open": 24100.0,
                    "high": 24110.0,
                    "low": 24095.0,
                    "close": 24105.0,
                    "volume": 1200,
                },
                {
                    "time": datetime(2026, 4, 11, 9, 16),
                    "open": 24105.0,
                    "high": 24115.0,
                    "low": 24100.0,
                    "close": 24112.0,
                    "volume": 900,
                },
            ]

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, *_args, **_kwargs):
            return _FakeResult()

    broker_calls = {"count": 0}

    class _FakeFyers:
        async def get_historical_candles(self, *_args, **_kwargs):
            broker_calls["count"] += 1
            return []

    monkeypatch.setattr("auction_intelligence.live.AsyncSessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(
        "auction_intelligence.live.get_active_adapter",
        lambda broker=None: _FakeFyers() if broker == "fyers" else None,
    )
    monkeypatch.setattr("auction_intelligence.live.ensure_fyers_session", AsyncMock(return_value=False))
    monkeypatch.setattr("auction_intelligence.live.ensure_upstox_session", AsyncMock(return_value=False))
    monkeypatch.setattr("auction_intelligence.live.get_broker_token", lambda _broker: "")

    rows, source, history_symbol = asyncio.run(
        _fetch_recent_minute_rows(
            "NIFTY",
            lookback_days=7,
            allow_live_broker_refresh=False,
        )
    )

    assert len(rows) == 2
    assert source == "timescaledb_spot_1minute"
    assert history_symbol == "NSE:NIFTY50-INDEX"
    assert broker_calls["count"] == 0


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


def test_paper_journal_endpoint_returns_filtered_summary(monkeypatch) -> None:
    monkeypatch.setattr(
        auction_intelligence_router._paper_journal,
        "iter_records",
        lambda: [
            {
                "recorded_at": "2026-04-03T10:10:00+00:00",
                "symbol": "NIFTY FUT",
                "underlying_symbol": "NIFTY",
                "trading_symbol": "NIFTY26APR22500CE",
                "agent_name": "swing",
                "action": "LONG",
                "confidence": 0.78,
                "premium": 184.5,
                "execution_style": "PASSIVE",
            },
            {
                "recorded_at": "2026-04-02T10:10:00+00:00",
                "symbol": "BANKNIFTY FUT",
                "underlying_symbol": "BANKNIFTY",
                "trading_symbol": "BANKNIFTY26APR51000PE",
                "agent_name": "scalp",
                "action": "SHORT",
                "confidence": 0.61,
                "premium": 212.0,
                "execution_style": "AGGRESSIVE",
            },
        ],
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get("/api/auction-intelligence/paper-journal", params={"symbol": "NIFTY", "limit": 10})

    assert response.status_code == 200
    payload = response.json()
    assert payload["symbol_filter"] == "NIFTY"
    assert payload["total_records"] == 1
    assert payload["summary"]["action_breakdown"]["LONG"] == 1
    assert payload["records"][0]["trading_symbol"] == "NIFTY26APR22500CE"


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


def test_build_demo_analysis_includes_quote_history_payload() -> None:
    payload = build_demo_analysis(symbol_code="NIFTY", scenario="acceptance_up")

    assert payload["request"]["quote_history"]
    assert payload["request"]["quote_history"][0]["bid"] < payload["request"]["quote_history"][0]["ask"]


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


def test_rl_cycle_endpoint_returns_guarded_cycle_result(monkeypatch) -> None:
    class _Trainer:
        async def run_cycle(self, **kwargs):
            return {"status": "promoted", "source": kwargs["source"], "decision": {"should_promote": True}}

    monkeypatch.setattr("auction_intelligence.rl.automation.rl_auto_trainer", _Trainer())
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.post(
        "/api/auction-intelligence/rl-cycle",
        params={"max_trades": 100, "symbol": "NIFTY FUT", "promote_if_eligible": True},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "promoted"
    assert payload["source"] == "manual"


def test_rl_versions_endpoint_returns_active_and_history(monkeypatch) -> None:
    class _Store:
        async def list_versions(self, *, limit=20, status=None):
            return [{"id": "version-1", "status": "candidate"}]

        async def latest_version(self, *, status=None):
            return {"id": "version-0", "status": "active"} if status == "active" else None

    monkeypatch.setattr("auction_intelligence.rl.versions.RLPolicyVersionStore", _Store)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get("/api/auction-intelligence/rl-versions", params={"limit": 10})

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["active_version"]["status"] == "active"
    assert payload["versions"][0]["id"] == "version-1"


def test_paper_service_resolves_relative_root_under_backend_runtime() -> None:
    service = PaperTradingService("runtime/auction_intelligence")

    assert service.writer.root == Path(__file__).resolve().parents[1] / "runtime" / "auction_intelligence"


def test_paper_positions_endpoint_returns_open_and_closed_positions(monkeypatch) -> None:
    monkeypatch.setattr(
        auction_intelligence_router._paper_book,
        "list_positions",
        AsyncMock(
            return_value={
                "symbol_filter": "NIFTY",
                "status": "all",
                "summary": {
                    "open_count": 1,
                    "closed_count": 2,
                    "realized_pnl": 4200.0,
                    "unrealized_pnl": 350.0,
                },
                "open_positions": [{"position_id": "open-1", "trading_symbol": "NIFTY26APR22500CE"}],
                "closed_positions": [{"position_id": "closed-1", "trading_symbol": "NIFTY26APR22400PE"}],
            }
        ),
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get("/api/auction-intelligence/paper-positions", params={"symbol": "NIFTY", "status": "all", "limit": 20})

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["open_count"] == 1
    assert payload["open_positions"][0]["trading_symbol"] == "NIFTY26APR22500CE"
    assert payload["closed_positions"][0]["trading_symbol"] == "NIFTY26APR22400PE"


def test_mp_dashboard_endpoint_returns_aggregated_structure(monkeypatch) -> None:
    async def _offline_live_snapshot(*args, **kwargs):
        raise RuntimeError("live snapshot unavailable")

    monkeypatch.setattr(auction_intelligence_router, "build_live_analysis", _offline_live_snapshot)

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get(
        "/api/auction-intelligence/mp-dashboard",
        params={"underlying": "NIFTY", "lookback": 12},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["underlying"] == "NIFTY"
    assert len(payload["sessions"]) <= 12
    assert payload["overview"]["session_count"] == len(payload["sessions"])
    assert "direction_distribution" in payload
    assert "day_type_distribution" in payload

    if payload["sessions"]:
        assert payload["latest"]["date"] == payload["sessions"][-1]["date"]
        assert isinstance(payload["context"], list)


def test_mp_dashboard_appends_live_session_when_packaged_data_is_stale(monkeypatch) -> None:
    async def _live_snapshot(*args, **kwargs):
        return {
            "session_date": "2026-04-20",
            "analysis": {
                "market_profile": {
                    "session_date": "2026-04-20",
                    "poc": 24426.0,
                    "vah": 24471.0,
                    "val": 24341.0,
                    "open_price": 24391.5,
                    "high_price": 24473.15,
                    "low_price": 24242.6,
                    "close_price": 24435.6,
                    "initial_balance_high": 24417.35,
                    "initial_balance_low": 24242.6,
                    "initial_balance_range": 174.75,
                    "range_extension_up": 55.8,
                    "range_extension_down": 0.0,
                    "sample_count": 312,
                    "poor_high": False,
                    "poor_low": True,
                    "excess_high": 0.0,
                    "excess_low": 0.0,
                    "selling_tail": [],
                    "buying_tail": [],
                },
                "agent_decisions": [
                    {
                        "agent_name": "swing",
                        "metadata": {
                            "buyer_fail_bin": 1,
                            "seller_fail_bin": 4,
                        },
                    }
                ],
            },
        }

    async def _no_durable_rows(*args, **kwargs):
        return []

    async def _no_db_spot_row(*args, **kwargs):
        return None

    async def _skip_durable_persist(*args, **kwargs):
        return 0

    monkeypatch.setattr(auction_intelligence_router, "build_live_analysis", _live_snapshot)
    monkeypatch.setattr(auction_intelligence_router, "_load_durable_mp_rows", _no_durable_rows)
    monkeypatch.setattr(auction_intelligence_router, "_build_db_spot_mp_row", _no_db_spot_row)
    monkeypatch.setattr(auction_intelligence_router, "_persist_durable_mp_rows", _skip_durable_persist)

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get(
        "/api/auction-intelligence/mp-dashboard",
        params={"underlying": "NIFTY", "lookback": 5},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["latest"]["date"] == "2026-04-20"
    assert payload["data_status"]["live_appended"] is True
    assert payload["data_status"]["source"].endswith("+live_snapshot")


def test_mp_rows_uses_durable_cache_when_live_bridge_misses(monkeypatch) -> None:
    old_row = {
        "date": "2026-04-07",
        "poc": 22400.0,
        "vah": 22500.0,
        "val": 22300.0,
        "ibh": 22480.0,
        "ibl": 22320.0,
        "ibr": 160.0,
        "session_high": 22520.0,
        "session_low": 22280.0,
        "open_price": 22410.0,
        "close_price": 22450.0,
    }
    durable_row = {
        **old_row,
        "date": "2026-04-20",
        "poc": 24426.0,
        "vah": 24471.0,
        "val": 24341.0,
        "ibh": 24417.35,
        "ibl": 24242.6,
        "ibr": 174.75,
        "session_high": 24473.15,
        "session_low": 24242.6,
        "open_price": 24391.5,
        "close_price": 24435.6,
    }

    async def _offline_live(*args, **kwargs):
        raise RuntimeError("bridge offline")

    async def _durable_rows(*args, **kwargs):
        return [durable_row]

    async def _no_db_spot_row(*args, **kwargs):
        return None

    async def _skip_durable_persist(*args, **kwargs):
        return 0

    monkeypatch.setattr(auction_intelligence_router, "_safe_csv", lambda path: [old_row])
    monkeypatch.setattr(auction_intelligence_router, "build_live_analysis", _offline_live)
    monkeypatch.setattr(auction_intelligence_router, "_load_durable_mp_rows", _durable_rows)
    monkeypatch.setattr(auction_intelligence_router, "_persist_durable_mp_rows", _skip_durable_persist)
    monkeypatch.setattr(auction_intelligence_router, "_build_db_spot_mp_row", _no_db_spot_row)
    monkeypatch.setattr(auction_intelligence_router, "_build_live_mp_row_from_fmp", lambda *args, **kwargs: None)
    monkeypatch.setattr(auction_intelligence_router, "_FMP_LIVE_MP_TIMEOUT_SECONDS", 0.001)

    rows, data_status = asyncio.run(auction_intelligence_router._load_mp_rows("NIFTY"))

    assert rows[-1]["date"] == "2026-04-20"
    assert data_status["latest_date"] == "2026-04-20"
    assert data_status["durable_appended"] is True
    assert data_status["source"].endswith("+durable_cache")


def test_mp_rows_refreshes_and_persists_same_day_live_candidate(monkeypatch) -> None:
    base_row = {
        "date": "2026-04-20",
        "poc": 24400.0,
        "vah": 24450.0,
        "val": 24350.0,
        "ibh": 24420.0,
        "ibl": 24250.0,
        "ibr": 170.0,
        "session_high": 24470.0,
        "session_low": 24240.0,
        "open_price": 24390.0,
        "close_price": 24420.0,
    }
    live_row = {
        **base_row,
        "poc": 24426.0,
        "vah": 24471.0,
        "val": 24341.0,
        "session_high": 24473.15,
        "session_low": 24242.6,
        "close_price": 24435.6,
    }
    persisted_rows: list[dict] = []

    async def _no_durable_rows(*args, **kwargs):
        return []

    async def _collect_live_rows(*args, **kwargs):
        return [("live_snapshot", live_row)], {
            "live_latest_date": "2026-04-20",
            "live_rejected": False,
            "live_bridge": ["live_snapshot"],
            "live_error": None,
        }

    async def _capture_persisted(*args, **kwargs):
        persisted_rows.extend(kwargs.get("rows") or args[1])
        return len(kwargs.get("rows") or args[1])

    monkeypatch.setattr(auction_intelligence_router, "_safe_csv", lambda path: [base_row])
    monkeypatch.setattr(auction_intelligence_router, "_load_durable_mp_rows", _no_durable_rows)
    monkeypatch.setattr(auction_intelligence_router, "_collect_live_mp_candidate_rows", _collect_live_rows)
    monkeypatch.setattr(auction_intelligence_router, "_persist_durable_mp_rows", _capture_persisted)

    rows, data_status = asyncio.run(auction_intelligence_router._load_mp_rows("NIFTY"))

    assert rows[-1]["poc"] == live_row["poc"]
    assert persisted_rows[0]["poc"] == live_row["poc"]
    assert data_status["live_refreshed"] is True
    assert data_status["source"].endswith("+live_refresh")


def test_durable_mp_persist_spools_when_postgres_is_unavailable(monkeypatch, tmp_path) -> None:
    row = {
        "date": "2026-04-20",
        "poc": 24426.0,
        "vah": 24471.0,
        "val": 24341.0,
        "ibh": 24417.35,
        "ibl": 24242.6,
        "ibr": 174.75,
        "session_high": 24473.15,
        "session_low": 24242.6,
        "open_price": 24391.5,
        "close_price": 24435.6,
    }

    def _db_down():
        raise RuntimeError("db down")

    from db import database

    monkeypatch.setattr(auction_intelligence_router, "_DURABLE_DAILY_MP_SPOOL_ROOT", tmp_path)
    monkeypatch.setattr(database, "AsyncSessionLocal", _db_down)

    persisted = asyncio.run(auction_intelligence_router._persist_durable_mp_rows("NIFTY", [row]))
    spooled_rows = auction_intelligence_router._load_spooled_durable_mp_rows("NIFTY")
    durable_rows = asyncio.run(auction_intelligence_router._load_durable_mp_rows("NIFTY"))

    assert persisted == 0
    assert spooled_rows[-1]["date"] == "2026-04-20"
    assert durable_rows[-1]["poc"] == row["poc"]
