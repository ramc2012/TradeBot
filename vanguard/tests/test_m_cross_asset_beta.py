"""Offline tests for the cross-asset beta math -- no network, no database."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parents[1]))

from features.m_cross_asset_beta import equal_weight_index, rolling_beta_corr  # noqa: E402


def test_equal_weight_index_starts_at_100_and_is_return_weighted_not_price_weighted():
    # Two symbols: one at price ~10, one at price ~1000 -- a price-weighted
    # average would be dominated by the second; equal-weight must not be.
    idx = pd.date_range("2026-01-01", periods=4, freq="D")
    closes = pd.DataFrame({
        "CHEAP": [10.0, 11.0, 11.0, 12.1],     # +10%, 0%, +10%
        "EXPENSIVE": [1000.0, 990.0, 980.1, 970.3],  # -1%, -1%, -1%
    }, index=idx)
    index = equal_weight_index(closes, ["CHEAP", "EXPENSIVE"])
    assert index.iloc[0] == 100.0
    # day 2 return should be mean(+10%, -1%) = +4.5%, not dominated by EXPENSIVE
    assert abs(index.iloc[1] - 104.5) < 0.01


def test_equal_weight_index_ignores_members_absent_from_closes():
    idx = pd.date_range("2026-01-01", periods=3, freq="D")
    closes = pd.DataFrame({"A": [10.0, 11.0, 12.0]}, index=idx)
    index = equal_weight_index(closes, ["A", "NOT_PRESENT"])
    assert len(index) == 3
    assert index.iloc[0] == 100.0


def test_equal_weight_index_empty_when_no_members_present():
    closes = pd.DataFrame({"A": [10.0, 11.0]})
    index = equal_weight_index(closes, ["B", "C"])
    assert index.empty


def test_rolling_beta_recovers_a_known_linear_relationship():
    # driver moves 1% each day; sector moves exactly 2x that plus noise-free
    # -- rolling beta over a window fully inside the series must converge to 2.0.
    n = 80
    rng = np.random.default_rng(0)
    driver_returns = pd.Series(rng.normal(0, 0.01, n))
    sector_returns = 2.0 * driver_returns  # exact beta = 2, corr = 1
    table = rolling_beta_corr(sector_returns, driver_returns, lookback=60)
    last = table.dropna().iloc[-1]
    assert abs(last["beta"] - 2.0) < 1e-6
    assert abs(last["corr"] - 1.0) < 1e-6


def test_rolling_beta_negative_for_an_inverse_relationship():
    n = 80
    rng = np.random.default_rng(1)
    driver_returns = pd.Series(rng.normal(0, 0.01, n))
    sector_returns = -0.5 * driver_returns
    table = rolling_beta_corr(sector_returns, driver_returns, lookback=60)
    last = table.dropna().iloc[-1]
    assert last["beta"] < 0
    assert last["corr"] < 0


def test_rolling_beta_handles_misaligned_indices_via_inner_join():
    sector_returns = pd.Series([0.01, 0.02, -0.01], index=[0, 1, 2])
    driver_returns = pd.Series([0.02, 0.04], index=[0, 1])  # index 2 missing
    table = rolling_beta_corr(sector_returns, driver_returns, lookback=2)
    assert len(table) <= 2  # only overlapping indices considered


def test_rolling_beta_empty_on_insufficient_overlap():
    table = rolling_beta_corr(pd.Series(dtype=float), pd.Series(dtype=float), lookback=60)
    assert table.empty
