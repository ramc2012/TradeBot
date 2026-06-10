import asyncio, asyncpg, pandas as pd
from datetime import time as dtime
from nomad_sniper.data.bars import close_stamp
from nomad_sniper.utils.normalize import atr_reference
DSN="postgresql://nomadcurie:nomadcurie@db:5432/nomadcurie"
async def go():
    c=await asyncpg.connect(DSN)
    rows=await c.fetch("select time,open,high,low,close,volume from underlying_spot_candles where underlying='NIFTY' and interval='1minute' and time >= now() - interval '75 days' order by time")
    df=pd.DataFrame(rows,columns=["time","open","high","low","close","volume"])
    df["time"]=pd.to_datetime(df["time"],utc=True).dt.tz_convert("Asia/Kolkata")
    df=df.set_index("time").sort_index()
    for col in ("open","high","low","close","volume"): df[col]=pd.to_numeric(df[col],errors="coerce")
    mins=df.index.hour*60+df.index.minute; df=df[(mins>=9*60+15)&(mins<=15*60+30)]
    print("rows raw RTH:",len(df),"dups:",int(df.index.duplicated().sum()))
    print("close min/max:",float(df.close.min()),float(df.close.max()))
    nod=df[~df.index.duplicated(keep="last")]
    nof=df[~df.index.duplicated(keep="first")]
    d=df.index[-1].date()
    print("atr keep=all:",round(atr_reference(close_stamp(df),d),1))
    print("atr keep=last:",round(atr_reference(close_stamp(nod),d),1))
    print("atr keep=first:",round(atr_reference(close_stamp(nof),d),1))
    # today close range
    td=nod[nod.index.date==d]; print("today close min/max:",float(td.close.min()),float(td.close.max()),"n=",len(td))
    # option resolution
    orows=await c.fetch("select time,interval,expiry,strike,option_type,close from option_premium_candles where underlying='NIFTY' and time::date=$1 order by time",d)
    od=pd.DataFrame(orows,columns=["time","interval","expiry","strike","option_type","close"])
    print("OPT rows today:",len(od))
    if len(od):
        od["strike"]=od.strike.astype(float)
        print("  expiries:",sorted(set(od.expiry)))
        print("  per (interval,expiry,type) counts:")
        print(od.groupby(["interval","expiry","option_type"]).size().to_string())
    await c.close()
asyncio.run(go())
