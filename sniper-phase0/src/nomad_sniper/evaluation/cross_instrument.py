"""Cross-instrument transfer harness.

Train on one set of underlyings (for example NIFTY+BANKNIFTY) and evaluate on a held-out
underlying (for example FINNIFTY). This is the practical test of whether the model learned
normalized auction structure rather than instrument identity.
"""

from __future__ import annotations

import pandas as pd

from nomad_sniper.evaluation.metrics import acted_ev, directional_classification_metrics
from nomad_sniper.models.directional import train_directional_model


def run_cross_instrument_transfer(
    features: pd.DataFrame,
    labels: pd.DataFrame,
    *,
    train_underlyings: list[str],
    test_underlying: str,
    categorical_features: list[str] | None = None,
    num_boost_round: int = 300,
) -> dict:
    """Train on selected instruments and evaluate on one held-out instrument."""
    common = features.index.intersection(labels.index)
    X = features.loc[common]
    y = labels.loc[common]
    if "underlying" in y.columns:
        underlyings = y["underlying"].astype(str)
    elif "underlying" in X.columns:
        underlyings = X["underlying"].astype(str)
    else:
        raise ValueError("Cross-instrument transfer requires an `underlying` column in labels or features")

    train_mask = underlyings.isin(train_underlyings)
    test_mask = underlyings == test_underlying
    if train_mask.sum() == 0 or test_mask.sum() == 0:
        raise ValueError("Train/test underlyings produced an empty split")

    categorical_features = [c for c in (categorical_features or []) if c in X.columns]
    model = train_directional_model(
        X.loc[train_mask],
        y.loc[train_mask],
        categorical_features=categorical_features,
        sample_weight=y.loc[train_mask, "sample_weight"] if "sample_weight" in y else None,
        num_boost_round=num_boost_round,
    )
    pred = model.predict_frame(X.loc[test_mask])
    metrics = directional_classification_metrics(y.loc[test_mask], pred)
    ev = acted_ev(y.loc[test_mask], pred, slippage_multiplier=2.0)
    return {
        "train_underlyings": train_underlyings,
        "test_underlying": test_underlying,
        "n_train": int(train_mask.sum()),
        "n_test": int(test_mask.sum()),
        "metrics": metrics,
        "acted_ev_2x": ev,
        "predictions": pred,
    }
