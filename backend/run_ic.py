import warnings; warnings.filterwarnings("ignore")
import numpy as np, pandas as pd
from st_analysis import per_session_ic, fmt

D=pd.read_parquet("/tmp/intraday.parquet")

# ===== derived structure/temporal features =====
eps=1e-9
atr=D["atr12"].replace(0,np.nan)

# 1. Distance from prior-day POC (naked POC pull): normalized by ATR. Positive when price ABOVE poc.
D["dist_pdpoc"]=(D["close"]-D["pd_poc"])/atr
# mean-reversion hypothesis: above POC -> pull DOWN -> expect NEG corr with fwd. So sign_expect=-1
# 2. Position vs prior-day value area: location relative to VAH/VAL midpoint, ATR-norm
D["va_mid"]=(D["pd_vah"]+D["pd_val"])/2
D["dist_vamid"]=(D["close"]-D["va_mid"])/atr
# 3. Open location vs prior value area (acceptance/rejection) - a session-level signal broadcast to bars
D["open_vs_vah"]=(D["day_open"]-D["pd_vah"])  # >0 = gap above value
D["open_vs_val"]=(D["day_open"]-D["pd_val"])
# accept-above = open above VAH; gap-fill hypothesis says revert toward value -> short bias early
# 4. Opening range breakout state: where is price vs OR after OR forms (bi>=6)
D["or_pos"]=(D["close"]-(D["orh"]+D["orl"])/2)/((D["orh"]-D["orl"]).replace(0,np.nan))
# 5. Overnight gap (spot): open vs prev session close, ATR-norm
D["gap"]=(D["day_open"]-D["prev_close"])/atr
# 6. Extension from day open (intraday momentum proxy), ATR-norm
D["ext_open"]=(D["close"]-D["day_open"])/atr
# 7. Extension from session HH/LL (distance to extremes) - fade-extreme
D["pos_in_range"]=(D["close"]-D["ll"])/((D["hh"]-D["ll"]).replace(0,np.nan))  # 0..1
# 8. Single-bar momentum (legacy baseline) - last bar return
D["mom1"]=D.groupby(["sym","sess"])["close"].pct_change()
# 9. 6-bar momentum
D["mom6"]=D.groupby(["sym","sess"])["close"].transform(lambda s:s.pct_change(6))

# time-of-day buckets
D["tod"]=pd.cut(D["bi"],bins=[-1,12,48,200],labels=["open","mid","close"])

def report(name, feat, sgn, sub=None, tgts=("fwd6","fwd12")):
    dd=D if sub is None else sub
    line=[f"[{name}] expect sign {sgn:+d}"]
    for t in tgts:
        r=per_session_ic(dd,feat,t,sgn)
        line.append(f"   {t}: {fmt(r)}")
    print("\n".join(line)); print()

print("="*70,"\nBASELINE momentum (confirm anti-predictive)\n","="*70)
report("mom1 single-bar","mom1",+1)
report("mom6","mom6",+1)

print("="*70,"\nSTRUCTURE features (mean-reversion / fade => expect NEG)\n","="*70)
report("dist_pdPOC (fade)","dist_pdpoc",-1)
report("dist_VAmid (fade)","dist_vamid",-1)
report("ext_from_open (fade)","ext_open",-1)
report("pos_in_session_range (fade)","pos_in_range",-1)
report("OR position (fade)","or_pos",-1)
report("overnight gap (fade=revert)","gap",-1)
