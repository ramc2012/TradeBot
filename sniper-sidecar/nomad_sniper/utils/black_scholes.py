"""Black-Scholes pricing primitives (no dividend). Shared by the option-economics gate
(`labels/profitability_gate.py`) and the m_breakeven calibrator (`labels/breakeven.py`)
so there is exactly one BS implementation in the codebase.

All times in YEARS, rates annualized, sigma annualized vol (e.g. 0.14 = 14%).
"""

from __future__ import annotations

import math

OptType = str  # "call" | "put"


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_price(S: float, K: float, T: float, r: float, sigma: float, opt: OptType) -> float:
    """European option price. Degrades to intrinsic value at/near expiry or zero vol."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(0.0, S - K) if opt == "call" else max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt == "call":
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)


def straddle_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """ATM-ish straddle = call + put at strike K."""
    return bs_price(S, K, T, r, sigma, "call") + bs_price(S, K, T, r, sigma, "put")


def implied_vol_from_straddle(
    straddle: float,
    S: float,
    K: float,
    T: float,
    r: float = 0.065,
    *,
    lo: float = 0.01,
    hi: float = 2.0,
    tol: float = 1e-4,
    max_iter: int = 100,
) -> float | None:
    """Invert a straddle price to an implied vol via bisection. None if out of bracket.

    Useful when you have a quoted ATM straddle but no IV column.
    """
    if straddle <= 0 or T <= 0 or S <= 0:
        return None
    f_lo = straddle_price(S, K, T, r, lo) - straddle
    f_hi = straddle_price(S, K, T, r, hi) - straddle
    if f_lo * f_hi > 0:
        return None  # target straddle not bracketed by [lo, hi]
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        f_mid = straddle_price(S, K, T, r, mid) - straddle
        if abs(f_mid) < tol:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)
