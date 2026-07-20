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
    # Seed universe. The lane's REAL universe is resolved from the spot store at
    # runtime when `universe_expansion.enabled` is on (all indices + stock spots +
    # commodity futures — 225 symbols have 30-minute history today: 6 indices,
    # 211 stocks, 8 MCX roots). This list is the fallback and the staging default.
    "universe": ["NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOIL", "GOLD", "SILVERM", "NATURALGAS"],
    # ── Universe expansion (GAP 2) ─────────────────────────────────────────
    # Measured cost of the 15-minute path: ~1.06 s per live_snapshot at
    # scan_concurrency=3 ⇒ ~77 s for 217 instruments, i.e. MORE than the 60 s
    # runner cadence, and ~36x today's DB read load on a Postgres OOM-killed
    # twice on 2026-07-20. The daily path is what makes the wide universe
    # affordable (one bounded 30-minute read per instrument per SESSION rather
    # than a deep intraday read per instrument per MINUTE), so this stays OFF
    # until the daily horizon has been observed live for a session.
    # `batch_size` is a round-robin slice per cycle with LOUD accounting
    # (scanned vs universe_size) — never a silent truncation.
    # NOT YET WIRED INTO THE LIVE LANE. `agent.run_once` still scans
    # `universe` only; `universe.resolve_universe` / `universe.SweepCursor` are
    # consumed by the OFFLINE sidecars (watchlist_runner.py, run_cycle_mapping
    # .py). Flipping `enabled` to True changes NOTHING until the agent reads
    # it — saying so here rather than leaving another orphaned knob like
    # `geometry.calendar_cycles`.
    "universe_expansion": {
        "enabled": False,
        "classes": ["index", "stock", "commodity"],
        "batch_size": 12,
        "min_recent_bars": 5,
        "freshness_days": 7,
    },
    "timeframes": ["1day", "15minute", "1hour"],
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
        # "auto" ⇒ gann_tp_delta.cycles.resolve_price_unit picks the power-of-ten
        # chart scale that puts sqrt(price/unit) in [60, 600]. With the unit
        # pinned at 1.0 the SQ9 step is a pure function of price LEVEL — 0.19 %
        # per 45 deg at SENSEX 77k vs 3.2 % at NATURALGAS 275 — so the
        # `reversal_require_cardinal_sq9` gate was a no-op for the expensive
        # symbols and unreachable for the cheap ones and for every NSE stock.
        # Of the seven legacy symbols only NATURALGAS changes (1.0 -> 0.01).
        "price_unit": "auto",
        "near_pct": 0.003,
        # On the daily frame the tolerance window is +/- DAYS, and it is set to
        # match cycle_prominence.TOLERANCE_SESSIONS so the live gate and the
        # historical measurement use the same window.
        "cycle_window_bars": 3,
        "squaring_tolerance": 0.05,
        # 60 daily bars ~ one quarter of forward projection (was 80 fifteen-
        # minute bars ~ 3 sessions).
        "projection_bars": 60,
    },
    # ── Gann TIME cycles (calendar-day, from gann_tp_delta.cycles) ─────────
    # `geometry.bar_cycles` counts BARS and is what the legacy engine used; on
    # the 15-minute frame "cycle 90" was 22.5 hours, not Gann's 90 days. The
    # real families live in gann_tp_delta/cycles.py and are consumed off daily
    # bars. `gate_on_prominence` makes the lane trade only cycles that are
    # demonstrably prominent for that instrument — which, on the 2026-07-20
    # mapping run, is NONE (see docstring in cycle_prominence.py and the
    # gann_cycle_prominence table). It therefore stays OFF: turning it on with
    # an empty prominent set would silently stop the lane rather than
    # improving it.
    # NOT YET WIRED INTO THE LIVE STRATEGY. `evaluate_gann_signal` still gates
    # on `geometry.bar_cycles` (bar counts, now DAILY bars ⇒ trading-day counts,
    # which is closer to Gann than the old 15-minute counts but still not his
    # CALENDAR counts). The calendar library in gann_tp_delta/cycles.py is
    # consumed by the watchlist writer and the prominence mapper only.
    "time_cycles": {
        "enabled": True,
        "gate_on_prominence": False,
        "prominence_run_id": None,
        "tolerance_sessions": 3,
        "next_turn_horizon_days": 180,
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
        # Completed post-warmup bars required to evaluate. On the DAILY frame
        # this is 60 sessions (~3 months), which is the minimum that gives the
        # 5-bar pivot engine enough confirmed anchors for a fan plus an ADX
        # that has left its warm-up. It was 40 fifteen-minute bars (~1 session).
        "min_signal_bars": 60,
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
        # Mandatory setup gates. Conviction ranks otherwise-valid setups; it
        # cannot compensate for a missing trigger or missing reversal geometry.
        "continuation_require_resumption": True,
        "reversal_require_cardinal_sq9": True,
        "reversal_require_major_cycle": True,
        "reversal_require_price_time_square": True,
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
        "commodity_underlyings": ["CRUDEOIL", "GOLD", "SILVERM", "NATURALGAS"],
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
        # Exit if held this long without +0.5R progress. The unit is SIGNAL
        # BARS, so the horizon change re-denominates it: 26 fifteen-minute bars
        # was 6.5 hours (intraday), 10 DAILY bars is two trading weeks. This is
        # the one risk parameter the horizon change strictly required — leaving
        # it at 26 would have meant a 26-session (~5 week) time stop, and
        # leaving it at 2 would have closed every trade inside the week and
        # defeated the "elapsing more than a day" instruction outright.
        "time_stop_bars": 10,
        "time_stop_min_r": 0.5,
        "option_premium_hard_stop_pct": 55.0,  # premium backstop vs theta bleed
        "option_expiry_day_exit": True,
    },
    "backtest": {
        "max_events": 120,
        # WARNING: this tails EVERY bt.run() input. It is an interactive-UI
        # guard, not a research setting — the tuning/validation sweeps now lift
        # it explicitly. Every per-underlying conviction floor above was
        # selected from runs that inherited this cap (~10 sessions on the
        # 15-minute frame, presented as 150-day results) and must be treated as
        # UNVALIDATED until those sweeps are re-run.
        "max_bars": 260,
        "risk_reward": 1.6,
    },
    "paper": {
        "journal_root": RUNTIME_ROOT / "paper",
    },
    "paper_agent": {
        "enabled": True,
        # ── HIGHER-ORDER HORIZON (GAP 3) ───────────────────────────────────
        # The owner's instruction: "it trades only higher order time elapsing
        # more than a day". This lane was deciding on 15-minute bars while its
        # Gann time constructs are calendar objects — 45/90/144/180/360 are day
        # counts tied to degrees of the annual circle, and translating them
        # into bars of an arbitrary intraday grid makes the numbers a
        # coincidence. On the daily frame those same integers become the
        # constructs they are named after, at zero conceptual cost, and the fan
        # angles get room to separate instead of stacking four "confluences"
        # into one location a few basis points wide.
        "timeframe": "1day",
        # DAILY sessions now, not intraday days. 400 ~ 1.6 years: enough for
        # the regime engine, the pivot/anchor engine and every cycle this
        # history can actually test (<= ~92 days), and it is one bounded
        # 30-minute read per instrument.
        "lookback_sessions": 400,
        "anchor_mode": "auto_pivot",
        "h_mode": "median_tpd",
        "live_refresh": False,
        "lots": 1,
        "max_positions": 20,
        # OWNER DECISION PENDING (deliberately left unchanged by the horizon
        # change): with a 10-session time stop the lane now holds for up to two
        # trading weeks, and the current expression is an ATM weekly option.
        # Weekly ATM against a two-week hold is a theta wall — the DTE floor and
        # the strike (ATM vs slightly-ITM) are an expression choice with real
        # cost implications, not a mechanical consequence of the horizon, so it
        # is flagged rather than silently retuned here.
        "max_days_to_expiry": 45,
        "max_entry_quote_age_seconds": 120,
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
