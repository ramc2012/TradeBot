"""Compute-once Market Profile: content-addressed cache over the single engine.

THE LOAD PROBLEM THIS SOLVES. institutional_convergence rebuilt a fresh
MarketProfileEngine and re-ran the full TPO ladder for BOTH the current and the
prior session on EVERY evaluation cycle, per symbol; the auction sleeves and the
UI widgets did their own passes over the same bars. The TPO build is the
documented hot spot (see the MAX_LADDER_LEVELS note in the engine: a fine tick
once seized the event loop for minutes). Profiles are pure functions of their
bars, so identical inputs are served from cache instead of recomputed.

CACHE KEY = (symbol, period_minutes, tick, first_ts, last_ts, bar_count,
last_close). A completed prior session hashes identically forever — its profile
is computed exactly once per process. A developing session changes key on every
new bar, which is precisely when recomputation is genuinely needed. last_close
is in the key so an updated (still-forming) final bar also misses.

Process-local by design: profiles hold numpy-free plain dataclasses, the working
set is tiny (hundreds of entries), and a shared-Redis layer would add
serialisation cost to save a computation that only repeats within one process
anyway. LRU-capped so a long-running lane cannot grow without bound.
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Any, Optional

from auction_intelligence.market_profile import MarketProfileEngine
from auction_intelligence.schemas import MarketBar, MarketProfileSnapshot

from mp_core.intelligence import unified_signals

_CACHE: OrderedDict[tuple, MarketProfileSnapshot] = OrderedDict()
_MAX_ENTRIES = 512
_HITS = 0
_MISSES = 0


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


def _key(symbol: str, bars: list[MarketBar], period_minutes: int,
         tick_size: float, prior_key: Optional[tuple]) -> tuple:
    return (symbol, period_minutes, round(tick_size, 6),
            bars[0].timestamp, bars[-1].timestamp, len(bars),
            round(bars[-1].close, 4), prior_key)


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
    global _HITS, _MISSES
    coerced = _coerce_bars(bars)
    if not coerced:
        raise ValueError("build_cached_profile requires at least one bar")
    prior_key = None
    if prior_profile is not None:
        prior_key = (prior_profile.symbol, prior_profile.session_date,
                     round(prior_profile.poc, 4), prior_profile.sample_count)
    key = _key(symbol, coerced, period_minutes, tick_size, prior_key)
    hit = _CACHE.get(key)
    if hit is not None:
        _HITS += 1
        _CACHE.move_to_end(key)
        return hit
    _MISSES += 1
    engine = MarketProfileEngine({
        "period_minutes": period_minutes,
        "tick_size": tick_size,
        "initial_balance_periods": initial_balance_periods,
    })
    snapshot = engine.build_profile(symbol, coerced, prior_profile=prior_profile)
    _CACHE[key] = snapshot
    while len(_CACHE) > _MAX_ENTRIES:
        _CACHE.popitem(last=False)
    return snapshot


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


def cache_stats() -> dict[str, int]:
    return {"entries": len(_CACHE), "hits": _HITS, "misses": _MISSES}
