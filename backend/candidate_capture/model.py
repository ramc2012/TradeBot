"""The baseline champion: L2 logistic regression, then isotonic calibration.

WHY LOGISTIC AND NOT GRADIENT-BOOSTED TREES
───────────────────────────────────────────
The plan asks for "a calibrated logistic model or gradient-boosted trees" as the
first champion. It is logistic here for a concrete reason: scikit-learn,
lightgbm, xgboost and torch are NOT installed in the backend container — only
numpy, scipy and pandas. Adding a heavy ML dependency to the image that runs
live trading lanes is a real operational change, and it is not one worth making
for a baseline whose whole job is to be a floor the next model must clear.

The house already made this call once: `directional_options/calibration.py`
implements isotonic regression by hand specifically to avoid the dependency.
This follows that precedent rather than reversing it.

WHY CALIBRATION IS A SEPARATE STAGE
───────────────────────────────────
A ranker needs an ORDER; a position sizer needs a PROBABILITY. Logistic output
is already a probability in form, but a regularized fit on an imbalanced target
is systematically over- or under-confident, and the plan explicitly requires
calibrated probabilities per contract class before promotion. Isotonic is fitted
on a HELD-OUT slice — never the training rows — because calibrating on the data
the model memorised produces a calibration curve that looks perfect and means
nothing.

Everything here is deterministic: no random initialisation, no shuffling, no
sampling. The same rows in the same order always produce the same artifact, so a
model version is reproducible from its stored inputs.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import numpy as np

MODEL_FAMILY_LOGISTIC = "l2_logistic_isotonic"
ARTIFACT_VERSION = 1

# Ridge penalty. Not swept against any outcome: it is a fixed, mild default that
# keeps the fit stable when a one-hot column is rare or collinear. Tuning it
# against measured returns would be a search over the evaluation set, which is
# the thing the promotion gates exist to prevent.
DEFAULT_L2 = 1.0
MAX_NEWTON_STEPS = 100
CONVERGENCE_TOL = 1e-8
# Below this a fit is refused outright. A logistic model on a handful of rows
# will separate perfectly and mean nothing.
MIN_TRAIN_ROWS = 200
# Both classes must actually be present; a single-class target has no gradient.
MIN_MINORITY_ROWS = 20


@dataclass
class FitResult:
    ok: bool
    reason: Optional[str] = None
    coefficients: list[float] = field(default_factory=list)
    intercept: float = 0.0
    n_rows: int = 0
    n_positive: int = 0
    iterations: int = 0
    converged: bool = False


def _sigmoid(z: np.ndarray) -> np.ndarray:
    # Split on sign so neither exp overflows — the standard stable form.
    out = np.empty_like(z, dtype=float)
    pos = z >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-z[pos]))
    ez = np.exp(z[~pos])
    out[~pos] = ez / (1.0 + ez)
    return out


def fit_logistic(
    X: Sequence[Sequence[float]],
    y: Sequence[int],
    *,
    l2: float = DEFAULT_L2,
    min_rows: int = MIN_TRAIN_ROWS,
) -> FitResult:
    """L2-penalised logistic regression by Newton-IRLS.

    Refuses rather than returns a weak fit: too few rows, or a target with
    almost no minority class, gives `ok=False` and a reason. A model that fits
    anything it is handed is how an untrainable stratum acquires a champion.
    """
    Xa = np.asarray(X, dtype=float)
    ya = np.asarray(y, dtype=float)
    if Xa.size == 0 or ya.size == 0:
        # Distinguished from a genuine shape mismatch: an EMPTY matrix means
        # every row was filtered out by the target, which is a data problem
        # (usually a column the query forgot to select), not a shape bug. The
        # merged message sent an earlier debug down entirely the wrong path.
        return FitResult(
            ok=False,
            reason=(
                "no usable training rows: every row returned None from the "
                "target. Check that the query selects the column the target "
                "reads."
            ),
        )
    if Xa.ndim != 2 or Xa.shape[0] != ya.shape[0]:
        return FitResult(ok=False, reason="X and y shapes disagree")

    n, d = Xa.shape
    positives = int(ya.sum())
    minority = min(positives, n - positives)

    if n < min_rows:
        return FitResult(
            ok=False, reason=f"only {n} rows; need >= {min_rows}", n_rows=n,
            n_positive=positives,
        )
    if minority < MIN_MINORITY_ROWS:
        return FitResult(
            ok=False,
            reason=(
                f"minority class has {minority} rows; need >= {MIN_MINORITY_ROWS}. "
                "A near-single-class target usually means the horizon is not "
                "decidable for this stratum, not that the signal is strong."
            ),
            n_rows=n, n_positive=positives,
        )

    # Intercept as an explicit column, left UNPENALISED below.
    Z = np.hstack([np.ones((n, 1)), Xa])
    w = np.zeros(Z.shape[1])
    penalty = np.eye(Z.shape[1]) * l2
    penalty[0, 0] = 0.0

    converged = False
    step = 0
    for step in range(1, MAX_NEWTON_STEPS + 1):
        p = _sigmoid(Z @ w)
        # Bound the IRLS weights away from zero; a saturated probability makes
        # the Hessian singular and the step explode.
        s = np.clip(p * (1.0 - p), 1e-10, None)
        gradient = Z.T @ (ya - p) - penalty @ w
        hessian = (Z.T * s) @ Z + penalty
        try:
            delta = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError:
            delta = np.linalg.lstsq(hessian, gradient, rcond=None)[0]
        w = w + delta
        if float(np.max(np.abs(delta))) < CONVERGENCE_TOL:
            converged = True
            break

    return FitResult(
        ok=True,
        coefficients=[float(v) for v in w[1:]],
        intercept=float(w[0]),
        n_rows=n,
        n_positive=positives,
        iterations=step,
        converged=converged,
    )


def predict_raw(
    X: Sequence[Sequence[float]], coefficients: Sequence[float], intercept: float
) -> list[float]:
    """Uncalibrated probabilities from the fitted linear score."""
    Xa = np.asarray(X, dtype=float)
    beta = np.asarray(coefficients, dtype=float)
    if Xa.ndim != 2 or Xa.shape[1] != beta.shape[0]:
        raise ValueError(
            f"feature width {Xa.shape[1] if Xa.ndim == 2 else '?'} does not match "
            f"the {beta.shape[0]} fitted coefficients — the vector layout changed"
        )
    return [float(v) for v in _sigmoid(Xa @ beta + intercept)]


# ── calibration ────────────────────────────────────────────────────────────
def fit_calibrator(probs: Sequence[float], outcomes: Sequence[int]) -> Optional[Any]:
    """Isotonic calibrator over HELD-OUT predictions.

    Reuses `directional_options/calibration.py` — the repo's existing PAV
    implementation — loaded as a leaf so this package never imports the
    directional service (which mounts a paper book). Returns None when the
    holdout is too thin to calibrate, in which case raw probabilities are used
    and the model records that it is uncalibrated.
    """
    from candidate_capture._leaf_import import isotonic

    module = isotonic()
    try:
        return module.fit_isotonic(list(probs), list(outcomes))
    except Exception:  # noqa: BLE001 — an uncalibratable holdout is not fatal
        return None


def apply_calibrator(calibrator: Optional[Any], probs: Sequence[float]) -> list[float]:
    if calibrator is None:
        return [float(p) for p in probs]
    return [float(calibrator.predict(float(p))) for p in probs]


# ── artifact ───────────────────────────────────────────────────────────────
def serialize_artifact(
    *,
    fit: FitResult,
    feature_names: Sequence[str],
    calibrator: Optional[Any],
) -> dict[str, Any]:
    """The stored model. Everything needed to score a row, and nothing else."""
    return {
        "artifact_version": ARTIFACT_VERSION,
        "family": MODEL_FAMILY_LOGISTIC,
        "intercept": fit.intercept,
        "coefficients": list(fit.coefficients),
        # Stored WITH the coefficients so a layout change is detectable rather
        # than silently rescoring every column as something else.
        "feature_names": list(feature_names),
        "calibrator": json.loads(calibrator.to_json()) if calibrator is not None else None,
        "calibrated": calibrator is not None,
        "n_rows": fit.n_rows,
        "n_positive": fit.n_positive,
        "converged": fit.converged,
        "iterations": fit.iterations,
    }


def score_rows(
    artifact: dict[str, Any],
    rows: Sequence[Sequence[float]],
    feature_names: Sequence[str],
) -> list[float]:
    """Calibrated probabilities for new rows, refusing a layout mismatch."""
    stored = list(artifact.get("feature_names") or [])
    if stored and list(feature_names) != stored:
        raise ValueError(
            "feature layout has changed since this model was fitted — refusing to "
            "score. Retrain rather than reordering; every coefficient would "
            "otherwise apply to a different column."
        )
    raw = predict_raw(rows, artifact["coefficients"], float(artifact["intercept"]))
    payload = artifact.get("calibrator")
    if not payload:
        return raw

    from candidate_capture._leaf_import import isotonic

    calibrator = isotonic().IsotonicCalibrator.from_json(json.dumps(payload))
    return apply_calibrator(calibrator, raw)
