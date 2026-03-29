"""Black-Scholes and Binomial options Greeks calculator."""
from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy.stats import norm


@dataclass
class Greeks:
    delta: float
    gamma: float
    theta: float   # per calendar day
    vega: float    # per 1% IV move
    rho: float
    iv: float


def _d1(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return 0.0
    return (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))


def _d2(d1: float, sigma: float, T: float) -> float:
    return d1 - sigma * math.sqrt(T)


def bs_price(S: float, K: float, T: float, r: float, sigma: float, option_type: str = "CE") -> float:
    """Black-Scholes option price. T in years."""
    if T <= 0:
        if option_type == "CE":
            return max(0, S - K)
        return max(0, K - S)
    d1 = _d1(S, K, T, r, sigma)
    d2 = _d2(d1, sigma, T)
    if option_type == "CE":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def bs_greeks(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str = "CE",
    iv: Optional[float] = None,
) -> Greeks:
    """
    Calculate Black-Scholes Greeks for European options (NSE index options).
    S: spot price, K: strike, T: time to expiry (years),
    r: risk-free rate, sigma: implied volatility.
    """
    iv_used = iv if iv is not None else sigma
    if T <= 0 or sigma <= 0:
        return Greeks(delta=0, gamma=0, theta=0, vega=0, rho=0, iv=iv_used)

    d1 = _d1(S, K, T, r, sigma)
    d2 = _d2(d1, sigma, T)
    sqrt_T = math.sqrt(T)
    nd1 = norm.pdf(d1)

    if option_type == "CE":
        delta = norm.cdf(d1)
        rho = K * T * math.exp(-r * T) * norm.cdf(d2) / 100
        theta = (
            -(S * nd1 * sigma) / (2 * sqrt_T)
            - r * K * math.exp(-r * T) * norm.cdf(d2)
        ) / 365
    else:
        delta = norm.cdf(d1) - 1
        rho = -K * T * math.exp(-r * T) * norm.cdf(-d2) / 100
        theta = (
            -(S * nd1 * sigma) / (2 * sqrt_T)
            + r * K * math.exp(-r * T) * norm.cdf(-d2)
        ) / 365

    gamma = nd1 / (S * sigma * sqrt_T)
    vega = S * sqrt_T * nd1 / 100   # per 1% move in IV

    return Greeks(
        delta=round(delta, 6),
        gamma=round(gamma, 6),
        theta=round(theta, 4),
        vega=round(vega, 4),
        rho=round(rho, 4),
        iv=round(iv_used, 4),
    )


def implied_volatility(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    option_type: str = "CE",
    tol: float = 1e-6,
    max_iter: int = 100,
) -> float:
    """Newton-Raphson IV solver."""
    if T <= 0 or market_price <= 0:
        return 0.0

    intrinsic = max(0, S - K) if option_type == "CE" else max(0, K - S)
    if market_price <= intrinsic:
        return 0.0

    sigma = 0.20  # initial guess
    for _ in range(max_iter):
        price = bs_price(S, K, T, r, sigma, option_type)
        vega = S * math.sqrt(T) * norm.pdf(_d1(S, K, T, r, sigma))
        if abs(vega) < 1e-10:
            break
        diff = price - market_price
        if abs(diff) < tol:
            break
        sigma -= diff / vega
        sigma = max(0.001, min(sigma, 10.0))  # clamp

    return round(sigma, 6)


# ── Binomial for American options (NSE stock options) ────────────────────────

def binomial_price(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str = "CE",
    n: int = 200,
) -> float:
    """Cox-Ross-Rubinstein binomial tree for American options."""
    if T <= 0:
        if option_type == "CE":
            return max(0.0, S - K)
        return max(0.0, K - S)

    dt = T / n
    u = math.exp(sigma * math.sqrt(dt))
    d = 1.0 / u
    p = (math.exp(r * dt) - d) / (u - d)
    discount = math.exp(-r * dt)

    # Terminal stock prices
    prices = np.array([S * (u**j) * (d ** (n - j)) for j in range(n + 1)])

    # Terminal option values
    if option_type == "CE":
        values = np.maximum(prices - K, 0)
    else:
        values = np.maximum(K - prices, 0)

    # Backward induction with early exercise
    for i in range(n - 1, -1, -1):
        stock = np.array([S * (u**j) * (d ** (i - j)) for j in range(i + 1)])
        values = discount * (p * values[1:i+2] + (1 - p) * values[0:i+1])
        if option_type == "CE":
            exercise = np.maximum(stock - K, 0)
        else:
            exercise = np.maximum(K - stock, 0)
        values = np.maximum(values, exercise)

    return float(values[0])


@dataclass
class PortfolioGreeks:
    delta: float
    gamma: float
    theta: float
    vega: float


def aggregate_portfolio_greeks(positions: list) -> PortfolioGreeks:
    """
    positions: list of dicts with keys:
      symbol, option_type, qty, action, spot, strike, expiry_days, iv, r
    """
    total_delta = total_gamma = total_theta = total_vega = 0.0
    for pos in positions:
        T = pos.get("expiry_days", 1) / 365
        g = bs_greeks(
            S=pos.get("spot", 100),
            K=pos.get("strike", 100),
            T=T,
            r=pos.get("r", 0.065),
            sigma=pos.get("iv", 0.20),
            option_type=pos.get("option_type", "CE"),
        )
        sign = 1 if pos.get("action") == "BUY" else -1
        qty = pos.get("qty", 0) * sign
        total_delta += g.delta * qty
        total_gamma += g.gamma * qty
        total_theta += g.theta * qty
        total_vega += g.vega * qty

    return PortfolioGreeks(
        delta=round(total_delta, 4),
        gamma=round(total_gamma, 4),
        theta=round(total_theta, 4),
        vega=round(total_vega, 4),
    )
