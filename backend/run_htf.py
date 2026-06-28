import warnings; warnings.filterwarnings("ignore")
import numpy as np, pandas as pd
from sqlalchemy import create_engine, text
from scipy.stats import spearmanr
ENG=create_engine("postgresql+psycopg2://nomadcurie:nomadcurie@db:5432/nomadcurie")
IDX=["NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY","SENSEX"]

# Build DAILY OHLC from 30-min deep history (2021->2026) for multi-day/calendar features.
SESS_START=pd.Timedelta(hours=3,minutes=45); SESS_END=pd.Timedelta(hours=10,minutes=15)
def load_daily(sym):
    q=text("""SELECT time,open,high,low,close FROM underlying_spot_candles
              WHERE underlying=:s AND interval='30minute' ORDER BY time""")
    df=pd.read_sql(q,ENG,params={"s":sym},parse_dates=["time"])
    df["time"]=pd.to_datetime(df["time"],utc=True)
    tod=df["time"]-df["time"].dt.normalize()
    df=df[(tod>=SESS_START)&(tod<SESS_END)].copy()
    df["sess"]=df["time"].dt.tz_convert("Asia/Kolkata").dt.date
    for c in ["open","high","low","close"]: df[c]=df[c].astype(float)
    d=df.groupby("sess").agg(open=("open","first"),high=("high","max"),
        low=("low","min"),close=("close","last"),nb=("close","count")).reset_index()
    d=d[d["nb"]>=10].copy()  # full-ish session
    d["sym"]=sym
    return d

frames=[load_daily(s) for s in IDX]
A=pd.concat(frames,ignore_index=True).sort_values(["sym","sess"]).reset_index(drop=True)
A["sess"]=pd.to_datetime(A["sess"])
g=A.groupby("sym")
A["ret"]=g["close"].pct_change()
# forward signed multi-day returns (entry at today close, hold N days)
for n in (1,2,3,5):
    A[f"fwd{n}d"]=g["close"].shift(-n)/A["close"]-1
# HTF features
A["ema20"]=g["close"].transform(lambda s:s.ewm(span=20).mean())
A["ema50"]=g["close"].transform(lambda s:s.ewm(span=50).mean())
A["dist_ema20"]=(A["close"]-A["ema20"])/A["ema20"]
A["trend_align"]=np.sign(A["ema20"]-A["ema50"])
A["ret5"]=g["close"].transform(lambda s:s.pct_change(5))
A["ret20"]=g["close"].transform(lambda s:s.pct_change(20))
A["atr14"]=g.apply(lambda x:(x["high"]-x["low"]).rolling(14).mean()).reset_index(level=0,drop=True)
A["ext_atr"]=(A["close"]-A["ema20"])/A["atr14"]
# calendar
A["dow"]=A["sess"].dt.dayofweek
A["dom"]=A["sess"].dt.day
# weekly index-expiry proxy: NSE indices weekly expiry historically Thu(=3). days-to-Thu
A["dte_thu"]=(3-A["dow"])%7

def ic_block(df,feat,tgt,sgn):
    sub=df[[feat,tgt,"sym"]].dropna()
    sub=sub[np.isfinite(sub[feat])&np.isfinite(sub[tgt])]
    if len(sub)<50: return "n/a"
    pooled=spearmanr(sub[feat],sub[tgt]).correlation
    # per-symbol IC (the "session" analog here is the symbol/year; use symbol)
    ics=[]
    for s,gg in sub.groupby("sym"):
        ics.append(spearmanr(gg[feat],gg[tgt]).correlation)
    ics=np.array(ics)
    frac=(np.sign(ics)==np.sign(sgn)).mean()
    return f"pooled={pooled:+.3f} | per-sym mean={ics.mean():+.3f} ({100*frac:.0f}% exp-sign) | n={len(sub)}d/{len(ics)}sym"

print("="*70,"\nMULTI-DAY HTF (daily from 30-min, 2021-2026)\n","="*70)
print("sessions/sym:",A.dropna(subset=['fwd1d']).groupby('sym')['sess'].nunique().to_dict())
print("\n-- momentum continuation? (expect + if trend persists, - if MR) --")
for tgt in ("fwd1d","fwd2d","fwd5d"):
    print(f"  ret5  ->{tgt}:",ic_block(A,"ret5",tgt,+1))
for tgt in ("fwd1d","fwd2d","fwd5d"):
    print(f"  ret20 ->{tgt}:",ic_block(A,"ret20",tgt,+1))
print("\n-- mean-reversion: distance from EMA20 (expect -) --")
for tgt in ("fwd1d","fwd2d","fwd5d"):
    print(f"  ext_atr ->{tgt}:",ic_block(A,"ext_atr",tgt,-1))
print("\n-- HTF trend alignment as DIRECTIONAL FILTER (expect +) --")
for tgt in ("fwd1d","fwd2d","fwd5d"):
    print(f"  trend_align ->{tgt}:",ic_block(A,"trend_align",tgt,+1))

# ===== CALENDAR: day-of-week & days-to-expiry forward returns =====
print("\n"+"="*70,"\nCALENDAR effects (next-day & 0-day signed returns)\n","="*70)
A["fwd0d"]=A["fwd1d"]  # holding from close to next close
print("\n-- mean fwd1d (bps) by day-of-week (entry at that day's close) --")
dn={0:"Mon",1:"Tue",2:"Wed",3:"Thu",4:"Fri"}
t=A.groupby("dow")["fwd1d"].agg(mean=lambda x:x.mean()*1e4,n="count")
t.index=[dn.get(i,i) for i in t.index]; print(t.round(2).to_string())
print("\n-- intraday same-day open->close mean (bps) by DOW (expiry-day drift?) --")
A["o2c"]=(A["close"]-A["open"])/A["open"]
t2=A.groupby("dow")["o2c"].agg(mean=lambda x:x.mean()*1e4,n="count")
t2.index=[dn.get(i,i) for i in t2.index]; print(t2.round(2).to_string())
print("\n-- days-to-Thu(weekly expiry proxy): same-day o2c mean (bps) --")
t3=A.groupby("dte_thu")["o2c"].agg(mean=lambda x:x.mean()*1e4,n="count")
print(t3.round(2).to_string())
