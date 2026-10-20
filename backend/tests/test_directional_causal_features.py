"""Causality of the directional feature frame.

The live path only reads the last row, which hid a full-sample
``rv_percentile``. The backtest walks every row, so a later vol spike
used to rewrite earlier regime labels.
"""
from __future__ import annotations

import pandas as pd
import pytest

from directional_options.config import DEFAULT_CONFIG
from directional_options.features import FeatureEngine
from directional_options.paper import DirectionalOptionsPaperStore


def _spot(n: int, *, spike_from: int | None = None) -> pd.DataFrame:
    close = []
    price = 22000.0
    for i in range(n):
        if spike_from is not None and i >= spike_from:
            price *= 1.04 if i % 2 == 0 else 0.96
        else:
            price *= 1.0004 if i % 2 == 0 else 0.9997
        close.append(price)
    times = pd.date_range("2026-04-01 03:45", periods=n, freq="1min")
    return pd.DataFrame(
        {
            "time": times,
            "open": close,
            "high": [c * 1.0005 for c in close],
            "low": [c * 0.9995 for c in close],
            "close": close,
            "volume": [1000.0] * n,
            "oi": [0.0] * n,
        }
    )


def test_rv_percentile_of_an_early_bar_ignores_a_later_spike() -> None:
    engine = FeatureEngine(dict(DEFAULT_CONFIG["feature_engine"]))
    calm = _spot(60)
    spiked = _spot(80, spike_from=60)
    early = engine.build_frame(calm, "1minute")
    full = engine.build_frame(spiked, "1minute")
    # Warmup drops the first bars, so compare a row that exists in both and
    # still sits before the spike. A causal percentile matches the prefix-only
    # frame; a whole-sample min/max does not, because the spike rewrites it.
    assert len(early) > 10 and len(full) > len(early)
    assert full["rv_percentile"].iloc[10] == pytest.approx(float(early["rv_percentile"].iloc[10]), abs=1e-9)


def test_directional_close_haircuts_slippage_before_charges(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("paper_engine.costs.PAPER_APPLY_COSTS", True)
    store = DirectionalOptionsPaperStore(tmp_path / "paper")
    position = {
        "position_id": "p1",
        "underlying": "NIFTY",
        "option_type": "CE",
        "trading_symbol": "NIFTY24APR22500CE",
        "quantity_units": 75,
        "entry_premium": 100.0,
    }
    store._close_position(
        position,
        mark={"premium": 120.0, "spot": 22500.0},
        close_time="2026-04-21T10:00:00+00:00",
        close_reason="target_hit",
    )
    gross = (120.0 - 100.0) * 75
    assert position["realized_pnl_gross"] == pytest.approx(gross)
    assert position["slippage_cost"] > 0
    assert position["transaction_cost"] > 0
    assert position["realized_pnl"] == pytest.approx(
        gross - position["slippage_cost"] - position["transaction_cost"]
    )
    assert position["realized_pnl"] < position["realized_pnl_gross"]
