"""Train SniperNet (multi-head NN) and save artifact.

Artifact layout (parallel to the LightGBM one, distinguished by metadata.model_type):

    artifacts/<artifact_id>/
        model.pt              # torch state_dict + config
        scaler.json           # StandardScaler mean/scale (no sklearn needed at inference)
        metadata.json         # feature_order, model_type="sniper_net_v1", provenance
        walk_forward_report.json
        history.json          # per-epoch loss curves for the final fit (viz)
        provenance.json
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from sniper_paper.common.logging import get_logger
from sniper_paper.common.settings import Settings
from sniper_paper.model.sniper_net import SniperNet, SniperNetConfig, multitask_loss
from sniper_paper.training.build_dataset import TrainingRow

log = get_logger(__name__)

torch.manual_seed(7)
np.random.seed(7)


def _rows_to_frame(rows: list[TrainingRow], feature_order: list[str]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            **r.features,
            "decision_ts": r.decision_ts,
            "net_R": r.net_R,
            "mae_R": abs(r.mae_R),   # store as positive magnitude
            "mfe_R": abs(r.mfe_R),
            "outcome": r.outcome,
        }
        for r in rows
    ])


def _fit_scaler(X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Compute mean/std ignoring non-finite values. Returns (mean, std)."""
    Xf = np.asarray(X, dtype=np.float64).copy()
    Xf[~np.isfinite(Xf)] = np.nan
    mean = np.nanmean(Xf, axis=0)
    mean = np.where(np.isfinite(mean), mean, 0.0)
    std = np.nanstd(Xf, axis=0)
    std = np.where(np.isfinite(std) & (std > 0), std, 1.0)
    return mean, std


def _apply_scaler(X: np.ndarray, mean: np.ndarray, std: np.ndarray) -> np.ndarray:
    """Standardise, then sanitise any remaining non-finite values and clip outliers.
    Clipping to ±10σ keeps a single extreme feature from blowing up BatchNorm."""
    Xf = np.asarray(X, dtype=np.float64).copy()
    Xf[~np.isfinite(Xf)] = np.nan
    Xs = (Xf - mean) / std
    Xs = np.nan_to_num(Xs, nan=0.0, posinf=0.0, neginf=0.0)
    return np.clip(Xs, -10.0, 10.0)


def _standardise(X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mean, std = _fit_scaler(X)
    return _apply_scaler(X, mean, std), mean, std


def _make_targets(df: pd.DataFrame) -> dict[str, torch.Tensor]:
    return {
        "win": torch.tensor((df["net_R"].values > 0).astype(np.float32)),
        "net_R": torch.tensor(df["net_R"].values.astype(np.float32)),
        "mae_R": torch.tensor(df["mae_R"].values.astype(np.float32)),
        "mfe_R": torch.tensor(df["mfe_R"].values.astype(np.float32)),
    }


def _train_one(
    X: np.ndarray, df: pd.DataFrame, cfg: SniperNetConfig,
    epochs: int, lr: float, turnover_penalty: float,
    record_history: bool = False,
) -> tuple[SniperNet, list[dict]]:
    net = SniperNet(cfg)
    opt = torch.optim.Adam(net.parameters(), lr=lr, weight_decay=1e-4)
    xt = torch.tensor(X.astype(np.float32))
    targets = _make_targets(df)
    history: list[dict] = []

    net.train()
    for epoch in range(epochs):
        opt.zero_grad()
        out = net(xt)
        loss, parts = multitask_loss(out, targets, turnover_penalty=turnover_penalty)
        loss.backward()
        opt.step()
        if record_history:
            history.append({"epoch": epoch, **parts})
    return net, history


def _walk_forward_eval(
    df: pd.DataFrame, feature_order: list[str], cfg_template: SniperNetConfig,
    epochs: int, lr: float, turnover_penalty: float,
    train_months: int = 6, test_months: int = 1,
) -> dict:
    df = df.copy().reset_index(drop=True)
    df["month"] = pd.to_datetime(df["decision_ts"]).dt.to_period("M")
    months = sorted(df["month"].unique())
    if len(months) < train_months + test_months:
        return {"skipped": True, "reason": "not_enough_months", "n_months": len(months)}

    all_preds = []
    for i in range(train_months, len(months) - test_months + 1):
        tr_mask = df["month"].isin(months[i - train_months:i])
        te_mask = df["month"].isin(months[i:i + test_months])
        if not tr_mask.any() or not te_mask.any():
            continue

        Xtr_raw = df.loc[tr_mask, feature_order].astype(float).values
        Xtr, mean, std = _standardise(Xtr_raw)
        net, _ = _train_one(Xtr, df[tr_mask], cfg_template, epochs, lr, turnover_penalty)

        Xte_raw = df.loc[te_mask, feature_order].astype(float).values
        Xte = _apply_scaler(Xte_raw, mean, std)
        net.eval()
        with torch.no_grad():
            out = net(torch.tensor(Xte.astype(np.float32)))
        all_preds.append(pd.DataFrame({
            "p_win": out["p_win"].numpy(),
            "expected_R": out["expected_R"].numpy(),
            "actual_R": df.loc[te_mask, "net_R"].values,
        }))

    if not all_preds:
        return {"skipped": True, "reason": "no_folds"}

    preds = pd.concat(all_preds, ignore_index=True)
    n = len(preds)
    k = max(1, n // 10)
    skipped = preds.nsmallest(k, "expected_R")
    skip_acc = float((skipped["actual_R"] <= 0).mean())
    traded = preds[~preds.index.isin(skipped.index)]
    wins = traded[traded["actual_R"] > 0]["actual_R"].sum()
    losses = abs(traded[traded["actual_R"] < 0]["actual_R"].sum())
    pf = float(wins / losses) if losses > 0 else float("inf")
    return {
        "n_test_predictions": int(n),
        "skip_accuracy_by_ev": skip_acc,
        "profit_factor_traded_set": pf,
        "mean_net_R_traded": float(traded["actual_R"].mean()) if len(traded) else 0.0,
        "win_rate_traded": float((traded["actual_R"] > 0).mean()) if len(traded) else 0.0,
    }


def train_and_save_nn(
    rows: list[TrainingRow], settings: Settings, notes: str = "",
    epochs: int = 300, lr: float = 1e-3, turnover_penalty: float = 0.05,
) -> Path:
    feature_order = settings.model.predict_features
    df = _rows_to_frame(rows, feature_order)
    log.info("NN training on %d rows, %d features", len(df), len(feature_order))

    cfg = SniperNetConfig(n_features=len(feature_order))

    wf = _walk_forward_eval(df, feature_order, cfg, epochs, lr, turnover_penalty)
    log.info("NN walk-forward: %s", wf)

    # Final fit on all data (with history for the viz).
    X_raw = df[feature_order].astype(float).values
    X, mean, std = _standardise(X_raw)
    net, history = _train_one(X, df, cfg, epochs, lr, turnover_penalty, record_history=True)

    artifact_id = f"nifty_nn_v1_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    out_dir = Path(settings.model.artifact_dir) / artifact_id
    out_dir.mkdir(parents=True, exist_ok=True)

    torch.save(
        {"state_dict": net.state_dict(), "config": cfg.__dict__},
        out_dir / "model.pt",
    )
    (out_dir / "scaler.json").write_text(json.dumps({
        "mean": mean.tolist(), "scale": std.tolist(), "feature_order": feature_order,
    }))
    (out_dir / "metadata.json").write_text(json.dumps({
        "artifact_id": artifact_id,
        "trained_at": datetime.now().isoformat(),
        "feature_order": feature_order,
        "n_training_rows": int(len(df)),
        "instruments_trained_on": ["NIFTY"],
        "model_type": "sniper_net_v1",
        "architecture": {
            "encoder_blocks": cfg.n_encoder_blocks,
            "hidden": cfg.hidden,
            "latent": cfg.latent,
            "dropout": cfg.dropout,
            "heads": ["p_win", "expected_R", "mfe", "mae"],
            "n_params": int(sum(p.numel() for p in net.parameters())),
        },
        "hyperparams": {"epochs": epochs, "lr": lr, "turnover_penalty": turnover_penalty},
        "notes": notes,
    }, indent=2))
    (out_dir / "walk_forward_report.json").write_text(json.dumps(wf, indent=2, default=str))
    (out_dir / "history.json").write_text(json.dumps(history, indent=2))
    (out_dir / "provenance.json").write_text(json.dumps({
        "git_sha": _git_sha(),
        "config_hash": hashlib.sha256(settings.model_dump_json().encode()).hexdigest()[:12],
        "trained_at": datetime.now().isoformat(),
    }, indent=2))

    log.info("Saved NN artifact: %s", out_dir)
    return out_dir


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return "unknown"
