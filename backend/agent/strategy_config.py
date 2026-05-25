"""Strategy constants derived from STRATEGY_DOCUMENT.md backtest analysis.

All numeric values are validated against 725 signals across 211 NSE F&O
underlyings (Apr 2025 – Mar 2026).  Change with care — each constant has
a statistical rationale documented in STRATEGY_DOCUMENT.md §14.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet


# ── MACD Parameters ──────────────────────────────────────────────────────────

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
MACD_INTERVAL = "30minute"
MACD_MIN_BARS = 35  # slow + signal warmup


# ── Trading Window (Physical Delivery Constraint) ────────────────────────────

WINDOW_BUFFER_DAYS = 7  # days before expiry to exit (avoid delivery margins)


# ── Entry Filters ────────────────────────────────────────────────────────────

# IV filter is now *relative* and *size-scaling*, not a hard gate.
#   1. Compare per-instrument IV against the prevailing market IV
#      (median IV across the F&O universe). A high IV in isolation
#      is much less informative than a high IV-vs-market spread.
#   2. When IV is elevated, REDUCE position size rather than reject
#      outright. The thinking: high-IV setups are still tradeable,
#      they just deserve smaller bets because the premium itself is
#      already paying for expected move.
# These thresholds are spread-vs-market (in percentage points), NOT
# absolute IV. e.g. market IV 22%, instrument IV 32% → +10 pp spread.
IV_SPREAD_CAUTION_PP = 8.0    # > +8 pp over market → 0.75× size
IV_SPREAD_HEAVY_PP   = 15.0   # > +15 pp over market → 0.50× size
IV_SPREAD_EXTREME_PP = 25.0   # > +25 pp over market → 0.25× size
# Hard reject only when the instrument IV is implausibly high — a
# defensive sanity-check against bad broker data, not a strategy gate.
IV_SANITY_MAX_PCT    = 90.0
# Legacy aliases kept for backward-compat with imports elsewhere in
# the codebase. The new IV policy supersedes both but downstream code
# may still import these names.
MAX_ENTRY_IV_PCT = 30.0
HARD_MAX_IV_PCT  = IV_SANITY_MAX_PCT

# Indices are cash-settled — they can be traded right up to the day
# before expiry (T-1). S1 specifically does NOT enter on the expiry
# day itself (T-0) because the MACD signal has no time to play out
# and end-of-day theta annihilation kills long-premium positions.
#
# Stocks are physically settled — rolling to next month when the
# active expiry is too close avoids assignment / delivery risk. The
# MI layer's _stock_monthly_for_selected_expiry handles the rollover
# at watchlist build time (≤3 trading days → next monthly).
MIN_TTE_DAYS_INDEX = 1        # T-1 entry OK; T-0 (expiry day) skipped
MIN_TTE_DAYS_STOCK = 3        # MI rolls to next expiry when ≤3td left
# Legacy name retained for callers; defaults to the index threshold.
MIN_TTE_DAYS = MIN_TTE_DAYS_INDEX

# Premium price filtering is DELIBERATELY removed.
# The strategy only ever trades ATM options. By construction the ATM
# contract on a live F&O underlying is liquid enough to fill at the
# market — there is no premium band, rupee floor, or spot-relative
# floor that adds signal here. The old fixed band (₹2–₹500) wrongly
# rejected legitimate setups across the universe (deep-ITM RELIANCE,
# GOLD options at ₹6k, etc.). The legacy aliases below are retained
# only so downstream imports do not break; they are not gating.
MIN_PREMIUM = 0.0
MAX_PREMIUM = float("inf")

MIN_CANDLE_BARS = 20          # minimum 30-min bars in window before signal
MIN_CANDLE_BARS_ATM = 20      # minimum bars for ATM strike selection query


# ── MACD Quadrant Regime ─────────────────────────────────────────────────────
# CE MACD ≥0 + PE MACD <0 = Bullish   → buy CE only
# CE MACD <0 + PE MACD ≥0 = Bearish   → buy PE only
# CE MACD <0 + PE MACD <0 = Dead Zone → no trade
# CE MACD ≥0 + PE MACD ≥0 = IV Spike  → evaluate individually

REGIME_BULLISH = "bullish"
REGIME_BEARISH = "bearish"
REGIME_DEAD = "dead_zone"
REGIME_IV_SPIKE = "iv_spike"


# ── Strike Selection ─────────────────────────────────────────────────────────

STRIKE_DEFAULT = "ATM"        # highest win rate 87.9%, highest abs profit
STRIKE_BIG_MOVE = "OTM1"     # for expected spot move ≥10%, OTM wins 65.7%
STRIKE_HIGH_IV = "ITM1"      # for IV >40%, higher delta = less theta
BIG_MOVE_THRESHOLD_PCT = 10.0 # annualized spot vol threshold for OTM switch


# ── Position Sizing (Kelly-based) ────────────────────────────────────────────

KELLY_FRACTION = 0.25         # 0.25× Kelly ≈ 22% capital per trade
KELLY_PREMIUM_FRACTION = 0.50 # 0.50× Kelly for premium setup signals
KELLY_CAUTIOUS_FRACTION = 0.10  # 0.10× Kelly for high-IV or uncertain
MAX_SIMULTANEOUS_POSITIONS = 5
MAX_PER_UNDERLYING = 1        # CE or PE per underlying, not both
MAX_SECTOR_CONCENTRATION = 3
CASH_RESERVE_PCT = 0.20       # keep ≥20% cash for new signals


# ── Exit Strategy — Layered ──────────────────────────────────────────────────

@dataclass(frozen=True)
class ExitConfig:
    # Layer 1: Target (50% of position)
    target_pct: float = 50.0            # exit 50% of lots when +50% reached
    target_exit_fraction: float = 0.50  # fraction of position to exit at target

    # Layer 2: Runner (remaining 50%)
    # Was 100% activation — most runners decayed back before reaching it.
    # 60% mirrors the commodity playbook's "arm trail at +1.5R" idea
    # translated into option-premium %: typical ATM option risk_distance is
    # ~40% of entry premium, so +60% corresponds to roughly +1.5R favorable
    # move on the option side. Then the 25% giveback floor + ATR-points
    # floor (max of the two) protects both relative and absolute drawdown.
    trail_activation_pct: float = 60.0    # arm trail after +60% (was 100)
    trail_drawdown_pct: float = 25.0      # exit on 25% drop from peak (was 20)
    trail_atr_multiplier: float = 1.5     # NEW: ATR-points floor: peak - 1.5×ATR
    macd_death_min_profit_pct: float = 30.0  # MACD reversal exit only after +30%

    # Layer 3: Hard stop
    hard_stop_pct: float = 25.0         # -25% from entry → exit 100%

    # Window end
    window_end_buffer_days: int = 1     # exit 1 day before window_end


EXIT = ExitConfig()


# ── Option MA Management ─────────────────────────────────────────────────────
# MA20 trail from entry is PROVEN DESTRUCTIVE (median -6.3%).
# First MA20 re-touch within 4.5 hrs is NORMAL chop — never exit on it.
# MA20 trail only viable AFTER +100% profit (Layer 2 territory).

OPTION_MA20_TRAIL_ALLOWED = False  # do NOT use MA20 trail from entry
FIRST_PULLBACK_IGNORE_BARS = 20   # ignore MA20 touch in first 20 bars (10 hrs)
OPTION_ENTRY_MA_FAST = 20
OPTION_ENTRY_MA_SLOW = 50
OPTION_ENTRY_REQUIRE_ABOVE_MA20 = True


# ── Spot MA Context at Entry ─────────────────────────────────────────────────

SPOT_MA_FAST = 20
SPOT_MA_SLOW = 50

# Setup classifications:
SETUP_BREAKOUT = "breakout"     # spot above MA20, below MA50 → +202% exit
SETUP_TREND = "trend"           # spot above both MA20+MA50    → +133% exit
SETUP_REVERSAL = "reversal"     # spot below MA20              → +67% exit
SETUP_PREMIUM = "premium"       # option below its own MA50    → +238% max


# ── Risk / Circuit Breakers ──────────────────────────────────────────────────

@dataclass(frozen=True)
class CircuitBreakerConfig:
    max_consecutive_stops: int = 3       # 3 stops → pause new entries
    pause_days_after_stops: int = 5      # pause duration in trading days
    max_portfolio_drawdown_pct: float = 15.0  # reduce to cautious sizing
    dead_zone_universe_pct: float = 70.0  # if 70%+ in dead zone → cash mode
    vix_high_threshold: float = 25.0     # only ITM, half size


CIRCUIT = CircuitBreakerConfig()


# ── Exclusion List ───────────────────────────────────────────────────────────
# Underlyings that consistently fail to deliver on signals (§13.2)

EXCLUDED_UNDERLYINGS: FrozenSet[str] = frozenset({"IEX"})


# ── Telegram / Reporting ─────────────────────────────────────────────────────

SIGNAL_LOG_MAX = 200          # max signal history in memory
COMMENTARY_MAX = 40           # max commentary entries
