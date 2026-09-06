"""Index volatility-surface substrate for the directional long-options lane.

Scope is deliberately narrow: NIFTY, BANKNIFTY and SENSEX only. Single-stock
option chains in this stack peak around 6.6 quoted contracts per symbol per
day, which is not enough points to identify a surface; the index chains carry
20-130 strikes per (expiry, bar) on the 30-minute grid and comfortably do.

The package exists because a *contract* IV series cannot be modelled — its
implied vol moves because the surface moved, because the contract slid along
the skew, and because time ran out, all at once.  Everything downstream
(cones, variance risk premium, skew z-scores, minimum-variance delta) needs a
FIXED coordinate on the surface instead, which is what `series` produces.
"""
from __future__ import annotations

from directional_options.vol.blackscholes import (
    IVResult,
    OptionGreeks,
    black76_greeks,
    black76_price,
    implied_vol,
)
from directional_options.vol.svi import SVIFit, SVIParams, calendar_arbitrage, fit_svi_slice
from directional_options.vol.surface import (
    SliceQuote,
    SurfaceSlice,
    build_surface_snapshot,
)
from directional_options.vol.series import ConstantMaturityPoint, extract_cm_series

__all__ = [
    "IVResult",
    "OptionGreeks",
    "black76_greeks",
    "black76_price",
    "implied_vol",
    "SVIFit",
    "SVIParams",
    "calendar_arbitrage",
    "fit_svi_slice",
    "SliceQuote",
    "SurfaceSlice",
    "build_surface_snapshot",
    "ConstantMaturityPoint",
    "extract_cm_series",
]
