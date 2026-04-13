from __future__ import annotations

from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")

SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
LATEST_ENTRY_TIME = time(14, 30)
FORCE_EXIT_TIME = time(15, 0)

SUPPORTED_SYMBOLS: tuple[str, ...] = ("NIFTY", "SENSEX")

INDEX_APP_SYMBOLS: dict[str, str] = {
    "NIFTY": "NSE:NIFTY50-INDEX",
    "SENSEX": "BSE:SENSEX-INDEX",
}

LOT_SIZES: dict[str, int] = {
    "NIFTY": 65,
    "SENSEX": 10,
}

OPTION_STRIKE_STEPS: dict[str, float] = {
    "NIFTY": 50.0,
    "SENSEX": 100.0,
}

PROFILE_CONFIG: dict[str, float | int] = {
    "daily_period_minutes": 30,
    "hourly_period_minutes": 3,
    "daily_initial_balance_periods": 2,
    "hourly_initial_balance_periods": 2,
    "value_area_pct": 0.70,
}

SCAN_CONFIG: dict[str, float | int] = {
    "narrow_hourly_ib_factor": 0.70,
    "wide_daily_ib_factor": 1.50,
    "min_value_migration_abs": 2,
    "bullish_pcr_min": 1.20,
    "bearish_pcr_max": 0.80,
    "max_iv_rank_for_buying": 50.0,
    "india_vix_defined_risk": 22.0,
    "min_dte_for_long_options": 5,
}

RISK_CONFIG: dict[str, float | int] = {
    "max_risk_per_trade_pct": 0.015,
    "max_daily_loss_pct": 0.02,
    "max_premium_loss_pct": 0.50,
    "max_concurrent_positions": 2,
}

BACKEND_ROOT = Path(__file__).resolve().parent.parent
PAPER_ROOT = BACKEND_ROOT / "runtime" / "fractal_market_profile"
REPLAY_ROOT = PAPER_ROOT / "replays"


def analytics_root() -> Path:
    return BACKEND_ROOT / "runtime" / "index_analytics_data"
