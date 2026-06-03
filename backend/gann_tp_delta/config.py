"""Default configuration for the Gann TP Delta harmonic module."""
from __future__ import annotations

from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = PACKAGE_ROOT.parent
RUNTIME_ROOT = BACKEND_ROOT / "runtime" / "gann_tp_delta"
DATA_ROOT = BACKEND_ROOT / "runtime" / "index_analytics_data"

GANN_RATIOS: list[tuple[str, float]] = [
    ("1x8", 0.125),
    ("1x4", 0.25),
    ("1x3", 1.0 / 3.0),
    ("1x2", 0.5),
    ("1x1", 1.0),
    ("2x1", 2.0),
    ("3x1", 3.0),
    ("4x1", 4.0),
    ("8x1", 8.0),
]

SQ9_DEGREES = [45, 90, 135, 180, 225, 270, 315, 360]
BAR_CYCLES = [7, 9, 14, 21, 30, 45, 60, 72, 90, 120, 144, 180, 225, 270, 315, 360]
CALENDAR_CYCLES = [30, 45, 60, 90, 180, 360]


DEFAULT_CONFIG: dict[str, Any] = {
    "key": "gann_tp_delta",
    "label": "Gann TP Delta Harmonic",
    "description": "Price-time geometry, TP Delta harmonic speed, Square of Nine, cycles, and confluence research.",
    "data_root": DATA_ROOT,
    "runtime_root": RUNTIME_ROOT,
    "universe": ["NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOIL", "GOLD", "SILVERM", "NATURALGAS"],
    "timeframes": ["5minute", "15minute", "1hour", "1day"],
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
    "anchors": {
        "pivot_left": 5,
        "pivot_right": 5,
        "pivot_vector_count": 9,
        "manual_time": None,
        "manual_price": None,
        "session_mode": "previous_day",
    },
    "scaling": {
        "default_h_mode": "median_tpd",
        "manual_h": 47.0,
        "atr_multiplier": 1.0,
        "min_h": 0.01,
    },
    "geometry": {
        "gann_ratios": GANN_RATIOS,
        "sq9_degrees": SQ9_DEGREES,
        "bar_cycles": BAR_CYCLES,
        "calendar_cycles": CALENDAR_CYCLES,
        "price_unit": 1.0,
        "near_pct": 0.003,
        "cycle_window_bars": 2,
        "squaring_tolerance": 0.05,
        "projection_bars": 80,
    },
    "signals": {
        "score_threshold": 3,
        "structure_lookback": 8,
        "atr_stop_multiplier": 1.1,
    },
    # ── Regime-gated confluence engine (v2) ─────────────────────────────────
    # The legacy `confluence_signal` flipped bias on every new pivot, so the
    # paper agent whipsawed itself (11 of 12 closes were self-reversals). The
    # v2 engine establishes a STABLE regime (EMA + structure + 1x1 master
    # angle, gated by ADX) and only trades two explicit archetypes, scored by
    # how EXACTLY price sits on each Gann element and how important that
    # element is.
    "strategy": {
        "enabled": True,
        # Regime detection
        "adx_trend_min": 18.0,          # ADX >= this ⇒ a real trend is present
        "regime_min_score": 2,          # |EMA+structure+1x1 vote| >= this ⇒ directional
        "structure_lookback": 8,
        # Archetype thresholds on the weighted conviction (~0..10 scale).
        # Tuned from a 150-day offline sweep (gann_tp_delta/tune_sweep.py): the
        # conviction floor is the dominant lever — higher = fewer, better trades
        # almost everywhere. 5.0 peaks NIFTY (+6.4R) and SENSEX (+8.3R) vs 4.0.
        "continuation_min_conviction": 5.0,
        "reversal_min_conviction": 6.5,
        "reversal_size_factor": 0.5,    # counter-trend reversals trade half size
        # Commodities over-trade and are negative-EV at the index bar. The sweep
        # flips the commodity book from deeply negative to net +5.2R at a 6.0
        # floor (GOLD +4.0R, SILVERM +1.6R, NATURALGAS +0.6R; CRUDEOIL still
        # ~-1R — structurally weak, watch it). 0 disables the extra floor.
        "commodity_min_conviction": 6.0,
        # Per-underlying floor overrides (max'd with the above). BANKNIFTY is
        # negative-EV at every floor in backtest (-7.75R @4.0); 6.0 brings it to
        # ~breakeven (-0.72R) so it stops bleeding the otherwise-strong index book.
        "per_underlying_min_conviction": {"BANKNIFTY": 6.0},
        "reversal_edge_over_continuation": 1.0,  # reversal must beat in-trend by this to override
        # Exactness tolerances (fraction of price) — tight, so a "touch" is real
        "angle_tolerance_pct": 0.0025,  # 0.25%
        "sq9_tolerance_pct": 0.0025,
        "pullback_tolerance_pct": 0.005,  # continuation pullback proximity to support
        # Element importance weights
        "weights": {
            "angle_1x1": 2.0,
            "angle_major": 1.0,         # 1x2, 2x1
            "angle_minor": 0.5,
            "sq9_cardinal": 1.5,        # 90/180/270/360
            "sq9_ordinal": 0.75,        # 45/135/225/315
            "cycle_major": 1.5,         # 90/144/180/270/360
            "cycle_minor": 0.75,
            "price_time_square": 2.0,
            "regime_align": 1.5,
            "structure_align": 1.0,
            "confirmation_bar": 1.5,
        },
        "major_cycles": [90, 144, 180, 270, 360],
        "major_angles": ["1x2", "2x1"],
        "stop_atr_buffer": 0.5,         # ATR multiple beyond the Gann level for the stop
        "min_stop_pct": 0.0015,         # floor the underlying stop distance at 0.15%
        # Only target Gann levels at least this many R away — a near level gives
        # a sub-1R win against a full -1R stop, which is negative-expectancy even
        # at a 50% hit-rate. If no level qualifies, the trade runs on the trail.
        "min_target_r": 1.5,
    },
    # ── Risk / execution ────────────────────────────────────────────────────
    "risk": {
        "option_premium_budget": 50000.0,    # ₹ premium outlay target per index option
        "futures_notional_target": 1500000.0,  # ₹ notional per commodity futures (matches commodity desk)
        "daily_loss_cap": 25000.0,           # stop opening new trades once today's realized <= -cap
        "max_portfolio_positions": 12,
        "breakeven_at_r": 1.0,               # move stop→entry after +1R (on the underlying)
        "trail_start_r": 1.5,                # start trailing after +1.5R
        "trail_atr_mult": 2.0,               # trail this many ATR behind the underlying
        "time_stop_bars": 26,                # exit if held this long without +0.5R progress
        "time_stop_min_r": 0.5,
        "option_premium_hard_stop_pct": 55.0,  # premium backstop vs theta bleed
        "option_expiry_day_exit": True,
    },
    "backtest": {
        "max_events": 120,
        "max_bars": 260,
        "risk_reward": 1.6,
    },
    "paper": {
        "journal_root": RUNTIME_ROOT / "paper",
    },
    "paper_agent": {
        "enabled": True,
        "timeframe": "15minute",
        "lookback_sessions": 45,
        "anchor_mode": "auto_pivot",
        "h_mode": "median_tpd",
        "live_refresh": False,
        "lots": 1,
        "max_positions": 20,
        "max_days_to_expiry": 45,
        # Memory guard: each scanned underlying loads a deep 1-min frame (~20-30k
        # bars) and builds features. Six concurrently OOM-kills the memory-limited
        # prod box (and a recreate re-syncs the bind mount). 3 matches the old
        # working peak; open-position management reuses cached scan snapshots so
        # there is no extra per-position frame load on top.
        "scan_concurrency": 3,
        "min_score": 3,
        "stop_loss_pct": 35.0,
        "target_pct": 50.0,
    },
}


def clone_default_config() -> dict[str, Any]:
    import copy

    return copy.deepcopy(DEFAULT_CONFIG)
