"""MultiHorizonModel — the spec's core model bundle (v2 §8, §21, §22-baseline).

Per horizon (EOD/1d/2d/3d/1w/…) it holds:
  - expected_move : LightGBM regressor of signed forward return in ATR (the primary target;
                    its SIGN is direction, its MAGNITUDE sizes the trade, and at value-area
                    extremes it forecasts reversion toward POC — captures BOTH directional and
                    mean-reverting opportunities),
  - mfe / mae     : LightGBM regressors of favourable / adverse excursion in ATR (for targets,
                    stops, and risk),
  - direction     : 3-class classifier (down/none/up) for a discrete read.

Pooled across instruments — features are instrument-independent (contract §2), so one model
learns auction behaviour, not instrument identity. `sample_weight` carries uniqueness weights.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd

from nomad_sniper.models.directional import (
    DIRECTION_CLASSES,
    _DIR_TO_IDX,
    _encode_categoricals,
)
from nomad_sniper.utils.logging import get_logger
from nomad_sniper.utils.provenance import make_provenance

log = get_logger()

_IDX_TO_DIR = {i: c for c, i in _DIR_TO_IDX.items()}


@dataclass
class MultiHorizonModel:
    horizons: list[str]
    boosters: dict[str, dict[str, lgb.Booster]]  # horizon -> {expected_move, mfe, mae, direction}
    feature_names: list[str]
    categorical_features: list[str] = field(default_factory=list)
    provenance: dict[str, str] = field(default_factory=dict)

    def predict(self, X: pd.DataFrame) -> dict[str, dict[str, np.ndarray]]:
        Xo = _encode_categoricals(X[self.feature_names].copy(), self.categorical_features)
        out: dict[str, dict[str, np.ndarray]] = {}
        for h, heads in self.boosters.items():
            res: dict[str, np.ndarray] = {}
            for name, booster in heads.items():
                if name == "direction":
                    proba = np.atleast_2d(booster.predict(Xo))
                    res["direction"] = np.array([_IDX_TO_DIR[i] for i in proba.argmax(axis=1)])
                    res["direction_proba"] = proba
                else:
                    res[name] = booster.predict(Xo)
            out[h] = res
        return out

    def save(self, path: Path | str) -> None:
        path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self, path)
        log.info(f"Saved MultiHorizonModel ({len(self.horizons)} horizons) → {path}")

    @classmethod
    def load(cls, path: Path | str) -> "MultiHorizonModel":
        return joblib.load(path)


def _reg(X, y, w, cats, n):
    return lgb.train(
        {"objective": "regression", "metric": "rmse", "learning_rate": 0.03, "num_leaves": 31,
         "min_data_in_leaf": 50, "feature_fraction": 0.8, "bagging_fraction": 0.8,
         "bagging_freq": 5, "verbose": -1, "seed": 42},
        lgb.Dataset(X, label=y, weight=w, categorical_feature=cats),
        num_boost_round=n, callbacks=[lgb.log_evaluation(period=0)],
    )


def train_multihorizon_model(
    merged: pd.DataFrame,
    horizons: list[str],
    *,
    feature_columns: list[str],
    categorical_features: list[str],
    sample_weight: pd.Series | None = None,
    num_boost_round: int = 350,
) -> MultiHorizonModel:
    """Train the per-horizon bundle on a feature+label merged frame.

    `merged` must contain `feature_columns` plus, per horizon h: ret_atr_{h}, dir_{h},
    mfe_atr_{h}, mae_atr_{h}.
    """
    X = _encode_categoricals(merged[feature_columns].copy(), categorical_features)
    w = (sample_weight.reindex(merged.index).fillna(1.0).values
         if sample_weight is not None else np.ones(len(merged)))

    boosters: dict[str, dict[str, lgb.Booster]] = {}
    for h in horizons:
        if f"ret_atr_{h}" not in merged.columns:
            continue
        m = merged[f"ret_atr_{h}"].notna().values
        if m.sum() < 200:
            continue
        Xh, wh = X[m], w[m]
        heads: dict[str, lgb.Booster] = {
            "expected_move": _reg(Xh, merged.loc[m, f"ret_atr_{h}"].astype(float), wh,
                                  categorical_features, num_boost_round),
            "mfe": _reg(Xh, merged.loc[m, f"mfe_atr_{h}"].astype(float), wh,
                        categorical_features, num_boost_round),
            "mae": _reg(Xh, merged.loc[m, f"mae_atr_{h}"].astype(float), wh,
                        categorical_features, num_boost_round),
        }
        y_dir = merged.loc[m, f"dir_{h}"].map(_DIR_TO_IDX).astype(int)
        heads["direction"] = lgb.train(
            {"objective": "multiclass", "num_class": len(DIRECTION_CLASSES),
             "metric": "multi_logloss", "learning_rate": 0.03, "num_leaves": 31,
             "min_data_in_leaf": 50, "feature_fraction": 0.8, "bagging_fraction": 0.8,
             "bagging_freq": 5, "verbose": -1, "seed": 42},
            lgb.Dataset(Xh, label=y_dir, weight=wh, categorical_feature=categorical_features),
            num_boost_round=num_boost_round, callbacks=[lgb.log_evaluation(period=0)],
        )
        boosters[h] = heads
        log.info(f"horizon {h}: trained on {int(m.sum())} rows")

    return MultiHorizonModel(
        horizons=[h for h in horizons if h in boosters],
        boosters=boosters,
        feature_names=feature_columns,
        categorical_features=categorical_features,
        provenance=make_provenance({"model": "multihorizon", "horizons": horizons}),
    )
