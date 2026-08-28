"""The intelligence layer: what a Market Profile is ENTITLED to say.

Everything here was measured on 2026-08-28 across ~5 years of 30-minute data
(BANKNIFTY / NIFTY / 16 bank stocks), with session-clustered errors,
walk-forward where a rule was selected, and adversarial review. The verdicts
travel as data so every consumer — lane, UI, research — reads the same ones
instead of re-learning them.

Functions here are PURE: they take MarketProfileSnapshot objects (the single
TPO engine's output) plus optional higher-timeframe value areas, and return
plain dicts. No I/O, no state — the caching lives in mp_core.service.
"""
from __future__ import annotations

from typing import Any, Optional

from auction_intelligence.schemas import MarketProfileSnapshot

# Machine-readable research verdicts. status: validated | context | falsified.
VERDICTS: dict[str, dict[str, str]] = {
    "ib_width": {
        "status": "context",
        "meaning": "Predicts RANGE (|move| IC ~+0.46, t>10), NOT direction "
                   "(signed |t|<1.8). Use for sizing and expectation, never side.",
    },
    "value_area": {
        "status": "context",
        "meaning": "Location vs value ranks nothing by itself; close ABOVE value "
                   "in the 0.70-0.90 range band is the validated form (see "
                   "sig_strong_close). Margin above VAH adds nothing — larger "
                   "margins do WORSE (win 59%->36% as margin grows).",
    },
    "sig_strong_close": {
        "status": "validated",
        "meaning": "Close above VAH AND close_pos in [0.70,0.90] (acceptance, "
                   "not spike). Validated expression: overnight gap to next "
                   "open — BANKNIFTY 5y +0.175%/night, 71% win, t+3.92, "
                   "positive every year; edge is SPENT by 09:15. Futures "
                   "transfer consistent (+0.174%, n=34).",
    },
    "sig_oversold_mtf": {
        "status": "validated",
        "meaning": "Close below the day's AND prior week's AND prior month's "
                   "value. Contrarian UP signal; the only condition that "
                   "replicated across NIFTY/BANKNIFTY/stocks (lifts "
                   "1.24/1.70/1.44). Path dips first: tight stops halve it.",
    },
    "mtf_alignment": {
        "status": "falsified",
        "meaning": "'Above day+week+month value' REDUCES hit rates "
                   "monotonically (49->44%, 28->24%, 19->17%). Do not gate on it.",
    },
    "eighty_percent_rule": {
        "status": "falsified",
        "meaning": "Re-entry into prior value traversed it 49.4% of the time "
                   "over 1,249 sessions — a coin flip, not 80%.",
    },
    "compression": {
        "status": "falsified",
        "meaning": "Narrow VA/IB/low ATR all LOWER the odds of a 2% move "
                   "(lifts 0.84-0.89). Volatility clusters; it does not coil.",
    },
    "ib_break": {
        "status": "falsified",
        "meaning": "Directional IB-break trades fail at every tested horizon "
                   "(intraday scalp net t-2.9; 3-day hold -0.17%/trade). The "
                   "poorer-known fact: 79% of sessions break the IB at all.",
    },
    "day_type": {
        "status": "context",
        "meaning": "Descriptive at the close; no next-session direction "
                   "(|t|<1.4 across all six types). Trend days show skew 1.58 "
                   "for CONTINUED movement size, not sign.",
    },
}

TREND_CLOSE_PCT = 0.15


def _close_pos(s: MarketProfileSnapshot) -> float:
    rng = max(s.high_price - s.low_price, 1e-9)
    return (s.close_price - s.low_price) / rng


def _double_distribution(s: MarketProfileSnapshot) -> bool:
    """Two separated TPO modes with a genuine valley — ported from research."""
    counts = s.tpo_counts
    if len(counts) < 7:
        return False
    prices = sorted(counts)
    vals = [counts[p] for p in prices]
    peak = max(vals)
    if peak < 3:
        return False
    strong = [i for i, v in enumerate(vals) if v >= 0.7 * peak]
    runs: list[tuple[int, int]] = []
    start = strong[0]
    for a, b in zip(strong, strong[1:]):
        if b - a > 1:
            runs.append((start, a))
            start = b
    runs.append((start, strong[-1]))
    if len(runs) < 2:
        return False
    (a0, a1), (b0, b1) = runs[0], runs[-1]
    if b0 <= a1 + 1:
        return False
    valley = vals[a1 + 1:b0]
    return bool(valley) and min(valley) <= 0.4 * min(max(vals[a0:a1 + 1]),
                                                    max(vals[b0:b1 + 1]))


def classify_day_type(s: MarketProfileSnapshot) -> str:
    """Dalton's day types from one session's snapshot (same rules as research)."""
    ib = max(s.initial_balance_range, 1e-9)
    r = s.day_range / ib
    close_pos = _close_pos(s)
    two_sided = s.range_extension_up > 0 and s.range_extension_down > 0
    if _double_distribution(s) and r >= 2.0:
        return "double_distribution"
    if two_sided:
        return ("neutral_extreme"
                if (close_pos >= 0.85 or close_pos <= 0.15) else "neutral")
    if r >= 2.0 and (close_pos >= 1 - TREND_CLOSE_PCT
                     or close_pos <= TREND_CLOSE_PCT):
        return "trend"
    if r < 1.15:
        return "normal"
    return "normal_variation"


def unified_signals(
    s: MarketProfileSnapshot,
    *,
    weekly_va: Optional[tuple[float, float]] = None,   # (val, vah), prior COMPLETED week
    monthly_va: Optional[tuple[float, float]] = None,  # (val, vah), prior COMPLETED month
) -> dict[str, Any]:
    """The intelligence read of one session: day type, the validated flags, and
    the range expectation. Weekly/monthly value areas are optional; the
    oversold flag is only emitted when both are supplied (a partial check is a
    different, unvalidated signal and is not silently substituted)."""
    close_pos = _close_pos(s)
    strong_close = bool(s.close_price > s.vah and 0.70 <= close_pos <= 0.90)
    oversold: Optional[bool] = None
    if weekly_va is not None and monthly_va is not None:
        oversold = bool(
            s.close_price < s.val
            and s.close_price < weekly_va[0]
            and s.close_price < monthly_va[0]
        )
    return {
        "day_type": classify_day_type(s),
        "close_pos": round(close_pos, 4),
        "sig_strong_close": strong_close,
        "sig_oversold_mtf": oversold,
        "range_over_ib": round(s.day_range / max(s.initial_balance_range, 1e-9), 3),
        "ib_width_pct": round(s.initial_balance_range / max(s.close_price, 1e-9) * 100, 4),
        "va_width_pct": round((s.vah - s.val) / max(s.close_price, 1e-9) * 100, 4),
        "verdicts": VERDICTS,
    }
