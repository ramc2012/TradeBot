from __future__ import annotations
import os, sys, warnings
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.refute_cache import get, INDICES
from research.mp_time_stability import t_of, summarise, two_sample, cohort_row, per_session_break_bias
warnings.filterwarnings("ignore")
PCT = 100.0; pd.set_option("display.width", 240)
s, bank, bars = get()
s = s.sort_values(["dt","underlying"]).reset_index(drop=True)

print("=" * 96)
print("K. THE MOST GENEROUS CAUSAL REGIME LABEL still kills the at-highs tilt.")
print("   dd_ib = IB close (10:15 today) / running peak of closes THROUGH")
print("   YESTERDAY - 1. This is the maximum information a trader has when the")
print("   break is being watched for, and it still uses today's price.")
print("=" * 96)
nif = s[s["underlying"]=="NIFTY"].sort_values("dt").set_index("dt")
peak_prev = nif["close"].cummax().shift(1)
defs = {
 "same-session close (as published)": nif["close"]/nif["close"].cummax() - 1.0,
 "prior close / prior peak (causal)": (nif["close"]/nif["close"].cummax()-1.0).shift(1),
 "today's 10:15 IB close / prior peak (causal, most generous)":
     nif["ib_ref"]/peak_prev - 1.0,
}
for lbl, dd in defs.items():
    s["_dd"] = pd.to_datetime(s["dt"]).map(dd)
    v = s["_dd"]
    bkt = pd.Series(np.where(v >= -0.02, "at-highs",
                    np.where(v >= -0.07, "pullback", "drawdown")), index=s.index)
    bkt[v.isna()] = "NA"
    print(f"\n   --- {lbl} ---")
    out = {}
    for k in ("at-highs","pullback","drawdown"):
        sub = s[bkt == k]
        if not len(sub): continue
        r = cohort_row(sub); bi = per_session_break_bias(sub)
        m,t,n = summarise(bi)
        out[k] = {"sessions": sub["dt"].nunique(), "up%": r["up%"], "down%": r["down%"],
                  "d": m, "t": t}
    print(pd.DataFrame(out).T.to_string(float_format=lambda x: f"{x:9.3f}"))
    for x,y in (("at-highs","drawdown"),("at-highs","pullback")):
        d,t,n1,n2 = two_sample(per_session_break_bias(s[bkt==x]),
                               per_session_break_bias(s[bkt==y]))
        print(f"     contrast {x} vs {y}: {d:+.3f}  Welch t {t:+.2f}")

print("\n" + "=" * 96)
print("L. HINDSIGHT LEG SELECTION, LENGTH-MATCHED to the reported episodes.")
print("=" * 96)
r3 = nif[nif["side"]==1]["ret_3d"].dropna()*PCT
dts = np.array(sorted(nif.index)); rng = np.random.default_rng(11)
for name, L, reported in (("bull-leg (250 sess)", 250, +0.371),
                          ("DD1 (166 sess)", 166, -0.592)):
    vals = []
    for _ in range(4000):
        i = rng.integers(0, len(dts)-L)
        w = r3[(r3.index >= dts[i]) & (r3.index <= dts[i+L])]
        if len(w) > 25: vals.append(w.mean())
    vals = np.array(vals)
    pct = float((vals <= reported).mean()*PCT)
    print(f"   {name:20s} reported {reported:+.3f}  |  random windows:"
          f" 5th {np.percentile(vals,5):+.3f}  med {np.median(vals):+.3f}"
          f"  95th {np.percentile(vals,95):+.3f}"
          f"  -> reported sits at the {pct:.1f}th percentile")
print("   Picking the STRONGEST advance and the DEEPEST decline is picking the")
print("   two tails of exactly this distribution, so the 'difference, Welch")
print("   t +3.48' is the selection, restated.")

print("\n" + "=" * 96)
print("M. IS THE BANKS' BREAK-SIDE GAP FULLY THE DAY-SIGN MIX? Condition each")
print("   name-day on the sign of its OWN post-IB move (close vs the 10:15")
print("   close) and see what break-side asymmetry is left.")
print("=" * 96)
for lbl, f in (("banks 2024-09..", bank), ("indices, all", s)):
    g = f.copy(); g["post"] = g["close"]/g["ib_ref"] - 1.0
    print(f"   --- {lbl} ---")
    for nm, sel in (("post-IB up  ", g["post"] > 0), ("post-IB down", g["post"] < 0)):
        q = g[sel]; r = cohort_row(q)
        print(f"     {nm}  n {len(q):5d}  up {r['up%']:5.2f}  down {r['down%']:5.2f}"
              f"  never {r['never%']:5.2f}  gap(down-up) {r['down%']-r['up%']:+6.2f}pp")
    # mix-adjusted: reweight the two sign groups to 50/50
    parts = []
    for sel in (g["post"] > 0, g["post"] < 0):
        r = cohort_row(g[sel]); parts.append(r["down%"] - r["up%"])
    print(f"     observed gap {cohort_row(g)['down%']-cohort_row(g)['up%']:+6.2f}pp"
          f"   |  gap with the two sign groups reweighted 50/50:"
          f" {np.mean(parts):+6.2f}pp")
