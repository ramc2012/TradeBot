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
    "backtest": {
        "max_events": 120,
        "risk_reward": 1.6,
    },
    "paper": {
        "journal_root": RUNTIME_ROOT / "paper",
    },
    "paper_agent": {
        "enabled": True,
        "timeframe": "15minute",
        "lookback_sessions": 60,
        "anchor_mode": "auto_pivot",
        "h_mode": "median_tpd",
        "live_refresh": False,
        "lots": 1,
        "max_positions": 20,
        "max_days_to_expiry": 45,
        "scan_concurrency": 6,
        "min_score": 3,
        "stop_loss_pct": 35.0,
        "target_pct": 50.0,
    },
}


def clone_default_config() -> dict[str, Any]:
    import copy

    return copy.deepcopy(DEFAULT_CONFIG)
