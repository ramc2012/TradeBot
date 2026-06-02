"""Triple-barrier labeling tests."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from nomad_sniper.labels.triple_barrier import label_triple_barrier
from nomad_sniper.utils.timeutil import IST


def _make_bars(prices):
    """Build minute bars with given close prices, hi/lo bracketing close ± 1."""
    start = IST.localize(datetime(2025, 1, 8, 11, 0))
    rows = []
    for i, c in enumerate(prices):
        rows.append({
            "open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1000,
        })
    idx = pd.date_range(start, periods=len(prices), freq="1min", tz=IST)
    return pd.DataFrame(rows, index=idx)


def test_long_target_hit():
    bars = _make_bars([22000, 22020, 22040, 22060, 22080])
    entry = IST.localize(datetime(2025, 1, 8, 10, 59))  # before first bar
    lbl = label_triple_barrier(
        bars, entry, "long",
        stop_price=21980, target_price=22050, max_holding=timedelta(minutes=10),
    )
    assert lbl is not None
    assert lbl.exit_reason == "target"
    assert lbl.realised_r > 0


def test_long_stop_hit():
    bars = _make_bars([22000, 21990, 21970, 21950, 21940])
    entry = IST.localize(datetime(2025, 1, 8, 10, 59))
    lbl = label_triple_barrier(
        bars, entry, "long",
        stop_price=21980, target_price=22050, max_holding=timedelta(minutes=10),
    )
    assert lbl is not None
    assert lbl.exit_reason == "stop"
    assert lbl.realised_r == pytest.approx(-1.0, abs=0.1)


def test_timeout_exit():
    bars = _make_bars([22000, 22005, 22010, 22008, 22012])
    entry = IST.localize(datetime(2025, 1, 8, 10, 59))
    lbl = label_triple_barrier(
        bars, entry, "long",
        stop_price=21900, target_price=22500, max_holding=timedelta(minutes=4),
    )
    assert lbl is not None
    assert lbl.exit_reason == "timeout"


def test_short_target_hit():
    bars = _make_bars([22000, 21980, 21960, 21940, 21920])
    entry = IST.localize(datetime(2025, 1, 8, 10, 59))
    lbl = label_triple_barrier(
        bars, entry, "short",
        stop_price=22050, target_price=21950, max_holding=timedelta(minutes=10),
    )
    assert lbl is not None
    assert lbl.exit_reason == "target"
    assert lbl.realised_r > 0


def test_invalid_long_geometry_raises():
    bars = _make_bars([22000, 22010])
    entry = IST.localize(datetime(2025, 1, 8, 10, 59))
    # Target below entry — invalid for a long
    with pytest.raises(ValueError):
        label_triple_barrier(
            bars, entry, "long",
            stop_price=21980, target_price=21990, max_holding=timedelta(minutes=10),
        )
