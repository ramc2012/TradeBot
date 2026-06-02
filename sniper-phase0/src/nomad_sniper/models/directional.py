"""Multi-head LightGBM directional model.

Heads:
- direction: multiclass none/up/down
- is_move: binary direction != none
- magnitude_atr, time_to_target, mae_atr: regressors trained on move rows only
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

from nomad_sniper.labels.directional import CLASS_TO_DIRECTION, DIRECTION_TO_CLASS
from nomad_sniper.utils.logging import get_logger
from nomad_sniper.utils.provenance import make_provenance

log = get_logger()


@dataclass
class DirectionalModel:
    boosters: dict[str, Any]
    feature_names: list[str]
    categorical_features: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, str] = field(default_factory=dict)

    def predict(self, X: pd.DataFrame) -> dict[str, np.ndarray]:
        lgb = _require_lightgbm()
        Xp = _encode_categoricals(self._align_columns(X).copy(), self.categorical_features)
        direction_proba = self.boosters["direction"].predict(Xp)
        is_move_proba = self.boosters["is_move"].predict(Xp)
        out = {
            "direction_proba": np.asarray(direction_proba),
            "is_move_proba": np.asarray(is_move_proba),
        }
        for head in ("magnitude_atr", "time_to_target", "mae_atr"):
            booster = self.boosters.get(head)
            out[head] = np.asarray(booster.predict(Xp)) if booster is not None else np.full(len(Xp), np.nan)
        classes = out["direction_proba"].argmax(axis=1)
        out["pred_direction_class"] = classes
        out["pred_direction"] = np.asarray([CLASS_TO_DIRECTION[int(c)] for c in classes])
        return out

    def predict_frame(self, X: pd.DataFrame) -> pd.DataFrame:
        pred = self.predict(X)
        df = pd.DataFrame(index=X.index)
        df["pred_direction"] = pred["pred_direction"]
        df["pred_direction_class"] = pred["pred_direction_class"]
        df["p_none"] = pred["direction_proba"][:, DIRECTION_TO_CLASS["none"]]
        df["p_up"] = pred["direction_proba"][:, DIRECTION_TO_CLASS["up"]]
        df["p_down"] = pred["direction_proba"][:, DIRECTION_TO_CLASS["down"]]
        df["p_is_move"] = pred["is_move_proba"]
        df["pred_magnitude_atr"] = pred["magnitude_atr"]
        df["pred_time_to_target"] = pred["time_to_target"]
        df["pred_mae_atr"] = pred["mae_atr"]
        return df

    def save(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self, path)
        log.info(f"Saved DirectionalModel -> {path}")

    @classmethod
    def load(cls, path: Path | str) -> "DirectionalModel":
        return joblib.load(path)

    def _align_columns(self, X: pd.DataFrame) -> pd.DataFrame:
        missing = [c for c in self.feature_names if c not in X.columns]
        if missing:
            raise ValueError(f"Missing features at predict time: {missing[:10]}")
        return X[self.feature_names]


def train_directional_model(
    features: pd.DataFrame,
    labels: pd.DataFrame,
    *,
    feature_columns: list[str] | None = None,
    categorical_features: list[str] | None = None,
    sample_weight: pd.Series | None = None,
    params: dict[str, Any] | None = None,
    num_boost_round: int = 300,
) -> DirectionalModel:
    lgb = _require_lightgbm()
    categorical_features = categorical_features or []
    common = features.index.intersection(labels.index)
    features = features.loc[common]
    labels = labels.loc[common]
    if feature_columns is None:
        feature_columns = _auto_feature_columns(features, categorical_features)

    X = _encode_categoricals(features[feature_columns].copy(), categorical_features)
    weights = sample_weight.loc[X.index] if sample_weight is not None else labels.get("sample_weight")
    if weights is not None:
        weights = pd.Series(weights, index=X.index).fillna(1.0).astype(float)

    default = {
        "learning_rate": 0.03,
        "num_leaves": 31,
        "min_data_in_leaf": 10,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.9,
        "bagging_freq": 3,
        "verbose": -1,
        "seed": 42,
    }
    default.update(params or {})

    boosters: dict[str, Any] = {}
    dir_params = {**default, "objective": "multiclass", "num_class": 3, "metric": "multi_logloss"}
    boosters["direction"] = lgb.train(
        dir_params,
        lgb.Dataset(
            X,
            label=labels["direction_class"].astype(int),
            weight=weights,
            categorical_feature=categorical_features,
        ),
        num_boost_round=num_boost_round,
        callbacks=[lgb.log_evaluation(period=0)],
    )

    bin_params = {**default, "objective": "binary", "metric": "binary_logloss"}
    boosters["is_move"] = lgb.train(
        bin_params,
        lgb.Dataset(
            X,
            label=labels["is_move"].astype(int),
            weight=weights,
            categorical_feature=categorical_features,
        ),
        num_boost_round=num_boost_round,
        callbacks=[lgb.log_evaluation(period=0)],
    )

    move_idx = labels.index[labels["is_move"].astype(int) == 1]
    reg_params = {**default, "objective": "regression", "metric": "l2"}
    for head in ("magnitude_atr", "time_to_target", "mae_atr"):
        y = labels.loc[move_idx, head].astype(float).replace([np.inf, -np.inf], np.nan).dropna()
        idx = X.index.intersection(y.index)
        if len(idx) < 5:
            boosters[head] = None
            continue
        boosters[head] = lgb.train(
            reg_params,
            lgb.Dataset(
                X.loc[idx],
                label=y.loc[idx],
                weight=weights.loc[idx] if weights is not None else None,
                categorical_feature=categorical_features,
            ),
            num_boost_round=num_boost_round,
            callbacks=[lgb.log_evaluation(period=0)],
        )

    return DirectionalModel(
        boosters=boosters,
        feature_names=feature_columns,
        categorical_features=categorical_features,
        config={
            "params": default,
            "num_boost_round": num_boost_round,
            "feature_columns": feature_columns,
            "categorical_features": categorical_features,
        },
        provenance=make_provenance({"model": "directional_lightgbm", "params": default}),
    )


def _auto_feature_columns(features: pd.DataFrame, categoricals: list[str]) -> list[str]:
    numeric = features.select_dtypes(include=[np.number]).columns.tolist()
    cats = [c for c in categoricals if c in features.columns]
    excluded = {"decision_time", "label_end_time", "entry_price", "exit_price", "direction_class"}
    out = []
    seen = set()
    for col in numeric + cats:
        if col not in excluded and col not in seen:
            out.append(col)
            seen.add(col)
    return out


def _encode_categoricals(X: pd.DataFrame, cats: list[str]) -> pd.DataFrame:
    for col in cats:
        if col in X.columns:
            X[col] = X[col].astype("category")
    return X


def _require_lightgbm():
    try:
        import lightgbm as lgb
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "lightgbm is required for DirectionalModel. Install project dependencies with "
            '`pip install -e ".[dev]"` or add lightgbm to PYTHONPATH.'
        ) from exc
    return lgb
