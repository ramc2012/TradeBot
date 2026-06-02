from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from nomad_sniper.evaluation.metrics import acted_ev, directional_classification_metrics
from nomad_sniper.evaluation.splits import Split, sample_uniqueness_weights
from nomad_sniper.utils.timeutil import IST


def test_directional_metrics_and_acted_ev():
    idx = ["a", "b", "c"]
    labels = pd.DataFrame({
        "direction": ["none", "up", "down"],
        "magnitude_atr": [0.2, 1.5, 1.2],
        "mae_atr": [0.3, 0.4, 0.5],
    }, index=idx)
    pred = pd.DataFrame({"pred_direction": ["none", "up", "up"]}, index=idx)
    metrics = directional_classification_metrics(labels, pred)
    assert metrics["none_recall"] == 1
    assert metrics["up_precision"] == 0.5
    ev = acted_ev(labels, pred, slippage_multiplier=2.0, cost_atr=0.05)
    assert ev["n_acted"] == 2
    assert ev["acted_total_atr"] > 0


def test_split_embargo_drops_overlapping_train_rows():
    ts = pd.Series([
        IST.localize(datetime(2025, 1, 1, 10)),
        IST.localize(datetime(2025, 1, 2, 10)),
        IST.localize(datetime(2025, 1, 3, 10)),
    ])
    ends = ts + timedelta(days=2)
    split = Split(
        train_start=ts.iloc[0].date(),
        train_end=ts.iloc[1].date(),
        test_start=ts.iloc[2].date(),
        test_end=ts.iloc[2].date(),
        embargo_minutes=0,
    )
    mask = split.train_mask(ts, label_end_times=ends)
    assert mask.tolist() == [False, False, False]


def test_sample_uniqueness_weights_downweight_overlaps():
    t0 = IST.localize(datetime(2025, 1, 1, 10))
    windows = pd.DataFrame({
        "decision_time": [t0, t0 + timedelta(minutes=5)],
        "label_end_time": [t0 + timedelta(minutes=60), t0 + timedelta(minutes=65)],
    }, index=["a", "b"])
    weights = sample_uniqueness_weights(windows)
    assert weights.loc["a"] < 1
    assert weights.loc["b"] < 1
