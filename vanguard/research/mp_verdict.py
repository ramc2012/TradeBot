"""Joint verdict: artefact removal + honest fills + selection cost, on one table."""
from __future__ import annotations
import os, sys, numpy as np, pandas as pd, psycopg2
from datetime import date, timedelta
sys.path.insert(0, "/vanguard")
from research.mp_auction import load                      # noqa
from research.banknifty_rotation import BANKS             # noqa

FOUR = ["SBIN", "AUBANK", "FEDERALBNK", "ICICIBANK"]
RW, MP = 120, 60
START = date.today() - timedelta(days=int(3 * 365.25))

con = psycopg2.connect(os.environ["VANGUARD_DATABASE_URL"])
s16 = load(con, list(BANKS), START)
bars = pd.read_sql("""
SELECT underlying, (time AT TIME ZONE 'Asia/Kolkata') AS ts,
       date(time AT TIME ZONE 'Asia/Kolkata') AS dt,
       open, high, low, close, volume, source
FROM underlying_spot_candles
WHERE interval='30minute' AND time >= %(start)s AND underlying = ANY(%(n)s)
  AND (time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15' AND '15:15'
ORDER BY underlying, ts""", con, params={"start": START, "n": FOUR})
con.close()

s16 = s16.sort_values(["underlying", "dt"]).reset_index(drop=True)
s16["cp_rank"] = (s16.groupby("underlying")["close_pos"].transform(
    lambda x: x.rolling(RW, min_periods=MP).apply(lambda w: (w[-1] > w[:-1]).mean(), raw=True)))
u16 = s16.dropna(subset=["cp_rank", "next_open_ret"])
t16 = u16[u16["cp_rank"] >= 2/3]

# ---------- 1. SELECTION COST: where does SBIN sit among the 16 books? ----------
rows = []
for nm, g in t16.groupby("underlying"):
    r = g["next_open_ret"].dropna()
    if len(r) < 20: continue
    rows.append({"name": nm, "n": len(r), "bp": r.mean()*1e4, "med": r.median()*1e4,
                 "t": r.mean()/(r.std(ddof=1)/np.sqrt(len(r)))})
sel = pd.DataFrame(rows).sort_values("bp", ascending=False)
print("=== 16 BANK BOOKS, SAME RULE, same window (the pool SBIN was picked from) ===")
print(sel.to_string(index=False, float_format=lambda v: f"{v:+.2f}"))
allr = t16["next_open_ret"].dropna()
print(f"   pool: {len(sel)} books, mean of book means {sel['bp'].mean():+.1f}bp, "
      f"median book {sel['bp'].median():+.1f}bp, books>0 {(sel['bp']>0).sum()}/{len(sel)}")
print(f"   SBIN rank {list(sel['name']).index('SBIN')+1}/{len(sel)} by bp, "
      f"t-rank {sel['t'].rank(ascending=False)[sel['name'].eq('SBIN')].iloc[0]:.0f}")
# max-of-N null: how often does the best of N iid t(97) books exceed SBIN's t?
rng = np.random.default_rng(7)
N = len(sel); sb_t = float(sel.loc[sel['name']=='SBIN','t'].iloc[0])
sim = rng.standard_t(97, size=(200000, N)).max(axis=1)
print(f"   SBIN naive t={sb_t:.2f}; P(max of {N} independent null books >= that) = {(sim>=sb_t).mean():.3f}")

# ---------- 2. HONEST FILLS on the four ----------
bars["ts"] = pd.to_datetime(bars["ts"]); bars["dt"] = pd.to_datetime(bars["dt"])
for c in ("open","high","low","close"): bars[c] = pd.to_numeric(bars[c])
ex = {}
for (nm, dt), g in bars.groupby(["underlying","dt"], sort=False):
    g = g.sort_values("ts")
    tt = g["ts"].dt.time.astype(str)
    if tt.iloc[0] != "09:15:00": continue
    w1 = g[(tt.values >= "09:15:00") & (tt.values < "09:45:00")]      # first 30 min, clock
    rest = g[tt.values >= "09:45:00"]
    o = float(w1["open"].iloc[0])
    ex[(nm, dt)] = dict(
        o=o, w1_h=float(w1["high"].max()), w1_l=float(w1["low"].min()),
        w1_c=float(w1["close"].iloc[-1]),
        p0945=float(rest["open"].iloc[0]) if len(rest) else float(w1["close"].iloc[-1]),
        rest_h=float(rest["high"].max()) if len(rest) else float(w1["high"].max()),
        after_open_h=float(g["high"].iloc[1:].max()),
        src=str(g["source"].iloc[0]), nbars=len(g))
en = {}
for (nm, dt), g in bars.groupby(["underlying","dt"], sort=False):
    g = g.sort_values("ts"); tt = g["ts"].dt.time.astype(str)
    if tt.iloc[0] != "09:15:00": continue
    last = g[tt.values == "15:15:00"]
    if not len(last): continue
    en[(nm, dt)] = dict(c=float(last["close"].iloc[0]), h=float(last["high"].iloc[0]),
                        l=float(last["low"].iloc[0]))

tr = t16[t16["underlying"].isin(FOUR)].copy()
# exit session = next session for that name
nxt = {}
for nm, g in s16[s16["underlying"].isin(FOUR)].groupby("underlying"):
    d = list(g.sort_values("dt")["dt"])
    for a, b in zip(d, d[1:]): nxt[(nm, a)] = b

rec = []
for _, r in tr.iterrows():
    k = (r["underlying"], r["dt"]); kn = nxt.get(k)
    if kn is None or (r["underlying"], kn) not in ex or k not in en: continue
    E, X = en[k], ex[(r["underlying"], kn)]
    c = E["c"]
    rec.append(dict(name=r["underlying"], dt=r["dt"], xdt=kn, entry=c,
        entry_mid=(c+E["h"])/2, o=X["o"], w1_c=X["w1_c"], p0945=X["p0945"],
        typ=(X["w1_h"]+X["w1_l"]+X["w1_c"])/3, rest_h=X["rest_h"],
        after_open_h=X["after_open_h"], src=X["src"], nb=X["nbars"],
        helper=r["next_open_ret"]))
T = pd.DataFrame(rec)
T["r_open"] = T["o"]/T["entry"] - 1
print(f"\nreproduction: max|r_open - helper| = {np.abs(T['r_open']-T['helper']).max():.2e}  n={len(T)}")
T["r_w1c"]  = T["w1_c"]/T["entry"] - 1
T["r_0945"] = T["p0945"]/T["entry"] - 1
T["r_typ"]  = T["typ"]/T["entry"] - 1
T["r_mid"]  = ((T["o"]+T["w1_c"])/2)/T["entry"] - 1
# unrevisited opening print: the exit price never traded again that day
T["unrev_day"] = T["o"] > T["after_open_h"]                  # never again after bar 1
T["unrev_rest"] = T["o"] > T["rest_h"]                       # never again after 09:45
# corrected exit: honest print everywhere it matters
T["r_corr"] = np.where(T["unrev_rest"], T["r_typ"], T["r_open"])
T["zero"] = np.isclose(T["r_open"], 0.0, atol=1e-12)

def stat(r):
    r = pd.Series(r).dropna(); n = len(r); eq = (1+r).cumprod()
    sd = r.std(ddof=1); nz = (r != 0)
    pos = int((r > 0).sum()); neg = int((r < 0).sum())
    from math import comb
    k = min(pos, neg); m = pos+neg
    p = min(1.0, 2*sum(comb(m,i) for i in range(k+1))/2**m) if m else 1.0
    top1 = r.nlargest(1).sum()/r.sum() if r.sum() != 0 else np.nan
    top2 = r.nlargest(2).sum()/r.sum() if r.sum() != 0 else np.nan
    d2 = r.drop(r.nlargest(2).index)
    return dict(n=n, bp=r.mean()*1e4, med=r.median()*1e4, win=(r>0).mean()*100,
                tot=(eq.iloc[-1]-1)*100, dd=(eq/eq.cummax()-1).min()*100,
                t=r.mean()/(sd/np.sqrt(n)), sgn=p, top1=top1*100, top2=top2*100,
                d2=d2.mean()*1e4)

print("\n=== UNREVISITED OPENING PRINTS (exit print never traded again that session) ===")
for nm, g in T.groupby("name"):
    u = g[g["unrev_rest"]]
    print(f"  {nm:<11} {len(u)}/{len(g)} trades; they carry "
          f"{u['r_open'].sum()/g['r_open'].sum()*100 if g['r_open'].sum()!=0 else float('nan'):+.0f}% "
          f"of arithmetic total; give-up if worked out over 30min: "
          f"{(u['r_typ']-u['r_open']).sum()*1e4:+.0f}bp")
    for _, x in u.sort_values("r_open", ascending=False).head(4).iterrows():
        print(f"      {str(x['xdt'])[:10]}  open {x['o']:.2f}  rest-of-day high {x['rest_h']:.2f} "
              f" r_open {x['r_open']*100:+.2f}%  r_typ {x['r_typ']*100:+.2f}%")

print("\n=== BOOKS UNDER EACH FILL (entry = 15:15 close) ===")
cols = ["as-reported r_open","zero-gap dropped","unrev repriced (r_corr)",
        "typical-price exit","09:45 exit","entry mid(15:15 c..h) + r_corr"]
hdr = f"{'name':<11}{'fill':<32}{'n':>4}{'bp':>8}{'med':>7}{'win':>6}{'tot%':>8}{'DD%':>7}{'t':>7}{'sign p':>8}{'top2%':>7}"
print(hdr)
for nm in FOUR:
    g = T[T["name"] == nm]
    variants = [("as-reported r_open", g["r_open"]),
                ("zero-gap dropped", g.loc[~g["zero"], "r_open"]),
                ("unrev repriced (r_corr)", g["r_corr"]),
                ("typical-price exit", g["r_typ"]),
                ("09:45 exit", g["r_0945"]),
                ("entry mid + r_corr", (1+g["r_corr"])*g["entry"]/g["entry_mid"] - 1)]
    for lab, r in variants:
        st = stat(r)
        print(f"{nm:<11}{lab:<32}{st['n']:>4}{st['bp']:>+8.1f}{st['med']:>+7.1f}"
              f"{st['win']:>5.0f}%{st['tot']:>+8.1f}{st['dd']:>+7.1f}{st['t']:>+7.2f}"
              f"{st['sgn']:>8.3f}{st['top2']:>7.0f}")
    print()

print("=== POOLED (all 388 name-nights) and NIGHT-EQUAL-WEIGHT PORTFOLIO ===")
for lab, col in [("r_open","r_open"),("r_corr","r_corr"),("r_typ","r_typ"),("r_0945","r_0945")]:
    r = T[col]; st = stat(r)
    pf = T.groupby("xdt")[col].mean()          # one unit of capital split across signals
    sp = stat(pf)
    print(f"  {lab:<9} pooled n={st['n']} {st['bp']:+.1f}bp med {st['med']:+.1f} t {st['t']:+.2f} "
          f"| portfolio nights={sp['n']} {sp['bp']:+.1f}bp med {sp['med']:+.1f} t {sp['t']:+.2f} "
          f"tot {sp['tot']:+.1f}% DD {sp['dd']:+.1f}%")

print("\n=== FYERS-ERA EXIT PRINTS ===")
f = T[T["src"].str.contains("fyers", case=False, na=False)]
print(f"  {len(f)} of {len(T)} trades exit on a fyers-sourced bar; sources seen: {sorted(T['src'].unique())}")
if len(f):
    for nm, g in f.groupby("name"):
        print(f"    {nm:<11} n={len(g)} r_open {g['r_open'].mean()*1e4:+.1f}bp "
              f"r_typ {g['r_typ'].mean()*1e4:+.1f}bp  span {str(g['xdt'].min())[:10]}..{str(g['xdt'].max())[:10]}")
    for nm in FOUR:
        g = T[(T["name"]==nm) & ~T["src"].str.contains("fyers", case=False, na=False)]
        print(f"    ex-fyers {nm:<11} n={len(g)} r_open {g['r_open'].mean()*1e4:+.1f}bp "
              f"r_corr {g['r_corr'].mean()*1e4:+.1f}bp")

print("\n=== COST LADDER on the corrected fill (round-trip bp, delivery/CNC) ===")
print(f"{'name':<11}" + "".join(f"{c:>9}" for c in (0,5,10,20,25)))
for nm in FOUR:
    g = T[T["name"]==nm]["r_corr"]
    print(f"{nm:<11}" + "".join(f"{(g-c/1e4).mean()*1e4:>+9.1f}" for c in (0,5,10,20,25)))
pfc = T.groupby("xdt")["r_corr"].mean()
print(f"{'PORTFOLIO':<11}" + "".join(f"{(pfc-c/1e4).mean()*1e4:>+9.1f}" for c in (0,5,10,20,25)))

print("\n=== SBIN CONCENTRATION AFTER CORRECTION ===")
g = T[T["name"]=="SBIN"]
for lab, col in [("r_open","r_open"),("r_corr","r_corr")]:
    r = g[col].sort_values(ascending=False)
    cum = r.cumsum()/r.sum()
    print(f"  {lab}: top1 {r.iloc[0]*1e4:+.0f}bp ({r.iloc[0]/r.sum()*100:.0f}%), "
          f"top3 {r.head(3).sum()/r.sum()*100:.0f}%, n for half = {(cum<0.5).sum()+1}, "
          f"median {r.median()*1e4:+.1f}bp, sym-trim20% {r.sort_values().iloc[int(.1*len(r)):len(r)-int(.1*len(r))].mean()*1e4:+.1f}bp")
ex_feb = g[~((g["dt"].dt.year==2026)&(g["dt"].dt.month==2))]
print(f"  SBIN ex Feb-2026, corrected: n={len(ex_feb)} {ex_feb['r_corr'].mean()*1e4:+.1f}bp "
      f"med {ex_feb['r_corr'].median()*1e4:+.1f} t {ex_feb['r_corr'].mean()/(ex_feb['r_corr'].std(ddof=1)/np.sqrt(len(ex_feb))):+.2f}")
h = len(g)//2
print(f"  SBIN split-half corrected: 1st {g['r_corr'].iloc[:h].mean()*1e4:+.1f}bp  "
      f"2nd {g['r_corr'].iloc[h:].mean()*1e4:+.1f}bp")
T.to_csv("/vanguard/research/_verdict_trades.csv", index=False)
