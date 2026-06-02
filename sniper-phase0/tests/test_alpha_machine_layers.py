from __future__ import annotations

import pandas as pd

from nomad_sniper.execution import choose_execution
from nomad_sniper.live import build_alpha_signal, compute_drift_report
from nomad_sniper.meta import decide_meta_action
from nomad_sniper.options import select_option_expression
from nomad_sniper.payoff import estimate_payoff
from nomad_sniper.regime import classify_regime
from nomad_sniper.risk import RiskState, apply_risk_governor
from nomad_sniper.setups import score_setup_experts


def _features():
    return {
        "h_bullish_confluence": 0.8,
        "h_bearish_confluence": 0.1,
        "h_timeframe_conflict": 0.1,
        "u_trend_day_score": 0.7,
        "u_balanced_day_score": 0.1,
        "u_range_expansion_score": 0.5,
        "u_price_above_ib": 1,
        "u_price_below_ib": 0,
        "u_rejection_from_value_score": 0.1,
        "u_dist_dev_poc_atr": 0.2,
        "u_volume_z": 0.8,
        "u_book_imbalance_pct": 0.1,
        "u_buy_sweep_intensity": 0.1,
        "u_sell_sweep_intensity": 0.0,
        "c_atr_percentile": 55,
        "c_range_consumed_pct": 45,
        "c_days_to_weekly_expiry": 2,
        "c_is_expiry_day": 0,
        "o_iv_level": 0.16,
        "o_iv_change": -0.01,
        "o_ce_volume_z": 1.0,
        "o_pe_volume_z": 0.2,
    }


def _prediction():
    return {
        "pred_direction": "up",
        "p_up": 0.72,
        "p_down": 0.10,
        "p_none": 0.18,
        "p_is_move": 0.76,
        "pred_magnitude_atr": 1.2,
        "pred_mae_atr": 0.35,
        "pred_time_to_target": 35,
    }


def test_regime_meta_payoff_layers_accept_good_signal():
    regime = classify_regime(_features())
    assert regime.regime == "trend_up"
    meta = decide_meta_action(_prediction(), _features(), regime)
    assert meta.action == "take"
    payoff = estimate_payoff(_prediction())
    assert payoff.expected_r > 0


def test_execution_options_risk_layers():
    execution = choose_execution(_features(), meta_action="take", urgency=0.8)
    assert execution.action in {"market_order", "passive_limit_order", "scale_in"}
    option = select_option_expression("up", _features(), edge_score=0.8)
    assert option.side == "call"
    risk = apply_risk_governor(edge_score=0.8, liquidity_score=0.8, regime_score=0.7, meta_size_multiplier=1.0)
    assert risk.allowed
    blocked = apply_risk_governor(edge_score=0.8, liquidity_score=0.8, regime_score=0.7, meta_size_multiplier=1.0, state=RiskState(daily_pnl_r=-4))
    assert blocked.kill_switch


def test_setup_experts_and_final_signal():
    experts = score_setup_experts(_features())
    assert experts.selected_expert
    signal = build_alpha_signal(_prediction(), _features())
    assert signal.action == "long"
    assert signal.position_size_multiplier > 0
    assert "Expected R" in signal.explanation


def test_drift_monitor_actions():
    realized = pd.DataFrame({
        "pred_expected_r": [1.0, 1.0],
        "realized_r": [-0.5, -0.6],
        "confidence": [0.8, 0.9],
        "is_winner": [0, 0],
        "pred_slippage_atr": [0.05, 0.05],
        "realized_slippage_atr": [0.4, 0.5],
    })
    report = compute_drift_report(realized, feature_drift_score=0.6)
    assert report.action in {"reduce_size", "disable_automation", "paper_mode"}
