"""Per-symbol Market-Profile value tick — single source of truth.

A global 0.5 tick over-fragments the TPO histogram for high-priced indices
(BANKNIFTY ~52k, SENSEX ~80k): on 252 real sessions it drove poor_high / poor_low
to ~1-2% of sessions (no discriminating information) and buying/selling tails to
~96-98% (saturated). Both branches of the excess-vs-poor-structure auction thesis
therefore carried zero signal. Per-symbol coarse ticks restore poor-high/low to a
sane ~11-17% and tails to ~53-70%.

This tick is used ONLY for BUILDING the value-area profile (TPO bucketing in both
the index tick-profile and the 30-minute bar TPO). It is deliberately separate
from the fine quote tick used to synthesize bid/ask / classify trade side — those
must stay at the real instrument tick so the order-flow proxy is not distorted.
"""

from __future__ import annotations

# Targets ~40-120 clean TPO levels per session against each index's typical range.
MP_TICK_BY_SYMBOL: dict[str, float] = {
    "NIFTY": 5.0,       # ~24.5k, range ~200-300 → ~50-60 levels
    "BANKNIFTY": 5.0,   # ~52k, range ~500-800 → review-validated (poor-h/l 11-17%)
    "FINNIFTY": 5.0,    # ~26k
    "MIDCPNIFTY": 2.5,  # ~13k
    "SENSEX": 20.0,     # ~80k → review-validated
    "CRUDEOIL": 5.0,    # MCX crude in the auction map (~6-7k)
}


def mp_tick_for(symbol: str, fallback: float = 0.5) -> float:
    """Per-symbol MP value tick, falling back to the caller's default."""
    try:
        fb = float(fallback)
    except (TypeError, ValueError):
        fb = 0.5
    return float(MP_TICK_BY_SYMBOL.get(str(symbol or "").upper(), fb))
