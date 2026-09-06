"""Public MP API; the engine owns versioned caching across all callers.

The full candle content, effective parameters and prior profile define identity.
Redis shares identical results between the API, workers and research processes.
"""
from __future__ import annotations

from typing import Any, Optional

from auction_intelligence.market_profile import MarketProfileEngine
from auction_intelligence.schemas import MarketBar, MarketProfileSnapshot

from mp_core.intelligence import unified_signals

def _coerce_bars(bars: list[Any]) -> list[MarketBar]:
    if bars and isinstance(bars[0], MarketBar):
        return bars
    out = []
    for row in bars:
        get = row.get if isinstance(row, dict) else lambda k, _r=row: getattr(_r, k, None)
        out.append(MarketBar(
            timestamp=get("time") or get("timestamp"),
            open=float(get("open") or 0.0), high=float(get("high") or 0.0),
            low=float(get("low") or 0.0), close=float(get("close") or 0.0),
            volume=float(get("volume") or 0.0),
        ))
    return out


def build_cached_profile(
    symbol: str,
    bars: list[Any],
    *,
    tick_size: float,
    period_minutes: int = 30,
    initial_balance_periods: int = 2,
    prior_profile: Optional[MarketProfileSnapshot] = None,
) -> MarketProfileSnapshot:
    """Drop-in for MarketProfileEngine.build_profile, memoised.

    The prior profile participates in the key through its identity fields so a
    current-session profile computed against a different prior is not served
    stale comparatives."""
    coerced = _coerce_bars(bars)
    engine = MarketProfileEngine({
        "period_minutes": period_minutes, "tick_size": tick_size,
        "initial_balance_periods": initial_balance_periods,
    })
    return engine.build_profile(symbol, coerced, prior_profile=prior_profile)


def unified_snapshot(
    symbol: str,
    current_bars: list[Any],
    *,
    tick_size: float,
    prior_bars: Optional[list[Any]] = None,
    weekly_va: Optional[tuple[float, float]] = None,
    monthly_va: Optional[tuple[float, float]] = None,
    period_minutes: int = 30,
) -> dict[str, Any]:
    """One profile + intelligence payload for every consumer (lanes, UI, API)."""
    prior = None
    if prior_bars:
        prior = build_cached_profile(symbol, prior_bars, tick_size=tick_size,
                                     period_minutes=period_minutes)
    current = build_cached_profile(symbol, current_bars, tick_size=tick_size,
                                   period_minutes=period_minutes,
                                   prior_profile=prior)
    intel = unified_signals(current, weekly_va=weekly_va, monthly_va=monthly_va)
    # The profile block is the FULL snapshot in the exact shape the frontend
    # workbench already normalises for the convergence lane (asdict + a prior
    # levels block) -- so adopting this endpoint is a source swap, not a
    # re-render. tpo_counts/tpo_letters are what the TPO ladder draws from.
    from dataclasses import asdict

    profile_block: dict[str, Any] = asdict(current)
    if prior is not None:
        profile_block["prior"] = {"vah": prior.vah, "val": prior.val,
                                  "poc": prior.poc, "high": prior.high_price,
                                  "low": prior.low_price,
                                  "close": prior.close_price}
    payload: dict[str, Any] = {
        "symbol": symbol,
        "session_date": current.session_date,
        "profile": profile_block,
        "comparatives": {
            "value_area_overlap": current.value_area_overlap,
            "poc_shift": current.poc_shift,
            "value_migration": current.value_migration,
            "prior_poc_untouched": current.prior_poc_untouched,
            "bracket_state": current.bracket_state,
        },
        "intelligence": intel,
        "cache": cache_stats(),
    }
    return payload


def cache_stats() -> dict:
    from mp_core.cache import stats
    return stats()
