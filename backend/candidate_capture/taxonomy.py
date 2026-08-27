"""Contract taxonomy — the explicit per-candidate tags every downstream model
conditions on.

Why a separate module
─────────────────────
`core.expiry_policy` answers "which contract does this instrument TRADE right
now" — a decision. `market_data.strike_ladder` answers "is this strike real" —
a validation. Neither produces a per-contract LABEL, and a learner that mixes
index weeklies with stock monthlies because nothing told them apart will learn
the average of two different processes. This module only labels; it decides
nothing and reads nothing.

Everything here is pure computation over values the caller already has, so it
is unit-testable without a DB, a broker or a clock.

EXPIRY CLASS IS DERIVED FROM THE LISTED SET, NEVER FROM A WEEKDAY
─────────────────────────────────────────────────────────────────
`INDEX_EXPIRY_WEEKDAY` exists and is correct today, but it is a model of an
exchange rule, and exchange rules change (NSE has already moved the index
expiry weekday once inside this repo's history). So `classify_expiry` takes the
actual listed expiries for the underlying and reads the structure off them:
the LAST listed expiry in a calendar month is that month's monthly, everything
earlier in the same month is a weekly. Nothing infers a date arithmetically.

When the listed set is unavailable the class is UNKNOWN and the reason is
recorded. It is never guessed — a mislabelled expiry_class silently pools two
populations, which is exactly the failure this module exists to prevent.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

IST = timezone(timedelta(hours=5, minutes=30))

DEFINITION_VERSION = "candidate_taxonomy_v1"

# ── underlying class ──────────────────────────────────────────────────────
UNDERLYING_INDEX = "INDEX"
UNDERLYING_STOCK = "STOCK"

# ── expiry class ──────────────────────────────────────────────────────────
EXPIRY_WEEKLY = "WEEKLY"
EXPIRY_MONTHLY = "MONTHLY"
EXPIRY_QUARTERLY = "QUARTERLY"
EXPIRY_LONG_DATED = "LONG_DATED"
EXPIRY_UNKNOWN = "UNKNOWN"

# A monthly expiry this many months out or further stops being "the monthly
# everyone trades" and becomes a quarterly / long-dated contract with its own
# liquidity and decay behaviour. Three is the structural point: NSE lists
# exactly three near monthlies at any time, and anything beyond them is a
# different product in practice.
NEAR_MONTHLY_COUNT = 3
QUARTER_END_MONTHS = frozenset({3, 6, 9, 12})

# ── moneyness ─────────────────────────────────────────────────────────────
MONEYNESS_ATM = "ATM"
MONEYNESS_NEAR_ITM = "NEAR_ITM"
MONEYNESS_ITM = "ITM"
MONEYNESS_DEEP_ITM = "DEEP_ITM"
MONEYNESS_NEAR_OTM = "NEAR_OTM"
MONEYNESS_OTM = "OTM"
MONEYNESS_DEEP_OTM = "DEEP_OTM"
MONEYNESS_UNKNOWN = "UNKNOWN"

# Band edges in LADDER STEPS, not rupees or percent. A step is the contract's
# own listed increment, so one definition covers NIFTY (50-wide) and ITC
# (2.5-wide) without a per-underlying table. Round structural points, not swept
# values: no forward return or P&L was consulted in choosing them.
ATM_BAND_STEPS = 0.5     # within half a rung of spot — the ATM rung itself
NEAR_BAND_STEPS = 2.0
DEEP_BAND_STEPS = 4.0

# ── liquidity ─────────────────────────────────────────────────────────────
LIQUIDITY_TOP = "TOP"
LIQUIDITY_HIGH = "HIGH"
LIQUIDITY_MID = "MID"
LIQUIDITY_LOW = "LOW"
LIQUIDITY_UNKNOWN = "UNKNOWN"

# Percentile cut points within the contract's OWN chain.
#
# DELIBERATELY RELATIVE, with a known cost. A percentile always puts ~20% of a
# chain in TOP even when the entire chain is illiquid, so `liquidity_bucket`
# ranks contracts against their peers and does NOT certify tradability. That is
# the right split of duties: the absolute question ("is this quotable at all")
# is answered by the spread / freshness envelope in `service.py`, which works
# on the raw quote, while this tag answers "where in this chain does it sit".
# Raw `oi` and `volume` are stored on every row so an absolute rule can be
# applied later without recapturing.
LIQUIDITY_TOP_PCT = 0.80
LIQUIDITY_HIGH_PCT = 0.50
LIQUIDITY_MID_PCT = 0.20


def _finite(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _as_date(value: date | str | None) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


@dataclass(frozen=True)
class ContractTaxonomy:
    exchange: str
    underlying: str
    underlying_class: str
    expiry: Optional[date]
    expiry_class: str
    expiry_class_reason: Optional[str]
    days_to_expiry: Optional[int]
    hours_to_expiry: Optional[float]
    expiry_day_flag: bool
    monthly_expiry_week_flag: bool
    option_type: str
    strike: Optional[float]
    moneyness: str
    moneyness_steps: Optional[float]
    liquidity_bucket: str
    liquidity_percentile: Optional[float]
    definition_version: str = DEFINITION_VERSION

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["expiry"] = self.expiry.isoformat() if self.expiry else None
        return out


def classify_underlying(underlying: str, index_symbols: Iterable[str]) -> str:
    """INDEX | STOCK, from the F&O index list the caller supplies.

    The list is a parameter rather than an import so this stays pure; callers
    pass `analysis.instruments.ALL_FO_INDICES`.
    """
    symbol = str(underlying or "").strip().upper()
    return UNDERLYING_INDEX if symbol in {str(s).upper() for s in index_symbols} else UNDERLYING_STOCK


def monthly_expiries(listed: Sequence[date]) -> list[date]:
    """The monthly expiry of each calendar month present in `listed`.

    The last listed expiry inside a month IS that month's monthly — this is the
    definition, read off the exchange's own listings rather than derived from a
    weekday rule that can change.
    """
    by_month: dict[tuple[int, int], date] = {}
    for value in listed:
        day = _as_date(value)
        if day is None:
            continue
        key = (day.year, day.month)
        current = by_month.get(key)
        if current is None or day > current:
            by_month[key] = day
    return sorted(by_month.values())


def classify_expiry(
    expiry: date | str | None,
    listed_expiries: Sequence[date] | None,
    *,
    today: Optional[date] = None,
) -> tuple[str, Optional[str]]:
    """(expiry_class, reason_when_unknown) for one expiry.

    `listed_expiries` is the exchange's own listed set for this underlying. An
    empty or missing set yields UNKNOWN with a reason — never a guess, because
    a wrong class silently pools weeklies with monthlies in training data.
    """
    target = _as_date(expiry)
    if target is None:
        return EXPIRY_UNKNOWN, "unparseable_expiry"

    listed = [d for d in (_as_date(v) for v in (listed_expiries or [])) if d is not None]
    if not listed:
        return EXPIRY_UNKNOWN, "no_listed_expiries_for_underlying"
    if target not in set(listed):
        # The contract is not in the listing we were given. Refusing to classify
        # is the honest answer: it may be a stale catalog, or a contract from a
        # different underlying entirely.
        return EXPIRY_UNKNOWN, "expiry_not_in_listed_set"

    monthlies = monthly_expiries(listed)
    if target not in set(monthlies):
        return EXPIRY_WEEKLY, None

    today = today or datetime.now(IST).date()
    forward_monthlies = [d for d in monthlies if d >= today]
    try:
        position = forward_monthlies.index(target)
    except ValueError:
        # A monthly that has already expired relative to `today`. Still a
        # monthly — the label describes the contract, not its remaining life.
        return EXPIRY_MONTHLY, None

    if position < NEAR_MONTHLY_COUNT:
        return EXPIRY_MONTHLY, None
    if target.month in QUARTER_END_MONTHS:
        return EXPIRY_QUARTERLY, None
    return EXPIRY_LONG_DATED, None


def expiry_horizon(
    expiry: date | str | None,
    *,
    now: Optional[datetime] = None,
    session_close_ist: tuple[int, int] = (15, 30),
) -> tuple[Optional[int], Optional[float], bool]:
    """(days_to_expiry, hours_to_expiry, expiry_day_flag).

    `hours_to_expiry` runs to the session CLOSE on expiry day (15:30 IST), not
    to midnight: an option stops trading at the close, and the eight hours after
    it are not decay the holder can act in. Negative once that instant passes.
    """
    target = _as_date(expiry)
    if target is None:
        return None, None, False
    now = (now or datetime.now(IST)).astimezone(IST)
    today = now.date()
    days = (target - today).days
    close_at = datetime.combine(
        target, datetime.min.time().replace(hour=session_close_ist[0], minute=session_close_ist[1]),
        tzinfo=IST,
    )
    hours = round((close_at - now).total_seconds() / 3600.0, 4)
    return days, hours, days == 0


def monthly_expiry_week(
    expiry: date | str | None,
    listed_expiries: Sequence[date] | None,
    *,
    today: Optional[date] = None,
) -> bool:
    """Is `today` inside the week that ends at the underlying's next monthly?

    Expiry-week behaviour (pinning, gamma, liquidity migration) is a property of
    the CALENDAR WEEK, so this is answered against the next monthly of the
    underlying — not against this contract's own expiry, which is why a weekly
    contract can and should carry the flag too.
    """
    listed = [d for d in (_as_date(v) for v in (listed_expiries or [])) if d is not None]
    if not listed:
        return False
    today = today or datetime.now(IST).date()
    upcoming = [d for d in monthly_expiries(listed) if d >= today]
    if not upcoming:
        return False
    next_monthly = upcoming[0]
    # Same ISO week ⇒ inside the monthly's expiry week.
    return next_monthly.isocalendar()[:2] == today.isocalendar()[:2]


def moneyness_steps(
    *,
    spot: Optional[float],
    strike: Optional[float],
    option_type: str,
    step: Optional[float],
) -> Optional[float]:
    """Signed distance from the money, in ladder steps. POSITIVE = in-the-money.

    A CE is in-the-money below spot, a PE above it, so the sign flips with the
    side. Expressing it in steps rather than rupees makes the number comparable
    across underlyings with different ladder increments.
    """
    spot_v = _finite(spot)
    strike_v = _finite(strike)
    step_v = _finite(step)
    if spot_v is None or strike_v is None or not step_v or step_v <= 0:
        return None
    side = str(option_type or "").strip().upper()
    if side == "CE":
        raw = (spot_v - strike_v) / step_v
    elif side == "PE":
        raw = (strike_v - spot_v) / step_v
    else:
        return None
    return round(raw, 6)


def classify_moneyness(steps: Optional[float]) -> str:
    """Band a signed step-distance into an ITM/ATM/OTM label."""
    if steps is None:
        return MONEYNESS_UNKNOWN
    magnitude = abs(steps)
    if magnitude <= ATM_BAND_STEPS:
        return MONEYNESS_ATM
    in_the_money = steps > 0
    if magnitude <= NEAR_BAND_STEPS:
        return MONEYNESS_NEAR_ITM if in_the_money else MONEYNESS_NEAR_OTM
    if magnitude <= DEEP_BAND_STEPS:
        return MONEYNESS_ITM if in_the_money else MONEYNESS_OTM
    return MONEYNESS_DEEP_ITM if in_the_money else MONEYNESS_DEEP_OTM


def _percentile_ranks(values: Sequence[Optional[float]]) -> list[Optional[float]]:
    """Fractional rank of each value among the non-null ones, ties averaged.

    Ties are averaged rather than broken by order so two contracts with
    identical OI never receive different liquidity percentiles because of the
    order the chain happened to arrive in.
    """
    usable = [(i, v) for i, v in enumerate(values) if _finite(v) is not None]
    out: list[Optional[float]] = [None] * len(values)
    n = len(usable)
    if n == 0:
        return out
    if n == 1:
        out[usable[0][0]] = 1.0
        return out
    ordered = sorted(usable, key=lambda pair: float(pair[1]))
    position = 0
    while position < n:
        end = position
        while end + 1 < n and float(ordered[end + 1][1]) == float(ordered[position][1]):
            end += 1
        # Average rank across the tied block, normalised to [0, 1].
        mean_rank = (position + end) / 2.0
        share = mean_rank / (n - 1)
        for index, _ in ordered[position:end + 1]:
            out[index] = round(share, 6)
        position = end + 1
    return out


def chain_liquidity_percentiles(
    rows: Sequence[dict[str, Any]],
) -> list[Optional[float]]:
    """Blend each contract's OI and volume rank within its own chain.

    OI and volume are different units and cannot be summed, so each is ranked
    separately and the two PERCENTILES are averaged — a unit-free combination.
    A contract with only one of the two is ranked on that one; a contract with
    neither gets None (and, downstream, LIQUIDITY_UNKNOWN — never LOW, which
    would be a claim the data does not support).
    """
    oi_ranks = _percentile_ranks([r.get("oi") for r in rows])
    vol_ranks = _percentile_ranks([r.get("volume") for r in rows])
    out: list[Optional[float]] = []
    for oi_rank, vol_rank in zip(oi_ranks, vol_ranks):
        present = [v for v in (oi_rank, vol_rank) if v is not None]
        out.append(round(sum(present) / len(present), 6) if present else None)
    return out


def classify_liquidity(percentile: Optional[float]) -> str:
    if percentile is None:
        return LIQUIDITY_UNKNOWN
    if percentile >= LIQUIDITY_TOP_PCT:
        return LIQUIDITY_TOP
    if percentile >= LIQUIDITY_HIGH_PCT:
        return LIQUIDITY_HIGH
    if percentile >= LIQUIDITY_MID_PCT:
        return LIQUIDITY_MID
    return LIQUIDITY_LOW


def classify_contract(
    *,
    exchange: str,
    underlying: str,
    index_symbols: Iterable[str],
    expiry: date | str | None,
    listed_expiries: Sequence[date] | None,
    option_type: str,
    strike: Optional[float],
    spot: Optional[float],
    ladder_step: Optional[float],
    liquidity_percentile: Optional[float] = None,
    now: Optional[datetime] = None,
) -> ContractTaxonomy:
    """Label one contract. Pure: no I/O, no globals, no clock unless defaulted."""
    now = (now or datetime.now(IST)).astimezone(IST)
    today = now.date()
    expiry_class, expiry_reason = classify_expiry(expiry, listed_expiries, today=today)
    days, hours, is_expiry_day = expiry_horizon(expiry, now=now)
    steps = moneyness_steps(
        spot=spot, strike=strike, option_type=option_type, step=ladder_step
    )
    return ContractTaxonomy(
        exchange=str(exchange or "").strip().upper(),
        underlying=str(underlying or "").strip().upper(),
        underlying_class=classify_underlying(underlying, index_symbols),
        expiry=_as_date(expiry),
        expiry_class=expiry_class,
        expiry_class_reason=expiry_reason,
        days_to_expiry=days,
        hours_to_expiry=hours,
        expiry_day_flag=is_expiry_day,
        monthly_expiry_week_flag=monthly_expiry_week(expiry, listed_expiries, today=today),
        option_type=str(option_type or "").strip().upper(),
        strike=_finite(strike),
        moneyness=classify_moneyness(steps),
        moneyness_steps=steps,
        liquidity_bucket=classify_liquidity(liquidity_percentile),
        liquidity_percentile=liquidity_percentile,
    )
