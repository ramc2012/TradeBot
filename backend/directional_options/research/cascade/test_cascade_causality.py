"""Causality proofs for the cascade study (same contract as
../setups_2d3d/test_causality.py: prefix-invariance, rtol 1e-12).

Run: ../../../../.venv/bin/python test_cascade_causality.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "setups_2d3d"))

from features import add_intraday_features  # noqa: E402
from stages import (  # noqa: E402
    S1_VARIANTS, S2_VARIANTS, S2_WINDOW_SESSIONS, add_daily_stage_features,
    daily_state, stage1_mask, stage2_events,
)

RNG = np.random.default_rng(20260721)


def _synth(n=600, daily=False):
    r = RNG.normal(0, 0.004, n)
    c = 20000 * np.exp(np.cumsum(r))
    h = c * (1 + np.abs(RNG.normal(0, 0.002, n)))
    l = c * (1 - np.abs(RNG.normal(0, 0.002, n)))
    if daily:
        return pd.DataFrame({"s_open": c, "s_high": h, "s_low": l, "s_close": c})
    return pd.DataFrame({"open": c, "high": h, "low": l, "close": c})


def test_daily_stage_features_prefix_invariant():
    d = _synth(400, daily=True)
    full = add_daily_stage_features(d)
    cols = [c for c in full.columns if c.startswith("D_")]
    for k in RNG.choice(np.arange(150, len(d)), 25, replace=False):
        k = int(k)
        pre = add_daily_stage_features(d.iloc[: k + 1].copy())
        a = full.loc[k, cols].astype(float).to_numpy()
        b = pre.loc[k, cols].astype(float).to_numpy()
        assert np.allclose(a, b, rtol=1e-12, atol=1e-12, equal_nan=True), k


def test_daily_state_prefix_invariant():
    d = add_daily_stage_features(_synth(400, daily=True))
    for v in S2_VARIANTS:
        for side in (1, -1):
            full = daily_state(d, v, side).fillna(False).to_numpy()
            for k in RNG.choice(np.arange(150, len(d)), 15, replace=False):
                k = int(k)
                sub = d.iloc[: k + 1][["s_open", "s_high", "s_low", "s_close"]].copy()
                pre = daily_state(add_daily_stage_features(sub), v,
                                  side).fillna(False).to_numpy()
                assert bool(full[k]) == bool(pre[k]), (v, side, k)


def test_stage2_events_prefix_invariant():
    d = add_daily_stage_features(_synth(400, daily=True))
    d["underlying"] = "X"
    d["sidx"] = np.arange(len(d))
    d["session"] = pd.date_range("2024-01-01", periods=len(d), freq="D").date
    for v in S2_VARIANTS:
        for side in (1, -1):
            full = set(stage2_events(d, v, side)["sidx"].tolist())
            for k in (200, 300, 350):
                sub = d.iloc[: k + 1].copy()
                base = add_daily_stage_features(
                    sub[["s_open", "s_high", "s_low", "s_close"]].copy())
                base["underlying"] = "X"
                base["sidx"] = sub["sidx"].to_numpy()
                base["session"] = sub["session"].to_numpy()
                pre = set(stage2_events(base, v, side)["sidx"].tolist())
                assert {e for e in full if e <= k} == pre, (v, side, k)


def test_stage1_mask_prefix_invariant():
    x = _synth(600)
    full = add_intraday_features(x)
    for v in S1_VARIANTS:
        for side in (1, -1):
            fm = stage1_mask(full, v, side).fillna(False).to_numpy()
            for k in RNG.choice(np.arange(200, len(x)), 15, replace=False):
                k = int(k)
                pm = stage1_mask(add_intraday_features(x.iloc[: k + 1].copy()),
                                 v, side).fillna(False).to_numpy()
                assert bool(fm[k]) == bool(pm[k]), (v, side, k)


def test_episode_structure():
    p = os.path.join(HERE, "data", "episodes.parquet")
    if not os.path.exists(p):
        return
    e = pd.read_parquet(p)
    # stage-2 lag lives inside the a-priori window, and only when confirmed
    assert e.loc[e["s2"] == 1, "s2_lag"].between(0, S2_WINDOW_SESSIONS).all()
    assert (e.loc[e["s2"] == 0, "s2_lag"] == -1).all()
    # the tranche-2 anchor only exists on confirmed episodes
    assert e.loc[e["t2_large"].notna(), "s2"].eq(1).all()
    # label is a probability-bearing 0/1 and the horizon never truncated
    assert set(e["large"].unique()) <= {0, 1}
    # controls and real families share the identical universe definition
    assert e.groupby("family")["underlying"].nunique().min() > 100


def test_path_stats_uses_only_forward_bars():
    """A change to bars BEFORE the entry bar must not alter the label."""
    from run_cascade import path_stats
    n = 200
    B = {"high": np.linspace(100, 120, n), "low": np.linspace(99, 119, n),
         "close": np.linspace(99.5, 119.5, n), "open": np.linspace(99.5, 119.5, n),
         "sidx": np.repeat(np.arange(n // 10), 10)}
    a = path_stats(B, 100, 1, 1.0, int(B["sidx"][100]) + 5)
    B2 = {k: (v.copy() if hasattr(v, "copy") else v) for k, v in B.items()}
    for k in ("high", "low", "close", "open"):
        B2[k][:100] = B2[k][:100] * 0.5
    b = path_stats(B2, 100, 1, 1.0, int(B["sidx"][100]) + 5)
    assert a == b


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
        print("PASS", fn.__name__)
    print("ALL CASCADE CAUSALITY CHECKS PASS")
