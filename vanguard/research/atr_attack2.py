"""Two loose ends from atr_attack.py."""
from __future__ import annotations
import os, sys, warnings
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.atr_refutation import session_ics, t_of
from research.atr_attack import extra_features, nw_t, block_boot_t
from research.mp_profile import FWD_SESSIONS
warnings.filterwarnings("ignore")

s = extra_features(pd.read_pickle("/tmp/atr_attack_sessions.pkl"))
b = s[s["side"] != 0].dropna(subset=["atr20_leak","atr20_lag","atr_prior"]).copy()
b["mfe3d_norm"]      = b[f"mfe_{FWD_SESSIONS}d"] / b["atr20_lag"]        # report's 3c
b["mfe3d_norm_disj"] = b[f"mfe_{FWD_SESSIONS}d"] / b["atr_prior_pc"]     # unbiased
b["mfe_norm_pc"]     = b["mfe_total"] / b["atr_prior_pc"]
b["range3d_norm_disj"]= ((b["high"]-b["low"])/b["close"]) / b["atr_prior_pc"]

print("1. SECTION 3c USED THE SELF-REFERENTIAL DENOMINATOR THE REPORT ITSELF WARNED ABOUT")
print(f"   {'target':<40}{'IC':>8}{'t(iid)':>8}{'t(NW4)':>9}{'p(boot)':>9}{'sess':>7}")
for tgt,lab in ((\
 "mfe3d_norm","mfe_3d / atr20_lag   (report 3c, shared denom)"),
 ("mfe3d_norm_disj","mfe_3d / atr_prior_pc (disjoint denom)")):
    ic = session_ics(b,"atr20_lag",tgt)["ic"].to_numpy()
    tb,pb = block_boot_t(ic)
    print(f"   {lab:<40}{ic.mean():>+8.3f}{t_of(ic):>+8.2f}{nw_t(ic,4):>+9.2f}{pb:>9.3f}{len(ic):>7}")

print("\n2. HALF-BY-HALF t FOR THE CORRECTED HEADLINE (report prints means only)")
for tgt in ("mfe_total","mfe_norm_pc"):
    ics = session_ics(b,"atr20_lag",tgt)
    h = len(ics)//2
    a,c = ics["ic"].iloc[:h].to_numpy(), ics["ic"].iloc[h:].to_numpy()
    print(f"   {tgt:<14} 1st half {a.mean():+.3f} t{t_of(a):+.2f} "
          f"({ics['dt'].iloc[0].date()}..{ics['dt'].iloc[h-1].date()}, n={len(a)})   "
          f"2nd half {c.mean():+.3f} t{t_of(c):+.2f} "
          f"({ics['dt'].iloc[h].date()}..{ics['dt'].iloc[-1].date()}, n={len(c)})")

print("\n3. HOW MANY SESSIONS DOES EACH REPORTED IC ACTUALLY USE?")
print(f"   name-day rows in break sample .......... {len(b):,}")
print(f"   distinct sessions in break sample ...... {b['dt'].nunique()}   <- the report's '421 sessions'")
print(f"   sessions with >=6 names (session_ics) .. {session_ics(b,'atr20_lag','mfe_total')['dt'].nunique()}   <- what every IC is actually built on")
u = session_ics(b,"atr20_lag","mfe_total")
print(f"   effective window ....................... {u['dt'].min().date()} .. {u['dt'].max().date()}"
      f"  ({(u['dt'].max()-u['dt'].min()).days/30.44:.1f} months)")
print(f"   name-days inside those sessions ........ {b[b['dt'].isin(u['dt'])].shape[0]:,} of {len(b):,}")
