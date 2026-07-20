"""Default configuration for the MACD Refined long-premium engine.

MACD Refined implements the deployment spec in
`STRATEGY-premium-macd-lowiv.md` as a standalone strategy lane:

  Buy the single-leg ATM option (CE *or* PE) whose **option-premium MACD**
  just crossed zero, only when its IV is cheap vs its own history, sized to
  its live traded volume, held to the expiry-7d window, run as separate
  capped CE / PE books — calls carry up-markets, puts carry down-markets,
  volume gives ~6 days' and a directional heads-up.

All parameters below are the **frozen** values from the historical study
(spec §11A). They are intentionally NOT optimised on live data — the
walk-forward protocol re-estimates only the rolling IV-rank / turnover
*baselines*, never the parameters.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from analysis.instruments import ALL_FO_INDICES, STRIKE_STEPS


PACKAGE_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = PACKAGE_ROOT.parent
PROJECT_ROOT = BACKEND_ROOT.parent

# The canonical research dataset lives at the repo root in `data/` — 30-min
# option premium candles (premium OHLC + volume + OI + IV + greeks), 30-min
# spot candles, catalogs (lot sizes), and the MACD signal exports that this
# strategy was derived from. This is the "existing data" the backtest runs on.
DATA_ROOT = PROJECT_ROOT / "data"
RUNTIME_ROOT = BACKEND_ROOT / "runtime" / "macd_refined"

# Funded equity anchor for paper-trading capital accounting (spec §6/§11A:
# total strategy capacity ≈ ₹50L–₹1cr; we anchor at ₹50L).
MACD_REFINED_INITIAL_CAPITAL: float = 5_000_000.0

# Backtest universe = every F&O single-stock + index present in the dataset.
# Resolved dynamically from the catalog at runtime; this is only the fallback.
FNO_STOCK_FALLBACK: list[str] = sorted(
    symbol for symbol in STRIKE_STEPS if symbol not in set(ALL_FO_INDICES)
)

# Live positioning universe — the symbols whose current + next monthly expiry
# chains the auto-runner fetches and paper-trades. Kept to the liquid core by
# default so the broker fetch stays inside rate limits; widen via config /
# env when fills have been validated (spec §6 capacity note). Indices first,
# then a curated set of the most liquid F&O stocks.
MACD_REFINED_LIVE_UNIVERSE: list[str] = [
    "NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX",
    "RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "INFY", "TCS",
    "AXISBANK", "KOTAKBANK", "ITC", "LT", "BHARTIARTL", "BAJFINANCE",
    "TATASTEEL", "TITAN", "MARUTI", "HINDALCO",
]


DEFAULT_CONFIG: dict[str, Any] = {
    "key": "macd_refined",
    "label": "MACD Refined",
    # Market profile — drives broker adapter, underlying-symbol mapping, expiry
    # calendar, and lot size. "india" = FYERS + NSE F&O (default); "us" = Alpaca
    # + US equity/ETF options (see clone_us_config()).
    "market": "india",
    "lot_size_override": None,
    "description": (
        "Premium-MACD entry, IV-regime mapped, liquidity-gated single-leg long options. "
        "Buys the ATM CE/PE whose option-premium MACD(12,26,9) just crossed zero "
        "with IV rank recorded as context, sized to live option turnover, held to expiry-7d, "
        "run as separate capped CE & PE books (1 leg per stock)."
    ),
    "data_root": str(DATA_ROOT),
    "runtime_root": str(RUNTIME_ROOT),
    "default_underlying": "NIFTY",
    "timeframe": "30minute",          # spec §3 — 30-minute bar
    # Live universe resolution. "full" → every F&O underlying in
    # fo_underlying_catalog (the spec's ~180–217-name universe); "list" → the
    # curated MACD_REFINED_LIVE_UNIVERSE below. Full-universe cycles fetch
    # current+next expiry chains for all names, so the auto-runner interval is
    # aligned to 30-minute bars (env MACD_REFINED_AUTO_INTERVAL_SECONDS, default
    # 1800s) to respect
    # broker rate limits — a 30-min strategy doesn't need a tighter cadence.
    "live_universe_mode": "full",
    "live_universe": list(MACD_REFINED_LIVE_UNIVERSE),

    # ── Signal (spec §4) ──────────────────────────────────────────────────
    "signal": {
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        # Strike selection — buy ATM (spec §1/§3). Allow a 1-step tolerance so
        # the contract nearest spot at signal time qualifies as "the ATM leg".
        "strike_selection": "ATM",
        "atm_tolerance_steps": 1,
        # Early-warning volume surge (spec §4.4). NOTE: the standalone 2-stage
        # "deploy ⅓ on the surge ~6 days before the MACD cross" STARTER is NOT
        # yet wired — see exits.starter_invalidation_sessions and
        # sizing.starter_fraction (both reserved). Today the surge ratio is
        # computed and recorded as CONTEXT on each signal, not acted on alone.
        "volume_surge_multiple": 2.0,
        "volume_baseline_sessions": 10,
        # Directional read from the CE/PE turnover imbalance (spec §4.5).
        # USED as the leg-selection FALLBACK in both the engine and the live
        # path when the spot/MA trend is unavailable (PE-dominant → PE/down,
        # CE-dominant → CE/up). It is also recorded as context on every signal.
        "pe_dominant_ratio": 1.5,     # PE turnover ≥ 1.5× CE → down bias
        "ce_dominant_ratio": 1.5,     # CE turnover ≥ 1.5× PE → up bias
    },

    # ── Filters / gates ───────────────────────────────────────────────────
    # The MACD strategy is PURE premium-MACD zero-cross: buy whichever ATM leg's
    # own premium MACD crosses above zero. Gates are only the tradeability
    # filters (liquidity + entry window). IV is NOT a gate — it is recorded for
    # MAPPING only (labelling each signal's vol regime). Direction is NOT
    # predicted (no trend leg-selection): both CE and PE are eligible on their
    # own crosses.
    "filters": {
        # IV is mapping-only. iv_gate_enabled stays False — IV-rank is computed
        # and attached to every signal as a label, never used to accept/reject.
        "iv_gate_enabled": False,
        "iv_rank_max": 0.30,               # (label threshold for mapping only)
        "iv_rank_window_sessions": 252,
        # Retained for causal IV-regime mapping and research/backtest
        # compatibility. These values do not gate live entries while
        # iv_gate_enabled is False.
        "iv_below_median_ratio": 0.80,
        "iv_below_realized_vol": True,
        # Liquidity floor — skip if recent daily turnover < ₹3L/day (real
        # tradeability guard, not an edge gate).
        "min_daily_turnover_rupees": 300_000.0,
        # Entry window — no new entries inside the last N days to expiry.
        "entry_window_days_before_expiry": 7,
        # No directional leg-selection — pure MACD takes both legs.
        "trend_alignment_enabled": False,
        "trend_ma_sessions": 20,
    },

    # ── Position sizing (spec §6) ─────────────────────────────────────────
    "sizing": {
        # Per-trade rupee size = min of these three.
        "turnover_fraction": 0.05,         # 5% × contract recent daily turnover
        "equity_fraction": 0.10,           # 10% of current portfolio equity
        "per_name_cap_rupees": 1_000_000.0,  # ₹10L hard cap per name
        "min_ticket_rupees": 50_000.0,     # skip below this economic ticket
        # Reserved for two-stage accumulation (deploy ~⅓ on the volume surge).
        # The starter path is NOT yet wired — this is a placeholder for the
        # §6 two-stage entry once the early-warning starter is implemented.
        "starter_fraction": 0.3334,
    },

    # ── Portfolio construction (spec §7) ──────────────────────────────────
    "portfolio": {
        "ce_slots": 10,                    # CE book position limit
        "pe_slots": 10,                    # PE book position limit
        "one_leg_per_stock": True,         # never hold CE and PE on same name
        "daily_new_entry_cap": 8,          # spec §9 — avoid turn-day clustering
        "reinvest": True,                  # size off current equity (compounding)
    },

    # ── Exit rules: hard stop + partial booking + trailing ────────────────
    # The strategy exit model (per desk spec): a HARD stop-loss on the premium,
    # PARTIAL profit booking at a target ladder, and a TRAILING stop on the
    # runner after the first target. All evaluated every live cycle on the
    # freshest mark; the hard stop is gap-safe (always evaluated, never
    # suppressed on a stale mark). window_end is the final time-based exit.
    "exits": {
        "stop_loss_pct": 0.30,             # hard SL: close all if premium ≤ entry×(1−0.30)
        # Partial booking ladder — book `book_fraction` of the ORIGINAL qty the
        # first time premium ≥ entry×(1+gain_pct). Applied in order, once each.
        "targets": [
            {"gain_pct": 0.50, "book_fraction": 0.34},   # +50% → book ~1/3
            {"gain_pct": 1.00, "book_fraction": 0.50},   # +100% → book ~1/2 of remainder
        ],
        # Trailing stop on the remainder after the first target books.
        "trail_after_first_target": True,
        "trail_giveback_pct": 0.25,        # trail = peak_premium × (1 − 0.25)
        "hold_to_window_end": True,        # final exit at expiry−7d window end
        # Legacy alias kept for any external reader; the live stop uses stop_loss_pct.
        "catastrophe_stop_pct": 0.50,
    },

    # ── Risk limits (spec §9) ─────────────────────────────────────────────
    "risk": {
        "starting_equity": MACD_REFINED_INITIAL_CAPITAL,
        # Kill switch — pause new entries if rolling 20-trade win-rate < 60%
        # or rolling drawdown exceeds the equity fraction below.
        "kill_switch_winrate_window": 20,
        "kill_switch_min_winrate": 0.60,
        "kill_switch_max_drawdown_pct": 0.25,
    },

    # ── Execution assumptions (spec §11B — honest, post-cost) ─────────────
    "execution": {
        # Round-trip slippage assumption applied in the backtest so figures
        # are not the optimistic "fills at recorded prices" upper bound.
        "round_trip_slippage_pct": 0.05,   # 5% round-trip (2.5% entry + 2.5% exit)
        "fill_on": "next_bar_open",        # enter on the next bar (no lookahead)
    },

    # ── Backtest defaults ─────────────────────────────────────────────────
    "backtest": {
        "lookback_expiries": 6,            # how many recent expiry files to scan
        "max_underlyings": None,           # None = full universe
    },

    "paper_trading": {
        "journal_root": str(RUNTIME_ROOT / "paper"),
        "one_leg_per_stock": True,
    },

    "live": {
        # Volume / turnover tracking store (spec: "add volume tracking of
        # options contracts"). Per-contract snapshots accumulate here so the
        # turnover baselines and IV-rank windows build over time.
        "volume_store_root": str(RUNTIME_ROOT / "volume_tracking"),
        # Fetch this many strikes either side of ATM per (underlying, expiry).
        "strikes_each_side": 3,
        # Resolve current + next monthly expiry for next-month positioning.
        "expiries_ahead": 2,
        # Per-name chain/history work is independent. The Fyers adapter's
        # process-global limiter still caps aggregate REST admission.
        "max_concurrent_names": 6,
        "broker_timeout_seconds": 12.0,
        # Preserve the full F&O universe while preventing one bad contract
        # from occupying a bulk-worker slot until the supervisor deadline.
        "name_timeout_seconds": 75.0,
        # Max age (seconds) a real-time tick / chain-cache mark may have before
        # the seconds-cadence protective-exit pass treats a held position as
        # having NO fresh mark (fresh=False → its price exits are skipped, the
        # 30m cycle stays its backstop). Sits just above the ~45s marks cadence.
        "marks_max_age_seconds": 60.0,
    },
}


def clone_default_config() -> dict[str, Any]:
    """Return a deep-copy-safe configuration dictionary."""
    return copy.deepcopy(DEFAULT_CONFIG)


# ── US market profile — RETIRED 2026-07-20 ───────────────────────────────
# `us_macd_refined` was removed on the owner's instruction that only MACD and
# MACD-refined remain. US_RUNTIME_ROOT / MACD_REFINED_US_* / clone_us_config()
# are deleted; the historical runtime tree backend/runtime/us_macd_refined/ is
# KEPT untouched.
