"""Phase 0 verdict: did the feature set + baseline model clear the go/no-go criteria?

Output: a JSON written to `artifacts/phase0_verdict.json` and a one-line console summary.
Verdict is `go` if and only if ALL four criteria (from CLAUDE.md) pass.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from nomad_sniper.evaluation.metrics import (
    acted_ev,
    counterfactual_pnl,
    daily_pnl_series,
    directional_classification_metrics,
    sharpe_ratio,
    skip_accuracy_by_quality_bucket,
)
from nomad_sniper.utils.logging import get_logger
from nomad_sniper.utils.provenance import make_provenance

log = get_logger()


# Pre-committed thresholds. DO NOT change after seeing results (see CLAUDE.md).
SKIP_ACCURACY_THRESHOLD = 0.65        # On bottom-decile losers
PNL_IMPROVEMENT_THRESHOLD_PCT = 30.0  # Counterfactual vs actual
SHARPE_RATIO_MULTIPLIER = 1.5         # Retained-trades Sharpe / full Sharpe

NONE_RECALL_THRESHOLD = 0.70
UP_DOWN_PRECISION_THRESHOLD = 0.55
ACTED_EV_THRESHOLD_ATR = 0.0


@dataclass
class Phase0Verdict:
    verdict: str  # "go" | "no-go"
    skip_accuracy_bottom_decile: float
    net_pnl_improvement_pct: float
    full_sharpe: float
    retained_sharpe: float
    sharpe_uplift: float
    leakage_tests_passed: bool
    reasons: list[str] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    bucket_breakdown: list[dict] = field(default_factory=list)
    counterfactual: dict = field(default_factory=dict)


@dataclass
class DirectionalPhase0Verdict:
    verdict: str
    none_recall: float
    up_precision: float
    down_precision: float
    acted_ev_atr_2x_slippage: float
    leakage_tests_passed: bool
    instrument_independence_tests_passed: bool
    reasons: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    acted_ev: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)


def run_phase0_verdict(
    labels: pd.DataFrame,
    skip_decisions: pd.Series,
    *,
    leakage_tests_passed: bool,
    artifact_path: Path | str | None = None,
) -> Phase0Verdict:
    """Compute Phase 0 verdict from out-of-sample skip decisions.

    Args:
        labels:               Round-trip labels (must include `pnl_decile`, `net_pnl`, `exit_at`).
        skip_decisions:       Series indexed by trade_id; 1 = skip, 0 = take.
        leakage_tests_passed: Has `pytest tests/test_no_leakage.py` been run and passed?
                              This must be supplied by the caller — it is not auto-run here.
        artifact_path:        Where to write the verdict JSON. Defaults to
                              `artifacts/phase0_verdict.json`.
    """
    common = labels.index.intersection(skip_decisions.index)
    labels = labels.loc[common]
    skip_decisions = skip_decisions.loc[common]

    if labels.empty:
        raise ValueError("No overlap between labels and skip_decisions.")

    bucket = skip_accuracy_by_quality_bucket(labels, skip_decisions, bucket_col="pnl_decile")
    bottom_decile_skip = float(bucket.loc[bucket.index.min(), "skip_rate"])

    cf = counterfactual_pnl(labels, skip_decisions)

    full_pnl = daily_pnl_series(labels)
    retained = daily_pnl_series(labels, mask=(skip_decisions == 0))
    full_sharpe = sharpe_ratio(full_pnl)
    retained_sharpe_v = sharpe_ratio(retained)
    sharpe_uplift = retained_sharpe_v / full_sharpe if full_sharpe != 0 else np.inf

    reasons = []
    if bottom_decile_skip < SKIP_ACCURACY_THRESHOLD:
        reasons.append(
            f"Skip rate on bottom decile = {bottom_decile_skip:.1%}, "
            f"required ≥ {SKIP_ACCURACY_THRESHOLD:.0%}"
        )
    if cf["improvement_pct"] < PNL_IMPROVEMENT_THRESHOLD_PCT:
        reasons.append(
            f"Net P&L improvement = {cf['improvement_pct']:.1f}%, "
            f"required ≥ {PNL_IMPROVEMENT_THRESHOLD_PCT:.0f}%"
        )
    if sharpe_uplift < SHARPE_RATIO_MULTIPLIER:
        reasons.append(
            f"Sharpe uplift = {sharpe_uplift:.2f}x, required ≥ {SHARPE_RATIO_MULTIPLIER}x"
        )
    if not leakage_tests_passed:
        reasons.append("Leakage tests not passed.")

    verdict_str = "go" if not reasons else "no-go"

    verdict = Phase0Verdict(
        verdict=verdict_str,
        skip_accuracy_bottom_decile=bottom_decile_skip,
        net_pnl_improvement_pct=cf["improvement_pct"],
        full_sharpe=full_sharpe,
        retained_sharpe=retained_sharpe_v,
        sharpe_uplift=float(sharpe_uplift) if np.isfinite(sharpe_uplift) else -1.0,
        leakage_tests_passed=leakage_tests_passed,
        reasons=reasons,
        provenance=make_provenance({"phase": "0", "thresholds": {
            "skip_accuracy": SKIP_ACCURACY_THRESHOLD,
            "pnl_improvement_pct": PNL_IMPROVEMENT_THRESHOLD_PCT,
            "sharpe_uplift": SHARPE_RATIO_MULTIPLIER,
        }}),
        bucket_breakdown=bucket.reset_index().to_dict(orient="records"),
        counterfactual=cf,
    )

    artifact_path = Path(artifact_path) if artifact_path else Path("artifacts/phase0_verdict.json")
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(asdict(verdict), indent=2, default=str))

    log.info(
        f"Phase 0 verdict: {verdict_str.upper()} | "
        f"skip-acc bottom decile {bottom_decile_skip:.1%} | "
        f"P&L Δ {cf['improvement_pct']:+.1f}% | "
        f"Sharpe uplift {sharpe_uplift:.2f}x"
    )
    if reasons:
        for r in reasons:
            log.warning(f"  - {r}")

    return verdict


def run_directional_phase0_verdict(
    labels: pd.DataFrame,
    predictions: pd.DataFrame,
    *,
    leakage_tests_passed: bool,
    instrument_independence_tests_passed: bool,
    artifact_path: Path | str | None = None,
) -> DirectionalPhase0Verdict:
    """Compute the directional Phase-0 verdict from out-of-sample predictions."""
    metrics = directional_classification_metrics(labels, predictions)
    ev = acted_ev(labels, predictions, slippage_multiplier=2.0)

    none_recall = float(metrics["none_recall"])
    up_precision = float(metrics["up_precision"])
    down_precision = float(metrics["down_precision"])
    acted_ev_atr_2x = float(ev["acted_ev_atr"])

    reasons = []
    if none_recall < NONE_RECALL_THRESHOLD:
        reasons.append(f"none recall {none_recall:.1%} < required {NONE_RECALL_THRESHOLD:.0%}")
    if up_precision < UP_DOWN_PRECISION_THRESHOLD:
        reasons.append(f"up precision {up_precision:.1%} < required {UP_DOWN_PRECISION_THRESHOLD:.0%}")
    if down_precision < UP_DOWN_PRECISION_THRESHOLD:
        reasons.append(f"down precision {down_precision:.1%} < required {UP_DOWN_PRECISION_THRESHOLD:.0%}")
    if acted_ev_atr_2x <= ACTED_EV_THRESHOLD_ATR:
        reasons.append(f"acted EV at 2x slippage {acted_ev_atr_2x:.3f} ATR <= 0")
    if not leakage_tests_passed:
        reasons.append("Leakage tests not passed.")
    if not instrument_independence_tests_passed:
        reasons.append("Instrument-independence tests not passed.")

    verdict = DirectionalPhase0Verdict(
        verdict="go" if not reasons else "no-go",
        none_recall=none_recall,
        up_precision=up_precision,
        down_precision=down_precision,
        acted_ev_atr_2x_slippage=acted_ev_atr_2x,
        leakage_tests_passed=leakage_tests_passed,
        instrument_independence_tests_passed=instrument_independence_tests_passed,
        reasons=reasons,
        metrics=metrics,
        acted_ev=ev,
        provenance=make_provenance({
            "phase": "0_directional",
            "thresholds": {
                "none_recall": NONE_RECALL_THRESHOLD,
                "up_down_precision": UP_DOWN_PRECISION_THRESHOLD,
                "acted_ev_atr": ACTED_EV_THRESHOLD_ATR,
            },
        }),
    )

    artifact_path = (
        Path(artifact_path) if artifact_path else Path("artifacts/directional_phase0_verdict.json")
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(asdict(verdict), indent=2, default=str))
    log.info(
        f"Directional Phase 0 verdict: {verdict.verdict.upper()} | "
        f"none recall {none_recall:.1%} | "
        f"up/down precision {up_precision:.1%}/{down_precision:.1%} | "
        f"acted EV {acted_ev_atr_2x:.3f} ATR"
    )
    return verdict
