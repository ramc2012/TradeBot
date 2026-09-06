import pandas as pd, numpy as np
T=pd.read_csv("/vanguard/research/_verdict_trades.csv",parse_dates=["dt","xdt"])
# rebuild sym fill here (r_typ where open outside rest-of-day range, both directions)
import psycopg2, os
con=psycopg2.connect(os.environ["VANGUARD_DATABASE_URL"])
b=pd.read_sql("""SELECT underlying,(time AT TIME ZONE 'Asia/Kolkata') ts,
 date(time AT TIME ZONE 'Asia/Kolkata') dt,open,high,low,close FROM underlying_spot_candles
 WHERE interval='30minute' AND underlying=ANY(%(n)s)
 AND (time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15' AND '15:15' ORDER BY underlying,ts""",
 con,params={"n":["SBIN","AUBANK","FEDERALBNK","ICICIBANK"]});con.close()
b["ts"]=pd.to_datetime(b["ts"]);b["dt"]=pd.to_datetime(b["dt"])
for c in ("open","high","low","close"): b[c]=pd.to_numeric(b[c])
rl={}
for (nm,dt),g in b.groupby(["underlying","dt"],sort=False):
    g=g.sort_values("ts");tt=g["ts"].dt.time.astype(str).values
    if tt[0]!="09:15:00":continue
    rest=g[tt>="09:45:00"]
    rl[(nm,dt)]=float(rest["low"].min()) if len(rest) else float(g["low"].iloc[0])
T["rl"]=[rl.get((r["name"],r["xdt"]),np.nan) for _,r in T.iterrows()]
T["sym"]=np.where((T["o"]>T["rest_h"])|(T["o"]<T["rl"]),T["r_typ"],T["r_open"])
print(f"{'name':<11}{'n':>4}{'sym bp':>8}{'med':>7}{'win':>6}{'top1%':>7}{'top2%':>7}{'#half':>7}{'drop2':>8}{'tot%':>8}{'DD%':>7}")
for nm in ["SBIN","AUBANK","FEDERALBNK","ICICIBANK"]:
    r=T[T["name"]==nm]["sym"];eq=(1+r).cumprod();c=r.sort_values(ascending=False).cumsum()/r.sum()
    print(f"{nm:<11}{len(r):>4}{r.mean()*1e4:>+8.1f}{r.median()*1e4:>+7.1f}{(r>0).mean()*100:>5.0f}%"
          f"{r.max()/r.sum()*100:>+7.0f}{r.nlargest(2).sum()/r.sum()*100:>+7.0f}"
          f"{(c<0.5).sum()+1:>7}{r.drop(r.nlargest(2).index).mean()*1e4:>+8.1f}"
          f"{(eq.iloc[-1]-1)*100:>+8.1f}{(eq/eq.cummax()-1).min()*100:>+7.1f}")
a=T[(T["name"]=="AUBANK")]
print("AUBANK 2025-08-08 under sym:", f"{a[a['xdt']=='2025-08-08']['sym'].iloc[0]*100:+.2f}%",
      "| AUBANK sym ex that night:", f"{a[a['xdt']!='2025-08-08']['sym'].mean()*1e4:+.1f}bp (n={len(a)-1})")
s=T[T["name"]=="SBIN"]
print("SBIN sym ex 2026-02-03:", f"{s[s['xdt']!='2026-02-03']['sym'].mean()*1e4:+.1f}bp (n={len(s)-1})",
      "| ex top-3 nights:", f"{s['sym'].drop(s['sym'].nlargest(3).index).mean()*1e4:+.1f}bp")
