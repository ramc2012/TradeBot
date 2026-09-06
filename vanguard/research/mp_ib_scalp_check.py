"""Sanity checks for mp_ib_scalp: does the stacked frame line up with walk_forward?

walk_forward re-sorts the frame by dt and resets the index, so a candidate mask
built on the caller's frame is only valid if that sort is the identity. This
recomputes the fold trades by hand and compares them to what the engine
recorded, and re-derives one rule's returns straight from the bars.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.mp_ib_scalp import RULES, add_horizon, load_bars, signals, stack  # noqa: E402
from research.mp_walkforward import walk_forward  # noqa: E402


class A:
    symbol, years, dsn, refresh = "BANKNIFTY", 5.4, "", False


def main() -> int:
    f = add_horizon(load_bars(A))
    sig = signals(f)
    t = stack(sig)

    # 1. the engine's internal sort must be the identity on this frame
    f2 = t.sort_values("dt").reset_index(drop=True)
    assert (f2["rule"].values == t["rule"].values).all(), "sort permuted rows"
    assert t["dt"].is_monotonic_increasing and t["dt"].is_unique
    print("1. dt unique + monotone, engine sort is identity          OK")

    # 2. hand-count the first fold the engine reported
    m = t["dt"].dt.to_period("M")
    uniq = sorted(m.unique())
    te = t[m.isin(uniq[18:24])]
    n_hand = int((te["rule"] == "ibf_wide").sum())
    res = walk_forward(t, {r: (t["rule"] == r) for r in RULES}, "r_eod")
    n_eng = int(res["folds"].iloc[0]["n_test"])
    print(f"2. fold 1 ibf_wide trades  hand={n_hand}  engine={n_eng}         "
          f"{'OK' if n_hand == n_eng else 'MISMATCH'}")
    assert n_hand == n_eng

    # 3. mean of the engine's own OOS rows recomputed
    o = res["oos"]
    print(f"3. OOS mean recomputed {o['r_eod'].mean():+.4f} vs "
          f"{res['mean']:+.4f}                    "
          f"{'OK' if abs(o['r_eod'].mean() - res['mean']) < 1e-9 else 'MISMATCH'}")

    # 4. re-derive ibc returns from raw bars, ignoring the signals() code path
    bad = 0
    checked = 0
    ibc = sig[sig["rule"] == "ibc"]
    for _, row in ibc.sample(200, random_state=7).iterrows():
        g = f[f["dt"] == row["dt"]].sort_values("bar")
        k = g[g["bar"] == row["bar"]].iloc[0]
        want = (g["close"].iloc[-1] / k["close"] - 1) * 100 * row["side"]
        checked += 1
        if abs(want - row["r_eod"]) > 1e-6:
            bad += 1
        # the entry bar must be the first close beyond the IB, in the right side
        prior = g[g["bar"] < row["bar"]]
        inside_before = ((prior["close"] <= k["ib_hi"]) &
                         (prior["close"] >= k["ib_lo"])).all()
        beyond = (k["close"] > k["ib_hi"]) if row["side"] > 0 else (k["close"] < k["ib_lo"])
        if not (inside_before and beyond):
            bad += 1
    print(f"4. 200 random ibc trades re-derived from bars: {bad} bad        "
          f"{'OK' if bad == 0 else 'MISMATCH'}")

    # 5. entry window and one-trade-per-session-per-rule
    print(f"5. entry bars {sig['bar'].min()}..{sig['bar'].max()}, "
          f"dupes per (dt,rule) = {int(sig.duplicated(['dt', 'rule']).sum())}      "
          f"{'OK' if sig['bar'].between(2, 10).all() else 'MISMATCH'}")

    # 6. no look-ahead in narrow/wide: recompute the split independently
    sess = f.groupby("dt")["ib_width"].first().sort_index()
    tr = sess.rolling(60, min_periods=20).median().shift(1)
    nw = sig[sig["rule"] == "ibc_narrow"]["dt"].unique()
    wd = sig[sig["rule"] == "ibc_wide"]["dt"].unique()
    ok = all(sess[d] < tr[d] for d in nw) and all(sess[d] >= tr[d] for d in wd)
    print(f"6. narrow/wide split uses only prior sessions              "
          f"{'OK' if ok else 'MISMATCH'}")

    # 7. how much of the OOS window is actually traded
    span = (t["dt"].max() - t["dt"].min()).days / 365.25
    print(f"\n   full span {span:.1f}y, OOS n={res['n']} trades over "
          f"{pd.to_datetime(o['dt']).dt.date.nunique()} sessions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
