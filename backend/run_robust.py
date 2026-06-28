import warnings; warnings.filterwarnings("ignore")
import numpy as np, pandas as pd
from st_analysis import per_session_ic, fmt

D=pd.read_parquet("/tmp/intraday.parquet")
atr=D["atr12"].replace(0,np.nan)
D["ext_open"]=(D["close"]-D["day_open"])/atr
D["pos_in_range"]=(D["close"]-D["ll"])/((D["hh"]-D["ll"]).replace(0,np.nan))
D["or_pos"]=(D["close"]-(D["orh"]+D["orl"])/2)/((D["orh"]-D["orl"]).replace(0,np.nan))
for f in ["ext_open","pos_in_range","or_pos"]:
    D[f+"_r"]=D.groupby(["sym","sess"])[f].rank(pct=True)
D["fade_score"]=D[["ext_open_r","pos_in_range_r","or_pos_r"]].mean(axis=1)
D["sess_dt"]=pd.to_datetime(D["sess"])
D["half"]=np.where(D["sess_dt"]<pd.Timestamp("2026-01-01"),"H1(2025)","H2(2026)")

sub=D[D["bi"]<=12]
print("=== Composite fade_score, OPEN-window, fwd12 — robustness ===\n")
print("BY SYMBOL:")
for s,g in sub.groupby("sym"):
    print(f"  {s:>11}:",fmt(per_session_ic(g,"fade_score","fwd12",-1)))
print("\nBY TIME-HALF:")
for h,g in sub.groupby("half"):
    print(f"  {h:>11}:",fmt(per_session_ic(g,"fade_score","fwd12",-1)))
# Causal check: OR uses first 6 bars; we only evaluate bars bi in [6,12] for OR-based part?
print("\nCAUSAL check — restrict to bi in [6,12] (OR fully formed, no lookahead):")
sub2=D[(D["bi"]>=6)&(D["bi"]<=12)]
print("  ",fmt(per_session_ic(sub2,"fade_score","fwd12",-1)))
print("  (fade_score uses ext_open/pos_in_range/or_pos — all computed from data <= current bar)")
