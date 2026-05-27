"""Go/no-go report generation.

Primary skip metric: EV-ranked (skip_accuracy_by_ev). p_win-ranked is reported
as a secondary diagnostic. Regime breakdown is reported but does not gate the
overall pass/fail — it's diagnostic for the fail-triage workflow.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from sniper_phase0.evaluation.regime import skip_accuracy_by_regime
from sniper_phase0.evaluation.skip_accuracy import (
    aggregate_skip_accuracy,
    max_drawdown_R,
    profit_factor,
    skip_accuracy_by_ev,
    skip_accuracy_by_pwin,
)
from sniper_phase0.evaluation.walk_forward import FoldResult
from sniper_phase0.utils.settings import Settings


def build_report(
    folds: list[FoldResult],
    settings: Settings,
    features: pd.DataFrame | None = None,
) -> dict:
    per_fold = aggregate_skip_accuracy(folds)
    all_preds = (
        pd.concat([f.predictions for f in folds], ignore_index=True)
        if folds else pd.DataFrame()
    )

    overall_skip_ev = skip_accuracy_by_ev(all_preds) if not all_preds.empty else float("nan")
    overall_skip_pwin = skip_accuracy_by_pwin(all_preds) if not all_preds.empty else float("nan")
    pf = profit_factor(all_preds) if not all_preds.empty else float("nan")
    mdd_R = max_drawdown_R(all_preds) if not all_preds.empty else 0.0

    regime_df = (
        skip_accuracy_by_regime(all_preds, features)
        if features is not None and not all_preds.empty
        else pd.DataFrame()
    )

    gate = settings.decision_gate
    passes = {
        "skip_accuracy_gate": (
            overall_skip_ev == overall_skip_ev  # not nan
            and overall_skip_ev >= gate.skip_accuracy_bottom_decile_min
        ),
        "profit_factor_gate": pf >= gate.net_profit_factor_min_at_2x_slippage,
        "max_drawdown_gate": abs(mdd_R) <= gate.max_drawdown_pct_max,
    }

    purge_totals = {
        "n_train_purged_total": int(sum(f.n_train_purged for f in folds)),
        "n_train_total": int(sum(f.n_train for f in folds)),
    }

    return {
        "n_folds": len(folds),
        "n_test_trades": int(len(all_preds)),
        "overall_skip_accuracy_by_ev": overall_skip_ev,
        "overall_skip_accuracy_by_pwin_diagnostic": overall_skip_pwin,
        "overall_profit_factor": pf,
        "overall_max_drawdown_R": mdd_R,
        "purging": purge_totals,
        "per_fold": per_fold.to_dict(orient="records"),
        "per_regime": regime_df.to_dict(orient="records"),
        "gate": {
            "thresholds": gate.model_dump(),
            "passes": passes,
            "phase0_pass": all(passes.values()),
        },
    }


def write_report(report: dict, out_dir: str | Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "phase0_report.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    return path
