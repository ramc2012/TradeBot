"""Metrics that matter for Phase 0.

Skip-accuracy and counterfactual P&L are the headline numbers. Sharpe is secondary but
required by the verdict criteria.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


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


# ─────────────────────────────────────────────────────────────────────
# Directional metrics (contract §8 / amendments Step 8) — the headline set.
# `acted-EV` is the headline number, not accuracy.
# ─────────────────────────────────────────────────────────────────────
from nomad_sniper.models.directional import DIRECTION_CLASSES  # noqa: E402


def per_class_precision_recall(y_true: pd.Series, y_pred: pd.Series) -> dict:
    """Precision/recall/support per direction class (up/down/none)."""
    out: dict[str, dict] = {}
    yt = y_true.astype(str).values
    yp = y_pred.astype(str).values
    for cls in DIRECTION_CLASSES:
        tp = int(((yp == cls) & (yt == cls)).sum())
        fp = int(((yp == cls) & (yt != cls)).sum())
        fn = int(((yp != cls) & (yt == cls)).sum())
        support = int((yt == cls).sum())
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        out[cls] = {"precision": precision, "recall": recall, "support": support,
                    "tp": tp, "fp": fp, "fn": fn}
    return out


def is_move_precision(y_true_dir: pd.Series, y_pred_dir: pd.Series) -> dict:
    """How often a 'move' call (pred up/down) was actually a move (true up/down)."""
    yp = y_pred_dir.astype(str).values
    yt = y_true_dir.astype(str).values
    pred_move = yp != "none"
    true_move = yt != "none"
    n_calls = int(pred_move.sum())
    tp = int((pred_move & true_move).sum())
    return {
        "n_move_calls": n_calls,
        "move_precision": (tp / n_calls) if n_calls else 0.0,
        "move_recall": (tp / int(true_move.sum())) if true_move.sum() else 0.0,
    }


def directional_accuracy_on_calls(y_true_dir: pd.Series, y_pred_dir: pd.Series) -> float:
    """Among rows where the model called up/down, fraction where the direction was right
    (true direction equals predicted, i.e. correct side AND it actually moved)."""
    yp = y_pred_dir.astype(str).values
    yt = y_true_dir.astype(str).values
    called = yp != "none"
    if called.sum() == 0:
        return 0.0
    return float((yp[called] == yt[called]).mean())


def acted_ev(
    predictions: pd.DataFrame,
    *,
    atr_inr: float,
    slippage_multiplier: float = 1.0,
    base_cost_atr: float = 0.15,
    size_col: str | None = "size",
) -> dict:
    """Expected net option P&L (in ATR units) if we took every up/down call at chosen size.

    Honest, simple model: a correct call earns `magnitude_atr` (favourable excursion); a wrong
    call (predicted move that resolved the other way or `none`) loses `mae_atr` (adverse
    excursion). Each acted call pays `base_cost_atr * slippage_multiplier` in costs.

    `predictions` columns required: `pred_direction`, `true_direction`, `magnitude_atr`
    (realized favourable), `mae_atr` (realized adverse). Optional `size` in [0, 1.5].
    The headline is `acted_ev_atr` (mean per-call) and `total_ev_atr`.
    """
    df = predictions
    called = df["pred_direction"].astype(str) != "none"
    acts = df[called]
    if acts.empty:
        return {"n_acted": 0, "acted_ev_atr": 0.0, "total_ev_atr": 0.0,
                "win_rate": 0.0, "slippage_multiplier": slippage_multiplier}

    correct = acts["pred_direction"].astype(str) == acts["true_direction"].astype(str)
    size = acts[size_col] if (size_col and size_col in acts.columns) else 1.0
    gross = np.where(correct, acts["magnitude_atr"].astype(float),
                     -acts["mae_atr"].astype(float).abs())
    cost = base_cost_atr * slippage_multiplier
    net = (gross - cost) * size
    return {
        "n_acted": int(len(acts)),
        "acted_ev_atr": float(np.mean(net)),
        "total_ev_atr": float(np.sum(net)),
        "acted_ev_inr_per_unit": float(np.mean(net) * atr_inr),
        "win_rate": float(correct.mean()),
        "slippage_multiplier": slippage_multiplier,
    }
