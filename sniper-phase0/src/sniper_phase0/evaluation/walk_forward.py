"""Walk-forward evaluation harness.

No random shuffles. No k-fold. Every test period is strictly forward of train.

**Purging:** triple-barrier labels span time. A training sample whose label
window (entry_ts → exit_ts) ends inside the validation or test window leaks
forward information. We drop those training rows. `purge_minutes` is an
additional safety margin beyond the actual exit_ts.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from sniper_phase0.models.lightgbm_baseline import BaselineModel, predict, train_baseline
from sniper_phase0.utils.settings import Settings
from sniper_phase0.utils.time import walk_forward_splits


@dataclass
class FoldResult:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    val_start: pd.Timestamp
    val_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    predictions: pd.DataFrame
    n_train: int
    n_train_purged: int
    model: BaselineModel = field(repr=False)


def _slice(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    return df[(df["decision_ts"] >= start) & (df["decision_ts"] < end)]


def _purge_training(
    features: pd.DataFrame,
    labels: pd.DataFrame,
    forward_boundary: pd.Timestamp,
    purge_minutes: int,
) -> tuple[pd.DataFrame, int]:
    """Drop training rows whose label exit_ts (+ margin) crosses forward_boundary."""
    if "exit_ts" not in labels.columns:
        return features, 0
    margin = pd.Timedelta(minutes=purge_minutes)
    label_exit = labels.set_index("trade_id")["exit_ts"]
    label_exit = pd.to_datetime(label_exit)
    aligned = features.merge(
        label_exit.rename("exit_ts").reset_index(), on="trade_id", how="left"
    )
    keep_mask = aligned["exit_ts"].fillna(pd.Timestamp.min) + margin < forward_boundary
    purged = int((~keep_mask).sum())
    return features[keep_mask.values].copy(), purged


def run_walk_forward(
    features: pd.DataFrame,
    labels: pd.DataFrame,
    settings: Settings,
    purge_minutes: int | None = None,
) -> list[FoldResult]:
    if purge_minutes is None:
        purge_minutes = getattr(settings.walk_forward, "purge_minutes", settings.labeling.max_hold_minutes)

    splits = walk_forward_splits(
        settings.walk_forward.start,
        settings.walk_forward.end,
        settings.walk_forward.train_months,
        settings.walk_forward.validate_months,
        settings.walk_forward.test_months,
        settings.walk_forward.step_months,
    )

    label_idx = labels.set_index("trade_id")
    results: list[FoldResult] = []
    for tr_s, tr_e, va_s, va_e, te_s, te_e in splits:
        f_train_raw = _slice(features, tr_s, tr_e)
        f_val = _slice(features, va_s, va_e)
        f_test = _slice(features, te_s, te_e)
        if f_train_raw.empty or f_test.empty:
            continue

        f_train, purged_n = _purge_training(f_train_raw, labels, va_s, purge_minutes)
        if f_train.empty:
            continue

        l_train = label_idx.loc[f_train["trade_id"]].reset_index()
        l_val = label_idx.loc[f_val["trade_id"]].reset_index() if not f_val.empty else None
        l_test = label_idx.loc[f_test["trade_id"]].reset_index()

        model = train_baseline(f_train, l_train, f_val, l_val)
        preds = predict(model, f_test)
        preds = preds.merge(
            l_test[["trade_id", "net_R", "outcome"]], on="trade_id", how="left"
        )
        results.append(
            FoldResult(
                train_start=tr_s, train_end=tr_e,
                val_start=va_s, val_end=va_e,
                test_start=te_s, test_end=te_e,
                predictions=preds,
                n_train=len(f_train),
                n_train_purged=purged_n,
                model=model,
            )
        )
    return results
