"""Train LightGBM classifier (p_win) + regressor (expected_net_R) and save artifact.

Walk-forward validation on the held-out tail before promotion. Artifact layout:

    artifacts/<artifact_id>/
        classifier.txt
        regressor.txt
        metadata.json
        provenance.json
        walk_forward_report.json
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from sniper_paper.common.logging import get_logger
from sniper_paper.common.settings import Settings
from sniper_paper.training.build_dataset import TrainingRow

log = get_logger(__name__)


def _rows_to_frame(rows: list[TrainingRow], feature_order: list[str]) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    df = pd.DataFrame([
        {**r.features, "decision_ts": r.decision_ts, "net_R": r.net_R, "outcome": r.outcome}
        for r in rows
    ])
    X = df[feature_order].astype(float)
    y_cls = (df["net_R"] > 0).astype(int)
    y_reg = df["net_R"].astype(float)
    return df, X, y_cls, y_reg


def _walk_forward_eval(
    df: pd.DataFrame, X: pd.DataFrame, y_cls: pd.Series, y_reg: pd.Series,
    train_months: int = 6, test_months: int = 1,
) -> dict:
    """Single rolling split walk-forward. Returns aggregate metrics."""
    df = df.copy().reset_index(drop=True)
    df["month"] = pd.to_datetime(df["decision_ts"]).dt.to_period("M")
    months = sorted(df["month"].unique())
    if len(months) < train_months + test_months:
        return {"skipped": True, "reason": "not_enough_months", "n_months": len(months)}

    all_preds = []
    for i in range(train_months, len(months) - test_months + 1):
        train_months_set = months[i - train_months : i]
        test_months_set = months[i : i + test_months]
        tr_mask = df["month"].isin(train_months_set)
        te_mask = df["month"].isin(test_months_set)
        if not tr_mask.any() or not te_mask.any():
            continue

        cls = lgb.train(
            {"objective": "binary", "metric": "binary_logloss", "verbosity": -1, "num_leaves": 31},
            lgb.Dataset(X[tr_mask], label=y_cls[tr_mask]),
            num_boost_round=200,
        )
        reg = lgb.train(
            {"objective": "regression", "metric": "rmse", "verbosity": -1, "num_leaves": 31},
            lgb.Dataset(X[tr_mask], label=y_reg[tr_mask]),
            num_boost_round=200,
        )
        p_win = cls.predict(X[te_mask])
        exp_R = reg.predict(X[te_mask])
        actual_R = y_reg[te_mask].values
        all_preds.append(pd.DataFrame({
            "p_win": p_win, "expected_R": exp_R, "actual_R": actual_R,
        }))

    if not all_preds:
        return {"skipped": True, "reason": "no_folds"}

    preds = pd.concat(all_preds, ignore_index=True)
    # EV-ranked skip accuracy on bottom decile
    n = len(preds)
    k = max(1, n // 10)
    skipped = preds.nsmallest(k, "expected_R")
    skip_acc_by_ev = float((skipped["actual_R"] <= 0).mean())
    # PF on top 9 deciles
    traded = preds[~preds.index.isin(skipped.index)]
    wins = traded[traded["actual_R"] > 0]["actual_R"].sum()
    losses = abs(traded[traded["actual_R"] < 0]["actual_R"].sum())
    pf = float(wins / losses) if losses > 0 else float("inf")

    return {
        "n_test_predictions": int(n),
        "skip_accuracy_by_ev": skip_acc_by_ev,
        "profit_factor_traded_set": pf,
        "mean_net_R_traded": float(traded["actual_R"].mean()) if len(traded) else 0.0,
        "win_rate_traded": float((traded["actual_R"] > 0).mean()) if len(traded) else 0.0,
    }


def train_and_save(
    rows: list[TrainingRow], settings: Settings, notes: str = "",
) -> Path:
    feature_order = settings.model.predict_features
    df, X, y_cls, y_reg = _rows_to_frame(rows, feature_order)
    log.info("Training on %d rows, %d features", len(df), len(feature_order))

    wf = _walk_forward_eval(df, X, y_cls, y_reg)
    log.info("Walk-forward: %s", wf)

    # Train final model on ALL data for production use.
    cls = lgb.train(
        {"objective": "binary", "metric": "binary_logloss", "verbosity": -1, "num_leaves": 31},
        lgb.Dataset(X, label=y_cls),
        num_boost_round=200,
    )
    reg = lgb.train(
        {"objective": "regression", "metric": "rmse", "verbosity": -1, "num_leaves": 31},
        lgb.Dataset(X, label=y_reg),
        num_boost_round=200,
    )

    # Artifact directory.
    artifact_id = f"nifty_candle_v0_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    out_dir = Path(settings.model.artifact_dir) / artifact_id
    out_dir.mkdir(parents=True, exist_ok=True)

    cls.save_model(str(out_dir / "classifier.txt"))
    reg.save_model(str(out_dir / "regressor.txt"))

    metadata = {
        "artifact_id": artifact_id,
        "trained_at": datetime.now().isoformat(),
        "feature_order": feature_order,
        "n_training_rows": int(len(df)),
        "instruments_trained_on": ["NIFTY"],
        "model_type": "lightgbm_candle_v0",
        "notes": notes,
    }
    (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))
    (out_dir / "walk_forward_report.json").write_text(json.dumps(wf, indent=2, default=str))

    provenance = {
        "git_sha": _git_sha(),
        "config_hash": _hash_settings(settings),
        "training_rows_hash": _hash_df(df),
        "trained_at": datetime.now().isoformat(),
    }
    (out_dir / "provenance.json").write_text(json.dumps(provenance, indent=2))

    log.info("Saved artifact: %s", out_dir)
    return out_dir


def promote(artifact_dir: Path, settings: Settings) -> None:
    pointer = Path(settings.model.active_model_pointer)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(str(artifact_dir.resolve()))
    log.info("Promoted %s -> ACTIVE", artifact_dir.name)


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return "unknown"


def _hash_settings(settings: Settings) -> str:
    return hashlib.sha256(settings.model_dump_json().encode()).hexdigest()[:12]


def _hash_df(df: pd.DataFrame) -> str:
    return hashlib.sha256(pd.util.hash_pandas_object(df, index=True).values.tobytes()).hexdigest()[:12]
