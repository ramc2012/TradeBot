"""Self-check on the ATR-matched bootstrap + a few even-handed counter-checks.

If month x ATR-quintile cells are tiny the draw is forced onto the signal day
itself and the null is spuriously inflated -> p too high -> my refutation would
be the artefact. Check cell occupancy, then redo with COARSER, better-populated
strata (quarter x ATR tercile, and ATR quintile alone) as well.

Also, in fairness to the claim:
  - sign test on the de-overlapped strategy trades
  - strategy excess measured against the NO-SIGNAL mean (the claim's benchmark)
    rather than the all-session mean
  - reproduce the rolling-protocol collapse
"""
from __future__ import annotations

import os
import sys
from datetime import date

import numpy as np
import pandas as pd
import psycopg2
from scipy import stats

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.mp_auction import dsn, load                          # noqa: E402
from research.mp_walkforward import walk_forward                   # noqa: E402
from research.mp_swing_failure import (build, rule_table, stack,   # noqa: E402
                                       t_stat, newey_west_t)
from research.mp_swing_refute import hac_dummy, OOS_START          # noqa: E402

H = 4


def strat_boot(dfm, need, cells, yv, rng, n=20000):
    draws = np.empty(n)
    forced = 0
    for i in range(n):
        idx = []
        for key, k in need.items():
            pool = cells.get(key, np.array([], dtype=int))
            if len(pool) == 0:
                continue
            idx.extend(rng.choice(pool, size=min(k, len(pool)), replace=False))
        draws[i] = yv[idx].mean()
    return draws


def main() -> int:
    connection = psycopg2.connect(dsn())
    try:
        raw = load(connection, ["BANKNIFTY"], date(2021, 1, 1))
    finally:
        connection.close()
    bn = build(raw[raw["underlying"] == "BANKNIFTY"]).sort_values("dt").reset_index(drop=True)
    bn["sess"] = np.arange(len(bn))

    o = bn[(bn["dt"] >= OOS_START) & bn[f"long{H}"].notna()].reset_index(drop=True)
    fires = o["big_tail_low"].fillna(False).astype(bool).values
    y = o[f"long{H}"].values
    atr = o["atr20"].values * 100.0
    m = np.isfinite(atr) & np.isfinite(y)
    d = pd.DataFrame({"y": y[m], "atr": atr[m], "sig": fires[m]})
    d["mo"] = o.loc[m, "dt"].dt.to_period("M").values
    d["qt"] = o.loc[m, "dt"].dt.to_period("Q").values
    obs = d.loc[d["sig"], "y"].mean()
    yv = d["y"].values
    rng = np.random.default_rng(11)

    print("\n" + "=" * 100)
    print("SELF-CHECK: is the ATR-matched bootstrap an artefact of tiny strata?")
    print("=" * 100)
    d["aq5"] = pd.qcut(d["atr"], 5, labels=False, duplicates="drop")
    d["aq3"] = pd.qcut(d["atr"], 3, labels=False, duplicates="drop")

    specs = [
        ("month only            (claim's test B)", ["mo"]),
        ("month x ATR quintile  (my test)       ", ["mo", "aq5"]),
        ("quarter x ATR tercile (coarser)       ", ["qt", "aq3"]),
        ("quarter x ATR quintile                ", ["qt", "aq5"]),
        ("ATR quintile only                     ", ["aq5"]),
    ]
    for label, keys in specs:
        cells = d.groupby(keys).indices
        need = d[d["sig"]].groupby(keys).size()
        pool_sizes = np.array([len(cells.get(k, [])) for k in need.index])
        forced = (pool_sizes <= need.values).sum()
        occ = (need.values / np.maximum(pool_sizes, 1))
        draws = strat_boot(d, need, cells, yv, np.random.default_rng(11))
        p = float((draws >= obs).mean())
        print(f"   {label}  cells {len(need):>3}  median pool {np.median(pool_sizes):>5.1f}  "
              f"fully-forced cells {forced:>3}/{len(need)}  mean occupancy "
              f"{occ.mean()*100:>4.0f}%")
        print(f"       null mean {draws.mean():+.3f}%  95th {np.percentile(draws,95):+.3f}%  "
              f"observed {obs:+.3f}%  p {p:.4f}  "
              f"{'survives' if p < 0.05 else 'FAILS at 5%'}")

    print("\n" + "=" * 100)
    print("IN FAIRNESS TO THE CLAIM")
    print("=" * 100)
    rules = rule_table(bn)
    st = stack(bn, rules, H)
    cands = {n: (st["rule"] == n) for n in rules}
    res = walk_forward(st, cands, "ret", train_m=18, test_m=6, anchored=True, min_trades=12)
    oos = res["oos"].sort_values("dt").reset_index(drop=True)
    oos["sess"] = oos["dt"].dt.normalize().map(dict(zip(bn["dt"], bn["sess"])))
    kept, last = [], -99
    for i in range(len(oos)):
        s = oos["sess"].iloc[i]
        if s - last >= H:
            kept.append(i)
            last = s
    de = oos.loc[kept, "ret"].values
    r = oos["ret"].values
    nw = (d.loc[~d["sig"], "y"]).mean()
    allm = bn[(bn["dt"] >= oos["dt"].min().normalize()) &
              (bn["dt"] <= oos["dt"].max().normalize())][f"long{H}"].dropna().mean()
    k = int((de > 0).sum())
    print(f"   de-overlapped strategy: {k}/{len(de)} wins, sign-test p "
          f"{stats.binomtest(k, len(de), 0.5, alternative='greater').pvalue:.3f}")
    print(f"   median de-overlapped trade {np.median(de):+.3f}%   "
          f"median all 165 {np.median(r):+.3f}%")
    print(f"   strategy excess vs NO-SIGNAL mean ({nw:+.3f}%):  full "
          f"{r.mean()-nw:+.3f}% t {t_stat(r-nw):+.2f}   de-ovl {de.mean()-nw:+.3f}% "
          f"t {t_stat(de-nw):+.2f}")
    print(f"   strategy excess vs ALL-SESSION mean ({allm:+.3f}%): full "
          f"{r.mean()-allm:+.3f}% t {t_stat(r-allm):+.2f}   de-ovl {de.mean()-allm:+.3f}% "
          f"t {t_stat(de-allm):+.2f}")

    print("\n   ROLLING protocol reproduction:")
    for lab, kw in [("anchored 18/6", dict(train_m=18, test_m=6, anchored=True)),
                    ("ROLLING  18/6", dict(train_m=18, test_m=6, anchored=False)),
                    ("ROLLING  24/6", dict(train_m=24, test_m=6, anchored=False))]:
        rr = walk_forward(st, cands, "ret", min_trades=12, **kw)
        print(f"      {lab}  n {rr['n']:>4}  mean {rr['mean']:+.3f}%  t {rr['t']:+.2f}  "
              f"stability {rr['stability']*100:.0f}%  rules {len(set(rr['picks']))}")
    print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
