"""Skip-accuracy — Phase 0's headline metric.

The **primary** ranking is by `expected_net_R` (EV), not `p_win`. A 70%-confidence
trade with low payoff can be net-negative; a 45%-confidence trade with large
payoff can be net-positive. We skip on EV.

`skip_accuracy_by_pwin` is reported as a secondary diagnostic only.

The decision gate (configs/base.yaml) requires `skip_accuracy_by_ev` on the
bottom decile to be >= 0.65 in aggregate across folds for Phase 0 to pass.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from sniper_phase0.evaluation.walk_forward import FoldResult


def _bottom_decile(predictions: pd.DataFrame, rank_col: str) -> pd.DataFrame:
    n = len(predictions)
    k = max(1, n // 10)
    return predictions.nsmallest(k, rank_col)


def skip_accuracy_by_ev(predictions: pd.DataFrame) -> float:
    """Primary metric: rank by expected_net_R, fraction of bottom decile that lost."""
    if predictions.empty:
        return float("nan")
    skipped = _bottom_decile(predictions, "expected_net_R")
    losers = (skipped["net_R"] <= 0).sum()
    return float(losers) / float(len(skipped))


def skip_accuracy_by_pwin(predictions: pd.DataFrame) -> float:
    """Secondary diagnostic: rank by p_win."""
    if predictions.empty:
        return float("nan")
    skipped = _bottom_decile(predictions, "p_win")
    losers = (skipped["net_R"] <= 0).sum()
    return float(losers) / float(len(skipped))


def aggregate_skip_accuracy(folds: list[FoldResult]) -> pd.DataFrame:
    rows = []
    for f in folds:
        rows.append(
            {
                "test_start": f.test_start,
                "test_end": f.test_end,
                "n_trades": len(f.predictions),
                "skip_accuracy_by_ev": skip_accuracy_by_ev(f.predictions),
                "skip_accuracy_by_pwin": skip_accuracy_by_pwin(f.predictions),
                "mean_net_R": float(f.predictions["net_R"].mean()),
                "win_rate": float((f.predictions["net_R"] > 0).mean()),
            }
        )
    return pd.DataFrame(rows)


def profit_factor(predictions: pd.DataFrame, slippage_multiplier: float = 1.0) -> float:
    """Profit factor on traded set = sum(wins) / |sum(losses)|.

    `slippage_multiplier` is informational — the caller is expected to have
    already labelled with the desired multiplier. Passed through for reporting.
    """
    wins = predictions.loc[predictions["net_R"] > 0, "net_R"].sum()
    losses = predictions.loc[predictions["net_R"] <= 0, "net_R"].sum()
    if losses == 0:
        return float("inf")
    return float(wins / abs(losses))


def max_drawdown_R(predictions: pd.DataFrame) -> float:
    """Max drawdown in R-units across the ordered prediction sequence."""
    if predictions.empty:
        return 0.0
    s = predictions.sort_values("trade_id")["net_R"].cumsum().to_numpy()
    peak = np.maximum.accumulate(s)
    dd = (s - peak).min()
    return float(dd)
