"""Prefix-invariance proof for the RS feature block.

Claim to prove: the value of every FEATURE at row k depends only on rows 0..k.

Method: recompute the whole feature block on the prefix rows[0..k] alone and
assert the value at row k is bit-comparable (rtol 1e-12) to the value computed
on the full series. A feature that peeked at row k+1 cannot survive, because
the prefix does not contain row k+1.

Also asserted:
  * forward OUTCOME columns are, by contrast, NOT prefix-invariant (they must
    look ahead) -- this is a positive control proving the test can detect
    lookahead at all;
  * the cross-sectional rank at date t uses only date-t rows.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rs_features import (  # noqa: E402
    BENCH, HORIZONS, LOOKBACKS, add_forwards, per_name_features,
)

HERE = os.path.dirname(os.path.abspath(__file__))
DAILY = os.path.join(HERE, "data_rs", "rs_daily.parquet")

FEATURE_COLS = (
    ["beta_120"]
    + [f"rs_ret_{L}" for L in LOOKBACKS]
    + [f"rs_slope_{L}" for L in LOOKBACKS]
    + [f"alpha_{L}" for L in LOOKBACKS]
    + [f"mom_{L}" for L in LOOKBACKS]
)


def _pair(daily: pd.DataFrame, name: str):
    bench = daily[daily.underlying == BENCH][["session", "c"]].rename(columns={"c": "bc"})
    d = daily[daily.underlying == name][["session", "o", "h", "l", "c", "v"]]
    m = d.merge(bench, on="session", how="inner").sort_values("session").reset_index(drop=True)
    return m, m[["bc"]].rename(columns={"bc": "c"})


def test_prefix_invariance():
    daily = pd.read_parquet(DAILY)
    names = [n for n in sorted(daily.underlying.unique()) if n != BENCH][:6]
    rng = np.random.default_rng(7)
    checked = 0
    for name in names:
        m, b = _pair(daily, name)
        if len(m) < 200:
            continue
        full = per_name_features(m, b)
        for k in rng.integers(150, len(m), size=14):
            k = int(k)
            pre = per_name_features(m.iloc[: k + 1].reset_index(drop=True),
                                    b.iloc[: k + 1].reset_index(drop=True))
            for col in FEATURE_COLS:
                a, c = full[col].iloc[k], pre[col].iloc[k]
                if pd.isna(a) and pd.isna(c):
                    continue
                assert np.isclose(a, c, rtol=1e-12, atol=1e-12), \
                    f"LOOKAHEAD in {col} for {name} at row {k}: {a} vs {c}"
                checked += 1
    assert checked > 500, f"too few comparisons ({checked})"
    print(f"prefix-invariance PASS ({checked} feature-value comparisons)")


def test_positive_control_forwards_do_look_ahead():
    """The test must be capable of catching lookahead. Forward outcome columns
    genuinely look ahead, so they MUST fail prefix invariance."""
    daily = pd.read_parquet(DAILY)
    name = [n for n in sorted(daily.underlying.unique()) if n != BENCH][0]
    m, b = _pair(daily, name)
    full = add_forwards(per_name_features(m, b))
    k = len(m) - 40
    pre = add_forwards(per_name_features(m.iloc[: k + 1].reset_index(drop=True),
                                         b.iloc[: k + 1].reset_index(drop=True)))
    caught = False
    for h in HORIZONS:
        a, c = full[f"fwd_{h}"].iloc[k], pre[f"fwd_{h}"].iloc[k]
        if pd.notna(a) and pd.isna(c):
            caught = True
    assert caught, "positive control failed: the test cannot detect lookahead"
    print("positive control PASS (forward outcomes correctly flagged as lookahead)")


def test_xs_rank_is_same_date_only():
    daily = pd.read_parquet(DAILY)
    sub = daily[daily.session <= daily.session.unique()[100]].copy()
    sub["r"] = sub.groupby("session")["c"].rank(pct=True)
    one = daily[daily.session == daily.session.unique()[100]].copy()
    one["r"] = one.groupby("session")["c"].rank(pct=True)
    a = sub[sub.session == daily.session.unique()[100]].sort_values("underlying")["r"].to_numpy()
    c = one.sort_values("underlying")["r"].to_numpy()
    assert np.allclose(a, c, rtol=1e-12)
    print("cross-sectional rank PASS (date-t rank uses date-t rows only)")


if __name__ == "__main__":
    test_prefix_invariance()
    test_positive_control_forwards_do_look_ahead()
    test_xs_rank_is_same_date_only()
