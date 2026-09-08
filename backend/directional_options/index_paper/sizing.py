"""Position sizing derived from the option's own volatility, never from a constant.

Two failures in this stack's history are encoded directly in this module.

  * Sizing off FULL PREMIUM made every risk limit about 6.7x too loose, because
    a long option almost never travels to zero inside a holding period.  Risk
    here is the modelled ADVERSE MOVE over the intended horizon, and the full
    premium enters only as a separate outlay cap (the true worst case).

  * A stop floor stated as a multiple of ATR, combined with a rupee risk cap,
    left an EMPTY FEASIBLE SET on COPPER — the strong setup fired 34 times and
    executed zero, silently.  Here, "the risk budget does not buy one lot" is a
    first-class REFUSAL with the shortfall attached, so it lands in the
    rejection journal instead of looking like an absence of signal.

The adverse move is built from the greeks and the surface rather than a price
fraction: a delta term driven by the index's own implied vol over the horizon,
a vega term driven by a prior on how much implied vol itself moves, and the
theta the position will actually pay while it waits.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# 252 trading sessions a year.
SESSIONS_PER_YEAR = 252.0
CALENDAR_DAYS_PER_YEAR = 365.0

# Prior: one session's change in an index's implied vol has a standard
# deviation of roughly 5% of the IV LEVEL (a 12% IV moves ~0.6 vol points a
# day).  Relative rather than absolute so it scales with the regime.
DAILY_IV_RELATIVE_MOVE = 0.05


@dataclass
class SizingDecision:
    approved: bool
    lots: int
    quantity: int
    reason_code: str
    reason: str = ""
    risk_budget: float | None = None
    adverse_move_per_unit: float | None = None
    risk_per_lot: float | None = None
    premium_outlay: float | None = None
    stop_premium: float | None = None
    target_premium: float | None = None
    horizon_sessions: float | None = None
    components: dict[str, float] = field(default_factory=dict)
    caps: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "lots": self.lots,
            "quantity": self.quantity,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "risk_budget": self.risk_budget,
            "adverse_move_per_unit": self.adverse_move_per_unit,
            "risk_per_lot": self.risk_per_lot,
            "premium_outlay": self.premium_outlay,
            "stop_premium": self.stop_premium,
            "target_premium": self.target_premium,
            "horizon_sessions": self.horizon_sessions,
            "components": self.components,
            "caps": self.caps,
        }


def adverse_premium_move(
    *,
    spot: float,
    atm_iv: float,
    delta: float,
    vega: float,
    theta_per_day: float,
    sessions: float,
    calendar_days: float,
    stop_sigmas: float = 1.0,
    daily_iv_relative_move: float = DAILY_IV_RELATIVE_MOVE,
) -> tuple[float, dict[str, float]]:
    """How far the premium can move against a long option over the horizon.

    Delta and vega terms are combined in quadrature because a spot move and an
    implied-vol move are not the same shock; theta is ADDED rather than
    combined, because it is not a shock at all — it is a certainty the position
    pays for waiting, and it is the reason long premium is structurally hard.

    TWO CLOCKS, and they are not interchangeable. Diffusion runs on TRADING
    SESSIONS: the index does not move over a weekend, so a 3-session move is
    sqrt(3/252) of annual vol. Decay runs on CALENDAR DAYS: the option does
    decay over that weekend. The first version charged theta per trading
    session, which over a 5-session hold understated seven calendar days of
    decay as five — a 40% error on precisely the term that makes this strategy
    hard.

    Gamma is deliberately omitted. For a long option it cushions the adverse
    side, so leaving it out makes the estimate conservative in the direction
    that matters.

    Units: `vega` is per 1.00 of sigma, matching `OptionGreeks.vega`, and the
    implied-vol move is in the same absolute units (0.006 = 0.6 vol points).
    Nothing here is expressed in percent, so there is no place for a
    percent-versus-fraction factor of 100 to hide.
    """
    horizon_years = sessions / SESSIONS_PER_YEAR
    spot_move = abs(spot) * max(atm_iv, 0.0) * math.sqrt(max(horizon_years, 0.0))
    iv_move = daily_iv_relative_move * max(atm_iv, 0.0) * math.sqrt(max(sessions, 0.0))

    delta_term = abs(delta) * spot_move
    vega_term = abs(vega) * iv_move
    theta_term = abs(theta_per_day) * max(calendar_days, 0.0)

    shock = math.hypot(delta_term, vega_term) * max(stop_sigmas, 0.0)
    total = shock + theta_term
    return total, {
        "spot_move": spot_move,
        "iv_move": iv_move,
        "delta_term": delta_term,
        "vega_term": vega_term,
        "theta_term": theta_term,
        "shock": shock,
        "sessions": sessions,
        "calendar_days": calendar_days,
    }


def size_position(
    *,
    capital: float,
    premium: float,
    lot_size: int,
    spot: float,
    atm_iv: float,
    delta: float,
    vega: float,
    theta_per_day: float,
    horizon_sessions: int,
    horizon_calendar_days: float | None = None,
    risk_fraction: float = 0.004,
    risk_multiplier: float = 1.0,
    stop_sigmas: float = 1.0,
    reward_multiple: float = 2.0,
    max_premium_fraction: float = 0.02,
    max_lots: int = 20,
    min_stop_ticks: float = 4.0,
    tick_size: float = 0.05,
) -> SizingDecision:
    """Lots to buy, or an explicit refusal with the shortfall attached.

    `horizon_sessions` is the intended hold in TRADING SESSIONS and
    `horizon_calendar_days` the calendar span it covers; pass both, because
    diffusion and decay run on different clocks.

    `risk_multiplier` is where a vol-regime view enters — a variance risk
    premium that says premium is expensive shrinks the budget rather than
    vetoing the trade, which is the same "size, never veto" discipline the
    existing IV sizing factor uses.

    Two independent caps apply and the smaller wins:
      * RISK cap    — budget / (adverse move x lot size)
      * OUTLAY cap  — a ceiling on premium at risk, since the true worst case
                      for a long option is the whole premium.
    """
    if premium <= 0 or lot_size <= 0 or capital <= 0:
        return SizingDecision(
            approved=False, lots=0, quantity=0,
            reason_code="bad_input",
            reason=f"premium={premium}, lot_size={lot_size}, capital={capital}",
        )

    sessions = float(max(horizon_sessions, 0))
    # Fall back to the 7/5 calendar ratio only when the caller has no real
    # calendar; `horizon.calendar_days_for_sessions` gives the true span and
    # charges a long weekend at its actual cost.
    calendar_days = (
        float(horizon_calendar_days)
        if horizon_calendar_days is not None
        else sessions * CALENDAR_DAYS_PER_YEAR / SESSIONS_PER_YEAR
    )
    adverse, components = adverse_premium_move(
        spot=spot, atm_iv=atm_iv, delta=delta, vega=vega,
        theta_per_day=theta_per_day, sessions=sessions,
        calendar_days=calendar_days, stop_sigmas=stop_sigmas,
    )

    # The stop cannot sit below the tick grid, and cannot sit below zero — a
    # long option's floor is worthlessness, not a negative price.
    adverse = max(adverse, min_stop_ticks * tick_size)
    adverse = min(adverse, premium)

    risk_budget = capital * risk_fraction * max(risk_multiplier, 0.0)
    risk_per_lot = adverse * lot_size
    if risk_per_lot <= 0:
        return SizingDecision(
            approved=False, lots=0, quantity=0,
            reason_code="degenerate_risk",
            reason="modelled adverse move is zero; refusing to size on it",
            components=components, horizon_sessions=sessions,
        )

    lots_by_risk = int(math.floor(risk_budget / risk_per_lot))
    outlay_cap = capital * max_premium_fraction
    lots_by_outlay = int(math.floor(outlay_cap / (premium * lot_size)))
    lots = max(min(lots_by_risk, lots_by_outlay, max_lots), 0)

    caps = {
        "lots_by_risk": lots_by_risk,
        "lots_by_outlay": lots_by_outlay,
        "max_lots": max_lots,
        "binding": _binding_cap(lots_by_risk, lots_by_outlay, max_lots),
        "outlay_cap": outlay_cap,
    }

    stop_premium = max(premium - adverse, 0.0)
    target_premium = premium + adverse * max(reward_multiple, 0.0)

    if lots < 1:
        shortfall_risk = risk_per_lot - risk_budget
        shortfall_outlay = (premium * lot_size) - outlay_cap
        return SizingDecision(
            approved=False, lots=0, quantity=0,
            reason_code="below_one_lot",
            reason=(
                f"one lot needs risk {risk_per_lot:,.0f} against a budget of "
                f"{risk_budget:,.0f} (short {max(shortfall_risk, 0):,.0f}) and outlay "
                f"{premium * lot_size:,.0f} against a cap of {outlay_cap:,.0f} "
                f"(short {max(shortfall_outlay, 0):,.0f}); the feasible set is empty"
            ),
            risk_budget=risk_budget,
            adverse_move_per_unit=adverse,
            risk_per_lot=risk_per_lot,
            premium_outlay=premium * lot_size,
            stop_premium=stop_premium,
            target_premium=target_premium,
            horizon_sessions=sessions,
            components=components,
            caps=caps,
        )

    quantity = lots * lot_size
    return SizingDecision(
        approved=True,
        lots=lots,
        quantity=quantity,
        reason_code="sized",
        risk_budget=risk_budget,
        adverse_move_per_unit=adverse,
        risk_per_lot=risk_per_lot,
        premium_outlay=premium * quantity,
        stop_premium=stop_premium,
        target_premium=target_premium,
        horizon_sessions=sessions,
        components=components,
        caps=caps,
    )


def _binding_cap(by_risk: int, by_outlay: int, max_lots: int) -> str:
    smallest = min(by_risk, by_outlay, max_lots)
    if smallest == by_risk:
        return "risk"
    if smallest == by_outlay:
        return "outlay"
    return "max_lots"


def vrp_risk_multiplier(
    vrp_spread: float | None,
    *,
    rich_threshold: float = 0.02,
    floor: float = 0.35,
) -> tuple[float, str]:
    """Shrink the risk budget when implied vol is rich against realised.

    A long-premium lane pays the variance risk premium.  When implied sits
    well above trailing realised, every long option is buying an overpriced
    lottery ticket and the correct response is a smaller ticket, not a veto —
    the direction may still be right, and vetoing removes the observation that
    would have taught the lane something.

    Returns (multiplier, explanation).  `rich_threshold` is in vol units, so
    0.02 is two vol points of premium richness.
    """
    if vrp_spread is None:
        return 1.0, "no variance risk premium available; risk budget unchanged"
    if vrp_spread <= 0:
        return 1.0, (
            f"implied is at or below realised ({vrp_spread * 100:+.2f} vol points); "
            "long premium is not paying a carry penalty"
        )
    scaled = max(1.0 - vrp_spread / max(rich_threshold, 1e-9) * (1.0 - floor), floor)
    return scaled, (
        f"implied is {vrp_spread * 100:+.2f} vol points above realised; "
        f"risk budget scaled to {scaled:.2f}x"
    )
