from __future__ import annotations

import pandas as pd

from nomad_sniper.evaluation.cross_instrument import run_cross_instrument_transfer


def test_cross_instrument_requires_underlying_column():
    features = pd.DataFrame({"x": [1, 2]}, index=["a", "b"])
    labels = pd.DataFrame({
        "direction": ["none", "up"],
        "direction_class": [0, 1],
        "is_move": [0, 1],
        "magnitude_atr": [0.1, 1.0],
        "time_to_target": [60, 10],
        "mae_atr": [0.1, 0.2],
        "sample_weight": [1, 1],
    }, index=["a", "b"])
    try:
        run_cross_instrument_transfer(
            features,
            labels,
            train_underlyings=["nifty"],
            test_underlying="finnifty",
            num_boost_round=1,
        )
    except ValueError as exc:
        assert "underlying" in str(exc)
    else:
        raise AssertionError("expected ValueError")
