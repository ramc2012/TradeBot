import warnings; warnings.filterwarnings("ignore")
import numpy as np, pandas as pd
from st_analysis import per_session_ic, fmt
from scipy.stats import spearmanr

D=pd.read_parquet("/tmp/intraday.parquet")
eps=1e-9
atr=D["atr12"].replace(0,np.nan)
D["ext_open"]=(D["close"]-D["day_open"])/atr
D["pos_in_range"]=(D["close"]-D["ll"])/((D["hh"]-D["ll"]).replace(0,np.nan))
D["or_pos"]=(D["close"]-(D["orh"]+D["orl"])/2)/((D["orh"]-D["orl"]).replace(0,np.nan))
D["dist_pdpoc"]=(D["close"]-D["pd_poc"])/atr

# ---- COMPOSITE FADE SCORE: average rank of the 3 robust intraday fade features ----
for f in ["ext_open","pos_in_range","or_pos"]:
    D[f+"_r"]=D.groupby(["sym","sess"])[f].rank(pct=True)
D["fade_score"]=D[["ext_open_r","pos_in_range_r","or_pos_r"]].mean(axis=1)
# expect NEG corr w fwd (high score=extended up=>pull down)

print("="*70,"\nCOMPOSITE FADE SCORE (mean rank of ext_open+pos_in_range+or_pos)\n","="*70)
for t in ("fwd6","fwd12"):
    print(f"  ALL {t}:",fmt(per_session_ic(D,"fade_score",t,-1)))
# open-window only
sub=D[D["bi"]<=12]
for t in ("fwd6","fwd12"):
    print(f"  OPEN-window(bi<=12) {t}:",fmt(per_session_ic(sub,"fade_score",t,-1)))

# ===== ECONOMIC magnitude: forward move conditional on extreme fade_score =====
print("\n"+"="*70,"\nECONOMIC: mean fwd6 by fade_score decile (open window)\n","="*70)
s=sub.dropna(subset=["fade_score","fwd6"]).copy()
s["dec"]=pd.qcut(s["fade_score"],10,labels=False,duplicates="drop")
tab=s.groupby("dec")["fwd6"].agg(["mean","median","count"])
tab["mean_bps"]=tab["mean"]*1e4
print(tab[["mean_bps","count"]].round(2).to_string())
# long-short: bottom decile (extended down=>buy CE) minus top decile (extended up=>buy PE)
lo=s[s["dec"]==0]["fwd6"].mean(); hi=s[s["dec"]==s["dec"].max()]["fwd6"].mean()
print(f"\n  Bottom-decile fwd6 (BUY CE side): {lo*1e4:+.1f} bps")
print(f"  Top-decile    fwd6 (BUY PE side): {hi*1e4:+.1f} bps")
print(f"  Fade spread (lo - hi): {(lo-hi)*1e4:+.1f} bps over 30 min")

# ===== GAP fill-vs-go: classify gap direction & size, measure first-hour move =====
print("\n"+"="*70,"\nGAP behavior: fill-vs-go (signed first-30min move after gap)\n","="*70)
gg=D.groupby(["sym","sess"]).agg(op=("day_open","first"),pc=("prev_close","first"),
    atrm=("atr12","median")).reset_index()
# first-30min realized: close at bi=6 vs open
c6=D[D["bi"]==6].set_index(["sym","sess"])["close"]
gg=gg.set_index(["sym","sess"]); gg["c6"]=c6; gg=gg.reset_index().dropna()
gg["gap"]=(gg["op"]-gg["pc"])/gg["atrm"]
gg["fwd_30m"]=(gg["c6"]-gg["op"])/gg["op"]
gg2=gg[np.isfinite(gg["gap"])&np.isfinite(gg["fwd_30m"])]
ic=spearmanr(gg2["gap"],gg2["fwd_30m"]).correlation
print(f"  IC(gap, first-30m move) all-sessions pooled = {ic:+.3f}  (n={len(gg2)} sessions)")
gg2["gapbk"]=pd.cut(gg2["gap"],[-99,-1,-0.3,0.3,1,99],labels=["big_dn","sm_dn","flat","sm_up","big_up"])
print(gg2.groupby("gapbk")["fwd_30m"].agg(mean=lambda x:x.mean()*1e4,n="count").round(1).to_string())
print("  (positive mean after up-gap => gap GOES; negative => gap FILLS)")
