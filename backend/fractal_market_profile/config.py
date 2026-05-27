from __future__ import annotations

from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")

SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
LATEST_ENTRY_TIME = time(14, 30)
FORCE_EXIT_TIME = time(15, 0)
MCX_SESSION_OPEN = time(9, 0)
MCX_SESSION_CLOSE = time(23, 30)
MCX_LATEST_ENTRY_TIME = time(22, 30)

SUPPORTED_SYMBOLS: tuple[str, ...] = ("NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOIL")

INDEX_APP_SYMBOLS: dict[str, str] = {
    "NIFTY": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
    "SENSEX": "BSE:SENSEX-INDEX",
    "CRUDEOIL": "MCX:CRUDEOIL26JUNFUT",
}

LOT_SIZES: dict[str, int] = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "SENSEX": 20,
    "CRUDEOIL": 100,
}

OPTION_STRIKE_STEPS: dict[str, float] = {
    "NIFTY": 50.0,
    "BANKNIFTY": 100.0,
    "SENSEX": 100.0,
    "CRUDEOIL": 50.0,
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
    "min_value_migration_abs": 1,
    "trend_pullback_tolerance_factor": 0.18,
    "balance_reversion_tolerance_factor": 0.14,
    "actionable_confidence_min": 0.55,
    "bullish_pcr_min": 1.20,
    "bearish_pcr_max": 0.80,
    "max_iv_rank_for_buying": 50.0,
    "india_vix_defined_risk": 22.0,
    "min_dte_for_long_options": 5,
    "soft_option_penalty": 0.04,
    "soft_iv_penalty": 0.03,
    "soft_vix_penalty": 0.03,
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

# Notional paper-account capital. The summary surface reports total_equity,
# available_capital, and drawdown against this anchor so cross-strategy
# comparisons match S1/S2/Commodity (all ₹10L).
FMP_INITIAL_CAPITAL = 1_000_000.0


def analytics_root() -> Path:
    return BACKEND_ROOT / "runtime" / "index_analytics_data"
