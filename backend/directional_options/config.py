"""Default configuration for the directional long-options engine.

Scoped to NSE index options only — NIFTY, BANKNIFTY, SENSEX. Hard
thresholds (min_confidence, min_expected_edge_pct, regime/delta blocks)
have been retired; the RL policy at `directional_options.policy` learns
them online from realized paper-trade outcomes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parent
RUNTIME_ROOT = PACKAGE_ROOT.parent / "runtime" / "directional_options"
DATA_ROOT = PACKAGE_ROOT.parent / "runtime" / "index_analytics_data"

# Funded equity anchor for paper-trading capital accounting.
DIRECTIONAL_INITIAL_CAPITAL: float = 3_000_000.0


DEFAULT_CONFIG: dict[str, Any] = {
    "label": "Directional Long Options",
    "description": (
        "Long-premium research and execution sandbox for NSE index options "
        "(NIFTY / BANKNIFTY / SENSEX). Trade/skip, strike choice, and sizing "
        "are learned online by a contextual bandit instead of hand-tuned hurdles."
    ),
    "data_root": DATA_ROOT,
    "runtime_root": RUNTIME_ROOT,
    "universe": ["NIFTY", "BANKNIFTY", "SENSEX"],
    "timeframes": ["5minute", "15minute"],
    "default_underlying": "NIFTY",
    "default_timeframe": "5minute",
    "feature_engine": {
        "ema_fast": 8,
        "ema_slow": 21,
        "adx_period": 14,
        "atr_period": 14,
        "rsi_period": 14,
        "breakout_lookback": 12,
        "rv_window": 20,
        "range_window": 20,
        "volume_z_window": 20,
        "opening_range_minutes": 30,
        "warmup_bars": 32,
    },
    "signal_engine": {
        # No hard confidence cutoff and no regime gate — the RL policy
        # decides act/skip from the value posterior. We keep only a
        # tiny direction-score floor to skip literal dead-tape bars
        # (every momentum / breakout / DI input exactly 0). Every other
        # bar — including chop — produces a signal that flows to the
        # policy. The policy will learn from realised R-multiples that
        # chop trades bleed theta and skip them.
        "min_direction_score_floor": 0.001,
        "breakout_confidence_bonus": 0.06,
        "expected_move_atr_multiplier": 1.25,
        "expected_move_trend_multiplier": 0.85,
        "short_horizon_bars": 3,
        "medium_horizon_bars": 6,
        "long_horizon_bars": 9,
    },
    "selector": {
        # Surface up to this many candidates to the policy for ranking.
        "max_candidates": 12,
        "preferred_weekly_days": 8,
        "max_days_to_expiry": 45,
        # Index weekly minimums — these are real liquidity floors, not
        # edge gates. Below this the bid/ask is wide enough that the
        # post-cost realised return diverges from the model. Kept as a
        # data-quality guard, not a strategy gate.
        "min_volume": 150.0,
        "min_oi": 2500.0,
        "max_spread_pct": 0.20,
        "fallback_spread_pct": 0.30,
        # Index IV envelope (Nifty ATM IV historically 8–35%, BANKNIFTY
        # 10–45%, SENSEX 9–32%). 0.62 ceiling fits indices cleanly now
        # that commodities are out of scope.
        "sigma_floor": 0.10,
        "sigma_ceiling": 0.62,
        "sigma_multiplier": 1.08,
        "risk_free_rate": 0.065,
        # The distributional optimizer no longer GATES candidates — every
        # contract that passes the liquidity floor is surfaced to the
        # policy. These thresholds are kept as INFORMATIVE scores that
        # feed the policy's feature vector (p_minus_q_tail, timing_fit,
        # skew_tax, model_uncertainty, etc.).
        "distributional_optimizer": {
            "min_net_edge_pct": 0.0,
            "min_probability_of_profit": 0.0,
            "min_liquidity_score": 0.0,
            "min_timing_fit": 0.0,
            "model_error_base_pct": 0.015,
            # Delta windows are HINTS used to compute the delta-bucket
            # feature, not hard rejections. The policy may discover that
            # for a given regime the optimal delta sits outside these
            # canonical bands.
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
        # Matches the paper desk's actual equity (~₹30L).
        "starting_equity": 3_000_000.0,
        # Base sizing — the RL policy multiplies these by a learned size
        # multiplier in {0.5×, 1.0×, 1.5×, 2.0×} per trade.
        "risk_pct": 0.005,
        # NO premium cap — user directive: "without any limit on size".
        # The risk_pct × size_multiplier path is the only sizing gate.
        # Capital safety still comes from one-position-per-symbol and the
        # daily/weekly loss caps below.
        "premium_cap_pct": None,
        "planned_stop_pct": 0.35,
        "profit_target_pct": 0.45,
        "trail_giveback_pct": 0.18,
        "expiry_guard_days": 0.8,
        # Capital safety caps — these are NOT edge gates. They prevent
        # blowups on bad days but never block individual trades from
        # firing based on signal quality.
        # [DAILY-LOSS DISABLED 2026-06-08 — fine-tuning; restore 4.0 for production.]
        # 0.0 disables the daily loss cap (gate in risk.py guarded with `> 0`).
        "daily_loss_cap_r": 0.0,
        "weekly_loss_cap_r": 0.0,
    },
    "execution": {
        "entry_slippage_pct": 0.0075,
        "exit_slippage_pct": 0.006,
        "fee_per_unit": 0.45,
    },
    "backtest": {
        "lookback_sessions": 16,
        "mark_to_market_every_bar": True,
        "max_trades_per_day": 4,
    },
    "paper_trading": {
        "journal_root": RUNTIME_ROOT / "paper",
        "live_lookback_days": 10,
        "stale_watchlist_seconds": 600,
        # Anti-churn: minimum bars a position must be held before
        # honouring a signal-flip or flat-signal close. Stop / target
        # exits still fire immediately.
        "min_hold_bars": 3,
        # Wall-clock floor on the min-hold (on top of min_hold_bars). Fast
        # timeframes (1m -> 3 bars = 3 min) otherwise let a position flatten
        # within minutes; 8 min stops sub-bar noise churning the book.
        "min_hold_floor_minutes": 8.0,
        # Anti-churn re-entry cooldown: after a flat_signal / signal_flip
        # exit on a symbol, suppress NEW opens on that symbol for max(bars *
        # tf, floor_seconds). Stops the open->fade->close->reopen cycle that
        # bled the book (16 closes / 3 symbols on 2026-06-08, all whipsaw).
        "reentry_cooldown_bars": 3,
        "reentry_cooldown_floor_seconds": 600.0,
        # Anti-churn dead band in time (2026-06-10 audit): entries and true
        # CE<->PE flip-exits require this many CONSECUTIVE same-direction
        # actionable ~60s cycles. The signal is a knife-edge argmax and the
        # policy act/skip is a fresh Thompson draw vs 0 each cycle — without
        # persistence a single noisy cycle could open or reverse a position.
        # 5 cycles ≈ one full 5-minute bar of agreement. 0/1 disables.
        "signal_persistence_cycles": 5,
        # One open position per underlying. New signals on a symbol that
        # already has an open position are journaled but do NOT open a
        # second position. (Refresh / signal-flip on the SAME symbol is
        # handled by the existing _same_contract path.)
        "one_position_per_symbol": True,
    },
    "rl_policy": {
        # Persistent posterior lives next to the paper book.
        "state_path": RUNTIME_ROOT / "policy_state.json",
        # If True, the policy decides trade/skip + size from learned
        # posterior. If False, the engine falls back to a permissive
        # always-act path (used by tests that want deterministic flow).
        "enabled": True,
    },
    "ai_model": {
        "min_rule_score": 38.0,
        "min_liquidity_score": 0.08,
        "max_spread_pct": 0.28,
        "min_delta_abs": 0.12,
        "max_delta_abs": 0.92,
        "min_days_to_expiry": 0.20,
        "late_session_cutoff": 0.92,
        "late_session_min_days_to_expiry": 1.0,
    },
}


def clone_default_config() -> dict[str, Any]:
    """Return a deep-copy safe configuration dictionary."""
    import copy

    return copy.deepcopy(DEFAULT_CONFIG)
