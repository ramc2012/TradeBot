"""Constant-maturity, constant-delta extraction — the fixed surface coordinates.

This is the module the whole substrate exists for.  A contract's implied-vol
history mixes three unrelated effects (the surface moved, the contract slid
along the skew, time ran out) and cannot be regressed, z-scored or coned.  A
FIXED coordinate — "30-day, 25-delta put" — is a clean series, and every vol
feature the lane uses is built on these and never on a contract.

Conventions, stated because they are the kind of thing that silently differs
between two implementations and makes two "30d ATM IV" series incomparable:

  * Maturity interpolation is LINEAR IN TOTAL VARIANCE (w = sigma^2 T) between
    the bracketing expiries.  This is the interpolation that preserves the
    calendar no-arbitrage condition; interpolating implied vol directly does
    not.
  * ATM means AT-THE-FORWARD, k = ln(K/F) = 0.  Not the nearest listed strike,
    which drifts with spot and re-introduces exactly the contamination this
    module removes.
  * Delta is the undiscounted Black-76 delta, N(d1) for calls, N(d1)-1 for
    puts.  A "25-delta put" is therefore the k where N(d1) = 0.75.
  * A point is REFUSED, not extrapolated, when the k it solves to falls
    outside the range of strikes the slice was actually fitted on.  The 25-delta
    wings of a thin far-dated expiry genuinely do not exist in this chain, and
    inventing them is how a skew feature turns into a duplicate of the level.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

import numpy as np
from scipy.optimize import brentq
from scipy.stats import norm

from directional_options.vol.svi import SVIParams
from directional_options.vol.surface import SurfaceSlice, SurfaceSnapshot

# Target tenors in calendar days.  7/14 cover the NIFTY weeklies, 30 is the
# reference point for every cone and variance-risk-premium comparison, 60
# gives the term-structure slope a far anchor.
DEFAULT_TENORS_DAYS: tuple[int, ...] = (7, 14, 30, 60)

# |delta| targets for the wings.
DEFAULT_DELTA_TARGETS: tuple[float, ...] = (0.25,)

_K_BRACKET = 0.60

# Tenor extrapolation band.  When no second expiry brackets the target, the
# target may still be served from the single observed slice — but only inside
# this ratio band around that slice's own maturity.  Stretching a 4-day slice
# to 60 days produced a POSITIVE risk reversal on an equity index and a 15-vol
# point butterfly, i.e. pure fabrication, which is what this band exists to
# stop.  Only one expiry per bar carries enough strikes to fit in this
# database, so in practice this is the binding constraint on the CM grid, and
# an empty grid point is the correct output rather than a defect.
EXTRAP_RATIO_LO = 0.6
EXTRAP_RATIO_HI = 1.6


@dataclass(frozen=True)
class ConstantMaturityPoint:
    underlying: str
    ts: datetime
    tenor_days: int
    tag: str                 # "atm" | "25d_call" | "25d_put"
    implied_vol: float | None
    total_variance: float | None
    log_moneyness: float | None
    strike: float | None
    forward: float | None
    interpolated: bool
    status: str              # "ok" | "extrapolated_k" | "no_bracket" | "no_root"
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.implied_vol is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "ts": self.ts.isoformat(),
            "tenor_days": self.tenor_days,
            "tag": self.tag,
            "implied_vol": self.implied_vol,
            "total_variance": self.total_variance,
            "log_moneyness": self.log_moneyness,
            "strike": self.strike,
            "forward": self.forward,
            "interpolated": self.interpolated,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass
class CMTenor:
    """All coordinates at one target tenor, plus the derived skew measures."""

    underlying: str
    ts: datetime
    tenor_days: int
    points: dict[str, ConstantMaturityPoint] = field(default_factory=dict)
    forward: float | None = None
    rr_25: float | None = None
    bf_25: float | None = None
    skew_slope: float | None = None      # d(sigma)/dk at the forward
    status: str = "ok"
    reason: str = ""

    def atm_iv(self) -> float | None:
        point = self.points.get("atm")
        return point.implied_vol if point and point.ok else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "ts": self.ts.isoformat(),
            "tenor_days": self.tenor_days,
            "forward": self.forward,
            "atm_iv": self.atm_iv(),
            "rr_25": self.rr_25,
            "bf_25": self.bf_25,
            "skew_slope": self.skew_slope,
            "status": self.status,
            "reason": self.reason,
            "points": {k: v.as_dict() for k, v in self.points.items()},
        }


@dataclass
class _Interpolator:
    """Total variance at a target tenor, blended from bracketing slices."""

    target_years: float
    near: SurfaceSlice
    far: SurfaceSlice | None
    weight: float          # 0 -> pure near, 1 -> pure far
    interpolated: bool
    k_min: float
    k_max: float

    def total_variance(self, k: float) -> float:
        w_near = float(self.near.fit.params.total_variance(k))
        if self.far is None or self.weight <= 0.0:
            # Flat-forward-variance extrapolation: hold the near slice's
            # variance RATE constant rather than its total variance, which is
            # what a desk does when the target sits inside the front expiry.
            scale = self.target_years / max(self.near.tenor_years, 1e-9)
            return max(w_near * scale, 1e-12)
        w_far = float(self.far.fit.params.total_variance(k))
        return max(w_near + (w_far - w_near) * self.weight, 1e-12)

    def d_total_variance(self, k: float) -> float:
        d_near = float(self.near.fit.params.d_total_variance(k))
        if self.far is None or self.weight <= 0.0:
            scale = self.target_years / max(self.near.tenor_years, 1e-9)
            return d_near * scale
        d_far = float(self.far.fit.params.d_total_variance(k))
        return d_near + (d_far - d_near) * self.weight

    def implied_vol(self, k: float) -> float:
        return math.sqrt(self.total_variance(k) / self.target_years)

    def dsigma_dk(self, k: float) -> float:
        w = self.total_variance(k)
        return self.d_total_variance(k) / (2.0 * math.sqrt(max(w, 1e-12) * self.target_years))

    @property
    def forward(self) -> float | None:
        if self.far is not None and self.weight > 0.5:
            return self.far.forward
        return self.near.forward


def _build_interpolator(slices: Sequence[SurfaceSlice], target_years: float) -> _Interpolator | None:
    usable = sorted([s for s in slices if s.ok], key=lambda s: s.tenor_years)
    if not usable:
        return None

    near: SurfaceSlice | None = None
    far: SurfaceSlice | None = None
    for candidate in usable:
        if candidate.tenor_years <= target_years:
            near = candidate
        elif far is None:
            far = candidate

    if near is None:
        # Target is shorter than every listed expiry — scale the front slice's
        # variance rate down instead of pretending we observed that tenor.
        near = usable[0]
        far = None
        weight = 0.0
        interpolated = False
    elif far is None:
        weight = 0.0
        interpolated = False
    else:
        span = far.tenor_years - near.tenor_years
        weight = 0.0 if span <= 0 else (target_years - near.tenor_years) / span
        interpolated = True

    if far is None:
        ratio = target_years / max(near.tenor_years, 1e-9)
        if ratio < EXTRAP_RATIO_LO or ratio > EXTRAP_RATIO_HI:
            return None

    contributors = [s for s in (near, far) if s is not None]
    k_min = max(s.fit.k_min for s in contributors if s.fit and s.fit.k_min is not None)
    k_max = min(s.fit.k_max for s in contributors if s.fit and s.fit.k_max is not None)

    return _Interpolator(
        target_years=target_years,
        near=near,
        far=far,
        weight=weight,
        interpolated=interpolated,
        k_min=k_min,
        k_max=k_max,
    )


def _solve_k_for_delta(interp: _Interpolator, target_d1: float) -> float | None:
    """Find k where the Black-76 d1 equals the target.

    d1(k) = (-k + w(k)/2) / sqrt(w(k)).  w depends on k through the smile, so
    this is a genuine root-find rather than a closed form; the function is
    monotone decreasing in k for any sane smile, which brentq handles cleanly.
    """

    def objective(k: float) -> float:
        w = interp.total_variance(k)
        root_w = math.sqrt(max(w, 1e-12))
        return (-k + 0.5 * w) / root_w - target_d1

    lo, hi = -_K_BRACKET, _K_BRACKET
    try:
        f_lo, f_hi = objective(lo), objective(hi)
        if f_lo * f_hi > 0:
            return None
        return float(brentq(objective, lo, hi, xtol=1e-10, maxiter=200))
    except (ValueError, RuntimeError):
        return None


def _point(
    underlying: str,
    ts: datetime,
    tenor_days: int,
    tag: str,
    interp: _Interpolator,
    k: float | None,
    reason_if_none: str,
) -> ConstantMaturityPoint:
    if k is None:
        return ConstantMaturityPoint(
            underlying=underlying, ts=ts, tenor_days=tenor_days, tag=tag,
            implied_vol=None, total_variance=None, log_moneyness=None, strike=None,
            forward=interp.forward, interpolated=interp.interpolated,
            status="no_root", reason=reason_if_none,
        )

    forward = interp.forward
    strike = forward * math.exp(k) if forward else None
    w = interp.total_variance(k)
    sigma = math.sqrt(w / interp.target_years)

    if k < interp.k_min - 1e-9 or k > interp.k_max + 1e-9:
        return ConstantMaturityPoint(
            underlying=underlying, ts=ts, tenor_days=tenor_days, tag=tag,
            implied_vol=None, total_variance=None, log_moneyness=k, strike=strike,
            forward=forward, interpolated=interp.interpolated,
            status="extrapolated_k",
            reason=(
                f"k={k:.4f} outside fitted range [{interp.k_min:.4f}, {interp.k_max:.4f}] "
                "— this wing was never quoted, refusing to invent it"
            ),
        )

    return ConstantMaturityPoint(
        underlying=underlying, ts=ts, tenor_days=tenor_days, tag=tag,
        implied_vol=sigma, total_variance=w, log_moneyness=k, strike=strike,
        forward=forward, interpolated=interp.interpolated, status="ok",
    )


def extract_cm_tenor(
    snapshot: SurfaceSnapshot,
    tenor_days: int,
    *,
    delta_target: float = 0.25,
) -> CMTenor:
    out = CMTenor(underlying=snapshot.underlying, ts=snapshot.ts, tenor_days=tenor_days)
    target_years = tenor_days / 365.0

    interp = _build_interpolator(snapshot.slices, target_years)
    if interp is None:
        usable = snapshot.usable_slices()
        out.status = "no_bracket"
        if usable:
            observed = ", ".join(f"{s.days_to_expiry:.1f}d" for s in usable)
            out.reason = (
                f"{tenor_days}d is outside the "
                f"[{EXTRAP_RATIO_LO:.1f}x, {EXTRAP_RATIO_HI:.1f}x] extrapolation band "
                f"of the only fitted maturity/maturities ({observed})"
            )
        else:
            out.reason = "no usable fitted slice at this bar"
        return out

    out.forward = interp.forward

    atm = _point(snapshot.underlying, snapshot.ts, tenor_days, "atm", interp, 0.0, "")
    out.points["atm"] = atm

    d1_call = float(norm.ppf(delta_target))            # N(d1) = 0.25 -> d1 < 0
    d1_put = float(norm.ppf(1.0 - delta_target))       # N(d1) = 0.75 -> d1 > 0

    k_call = _solve_k_for_delta(interp, d1_call)
    k_put = _solve_k_for_delta(interp, d1_put)

    tag_call = f"{int(round(delta_target * 100))}d_call"
    tag_put = f"{int(round(delta_target * 100))}d_put"
    out.points[tag_call] = _point(
        snapshot.underlying, snapshot.ts, tenor_days, tag_call, interp, k_call,
        "no k solves the target call delta on this smile",
    )
    out.points[tag_put] = _point(
        snapshot.underlying, snapshot.ts, tenor_days, tag_put, interp, k_put,
        "no k solves the target put delta on this smile",
    )

    call_pt, put_pt = out.points[tag_call], out.points[tag_put]
    if call_pt.ok and put_pt.ok:
        out.rr_25 = call_pt.implied_vol - put_pt.implied_vol
        if atm.ok:
            out.bf_25 = 0.5 * (call_pt.implied_vol + put_pt.implied_vol) - atm.implied_vol

    # ATM skew slope is always available from the fit even when the 25-delta
    # wings are not, which matters here: the wings genuinely go missing on the
    # thin expiries, and a level-only skew proxy is what previously turned a
    # skew feature into a duplicate of the IV level.
    if atm.ok:
        out.skew_slope = interp.dsigma_dk(0.0)

    if not atm.ok:
        out.status = atm.status
        out.reason = atm.reason
    return out


def extract_cm_series(
    snapshot: SurfaceSnapshot,
    *,
    tenors_days: Sequence[int] = DEFAULT_TENORS_DAYS,
    delta_target: float = 0.25,
) -> list[CMTenor]:
    if not snapshot.usable_slices():
        return []
    return [extract_cm_tenor(snapshot, int(t), delta_target=delta_target) for t in tenors_days]


def term_slope(tenors: Sequence[CMTenor], near_days: int = 7, far_days: int = 30) -> float | None:
    """Far ATM IV minus near ATM IV; positive is contango, negative backwardation."""
    by_tenor = {t.tenor_days: t for t in tenors}
    near, far = by_tenor.get(near_days), by_tenor.get(far_days)
    if not near or not far:
        return None
    a, b = near.atm_iv(), far.atm_iv()
    if a is None or b is None:
        return None
    return b - a


def sticky_moneyness_delta(
    bs_delta: float,
    vega: float,
    dsigma_dk: float,
    spot: float,
) -> float | None:
    """Skew-adjusted delta under a sticky log-moneyness smile assumption.

    This is a scenario derivative, NOT an empirically minimum-variance hedge.
    The latter requires the conditional dynamics of IV given spot moves;
    the cross-sectional smile slope alone does not identify those dynamics.
    Vega is per 1.00 sigma; d(sigma)/dS = -dsigma_dk / spot.
    """
    if spot is None or spot <= 0 or vega is None or dsigma_dk is None:
        return None
    return float(bs_delta + vega * (-dsigma_dk / spot))


# Compatibility for historical research imports. New callers and displays
# must use the explicit scenario label; legacy DB columns keep their names.
minimum_variance_delta = sticky_moneyness_delta


def front_coordinates(
    snapshot: SurfaceSnapshot,
    *,
    delta_target: float = 0.25,
) -> CMTenor | None:
    """Coordinates at the NATIVE maturity of the shortest fitted slice.

    The constant-maturity grid is the right long-run store, but it is empty
    most bars in this database because only one expiry per bar carries enough
    strikes to fit.  This returns the coordinates at the maturity that WAS
    observed — no tenor interpolation, no extrapolation — which is what the
    live lane can actually act on today.  `tenor_days` is the observed DTE
    rounded to the nearest day, so a consumer can still tell the coordinates
    apart across days.
    """
    usable = sorted(snapshot.usable_slices(), key=lambda s: s.tenor_years)
    if not usable:
        return None
    front = usable[0]
    return extract_cm_tenor(
        snapshot,
        int(round(front.days_to_expiry)),
        delta_target=delta_target,
    )
