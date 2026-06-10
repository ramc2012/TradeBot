"""Phase 0 directional verdict (contract §8) — go/no-go on the directional gate.

Output: JSON to `artifacts/phase0_verdict.json` + a one-line console summary. Verdict is
`go` iff ALL four pre-committed criteria pass. DO NOT change thresholds after seeing results.

Pre-committed criteria:
  1. `none`-class recall ≥ 0.70 — reliably keeps you out of chop.
  2. up/down precision ≥ 0.55 after the option-economics gate.
  3. acted-EV positive at 2× slippage.
  4. leakage + instrument-independence tests pass.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from nomad_sniper.evaluation.metrics import (
    acted_ev,
    directional_accuracy_on_calls,
    is_move_precision,
    per_class_precision_recall,
)
from nomad_sniper.utils.logging import get_logger
from nomad_sniper.utils.provenance import make_provenance

log = get_logger()


# Pre-committed thresholds. DO NOT change after seeing results (see CLAUDE.md / contract §8).
NONE_RECALL_THRESHOLD = 0.70
UPDOWN_PRECISION_THRESHOLD = 0.55
ACTED_EV_SLIPPAGE_MULTIPLIER = 2.0


@dataclass
class Phase0Verdict:
    verdict: str  # "go" | "no-go"
    none_recall: float
    updown_precision: float
    acted_ev_atr_at_2x: float
    leakage_tests_passed: bool
    instrument_independence_passed: bool
    reasons: list[str] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    per_class: dict = field(default_factory=dict)
    is_move: dict = field(default_factory=dict)
    acted_ev_sweep: list[dict] = field(default_factory=list)


def run_phase0_verdict(
    predictions: pd.DataFrame,
    *,
    atr_inr: float = 100.0,
    leakage_tests_passed: bool,
    instrument_independence_passed: bool,
    artifact_path: Path | str | None = None,
) -> Phase0Verdict:
    """Compute the directional Phase 0 verdict from out-of-sample predictions.

    Args:
        predictions: OOS rows with columns `pred_direction`, `true_direction`,
                     `magnitude_atr`, `mae_atr` (and optional `size`).
        atr_inr:     ATR in rupees (for reporting acted-EV in INR); EV gate uses ATR units.
        leakage_tests_passed / instrument_independence_passed: supplied by the caller
                     (run pytest first — these are NOT auto-run here).
    """
    required = {"pred_direction", "true_direction", "magnitude_atr", "mae_atr"}
    missing = required - set(predictions.columns)
    if missing:
        raise ValueError(f"predictions missing columns: {sorted(missing)}")
    if predictions.empty:
        raise ValueError("Empty predictions.")

    pc = per_class_precision_recall(predictions["true_direction"], predictions["pred_direction"])
    none_recall = pc["none"]["recall"]
    updown_precision = directional_accuracy_on_calls(
        predictions["true_direction"], predictions["pred_direction"]
    )
    move = is_move_precision(predictions["true_direction"], predictions["pred_direction"])

    sweep = [
        acted_ev(predictions, atr_inr=atr_inr, slippage_multiplier=m)
        for m in (1.0, 1.5, 2.0, 3.0)
    ]
    ev_2x = next(r["acted_ev_atr"] for r in sweep if r["slippage_multiplier"] == 2.0)

    reasons: list[str] = []
    if none_recall < NONE_RECALL_THRESHOLD:
        reasons.append(f"none-recall {none_recall:.2f} < {NONE_RECALL_THRESHOLD}")
    if updown_precision < UPDOWN_PRECISION_THRESHOLD:
        reasons.append(f"up/down precision {updown_precision:.2f} < {UPDOWN_PRECISION_THRESHOLD}")
    if ev_2x <= 0:
        reasons.append(f"acted-EV at 2x slippage {ev_2x:.4f} ATR ≤ 0")
    if not leakage_tests_passed:
        reasons.append("leakage tests not passed")
    if not instrument_independence_passed:
        reasons.append("instrument-independence test not passed")

    verdict_str = "go" if not reasons else "no-go"
    verdict = Phase0Verdict(
        verdict=verdict_str,
        none_recall=none_recall,
        updown_precision=updown_precision,
        acted_ev_atr_at_2x=ev_2x,
        leakage_tests_passed=leakage_tests_passed,
        instrument_independence_passed=instrument_independence_passed,
        reasons=reasons,
        provenance=make_provenance({"phase": "0-directional", "thresholds": {
            "none_recall": NONE_RECALL_THRESHOLD,
            "updown_precision": UPDOWN_PRECISION_THRESHOLD,
            "acted_ev_slippage_multiplier": ACTED_EV_SLIPPAGE_MULTIPLIER,
        }}),
        per_class=pc,
        is_move=move,
        acted_ev_sweep=sweep,
    )

    artifact_path = Path(artifact_path) if artifact_path else Path("artifacts/phase0_verdict.json")
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(asdict(verdict), indent=2, default=str))

    log.info(
        f"Phase 0 verdict: {verdict_str.upper()} | none-recall {none_recall:.2f} | "
        f"up/down precision {updown_precision:.2f} | acted-EV@2x {ev_2x:+.4f} ATR"
    )
    for r in reasons:
        log.warning(f"  - {r}")
    return verdict
