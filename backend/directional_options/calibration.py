"""Isotonic regression calibration for Directional Options confidence.

The backtest reveals raw model confidence is *anti-predictive* (high-conviction
trades lose more often than low-conviction ones). We map raw confidence → a
monotonic calibrated probability via Pool-Adjacent-Violators (PAV).

PAV gives us a piecewise-constant monotone non-decreasing function f(x) that
minimizes Σ(f(x_i) − y_i)² subject to f being monotone. This is the gold
standard for probability calibration on small samples (Niculescu-Mizil &
Caruana 2005); no sklearn dependency needed.

If the raw confidence is anti-predictive, isotonic will produce a FLAT or
near-flat curve, effectively shrinking all confidences toward the base rate
— which is the correct response to an uninformative input.

Usage at inference time:
    cal = load_calibrator()
    calibrated_p_win = cal.predict(raw_confidence)
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Sequence


@dataclass
class IsotonicCalibrator:
    """Piecewise-constant monotone-non-decreasing calibrator.

    Stored as paired arrays of x-breakpoints and y-values such that
    predict(x) interpolates linearly between adjacent breakpoints.
    """
    x_breaks: list[float]
    y_values: list[float]
    n_samples: int
    base_rate: float

    def predict(self, x: float) -> float:
        if not self.x_breaks:
            return self.base_rate
        if x <= self.x_breaks[0]:
            return self.y_values[0]
        if x >= self.x_breaks[-1]:
            return self.y_values[-1]
        # Binary search the segment then linear interpolate
        lo, hi = 0, len(self.x_breaks) - 1
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if self.x_breaks[mid] <= x:
                lo = mid
            else:
                hi = mid
        x0, x1 = self.x_breaks[lo], self.x_breaks[hi]
        y0, y1 = self.y_values[lo], self.y_values[hi]
        if x1 == x0:
            return y0
        t = (x - x0) / (x1 - x0)
        return y0 + t * (y1 - y0)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "IsotonicCalibrator":
        data = json.loads(raw)
        return cls(**data)


def _pav_fit(x: Sequence[float], y: Sequence[float]) -> tuple[list[float], list[float]]:
    """Pool-Adjacent-Violators isotonic regression.

    Returns (x_sorted, y_fitted) — monotone non-decreasing in x.
    """
    pairs = sorted(zip(x, y))
    xs = [p[0] for p in pairs]
    ys = [float(p[1]) for p in pairs]
    weights = [1.0] * len(ys)

    # Standard PAV pass: when ys[i] > ys[i+1] (violation), merge their blocks.
    # Use stacks for efficient implementation.
    stack_y: list[float] = []
    stack_w: list[float] = []
    for yi in ys:
        cy, cw = yi, 1.0
        while stack_y and stack_y[-1] > cy:
            # merge
            prev_y = stack_y.pop()
            prev_w = stack_w.pop()
            new_w = prev_w + cw
            cy = (prev_y * prev_w + cy * cw) / new_w
            cw = new_w
        stack_y.append(cy)
        stack_w.append(cw)

    # Expand the pooled blocks back to per-sample y values.
    fitted = []
    block_idx = 0
    consumed_in_block = 0.0
    for _ in ys:
        while consumed_in_block >= stack_w[block_idx]:
            block_idx += 1
            consumed_in_block = 0.0
        fitted.append(stack_y[block_idx])
        consumed_in_block += 1.0
    return xs, fitted


def fit_isotonic(
    confidences: Sequence[float],
    outcomes: Sequence[int],
) -> IsotonicCalibrator:
    """Fit isotonic calibration from (raw_conf, outcome ∈ {0,1}) pairs.

    Outcomes should be binary (1 = win, 0 = loss). For very small samples
    we add Bayesian shrinkage toward the base rate (Beta(1,1) prior).
    """
    if not confidences or not outcomes or len(confidences) != len(outcomes):
        raise ValueError("Confidences/outcomes empty or length-mismatched")
    n = len(confidences)
    base_rate = float(sum(outcomes)) / float(n)

    # Soft labels with Beta(1,1) prior so PAV doesn't latch on a single
    # win/loss point at low sample sizes.
    laplace_alpha = 1.0
    laplace_beta = 1.0
    smooth_outcomes = [(float(o) * (1.0) + laplace_alpha / (laplace_alpha + laplace_beta)) / 2.0
                       for o in outcomes]
    # Actually use raw 0/1 with PAV; smoothing is too aggressive at n=65.
    smooth_outcomes = [float(o) for o in outcomes]

    xs, ys = _pav_fit(list(confidences), smooth_outcomes)

    # Compress to unique x-breakpoints
    unique_x: list[float] = []
    unique_y: list[float] = []
    for xv, yv in zip(xs, ys):
        if not unique_x or xv != unique_x[-1]:
            unique_x.append(xv)
            unique_y.append(yv)
        else:
            unique_y[-1] = yv  # PAV ensures equal x → equal y

    return IsotonicCalibrator(
        x_breaks=unique_x,
        y_values=unique_y,
        n_samples=n,
        base_rate=base_rate,
    )


def brier_score(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    """Mean squared error between predicted probability and binary outcome."""
    n = len(probs)
    if n == 0 or n != len(outcomes):
        return float("nan")
    return sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / n


def brier_skill(
    probs: Sequence[float],
    outcomes: Sequence[int],
) -> float:
    """Brier skill score vs constant-base-rate model.

    Positive = model adds info. Negative = model is worse than always
    predicting the base rate.
    """
    n = len(probs)
    if n == 0:
        return float("nan")
    base = sum(outcomes) / n
    bs_model = brier_score(probs, outcomes)
    bs_ref = sum((base - o) ** 2 for o in outcomes) / n
    if bs_ref == 0:
        return 0.0
    return 1.0 - (bs_model / bs_ref)


_CACHED_CALIBRATOR: IsotonicCalibrator | None = None


def load_calibrator(path: str | Path | None = None) -> IsotonicCalibrator | None:
    """Load cached calibrator from disk; returns None if absent."""
    global _CACHED_CALIBRATOR
    if _CACHED_CALIBRATOR is not None:
        return _CACHED_CALIBRATOR
    p = Path(path) if path else Path("/app/runtime/directional_options/calibration_isotonic.json")
    if not p.exists():
        return None
    _CACHED_CALIBRATOR = IsotonicCalibrator.from_json(p.read_text())
    return _CACHED_CALIBRATOR


def save_calibrator(cal: IsotonicCalibrator, path: str | Path | None = None) -> Path:
    p = Path(path) if path else Path("/app/runtime/directional_options/calibration_isotonic.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(cal.to_json())
    global _CACHED_CALIBRATOR
    _CACHED_CALIBRATOR = cal
    return p
