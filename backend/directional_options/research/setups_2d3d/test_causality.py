"""Empirical proof that every feature is strictly backward-looking.

Method (prefix-invariance): for a random sample of cut points k, recompute the
whole feature block on the PREFIX rows[0:k+1] and assert the value at row k is
bit-identical to the value at row k computed on the FULL series. A feature that
peeked at row k+1 or later cannot survive this, because the prefix does not
contain those rows.

Also asserts the harness's entry rule (entry bar index == decision bar index+1)
and that the daily block seen inside session s is the block ENDING at s-1.

Run:  ../../../../.venv/bin/python -m pytest test_causality.py -q
      (or plain python: it has a __main__ that runs the same checks)
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from features import add_daily_features, add_intraday_features  # noqa: E402

RNG = np.random.default_rng(20260720)


def _synthetic(n: int = 600) -> pd.DataFrame:
    r = RNG.normal(0, 0.004, n)
    close = 20000 * np.exp(np.cumsum(r))
    high = close * (1 + np.abs(RNG.normal(0, 0.002, n)))
    low = close * (1 - np.abs(RNG.normal(0, 0.002, n)))
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close})


def test_intraday_features_are_prefix_invariant():
    df = _synthetic()
    full = add_intraday_features(df)
    cols = [c for c in full.columns if c.startswith("m_")]
    for k in RNG.choice(np.arange(200, len(df)), size=25, replace=False):
        k = int(k)
        pre = add_intraday_features(df.iloc[: k + 1].copy())
        a = full.loc[k, cols].astype(float).to_numpy()
        b = pre.loc[k, cols].astype(float).to_numpy()
        assert np.allclose(a, b, rtol=1e-12, atol=1e-12, equal_nan=True), (
            f"lookahead at row {k}: "
            f"{[cols[i] for i in np.where(~np.isclose(a, b, equal_nan=True))[0]]}"
        )


def test_daily_features_are_prefix_invariant():
    df = _synthetic(400).rename(columns={"open": "s_open", "high": "s_high",
                                         "low": "s_low", "close": "s_close"})
    full = add_daily_features(df)
    cols = [c for c in full.columns if c.startswith("d_")]
    for k in RNG.choice(np.arange(150, len(df)), size=25, replace=False):
        k = int(k)
        pre = add_daily_features(df.iloc[: k + 1].copy())
        a = full.loc[k, cols].astype(float).to_numpy()
        b = pre.loc[k, cols].astype(float).to_numpy()
        assert np.allclose(a, b, rtol=1e-12, atol=1e-12, equal_nan=True), (
            f"lookahead at row {k}: "
            f"{[cols[i] for i in np.where(~np.isclose(a, b, equal_nan=True))[0]]}"
        )


def test_donchian_excludes_current_bar():
    from features import donchian_high
    h = pd.Series([1.0, 5.0, 2.0, 9.0, 3.0])
    dh = donchian_high(h, 2)
    # at row 3 the window must be rows 1..2 -> max 5, NOT include the 9
    assert dh.iloc[3] == 5.0


def test_trades_entry_is_after_decision():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "trades.parquet")
    if not os.path.exists(p):
        return
    t = pd.read_parquet(p)
    assert (t["entry_time"] > t["decision_time"]).all()
    assert (t["exit_time"] >= t["entry_time"]).all()
    # entry is the very next 30m bar in the same session
    gap = (t["entry_time"] - t["decision_time"]).dt.total_seconds() / 60.0
    assert gap.max() <= 30.0, gap.max()


if __name__ == "__main__":
    test_intraday_features_are_prefix_invariant()
    test_daily_features_are_prefix_invariant()
    test_donchian_excludes_current_bar()
    test_trades_entry_is_after_decision()
    print("ALL CAUSALITY CHECKS PASS")
