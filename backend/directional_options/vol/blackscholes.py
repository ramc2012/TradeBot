"""Black-76 pricing, greeks and a *refusing* implied-vol solver.

Why Black-76 and not Black-Scholes-on-spot: index options here are European
and we can read the forward straight off the chain via put-call parity, which
removes the need to assume a dividend yield or a repo rate.  Working in
forward space also makes the log-moneyness coordinate k = ln(K/F) exactly the
coordinate the SVI slice is fitted in, so nothing has to be re-based later.

The solver REFUSES rather than guessing.  A prior IV implementation in this
stack returned 9.3% for a true 8.0% because it happily inverted a price whose
vega was smaller than the exchange tick — at that point sigma is simply not
identified by the quote, and any number the root-finder returns is noise
dressed as data.  `implied_vol` returns `sigma=None` with a reason code in
that case, and callers must treat it as missing, not as zero.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from scipy.optimize import brentq
from scipy.stats import norm

OptionType = Literal["CE", "PE"]

# NSE/BSE index option tick.  Used as the price-resolution floor in the
# identification test: if moving sigma by one vol point does not move the
# theoretical price by at least one tick, the quote cannot pin sigma down.
DEFAULT_TICK_SIZE = 0.05

MIN_SIGMA = 1e-4
MAX_SIGMA = 5.0

IVStatus = Literal[
    "ok",
    "not_identified",
    "no_time_value",
    "arb_violation",
    "no_convergence",
    "bad_input",
]


def _norm_type(option_type: str) -> OptionType:
    value = (option_type or "").strip().upper()
    if value in {"CE", "C", "CALL"}:
        return "CE"
    if value in {"PE", "P", "PUT"}:
        return "PE"
    raise ValueError(f"unrecognised option type: {option_type!r}")


def _d1_d2(F: float, K: float, T: float, sigma: float) -> tuple[float, float]:
    vol_sqrt_t = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / vol_sqrt_t
    return d1, d1 - vol_sqrt_t


def black76_price(
    F: float,
    K: float,
    T: float,
    sigma: float,
    option_type: str = "CE",
    r: float = 0.0,
) -> float:
    """Undiscounted-forward Black-76 price, discounted at `r`.

    T is in years.  At or past expiry the payoff is the discounted intrinsic,
    which keeps the function continuous through the expiry boundary instead of
    exploding in the log.
    """
    kind = _norm_type(option_type)
    if F <= 0 or K <= 0:
        return 0.0
    discount = math.exp(-r * T) if T > 0 else 1.0
    if T <= 0 or sigma <= 0:
        intrinsic = (F - K) if kind == "CE" else (K - F)
        return discount * max(intrinsic, 0.0)
    d1, d2 = _d1_d2(F, K, T, sigma)
    if kind == "CE":
        return discount * (F * norm.cdf(d1) - K * norm.cdf(d2))
    return discount * (K * norm.cdf(-d2) - F * norm.cdf(-d1))


@dataclass(frozen=True)
class OptionGreeks:
    """Greeks in the units the rest of the lane expects.

    delta   — per 1.0 of underlying (spot delta; equals N(d1) under pure
              interest-rate carry, which is the case once F comes from parity)
    gamma   — per 1.0 of underlying, per 1.0 of underlying
    vega    — per 1.00 of sigma (multiply by 0.01 for "per vol point")
    theta   — per CALENDAR DAY, negative for a long option
    vanna   — d(delta)/d(sigma), per 1.00 of sigma
    charm   — d(delta)/d(t), per calendar day
    volga   — d(vega)/d(sigma), per 1.00 of sigma
    """

    delta: float
    gamma: float
    vega: float
    theta: float
    vanna: float = 0.0
    charm: float = 0.0
    volga: float = 0.0
    d1: float = 0.0
    d2: float = 0.0


_ONE_DAY_YEARS = 1.0 / 365.0


def black76_greeks(
    F: float,
    K: float,
    T: float,
    sigma: float,
    option_type: str = "CE",
    r: float = 0.0,
    spot: float | None = None,
) -> OptionGreeks:
    """Analytic first-order greeks; second-order ones by central difference.

    The second-order greeks (vanna, charm, volga) are differenced rather than
    written out analytically on purpose — they are used for exposure profiles
    and attribution where a sign error is far more costly than the handful of
    extra price evaluations that a difference costs.

    `spot` only rescales gamma/delta from forward space to spot space.  When
    omitted the forward is used, which is correct to within the discount
    factor and is what the exposure profiles want anyway.
    """
    kind = _norm_type(option_type)
    if F <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return OptionGreeks(delta=0.0, gamma=0.0, vega=0.0, theta=0.0)

    S = float(spot) if spot and spot > 0 else F
    discount = math.exp(-r * T)
    d1, d2 = _d1_d2(F, K, T, sigma)
    pdf_d1 = norm.pdf(d1)
    sqrt_t = math.sqrt(T)

    delta = norm.cdf(d1) if kind == "CE" else norm.cdf(d1) - 1.0
    gamma = pdf_d1 / (S * sigma * sqrt_t)
    vega = discount * F * pdf_d1 * sqrt_t

    # Theta per calendar day, by difference on T so the sign and the
    # day-count convention cannot drift apart.
    t_next = max(T - _ONE_DAY_YEARS, 1e-8)
    theta = black76_price(F, K, t_next, sigma, kind, r) - black76_price(F, K, T, sigma, kind, r)

    h_sigma = max(1e-4, sigma * 1e-3)

    def _delta_at(sig: float, tt: float) -> float:
        dd1, _ = _d1_d2(F, K, tt, sig)
        return norm.cdf(dd1) if kind == "CE" else norm.cdf(dd1) - 1.0

    vanna = (_delta_at(sigma + h_sigma, T) - _delta_at(sigma - h_sigma, T)) / (2.0 * h_sigma)
    charm = _delta_at(sigma, t_next) - _delta_at(sigma, T)
    volga = (
        black76_price(F, K, T, sigma + h_sigma, kind, r)
        - 2.0 * black76_price(F, K, T, sigma, kind, r)
        + black76_price(F, K, T, sigma - h_sigma, kind, r)
    ) / (h_sigma * h_sigma)

    return OptionGreeks(
        delta=delta,
        gamma=gamma,
        vega=vega,
        theta=theta,
        vanna=vanna,
        charm=charm,
        volga=volga,
        d1=d1,
        d2=d2,
    )


@dataclass(frozen=True)
class IVResult:
    """Outcome of an implied-vol inversion.

    `sigma` is None unless the inversion both converged AND the quote actually
    identifies sigma.  `sigma_raw` keeps whatever the root-finder returned so
    a diagnostic can look at it, but nothing downstream may consume it.
    """

    status: IVStatus
    sigma: float | None = None
    sigma_raw: float | None = None
    vega: float | None = None
    vega_per_vol_point: float | None = None
    time_value: float | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.sigma is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "sigma": self.sigma,
            "sigma_raw": self.sigma_raw,
            "vega_per_vol_point": self.vega_per_vol_point,
            "time_value": self.time_value,
            "reason": self.reason,
        }


def implied_vol(
    price: float,
    F: float,
    K: float,
    T: float,
    option_type: str = "CE",
    r: float = 0.0,
    tick_size: float = DEFAULT_TICK_SIZE,
    min_vega_ticks: float = 1.0,
) -> IVResult:
    """Invert Black-76 for sigma, refusing when the quote cannot identify it.

    Four refusals, in the order they are checked:

    1. `bad_input`     — non-positive price/forward/strike, or T <= 0.
    2. `arb_violation` — the price sits outside the no-arbitrage band, so no
                         sigma reproduces it.  Usually a stale or crossed quote.
    3. `no_time_value` — the price is intrinsic to within half a tick; sigma is
                         unbounded below and the inversion is meaningless.
    4. `not_identified`— a one-vol-point change in sigma moves the theoretical
                         price by less than `min_vega_ticks` ticks.  This is the
                         deep-wing / near-expiry case that produced the 9.3%-for-
                         8.0% error, and it is the reason this function exists.
    """
    if price is None or price <= 0 or F <= 0 or K <= 0 or T is None or T <= 0:
        return IVResult(status="bad_input", reason="non-positive price/forward/strike/tenor")

    kind = _norm_type(option_type)
    discount = math.exp(-r * T)
    intrinsic = discount * max((F - K) if kind == "CE" else (K - F), 0.0)
    upper = discount * (F if kind == "CE" else K)

    if price > upper * (1.0 + 1e-9):
        return IVResult(
            status="arb_violation",
            reason=f"price {price:.4f} above no-arb ceiling {upper:.4f}",
        )
    if price < intrinsic - tick_size / 2.0:
        return IVResult(
            status="arb_violation",
            reason=f"price {price:.4f} below discounted intrinsic {intrinsic:.4f}",
        )

    time_value = price - intrinsic
    if time_value <= tick_size / 2.0:
        return IVResult(
            status="no_time_value",
            time_value=time_value,
            reason=f"time value {time_value:.4f} within half a tick of intrinsic",
        )

    def _objective(sig: float) -> float:
        return black76_price(F, K, T, sig, kind, r) - price

    try:
        lo, hi = _objective(MIN_SIGMA), _objective(MAX_SIGMA)
        if lo > 0 or hi < 0:
            return IVResult(
                status="no_convergence",
                time_value=time_value,
                reason=f"price not bracketed on [{MIN_SIGMA}, {MAX_SIGMA}]",
            )
        sigma_raw = float(brentq(_objective, MIN_SIGMA, MAX_SIGMA, xtol=1e-8, maxiter=200))
    except (ValueError, RuntimeError) as exc:  # pragma: no cover - defensive
        return IVResult(status="no_convergence", time_value=time_value, reason=str(exc))

    greeks = black76_greeks(F, K, T, sigma_raw, kind, r)
    vega_per_point = greeks.vega * 0.01
    threshold = max(tick_size * float(min_vega_ticks), 1e-9)

    if vega_per_point < threshold:
        return IVResult(
            status="not_identified",
            sigma_raw=sigma_raw,
            vega=greeks.vega,
            vega_per_vol_point=vega_per_point,
            time_value=time_value,
            reason=(
                f"vega {vega_per_point:.4f}/vol-pt below {threshold:.4f} "
                "— sigma is not identified by this quote"
            ),
        )

    return IVResult(
        status="ok",
        sigma=sigma_raw,
        sigma_raw=sigma_raw,
        vega=greeks.vega,
        vega_per_vol_point=vega_per_point,
        time_value=time_value,
    )


def forward_from_parity(
    call_price: float,
    put_price: float,
    strike: float,
    T: float,
    r: float = 0.0,
) -> float | None:
    """F = K + e^{rT}(C - P).

    Reading the forward off the chain instead of assuming one is the single
    cheapest upgrade over a retail IV: it absorbs the dividend yield, the repo
    and any basis the market is actually pricing, none of which we can observe
    directly for an index.
    """
    if call_price is None or put_price is None or strike is None or strike <= 0:
        return None
    if T is None or T < 0:
        return None
    forward = strike + math.exp(r * T) * (float(call_price) - float(put_price))
    return forward if forward > 0 else None
