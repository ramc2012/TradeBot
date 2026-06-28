import warnings; warnings.filterwarnings("ignore")
import numpy as np, pandas as pd
from st_analysis import per_session_ic, fmt

D=pd.read_parquet("/tmp/intraday.parquet")
eps=1e-9
atr=D["atr12"].replace(0,np.nan)
D["dist_pdpoc"]=(D["close"]-D["pd_poc"])/atr
D["ext_open"]=(D["close"]-D["day_open"])/atr
D["pos_in_range"]=(D["close"]-D["ll"])/((D["hh"]-D["ll"]).replace(0,np.nan))
D["or_pos"]=(D["close"]-(D["orh"]+D["orl"])/2)/((D["orh"]-D["orl"]).replace(0,np.nan))
D["mom6"]=D.groupby(["sym","sess"])["close"].transform(lambda s:s.pct_change(6))
D["tod"]=pd.cut(D["bi"],bins=[-1,12,30,200],labels=["open(0-12)","mid(12-30)","late(30+)"])

# ===== regime: trend-day vs balance-day classification (per session) =====
# Use day range vs ATR & close location: trend day = closes near extreme & wide range
g=D.groupby(["sym","sess"])
day=g.agg(dh=("high","max"),dl=("low","min"),op=("open","first"),cl=("close","last"),
         atrm=("atr12","median")).reset_index()
day["rng_atr"]=(day["dh"]-day["dl"])/day["atrm"]
day["close_loc"]=(day["cl"]-day["dl"])/((day["dh"]-day["dl"])+eps)   # 1=close at high
day["trendiness"]=np.abs(day["close_loc"]-0.5)*2                      # 0 balance .. 1 trend
# classify by terciles of trendiness (computed cross-sectionally; this is hindsight for labeling
# the *day*, used only to SPLIT and observe whether fade IC differs — a descriptive conditional test)
day["regime"]=pd.qcut(day["trendiness"],3,labels=["balance","mixed","trend"])
D=D.merge(day[["sym","sess","regime","trendiness","rng_atr"]],on=["sym","sess"],how="left")

def cond_report(name,feat,sgn,col):
    print(f"--- {name}  split by {col} (expect {sgn:+d}) ---")
    for lvl in [c for c in D[col].cat.categories if D[col].dtype.name=='category'] if hasattr(D[col],'cat') else sorted(D[col].dropna().unique()):
        sub=D[D[col]==lvl]
        for t in ("fwd6","fwd12"):
            r=per_session_ic(sub,feat,t,sgn)
            print(f"   {lvl:>12} {t}: {fmt(r)}")
    print()

print("="*70,"\nCONDITIONAL: fade features by TIME-OF-DAY\n","="*70)
cond_report("or_pos","or_pos",-1,"tod")
cond_report("ext_open","ext_open",-1,"tod")
cond_report("pos_in_range","pos_in_range",-1,"tod")

print("="*70,"\nCONDITIONAL: fade features by REGIME (trend vs balance day)\n","="*70)
cond_report("or_pos","or_pos",-1,"regime")
cond_report("ext_open","ext_open",-1,"regime")

print("="*70,"\nCONDITIONAL: does MOMENTUM ever go POSITIVE? (by regime)\n","="*70)
cond_report("mom6 (expect +)","mom6",+1,"regime")
print("="*70,"\nCONDITIONAL: momentum by time-of-day\n","="*70)
cond_report("mom6 (expect +)","mom6",+1,"tod")
