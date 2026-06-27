"""Default configuration for the directional long-options engine.

Scoped to NSE index options only — NIFTY, BANKNIFTY, SENSEX. Hard
thresholds (min_confidence, min_expected_edge_pct, regime/delta blocks)
have been retired; the RL policy at `directional_options.policy` learns
them online from realized paper-trade outcomes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from analysis.instruments import ALL_FO_INDICES, STRIKE_STEPS


PACKAGE_ROOT = Path(__file__).resolve().parent
RUNTIME_ROOT = PACKAGE_ROOT.parent / "runtime" / "directional_options"
DATA_ROOT = PACKAGE_ROOT.parent / "runtime" / "index_analytics_data"

# Funded equity anchor for paper-trading capital accounting.
DIRECTIONAL_INITIAL_CAPITAL: float = 3_000_000.0

FNO_STOCK_FALLBACK: list[str] = sorted(
    symbol for symbol in STRIKE_STEPS if symbol not in set(ALL_FO_INDICES)
)
DIRECTIONAL_DEFAULT_UNIVERSE: list[str] = list(ALL_FO_INDICES) + FNO_STOCK_FALLBACK


DEFAULT_CONFIG: dict[str, Any] = {
    "label": "Directional Long Options",
    "description": (
        "Long-premium research and execution sandbox for index and F&O stock options. "
        "Trade/skip, strike choice, and sizing "
        "are learned online by a contextual bandit instead of hand-tuned hurdles."
    ),
    "data_root": DATA_ROOT,
    "runtime_root": RUNTIME_ROOT,
    "universe": DIRECTIONAL_DEFAULT_UNIVERSE,
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
        # Multi-factor-view inputs (always computed; consumed only when
        # DIRECTIONAL_MULTIFACTOR_VIEW_ENABLED). trend_tstat = rolling OLS t-stat
        # of log close (vol-robust trend backbone); htf_trend = long-window trend
        # proxy on the decision frame for HTF alignment.
        "trend_tstat_window": 20,
        "htf_trend_window": 50,
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
    # ── MULTI-FACTOR DIRECTIONAL VIEW ──────────────────────────────────────
    # Read ONLY when settings.DIRECTIONAL_MULTIFACTOR_VIEW_ENABLED is True. The
    # signed view score is a sum of tanh-bounded, sign-constrained terms (one per
    # orthogonal family), then attenuated by an ADX/chop gate and HTF alignment,
    # then conviction is damped by the dealer-gamma regime. direction = sign(score);
    # GEX is a SIZE/REGIME damper, NOT a directional vote (its 1-2 day / NSE edge is
    # unproven — weight stays low and is paper-validated). Skew/flow ship raw-bounded
    # (no causal normalization until a chain-history store exists).
    "view": {
        "adx_floor": 25.0,            # below this ADX, attenuate (chop)
        "adx_attenuation": 0.45,      # multiplier applied to score when ADX < floor
        "w_trend": 1.0,
        "w_extension": 0.35,
        "w_acceleration": 0.30,
        "w_skew": 0.45,
        "w_flow": 0.0,                 # flow (dex/OI) wired but INERT: sign convention unvalidated for NSE
        "skew_scale": 8.0,            # scales (risk_reversal/atm_iv) into tanh
        "flow_scale": 1.0,
        "htf_align_penalty": 0.55,    # score multiplier when view opposes the HTF trend
        "gex_damp_max": 0.40,         # +GEX (pinning) shrinks conviction by up to this
        "gex_amplify_max": 0.30,      # -GEX (trending) lifts conviction by up to this
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
        "daily_loss_cap_r": 4.0,
        "weekly_loss_cap_r": 10.0,
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
        # One open position per underlying. New signals on a symbol that
        # already has an open position are journaled but do NOT open a
        # second position. (Refresh / signal-flip on the SAME symbol is
        # handled by the existing _same_contract path.)
        "one_position_per_symbol": True,
    },
    # ── 1-2 DAY POSITIONAL MODE ────────────────────────────────────────────
    # Read ONLY when settings.DIRECTIONAL_POSITIONAL_MODE_ENABLED is True. When
    # the master flag is OFF (default) none of these are consulted and the lane
    # runs exactly as the legacy 5-min intraday engine. When ON, the lane:
    #   - decides open/flip on CLOSED `decision_timeframe` bars (side stays
    #     stable across a session instead of flipping every few minutes),
    #   - holds for ~1-2 sessions, counting held-time in TRADING-SESSION bars
    #     (the overnight gap no longer inflates the horizon → no day-2 auto-close),
    #   - marks + checks exits CONTINUOUSLY on `monitor_timeframe` granular data
    #     (market-intelligence feed) so a large move is squared off immediately,
    #   - sizes target / stop / invalidation in ATR (adaptive to volatility),
    #   - only buys options with >= `min_days_to_expiry` so the carry survives,
    #   - requires `flip_confirm_bars` consecutive opposite bars before flipping.
    # Horizons are in DECISION-TF bars; a 30-min session ≈ 12.5 bars, so
    # 13/19/25 ≈ 1 / 1.5 / 2 sessions.
    "positional": {
        "decision_timeframe": "30minute",
        "monitor_timeframe": "1minute",
        "short_horizon_bars": 13,
        "medium_horizon_bars": 19,
        "long_horizon_bars": 25,
        "min_days_to_expiry": 4,
        "prefer_longer_expiry": True,
        # ATR-based adaptive levels (underlying move basis). The premium-percent
        # backstops below still apply as a hard floor on option-premium loss.
        "atr_stop_mult": 1.2,        # underlying invalidation at entry ∓ 1.2×ATR
        "atr_target_mult": 2.5,      # thesis-achieved take-profit at ± 2.5×ATR
        "atr_trail_mult": 1.5,       # trail the underlying stop by 1.5×ATR in profit
        "large_move_atr_mult": 3.0,  # ± 3×ATR favourable → bank immediately
        # Premium backstops, widened for a multi-day swing (vs the 0.35/0.45
        # scalp values used in intraday mode).
        "planned_stop_pct": 0.45,
        "profit_target_pct": 0.70,
        "trail_giveback_pct": 0.22,
        "expiry_guard_days": 1.0,
        "flip_confirm_bars": 2,
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
    """Return a deep-copy safe configuration dictionary.

    When ``settings.DIRECTIONAL_POSITIONAL_MODE_ENABLED`` is set, the
    ``positional`` tunables are overlaid so the whole lane runs in 1-2 day
    positional mode: decision timeframe, hold horizons, premium backstops and
    expiry selection all take their positional values from one place (the
    supervisor and the service both read this merged config). Off by default →
    the legacy 5-min intraday config is returned unchanged. The other half of
    positional mode (session-clock held-time, decide-on-bar-close, min-DTE
    enforcement, confirmed-flip, ATR adaptive exits) lives as code branches that
    key off the ``_positional_active`` marker set here — do NOT enable the flag
    until that full set has landed.
    """
    import copy

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    try:
        from core.config import settings

        positional_on = bool(settings.DIRECTIONAL_POSITIONAL_MODE_ENABLED)
    except Exception:
        positional_on = False
    if not positional_on:
        return cfg

    p = cfg.get("positional") or {}
    cfg["default_timeframe"] = str(p.get("decision_timeframe", cfg["default_timeframe"]))
    tfs = list(cfg.get("timeframes") or [])
    if cfg["default_timeframe"] not in tfs:
        tfs.insert(0, cfg["default_timeframe"])
    cfg["timeframes"] = tfs
    se = cfg.setdefault("signal_engine", {})
    for key in ("short_horizon_bars", "medium_horizon_bars", "long_horizon_bars"):
        if key in p:
            se[key] = int(p[key])
    rk = cfg.setdefault("risk", {})
    for key in ("planned_stop_pct", "profit_target_pct", "trail_giveback_pct", "expiry_guard_days"):
        if key in p:
            rk[key] = float(p[key])
    sel = cfg.setdefault("selector", {})
    if "min_days_to_expiry" in p:
        sel["min_days_to_expiry"] = int(p["min_days_to_expiry"])
    sel["prefer_longer_expiry"] = bool(p.get("prefer_longer_expiry", True))
    # Marker so downstream code branches (paper.py / selector.py) can detect
    # positional mode from the config alone, without re-reading settings.
    cfg["_positional_active"] = True
    return cfg
