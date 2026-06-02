"""Metrics that matter for Phase 0.

Skip-accuracy and counterfactual P&L are the headline numbers. Sharpe is secondary but
required by the verdict criteria.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from nomad_sniper.labels.directional import CLASS_TO_DIRECTION, DIRECTION_TO_CLASS


def skip_accuracy_by_quality_bucket(
    labels: pd.DataFrame,
    skip_decisions: pd.Series,
    *,
    bucket_col: str = "pnl_decile",
) -> pd.DataFrame:
    """For each P&L quality bucket, what fraction of trades did the model recommend skipping?

    Good model: high skip rate in low-quality buckets, low skip rate in high-quality buckets.
    """
    if not skip_decisions.index.equals(labels.index):
        common = labels.index.intersection(skip_decisions.index)
        labels = labels.loc[common]
        skip_decisions = skip_decisions.loc[common]

    df = labels[[bucket_col, "net_pnl"]].copy()
    df["skip"] = skip_decisions.values

    grouped = df.groupby(bucket_col).agg(
        n_trades=("skip", "size"),
        n_skipped=("skip", "sum"),
        mean_net_pnl=("net_pnl", "mean"),
        total_net_pnl=("net_pnl", "sum"),
    )
    grouped["skip_rate"] = grouped["n_skipped"] / grouped["n_trades"]
    return grouped


def counterfactual_pnl(
    labels: pd.DataFrame,
    skip_decisions: pd.Series,
) -> dict:
    """If we had honoured the model's skip decisions, what would total P&L have been?"""
    common = labels.index.intersection(skip_decisions.index)
    labels = labels.loc[common]
    skip_decisions = skip_decisions.loc[common]

    actual_pnl = float(labels["net_pnl"].sum())
    taken_mask = skip_decisions == 0
    counterfactual = float(labels.loc[taken_mask, "net_pnl"].sum())
    n_taken = int(taken_mask.sum())
    n_skipped = int((~taken_mask).sum())

    improvement_pct = (
        100 * (counterfactual - actual_pnl) / abs(actual_pnl) if actual_pnl != 0 else 0.0
    )

    return {
        "actual_total_pnl": actual_pnl,
        "counterfactual_total_pnl": counterfactual,
        "improvement_inr": counterfactual - actual_pnl,
        "improvement_pct": improvement_pct,
        "n_trades_taken": n_taken,
        "n_trades_skipped": n_skipped,
        "skip_rate": n_skipped / max(1, len(labels)),
        "retained_win_rate": float((labels.loc[taken_mask, "net_pnl"] > 0).mean()) if n_taken else 0.0,
        "skipped_win_rate": float((labels.loc[~taken_mask, "net_pnl"] > 0).mean()) if n_skipped else 0.0,
    }


def sharpe_ratio(returns: pd.Series, *, periods_per_year: float = 252.0) -> float:
    """Annualised Sharpe of a returns series (e.g. daily P&L / capital)."""
    r = returns.dropna()
    if len(r) < 2 or r.std() == 0:
        return 0.0
    return float(np.sqrt(periods_per_year) * r.mean() / r.std(ddof=1))


def daily_pnl_series(labels: pd.DataFrame, *, mask: pd.Series | None = None) -> pd.Series:
    """Aggregate per-trade net_pnl into a daily P&L series."""
    df = labels.copy()
    if mask is not None:
        df = df.loc[mask]
    if df.empty:
        return pd.Series(dtype=float)
    df["date"] = pd.to_datetime(df["exit_at"]).dt.date
    return df.groupby("date")["net_pnl"].sum()


def cost_sensitivity_sweep(
    counterfactual_func,
    multipliers=(0.5, 1.0, 1.5, 2.0, 3.0),
) -> pd.DataFrame:
    """Run counterfactual P&L across slippage multipliers; return a tidy DataFrame."""
    rows = []
    for m in multipliers:
        result = counterfactual_func(slippage_multiplier=m)
        rows.append({"slippage_multiplier": m, **result})
    return pd.DataFrame(rows)


def directional_classification_metrics(
    labels: pd.DataFrame,
    predictions: pd.DataFrame,
    *,
    truth_col: str = "direction",
    pred_col: str = "pred_direction",
) -> dict:
    """Per-class precision/recall for up/down/none and move precision."""
    common = labels.index.intersection(predictions.index)
    y = labels.loc[common, truth_col].astype(str)
    p = predictions.loc[common, pred_col].astype(str)
    out: dict[str, float | int] = {"n": int(len(common))}
    for cls in ("none", "up", "down"):
        tp = int(((y == cls) & (p == cls)).sum())
        fp = int(((y != cls) & (p == cls)).sum())
        fn = int(((y == cls) & (p != cls)).sum())
        out[f"{cls}_precision"] = tp / max(1, tp + fp)
        out[f"{cls}_recall"] = tp / max(1, tp + fn)
    move_pred = p != "none"
    move_true = y != "none"
    out["is_move_precision"] = float((move_true & move_pred).sum() / max(1, move_pred.sum()))
    out["is_move_recall"] = float((move_true & move_pred).sum() / max(1, move_true.sum()))
    out["accuracy"] = float((y == p).mean()) if len(y) else 0.0
    return out


def predictions_from_direction_proba(
    proba: np.ndarray,
    *,
    threshold: float = 0.5,
) -> pd.Series:
    """Convert class-probability matrix [none, up, down] to direction strings."""
    arr = np.asarray(proba)
    if arr.ndim != 2 or arr.shape[1] != 3:
        raise ValueError("direction probability matrix must have shape (n, 3)")
    move_best = np.maximum(arr[:, DIRECTION_TO_CLASS["up"]], arr[:, DIRECTION_TO_CLASS["down"]])
    classes = arr.argmax(axis=1)
    classes = np.where(move_best >= threshold, classes, DIRECTION_TO_CLASS["none"])
    return pd.Series([CLASS_TO_DIRECTION[int(c)] for c in classes])


def acted_ev(
    labels: pd.DataFrame,
    predictions: pd.DataFrame,
    *,
    pred_col: str = "pred_direction",
    slippage_multiplier: float = 1.0,
    cost_atr: float = 0.05,
) -> dict:
    """Expected ATR P&L from taking every up/down prediction.

    This uses label geometry, not raw money: correct direction earns `magnitude_atr`; wrong move
    loses `mae_atr`; every acted trade pays a configurable ATR cost stress.
    """
    common = labels.index.intersection(predictions.index)
    y = labels.loc[common]
    p = predictions.loc[common, pred_col].astype(str)
    acted = p != "none"
    pnl = []
    for idx in y.index[acted]:
        pred = p.loc[idx]
        truth = str(y.loc[idx, "direction"])
        cost = cost_atr * slippage_multiplier
        if pred == truth and truth != "none":
            pnl.append(float(y.loc[idx, "magnitude_atr"]) - cost)
        else:
            pnl.append(-float(y.loc[idx, "mae_atr"] or 0.0) - cost)
    total = float(np.sum(pnl)) if pnl else 0.0
    return {
        "acted_ev_atr": total / max(1, len(pnl)),
        "acted_total_atr": total,
        "n_acted": int(len(pnl)),
        "coverage": float(len(pnl) / max(1, len(common))),
        "slippage_multiplier": slippage_multiplier,
    }
