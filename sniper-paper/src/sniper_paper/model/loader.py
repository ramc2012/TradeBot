"""Load the active model artifact — LightGBM or SniperNet, transparently.

The ACTIVE pointer (configs/paper.yaml → model.active_model_pointer) holds a path
to an artifact directory. `metadata.json["model_type"]` decides how to load it:

  - "lightgbm_candle_v0" : classifier.txt + regressor.txt (LightGBM boosters)
  - "sniper_net_v1"      : model.pt + scaler.json (PyTorch SniperNet)

`predict_one(model, x)` returns (p_win, expected_net_R) for either backend, so the
signal engine and runner are agnostic to which model is active.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np


@dataclass
class ActiveModel:
    model_type: str
    feature_order: list[str]
    artifact_dir: Path
    metadata: dict
    # LightGBM backend
    classifier: Any | None = None
    regressor: Any | None = None
    # NN backend
    net: Any | None = None
    scaler_mean: np.ndarray | None = None
    scaler_scale: np.ndarray | None = None
    _torch: Any = field(default=None, repr=False)

    @property
    def artifact_id(self) -> str:
        return self.artifact_dir.name

    @property
    def is_nn(self) -> bool:
        return self.model_type.startswith("sniper_net")


def _resolve_artifact_dir(pointer_path: str | Path) -> Path:
    pointer = Path(pointer_path)
    if not pointer.exists():
        raise FileNotFoundError(
            f"ACTIVE pointer not found at {pointer}. "
            "Train and promote a model first."
        )
    artifact_dir = Path(pointer.read_text().strip())
    if not artifact_dir.is_absolute():
        artifact_dir = pointer.parent / artifact_dir
    return artifact_dir


def load_active(pointer_path: str | Path) -> ActiveModel:
    artifact_dir = _resolve_artifact_dir(pointer_path)
    meta_path = artifact_dir / "metadata.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"Model artifact missing metadata: {meta_path}")
    metadata = json.loads(meta_path.read_text())
    model_type = metadata.get("model_type", "lightgbm_candle_v0")
    feature_order = metadata["feature_order"]

    if model_type.startswith("sniper_net"):
        return _load_nn(artifact_dir, metadata, feature_order)
    return _load_lgbm(artifact_dir, metadata, feature_order)


def _load_lgbm(artifact_dir: Path, metadata: dict, feature_order: list[str]) -> ActiveModel:
    import lightgbm as lgb

    cls_path = artifact_dir / "classifier.txt"
    reg_path = artifact_dir / "regressor.txt"
    for p in (cls_path, reg_path):
        if not p.exists():
            raise FileNotFoundError(f"LightGBM artifact missing: {p}")
    return ActiveModel(
        model_type=metadata.get("model_type", "lightgbm_candle_v0"),
        feature_order=feature_order,
        artifact_dir=artifact_dir,
        metadata=metadata,
        classifier=lgb.Booster(model_file=str(cls_path)),
        regressor=lgb.Booster(model_file=str(reg_path)),
    )


def _load_nn(artifact_dir: Path, metadata: dict, feature_order: list[str]) -> ActiveModel:
    import torch

    from sniper_paper.model.sniper_net import SniperNet, SniperNetConfig

    ckpt = torch.load(artifact_dir / "model.pt", map_location="cpu", weights_only=False)
    cfg = SniperNetConfig(**ckpt["config"])
    net = SniperNet(cfg)
    net.load_state_dict(ckpt["state_dict"])
    net.eval()

    scaler = json.loads((artifact_dir / "scaler.json").read_text())
    return ActiveModel(
        model_type=metadata.get("model_type", "sniper_net_v1"),
        feature_order=feature_order,
        artifact_dir=artifact_dir,
        metadata=metadata,
        net=net,
        scaler_mean=np.array(scaler["mean"], dtype=np.float64),
        scaler_scale=np.array(scaler["scale"], dtype=np.float64),
        _torch=torch,
    )


def _scale_nn(model: ActiveModel, x: np.ndarray) -> np.ndarray:
    """Match the training-time scaler: sanitise non-finite, standardise, clip ±10."""
    xf = np.asarray(x, dtype=np.float64).copy()
    xf[~np.isfinite(xf)] = np.nan
    xs = (xf - model.scaler_mean) / model.scaler_scale
    xs = np.nan_to_num(xs, nan=0.0, posinf=0.0, neginf=0.0)
    return np.clip(xs, -10.0, 10.0)


def predict_one(model: ActiveModel, feature_array: np.ndarray) -> tuple[float, float]:
    """Return (p_win, expected_net_R) for either backend."""
    x = feature_array.reshape(1, -1)
    if model.is_nn:
        xs = _scale_nn(model, x)
        with model._torch.no_grad():
            out = model.net(model._torch.tensor(xs.astype(np.float32)))
        return float(out["p_win"][0]), float(out["expected_R"][0])
    p_win = float(model.classifier.predict(x)[0])
    expected_R = float(model.regressor.predict(x)[0])
    return p_win, expected_R


def predict_full(model: ActiveModel, feature_array: np.ndarray) -> dict:
    """Richer prediction including MFE/MAE heads (NN only) + latent for viz."""
    x = feature_array.reshape(1, -1)
    if not model.is_nn:
        p, r = predict_one(model, feature_array)
        return {"p_win": p, "expected_R": r, "mfe": None, "mae": None, "latent": None}
    xs = (np.nan_to_num(x, nan=0.0) - model.scaler_mean) / model.scaler_scale
    with model._torch.no_grad():
        out = model.net(model._torch.tensor(xs.astype(np.float32)), return_internals=True)
    return {
        "p_win": float(out["p_win"][0]),
        "expected_R": float(out["expected_R"][0]),
        "mfe": float(out["mfe"][0]),
        "mae": float(out["mae"][0]),
        "latent": out["latent"][0].tolist(),
        "activations": [a[0].tolist() for a in out["activations"]],
    }
