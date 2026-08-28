"""Does the model DISCRIMINATE? — separate from whether it should be promoted.

WHY THIS EXISTS
───────────────
The promotion gates answer one question: should this model be trusted with
capital? They do NOT answer whether there is signal in the data. Those come
apart constantly — a model can rank outcomes genuinely better than chance and
still lose money once costs are paid, and a model can look profitable on a
handful of lucky selections while having no discriminative power at all.

Reporting a gate refusal as "no edge" conflates them. It is a claim about
predictive content, and nothing in the gate suite measures predictive content.
So this module measures it directly, with the controls that make the number
mean something:

  DISCRIMINATION   AUC and Brier skill on out-of-sample walk-forward predictions
  A NULL           labels permuted WITHIN session, refit, to get the AUC a model
                   of this capacity reaches on this data by construction
  BASELINES        what trivial rules achieve, so "better than chance" is also
                   "better than the obvious thing"
  ABLATION         which feature groups carry the information
  LEARNING CURVE   whether the answer is data-starved or genuinely flat

THE PERMUTATION IS WITHIN SESSION, NOT GLOBAL
─────────────────────────────────────────────
Shuffling labels across the whole dataset destroys the session structure as well
as the signal, so the null it produces is too easy and every model looks
significant against it. Permuting inside each session preserves the daily base
rate and the clustering, and only destroys the row-level association — which is
the thing being tested.

Everything here is read-only and deterministic: no sampling that is not seeded by
construction, so a reported number can be reproduced from the same rows.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping, Optional, Sequence

import numpy as np
from loguru import logger

from candidate_capture.features import build_features, build_target, feature_names
from candidate_capture.model import fit_logistic, predict_raw
from candidate_capture.training import (
    SpecialistSpec,
    load_training_rows,
    walk_forward_folds,
)

UTC = timezone.utc

# Feature groups, in the order feature_names() emits them. Used for ablation:
# dropping a whole group answers "does this KIND of information matter", which
# is a question a per-column importance cannot answer when columns are collinear.
FEATURE_GROUPS: dict[str, tuple[int, int]] = {
    "geometry": (0, 6),
    "quote": (6, 9),
    "activity": (9, 14),
    "pricing": (14, 23),
    "chain_context": (23, 30),
    "one_hot": (30, 47),
}

DEFAULT_PERMUTATIONS = 20
# A permuted refit that cannot fit is skipped rather than counted as a zero —
# scoring it would drag the null down and flatter the real model.
MIN_ROWS_FOR_FOLD = 200


def auc(probs: Sequence[float], labels: Sequence[int]) -> Optional[float]:
    """Area under the ROC curve, by rank (Mann-Whitney U), ties averaged.

    Rank-based rather than threshold-sweeping so it is exact and needs no bin
    choice. Returns None when one class is absent, because AUC is undefined
    then — not 0.5, which would read as "no skill" rather than "not measurable".
    """
    p = np.asarray(list(probs), dtype=float)
    y = np.asarray(list(labels), dtype=int)
    if p.size == 0 or p.size != y.size:
        return None
    pos, neg = int((y == 1).sum()), int((y == 0).sum())
    if pos == 0 or neg == 0:
        return None
    order = np.argsort(p, kind="mergesort")
    ranks = np.empty_like(order, dtype=float)
    sorted_p = p[order]
    i = 0
    while i < sorted_p.size:
        j = i
        while j + 1 < sorted_p.size and sorted_p[j + 1] == sorted_p[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return float((ranks[y == 1].sum() - pos * (pos + 1) / 2.0) / (pos * neg))


def brier_skill(probs: Sequence[float], labels: Sequence[int]) -> Optional[float]:
    """Brier score relative to always predicting the base rate.

    Skill rather than raw Brier: a raw score looks excellent on any rare event
    simply by predicting it never happens.
    """
    p = np.asarray(list(probs), dtype=float)
    y = np.asarray(list(labels), dtype=float)
    if p.size == 0 or p.size != y.size:
        return None
    base = float(y.mean())
    ref = float(((base - y) ** 2).mean())
    if ref <= 0:
        return None
    return float(1.0 - ((p - y) ** 2).mean() / ref)


def calibration_bins(
    probs: Sequence[float], labels: Sequence[int], bins: int = 10
) -> list[dict[str, Any]]:
    """Predicted vs realised frequency, so miscalibration is visible as shape."""
    p = np.asarray(list(probs), dtype=float)
    y = np.asarray(list(labels), dtype=float)
    if p.size == 0:
        return []
    out: list[dict[str, Any]] = []
    edges = np.linspace(0.0, 1.0, bins + 1)
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (p >= lo) & (p < hi if hi < 1.0 else p <= hi)
        n = int(mask.sum())
        if n == 0:
            continue
        out.append(
            {
                "bin_low": round(float(lo), 3),
                "bin_high": round(float(hi), 3),
                "n": n,
                "predicted": round(float(p[mask].mean()), 5),
                "realised": round(float(y[mask].mean()), 5),
            }
        )
    return out


@dataclass
class FoldPredictions:
    probs: list[float]
    labels: list[int]
    sessions: list[Any]
    rows: list[dict[str, Any]]


def _design(rows: Sequence[Mapping[str, Any]], target: str):
    X, y, kept = [], [], []
    for row in rows:
        label = build_target(row, target)
        if label is None:
            continue
        X.append(build_features(row))
        y.append(label)
        kept.append(dict(row))
    return X, y, kept


def walk_forward_predictions(
    rows: Sequence[Mapping[str, Any]],
    target: str,
    *,
    columns: Optional[Sequence[int]] = None,
    permute_within_session: bool = False,
    seed: int = 0,
) -> FoldPredictions:
    """Out-of-sample predictions from an expanding window.

    `columns` restricts the design matrix to a subset (ablation). `permute`
    shuffles labels INSIDE each training session, which destroys the row-level
    association while preserving the session base rate — the null.
    """
    by_session: dict[Any, list[Mapping[str, Any]]] = {}
    for row in rows:
        by_session.setdefault(row["session_date"], []).append(row)
    sessions = sorted(by_session)
    folds = walk_forward_folds(sessions)

    rng = np.random.default_rng(seed)
    out = FoldPredictions([], [], [], [])

    for train_s, _cal_s, eval_day in folds:
        train_rows = [r for d in train_s for r in by_session.get(d, [])]
        X_train, y_train, _ = _design(train_rows, target)
        if len(X_train) < MIN_ROWS_FOR_FOLD:
            continue

        if permute_within_session:
            # Permute inside each session so the daily base rate survives.
            start = 0
            y_train = list(y_train)
            for d in train_s:
                n = len([r for r in by_session.get(d, []) if build_target(r, target) is not None])
                if n > 1:
                    block = y_train[start : start + n]
                    rng.shuffle(block)
                    y_train[start : start + n] = block
                start += n

        Xa = np.asarray(X_train, dtype=float)
        if columns is not None:
            Xa = Xa[:, list(columns)]
        fit = fit_logistic(Xa.tolist(), y_train)
        if not fit.ok:
            continue

        X_eval, y_eval, kept = _design(by_session.get(eval_day, []), target)
        if not X_eval:
            continue
        Xe = np.asarray(X_eval, dtype=float)
        if columns is not None:
            Xe = Xe[:, list(columns)]
        probs = predict_raw(Xe.tolist(), fit.coefficients, fit.intercept)

        out.probs.extend(probs)
        out.labels.extend(y_eval)
        out.sessions.extend([eval_day] * len(y_eval))
        out.rows.extend(kept)
    return out


def selection_baselines(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """What trivial rules earn on the same decision sets.

    "Better than chance" is a weak claim if the obvious heuristic does as well,
    so the model has to clear these too. Returns mean NET return per rule, with
    the abstain rule fixed at exactly 0 by definition.
    """
    by_set: dict[Any, list[Mapping[str, Any]]] = {}
    for row in rows:
        by_set.setdefault(row["decision_id"], []).append(row)

    def _net(row: Mapping[str, Any]) -> Optional[float]:
        value = row.get("option_net_return_pct")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    picks: dict[str, list[float]] = {
        "always_atm": [], "most_liquid": [], "cheapest": [], "first_listed": [],
    }
    for members in by_set.values():
        def _take(key, reverse=False):
            usable = [m for m in members if m.get(key) is not None]
            if not usable:
                return None
            return sorted(usable, key=lambda m: float(m[key]), reverse=reverse)[0]

        atm = min(
            (m for m in members if m.get("moneyness_steps") is not None),
            key=lambda m: abs(float(m["moneyness_steps"])), default=None,
        )
        liquid = _take("liquidity_percentile", reverse=True)
        cheap = _take("ltp")
        first = members[0]
        for name, chosen in (
            ("always_atm", atm), ("most_liquid", liquid),
            ("cheapest", cheap), ("first_listed", first),
        ):
            if chosen is None:
                continue
            net = _net(chosen)
            if net is not None:
                picks[name].append(net)

    out: dict[str, Any] = {
        "always_abstain": {"mean_net_return": 0.0, "trades": 0,
                           "note": "abstaining returns exactly 0 by definition"},
    }
    for name, values in picks.items():
        out[name] = {
            "mean_net_return": round(sum(values) / len(values), 8) if values else None,
            "trades": len(values),
        }
    return out


async def run_experiment(
    spec: SpecialistSpec, *, permutations: int = DEFAULT_PERMUTATIONS
) -> dict[str, Any]:
    """Full experimental battery for one specialist. Trains nothing persistent."""
    started = datetime.now(UTC)
    rows = await load_training_rows(spec)
    if not rows:
        return {"specialist": spec.name, "status": "no_data"}

    names = feature_names()
    actual = walk_forward_predictions(rows, spec.target)
    if not actual.probs:
        return {"specialist": spec.name, "status": "no_folds",
                "reason": "no fold produced a usable fit"}

    observed_auc = auc(actual.probs, actual.labels)
    base_rate = float(np.mean(actual.labels)) if actual.labels else None

    # ── the null ──────────────────────────────────────────────────────────
    null_aucs: list[float] = []
    for i in range(max(0, permutations)):
        permuted = walk_forward_predictions(
            rows, spec.target, permute_within_session=True, seed=i
        )
        value = auc(permuted.probs, permuted.labels)
        if value is not None:
            null_aucs.append(value)

    null_mean = float(np.mean(null_aucs)) if null_aucs else None
    null_sd = float(np.std(null_aucs, ddof=1)) if len(null_aucs) > 1 else None
    # One-sided empirical p: how often the null reached the observed AUC. The
    # +1 correction keeps p strictly positive — with 20 permutations the
    # smallest honest statement is p <= 0.048, not p = 0.
    p_value = (
        (sum(1 for v in null_aucs if v >= (observed_auc or 0)) + 1) / (len(null_aucs) + 1)
        if null_aucs and observed_auc is not None
        else None
    )

    # ── ablation ──────────────────────────────────────────────────────────
    ablation: list[dict[str, Any]] = []
    all_idx = list(range(len(names)))
    for group, (lo, hi) in FEATURE_GROUPS.items():
        kept = [i for i in all_idx if not (lo <= i < hi)]
        dropped = walk_forward_predictions(rows, spec.target, columns=kept)
        value = auc(dropped.probs, dropped.labels)
        ablation.append(
            {
                "group": group,
                "features_dropped": hi - lo,
                "auc_without": round(value, 5) if value is not None else None,
                "auc_delta": (
                    round((observed_auc or 0) - value, 5) if value is not None else None
                ),
            }
        )
    ablation.sort(key=lambda a: -(a["auc_delta"] or 0))

    # ── learning curve ────────────────────────────────────────────────────
    sessions = sorted({r["session_date"] for r in rows})
    curve: list[dict[str, Any]] = []
    for cut in (0.4, 0.6, 0.8, 1.0):
        take = max(12, int(len(sessions) * cut))
        subset = [r for r in rows if r["session_date"] in set(sessions[:take])]
        preds = walk_forward_predictions(subset, spec.target)
        value = auc(preds.probs, preds.labels)
        curve.append(
            {
                "train_sessions": take,
                "auc": round(value, 5) if value is not None else None,
                "eval_rows": len(preds.probs),
            }
        )

    return {
        "specialist": spec.name,
        "status": "ok",
        "target": spec.target,
        "horizon_seconds": spec.horizon_seconds,
        "expiry_class": spec.expiry_class,
        "n_rows": len(actual.probs),
        "n_sessions": len(set(actual.sessions)),
        "base_rate": round(base_rate, 5) if base_rate is not None else None,
        "discrimination": {
            "auc": round(observed_auc, 5) if observed_auc is not None else None,
            "brier_skill": (
                round(brier_skill(actual.probs, actual.labels), 5)
                if brier_skill(actual.probs, actual.labels) is not None else None
            ),
            "calibration": calibration_bins(actual.probs, actual.labels),
        },
        "permutation_null": {
            "n": len(null_aucs),
            "mean_auc": round(null_mean, 5) if null_mean is not None else None,
            "sd_auc": round(null_sd, 5) if null_sd is not None else None,
            "p_value": round(p_value, 5) if p_value is not None else None,
            "note": (
                "labels permuted WITHIN session, so the daily base rate and the "
                "clustering survive and only the row-level association is destroyed"
            ),
        },
        "ablation": ablation,
        "learning_curve": curve,
        "baselines": selection_baselines(actual.rows),
        "elapsed_seconds": round((datetime.now(UTC) - started).total_seconds(), 1),
    }
