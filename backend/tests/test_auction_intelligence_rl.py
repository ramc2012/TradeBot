from __future__ import annotations

from datetime import date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auction_intelligence.agents.swing import SwingAgent
from auction_intelligence.config import clone_default_config
from auction_intelligence.rl.policy import QLearningPolicy, encode_action
from auction_intelligence.rl.reward import compute_proxy_reward
from auction_intelligence.rl.state import extract_state
from auction_intelligence.rl.trainer import _action_from_record, _state_from_record
from auction_intelligence.schemas import (
    AgentContext,
    MarketProfileSnapshot,
    OrderFlowSnapshot,
    PortfolioSnapshot,
    RegimeAssessment,
    SessionContext,
)


def _profile() -> MarketProfileSnapshot:
    return MarketProfileSnapshot(
        symbol="NIFTY FUT",
        session_date="2026-04-11",
        period_minutes=30,
        tick_size=0.5,
        open_price=23000.0,
        high_price=23180.0,
        low_price=22960.0,
        close_price=23120.0,
        total_volume=100000.0,
        tpo_counts={23100.0: 4},
        tpo_letters={23100.0: "ABCD"},
        poc=23100.0,
        vah=23140.0,
        val=23060.0,
        initial_balance_high=23110.0,
        initial_balance_low=23020.0,
        initial_balance_range=90.0,
        day_range=220.0,
        range_extension_up=70.0,
        range_extension_down=0.0,
        single_prints=[],
        buying_tail=[22960.0, 22980.0, 23000.0],
        selling_tail=[23180.0, 23160.0],
        poor_high=False,
        poor_low=False,
        excess_high=0.0,
        excess_low=0.0,
        spike_direction="up",
        spike_price=None,
        period_count=4,
        sample_count=4,
    )


def _flow(
    *,
    trade_imbalance: float = 0.58,
    book_pressure: float = 0.67,
    toxicity_score: float = 0.82,
    timing_confidence: float = 0.79,
) -> OrderFlowSnapshot:
    return OrderFlowSnapshot(
        spread=0.5,
        mid_price=23120.0,
        micro_price=23120.4,
        top_imbalance=0.55,
        depth_imbalance=0.48,
        aggressive_buy_volume=650.0,
        aggressive_sell_volume=180.0,
        delta=470.0,
        cumulative_delta=610.0,
        vwap=23118.0,
        vwap_drift=2.0,
        queue_pressure=0.52,
        volatility_burst=1.25,
        passive_fill_probability=0.74,
        aggressive_fill_probability=0.81,
        adverse_selection_risk=0.41,
        timing_confidence=timing_confidence,
        execution_aggression="PASSIVE",
        micro_stop_distance=0.75,
        trade_imbalance=trade_imbalance,
        order_flow_imbalance=0.43,
        book_pressure=book_pressure,
        micro_price_offset_bps=1.8,
        trade_intensity_per_minute=7.5,
        quote_repricing_rate=5.0,
        toxicity_score=toxicity_score,
    )


def test_extract_state_includes_order_flow_bins() -> None:
    state = extract_state(
        "trend_continuation",
        _profile(),
        "LONG",
        order_flow=_flow(),
    )

    assert state.to_key().startswith("v2_")
    assert state.trade_imbalance_bin == 2
    assert state.book_pressure_bin == 2
    assert state.toxicity_bin == 2
    assert state.timing_bin == 2
    assert "trade=bullish" in state.label


def test_policy_summary_exposes_human_readable_state_label() -> None:
    policy = QLearningPolicy()
    policy._cache_loaded = True
    state = extract_state("trend_continuation", _profile(), "LONG", order_flow=_flow())
    policy._q_cache[state.to_key()] = [0.0] * 27
    policy._q_cache[state.to_key()][encode_action(min_confidence=0.7, risk_multiple=3.0, sleeve_fraction=0.5)] = 1.25

    summary = policy.get_policy_summary()

    assert summary["states_learned"] == 1
    assert summary["policy"][0]["state_label"].startswith("TREND_UP")
    assert "tox=high" in summary["policy"][0]["state_label"]


def test_proxy_reward_penalizes_poor_trade_quality() -> None:
    clean = compute_proxy_reward(
        action="LONG",
        entry_price=23120.0,
        stop_price=23070.0,
        target_price=23220.0,
        confidence=0.72,
    )
    penalized = compute_proxy_reward(
        action="LONG",
        entry_price=23120.0,
        stop_price=23070.0,
        target_price=23220.0,
        confidence=0.72,
        fill_drift_ticks=6.0,
        stale_signal=True,
        reconciliation_status="position_mismatch",
        toxicity_score=0.9,
        adverse_selection_risk=0.8,
        timing_confidence=0.35,
    )

    assert penalized < clean
    assert penalized < 0.5


def test_trainer_reconstructs_state_and_action_from_saved_metadata() -> None:
    record = {
        "action": "LONG",
        "regime_label": "trend_continuation",
        "confidence": 0.71,
        "metadata": {
            "decision_metadata": {
                "min_confidence": 0.70,
                "risk_multiple": 3.0,
                "sleeve_fraction": 0.50,
                "buyer_fail_bin": 2,
                "seller_fail_bin": 1,
                "ib_size_bin": 0,
                "diagnostics": {
                    "close_price": 23120.0,
                    "vah": 23140.0,
                    "val": 23060.0,
                    "trade_imbalance": 0.6,
                    "book_pressure": 0.5,
                    "toxicity_score": 0.78,
                    "timing_confidence": 0.74,
                },
            }
        },
    }

    state = _state_from_record(record)
    action_idx = _action_from_record(record)

    assert state is not None
    assert state.trade_imbalance_bin == 2
    assert state.book_pressure_bin == 2
    assert state.toxicity_bin == 2
    assert state.timing_bin == 2
    assert action_idx == encode_action(min_confidence=0.70, risk_multiple=3.0, sleeve_fraction=0.50)


def test_swing_agent_persists_rl_state_metadata_even_without_cache() -> None:
    config = clone_default_config()
    agent = SwingAgent(config["agents"]["swing"])
    decision = agent.evaluate(
        AgentContext(
            session=SessionContext(
                symbol="NIFTY FUT",
                session_date=date(2026, 4, 11),
                last_price=23120.0,
            ),
            portfolio=PortfolioSnapshot(net_liquidation=1_000_000.0),
            current_profile=_profile(),
            prior_profile=None,
            order_flow=_flow(trade_imbalance=0.45, book_pressure=0.4, toxicity_score=0.35, timing_confidence=0.72),
            regime=RegimeAssessment(
                label="trend_continuation",
                confidence=0.78,
                allowed_directions=["LONG"],
                reasons=["Higher value accepted."],
            ),
            config=config,
        )
    )

    assert decision.metadata["rl_state_key"].startswith("v2_")
    assert decision.metadata["risk_multiple"] == 2.0
    # sleeve_fraction default was deliberately cut 0.35 -> 0.04 in commit a716fe0a
    # (2026-06-15, "restore the risk-discipline thesis"). This is the shipped default
    # from auction_intelligence/config/defaults.json, not a learned Q-value.
    assert decision.metadata["sleeve_fraction"] == 0.04
    assert decision.metadata["buyer_fail_bin"] >= 0
