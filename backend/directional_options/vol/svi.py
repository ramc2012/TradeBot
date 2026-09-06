"""Raw-SVI slice calibration with the two static no-arbitrage checks.

Parameterisation (Gatheral): in TOTAL implied variance w = sigma^2 * T against
log-moneyness k = ln(K / F),

    w(k) = a + b * ( rho * (k - m) + sqrt((k - m)^2 + s^2) )

Fitting in total variance rather than in implied vol is what makes the
calendar check a simple pointwise comparison between slices, and it is the
space the constant-maturity interpolation in `series` works in too.

Two arbitrage conditions are checked, not assumed:

  * BUTTERFLY — Gatheral's g(k) >= 0 over the fitted range.  g < 0 means the
    slice implies a negative risk-neutral density, i.e. the fit would price a
    butterfly at a negative value.  A slice that fails this must not be used
    to extract a density, and we refuse rather than smooth it away.

  * CALENDAR — total variance non-decreasing in maturity at every k.  A
    violation across two expiries usually means one of the two slices was
    fitted to a stale snapshot, so it is as much a data-quality alarm as a
    modelling one.

The residual set is a first-class output.  A strike that will not fit is
almost always a stale or crossed quote, and the quarantine list it produces
has repeatedly been worth more on a given day than the fit itself.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
from scipy.optimize import least_squares

# A five-parameter model needs meaningfully more than five points before the
# fit means anything.  Index chains here carry 20-130 strikes per bar, so this
# floor only ever bites on the thin far-dated expiries.
MIN_POINTS_FOR_FIT = 6

# Butterfly check grid density over the observed k-range.
_ARB_GRID = 121


@dataclass(frozen=True)
class SVIParams:
    a: float
    b: float
    rho: float
    m: float
    s: float

    def total_variance(self, k: float | np.ndarray) -> float | np.ndarray:
        y = np.asarray(k, dtype=float) - self.m
        return self.a + self.b * (self.rho * y + np.sqrt(y * y + self.s * self.s))

    def d_total_variance(self, k: float | np.ndarray) -> np.ndarray:
        y = np.asarray(k, dtype=float) - self.m
        root = np.sqrt(y * y + self.s * self.s)
        return self.b * (self.rho + y / root)

    def d2_total_variance(self, k: float | np.ndarray) -> np.ndarray:
        y = np.asarray(k, dtype=float) - self.m
        root = np.sqrt(y * y + self.s * self.s)
        return self.b * self.s * self.s / (root ** 3)

    def implied_vol(self, k: float | np.ndarray, T: float) -> np.ndarray:
        if T <= 0:
            return np.zeros_like(np.asarray(k, dtype=float))
        w = np.maximum(np.asarray(self.total_variance(k), dtype=float), 1e-12)
        return np.sqrt(w / T)

    def as_dict(self) -> dict[str, float]:
        return {"a": self.a, "b": self.b, "rho": self.rho, "m": self.m, "s": self.s}


@dataclass
class SVIFit:
    status: str  # "ok" | "insufficient" | "no_convergence" | "butterfly_arb"
    params: SVIParams | None = None
    n_points: int = 0
    n_used: int = 0
    rmse_vol: float | None = None          # RMSE in implied-vol points (0.01 = 1 vol pt)
    max_abs_resid_vol: float | None = None
    butterfly_ok: bool | None = None
    min_g: float | None = None
    k_min: float | None = None
    k_max: float | None = None
    quarantined: list[dict[str, object]] = field(default_factory=list)
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.params is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "params": self.params.as_dict() if self.params else None,
            "n_points": self.n_points,
            "n_used": self.n_used,
            "rmse_vol": self.rmse_vol,
            "max_abs_resid_vol": self.max_abs_resid_vol,
            "butterfly_ok": self.butterfly_ok,
            "min_g": self.min_g,
            "k_min": self.k_min,
            "k_max": self.k_max,
            "quarantined": self.quarantined,
            "reason": self.reason,
        }


def gatheral_g(params: SVIParams, k: np.ndarray) -> np.ndarray:
    """Gatheral's butterfly function; g(k) >= 0 <=> non-negative density."""
    w = np.maximum(np.asarray(params.total_variance(k), dtype=float), 1e-12)
    wp = np.asarray(params.d_total_variance(k), dtype=float)
    wpp = np.asarray(params.d2_total_variance(k), dtype=float)
    term1 = (1.0 - k * wp / (2.0 * w)) ** 2
    term2 = (wp * wp / 4.0) * (1.0 / w + 0.25)
    return term1 - term2 + wpp / 2.0


def _wing_bound(T: float) -> float:
    """Roger Lee wing bound: b (1 + |rho|) <= 4 / T is necessary for no arb."""
    return 4.0 / max(T, 1e-6)


def fit_svi_slice(
    log_moneyness: Sequence[float],
    implied_vols: Sequence[float],
    T: float,
    weights: Sequence[float] | None = None,
    strikes: Sequence[float] | None = None,
    outlier_vol_threshold: float = 0.06,
    max_outlier_fraction: float = 0.25,
) -> SVIFit:
    """Fit one expiry slice, then quarantine and refit gross outliers.

    `weights` should be vega (or any positive confidence weight); residuals are
    measured in implied-vol points so the fit is not dominated by deep wings
    whose total variance is large but whose quote is meaningless.

    The refit pass drops points more than `outlier_vol_threshold` (default 6
    vol points) from the first fit, but never more than `max_outlier_fraction`
    of the slice — if a quarter of the chain disagrees with the model it is the
    model that is wrong, not the chain, and the fit is reported as-is.
    """
    k = np.asarray(list(log_moneyness), dtype=float)
    iv = np.asarray(list(implied_vols), dtype=float)
    strike_arr = np.asarray(list(strikes), dtype=float) if strikes is not None else np.full_like(k, np.nan)
    w_obs = iv * iv * T

    finite = np.isfinite(k) & np.isfinite(iv) & (iv > 0)
    k, iv, w_obs, strike_arr = k[finite], iv[finite], w_obs[finite], strike_arr[finite]

    if weights is not None:
        wt = np.asarray(list(weights), dtype=float)[: len(finite)]
        wt = wt[finite] if len(wt) == len(finite) else np.ones_like(k)
    else:
        wt = np.ones_like(k)
    wt = np.where(np.isfinite(wt) & (wt > 0), wt, 0.0)
    if not np.any(wt > 0):
        wt = np.ones_like(k)
    wt = wt / np.max(wt)

    n = int(k.size)
    if n < MIN_POINTS_FOR_FIT or T <= 0:
        return SVIFit(
            status="insufficient",
            n_points=n,
            reason=f"{n} usable points (need {MIN_POINTS_FOR_FIT}) at T={T:.5f}",
        )

    fit = _fit_once(k, iv, w_obs, wt, T)
    if not fit.ok:
        fit.n_points = n
        return fit

    resid = np.abs(fit.params.implied_vol(k, T) - iv)
    outliers = resid > outlier_vol_threshold
    n_out = int(outliers.sum())
    quarantined: list[dict[str, object]] = []

    if 0 < n_out <= int(max_outlier_fraction * n) and (n - n_out) >= MIN_POINTS_FOR_FIT:
        for idx in np.flatnonzero(outliers):
            quarantined.append(
                {
                    "strike": None if not np.isfinite(strike_arr[idx]) else float(strike_arr[idx]),
                    "log_moneyness": float(k[idx]),
                    "observed_iv": float(iv[idx]),
                    "fitted_iv": float(fit.params.implied_vol(k[idx], T)),
                    "abs_resid_vol": float(resid[idx]),
                    "reason": "residual above outlier threshold",
                }
            )
        keep = ~outliers
        refit = _fit_once(k[keep], iv[keep], w_obs[keep], wt[keep], T)
        if refit.ok:
            fit = refit

    fit.n_points = n
    fit.n_used = n - len(quarantined)
    fit.quarantined = quarantined
    fit.k_min = float(np.min(k))
    fit.k_max = float(np.max(k))

    grid = np.linspace(fit.k_min, fit.k_max, _ARB_GRID)
    g = gatheral_g(fit.params, grid)
    fit.min_g = float(np.min(g))
    fit.butterfly_ok = bool(fit.min_g >= -1e-8)
    if not fit.butterfly_ok:
        fit.status = "butterfly_arb"
        fit.reason = f"min g(k) = {fit.min_g:.6f} < 0 — fitted slice implies negative density"

    return fit


def _fit_once(
    k: np.ndarray,
    iv: np.ndarray,
    w_obs: np.ndarray,
    wt: np.ndarray,
    T: float,
) -> SVIFit:
    sqrt_wt = np.sqrt(np.maximum(wt, 1e-6))

    def residuals(theta: np.ndarray) -> np.ndarray:
        params = SVIParams(*[float(x) for x in theta])
        model_w = np.maximum(np.asarray(params.total_variance(k), dtype=float), 1e-12)
        model_iv = np.sqrt(model_w / T)
        return sqrt_wt * (model_iv - iv)

    w_atm = float(np.interp(0.0, k, w_obs)) if k.size else float(np.median(w_obs))
    spread = float(np.max(k) - np.min(k)) or 0.1

    # Multi-start: equity skew is normally negative-rho, but a stressed or
    # post-event slice can flip, and a single start lands in the wrong basin.
    starts = [
        (max(w_atm * 0.5, 1e-6), 0.10, -0.60, 0.0, 0.10),
        (max(w_atm * 0.8, 1e-6), 0.04, -0.30, 0.0, 0.05),
        (max(w_atm * 0.5, 1e-6), 0.20, 0.20, 0.0, 0.20),
        (max(w_atm * 0.3, 1e-6), 0.08, -0.85, -0.02, 0.03),
    ]
    wing = _wing_bound(T)
    lower = [-2.0, 0.0, -0.999, -3.0 * spread - 0.5, 1e-4]
    upper = [5.0, wing, 0.999, 3.0 * spread + 0.5, 2.0]

    best = None
    best_cost = math.inf
    for start in starts:
        theta0 = [min(max(v, lo), hi) for v, lo, hi in zip(start, lower, upper)]
        try:
            sol = least_squares(
                residuals,
                theta0,
                bounds=(lower, upper),
                method="trf",
                max_nfev=2000,
                xtol=1e-12,
                ftol=1e-12,
            )
        except Exception:  # pragma: no cover - defensive around solver internals
            continue
        if sol.cost < best_cost:
            best_cost, best = sol.cost, sol

    if best is None:
        return SVIFit(status="no_convergence", reason="least_squares failed from every start")

    params = SVIParams(*[float(x) for x in best.x])

    # w(k) >= 0 everywhere requires a + b s sqrt(1 - rho^2) >= 0.
    floor = params.a + params.b * params.s * math.sqrt(max(1.0 - params.rho ** 2, 0.0))
    if floor < -1e-8:
        return SVIFit(
            status="no_convergence",
            params=params,
            reason=f"fitted slice admits negative total variance (floor {floor:.6f})",
        )

    model_iv = params.implied_vol(k, T)
    resid = model_iv - iv
    return SVIFit(
        status="ok",
        params=params,
        n_points=int(k.size),
        n_used=int(k.size),
        rmse_vol=float(np.sqrt(np.mean(resid ** 2))),
        max_abs_resid_vol=float(np.max(np.abs(resid))),
    )


def calendar_arbitrage(
    slices: Sequence[tuple[float, SVIParams]],
    k_grid: Sequence[float] | None = None,
) -> dict[str, object]:
    """Check total variance is non-decreasing in T across fitted slices.

    `slices` is [(T, params), ...] in any order.  Returns the worst violation
    found, expressed in total-variance units, plus the pair it occurred on so
    a caller can quarantine the offending expiry rather than the whole surface.
    """
    ordered = sorted([(float(t), p) for t, p in slices if p is not None], key=lambda x: x[0])
    if len(ordered) < 2:
        return {"checked": False, "ok": True, "violations": [], "worst": None}

    grid = np.asarray(list(k_grid), dtype=float) if k_grid is not None else np.linspace(-0.25, 0.25, 101)
    violations: list[dict[str, object]] = []

    for (t1, p1), (t2, p2) in zip(ordered, ordered[1:]):
        w1 = np.asarray(p1.total_variance(grid), dtype=float)
        w2 = np.asarray(p2.total_variance(grid), dtype=float)
        gap = w2 - w1
        worst_idx = int(np.argmin(gap))
        if gap[worst_idx] < -1e-8:
            violations.append(
                {
                    "t_near": t1,
                    "t_far": t2,
                    "k": float(grid[worst_idx]),
                    "w_near": float(w1[worst_idx]),
                    "w_far": float(w2[worst_idx]),
                    "deficit": float(gap[worst_idx]),
                }
            )

    worst = min(violations, key=lambda v: v["deficit"]) if violations else None
    return {
        "checked": True,
        "ok": not violations,
        "violations": violations,
        "worst": worst,
    }
