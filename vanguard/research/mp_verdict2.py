"""Symmetric fill correction + does the SIGNAL add anything once fills are honest."""
from __future__ import annotations
import os, sys, numpy as np, pandas as pd, psycopg2
from datetime import date, timedelta
sys.path.insert(0, "/vanguard")
from research.mp_auction import load
FOUR = ["SBIN","AUBANK","FEDERALBNK","ICICIBANK"]; RW, MP = 120, 60
START = date.today() - timedelta(days=int(3*365.25))
con = psycopg2.connect(os.environ["VANGUARD_DATABASE_URL"])
s = load(con, FOUR, START)
bars = pd.read_sql("""SELECT underlying,(time AT TIME ZONE 'Asia/Kolkata') ts,
  date(time AT TIME ZONE 'Asia/Kolkata') dt, open,high,low,close,source
  FROM underlying_spot_candles WHERE interval='30minute' AND time>=%(s)s
  AND underlying=ANY(%(n)s) AND (time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15' AND '15:15'
  ORDER BY underlying,ts""", con, params={"s":START,"n":FOUR}); con.close()
bars["ts"]=pd.to_datetime(bars["ts"]); bars["dt"]=pd.to_datetime(bars["dt"])
for c in ("open","high","low","close"): bars[c]=pd.to_numeric(bars[c])
s = s.sort_values(["underlying","dt"]).reset_index(drop=True)
s["cp_rank"]=(s.groupby("underlying")["close_pos"].transform(
  lambda x: x.rolling(RW,min_periods=MP).apply(lambda w:(w[-1]>w[:-1]).mean(),raw=True)))
u = s.dropna(subset=["cp_rank","next_open_ret"]).copy()

ex, en = {}, {}
for (nm,dt),g in bars.groupby(["underlying","dt"],sort=False):
    g=g.sort_values("ts"); tt=g["ts"].dt.time.astype(str).values
    if tt[0]!="09:15:00": continue
    w1=g[(tt>="09:15:00")&(tt<"09:45:00")]; rest=g[tt>="09:45:00"]
    ex[(nm,dt)]=dict(o=float(w1["open"].iloc[0]),
        typ=float((w1["high"].max()+w1["low"].min()+w1["close"].iloc[-1])/3),
        p0945=float(rest["open"].iloc[0]) if len(rest) else float(w1["close"].iloc[-1]),
        rest_h=float(rest["high"].max()) if len(rest) else float(w1["high"].max()),
        rest_l=float(rest["low"].min()) if len(rest) else float(w1["low"].min()))
    last=g[tt=="15:15:00"]
    if len(last): en[(nm,dt)]=dict(c=float(last["close"].iloc[0]),h=float(last["high"].iloc[0]),l=float(last["low"].iloc[0]))
nxt={}
for nm,g in s.groupby("underlying"):
    d=list(g.sort_values("dt")["dt"])
    for a,b in zip(d,d[1:]): nxt[(nm,a)]=b
rec=[]
for _,r in u.iterrows():
    k=(r["underlying"],r["dt"]); kn=nxt.get(k)
    if kn is None or (r["underlying"],kn) not in ex or k not in en: continue
    E,X=en[k],ex[(r["underlying"],kn)]; c=E["c"]
    rec.append(dict(name=r["underlying"],dt=r["dt"],xdt=kn,cp=r["cp_rank"],entry=c,
        eh=E["h"],o=X["o"],typ=X["typ"],p0945=X["p0945"],rh=X["rest_h"],rl=X["rest_l"]))
A=pd.DataFrame(rec)
A["r_open"]=A["o"]/A["entry"]-1; A["r_typ"]=A["typ"]/A["entry"]-1; A["r_0945"]=A["p0945"]/A["entry"]-1
A["uh"]=A["o"]>A["rh"]; A["ul"]=A["o"]<A["rl"]
A["r_sym"]=np.where(A["uh"]|A["ul"], A["r_typ"], A["r_open"])
A["slip_bp"]=((A["eh"]-A["entry"])/A["entry"]/2)*1e4
T=A[A["cp"]>=2/3].copy()

print("=== IS THE 'UNREVISITED OPEN' CORRECTION SYMMETRIC? (top-tertile trades) ===")
print(f"{'name':<11}{'n':>5}{'open>rest hi':>14}{'open<rest lo':>14}{'up-only bp':>12}{'sym bp':>9}{'open bp':>9}")
for nm in FOUR:
    g=T[T["name"]==nm]
    up=np.where(g["uh"],g["r_typ"],g["r_open"])
    print(f"{nm:<11}{len(g):>5}{g['uh'].sum():>14}{g['ul'].sum():>14}"
          f"{np.mean(up)*1e4:>+12.1f}{g['r_sym'].mean()*1e4:>+9.1f}{g['r_open'].mean()*1e4:>+9.1f}")
g=T; up=np.where(g["uh"],g["r_typ"],g["r_open"])
print(f"{'POOLED':<11}{len(g):>5}{g['uh'].sum():>14}{g['ul'].sum():>14}"
      f"{np.mean(up)*1e4:>+12.1f}{g['r_sym'].mean()*1e4:>+9.1f}{g['r_open'].mean()*1e4:>+9.1f}")

def blk(x, by):
    """night-clustered t via block bootstrap on exit dates"""
    d=pd.DataFrame({"r":x,"d":by}); nights=d["d"].unique(); rng=np.random.default_rng(11)
    m=d["r"].mean(); bs=[]
    for _ in range(4000):
        pick=rng.choice(nights,len(nights),replace=True)
        bs.append(pd.concat([d[d["d"]==p] for p in pick])["r"].mean()) if False else None
    # faster: groupby means weighted by count
    grp=d.groupby("d")["r"].agg(["sum","count"])
    idx=rng.integers(0,len(grp),size=(4000,len(grp)))
    ssum=grp["sum"].values[idx].sum(1); scnt=grp["count"].values[idx].sum(1)
    bs=ssum/scnt
    return m*1e4, np.percentile(bs,2.5)*1e4, np.percentile(bs,97.5)*1e4, (np.array(bs)>0).mean()

print("\n=== FINAL BOOKS UNDER THE SYMMETRIC FILL (r_sym) + night-clustered 95% CI ===")
print(f"{'name':<11}{'n':>5}{'open bp':>9}{'sym bp':>9}{'0945 bp':>9}{'med':>7}{'win':>6}{'CI low':>8}{'CI hi':>8}{'P>0':>7}{'slip':>7}")
for nm in FOUR+["POOLED"]:
    g=T if nm=="POOLED" else T[T["name"]==nm]
    m,lo,hi,p=blk(g["r_sym"].values,g["xdt"].values)
    print(f"{nm:<11}{len(g):>5}{g['r_open'].mean()*1e4:>+9.1f}{m:>+9.1f}{g['r_0945'].mean()*1e4:>+9.1f}"
          f"{g['r_sym'].median()*1e4:>+7.1f}{(g['r_sym']>0).mean()*100:>5.0f}%{lo:>+8.1f}{hi:>+8.1f}{p:>7.2f}"
          f"{g['slip_bp'].mean():>7.1f}")

print("\n=== DOES THE SIGNAL ADD ANYTHING ONCE FILLS ARE HONEST? (tertiles, four names) ===")
A["b"]=np.where(A["cp"]>=2/3,"TOP",np.where(A["cp"]>=1/3,"MID","BOT"))
print(f"{'bucket':<8}{'n':>6}{'open bp':>9}{'sym bp':>9}{'0945 bp':>9}{'med sym':>9}")
for b in ("TOP","MID","BOT"):
    g=A[A["b"]==b]
    print(f"{b:<8}{len(g):>6}{g['r_open'].mean()*1e4:>+9.1f}{g['r_sym'].mean()*1e4:>+9.1f}"
          f"{g['r_0945'].mean()*1e4:>+9.1f}{g['r_sym'].median()*1e4:>+9.1f}")
print(f"{'ALL':<8}{len(A):>6}{A['r_open'].mean()*1e4:>+9.1f}{A['r_sym'].mean()*1e4:>+9.1f}"
      f"{A['r_0945'].mean()*1e4:>+9.1f}{A['r_sym'].median()*1e4:>+9.1f}")
d=T['r_sym'].mean()-A[A['b']!='TOP']['r_sym'].mean()
sd=np.sqrt(T['r_sym'].var(ddof=1)/len(T)+A[A['b']!='TOP']['r_sym'].var(ddof=1)/len(A[A['b']!='TOP']))
print(f"   TOP minus (MID+BOT) under sym fill: {d*1e4:+.1f}bp, t={d/sd:+.2f}")
d=T['r_open'].mean()-A[A['b']!='TOP']['r_open'].mean()
sd=np.sqrt(T['r_open'].var(ddof=1)/len(T)+A[A['b']!='TOP']['r_open'].var(ddof=1)/len(A[A['b']!='TOP']))
print(f"   TOP minus (MID+BOT) at the open  : {d*1e4:+.1f}bp, t={d/sd:+.2f}")
print("\n   per name, TOP vs ALL-NIGHTS under sym fill:")
for nm in FOUR:
    g=A[A["name"]==nm]
    print(f"   {nm:<11} TOP {g[g['b']=='TOP']['r_sym'].mean()*1e4:+7.1f}  "
          f"MID {g[g['b']=='MID']['r_sym'].mean()*1e4:+7.1f}  "
          f"BOT {g[g['b']=='BOT']['r_sym'].mean()*1e4:+7.1f}  "
          f"ALL {g['r_sym'].mean()*1e4:+7.1f}")

print("\n=== FINAL: costs, portfolio, selection under the honest fill ===")
for lab,col in [("open","r_open"),("sym","r_sym"),("0945","r_0945")]:
    print(f" {lab:<5}" + "".join(f"{c:>9}" for c in (0,5,10,20,22,25)))
    for nm in FOUR:
        g=T[T["name"]==nm][col]
        print(f"   {nm:<9}" + "".join(f"{(g-c/1e4).mean()*1e4:>+9.1f}" for c in (0,5,10,20,22,25)))
    pf=T.groupby("xdt")[col].mean()
    print(f"   {'PORTF':<9}" + "".join(f"{(pf-c/1e4).mean()*1e4:>+9.1f}" for c in (0,5,10,20,22,25))
          + f"   nights={len(pf)} tot0={( (1+pf).prod()-1)*100:+.1f}% "
            f"tot22={((1+pf-22/1e4).prod()-1)*100:+.1f}% t={pf.mean()/(pf.std(ddof=1)/np.sqrt(len(pf))):+.2f}")

sb=T[T["name"]=="SBIN"]
for col in ("r_open","r_sym","r_0945"):
    r=sb[col]; t=r.mean()/(r.std(ddof=1)/np.sqrt(len(r)))
    rng=np.random.default_rng(3); sim=rng.standard_t(97,size=(200000,16)).max(1)
    print(f"   SBIN {col:<7} t={t:+.2f}  P(best of 16 null books >= t) = {(sim>=t).mean():.3f}")
print(f"   SBIN sym: median {sb['r_sym'].median()*1e4:+.1f}bp  win {(sb['r_sym']>0).mean()*100:.0f}%  "
      f"top1 {sb['r_sym'].max()*1e4:+.0f}bp = {sb['r_sym'].max()/sb['r_sym'].sum()*100:.0f}% of total  "
      f"drop2best {sb['r_sym'].drop(sb['r_sym'].nlargest(2).index).mean()*1e4:+.1f}bp  "
      f"trim20% {sb['r_sym'].sort_values().iloc[9:-9].mean()*1e4:+.1f}bp")
h=len(sb)//2
print(f"   SBIN sym split-half {sb['r_sym'].iloc[:h].mean()*1e4:+.1f} / {sb['r_sym'].iloc[h:].mean()*1e4:+.1f}bp; "
      f"ex-Feb26 {sb[~((sb['dt'].dt.year==2026)&(sb['dt'].dt.month==2))]['r_sym'].mean()*1e4:+.1f}bp "
      f"(n={len(sb[~((sb['dt'].dt.year==2026)&(sb['dt'].dt.month==2))])})")
