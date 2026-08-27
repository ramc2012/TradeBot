"""Train one specialist, gate it, and version the result.

THE SPLIT IS CHRONOLOGICAL AND BY SESSION, ALWAYS
─────────────────────────────────────────────────
Three consecutive slices of whole sessions — train, calibrate, evaluate — in
time order, never shuffled and never overlapping. Two reasons this is not
negotiable here:

  * Rows inside one decision set share a spot path, a chain snapshot and a
    regime. A row-level shuffle puts near-duplicates of a training row into the
    test set and reports the memorisation as skill.
  * Calibration fitted on training rows produces a calibration curve that looks
    perfect and means nothing, because the model has already memorised them.

A SPECIALIST IS SCOPED, AND THE SCOPE IS STORED
───────────────────────────────────────────────
A model is fitted and gated for one (horizon, contract class, target) and is
valid for nothing else. The plan's index-weekly / index-monthly / stock-monthly
split is exactly this, and the scope lives on the model row so a model can never
be applied to a class it was never evaluated on.

FAILING TO TRAIN IS A NORMAL OUTCOME
────────────────────────────────────
Most of these will refuse, especially early, and refusing is the correct
behaviour rather than an error to be worked around. Insufficient sessions, a
target with almost no minority class, a stratum whose horizon cannot clear its
own cost — each returns a reason and writes no champion.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from loguru import logger
from sqlalchemy import text

from candidate_capture.evaluation import (
    MIN_EVAL_SESSIONS,
    Gate,
    evaluate_gates,
    gates_passed,
    refusal_summary,
)
from candidate_capture.features import (
    TARGET_NET_POSITIVE,
    build_features,
    build_target,
    feature_names,
)
from candidate_capture.model import (
    MIN_TRAIN_ROWS,
    MODEL_FAMILY_LOGISTIC,
    fit_calibrator,
    fit_logistic,
    predict_raw,
    apply_calibrator,
    serialize_artifact,
)
from candidate_capture.ranking import rank_decision_set, select
from db.database import AsyncSessionLocal

UTC = timezone.utc

# Chronological slice shares, by SESSION. Kept for the single-split helper,
# which the walk-forward below supersedes for training.
TRAIN_SHARE = 0.6
CALIBRATE_SHARE = 0.2
# Below this there is no point splitting at all.
MIN_TOTAL_SESSIONS = 12

# ── walk-forward ───────────────────────────────────────────────────────────
# Evaluation is WALK-FORWARD (expanding window), not one held-out tail slice.
#
# Two reasons, one statistical and one arithmetic:
#   * It is the stronger prequential test. Every evaluated session is scored by
#     a model that saw only earlier sessions, and the model is refitted as the
#     window expands — so the result is what the system would actually have done
#     in sequence, not what one fixed fit does on one fixed tail.
#   * A single 20% tail slice needs ~50 collected sessions to reach the
#     10-eval-session gate. The plan calls for 10-20 sessions before a first
#     baseline, so a tail split could never promote anything at the data volume
#     the plan actually anticipates. Walk-forward evaluates every session past
#     the initial window, which reaches the gate at roughly the intended volume.
WF_MIN_TRAIN_SESSIONS = 8
WF_CALIBRATE_SESSIONS = 2

STATUS_CHAMPION = "champion"
STATUS_REFUSED = "refused"
STATUS_CANDIDATE = "candidate"


@dataclass
class SpecialistSpec:
    horizon_seconds: int
    target: str = TARGET_NET_POSITIVE
    underlying_class: Optional[str] = None
    expiry_class: Optional[str] = None

    @property
    def name(self) -> str:
        parts = [f"h{self.horizon_seconds}", self.target]
        if self.underlying_class:
            parts.append(self.underlying_class.lower())
        if self.expiry_class:
            parts.append(self.expiry_class.lower())
        return "_".join(parts)


async def load_training_rows(spec: SpecialistSpec) -> list[dict[str, Any]]:
    """Joined snapshot + outcome rows for one specialist, oldest first.

    THE LABEL STATUS IS *NOT* FILTERED HERE, deliberately.

    An earlier version restricted this to `label_status = 'ok'`, which quietly
    made the evaluation dishonest: the ranker then chose from a menu already
    narrowed by facts that did not exist at the anchor. `label_status` and
    `trade_arrived` are both POST-anchor properties — whether a forward mark
    could be found, and whether a trade actually arrived — so filtering on them
    hands the model a decision set the future had already vetted. Measured on
    simulated ladders at this data's own non-arrival rates, up to 84% of the
    full-menu top picks were contracts the filter had removed, and the filter
    also manufactured abstentions the live model would never make.

    Live, the ranker must choose among every eligible contract, including the
    ones that turn out to be unmarkable. So the full menu is loaded and the
    caller decides: a label is required to TRAIN, but not to be RANKED.

    The join is on the full logical contract key plus decision_id — the same
    discipline the labeller uses, so a row can never be paired with another
    contract's outcome.
    """
    clauses = [
        "o.horizon_seconds = :horizon",
        "o.option_type <> 'NO_TRADE'",
    ]
    params: dict[str, Any] = {"horizon": int(spec.horizon_seconds)}
    if spec.underlying_class:
        clauses.append("s.underlying_class = :underlying_class")
        params["underlying_class"] = spec.underlying_class
    if spec.expiry_class:
        clauses.append("s.expiry_class = :expiry_class")
        params["expiry_class"] = spec.expiry_class

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT s.time, s.decision_id, s.session_date, s.underlying,
                           s.underlying_class, s.expiry, s.expiry_class,
                           s.days_to_expiry, s.hours_to_expiry,
                           s.expiry_day_flag, s.monthly_expiry_week_flag,
                           s.option_type, s.strike, s.moneyness, s.moneyness_steps,
                           s.liquidity_bucket, s.liquidity_percentile,
                           s.spot, s.ltp, s.bid, s.ask, s.spread_pct,
                           s.volume, s.oi, s.iv, s.delta, s.gamma, s.theta, s.vega,
                           s.features,
                           s.eligibility_status,
                           o.label_status, o.trade_arrived,
                           o.option_net_return_pct, o.option_mfe_pct,
                           o.breakeven_move_pct
                      FROM candidate_outcomes o
                      JOIN candidate_snapshots s
                        ON s.time = o.time
                       AND s.decision_id = o.decision_id
                       AND s.underlying = o.underlying
                       AND s.option_type = o.option_type
                       AND s.strike IS NOT DISTINCT FROM o.strike
                       AND s.expiry IS NOT DISTINCT FROM o.expiry
                     WHERE {' AND '.join(clauses)}
                     ORDER BY s.time
                    """
                ),
                params,
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def champion_mean_for(spec: SpecialistSpec) -> Optional[float]:
    """The incumbent champion's clustered mean return for this specialist slot.

    Without this the `beats_champion` gate had nothing to compare against and
    was silently skipped, so a WORSE model could replace a better one purely by
    being newer. The slot key matches the partial unique index in migration 036.
    """
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT metrics
                      FROM candidate_model_versions
                     WHERE status = 'champion'
                       AND horizon_seconds = :horizon
                       AND target = :target
                       AND COALESCE(underlying_class,'') = COALESCE(:ucls,'')
                       AND COALESCE(expiry_class,'') = COALESCE(:ecls,'')
                     LIMIT 1
                    """
                ),
                {
                    "horizon": spec.horizon_seconds, "target": spec.target,
                    "ucls": spec.underlying_class, "ecls": spec.expiry_class,
                },
            )
        ).mappings().first()
    if not row:
        return None
    metrics = row.get("metrics") or {}
    clustered = metrics.get("clustered") if isinstance(metrics, Mapping) else None
    if isinstance(clustered, Mapping):
        value = clustered.get("mean")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
    return None


def split_sessions(
    sessions: Sequence[date],
) -> tuple[list[date], list[date], list[date]]:
    """Three consecutive slices of whole sessions, in time order."""
    ordered = sorted(set(sessions))
    n = len(ordered)
    train_end = int(n * TRAIN_SHARE)
    cal_end = train_end + int(n * CALIBRATE_SHARE)
    # Guarantee a non-empty evaluation slice even at the minimum size.
    cal_end = min(cal_end, n - 1)
    return ordered[:train_end], ordered[train_end:cal_end], ordered[cal_end:]


def walk_forward_folds(
    sessions: Sequence[date],
    *,
    min_train: int = WF_MIN_TRAIN_SESSIONS,
    calibrate: int = WF_CALIBRATE_SESSIONS,
) -> list[tuple[list[date], list[date], date]]:
    """(train, calibrate, eval_session) for each step of the expanding window.

    Strictly ordered and non-overlapping within a fold: the calibration sessions
    sit between training and evaluation in time, so neither the fit nor the
    calibrator has seen the session being scored.
    """
    ordered = sorted(set(sessions))
    folds: list[tuple[list[date], list[date], date]] = []
    for i in range(min_train + calibrate, len(ordered)):
        train = ordered[: i - calibrate]
        calib = ordered[i - calibrate : i]
        folds.append((train, calib, ordered[i]))
    return folds


def _selected_returns(
    eval_rows: Sequence[Mapping[str, Any]], probs: Sequence[float]
) -> tuple[list[float], list[Any], int, int, int]:
    """Rank each FULL decision set, take the winner, collect what it earned.

    `eval_rows` is the whole menu — including contracts that could not be
    labelled — because that is the menu a live ranker faces.

    Three outcomes per set, and they are kept distinct:
      * the winner is a contract with an honest label  -> its return is counted
      * the winner is NO_TRADE                          -> abstention, counted
      * the winner has NO honest label                  -> UNRESOLVED

    An unresolved pick is never quietly replaced by the next-best labellable
    contract. Substituting would measure a decision process the system cannot
    execute, which is exactly how a refusal becomes a promotion.
    """
    by_set: dict[Any, list[tuple[Mapping[str, Any], float]]] = {}
    for row, prob in zip(eval_rows, probs):
        by_set.setdefault(row["decision_id"], []).append((row, prob))

    returns: list[float] = []
    clusters: list[Any] = []
    abstained = 0
    unresolved = 0
    for members in by_set.values():
        snapshots = [m[0] for m in members]
        probabilities = [m[1] for m in members]
        ranked = rank_decision_set(snapshots, probabilities)
        chosen = select(ranked)
        if chosen.is_no_trade:
            abstained += 1
            continue
        # Resolved by POSITION, never by field equality: two contracts in one
        # decision set can share a strike across different expiries.
        if chosen.index is None or chosen.index >= len(snapshots):
            unresolved += 1
            continue
        match = snapshots[chosen.index]
        net = match.get("option_net_return_pct")
        if (
            str(match.get("label_status")) != "ok"
            or match.get("trade_arrived") is False
            or net is None
        ):
            # The model picked something the data cannot honestly mark. Counted,
            # not substituted — and gated on downstream.
            unresolved += 1
            continue
        returns.append(float(net))
        clusters.append(match["session_date"])
    return returns, clusters, abstained, len(by_set), unresolved


async def train_specialist(
    spec: SpecialistSpec, *, persist: bool = True
) -> dict[str, Any]:
    """Fit, calibrate, gate and (if it clears every gate) promote one specialist."""
    rows = await load_training_rows(spec)
    if not rows:
        return {"specialist": spec.name, "status": "insufficient_data", "reason": "no labelled rows"}

    sessions = [r["session_date"] for r in rows]
    if len(set(sessions)) < MIN_TOTAL_SESSIONS:
        return {
            "specialist": spec.name,
            "status": "insufficient_data",
            "reason": (
                f"{len(set(sessions))} labelled session(s); a chronological "
                f"train/calibrate/evaluate split needs >= {MIN_TOTAL_SESSIONS}"
            ),
        }

    names = feature_names()

    def _xy(subset: Sequence[Mapping[str, Any]]):
        X, y, kept = [], [], []
        for row in subset:
            target = build_target(row, spec.target)
            if target is None:
                continue  # not evidence — never a silent 0
            X.append(build_features(row))
            y.append(target)
            kept.append(row)
        return X, y, kept

    by_session: dict[date, list[dict[str, Any]]] = {}
    for row in rows:
        by_session.setdefault(row["session_date"], []).append(row)

    champion_mean = await champion_mean_for(spec)
    folds = walk_forward_folds(sessions)
    if not folds:
        return {
            "specialist": spec.name,
            "status": "insufficient_data",
            "reason": (
                f"{len(set(sessions))} sessions; walk-forward needs > "
                f"{WF_MIN_TRAIN_SESSIONS + WF_CALIBRATE_SESSIONS}"
            ),
        }

    all_probs: list[float] = []
    all_outcomes: list[int] = []
    all_eval_rows: list[dict[str, Any]] = []
    selected: list[float] = []
    clusters: list[Any] = []
    abstained = 0
    total_sets = 0
    unresolved = 0
    fold_reasons: list[str] = []
    last_fit = None
    last_calibrator = None
    train_sessions_used: set = set()

    def _run_folds() -> None:
        """The whole walk-forward, run OFF the event loop.

        Every step here is synchronous CPU — feature building, Newton-IRLS per
        fold, isotonic, ranking — and the work grows with the square of the
        session count. Left inline it would hold the loop for the entire pass:
        this runner shares a process with /health, the supervisor's own timeout
        cannot preempt synchronous CPU, and a health endpoint dark for 75s is
        what triggers an autoheal restart. The house rule from the 2026-07-13
        no-trade incident is explicit — per-symbol CPU in runners must go
        through asyncio.to_thread.
        """
        nonlocal all_probs, all_outcomes, all_eval_rows, selected, clusters
        nonlocal abstained, total_sets, unresolved, last_fit, last_calibrator
        for train_s, cal_s, eval_day in folds:
            train_rows = [r for d in train_s for r in by_session.get(d, [])]
            X_train, y_train, _ = _xy(train_rows)
            fit = fit_logistic(X_train, y_train)
            if not fit.ok:
                fold_reasons.append(f"{eval_day.isoformat()}: {fit.reason}")
                continue
            last_fit = fit
            train_sessions_used.update(train_s)

            cal_rows = [r for d in cal_s for r in by_session.get(d, [])]
            X_cal, y_cal, _ = _xy(cal_rows)
            calibrator = (
                fit_calibrator(predict_raw(X_cal, fit.coefficients, fit.intercept), y_cal)
                if len(X_cal) >= 50
                else None
            )
            last_calibrator = calibrator

            # The EVAL MENU is every eligible contract in the session, labelled or
            # not — the menu a live ranker faces. Only the CALIBRATION and SCORING
            # metrics below are restricted to rows that carry an honest label.
            menu = [
                r for r in by_session.get(eval_day, [])
                if str(r.get("eligibility_status")) == "eligible"
            ]
            if not menu:
                continue
            X_menu = [build_features(r) for r in menu]
            menu_probs = apply_calibrator(
                calibrator, predict_raw(X_menu, fit.coefficients, fit.intercept)
            )

            # Calibration/Brier are scored only where a label exists.
            for row, prob in zip(menu, menu_probs):
                target = build_target(row, spec.target)
                if target is None:
                    continue
                all_probs.append(prob)
                all_outcomes.append(target)
                all_eval_rows.append(row)

            fold_sel, fold_clusters, fold_abstained, fold_sets, fold_unresolved = _selected_returns(
                menu, menu_probs
            )
            unresolved += fold_unresolved
            selected.extend(fold_sel)
            clusters.extend(fold_clusters)
            abstained += fold_abstained
            total_sets += fold_sets

    await asyncio.to_thread(_run_folds)

    if last_fit is None or not all_eval_rows:
        return {
            "specialist": spec.name,
            "status": "insufficient_data",
            "reason": "; ".join(fold_reasons[:3]) or "no fold produced a usable fit",
        }

    fit = last_fit
    calibrator = last_calibrator
    eval_days = sorted({r["session_date"] for r in all_eval_rows})

    gates, metrics = evaluate_gates(
        rows=all_eval_rows,
        probs=all_probs,
        outcomes=all_outcomes,
        selected_returns=selected,
        session_dates=clusters,
        champion_mean_return=champion_mean,
        unresolved_selections=unresolved,
    )
    passed = gates_passed(gates)
    metrics["abstained_sets"] = abstained
    metrics["unresolved_selections"] = unresolved
    metrics["decision_sets"] = total_sets
    metrics["calibrated"] = calibrator is not None
    metrics["walk_forward_folds"] = len(folds)

    train_s = sorted(train_sessions_used)
    eval_s = eval_days
    X_train_final, X_eval_final = [], all_eval_rows

    artifact = serialize_artifact(fit=fit, feature_names=names, calibrator=calibrator)
    version_name = f"{spec.name}__wf_{min(eval_s).isoformat()}_{max(eval_s).isoformat()}"

    result = {
        "specialist": spec.name,
        "version_name": version_name,
        "status": STATUS_CHAMPION if passed else STATUS_REFUSED,
        "gates_passed": passed,
        "reason": refusal_summary(gates),
        "metrics": metrics,
        "gates": [g.as_dict() for g in gates],
        "train_rows": fit.n_rows,
        "train_sessions": len(train_s),
        "eval_rows": len(all_eval_rows),
        "eval_sessions": len(eval_s),
    }

    if persist:
        await _persist_model(
            spec=spec, version_name=version_name, artifact=artifact,
            feature_names=names, gates=gates, metrics=metrics, passed=passed,
            train_s=train_s, eval_s=eval_s,
            train_rows=fit.n_rows, eval_rows=len(all_eval_rows),
        )
    return result


async def _persist_model(
    *,
    spec: SpecialistSpec,
    version_name: str,
    artifact: Mapping[str, Any],
    feature_names: Sequence[str],
    gates: Sequence[Gate],
    metrics: Mapping[str, Any],
    passed: bool,
    train_s: Sequence[date],
    eval_s: Sequence[date],
    train_rows: int,
    eval_rows: int,
) -> None:
    """Write the version, retiring any incumbent only when this one is promoted.

    Retire-then-insert inside ONE transaction: the partial unique index allows a
    single champion per specialist slot, so the two statements must succeed or
    fail together. A crash between them would otherwise leave the slot empty.
    """
    import json

    async with AsyncSessionLocal() as session:
        if passed:
            await session.execute(
                text(
                    """
                    UPDATE candidate_model_versions
                       SET status = 'retired', retired_at = now()
                     WHERE status = 'champion'
                       AND horizon_seconds = :horizon
                       AND target = :target
                       AND COALESCE(underlying_class,'') = COALESCE(:ucls,'')
                       AND COALESCE(expiry_class,'') = COALESCE(:ecls,'')
                    """
                ),
                {
                    "horizon": spec.horizon_seconds, "target": spec.target,
                    "ucls": spec.underlying_class, "ecls": spec.expiry_class,
                },
            )
        await session.execute(
            text(
                """
                INSERT INTO candidate_model_versions (
                    id, version_name, status, model_family,
                    horizon_seconds, underlying_class, expiry_class, target,
                    feature_names, artifact,
                    train_rows, train_sessions, eval_rows, eval_sessions,
                    train_start, train_end, eval_start, eval_end,
                    metrics, promotion_gates, gates_passed, promotion_reason,
                    promoted_at
                ) VALUES (
                    CAST(:id AS uuid), :version_name, :status, :family,
                    :horizon, :ucls, :ecls, :target,
                    CAST(:feature_names AS jsonb), CAST(:artifact AS jsonb),
                    :train_rows, :train_sessions, :eval_rows, :eval_sessions,
                    :train_start, :train_end, :eval_start, :eval_end,
                    CAST(:metrics AS jsonb), CAST(:gates AS jsonb),
                    :passed, :reason, :promoted_at
                )
                ON CONFLICT (version_name) DO UPDATE SET
                    status = EXCLUDED.status,
                    artifact = EXCLUDED.artifact,
                    metrics = EXCLUDED.metrics,
                    promotion_gates = EXCLUDED.promotion_gates,
                    gates_passed = EXCLUDED.gates_passed,
                    promotion_reason = EXCLUDED.promotion_reason
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "version_name": version_name,
                "status": STATUS_CHAMPION if passed else STATUS_REFUSED,
                "family": MODEL_FAMILY_LOGISTIC,
                "horizon": spec.horizon_seconds,
                "ucls": spec.underlying_class,
                "ecls": spec.expiry_class,
                "target": spec.target,
                "feature_names": json.dumps(list(feature_names)),
                "artifact": json.dumps(dict(artifact)),
                "train_rows": train_rows,
                "train_sessions": len(set(train_s)),
                "eval_rows": eval_rows,
                "eval_sessions": len(set(eval_s)),
                "train_start": min(train_s) if train_s else None,
                "train_end": max(train_s) if train_s else None,
                "eval_start": min(eval_s) if eval_s else None,
                "eval_end": max(eval_s) if eval_s else None,
                "metrics": json.dumps(dict(metrics), default=str),
                "gates": json.dumps([g.as_dict() for g in gates], default=str),
                "passed": passed,
                "reason": refusal_summary(gates),
                "promoted_at": datetime.now(UTC) if passed else None,
            },
        )
        await session.commit()


DEFAULT_SPECIALISTS = (
    SpecialistSpec(horizon_seconds=900, expiry_class="WEEKLY"),
    SpecialistSpec(horizon_seconds=900, expiry_class="MONTHLY"),
    SpecialistSpec(horizon_seconds=1800, expiry_class="WEEKLY"),
    SpecialistSpec(horizon_seconds=1800, expiry_class="MONTHLY"),
    SpecialistSpec(horizon_seconds=3600, expiry_class="MONTHLY"),
)


async def run_training(
    specialists: Sequence[SpecialistSpec] = DEFAULT_SPECIALISTS,
) -> dict[str, Any]:
    """Train every specialist. Flag-gated OFF by default."""
    from core.config import settings

    if not bool(getattr(settings, "CANDIDATE_TRAINING_ENABLED", False)):
        return {"status": "disabled", "flag": "CANDIDATE_TRAINING_ENABLED"}

    import json

    run_id = str(uuid.uuid4())
    started = datetime.now(UTC)
    produced: list[dict[str, Any]] = []
    for spec in specialists:
        try:
            produced.append(await train_specialist(spec))
        except Exception as exc:  # noqa: BLE001 — one specialist must not end the run
            produced.append(
                {"specialist": spec.name, "status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
            )
            logger.warning("[candidate-train] {} failed: {!r}", spec.name, exc)

    promoted = [p for p in produced if p.get("status") == STATUS_CHAMPION]
    status = "ok" if promoted else ("insufficient_data" if all(
        p.get("status") == "insufficient_data" for p in produced
    ) else "no_promotion")

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                INSERT INTO candidate_training_runs
                    (id, started_at, finished_at, status, reason, requested, produced)
                VALUES (CAST(:id AS uuid), :started, now(), :status, :reason,
                        CAST(:requested AS jsonb), CAST(:produced AS jsonb))
                """
            ),
            {
                "id": run_id,
                "started": started,
                "status": status,
                "reason": f"{len(promoted)} of {len(produced)} specialist(s) promoted",
                "requested": json.dumps([s.name for s in specialists]),
                "produced": json.dumps(produced, default=str),
            },
        )
        await session.commit()

    logger.info(
        "[candidate-train] run={} status={} promoted={}/{}",
        run_id, status, len(promoted), len(produced),
    )
    return {
        "run_id": run_id,
        "status": status,
        "promoted": len(promoted),
        "specialists": produced,
        "result_count": len(promoted),
    }
