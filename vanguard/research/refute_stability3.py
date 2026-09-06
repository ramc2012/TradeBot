"""Refutation part 3: the /atr20 'stability', the drift decomposition done
symmetrically for both cohorts, and costs."""
from __future__ import annotations
import os, sys, warnings
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.refute_cache import get, INDICES
from research.mp_time_stability import (t_of, summarise, two_sample, cohort_row,
                                        per_session_break_bias, per_session_up_minus_down)
warnings.filterwarnings("ignore")
PCT = 100.0
pd.set_option("display.width", 240)
s, bank, bars = get()
for f in (s, bank):
    f.sort_values(["underlying", "dt"], inplace=True)
s = s.reset_index(drop=True); bank = bank.reset_index(drop=True)
s["year"] = pd.to_datetime(s["dt"]).dt.year
s["fwd_ret"] = s["ret_3d"] * s["side"]
bank["fwd_ret"] = bank["ret_3d"] * bank["side"]

def add_causal_atr(f):
    out = []
    for _, g in f.groupby("underlying", sort=False):
        g = g.sort_values("dt").copy()
        pc = g["close"].shift(1)
        tr = pd.concat([g["high"]-g["low"], (g["high"]-pc).abs(), (g["low"]-pc).abs()],
                       axis=1).max(axis=1)
        # ATR through YESTERDAY only -- known at 10:15 today
        g["atr_causal"] = tr.shift(1).rolling(20, min_periods=10).mean() / pc
        out.append(g)
    return pd.concat(out, ignore_index=True)
s = add_causal_atr(s); bank = add_causal_atr(bank)

print("=" * 96)
print("D. THE 'REMARKABLY STABLE VOLATILITY GEOMETRY' IS A VOL/VOL TAUTOLOGY,")
print("   AND ITS DENOMINATOR IS NOT CAUSAL. mp_profile's atr20 = 20-session")
print("   mean TR INCLUDING TODAY, divided by TODAY's close. So ib_width (part")
print("   of today's true range) is divided by a number that contains it, and")
print("   the divisor is unknown until 15:15. atr_causal uses TR through")
print("   yesterday over yesterday's close -- available at the 10:15 break.")
print("=" * 96)
def ratio_table(f, key, cols=("ib_width","mfe_3d","mae_3d")):
    rows = {}
    for k in sorted(f[key].dropna().unique()):
        sub = f[f[key] == k]; b = sub[sub["side"] != 0]
        r = {"sessions": len(sub), "atr20%": sub["atr20"].median()*PCT,
             "atr_causal%": sub["atr_causal"].median()*PCT}
        for c in cols:
            src = sub if c == "ib_width" else b
            r[f"{c}/atr20"] = float((src[c]/src["atr20"]).median())
            r[f"{c}/atrC"] = float((src[c]/src["atr_causal"]).median())
        rows[k] = r
    return pd.DataFrame(rows).T
rt = ratio_table(s, "year")
print("\n   four indices pooled, by year:")
print(rt.to_string(float_format=lambda v: f"{v:8.3f}"))
bk = bank.copy(); bk["all"] = "banks 2024-09.."
print("\n   bank cohort, same columns:")
print(ratio_table(bk, "all").to_string(float_format=lambda v: f"{v:8.3f}"))

def spread(x):
    x = pd.Series(x).dropna()
    return float((x.max()-x.min())/x.mean()*PCT)
print("\n   YEAR-TO-YEAR SPREAD (max-min as % of mean) -- 'flat' means small:")
for c in rt.columns:
    if c == "sessions": continue
    print(f"     {c:20s} {spread(rt[c]):6.1f}%")
print("   The raw atr20 spread is what the report calls unstable; the ratios")
print("   are 10-20% wide, and swapping in the CAUSAL atr widens them.")

print("\n   IS THE RATIO EVEN A FINDING? ib_width is the first-hour range and")
print("   atr20 is the 20-day mean daily range. 'first hour is ~half a day's")
print("   range' is intraday variance accumulation, not market profile:")
for lbl, f in (("indices", s), ("banks", bank)):
    sub = f.dropna(subset=["ib_width", "atr_causal"])
    same = (sub["ib_width"] / ((sub["high"]-sub["low"])/sub["close"])).median()
    print(f"     {lbl:8s} median ib_width / SAME-DAY range = {same:.3f}"
          f"   (sqrt(2/13) = {np.sqrt(2/13):.3f} if variance accrued uniformly)")

print("\n   FORMAL TEST that ibw/atrC is flat across years (it is not):")
r = s.dropna(subset=["ib_width","atr_causal"]).copy()
r["v"] = r["ib_width"]/r["atr_causal"]
sess = r.groupby("dt")["v"].mean()
yy = pd.to_datetime(sess.index).year
gm = sess.groupby(yy).mean()
for a_, b_ in ((2022, 2025), (2021, 2026), (2023, 2026)):
    d, t, n1, n2 = two_sample(sess[yy == a_], sess[yy == b_])
    print(f"     {a_} ({gm[a_]:.3f}) vs {b_} ({gm[b_]:.3f}): diff {d:+.3f} Welch t {t:+.2f}"
          f"  (n {n1}/{n2})")

# ------------------------------------------------------------------ symmetry
print("\n" + "=" * 96)
print("E. THE DRIFT DECOMPOSITION IS NOT DONE THE SAME WAY ON BOTH SIDES.")
print("   Indices: per-name Welch (clean). Banks: 17 names pooled (illegal).")
print("   Here is the SAME within-session paired test on both cohorts:")
print("=" * 96)
for lbl, f in (("indices (4 names)", s), ("banks (17 names)", bank)):
    for col in ("ret_3d", "fwd_ret"):
        p = per_session_up_minus_down(f, col)
        m, t, n = summarise(p)
        h = p.sort_index(); mid = len(h)//2
        print(f"   {lbl:18s} {col:8s} paired {m:+7.3f}pp  t {t:+6.2f}  n {n:4d}"
              f"   H1 {h.iloc[:mid].mean():+6.3f}(t{t_of(h.iloc[:mid]):+5.2f})"
              f"  H2 {h.iloc[mid:].mean():+6.3f}(t{t_of(h.iloc[mid:]):+5.2f})")
print("   Note the index paired fwd_ret n: sessions holding both an up- and a")
print("   down-break among 4 names that move as one are the ODD days, not a")
print("   random sample -- so even the 'clean' index test is a selected subset.")

# ------------------------------------------------------------------ costs
print("\n" + "=" * 96)
print("F. COSTS. 0.05% per side = 0.10% round trip on spot. Mean fwd_ret by")
print("   side, in pp, against that hurdle.")
print("=" * 96)
COST = 0.10
for lbl, f in (("indices", s), ("banks", bank)):
    b = f[(f["side"] != 0) & f["fwd_ret"].notna()]
    u = b[b["side"] == 1]["fwd_ret"].mean()*PCT
    d = b[b["side"] == -1]["fwd_ret"].mean()*PCT
    print(f"   {lbl:8s} after up {u:+.3f}pp  after down {d:+.3f}pp"
          f"  -> drift {(u+d)/2:+.3f}  information {u-d:+.3f}")
    print(f"            LONG every up break, 3-session hold, net of {COST}%:"
          f" {u-COST:+.3f}pp per trade")
    print(f"            SHORT every down break (P&L = -fwd_ret), net:"
          f" {-d-COST:+.3f}pp per trade")
    # the correct t on the net series, one obs per session
    for sd, nm in ((1, "long up-breaks"), (-1, "short down-breaks")):
        leg = b[b["side"] == sd].copy()
        leg["pnl"] = (leg["fwd_ret"]*sd*np.sign(sd) if False else leg["fwd_ret"]*sd)*PCT
        leg["pnl"] = (leg["fwd_ret"]*PCT if sd == 1 else -leg["fwd_ret"]*PCT) - COST
        ses = leg.groupby("dt")["pnl"].mean()
        print(f"              {nm:18s} mean {ses.mean():+.3f}pp  t {t_of(ses):+.2f}"
              f"  n {len(ses)} sessions")
print("\n   And the at-highs 'edge' priced: it is a break-RATE tilt, so the only")
print("   way to trade it is to be long up-breaks when NIFTY is near its high.")
nif = s[s["underlying"]=="NIFTY"].sort_values("dt").set_index("dt")["close"].dropna()
ddp = (nif/nif.cummax()-1.0).shift(1)
s["dd_prev"] = pd.to_datetime(s["dt"]).map(ddp)
for tag, mask in (("SAME-session dd (look-ahead)",
                   pd.to_datetime(s["dt"]).map(nif/nif.cummax()-1.0) >= -0.02),
                  ("PRIOR-session dd (causal)", s["dd_prev"] >= -0.02)):
    q = s[mask & (s["side"] == 1) & s["fwd_ret"].notna()]
    ses = (q.groupby("dt")["fwd_ret"].mean()*PCT) - COST
    print(f"     {tag:32s} long up-breaks net {ses.mean():+.3f}pp"
          f"  t {t_of(ses):+.2f}  n {len(ses)}")
