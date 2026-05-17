"""Relative-IV position-sizing helper for the entry pipeline.

The original S1 design used an *absolute* IV gate
(MAX_ENTRY_IV_PCT=30 / HARD_MAX_IV_PCT=45) that hard-rejected setups
above 45% IV. That is regime-dependent and wrong:

  * 40% IV on RELIANCE in a calm market is genuinely overpriced.
  * 40% IV when market IV is 38% is just an average instrument.
  * 40% IV during a vol-spike week may even be cheap relative to peers.

The correct quantity is the *spread vs market IV*, not the absolute
level. ``iv_size_scaler(instrument_iv, market_iv)`` returns a
multiplicative size scaler in (0, 1] based on how much the instrument's
IV exceeds the market's. High IV → smaller bet, not no bet. Below or
near market IV → full size. A sanity hard-cap blocks implausible
broker readings (>90% IV).

NOTE: Premium price filtering was deliberately removed from the
strategy. We trade ATM options only, and the ATM contract on a live
F&O underlying is liquid by construction.
"""
from __future__ import annotations

from typing import Optional

from agent.strategy_config import (
    IV_SANITY_MAX_PCT,
    IV_SPREAD_CAUTION_PP,
    IV_SPREAD_HEAVY_PP,
    IV_SPREAD_EXTREME_PP,
)


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


__all__ = ["iv_size_scaler"]
