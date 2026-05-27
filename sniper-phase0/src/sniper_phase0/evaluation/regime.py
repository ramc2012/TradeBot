"""Regime tagging for stratified evaluation.

A profitable Phase 0 result on aggregate can mask regime-specific failure
(or vice versa). We tag each prediction with a regime drawn from features
already in the dataset and report skip-accuracy per regime.

Regimes used in v0:
  - expiry_day:  ctx_is_expiry_day == 1
  - expiry_week: ctx_is_expiry_week == 1 and not expiry_day
  - gap_day:     |ctx_overnight_gap_pct| > 0.5
  - opening_hr:  ctx_minutes_into_session < 60
  - closing_hr:  ctx_minutes_into_session > 315
  - normal:      everything else

Trend-day vs chop-day classification needs intraday OHLC + ATR z-score,
which we don't have without an underlying-OHLC backfill. Add later.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from sniper_phase0.evaluation.skip_accuracy import skip_accuracy_by_ev


def tag_regime(features: pd.DataFrame) -> pd.Series:
    """Return one regime label per row of `features`. Priority: expiry > gap > time-of-day > normal."""
    n = len(features)
    regime = pd.Series(["normal"] * n, index=features.index, dtype=object)

    def col(name: str) -> pd.Series:
        return features[name] if name in features.columns else pd.Series(np.nan, index=features.index)

    gap = col("ctx_overnight_gap_pct").abs() > 0.5
    is_expiry = col("ctx_is_expiry_day") == 1
    is_expiry_wk = (col("ctx_is_expiry_week") == 1) & ~is_expiry
    opening = col("ctx_minutes_into_session") < 60
    closing = col("ctx_minutes_into_session") > 315

    # Apply lowest-priority first; later assignments override.
    regime[closing.fillna(False)] = "closing_hr"
    regime[opening.fillna(False)] = "opening_hr"
    regime[gap.fillna(False)] = "gap_day"
    regime[is_expiry_wk.fillna(False)] = "expiry_week"
    regime[is_expiry.fillna(False)] = "expiry_day"
    return regime


def skip_accuracy_by_regime(
    predictions: pd.DataFrame, features: pd.DataFrame
) -> pd.DataFrame:
    """Skip-accuracy (EV-ranked) computed per regime.

    `predictions` must include trade_id and expected_net_R + net_R columns.
    `features` must include trade_id and the context features used by tag_regime.
    """
    if predictions.empty:
        return pd.DataFrame(columns=["regime", "n", "skip_accuracy_by_ev", "win_rate", "mean_net_R"])

    feats = features[["trade_id"]].copy()
    feats["regime"] = tag_regime(features).values
    merged = predictions.merge(feats, on="trade_id", how="left")

    rows = []
    for regime, group in merged.groupby("regime", dropna=False):
        rows.append(
            {
                "regime": regime,
                "n": int(len(group)),
                "skip_accuracy_by_ev": skip_accuracy_by_ev(group),
                "win_rate": float((group["net_R"] > 0).mean()),
                "mean_net_R": float(group["net_R"].mean()),
            }
        )
    return pd.DataFrame(rows).sort_values("n", ascending=False).reset_index(drop=True)
