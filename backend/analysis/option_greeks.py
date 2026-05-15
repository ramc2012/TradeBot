"""Black-Scholes option pricing, Greeks, and IV solver.

Stage 4 of the F&O analytics design — internal Greeks engine. Returns the
standard suite (delta, gamma, theta, vega, rho) plus IV solved from a
market premium, plus IV rank / IV percentile over a historical IV series.

Two IV modes are supported, matching the design document:

  * ``GreeksMode.EXCHANGE``  uses a fixed 10% risk-free rate, matching what
    NSE applies when it displays IV on the option-chain page. Use this
    when you want numbers that line up with the exchange's own display.

  * ``GreeksMode.INTERNAL``  uses the rate supplied by the caller (MIBOR /
    T-bill / etc) — preferred for risk and strategy analytics.

The pricer is Black-Scholes for European options. NSE index options and
MCX commodity options are both European-style, so this is the right model.
Stock options on NSE are also European since Jan 2011.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional, Sequence


class GreeksMode(str, Enum):
    EXCHANGE = "exchange"  # NSE-style: 10% rate, matches exchange-displayed IV
    INTERNAL = "internal"  # caller-supplied rate, used for risk/strategy


_EXCHANGE_RISK_FREE_RATE = 0.10


@dataclass(frozen=True)
class GreeksResult:
    """Full Greeks for one option contract at one snapshot."""

    price: float
    intrinsic_value: float
    time_value: float
    delta: float
    gamma: float
    theta: float  # per-day theta
    vega: float  # per 1 vol point (i.e. 0.01)
    rho: float  # per 1% rate change
    iv: Optional[float]  # implied vol if solved, else None
    iv_mode: str
    moneyness: float  # spot / strike (>1 for ITM call, <1 for ITM put)
    probability_itm: float  # N(d2) for call, N(-d2) for put
    break_even: float  # strike +/- premium
    days_to_expiry: float

    def as_dict(self) -> dict[str, float | str | None]:
        return {
            "price": _round(self.price, 4),
            "intrinsic_value": _round(self.intrinsic_value, 4),
            "time_value": _round(self.time_value, 4),
            "delta": _round(self.delta, 4),
            "gamma": _round(self.gamma, 6),
            "theta": _round(self.theta, 4),
            "vega": _round(self.vega, 4),
            "rho": _round(self.rho, 4),
            "iv": _round(self.iv, 4) if self.iv is not None else None,
            "iv_mode": self.iv_mode,
            "moneyness": _round(self.moneyness, 4),
            "probability_itm": _round(self.probability_itm, 4),
            "break_even": _round(self.break_even, 2),
            "days_to_expiry": _round(self.days_to_expiry, 2),
        }


# ── Standard normal helpers ────────────────────────────────────────────────


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


# ── Black-Scholes pricer ───────────────────────────────────────────────────


def _bs_d1_d2(
    spot: float,
    strike: float,
    tte_years: float,
    rate: float,
    sigma: float,
    dividend_yield: float = 0.0,
) -> tuple[float, float]:
    if sigma <= 0 or tte_years <= 0:
        # Degenerate edges — pricer falls back to intrinsic; d1/d2 don't matter
        return float("nan"), float("nan")
    variance = sigma * math.sqrt(tte_years)
    d1 = (
        math.log(spot / strike) + (rate - dividend_yield + 0.5 * sigma * sigma) * tte_years
    ) / variance
    d2 = d1 - variance
    return d1, d2


def black_scholes_price(
    *,
    option_type: str,
    spot: float,
    strike: float,
    tte_years: float,
    rate: float,
    sigma: float,
    dividend_yield: float = 0.0,
) -> float:
    """Return the Black-Scholes price of a European option."""
    is_call = _normalize_option_type(option_type) == "CE"
    if tte_years <= 0 or sigma <= 0:
        # At/past expiry: price is purely intrinsic.
        if is_call:
            return max(0.0, spot - strike)
        return max(0.0, strike - spot)
    d1, d2 = _bs_d1_d2(spot, strike, tte_years, rate, sigma, dividend_yield)
    discounted_strike = strike * math.exp(-rate * tte_years)
    discounted_spot = spot * math.exp(-dividend_yield * tte_years)
    if is_call:
        return discounted_spot * _norm_cdf(d1) - discounted_strike * _norm_cdf(d2)
    return discounted_strike * _norm_cdf(-d2) - discounted_spot * _norm_cdf(-d1)


def _normalize_option_type(option_type: str) -> str:
    t = str(option_type or "").strip().upper()
    if t in {"C", "CE", "CALL"}:
        return "CE"
    if t in {"P", "PE", "PUT"}:
        return "PE"
    raise ValueError(f"Unknown option_type: {option_type!r}")


# ── IV solver (Brent's method) ─────────────────────────────────────────────


def implied_volatility(
    *,
    market_premium: float,
    option_type: str,
    spot: float,
    strike: float,
    tte_years: float,
    rate: float,
    dividend_yield: float = 0.0,
    tolerance: float = 1e-5,
    max_iterations: int = 100,
) -> Optional[float]:
    """Solve for the implied volatility that reproduces ``market_premium``.

    Uses Brent's method bracketed between [1e-4, 5.0] (i.e. 0.01% to 500%
    annualised vol). Returns None when the premium is outside the no-arbitrage
    bounds, when TTE has expired, or when the solver fails to converge.
    """
    is_call = _normalize_option_type(option_type) == "CE"
    if market_premium is None or market_premium <= 0 or tte_years <= 0 or spot <= 0 or strike <= 0:
        return None

    intrinsic = max(0.0, spot - strike) if is_call else max(0.0, strike - spot)
    if market_premium < intrinsic - tolerance:
        # Premium below intrinsic — arbitrage zone, IV undefined
        return None
    if market_premium - intrinsic < tolerance and intrinsic > 0:
        # Deep-ITM at intrinsic: IV is undefined (vega ~ 0)
        return None

    def objective(sigma: float) -> float:
        return (
            black_scholes_price(
                option_type=option_type,
                spot=spot,
                strike=strike,
                tte_years=tte_years,
                rate=rate,
                sigma=sigma,
                dividend_yield=dividend_yield,
            )
            - market_premium
        )

    low, high = 1e-4, 5.0
    f_low = objective(low)
    f_high = objective(high)
    if f_low * f_high > 0:
        # Premium not bracketed in the standard vol range — give up
        return None

    # Brent's method, kept simple to avoid scipy dependency
    a, b = low, high
    fa, fb = f_low, f_high
    c, fc = a, fa
    d = b - a
    e = d
    for _ in range(max_iterations):
        if fb * fc > 0:
            c, fc = a, fa
            d = b - a
            e = d
        if abs(fc) < abs(fb):
            a, b, c = b, c, b
            fa, fb, fc = fb, fc, fb
        tol1 = 2.0 * 1e-12 * abs(b) + 0.5 * tolerance
        m = 0.5 * (c - b)
        if abs(m) <= tol1 or fb == 0.0:
            return b
        if abs(e) >= tol1 and abs(fa) > abs(fb):
            s = fb / fa
            if a == c:
                p = 2.0 * m * s
                q = 1.0 - s
            else:
                q = fa / fc
                r = fb / fc
                p = s * (2.0 * m * q * (q - r) - (b - a) * (r - 1.0))
                q = (q - 1.0) * (r - 1.0) * (s - 1.0)
            if p > 0:
                q = -q
            p = abs(p)
            if 2.0 * p < min(3.0 * m * q - abs(tol1 * q), abs(e * q)):
                e = d
                d = p / q
            else:
                d = m
                e = d
        else:
            d = m
            e = d
        a, fa = b, fb
        if abs(d) > tol1:
            b += d
        else:
            b += math.copysign(tol1, m)
        fb = objective(b)
    return None  # didn't converge


# ── Full Greeks computation ────────────────────────────────────────────────


def compute_greeks(
    *,
    option_type: str,
    spot: float,
    strike: float,
    tte_years: float,
    market_premium: Optional[float] = None,
    sigma: Optional[float] = None,
    mode: GreeksMode = GreeksMode.INTERNAL,
    rate: Optional[float] = None,
    dividend_yield: float = 0.0,
) -> GreeksResult:
    """Compute the full Greeks suite for one European option.

    Provide either ``sigma`` directly OR ``market_premium`` to back-solve IV.
    ``mode=GreeksMode.EXCHANGE`` overrides ``rate`` with 10% to match NSE.
    """
    is_call = _normalize_option_type(option_type) == "CE"
    if mode == GreeksMode.EXCHANGE:
        effective_rate = _EXCHANGE_RISK_FREE_RATE
    elif rate is None:
        raise ValueError("rate is required for GreeksMode.INTERNAL")
    else:
        effective_rate = float(rate)

    iv: Optional[float] = sigma
    if iv is None:
        if market_premium is None:
            raise ValueError("Provide either sigma or market_premium")
        iv = implied_volatility(
            market_premium=market_premium,
            option_type=option_type,
            spot=spot,
            strike=strike,
            tte_years=tte_years,
            rate=effective_rate,
            dividend_yield=dividend_yield,
        )

    intrinsic = max(0.0, spot - strike) if is_call else max(0.0, strike - spot)

    if iv is None or iv <= 0 or tte_years <= 0:
        # Degenerate / unsolvable — Greeks are zero except delta which we
        # approximate from intrinsic direction.
        price = market_premium if market_premium is not None else intrinsic
        return GreeksResult(
            price=float(price),
            intrinsic_value=intrinsic,
            time_value=max(0.0, float(price) - intrinsic),
            delta=(1.0 if is_call and spot > strike else (-1.0 if not is_call and spot < strike else 0.0)),
            gamma=0.0,
            theta=0.0,
            vega=0.0,
            rho=0.0,
            iv=iv,
            iv_mode=mode.value,
            moneyness=(spot / strike if strike > 0 else 0.0),
            probability_itm=(1.0 if intrinsic > 0 else 0.0),
            break_even=(strike + (market_premium or 0)) if is_call else (strike - (market_premium or 0)),
            days_to_expiry=tte_years * 365.0,
        )

    d1, d2 = _bs_d1_d2(spot, strike, tte_years, effective_rate, iv, dividend_yield)
    n_d1 = _norm_cdf(d1)
    n_d2 = _norm_cdf(d2)
    npdf_d1 = _norm_pdf(d1)
    disc_strike = strike * math.exp(-effective_rate * tte_years)
    disc_spot = spot * math.exp(-dividend_yield * tte_years)
    sqrt_tte = math.sqrt(tte_years)

    if is_call:
        price = disc_spot * n_d1 - disc_strike * n_d2
        delta = math.exp(-dividend_yield * tte_years) * n_d1
        rho = strike * tte_years * math.exp(-effective_rate * tte_years) * n_d2 / 100.0
        # Annualised theta then per-day
        theta_annual = (
            -(disc_spot * npdf_d1 * iv) / (2.0 * sqrt_tte)
            - effective_rate * disc_strike * n_d2
            + dividend_yield * disc_spot * n_d1
        )
        probability_itm = n_d2
        break_even = strike + price
    else:
        price = disc_strike * _norm_cdf(-d2) - disc_spot * _norm_cdf(-d1)
        delta = -math.exp(-dividend_yield * tte_years) * _norm_cdf(-d1)
        rho = -strike * tte_years * math.exp(-effective_rate * tte_years) * _norm_cdf(-d2) / 100.0
        theta_annual = (
            -(disc_spot * npdf_d1 * iv) / (2.0 * sqrt_tte)
            + effective_rate * disc_strike * _norm_cdf(-d2)
            - dividend_yield * disc_spot * _norm_cdf(-d1)
        )
        probability_itm = _norm_cdf(-d2)
        break_even = strike - price

    gamma = (math.exp(-dividend_yield * tte_years) * npdf_d1) / (spot * iv * sqrt_tte)
    vega = disc_spot * npdf_d1 * sqrt_tte / 100.0  # per 1 vol point (0.01)
    theta_per_day = theta_annual / 365.0
    time_value = max(0.0, price - intrinsic)

    return GreeksResult(
        price=price,
        intrinsic_value=intrinsic,
        time_value=time_value,
        delta=delta,
        gamma=gamma,
        theta=theta_per_day,
        vega=vega,
        rho=rho,
        iv=iv,
        iv_mode=mode.value,
        moneyness=spot / strike,
        probability_itm=probability_itm,
        break_even=break_even,
        days_to_expiry=tte_years * 365.0,
    )


# ── IV Rank / Percentile (historical context) ──────────────────────────────


def iv_rank(current_iv: float, history: Sequence[float]) -> Optional[float]:
    """``(current - min) / (max - min) * 100`` over the lookback window.

    Returns None if there's no history or all values are equal.
    """
    series = [float(v) for v in history if v is not None and v > 0]
    if not series:
        return None
    lo = min(series)
    hi = max(series)
    if hi <= lo:
        return None
    return (float(current_iv) - lo) / (hi - lo) * 100.0


def iv_percentile(current_iv: float, history: Sequence[float]) -> Optional[float]:
    """Percent of historical observations below ``current_iv``."""
    series = [float(v) for v in history if v is not None and v > 0]
    if not series:
        return None
    below = sum(1 for v in series if v < float(current_iv))
    return below / len(series) * 100.0


# ── Internal helpers ──────────────────────────────────────────────────────


def _round(value: Optional[float], digits: int) -> Optional[float]:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), digits)


__all__ = [
    "GreeksMode",
    "GreeksResult",
    "black_scholes_price",
    "compute_greeks",
    "implied_volatility",
    "iv_percentile",
    "iv_rank",
]
