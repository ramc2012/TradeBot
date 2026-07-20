"""(B) OUR PANEL — re-derive the 2-3 day facts.

Sections:
  1. holdability by moneyness x DTE (2 and 3 session horizon), weekly vs monthly
  2. spot-ATR barrier expressed in premium terms + hit frequency within 2-3d
  3. BANKNIFTY oi_build fwd3 recheck + analogues
  4. round-trip cost vs expected move
  5. data limits / survivorship diagnostics
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

pd.set_option("display.width", 200)
pd.set_option("display.max_rows", 300)

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

opt = pd.read_parquet(os.path.join(DATA, "panel_opt.parquet"))
spot = pd.read_parquet(os.path.join(DATA, "daily_spot.parquet"))
opt["session"] = pd.to_datetime(opt["session"])
opt["q"] = opt["session"].dt.to_period("Q")

INDEXES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}
opt["is_index"] = opt["underlying"].isin(INDEXES)

out = []
def P(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    out.append(s)


# ---------------------------------------------------------------- 1 holdability
P("=" * 100)
P("SECTION 1 — HOLDABILITY at 2 and 3 SESSIONS (median premium change, long premium)")
P("=" * 100)

liq = opt[(opt["close"] >= 1.0)].copy()

for label, sub in [("ALL underlyings", liq), ("INDEX only", liq[liq["is_index"]]),
                   ("STOCK only", liq[~liq["is_index"]])]:
    for tag, flag in [("MONTHLY", True), ("WEEKLY/non-monthly", False)]:
        d = sub[sub["is_monthly"] == flag]
        if len(d) < 500:
            continue
        t = d.groupby(["mny_b", "dte_b"]).agg(
            n=("ret2", "size"),
            med_ret2=("ret2", "median"),
            med_ret3=("ret3", "median"),
            mean_ret3=("ret3", "mean"),
            p25_ret3=("ret3", lambda x: x.quantile(0.25)),
            p75_ret3=("ret3", lambda x: x.quantile(0.75)),
            frac_up3=("ret3", lambda x: (x > 0).mean()),
        )
        t = t[t["n"] >= 200]
        P("")
        P(f"--- {label} / {tag} ---")
        P((t * 1).round(4).to_string())

P("")
P("--- HEADLINE CELL CHECK: prior claim was slight-ITM DTE 8-22 MONTHLY ~ 0% over 5 days ---")
for h, col in [(2, "ret2"), (3, "ret3")]:
    for mb in ["2_slight_ITM(-3..-0.75%)", "3_ATM(+-0.75%)"]:
        for db in ["B_3-7", "C_8-22", "D_23+"]:
            for flag, nm in [(True, "monthly"), (False, "weekly")]:
                d = liq[(liq["mny_b"] == mb) & (liq["dte_b"] == db) & (liq["is_monthly"] == flag)][col].dropna()
                if len(d) < 200:
                    continue
                P(f"h={h}d {mb:26s} {db:6s} {nm:8s} n={len(d):7d} median={d.median():+.4f} mean={d.mean():+.4f} up%={(d>0).mean():.3f}")

# theta-only decomposition: median return conditioned on |spot move| small
P("")
P("--- PURE CARRY (|3d spot move| < 0.25 x ATR) : what you pay to just hold ---")
c = liq[liq["s_ret3"].abs() < 0.25 * liq["atr_pct"]]
t = c.groupby(["mny_b", "dte_b", "is_monthly"]).agg(n=("ret3", "size"), med=("ret3", "median")).query("n>=150")
P(t.round(4).to_string())

P("")
P("--- PER-QUARTER stability of the 3-session carry (monthly, DTE 8-22, flat spot) ---")
cq = c[(c["dte_b"] == "C_8-22") & (c["is_monthly"])]
P(cq.pivot_table(index="q", columns="mny_b", values="ret3", aggfunc="median",
                 observed=False).mul(100).round(1).to_string())
P("n per cell:")
P(cq.pivot_table(index="q", columns="mny_b", values="ret3", aggfunc="size",
                 observed=False).to_string())


# ------------------------------------------------------- 2 ATR barrier -> premium
P("")
P("=" * 100)
P("SECTION 2 — SPOT-ATR BARRIER IN PREMIUM TERMS")
P("=" * 100)

# Direct conditional response: median premium %chg by SIZE of the directional
# spot move measured in ATR units. This avoids the ratio-of-medians artifact.
e = liq[liq["ret3"].notna() & liq["s_ret3"].notna() & liq["atr_pct"].notna()].copy()
e["sgn"] = np.where(e["option_type"] == "CE", 1.0, -1.0)
e["dir_atr"] = (e["s_ret3"] * e["sgn"]) / e["atr_pct"]
bins = [-99, -2, -1.5, -1, -0.5, 0.5, 1, 1.5, 2, 99]
labs = ["<-2", "-2..-1.5", "-1.5..-1", "-1..-0.5", "-0.5..0.5", "0.5..1", "1..1.5", "1.5..2", ">2"]
e["atr_bin"] = pd.cut(e["dir_atr"], bins=bins, labels=labs)
P("Median 3-session PREMIUM %chg by directional SPOT move in ATR units")
P("(rows = moneyness x DTE bucket, monthly contracts, side-aligned)")
for db in ["B_3-7", "C_8-22", "D_23+"]:
    sub = e[(e["dte_b"] == db) & (e["is_monthly"])]
    if len(sub) < 500:
        continue
    piv = sub.pivot_table(index="mny_b", columns="atr_bin", values="ret3",
                          aggfunc="median", observed=False)
    cnt = sub.pivot_table(index="mny_b", columns="atr_bin", values="ret3",
                          aggfunc="size", observed=False)
    P("")
    P(f"  DTE {db}  (median ret3)")
    P((piv * 100).round(1).to_string())
    P(f"  DTE {db}  (n)")
    P(cnt.to_string())

t = (e[e["dir_atr"].abs() > 0.5]
     .assign(omega=lambda x: x["ret3"] / (x["s_ret3"] * x["sgn"]))
     .query("abs(omega) < 60")
     .groupby(["mny_b", "dte_b"])
     .agg(n=("omega", "size"), med_omega=("omega", "median"))
     .query("n>=200"))
P("")
P("Realised 3-session elasticity omega = (premium %chg)/(directional spot %chg),")
P("restricted to moves > 0.5 ATR so the ratio is not dominated by noise:")
P(t.round(3).to_string())

P("")
P("ATR context: median ATR14 as %% of spot, by underlying class")
sp = spot.dropna(subset=["atr_pct"]).copy()
sp["is_index"] = sp["underlying"].isin(INDEXES)
P(sp.groupby("is_index")["atr_pct"].describe(percentiles=[0.25, 0.5, 0.75]).round(4).to_string())

# barrier hit frequency on SPOT within 3 sessions
P("")
P("--- SPOT barrier hit frequency within the next 3 sessions (from EOD close) ---")
b = spot.dropna(subset=["atr_pct", "s_maxhigh_3", "s_minlow_3"]).copy()
b = b[b["n_fwd_sessions"] == 3]
b["is_index"] = b["underlying"].isin(INDEXES)
for nm, bb in [("INDEX", b[b["is_index"]]), ("STOCK", b[~b["is_index"]])]:
    for k_t, k_s in [(1.0, 1.0), (1.5, 1.0), (2.0, 1.0), (1.5, 1.5)]:
        up = (bb["s_maxhigh_3"] >= bb["s_close"] * (1 + k_t * bb["atr_pct"]))
        dn = (bb["s_minlow_3"] <= bb["s_close"] * (1 - k_s * bb["atr_pct"]))
        P(f"{nm} target={k_t}xATR stop={k_s}xATR n={len(bb):6d} P(touch target)={up.mean():.3f} "
          f"P(touch stop)={dn.mean():.3f} P(BOTH touched, order unknown)={(up & dn).mean():.3f} "
          f"P(neither)={(~up & ~dn).mean():.3f}")

P("")
P("Implied PREMIUM barrier: k x ATR% x omega  (using median omega per bucket)")
med_atr_idx = sp[sp["is_index"]]["atr_pct"].median()
med_atr_stk = sp[~sp["is_index"]]["atr_pct"].median()
for mb, db in [("2_slight_ITM(-3..-0.75%)", "C_8-22"), ("3_ATM(+-0.75%)", "C_8-22"),
               ("3_ATM(+-0.75%)", "B_3-7"), ("4_slight_OTM(0.75..3%)", "C_8-22")]:
    r = t.loc[(mb, db)] if (mb, db) in t.index else None
    if r is None:
        continue
    for nm, atr in [("index", med_atr_idx), ("stock", med_atr_stk)]:
        for k in (1.0, 1.5):
            P(f"{mb:26s} {db:6s} {nm:5s} ATR%={atr:.4f} omega={r['med_omega']:.2f} -> {k}xATR = "
              f"{k*atr*r['med_omega']*100:+.1f}% premium move")

# how often does the PREMIUM itself move by the implied barrier within 3 sessions?
P("")
P("--- Realised |3-session premium move| distribution (long premium, holdable bucket) ---")
h = liq[(liq["mny_b"].isin(["2_slight_ITM(-3..-0.75%)", "3_ATM(+-0.75%)"])) &
        (liq["dte_b"] == "C_8-22") & liq["ret3"].notna()]
P(h.groupby("mny_b")["ret3"].describe(percentiles=[0.05, 0.25, 0.5, 0.75, 0.95]).round(3).to_string())


# ------------------------------------------------- 3 BANKNIFTY oi_build recheck
P("")
P("=" * 100)
P("SECTION 3 — oi_build_bias fwd3 RECHECK")
P("=" * 100)
pos = pd.read_csv(os.path.join(DATA, "positioning.csv"))
pos["d"] = pd.to_datetime(pos["d"])
pos = pos.sort_values(["underlying", "d"])
g = pos.groupby("underlying")
for h in (2, 3, 5):
    pos[f"fwd{h}"] = g["spot"].shift(-h) / pos["spot"] - 1.0
pos["side"] = np.sign(pos["oi_build_bias"])
for u, d in pos.groupby("underlying"):
    d = d.dropna(subset=["fwd3", "oi_build_bias"])
    if len(d) < 30:
        continue
    dd = d[d["side"] != 0]
    dr3 = dd["fwd3"] * dd["side"]
    dr5 = (dd["fwd5"] * dd["side"]).dropna()
    ic = d["oi_build_bias"].corr(d["fwd3"], method="spearman")
    # t-stat on directional return
    tstat = dr3.mean() / (dr3.std() / np.sqrt(len(dr3))) if len(dr3) > 2 else np.nan
    P(f"{u:11s} n={len(dd):4d} spearman_IC(bias,fwd3)={ic:+.3f}  dir_fwd3 mean={dr3.mean()*100:+.3f}% "
      f"hit={ (dr3>0).mean():.3f} t={tstat:+.2f}  dir_fwd5 mean={dr5.mean()*100 if len(dr5) else float('nan'):+.3f}%")

P("")
P("--- with the d_atm_iv >= 0 gate ---")
for u, d in pos.groupby("underlying"):
    d = d.dropna(subset=["fwd3", "oi_build_bias", "d_atm_iv"])
    d = d[(d["d_atm_iv"] >= 0) & (np.sign(d["oi_build_bias"]) != 0)]
    if len(d) < 25:
        continue
    dr = d["fwd3"] * np.sign(d["oi_build_bias"])
    t_ = dr.mean() / (dr.std() / np.sqrt(len(dr)))
    P(f"{u:11s} n={len(dr):4d} dir_fwd3 mean={dr.mean()*100:+.3f}% hit={(dr>0).mean():.3f} t={t_:+.2f}")


# ------------------------------------------------------------- 4 cost
P("")
P("=" * 100)
P("SECTION 4 — ROUND-TRIP COST vs EXPECTED MOVE")
P("=" * 100)


def charges(premium_turnover_buy: float, premium_turnover_sell: float) -> float:
    """Indian F&O (index/stock options) statutory + typical discount brokerage,
    on premium turnover. Returns total cost in rupees."""
    brokerage = 20.0 * 2                       # flat per executed order, both legs
    stt = 0.001 * premium_turnover_sell        # 0.1% on sell-side premium
    exch = 0.00035 * (premium_turnover_buy + premium_turnover_sell)  # NSE ~0.035%
    sebi = 0.000001 * (premium_turnover_buy + premium_turnover_sell)
    stamp = 0.00003 * premium_turnover_buy
    gst = 0.18 * (brokerage + exch + sebi)
    return brokerage + stt + exch + sebi + stamp + gst


P("Statutory+brokerage cost as %% of premium notional, by ticket size:")
for prem in (5_000, 20_000, 50_000, 200_000):
    c = charges(prem, prem)
    P(f"  premium notional Rs{prem:>8,d} -> Rs{c:8.1f} round trip = {c/prem*100:.3f}% of premium")

P("")
P("NOTE: the panel has NO bid/ask, so spread must be assumed. Typical NSE option")
P("quoted spread (index near-ATM monthly) ~0.3-0.8%% of premium per side; liquid")
P("stock options 1-3%% per side; illiquid strikes far worse.")
for sp_bps in (0.003, 0.008, 0.02):
    P(f"  assumed half-spread {sp_bps*100:.1f}% per side -> {sp_bps*2*100:.1f}% round trip on premium")

P("")
P("--- Cost as a fraction of the realised 3-session premium move ---")
for mb in ["2_slight_ITM(-3..-0.75%)", "3_ATM(+-0.75%)", "4_slight_OTM(0.75..3%)"]:
    for db in ["B_3-7", "C_8-22"]:
        d = liq[(liq["mny_b"] == mb) & (liq["dte_b"] == db) & liq["ret3"].notna()]
        if len(d) < 200:
            continue
        mad = d["ret3"].abs().median()
        P(f"{mb:26s} {db:6s} n={len(d):7d} median |3d premium move| = {mad*100:5.1f}%  "
          f"| cost@0.6%RT = {0.6/(mad*100)*100:5.1f}% of it | cost@1.6%RT = {1.6/(mad*100)*100:5.1f}% "
          f"| cost@4%RT = {4.0/(mad*100)*100:5.1f}%")


# ----------------------------------------------------------- 5 data limits
P("")
P("=" * 100)
P("SECTION 5 — DATA LIMITS / SURVIVORSHIP")
P("=" * 100)
P(f"panel rows {len(opt):,}  contracts {opt['contract'].nunique():,}  underlyings {opt['underlying'].nunique()}")
P(f"sessions {opt['session'].nunique()}  span {opt['session'].min().date()} .. {opt['session'].max().date()}")
P("")
P("rows per quarter:")
P(opt.groupby("q").agg(n=("close", "size"), u=("underlying", "nunique"),
                       have_ret3=("ret3", lambda x: x.notna().mean())).round(3).to_string())
P("")
P("SURVIVORSHIP: does the 18% of rows WITHOUT a fwd3 quote differ?")
a = opt[opt["ret3"].notna()]["ret1"].dropna()
b_ = opt[opt["ret3"].isna()]["ret1"].dropna()
P(f"  ret1 median | has fwd3 = {a.median():+.4f} (n={len(a):,})   no fwd3 = {b_.median():+.4f} (n={len(b_):,})")
P(f"  |mny| median | has fwd3 = {opt[opt['ret3'].notna()]['mny'].abs().median():.4f}   "
  f"no fwd3 = {opt[opt['ret3'].isna()]['mny'].abs().median():.4f}")
P("")
P("moneyness coverage (share of rows) — the ATM-tracker collection bias:")
P(opt["mny_b"].value_counts(normalize=True).round(3).to_string())
P("")
P("DTE coverage:")
P(opt.groupby(["dte_b", "is_monthly"]).size().to_string())


# ------------------------------------------- 6 required directional accuracy
P("")
P("=" * 100)
P("SECTION 6 — WHAT DIRECTIONAL ACCURACY IS REQUIRED TO BREAK EVEN AT 3 SESSIONS")
P("=" * 100)
P("For a signal that calls the sign of the 3-session spot move with probability p,")
P("E[ret] = p*E[ret3 | right] + (1-p)*E[ret3 | wrong]. Solve for p at 0 and at cost.")
P("Means are winsorised at the 1st/99th percentile so single tail trades cannot")
P("carry the estimate (that is exactly what killed the previous candidates).")
z = liq[liq["ret3"].notna() & liq["s_ret3"].notna() & liq["is_monthly"]].copy()
z["sgn"] = np.where(z["option_type"] == "CE", 1.0, -1.0)
z["dir"] = z["s_ret3"] * z["sgn"]
rows = []
for (mb, db), g in z.groupby(["mny_b", "dte_b"]):
    g = g[g["dir"].abs() > 1e-9]
    if len(g) < 1000:
        continue
    lo, hi = g["ret3"].quantile([0.01, 0.99])
    r = g["ret3"].clip(lo, hi)
    w = r[g["dir"] > 0]
    l = r[g["dir"] < 0]
    if len(w) < 200 or len(l) < 200:
        continue
    ew, el = w.mean(), l.mean()
    p0 = (0 - el) / (ew - el) if ew > el else np.nan
    p16 = (0.016 - el) / (ew - el) if ew > el else np.nan
    p40 = (0.040 - el) / (ew - el) if ew > el else np.nan
    rows.append(dict(mny=mb, dte=db, n=len(g), E_win=ew, E_loss=el,
                     p_breakeven=p0, p_at_1_6pct_cost=p16, p_at_4pct_cost=p40))
P(pd.DataFrame(rows).set_index(["mny", "dte"]).round(4).to_string())
P("")
P("(Baseline: P(3-session spot move is up) in this panel = "
  f"{(z.drop_duplicates(['underlying','session'])['s_ret3'] > 0).mean():.3f})")

with open(os.path.join(HERE, "results.txt"), "w") as fh:
    fh.write("\n".join(out))
print("\nwrote", os.path.join(HERE, "results.txt"))
