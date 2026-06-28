import warnings; warnings.filterwarnings("ignore")
import numpy as np, pandas as pd
from st_analysis import *

# ---- load market profiles (prior-day structure) ----
mp = pd.read_sql(text("""SELECT symbol,time,poc,vah,val,ib_high,ib_low FROM market_profiles
                         WHERE timeframe='auction_daily'"""), ENG, parse_dates=["time"])
mp["sess"]=pd.to_datetime(mp["time"],utc=True).dt.tz_convert("Asia/Kolkata").dt.date
mp=mp.sort_values(["symbol","sess"])
# the profile row dated D describes session D. For trading on D we use D-1 profile (prior-day value area).
# build prior-day map per symbol
mp["pd_poc"]=mp.groupby("symbol")["poc"].shift(1)
mp["pd_vah"]=mp.groupby("symbol")["vah"].shift(1)
mp["pd_val"]=mp.groupby("symbol")["val"].shift(1)
mp["pd_ibh"]=mp.groupby("symbol")["ib_high"].shift(1)
mp["pd_ibl"]=mp.groupby("symbol")["ib_low"].shift(1)
mp["pd_close_proxy"]=mp.groupby("symbol")["poc"].shift(1) # not true close; use spot instead later
MP = {s:g.set_index("sess") for s,g in mp.groupby("symbol")}

ALL=[]
for sym in IDX:
    d1=load_1m(sym)
    if d1.empty: continue
    df=resample_5m(d1)
    df=df.sort_values(["sess","time"]).reset_index(drop=True)
    # bar index within session (0-based). NSE 5-min ~ 75 bars
    df["bi"]=df.groupby("sess").cumcount()
    df["nb"]=df.groupby("sess")["bi"].transform("max")+1
    # forward signed returns
    g=df.groupby("sess")
    for h in (6,12):
        df[f"fwd{h}"]=g["close"].shift(-h)/df["close"]-1
    # ---- intraday derived state ----
    df["day_open"]=g["open"].transform("first")
    df["day_high_sofar"]=g["high"].cummax()  # NOTE within whole frame; fix per session below
    # recompute cumulative within session
    df["hh"]=g["high"].cummax()
    df["ll"]=g["low"].cummin()
    # ATR-ish: rolling 12-bar true range mean (within session via groupby)
    tr=(df["high"]-df["low"])
    df["atr12"]=tr.groupby(df["sess"]).transform(lambda s:s.rolling(12,min_periods=4).mean())
    # opening range: first 6 bars (30 min) high/low
    orh = g.apply(lambda x:x["high"].iloc[:6].max()).rename("orh")
    orl = g.apply(lambda x:x["low"].iloc[:6].min()).rename("orl")
    oro = g["open"].first().rename("oro")
    df=df.merge(orh,on="sess").merge(orl,on="sess").merge(oro,on="sess")
    # prior session close (spot): last close of previous session
    sess_close=g["close"].last()
    sess_close_prev=sess_close.shift(1).rename("prev_close")
    df=df.merge(sess_close_prev,on="sess",how="left")
    # ---- attach prior-day MP structure ----
    if sym in MP:
        m=MP[sym]
        for k in ["pd_poc","pd_vah","pd_val","pd_ibh","pd_ibl"]:
            df[k]=df["sess"].map(m[k])
    else:
        for k in ["pd_poc","pd_vah","pd_val","pd_ibh","pd_ibl"]:
            df[k]=np.nan
    df["sym"]=sym
    ALL.append(df)

D=pd.concat(ALL,ignore_index=True)
D.to_parquet("/tmp/intraday.parquet")
print("rows",len(D),"sessions",D.groupby("sym")["sess"].nunique().to_dict())
print("MP-covered sessions w/ pd_vah:", D.dropna(subset=["pd_vah"]).groupby("sym")["sess"].nunique().to_dict())
