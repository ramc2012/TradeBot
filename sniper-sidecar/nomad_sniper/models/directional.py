"""DirectionalModel — multi-head LightGBM for the directional-move target (contract §4.3).

LightGBM has no native multi-output booster, so we hold several:
  - `direction`       : multiclass (down / none / up) — the primary signal
  - `is_move`         : binary (direction != none) — when to be in the market at all
  - `magnitude_atr`   : regression, trained on is_move==1 rows only
  - `time_to_target`  : regression, trained on is_move==1 rows only
  - `mae_atr`         : regression, trained on is_move==1 rows only

`sample_weight` (uniqueness weights from evaluation.splits) is passed into every Dataset.
`predict(X)` returns a dict head → array. Categorical handling + provenance preserved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd

from nomad_sniper.utils.logging import get_logger
from nomad_sniper.utils.provenance import make_provenance

log = get_logger()

# Class index convention for the multiclass direction head.
DIRECTION_CLASSES = ("down", "none", "up")
_DIR_TO_IDX = {c: i for i, c in enumerate(DIRECTION_CLASSES)}
_IDX_TO_DIR = {i: c for c, i in _DIR_TO_IDX.items()}

_REGRESSION_HEADS = ("magnitude_atr", "time_to_target", "mae_atr")


@dataclass
class DirectionalModel:
    """Bundle of trained boosters + the feature schema they expect."""

    direction: lgb.Booster
    is_move: lgb.Booster
    regressors: dict[str, lgb.Booster]
    feature_names: list[str]
    categorical_features: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, str] = field(default_factory=dict)

    def predict(self, X: pd.DataFrame) -> dict[str, np.ndarray]:
        """Return head → array. `direction` is the argmax class label (str array);
        `direction_proba` is the [n, 3] class-probability matrix; `is_move` is P(move)."""
        Xo = _encode_categoricals(self._align(X).copy(), self.categorical_features)
        dir_proba = self.direction.predict(Xo)
        dir_proba = np.atleast_2d(dir_proba)
        dir_idx = dir_proba.argmax(axis=1)
        out: dict[str, np.ndarray] = {
            "direction": np.array([_IDX_TO_DIR[i] for i in dir_idx]),
            "direction_proba": dir_proba,
            "is_move": self.is_move.predict(Xo),
        }
        for head, booster in self.regressors.items():
            out[head] = booster.predict(Xo)
        return out

    def _align(self, X: pd.DataFrame) -> pd.DataFrame:
        missing = [c for c in self.feature_names if c not in X.columns]
        if missing:
            raise ValueError(f"Missing features at predict time: {missing[:10]}")
        return X[self.feature_names]

    def save(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self, path)
        log.info(f"Saved DirectionalModel → {path}")

    @classmethod
    def load(cls, path: Path | str) -> DirectionalModel:
        return joblib.load(path)


def train_directional_model(
    features: pd.DataFrame,
    labels: pd.DataFrame,
    *,
    feature_columns: list[str] | None = None,
    categorical_features: list[str] | None = None,
    sample_weight: pd.Series | None = None,
    params: dict | None = None,
    num_boost_round: int = 400,
) -> DirectionalModel:
    """Train the multi-head bundle.

    Args:
        features:   DataFrame of feature columns (+ optional `decision_time`), aligned to `labels`.
        labels:     DataFrame with `direction` (str), `is_move` (0/1), and the regression heads.
        sample_weight: per-row uniqueness weights (contract §6); defaults to 1.0.
    """
    if categorical_features is None:
        categorical_features = []
    if feature_columns is None:
        feature_columns = _auto_feature_columns(features, categorical_features)

    idx = features.index
    X = _encode_categoricals(features[feature_columns].copy(), categorical_features)
    w = (sample_weight.reindex(idx).fillna(1.0).values if sample_weight is not None
         else np.ones(len(idx)))

    y_dir = labels.loc[idx, "direction"].map(_DIR_TO_IDX).astype(int)
    y_move = labels.loc[idx, "is_move"].astype(int)

    base = {
        "learning_rate": 0.03, "num_leaves": 31, "min_data_in_leaf": 20,
        "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 5,
        "verbose": -1, "seed": 42,
    }
    base.update(params or {})

    # direction — multiclass
    dir_params = {**base, "objective": "multiclass", "num_class": len(DIRECTION_CLASSES),
                  "metric": "multi_logloss"}
    dir_booster = lgb.train(
        dir_params,
        lgb.Dataset(X, label=y_dir, weight=w, categorical_feature=categorical_features),
        num_boost_round=num_boost_round,
        callbacks=[lgb.log_evaluation(period=0)],
    )

    # is_move — binary
    move_params = {**base, "objective": "binary", "metric": ["auc", "binary_logloss"]}
    move_booster = lgb.train(
        move_params,
        lgb.Dataset(X, label=y_move, weight=w, categorical_feature=categorical_features),
        num_boost_round=num_boost_round,
        callbacks=[lgb.log_evaluation(period=0)],
    )

    # regression heads — trained on is_move==1 rows only
    move_mask = (y_move == 1).values
    regressors: dict[str, lgb.Booster] = {}
    reg_params = {**base, "objective": "regression", "metric": "rmse"}
    if move_mask.sum() >= 20:
        Xr = X[move_mask]
        wr = w[move_mask]
        for head in _REGRESSION_HEADS:
            yr = labels.loc[idx, head].astype(float).values[move_mask]
            regressors[head] = lgb.train(
                reg_params,
                lgb.Dataset(Xr, label=yr, weight=wr, categorical_feature=categorical_features),
                num_boost_round=num_boost_round,
                callbacks=[lgb.log_evaluation(period=0)],
            )
    else:
        log.warning(
            f"Only {int(move_mask.sum())} is_move==1 rows (<20); regression heads skipped."
        )

    log.info(
        f"Trained DirectionalModel on {len(idx)} rows "
        f"(moves={int(move_mask.sum())}, features={len(feature_columns)})"
    )

    return DirectionalModel(
        direction=dir_booster,
        is_move=move_booster,
        regressors=regressors,
        feature_names=feature_columns,
        categorical_features=categorical_features,
        config={
            "params": base,
            "num_boost_round": num_boost_round,
            "feature_columns": feature_columns,
            "categorical_features": categorical_features,
            "direction_classes": list(DIRECTION_CLASSES),
        },
        provenance=make_provenance({"model": "directional_multihead", "params": base}),
    )


def _auto_feature_columns(features: pd.DataFrame, categoricals: list[str]) -> list[str]:
    """Numeric columns + declared categoricals, excluding bookkeeping/label columns."""
    drop = {"decision_time", "underlying_key", "underlying", "direction", "is_move",
            "magnitude_atr", "time_to_target", "mae_atr", "sample_weight", "raw_candidate"}
    numeric = features.select_dtypes(include=[np.number]).columns.tolist()
    cats = [c for c in categoricals if c in features.columns]
    seen: set[str] = set()
    out: list[str] = []
    for c in numeric + cats:
        if c not in seen and c not in drop:
            out.append(c)
            seen.add(c)
    return out


def _encode_categoricals(X: pd.DataFrame, cats: list[str]) -> pd.DataFrame:
    for c in cats:
        if c in X.columns:
            X[c] = X[c].astype("category")
    return X
