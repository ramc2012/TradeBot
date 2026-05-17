"""Relative-IV and premium-band helpers for the entry pipeline.

The original S1 design used three fixed gates:
  * MIN_PREMIUM = ₹2, MAX_PREMIUM = ₹500
  * MAX_ENTRY_IV_PCT = 30 (prefer) / HARD_MAX_IV_PCT = 45 (reject)

Both were problematic in production:

  * Premium rupee-band is *underlying-dependent*. ATM weekly NIFTY runs
    ₹50–₹200; ATM weekly GOLD runs ₹2,000–₹6,000; deep ITM RELIANCE can
    be ₹600 even when the strategy is sound. A single rupee band rejects
    legitimate setups whenever the underlying spot is far from the band.

  * Absolute IV gate is *regime-dependent*. A 40% IV on RELIANCE during
    a calm market is genuinely overpriced. The same 40% IV when market
    IV is 38% is just an average instrument. The same 40% IV during a
    high-vol week may even be *cheap* relative to peers. The correct
    quantity is the *spread vs market IV*, not the absolute level.

This module replaces both with:

  * ``premium_passes_floor(premium, spot)`` — a minimal liquidity sanity
    check (absolute floor + spot-relative floor), no upper bound.

  * ``iv_size_scaler(instrument_iv_pct, market_iv_pct)`` — returns a
    multiplicative size scaler in (0, 1] based on how much the
    instrument's IV exceeds the market's. High IV → smaller bet, not no
    bet. Below or near market IV → full size (1.0×). A sanity hard-cap
    blocks implausible broker readings (>90%).
"""
from __future__ import annotations

from typing import Optional

from agent.strategy_config import (
    IV_SANITY_MAX_PCT,
    IV_SPREAD_CAUTION_PP,
    IV_SPREAD_HEAVY_PP,
    IV_SPREAD_EXTREME_PP,
    MIN_PREMIUM_ABS,
    MIN_PREMIUM_PCT_OF_SPOT,
)


def premium_passes_floor(premium: Optional[float], spot: Optional[float]) -> tuple[bool, str]:
    """Return (passes, reason). Replaces the old MIN_PREMIUM..MAX_PREMIUM
    rupee band with a spot-relative liquidity sanity floor.
    """
    try:
        prem = float(premium) if premium is not None else 0.0
        sp = float(spot) if spot is not None else 0.0
    except (TypeError, ValueError):
        return False, "premium_or_spot_invalid"
    if prem < MIN_PREMIUM_ABS:
        return False, f"premium_below_abs_floor_{MIN_PREMIUM_ABS}"
    if sp > 0:
        floor = sp * MIN_PREMIUM_PCT_OF_SPOT
        if prem < floor:
            return False, f"premium_below_spot_relative_floor_{floor:.2f}"
    return True, "ok"


def iv_size_scaler(
    instrument_iv_pct: Optional[float],
    market_iv_pct: Optional[float],
) -> tuple[float, str]:
    """Return (scaler in (0.0, 1.0], note). Hard-rejects with 0.0 only
    when the instrument IV is implausibly high (broker data sanity).

    Decision curve (spread = instrument_iv - market_iv, in pp):
        spread ≤ IV_SPREAD_CAUTION_PP  →  1.00× (full size)
        spread ≤ IV_SPREAD_HEAVY_PP    →  0.75×
        spread ≤ IV_SPREAD_EXTREME_PP  →  0.50×
        spread >  IV_SPREAD_EXTREME_PP →  0.25×
    """
    if instrument_iv_pct is None:
        # Unknown IV is informational, not blocking — return full size
        # with an explanatory note. Callers can decide if they want to
        # be more conservative.
        return 1.0, "iv_unknown"
    try:
        iv = float(instrument_iv_pct)
    except (TypeError, ValueError):
        return 1.0, "iv_unparseable"
    if iv >= IV_SANITY_MAX_PCT:
        return 0.0, f"iv_sanity_reject_{iv:.1f}"
    if market_iv_pct is None:
        # No market reference — treat the instrument IV vs a neutral
        # reference of 22% (median NSE F&O IV historical average) so
        # the scaler still does sensible scaling without market data.
        market_iv_pct = 22.0
    try:
        mkt = float(market_iv_pct)
    except (TypeError, ValueError):
        mkt = 22.0
    spread = iv - mkt
    if spread <= IV_SPREAD_CAUTION_PP:
        return 1.0, f"iv_spread_normal_{spread:+.1f}pp"
    if spread <= IV_SPREAD_HEAVY_PP:
        return 0.75, f"iv_spread_caution_{spread:+.1f}pp"
    if spread <= IV_SPREAD_EXTREME_PP:
        return 0.50, f"iv_spread_heavy_{spread:+.1f}pp"
    return 0.25, f"iv_spread_extreme_{spread:+.1f}pp"


__all__ = ["premium_passes_floor", "iv_size_scaler"]
