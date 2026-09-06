"""Sanity checks on mp_va_scalp: is the machinery doing what the docstring says?

Six things that, if broken, would silently manufacture or destroy an edge:
  1. bars per session -- the 2-bar exit must never fall off the end for bar<=10
  2. one entry per session per rule
  3. every entry sits in bars 2..10
  4. the fade/follow pairs are exact mirrors (a construction check)
  5. the 80%-rule side matches where the session actually opened
  6. how much edge this sample could DETECT, given the dispersion
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.mp_auction import dsn  # noqa: E402
from research.mp_intraday import load_intraday  # noqa: E402
from research.mp_va_scalp import BAR_HI, BAR_LO, COST, build_rules, prep, stack  # noqa: E402

start = date.today() - timedelta(days=int(5.4 * 365.25))
conn = psycopg2.connect(dsn())
try:
    raw = load_intraday(conn, ["BANKNIFTY"], start)
finally:
    conn.close()
f = prep(raw)
f = f[f["py_val"].notna()].reset_index(drop=True)
rb = build_rules(f)
t = stack(f, rb)

n = f.groupby(["underlying", "dt"]).size()
print(f"1. bars/session: {n.value_counts().to_dict()}   "
      f"max eligible bar {BAR_HI} + 2 = {BAR_HI + 2}, min session length {n.min()}")
f["_nbar"] = f.groupby(["underlying", "dt"])["bar"].transform("max")
e = f[f["bar"].between(BAR_LO, BAR_HI)]
print(f"   eligible bars whose 2-bar exit had to be truncated: "
      f"{int((e['bar'] + 2 > e['_nbar']).sum())}")
odd = n[n != 13]
print(f"   sessions not 13 bars: {len(odd)} -> {[str(d[1]) for d in odd.index]}")
print(f"   trade rows falling on those sessions: "
      f"{int(t['dt'].dt.date.isin([d[1] for d in odd.index]).sum())} of {len(t)}")

per = t.assign(day=t["ts"].dt.date).groupby(["rule", "day"]).size()
print(f"2. entries per (rule, session): max {per.max()}  "
      f"{'OK' if per.max() == 1 else 'BROKEN -- stacked entries'}")
print(f"3. entry bars: min {t['bar'].min()} max {t['bar'].max()}  "
      f"{'OK' if t['bar'].between(BAR_LO, BAR_HI).all() else 'BROKEN'}")

print("4. fade/follow mirrors (n equal, mean exactly negated):")
for tag in ("poc", "vah", "val"):
    a = t[t["rule"] == f"pv_{tag}_fade"]["ret_eod"]
    b = t[t["rule"] == f"pv_{tag}_follow"]["ret_eod"]
    ok = len(a) == len(b) and abs(a.mean() + b.mean()) < 1e-9
    print(f"   pv_{tag}: n {len(a)}/{len(b)}  {a.mean():+.4f} vs {b.mean():+.4f}  "
          f"{'OK' if ok else 'BROKEN'}")

m, s = rb.rules["r80_both"]
x = f.loc[m].copy()
x["side"] = s[m.values]
op = f[f["bar"] == 0][["dt", "sess_open", "close", "py_val", "py_vah"]].rename(
    columns={"close": "c0"})
x = x.merge(op, on="dt", suffixes=("", "_b0"))
long_ok = (x[x["side"] > 0]["c0"] < x[x["side"] > 0]["py_val_b0"]).all()
short_ok = (x[x["side"] < 0]["c0"] > x[x["side"] < 0]["py_vah_b0"]).all()
true_open_agree = np.mean(np.sign(np.where(
    x["sess_open"] < x["py_val_b0"], 1,
    np.where(x["sess_open"] > x["py_vah_b0"], -1, 0))) == x["side"])
print(f"5. 80% side vs bar-0 CLOSE: long {long_ok} short {short_ok}   "
      f"share where the true OPEN agrees with the bar-0 close: "
      f"{true_open_agree * 100:.0f}%")
print(f"   entries also inside prior value at entry: "
      f"{f.loc[m, 'in_py_value'].mean() * 100:.0f}%")

print("6. detectable-edge power, per rule family")
for name in ("r80_long", "r80_both", "poc_rev_50", "edge_fade_both"):
    r = t[t["rule"] == name]["ret_eod"].dropna()
    sd = r.std(ddof=1)
    oos_n = 97
    print(f"   {name:<16} sd {sd:.3f}%  n_full {len(r):>4}  "
          f"smallest mean detectable at t=2: full-sample "
          f"{2 * sd / np.sqrt(len(r)):.3f}%, at OOS n={oos_n} "
          f"{2 * sd / np.sqrt(oos_n):.3f}%   (cost is {COST}%)")
