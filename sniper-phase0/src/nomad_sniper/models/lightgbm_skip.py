"""Phase 0 model: LightGBM "skip-or-take" classifier.

Trained on `is_winner` from the actual round-trip labels. The model's job is to identify
which of the user's historical trades would have been better off skipped. Probability ranking
matters more than absolute calibration — the Phase 0 verdict uses skip-accuracy on the
bottom-decile losers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, roc_auc_score

from nomad_sniper.utils.logging import get_logger
from nomad_sniper.utils.provenance import make_provenance

log = get_logger()


@dataclass
class SkipClassifier:
    """Wraps a trained LightGBM + the feature schema it expects."""

    model: lgb.Booster
    feature_names: list[str]
    categorical_features: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, str] = field(default_factory=dict)

    def predict_proba_take(self, X: pd.DataFrame) -> np.ndarray:
        """Probability that this trade was a winner — high = take, low = skip."""
        X_ordered = self._align_columns(X)
        X_ordered = _encode_categoricals(X_ordered.copy(), self.categorical_features)
        return self.model.predict(X_ordered)

    def decision_skip(self, X: pd.DataFrame, *, threshold: float = 0.5) -> np.ndarray:
        """Return 1 for SKIP, 0 for TAKE."""
        proba = self.predict_proba_take(X)
        return (proba < threshold).astype(int)

    def _align_columns(self, X: pd.DataFrame) -> pd.DataFrame:
        missing = [c for c in self.feature_names if c not in X.columns]
        if missing:
            raise ValueError(f"Missing features at predict time: {missing[:10]}")
        return X[self.feature_names]

    def save(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self, path)
        log.info(f"Saved SkipClassifier → {path}")

    @classmethod
    def load(cls, path: Path | str) -> "SkipClassifier":
        return joblib.load(path)


def train_skip_classifier(
    features: pd.DataFrame,
    labels: pd.Series,
    *,
    feature_columns: list[str] | None = None,
    categorical_features: list[str] | None = None,
    params: dict | None = None,
    num_boost_round: int = 500,
    early_stopping_rounds: int = 50,
    valid_features: pd.DataFrame | None = None,
    valid_labels: pd.Series | None = None,
) -> SkipClassifier:
    """Train a LightGBM binary classifier.

    Args:
        features:           DataFrame indexed by trade_id with feature columns.
        labels:             Series of {0, 1}, 1 = winner. Indexed by trade_id.
        feature_columns:    Subset to use. Defaults to all numeric + listed categoricals.
        categorical_features: Column names to treat as categorical.
        params:             LightGBM params. Sensible defaults below.
        valid_features/valid_labels: Optional held-out validation for early stopping.
    """
    if categorical_features is None:
        categorical_features = []
    if feature_columns is None:
        feature_columns = _auto_feature_columns(features, categorical_features)

    X = features[feature_columns].copy()
    X = _encode_categoricals(X, categorical_features)

    default_params = {
        "objective": "binary",
        "metric": ["auc", "binary_logloss"],
        "learning_rate": 0.03,
        "num_leaves": 31,
        "min_data_in_leaf": 20,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "verbose": -1,
        "seed": 42,
    }
    default_params.update(params or {})

    train_set = lgb.Dataset(X, label=labels.loc[X.index], categorical_feature=categorical_features)
    valid_sets = [train_set]
    valid_names = ["train"]
    callbacks = []
    if valid_features is not None and valid_labels is not None:
        X_val = _encode_categoricals(valid_features[feature_columns].copy(), categorical_features)
        valid_set = lgb.Dataset(
            X_val,
            label=valid_labels.loc[X_val.index],
            categorical_feature=categorical_features,
            reference=train_set,
        )
        valid_sets.append(valid_set)
        valid_names.append("valid")
        callbacks.append(lgb.early_stopping(early_stopping_rounds, verbose=False))

    callbacks.append(lgb.log_evaluation(period=0))

    booster = lgb.train(
        default_params,
        train_set,
        num_boost_round=num_boost_round,
        valid_sets=valid_sets,
        valid_names=valid_names,
        callbacks=callbacks,
    )

    # Compute training AUC for sanity check
    train_pred = booster.predict(X)
    train_auc = float(roc_auc_score(labels.loc[X.index], train_pred))
    log.info(f"Trained SkipClassifier — train AUC: {train_auc:.4f}")

    return SkipClassifier(
        model=booster,
        feature_names=feature_columns,
        categorical_features=categorical_features,
        config={
            "params": default_params,
            "num_boost_round": num_boost_round,
            "feature_columns": feature_columns,
            "categorical_features": categorical_features,
        },
        provenance=make_provenance({"model": "lightgbm_skip", "params": default_params}),
    )


def _auto_feature_columns(features: pd.DataFrame, categoricals: list[str]) -> list[str]:
    """Numeric columns + declared categoricals, in stable order."""
    numeric = features.select_dtypes(include=[np.number]).columns.tolist()
    cats = [c for c in categoricals if c in features.columns]
    seen = set()
    out = []
    for c in numeric + cats:
        if c not in seen and c not in ("decision_time",):
            out.append(c)
            seen.add(c)
    return out


def _encode_categoricals(X: pd.DataFrame, cats: list[str]) -> pd.DataFrame:
    """LightGBM accepts string categoricals via dtype='category'."""
    for c in cats:
        if c in X.columns:
            X[c] = X[c].astype("category")
    return X
