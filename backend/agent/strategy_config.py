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

MAX_ENTRY_IV_PCT = 30.0       # prefer IV ≤ 30% (vol-adjusted return 3.4× better)
HARD_MAX_IV_PCT = 45.0        # refuse entry above 45%
MIN_TTE_DAYS = 7              # need ≥7 days for move to develop (lowered from 14 for weekly-expiry compatibility)
MIN_PREMIUM = 2.0             # avoid illiquid sub-₹2 options
MAX_PREMIUM = 500.0           # overpriced entries fail 4/17 times
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
    trail_activation_pct: float = 100.0   # activate trail after +100%
    trail_drawdown_pct: float = 20.0      # exit on 20% drop from peak
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
