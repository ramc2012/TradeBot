"""Futures-curve analytics — near/mid/far prices, calendar spread, rollover.

Stage 5 of the F&O analytics design. Given a set of futures contracts on
the same underlying, this module returns the curve shape (contango /
backwardation / flat), per-pair calendar spread, basis vs spot, and an
OI-weighted rollover percentage between near and the next expiry.

Inputs are deliberately minimal — a list of contract dicts with at least
``expiry`` (date or ISO string), ``price`` (last traded price), and ideally
``open_interest`` plus ``volume``. The module sorts by expiry and returns
a structured payload that can be rendered directly on the futures-curve
dashboard from the design doc.

Both NSE index/stock futures and MCX commodity futures use the same shape
so this module is exchange-agnostic.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Optional


@dataclass(frozen=True)
class CurveContract:
    """One point on the futures curve."""

    contract_id: str
    expiry: str  # ISO date
    price: float
    open_interest: Optional[float] = None
    volume: Optional[float] = None
    days_to_expiry: Optional[int] = None


@dataclass(frozen=True)
class CalendarSpread:
    """Spread between two adjacent (or any pair of) futures contracts."""

    near_contract_id: str
    far_contract_id: str
    near_expiry: str
    far_expiry: str
    near_price: float
    far_price: float
    spread: float  # far - near
    spread_pct: float  # (far - near) / near * 100
    annualized_basis_pct: Optional[float]  # spread_pct * 365 / days_apart


@dataclass(frozen=True)
class CurveAnalysis:
    """Full curve view for one underlying."""

    underlying: str
    spot_price: Optional[float]
    points: list[dict]
    curve_shape: str  # "contango" | "backwardation" | "flat" | "mixed" | "insufficient"
    calendar_spreads: list[dict]
    basis: Optional[float]  # near_future - spot
    basis_pct: Optional[float]
    annualized_basis_pct: Optional[float]
    rollover_pct: Optional[float]  # far_oi / (near_oi + far_oi) * 100
    rollover_quality: Optional[str]  # "strong" | "weak" | "neutral" | None
    notes: list[str]

    def as_dict(self) -> dict:
        return {
            "underlying": self.underlying,
            "spot_price": _round(self.spot_price, 4),
            "points": self.points,
            "curve_shape": self.curve_shape,
            "calendar_spreads": self.calendar_spreads,
            "basis": _round(self.basis, 4),
            "basis_pct": _round(self.basis_pct, 4),
            "annualized_basis_pct": _round(self.annualized_basis_pct, 4),
            "rollover_pct": _round(self.rollover_pct, 2),
            "rollover_quality": self.rollover_quality,
            "notes": self.notes,
        }


def _parse_expiry(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except (TypeError, ValueError):
        return None


def _round(value: Optional[float], digits: int) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return round(f, digits)


def build_curve(
    *,
    underlying: str,
    contracts: Iterable[dict],
    spot_price: Optional[float] = None,
    today: Optional[date] = None,
) -> CurveAnalysis:
    """Build the curve view for one underlying.

    ``contracts`` items should have keys: ``contract_id``, ``expiry``,
    ``price`` (or ``last_price``/``close``); ``open_interest`` and ``volume``
    are optional but enable rollover/quality metrics.
    """
    reference_day = today or date.today()
    points: list[CurveContract] = []
    for raw in contracts:
        if not isinstance(raw, dict):
            continue
        expiry = _parse_expiry(raw.get("expiry"))
        if expiry is None or expiry < reference_day:
            continue  # drop expired contracts
        price = raw.get("price") or raw.get("last_price") or raw.get("close")
        try:
            price_f = float(price) if price is not None else None
        except (TypeError, ValueError):
            price_f = None
        if price_f is None or price_f <= 0:
            continue
        oi = raw.get("open_interest") or raw.get("oi")
        vol = raw.get("volume")
        try:
            oi_f = float(oi) if oi is not None else None
        except (TypeError, ValueError):
            oi_f = None
        try:
            vol_f = float(vol) if vol is not None else None
        except (TypeError, ValueError):
            vol_f = None
        points.append(
            CurveContract(
                contract_id=str(raw.get("contract_id") or raw.get("symbol") or ""),
                expiry=expiry.isoformat(),
                price=price_f,
                open_interest=oi_f,
                volume=vol_f,
                days_to_expiry=(expiry - reference_day).days,
            )
        )

    points.sort(key=lambda c: c.expiry)
    notes: list[str] = []

    if len(points) < 2:
        return CurveAnalysis(
            underlying=underlying,
            spot_price=spot_price,
            points=[asdict(p) for p in points],
            curve_shape="insufficient" if len(points) < 2 else "flat",
            calendar_spreads=[],
            basis=None,
            basis_pct=None,
            annualized_basis_pct=None,
            rollover_pct=None,
            rollover_quality=None,
            notes=["Need at least two non-expired futures to build a curve."] if len(points) < 2 else [],
        )

    # Pairwise calendar spreads (every adjacent pair)
    spreads: list[CalendarSpread] = []
    for near, far in zip(points, points[1:]):
        spread = far.price - near.price
        spread_pct = (spread / near.price * 100.0) if near.price else 0.0
        days_apart = max(
            1,
            (date.fromisoformat(far.expiry) - date.fromisoformat(near.expiry)).days,
        )
        annualized_pct = spread_pct * 365.0 / days_apart if days_apart > 0 else None
        spreads.append(
            CalendarSpread(
                near_contract_id=near.contract_id,
                far_contract_id=far.contract_id,
                near_expiry=near.expiry,
                far_expiry=far.expiry,
                near_price=near.price,
                far_price=far.price,
                spread=spread,
                spread_pct=spread_pct,
                annualized_basis_pct=annualized_pct,
            )
        )

    # Curve shape from the dominant direction of all pairs
    signs = [1 if s.spread > 0 else (-1 if s.spread < 0 else 0) for s in spreads]
    if all(s > 0 for s in signs):
        curve_shape = "contango"
    elif all(s < 0 for s in signs):
        curve_shape = "backwardation"
    elif all(s == 0 for s in signs):
        curve_shape = "flat"
    else:
        curve_shape = "mixed"

    # Basis vs spot (uses the nearest non-expired future)
    basis: Optional[float] = None
    basis_pct: Optional[float] = None
    annualized_basis_pct: Optional[float] = None
    if spot_price and spot_price > 0:
        near = points[0]
        basis = near.price - spot_price
        basis_pct = basis / spot_price * 100.0
        days = max(1, near.days_to_expiry or 1)
        annualized_basis_pct = basis_pct * 365.0 / days

    # Rollover % from OI migration between near and next
    rollover_pct: Optional[float] = None
    rollover_quality: Optional[str] = None
    if len(points) >= 2 and points[0].open_interest is not None and points[1].open_interest is not None:
        near_oi = points[0].open_interest or 0.0
        far_oi = points[1].open_interest or 0.0
        total = near_oi + far_oi
        if total > 0:
            rollover_pct = far_oi / total * 100.0
            # Heuristics from the design: strong roll is when far OI builds
            # quickly relative to near. Calibrated for typical NSE/MCX context.
            if rollover_pct >= 65.0:
                rollover_quality = "strong"
            elif rollover_pct <= 30.0:
                rollover_quality = "weak"
            else:
                rollover_quality = "neutral"

    # Annotate observations the trader needs context for
    if curve_shape == "backwardation":
        notes.append(
            "Near contract trades above far — backwardation; "
            "typical of supply tightness or near-term event risk."
        )
    if curve_shape == "contango" and annualized_basis_pct is not None and annualized_basis_pct > 6:
        notes.append(
            f"Steep contango ({annualized_basis_pct:.1f}% annualised) — "
            "carry costs are elevated; check funding rates."
        )
    if rollover_quality == "weak":
        notes.append(
            "Rollover is weak — most OI still in the near contract; "
            "expiry-week liquidity / squeeze risk if traders are slow to roll."
        )

    return CurveAnalysis(
        underlying=underlying,
        spot_price=spot_price,
        points=[asdict(p) for p in points],
        curve_shape=curve_shape,
        calendar_spreads=[asdict(s) for s in spreads],
        basis=basis,
        basis_pct=basis_pct,
        annualized_basis_pct=annualized_basis_pct,
        rollover_pct=rollover_pct,
        rollover_quality=rollover_quality,
        notes=notes,
    )


__all__ = [
    "CalendarSpread",
    "CurveAnalysis",
    "CurveContract",
    "build_curve",
]
