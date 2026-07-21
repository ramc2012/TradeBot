"""VERIFY-D2: the hourly arm's gate reads a SAME-SESSION daily value.

DEFECT UNDER TEST. div_hourly.py gates each hourly cross on session `a` with
    st = div_of[(u, a)]           # (div at a, daily macd-sig diff at a)
    if not st[0] or st[1] > 0: skip
Both are daily-bar quantities of session `a`, which do not exist until that
session CLOSES at 15:30 IST. An hourly cross at 11:15 on session `a` is
therefore being filtered on information from four hours in its own future --
and the `st[1] > 0` clause specifically drops hourly crosses that happen on
the same session as the daily crossover, which is 29.4% of the oracle sample.

FIX: gate on session a-1 (the last daily bar that had closed). Everything else
-- barriers, ATR source, episode clustering, the oracle arm -- is untouched,
so the two runs are directly comparable.
"""
from __future__ import annotations
import os, sys
import numpy as np, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DIV = os.path.abspath(os.path.join(HERE, ".."))
DATA = os.path.join(DIV, "data")
sys.path.insert(0, DIV); sys.path.insert(0, os.path.join(DIV, "..", "cascade"))
sys.path.insert(0, os.path.join(DIV, "..", "setups_2d3d"))
import div_build as B, div_defs as D, div_hourly as H
import run_cascade as rc


def build(hx, div_of, atr_of, bars, gate_lag: int):
    rows = []
    for u, lst in hx.items():
        for a, bidx in lst:
            st = div_of.get((u, a - gate_lag))
            if st is None or not st[0] or st[1] > 0:
                continue
            p = H._pos_after(bars, u, a, bidx)
            if p is None:
                continue
            atr_h = atr_of.get((u, a - 1), np.nan)
            if not np.isfinite(atr_h) or atr_h <= 0:
                continue
            s = rc.path_stats(bars.u[u], p, D.SIDE, atr_h, a + D.HORIZON_SESSIONS)
            if not s:
                continue
            rows.append({"underlying": u, "sidx_entry": a, **s})
    t = pd.DataFrame(rows)
    if t.empty:
        return t
    t = t.sort_values(["underlying", "sidx_entry"])
    prev = t.groupby("underlying")["sidx_entry"].shift(1)
    return t[(((t["sidx_entry"] - prev) > D.EPISODE_GAP_SESSIONS) | prev.isna()).to_numpy()]


def main():
    daily, intra = B.load_panel()
    e = B.build_daily_elements(daily)
    h = B.build_hourly(intra)
    bars = rc.Bars(intra, daily)
    ep = pd.read_parquet(os.path.join(DATA, "episodes.parquet"))
    real = ep[ep["arm"] == D.PRIMARY_ARM].copy()
    sidx_of = {(r.underlying, r.session): int(r.sidx) for r in e.itertuples()}
    atr_of = {(r.underlying, int(r.sidx)): float(r.D_atr14) for r in e.itertuples()}
    div_of = {(r.underlying, int(r.sidx)): (bool(r.div), float(r.D_macd - r.D_macd_sig))
              for r in e.itertuples()}
    hx = {}
    for u, g in h.groupby("underlying", sort=False):
        g = g[g["H_cross"].fillna(False).to_numpy(bool)]
        arr = [(sidx_of.get((u, s), -1), int(b)) for s, b in zip(g["session"], g["bidx"])]
        hx[u] = sorted([(a, b) for a, b in arr if a >= 0])

    print("=" * 78)
    print("HOURLY GATE: same-session (as shipped) vs prior-session (causal)")
    print("=" * 78)
    for lag, name in ((0, "as shipped (lookahead)"), (1, "REPAIRED (causal)")):
        t = build(hx, div_of, atr_of, bars, lag)
        print(f"{name:24s} n={len(t):5d}  P(large)={t['large'].mean():.4f}  "
              f"term_atr={t['term_atr'].mean():+.4f}  mfe={t['mfe_atr'].mean():.3f}  "
              f"mae={t['mae_atr'].mean():.3f}")
    print(f"{'daily cross_div':24s} n={len(real):5d}  P(large)={real['large'].mean():.4f}  "
          f"term_atr={real['term_atr'].mean():+.4f}")
    # cluster-bootstrap the repaired hourly arm against the daily arm
    t1 = build(hx, div_of, atr_of, bars, 1)
    t1["arm"] = "hourly_causal"; r2 = real.copy(); r2["arm"] = "daily"
    both = pd.concat([t1, r2[["underlying", "arm", "large", "term_atr"]]], ignore_index=True)
    for m in ("large", "term_atr"):
        r = rc.cluster_boot_diff(both, m, (both["arm"] == "hourly_causal").to_numpy(),
                                 (both["arm"] == "daily").to_numpy())
        print(f"  repaired hourly - daily  {m:9s} diff {r['diff']:+.4f} "
              f"[{r['lo']:+.4f},{r['hi']:+.4f}] p={r['p']:.4f}")


if __name__ == "__main__":
    main()
