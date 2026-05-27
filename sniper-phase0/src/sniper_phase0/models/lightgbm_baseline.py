"""LightGBM baseline.

Two heads:
  - classifier: P(net_R > 0)
  - regressor:  E[net_R]

Phase 0's go/no-go question uses the classifier for skip-decisions and the
regressor for expectancy ranking. No sequence models. No fancy stacking.
"""
from __future__ import annotations

from dataclasses import dataclass

import lightgbm as lgb
import numpy as np
import pandas as pd


META_COLS = {"trade_id", "decision_ts", "instrument", "side"}


@dataclass
class BaselineModel:
    classifier: lgb.Booster
    regressor: lgb.Booster
    feature_names: list[str]


def feature_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if c not in META_COLS]


def train_baseline(
    train_features: pd.DataFrame,
    train_labels: pd.DataFrame,
    val_features: pd.DataFrame | None = None,
    val_labels: pd.DataFrame | None = None,
    num_boost_round: int = 500,
) -> BaselineModel:
    feats = feature_columns(train_features)
    X_train = train_features[feats]
    y_cls_train = (train_labels["net_R"] > 0).astype(int)
    y_reg_train = train_labels["net_R"]

    cls_params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_data_in_leaf": 50,
        "feature_fraction": 0.85,
        "bagging_fraction": 0.85,
        "bagging_freq": 5,
        "verbose": -1,
    }
    reg_params = {**cls_params, "objective": "regression", "metric": "rmse"}

    train_set_cls = lgb.Dataset(X_train, label=y_cls_train)
    train_set_reg = lgb.Dataset(X_train, label=y_reg_train)

    valid_sets_cls = [train_set_cls]
    valid_sets_reg = [train_set_reg]
    if val_features is not None and val_labels is not None:
        X_val = val_features[feats]
        valid_sets_cls.append(lgb.Dataset(X_val, label=(val_labels["net_R"] > 0).astype(int)))
        valid_sets_reg.append(lgb.Dataset(X_val, label=val_labels["net_R"]))

    cls = lgb.train(
        cls_params,
        train_set_cls,
        num_boost_round=num_boost_round,
        valid_sets=valid_sets_cls,
        callbacks=[lgb.early_stopping(50, verbose=False)] if len(valid_sets_cls) > 1 else None,
    )
    reg = lgb.train(
        reg_params,
        train_set_reg,
        num_boost_round=num_boost_round,
        valid_sets=valid_sets_reg,
        callbacks=[lgb.early_stopping(50, verbose=False)] if len(valid_sets_reg) > 1 else None,
    )

    return BaselineModel(classifier=cls, regressor=reg, feature_names=feats)


def predict(model: BaselineModel, features: pd.DataFrame) -> pd.DataFrame:
    X = features[model.feature_names]
    p_win = model.classifier.predict(X, num_iteration=model.classifier.best_iteration)
    e_R = model.regressor.predict(X, num_iteration=model.regressor.best_iteration)
    return pd.DataFrame(
        {
            "trade_id": features["trade_id"].to_numpy(),
            "p_win": np.asarray(p_win),
            "expected_net_R": np.asarray(e_R),
        }
    )
