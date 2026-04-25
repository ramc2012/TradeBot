"""Default configuration for the directional long-options engine."""
from __future__ import annotations

from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parent
RUNTIME_ROOT = PACKAGE_ROOT.parent / "runtime" / "directional_options"
DATA_ROOT = PACKAGE_ROOT.parent / "runtime" / "index_analytics_data"


DEFAULT_CONFIG: dict[str, Any] = {
    "label": "Directional Long Options",
    "description": (
        "Long-premium research and execution sandbox focused on buying calls and puts "
        "only when expected convexity clears theta, spread, slippage, and IV drag."
    ),
    "data_root": DATA_ROOT,
    "runtime_root": RUNTIME_ROOT,
    "universe": ["NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOIL"],
    "timeframes": ["5minute", "15minute"],
    "default_underlying": "NIFTY",
    "default_timeframe": "5minute",
    "feature_engine": {
        "ema_fast": 8,
        "ema_slow": 21,
        "adx_period": 14,
        "atr_period": 14,
        "breakout_lookback": 12,
        "rv_window": 20,
        "range_window": 20,
        "warmup_bars": 32,
    },
    "signal_engine": {
        "min_confidence": 0.58,
        "breakout_confidence_bonus": 0.06,
        "expected_move_atr_multiplier": 1.25,
        "expected_move_trend_multiplier": 0.85,
        "short_horizon_bars": 6,
        "medium_horizon_bars": 12,
        "long_horizon_bars": 18,
    },
    "selector": {
        "max_candidates": 18,
        "preferred_weekly_days": 8,
        "max_days_to_expiry": 45,
        "min_volume": 150.0,
        "min_oi": 2_500.0,
        "max_spread_pct": 0.12,
        "fallback_spread_pct": 0.18,
        "sigma_floor": 0.12,
        "sigma_ceiling": 0.62,
        "sigma_multiplier": 1.08,
        "risk_free_rate": 0.06,
        "distributional_optimizer": {
            "min_net_edge_pct": 0.025,
            "min_probability_of_profit": 0.38,
            "min_liquidity_score": 0.35,
            "min_timing_fit": 0.28,
            "model_error_base_pct": 0.035,
            "ordinary_delta_min": 0.45,
            "ordinary_delta_max": 0.65,
            "fast_move_delta_min": 0.35,
            "fast_move_delta_max": 0.55,
            "jump_delta_min": 0.20,
            "jump_delta_max": 0.40,
            "otm_jump_threshold": 0.42,
            "otm_timing_threshold": 0.58,
            "index_put_skew_tax": 0.035,
            "max_skew_tax": 0.22,
            "weekly_timing_threshold": 0.42,
            "event_variance_premium": 0.025,
            "drawdown_state_penalty": 0.0,
        },
        "score_weights": {
            "direction": 22.0,
            "expected_pnl": 26.0,
            "liquidity": 20.0,
            "iv_value": 12.0,
            "theta_penalty": 10.0,
            "slippage_penalty": 10.0,
            "tail_edge": 18.0,
            "timing_fit": 14.0,
            "skew_tax": 12.0,
            "model_uncertainty": 10.0,
        },
    },
    "risk": {
        "starting_equity": 1_000_000.0,
        "risk_pct": 0.005,
        "premium_cap_pct": 0.01,
        "planned_stop_pct": 0.35,
        "profit_target_pct": 0.45,
        "trail_giveback_pct": 0.18,
        "expiry_guard_days": 0.8,
        "daily_loss_cap_r": 2.0,
        "weekly_loss_cap_r": 5.0,
        "min_expected_edge_pct": 0.08,
        "max_open_positions": 1,
    },
    "execution": {
        "entry_slippage_pct": 0.0075,
        "exit_slippage_pct": 0.006,
        "fee_per_unit": 0.45,
    },
    "backtest": {
        "lookback_sessions": 16,
        "mark_to_market_every_bar": True,
        "max_trades_per_day": 2,
    },
    "paper_trading": {
        "journal_root": RUNTIME_ROOT / "paper",
        "live_lookback_days": 10,
        "stale_watchlist_seconds": 600,
    },
}


def clone_default_config() -> dict[str, Any]:
    """Return a deep-copy safe configuration dictionary."""
    import copy

    return copy.deepcopy(DEFAULT_CONFIG)
