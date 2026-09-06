"""Refutation part 2: the t-statistics, the /atr 'stability', and costs."""
from __future__ import annotations
import os, sys, warnings
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.refute_cache import get, INDICES
from research.mp_time_stability import (t_of, summarise, two_sample, cohort_row,
                                        per_session_break_bias, per_session_up_minus_down,
                                        two_sample_block, nonoverlap_t)
warnings.filterwarnings("ignore")
PCT = 100.0
pd.set_option("display.width", 220)
s, bank, bars = get()
s = s.sort_values(["dt", "underlying"]).reset_index(drop=True)
s["year"] = pd.to_datetime(s["dt"]).dt.year
s["fwd_ret"] = s["ret_3d"] * s["side"]
bank = bank.sort_values(["dt", "underlying"]).reset_index(drop=True)
bank["fwd_ret"] = bank["ret_3d"] * bank["side"]

def nw_t(x, lags=10):
    x = pd.Series(x).dropna().astype(float); n = len(x)
    if n < 20: return np.nan
    e = x.values - x.mean(); v = (e @ e)/n
    for L in range(1, min(lags, n-1)+1):
        v += 2*(1-L/(lags+1))*((e[L:] @ e[:-L])/n)
    return float(x.mean()/np.sqrt(v/n)) if v > 0 else np.nan

# ================================================================== C
print("=" * 96)
print("C. THE BANK 'CONTRARIAN INFORMATION' RESULT USES THE FORBIDDEN t.")
print("   two_sample_block() is called on the WHOLE 17-name bank frame, so its")
print("   Welch t treats 17 correlated bank-days as 17 independent draws --")
print("   exactly what METHOD RULE 2 forbids. (For the four indices the script")
print("   is careful and goes per name; for the banks it is not.)")
print("=" * 96)
b = bank[bank["side"] != 0]
d, t, nu, nd = two_sample(b[b["side"] == 1].set_index("dt")["fwd_ret"] * PCT,
                          b[b["side"] == -1].set_index("dt")["fwd_ret"] * PCT)
print(f"   AS PUBLISHED  (pooled 17 names, n_up {nu}, n_dn {nd}): "
      f"up-down {d:+.3f}pp  Welch t {t:+.2f}")
print(f"   names pooled: {bank['underlying'].nunique()};  naive inflation ~sqrt(17) = {np.sqrt(17):.1f}x")

paired = per_session_up_minus_down(bank, "fwd_ret")
m, tt, nn = summarise(paired)
print(f"\n   CORRECT: within-SESSION paired (up mean - down mean), t across sessions")
print(f"     mean {m:+.3f}pp  t {tt:+.2f}  n {nn} sessions"
      f"   NW t(10) {nw_t(paired):+.2f}   non-overlap t {nonoverlap_t(paired):+.2f}")
h = paired.sort_index(); mid = len(h)//2
print(f"     split-half  H1 {h.iloc[:mid].mean():+.3f} (t {t_of(h.iloc[:mid]):+.2f})"
      f"   H2 {h.iloc[mid:].mean():+.3f} (t {t_of(h.iloc[mid:]):+.2f})")

print("\n   PER NAME (banks), the same test the script applies to the indices:")
rows = {}
for nm in sorted(bank["underlying"].unique()):
    f = bank[(bank["underlying"] == nm) & (bank["side"] != 0)]
    dd, ttt, u, l = two_sample(f[f["side"] == 1]["fwd_ret"] * PCT,
                               f[f["side"] == -1]["fwd_ret"] * PCT)
    rows[nm] = {"up-down pp": dd, "Welch t": ttt, "n_up": u, "n_dn": l}
pn = pd.DataFrame(rows).T
print(pn.to_string(float_format=lambda v: f"{v:8.3f}"))
print(f"   names with t < -1.96: {(pn['Welch t'] < -1.96).sum()} of {len(pn)};"
      f"  median per-name t {pn['Welch t'].median():+.2f}")

print("\n   DUPLICATE-LABEL BUG in _drop2 for the pooled bank frame:")
up = b[b["side"] == 1].set_index("dt")["fwd_ret"]
print(f"     up leg has {len(up)} rows but only {up.index.nunique()} distinct dt labels.")
print("     _drop2 does up.drop(up.index[ku]) -- pandas drops EVERY row sharing")
print("     that label, i.e. a whole session of ~17 names, not one observation.")

print("\n   Same check for the INDEX per-name tests (these are clean):")
for nm in INDICES:
    f = s[(s["underlying"] == nm) & (s["side"] != 0) & s["fwd_ret"].notna()]
    u = f[f["side"] == 1].set_index("dt")["fwd_ret"]
    print(f"     {nm:10s} up rows {len(u)}, distinct dt {u.index.nunique()}"
          f"  -> {'OK' if len(u)==u.index.nunique() else 'DUPLICATES'}")

# ================================================================== C2
print("\n" + "=" * 96)
print("C2. nonoverlap_t IS MISAPPLIED TO THE BREAK-RATE SERIES.")
print("    'side' is a same-session quantity: consecutive sessions do not")
print("    overlap. Taking every 4th session cannot remove overlap that is not")
print("    there -- it just throws away 75% of the data and shrinks |t| by ~2.")
print("    So the report's 'non-overlap t -0.51 vs -1.16' is not a correction.")
print("=" * 96)
bias = per_session_break_bias(s)
print(f"   whole window d {bias.mean():+.4f}  t {t_of(bias):+.2f}  n {len(bias)}"
      f"   NW t(10) {nw_t(bias):+.2f}   script's non-overlap t {nonoverlap_t(bias):+.2f}")
print(f"   lag-1 autocorrelation of the daily d series: {bias.autocorr(1):+.3f}"
      f"   (lag-5 {bias.autocorr(5):+.3f})  -> essentially no serial dependence,")
print("   which is why NW barely moves it and why thinning is the wrong tool.")

# ================================================================== C3
print("\n" + "=" * 96)
print("C3. 'INDICES BREAK UP MORE OFTEN IN 5 OF 6 YEARS' IS THE SAME DRIFT THE")
print("    REPORT USES TO KILL THE ret_3d GAP. Break side is a proxy for the")
print("    day's own sign; the sign of d tracks the sign of the year's drift.")
print("=" * 96)
nif = s[s["underlying"] == "NIFTY"].sort_values("dt").set_index("dt")["close"]
nret = nif / nif.shift(1) - 1.0
rows = {}
for y in sorted(s["year"].unique()):
    bi = bias[pd.to_datetime(bias.index).year == y]
    r = nret[nret.index.year == y]
    rows[y] = {"sessions": len(bi), "d": bi.mean(), "t": t_of(bi),
               "P(NIFTY up day)%": float((r > 0).mean()*PCT),
               "NIFTY year ret%": float((nif[nif.index.year==y].iloc[-1] /
                                         nif[nif.index.year==y].iloc[0] - 1)*PCT)}
yt = pd.DataFrame(rows).T
print(yt.to_string(float_format=lambda v: f"{v:9.3f}"))
c = yt["d"].corr(yt["P(NIFTY up day)%"])
print(f"   corr(year d, year P(up day)) = {c:+.3f} across 6 years")
sess = pd.DataFrame({"d": bias, "up": (nret.reindex(bias.index) > 0).astype(float)}).dropna()
print(f"   session level: corr(d, day-is-up) = {sess['d'].corr(sess['up']):+.3f}"
      f"   (n {len(sess)})")
print("   d | up day  {:+.3f} ;  d | down day {:+.3f}  -- the break side simply"
      .format(sess[sess.up == 1]["d"].mean(), sess[sess.up == 0]["d"].mean()))
print("   reports the day's direction, so any 'break-rate bias' finding is a")
print("   restatement of how many up days the window happened to contain.")
