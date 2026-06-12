"""ExcursionEstimator — the canonical alpha model: estimate the MOVE at every timeframe.

The model's sole job is estimation, not execution. For each timeframe (30m … 1M) it learns
three heads from the instrument-independent feature state:

  magnitude   : how far price travels (max excursion, ATR units) — the "expected move size",
  direction   : p(up-dominant) — which way,
  time_to_peak: when the move peaks (the "expected period" — used downstream as R, the required
                holding period).

Trade/exit/roll decisions (expiry coverage, the higher-vs-lower-TF AND-gate, option rolls) are
NOT in here — they are applied as runtime rules in paper trading, on top of these estimates.

Instrument-independent by construction (features obey contract §2), so one estimator pools
across instruments. Persisted via joblib for the live/paper service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd

from nomad_sniper.models.directional import _encode_categoricals
from nomad_sniper.utils.logging import get_logger
from nomad_sniper.utils.provenance import make_provenance

log = get_logger()

HEADS = ("magnitude", "direction", "time_to_peak")


@dataclass
class ExcursionEstimator:
    timeframes: list[str]
    boosters: dict[str, dict[str, lgb.Booster]]      # tf -> {magnitude, direction, time_to_peak}
    feature_names: list[str]
    categorical_features: list[str] = field(default_factory=list)
    ttp_unit: dict[str, str] = field(default_factory=dict)   # tf -> "frac" | "days"
    provenance: dict[str, str] = field(default_factory=dict)

    def predict(self, X: pd.DataFrame) -> dict[str, dict[str, np.ndarray]]:
        Xo = _encode_categoricals(X[self.feature_names].copy(), self.categorical_features)
        # coerce non-categorical features to numeric — single-row / partly-null live rows arrive as
        # object dtype (None values), which LightGBM rejects. Categoricals are already encoded above.
        for c in Xo.columns:
            if c not in self.categorical_features and Xo[c].dtype == object:
                Xo[c] = pd.to_numeric(Xo[c], errors="coerce")
        out: dict[str, dict[str, np.ndarray]] = {}
        for tf, heads in self.boosters.items():
            r: dict[str, np.ndarray] = {}
            r["magnitude"] = np.clip(heads["magnitude"].predict(Xo), 0, None)
            r["p_up"] = heads["direction"].predict(Xo)
            r["time_to_peak"] = np.clip(heads["time_to_peak"].predict(Xo), 0, None)
            # signed expected move toward 'up' = magnitude * (2*p_up - 1), the convenience field
            r["signed_move"] = r["magnitude"] * (2 * r["p_up"] - 1)
            out[tf] = r
        return out

    def save(self, path: Path | str) -> None:
        path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self, path)
        log.info(f"Saved ExcursionEstimator ({len(self.timeframes)} timeframes) → {path}")

    @classmethod
    def load(cls, path: Path | str) -> "ExcursionEstimator":
        return joblib.load(path)


def _params(obj: str) -> dict:
    return {"objective": obj, "learning_rate": 0.03, "num_leaves": 31, "min_data_in_leaf": 40,
            "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 5,
            "verbose": -1, "seed": 42, "metric": "binary_logloss" if obj == "binary" else "rmse"}


def _fit(X, y, w, cats, obj, n):
    m = np.isfinite(np.asarray(y, dtype=float))
    return lgb.train(_params(obj), lgb.Dataset(X[m], label=np.asarray(y)[m],
                     weight=(w[m] if w is not None else None), categorical_feature=cats),
                     num_boost_round=n, callbacks=[lgb.log_evaluation(period=0)])


def train_excursion_estimator(
    merged: pd.DataFrame,
    timeframes: list[str],
    *,
    feature_columns: list[str],
    categorical_features: list[str],
    ttp_cols: dict[str, str],
    sample_weight: pd.Series | None = None,
    num_boost_round: int = 300,
) -> ExcursionEstimator:
    """Train magnitude/direction/time-to-peak heads per timeframe.

    `merged` must hold `feature_columns` plus, per tf: magnitude_atr_{tf}, dom_dir_{tf}, and the
    time-to-peak column named in `ttp_cols[tf]`.
    """
    X = _encode_categoricals(merged[feature_columns].copy(), categorical_features)
    w = (sample_weight.reindex(merged.index).fillna(1.0).values
         if sample_weight is not None else None)
    boosters: dict[str, dict[str, lgb.Booster]] = {}
    ttp_unit: dict[str, str] = {}
    for tf in timeframes:
        magc, dirc, ttpc = f"magnitude_atr_{tf}", f"dom_dir_{tf}", ttp_cols[tf]
        if magc not in merged.columns or merged[magc].notna().sum() < 200:
            continue
        heads = {
            "magnitude": _fit(X, merged[magc].astype(float), w, categorical_features, "regression", num_boost_round),
            "direction": _fit(X, (merged[dirc] == 1).astype(int), w, categorical_features, "binary", num_boost_round),
            "time_to_peak": _fit(X, merged[ttpc].astype(float), w, categorical_features, "regression", num_boost_round),
        }
        boosters[tf] = heads
        ttp_unit[tf] = "days" if ttpc.endswith("days") else "frac"
        log.info(f"timeframe {tf}: trained ({int(merged[magc].notna().sum())} rows)")

    return ExcursionEstimator(
        timeframes=[tf for tf in timeframes if tf in boosters],
        boosters=boosters, feature_names=feature_columns,
        categorical_features=categorical_features, ttp_unit=ttp_unit,
        provenance=make_provenance({"model": "excursion_estimator", "timeframes": timeframes}),
    )
