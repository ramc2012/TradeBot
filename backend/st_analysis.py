"""
structure_temporal entry-edge research for Directional Options (long-premium CE/PE).
Targets: SIGNED forward underlying return. IC = Spearman rank-corr.
Report pooled + per-session + conditional IC.
"""
import warnings; warnings.filterwarnings("ignore")
import numpy as np, pandas as pd
from scipy.stats import spearmanr
from sqlalchemy import create_engine, text

ENG = create_engine("postgresql+psycopg2://nomadcurie:nomadcurie@db:5432/nomadcurie")
IDX = ["NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY","SENSEX"]

# NSE session in UTC: 03:45 (09:15 IST) .. 10:00 (15:30 IST)
SESS_START = pd.Timedelta(hours=3, minutes=45)
SESS_END   = pd.Timedelta(hours=10, minutes=0)

def load_1m(sym):
    q = text("""SELECT time, open, high, low, close FROM underlying_spot_candles
                WHERE underlying=:s AND interval='1minute' ORDER BY time""")
    df = pd.read_sql(q, ENG, params={"s":sym}, parse_dates=["time"])
    if df.empty: return df
    df["time"] = pd.to_datetime(df["time"], utc=True)
    # keep NSE session only
    tod = df["time"] - df["time"].dt.normalize()
    df = df[(tod>=SESS_START)&(tod<SESS_END)].copy()
    df["sess"] = df["time"].dt.tz_convert("Asia/Kolkata").dt.date
    # require a real session
    cnt = df.groupby("sess")["close"].transform("count")
    df = df[cnt>=300].copy()
    for c in ["open","high","low","close"]: df[c]=df[c].astype(float)
    return df

def resample_5m(df1):
    out=[]
    for s,g in df1.groupby("sess"):
        g=g.set_index("time")
        r=g.resample("5min", label="left", closed="left").agg(
            {"open":"first","high":"max","low":"min","close":"last"}).dropna()
        r["sess"]=s
        out.append(r.reset_index())
    return pd.concat(out, ignore_index=True)

def per_session_ic(df, feat, tgt, sign_expect=1):
    """returns pooled IC, mean per-session IC, frac sessions matching expected sign, n_bars, n_sess"""
    sub = df[[feat,tgt,"sess"]].dropna()
    sub = sub[np.isfinite(sub[feat])&np.isfinite(sub[tgt])]
    if len(sub)<30: return None
    pooled = spearmanr(sub[feat],sub[tgt]).correlation
    ics=[]
    for s,g in sub.groupby("sess"):
        if len(g)<8 or g[feat].nunique()<3: continue
        ic=spearmanr(g[feat],g[tgt]).correlation
        if np.isfinite(ic): ics.append(ic)
    if not ics: return None
    ics=np.array(ics)
    mean_ps=ics.mean()
    # frac of sessions with sign == expected
    frac_sign = (np.sign(ics)==np.sign(sign_expect)).mean()
    return dict(pooled=pooled, mean_ps=mean_ps, frac_sign=frac_sign,
                n_bars=len(sub), n_sess=len(ics), t=mean_ps/(ics.std(ddof=1)/np.sqrt(len(ics)) + 1e-9))

def fmt(r):
    if r is None: return "n/a"
    return (f"pooled={r['pooled']:+.3f} | per-sess mean={r['mean_ps']:+.3f} "
            f"(t={r['t']:+.1f}, {100*r['frac_sign']:.0f}% expected-sign) | "
            f"n={r['n_bars']}bars/{r['n_sess']}sess")
