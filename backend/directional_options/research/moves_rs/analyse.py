"""(D) STUDY A -- monthly move statistics.

Answers, per stock per calendar month, over all available history:
  1. how many significant moves exist (full distribution, incl. zeros),
  2. their magnitude and duration distributions,
  3. the share of the month's high-low range delivered by the largest move,
  4. the fraction of sessions spent in a move vs consolidating,
  5. PERSISTENCE: is a movey stock movey next month? (the verdict that
     decides whether historical move-richness can select instruments at all),
  6. the universe-wide opportunity count per month.

Statistical hygiene:
  * every leg is ONE observation; legs are non-overlapping by construction,
    so there is no overlapping-window t-inflation inside a stock. Across
    stocks in the same month there IS common market-regime dependence, so the
    persistence test is run PER MONTH-PAIR (cross-sectional Spearman) and the
    t-test is taken over the ~15 pair-level rhos, not over stock-months. The
    naive pooled figure is printed alongside so the inflation is visible.
  * Bonferroni + Benjamini-Hochberg over the full persistence grid.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd
from scipy import stats

from moves import SIG_LEVELS, segment_all

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

MIN_SESSIONS_IN_MONTH = 15      # a month counts only if the stock traded >=15 sessions
MIN_MONTHS = 12                 # a stock is INCLUDED only with >=12 such months
INDEXES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX",
           "NIFTYNXT50", "NIFTY50", "NIFTYIT"}
COMMODITIES = {"CRUDEOIL", "NATURALGAS", "GOLD", "SILVER", "COPPER", "ZINC",
               "ALUMINIUM", "LEAD", "NICKEL", "GOLDM", "SILVERM", "CRUDEOILM",
               "NATURALGASM", "SILVERMIC", "GOLDGUINEA", "MENTHAOIL", "COTTON",
               "CASTORSEED", "KAPAS"}

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 60)
pd.set_option("display.max_rows", 400)


def q(x, ps=(0, 10, 25, 50, 75, 90, 95, 99, 100)):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    if not len(x):
        return {}
    return {f"p{p}": float(np.percentile(x, p)) for p in ps}


def fmt(d: dict) -> str:
    return "  ".join(f"{k}={v:,.2f}" for k, v in d.items())


# ---------------------------------------------------------------- load
daily = pd.read_parquet(os.path.join(DATA, "daily.parquet"))
daily["session"] = pd.to_datetime(daily["session"])
daily["month"] = daily["session"].dt.to_period("M")

is_stock = ~daily["underlying"].isin(INDEXES | COMMODITIES)
stocks = daily[is_stock].copy()

# ------------------------------------------------- coverage / exclusions
sm = (stocks.groupby(["underlying", "month"])
      .agg(n_sessions=("close", "size"),
           hi=("high", "max"), lo=("low", "min"),
           first_close=("close", "first"), last_close=("close", "last"))
      .reset_index())
sm_ok = sm[sm["n_sessions"] >= MIN_SESSIONS_IN_MONTH].copy()
cov = sm_ok.groupby("underlying").agg(months=("month", "nunique")).reset_index()
included = set(cov[cov["months"] >= MIN_MONTHS]["underlying"])
excluded = sorted(set(stocks["underlying"]) - included)

print("=" * 100)
print("COVERAGE")
print("=" * 100)
print(f"rows                 : {len(daily):,} daily bars, {daily['underlying'].nunique()} underlyings")
print(f"span                 : {daily['session'].min().date()} -> {daily['session'].max().date()}")
print(f"non-stock removed    : {sorted(set(daily['underlying']) - set(stocks['underlying']))}")
print(f"stock names          : {stocks['underlying'].nunique()}")
print(f"INCLUDED (>= {MIN_MONTHS} months of >= {MIN_SESSIONS_IN_MONTH} sessions): {len(included)}")
print(f"EXCLUDED             : {len(excluded)} -> {excluded}")
print()
print("months-of-history per INCLUDED stock:",
      fmt(q(cov[cov['underlying'].isin(included)]['months'], (0, 25, 50, 75, 100))))
per_name_sessions = stocks[stocks["underlying"].isin(included)].groupby("underlying").size()
print("daily sessions per INCLUDED stock  :", fmt(q(per_name_sessions, (0, 25, 50, 75, 100))))

st = stocks[stocks["underlying"].isin(included)].copy()

# ------------------------------------------------------------ segment
legs = segment_all(st[["underlying", "session", "open", "high", "low", "close"]])
legs["month"] = legs["start_date"].dt.to_period("M")
legs["end_month"] = legs["end_date"].dt.to_period("M")
print()
print("=" * 100)
print("SEGMENTATION (ATR-zigzag, noise filter = 1.0 x ATR14, fixed)")
print("=" * 100)
print(f"confirmed legs       : {len(legs):,} across {legs['underlying'].nunique()} names")
print(f"legs per name        : {fmt(q(legs.groupby('underlying').size(), (0,25,50,75,100)))}")
print(f"all-leg atr_mult     : {fmt(q(legs['atr_mult']))}")
print(f"all-leg duration     : {fmt(q(legs['duration']))}")
print(f"confirm lag (sessions after the extreme, i.e. un-capturable tail): "
      f"{fmt(q(legs['lag'], (25,50,75,90,100)))}")
for k in SIG_LEVELS:
    s = legs[f"sig_{k:g}"]
    print(f"  K={k:g}: {s.sum():,} significant legs ({100*s.mean():.1f}% of legs), "
          f"up {int((s & (legs['direction']>0)).sum()):,} / down {int((s & (legs['direction']<0)).sum()):,}")

# ------------------------------------- monthly panel of move statistics
months = sorted(sm_ok[sm_ok["underlying"].isin(included)]["month"].unique())
grid = sm_ok[sm_ok["underlying"].isin(included)][
    ["underlying", "month", "n_sessions", "hi", "lo"]].copy()
grid["hl_range"] = grid["hi"] - grid["lo"]

# realised vol control
st = st.sort_values(["underlying", "session"])
st["ret1"] = st.groupby("underlying")["close"].pct_change()
vol = (st.groupby(["underlying", "month"])["ret1"].std().reset_index()
       .rename(columns={"ret1": "rvol"}))
grid = grid.merge(vol, on=["underlying", "month"], how="left")

# session index per (name, month) so leg spans can be clipped to the month
st["si"] = st.groupby("underlying").cumcount()
month_bounds = st.groupby(["underlying", "month"])["si"].agg(["min", "max"]).reset_index()
month_bounds.columns = ["underlying", "month", "si_lo", "si_hi"]
close_by = {(r.underlying, r.si): r.close for r in st.itertuples()}

panel = {}
for k in SIG_LEVELS:
    sub = legs[legs[f"sig_{k:g}"]].copy()
    # (a) moves STARTING in the month
    cnt = sub.groupby(["underlying", "month"]).agg(
        n_moves=("ret", "size"),
        mean_absret=("ret", lambda s: float(np.abs(s).mean())),
        max_absret=("ret", lambda s: float(np.abs(s).max())),
        sum_absret=("ret", lambda s: float(np.abs(s).sum())),
        mean_atrmult=("atr_mult", "mean"),
        mean_dur=("duration", "mean"),
    ).reset_index()
    g = grid.merge(cnt, on=["underlying", "month"], how="left")
    for c in ("n_moves", "sum_absret"):
        g[c] = g[c].fillna(0.0)
    g["n_moves"] = g["n_moves"].astype(int)

    # (b) sessions spent inside a significant leg, clipped to the month
    rows = []
    mb = month_bounds.set_index(["underlying", "month"])
    for r in sub.itertuples():
        for mth in pd.period_range(r.month, r.end_month, freq="M"):
            key = (r.underlying, mth)
            if key not in mb.index:
                continue
            slo, shi = mb.loc[key, "si_lo"], mb.loc[key, "si_hi"]
            a, b = max(r.start_i, slo), min(r.end_i, shi)
            if b <= a:
                continue
            pa, pb = close_by.get((r.underlying, a)), close_by.get((r.underlying, b))
            rows.append((r.underlying, mth, b - a, abs(float(pb) - float(pa))))
    occ = pd.DataFrame(rows, columns=["underlying", "month", "sess_in_move", "price_span"])
    if len(occ):
        occ_ag = occ.groupby(["underlying", "month"]).agg(
            sess_in_move=("sess_in_move", "sum"),
            largest_span=("price_span", "max")).reset_index()
        g = g.merge(occ_ag, on=["underlying", "month"], how="left")
    else:
        g["sess_in_move"], g["largest_span"] = 0.0, np.nan
    g["sess_in_move"] = g["sess_in_move"].fillna(0.0)
    g["frac_in_move"] = (g["sess_in_move"] / g["n_sessions"]).clip(0, 1)
    g["share_of_range"] = (g["largest_span"] / g["hl_range"]).clip(0, 1)
    panel[k] = g

# -------------------------------------------------------------- report
print()
print("=" * 100)
print("A1. MOVES PER STOCK PER MONTH -- FULL DISTRIBUTION")
print("=" * 100)
for k in SIG_LEVELS:
    g = panel[k]
    vc = g["n_moves"].value_counts(normalize=True).sort_index()
    print(f"\nK={k:g} ATR   (n = {len(g):,} stock-months, {g['underlying'].nunique()} names, "
          f"{g['month'].nunique()} months)")
    print("  mean = %.3f   median = %.0f   sd = %.3f" %
          (g["n_moves"].mean(), g["n_moves"].median(), g["n_moves"].std()))
    print("  P(n_moves = x): " + "  ".join(
        f"{int(i)}:{100*v:.1f}%" for i, v in vc.items() if i <= 8))
    print("  percentiles   : " + fmt(q(g["n_moves"], (5, 25, 50, 75, 90, 95, 99, 100))))
    print("  P(zero moves in the month) = %.1f%%" % (100 * (g["n_moves"] == 0).mean()))

print()
print("=" * 100)
print("A2. MAGNITUDE + DURATION OF SIGNIFICANT MOVES")
print("=" * 100)
for k in SIG_LEVELS:
    sub = legs[legs[f"sig_{k:g}"]]
    print(f"\nK={k:g} ATR   n={len(sub):,} moves")
    print("  |ret| %%      : " + fmt(q(100 * sub["ret"].abs())))
    print("  atr_mult    : " + fmt(q(sub["atr_mult"])))
    print("  duration    : " + fmt(q(sub["duration"])))
    print("  confirm lag : " + fmt(q(sub["lag"], (25, 50, 75, 90, 100))))
    up, dn = sub[sub["direction"] > 0], sub[sub["direction"] < 0]
    print("  UP   n=%-6d median |ret|=%.2f%%  median dur=%.0f" %
          (len(up), 100 * up["ret"].abs().median(), up["duration"].median()))
    print("  DOWN n=%-6d median |ret|=%.2f%%  median dur=%.0f" %
          (len(dn), 100 * dn["ret"].abs().median(), dn["duration"].median()))
    # capturable fraction: the 1-ATR retrace needed to confirm the end is lost
    capt = (sub["atr_mult"] - 1.0) / sub["atr_mult"]
    print("  fraction of the move left AFTER the 1-ATR confirmation retrace: "
          "median=%.2f  mean=%.2f" % (capt.median(), capt.mean()))

print()
print("=" * 100)
print("A3. SHARE OF THE MONTH'S HIGH-LOW RANGE DELIVERED BY THE LARGEST MOVE")
print("=" * 100)
for k in SIG_LEVELS:
    g = panel[k]
    s = g.loc[g["n_moves"] > 0, "share_of_range"]
    print(f"K={k:g}: months with >=1 move n={len(s):,}   " + fmt(q(s, (10, 25, 50, 75, 90))))

print()
print("=" * 100)
print("A4. TIME IN MOVE vs CONSOLIDATION")
print("=" * 100)
for k in SIG_LEVELS:
    g = panel[k]
    print(f"K={k:g}: mean frac of sessions inside a significant move = %.3f "
          "(=> consolidation %.3f);  " % (g["frac_in_move"].mean(), 1 - g["frac_in_move"].mean())
          + fmt(q(g["frac_in_move"], (10, 25, 50, 75, 90))))

print()
print("=" * 100)
print("A5. OPPORTUNITY COUNT ACROSS THE UNIVERSE")
print("=" * 100)
for k in SIG_LEVELS:
    g = panel[k]
    per_month = g.groupby("month")["n_moves"].sum()
    names_per_month = g.groupby("month")["underlying"].nunique()
    print(f"\nK={k:g}: moves starting per calendar month across the universe")
    print("  mean = %.0f   median = %.0f   min = %d   max = %d   (over %d names/month)"
          % (per_month.mean(), per_month.median(), per_month.min(), per_month.max(),
             names_per_month.median()))
    print("  per-month series (moves / names covered): "
          + ", ".join(f"{m}:{v}/{names_per_month[m]}" for m, v in per_month.items()))
    full = per_month[names_per_month >= 150]
    print("  FULL-UNIVERSE months only (>=150 names): mean=%.0f  median=%.0f  min=%d  max=%d  n=%d"
          % (full.mean(), full.median(), full.min(), full.max(), len(full)))
    print("  => %.2f moves per name per month; %.2f per trading session universe-wide"
          % (full.mean() / names_per_month[full.index].median(), full.mean() / 20.0))

# ------------------------------------------------------- A6 persistence
print()
print("=" * 100)
print("A6. PERSISTENCE -- is a movey stock movey NEXT month?")
print("=" * 100)


def persistence(g: pd.DataFrame, col: str, label: str) -> dict:
    """Cross-sectional Spearman of col(m) vs col(m+1), one rho per month-pair."""
    w = g.pivot_table(index="month", columns="underlying", values=col)
    ms = sorted(w.index)
    rhos, ns = [], []
    for a, b in zip(ms[:-1], ms[1:]):
        if (b - a).n != 1:
            continue
        x, y = w.loc[a], w.loc[b]
        m = x.notna() & y.notna()
        if m.sum() < 30:
            continue
        r = stats.spearmanr(x[m], y[m]).statistic
        if np.isfinite(r):
            rhos.append(r)
            ns.append(int(m.sum()))
    rhos = np.array(rhos)
    t = stats.ttest_1samp(rhos, 0.0)
    # naive pooled (deliberately shown to expose the inflation)
    lag = g.sort_values(["underlying", "month"]).copy()
    lag["mi"] = lag["month"].apply(lambda p: p.year * 12 + p.month)
    lag["nxt"] = lag.groupby("underlying")[col].shift(-1)
    lag["gap"] = lag.groupby("underlying")["mi"].shift(-1) - lag["mi"]
    lag = lag[(lag["gap"] == 1) & lag["nxt"].notna()]
    pooled = stats.spearmanr(lag[col], lag["nxt"])
    return dict(label=label, n_pairs=len(rhos), mean_rho=float(rhos.mean()),
                sd_rho=float(rhos.std(ddof=1)), t=float(t.statistic),
                p=float(t.pvalue), min_rho=float(rhos.min()), max_rho=float(rhos.max()),
                frac_pos=float((rhos > 0).mean()), median_n=int(np.median(ns)),
                pooled_rho=float(pooled.statistic), pooled_p=float(pooled.pvalue))


tests = []
for k in SIG_LEVELS:
    g = panel[k]
    tests.append(persistence(g, "n_moves", f"K={k:g} move COUNT"))
    tests.append(persistence(g, "sum_absret", f"K={k:g} move MAGNITUDE (sum|ret|)"))
ctrl = persistence(panel[SIG_LEVELS[0]], "rvol", "CONTROL: realised vol")

res = pd.DataFrame(tests)
m = len(res)
res["p_bonf"] = np.minimum(1.0, res["p"] * m)
order = res["p"].rank(method="first")
res["p_bh"] = np.minimum(1.0, res["p"] * m / order)
res["p_bh"] = res["p_bh"][::-1].cummin()[::-1]

print("\nPRIMARY (cross-sectional Spearman per month-pair; t-test over the pair-level rhos)")
print(res[["label", "n_pairs", "median_n", "mean_rho", "sd_rho", "t", "p",
           "p_bonf", "p_bh", "frac_pos", "min_rho", "max_rho"]].to_string(index=False,
      float_format=lambda v: f"{v:,.4f}"))
print("\nNAIVE POOLED (all stock-months in one Spearman -- inflated, shown for contrast)")
print(res[["label", "pooled_rho", "pooled_p"]].to_string(index=False,
      float_format=lambda v: f"{v:,.4f}"))
print("\nCONTROL -- the same test on realised volatility, the quantity that is KNOWN to persist:")
print("  mean_rho=%.4f  t=%.2f  p=%.2e  frac_pos=%.2f  (n_pairs=%d)"
      % (ctrl["mean_rho"], ctrl["t"], ctrl["p"], ctrl["frac_pos"], ctrl["n_pairs"]))

# is move-richness just volatility?
g0 = panel[SIG_LEVELS[0]]
cs = []
for mth, gg in g0.groupby("month"):
    gg = gg.dropna(subset=["rvol"])
    if len(gg) < 30:
        continue
    cs.append(stats.spearmanr(gg["n_moves"], gg["rvol"]).statistic)
print("\nCONTEMPORANEOUS rank corr (move count vs realised vol), per month: mean=%.4f  "
      "(if ~0, ATR-normalisation did its job and 'movey' != 'volatile')" % np.mean(cs))

# decile spread: what does last month's top decile actually buy you?
print("\nDECILE READ-ACROSS -- rank stocks by this month's move count, look at next month:")
for k in SIG_LEVELS:
    g = panel[k].sort_values(["underlying", "month"]).copy()
    g["mi"] = g["month"].apply(lambda p: p.year * 12 + p.month)
    g["nxt"] = g.groupby("underlying")["n_moves"].shift(-1)
    g["gap"] = g.groupby("underlying")["mi"].shift(-1) - g["mi"]
    g = g[(g["gap"] == 1) & g["nxt"].notna()].copy()
    g["dec"] = g.groupby("month")["n_moves"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop"))
    g = g.dropna(subset=["dec"])
    tab = g.groupby("dec")["nxt"].agg(["mean", "size"])
    print(f"  K={k:g}  next-month mean move count by current-month quintile: "
          + "  ".join(f"Q{int(i)+1}={r['mean']:.2f}" for i, r in tab.iterrows())
          + f"   (n/quintile ~{int(tab['size'].median())})")

# ------------------------------------------- robustness: fixed-% definition
print()
print("=" * 100)
print("A7. ROBUSTNESS -- fixed-% significance bar instead of ATR-normalised")
print("=" * 100)
for pct in (0.05, 0.08, 0.12):
    sub = legs[legs["ret"].abs() >= pct]
    cnt = sub.groupby(["underlying", "month"]).size().rename("n_moves").reset_index()
    g = grid.merge(cnt, on=["underlying", "month"], how="left")
    g["n_moves"] = g["n_moves"].fillna(0).astype(int)
    pr = persistence(g, "n_moves", f">={100*pct:.0f}%")
    print("  >=%.0f%%: %.2f moves/stock/month  P(0)=%.0f%%  median|ret|=%.1f%%  "
          "persistence mean_rho=%.3f t=%.2f p=%.2e"
          % (100 * pct, g["n_moves"].mean(), 100 * (g["n_moves"] == 0).mean(),
             100 * sub["ret"].abs().median(), pr["mean_rho"], pr["t"], pr["p"]))

print()
print("=" * 100)
print("A8. WHY IS ATR-NORMALISED PERSISTENCE NEGATIVE? -- the mechanical check")
print("=" * 100)
atr_m = (st.assign(tr=st.groupby("underlying")["close"].transform(
            lambda s: s.diff().abs()))
         .groupby(["underlying", "month"])["tr"].mean().reset_index()
         .rename(columns={"tr": "adr"}))
chk = panel[SIG_LEVELS[1]].merge(atr_m, on=["underlying", "month"], how="left")
chk = chk.sort_values(["underlying", "month"])
chk["mi"] = chk["month"].apply(lambda p: p.year * 12 + p.month)
chk["adr_next"] = chk.groupby("underlying")["adr"].shift(-1)
chk["gap"] = chk.groupby("underlying")["mi"].shift(-1) - chk["mi"]
chk = chk[(chk["gap"] == 1) & chk["adr_next"].notna()]
chk["adr_growth"] = chk["adr_next"] / chk["adr"]
rs = [stats.spearmanr(gg["n_moves"], gg["adr_growth"]).statistic
      for _, gg in chk.groupby("month") if len(gg) >= 30]
print("  rank corr(this month's move count, NEXT month's ADR / this month's ADR) = %.3f"
      % np.mean(rs))
print("  => a movey month INFLATES the stock's own ATR, raising next month's K x ATR bar.")
print("     That feedback is mechanical, and it is why the ATR-normalised persistence is")
print("     negative. The fixed-%% definition (A7) has no such feedback and shows ~ZERO")
print("     persistence. Neither formulation gives a usable positive signal.")

print("\n  ERA SPLIT of the K=3 count persistence (first vs second half of month-pairs):")
w = panel[SIG_LEVELS[1]].pivot_table(index="month", columns="underlying", values="n_moves")
ms = sorted(w.index)
rh = []
for a, b in zip(ms[:-1], ms[1:]):
    if (b - a).n != 1:
        continue
    x, y = w.loc[a], w.loc[b]
    mm = x.notna() & y.notna()
    if mm.sum() >= 30:
        rh.append((str(b), stats.spearmanr(x[mm], y[mm]).statistic))
h = len(rh) // 2
print("    first half  mean_rho = %.3f  (%s)" % (np.mean([r for _, r in rh[:h]]), rh[0][0]))
print("    second half mean_rho = %.3f  (%s)" % (np.mean([r for _, r in rh[h:]]), rh[-1][0]))
print("    per-pair: " + "  ".join(f"{m}:{r:+.2f}" for m, r in rh))

print("\n  CROSS-SECTIONAL DISPERSION of per-stock mean move count (K=3), full sample:")
pm = panel[SIG_LEVELS[1]].groupby("underlying")["n_moves"].mean()
print("    " + fmt(q(pm, (0, 5, 25, 50, 75, 95, 100))))
print("    top 5 : " + ", ".join(f"{n}={v:.2f}" for n, v in pm.nlargest(5).items()))
print("    bot 5 : " + ", ".join(f"{n}={v:.2f}" for n, v in pm.nsmallest(5).items()))
print("    sd of per-stock means = %.3f vs sd of a single stock-month = %.3f"
      % (pm.std(), panel[SIG_LEVELS[1]]['n_moves'].std()))

for k in SIG_LEVELS:
    panel[k].to_parquet(os.path.join(DATA, f"panel_K{k:g}.parquet"), index=False)
legs.to_parquet(os.path.join(DATA, "legs.parquet"), index=False)
print("\nwrote data/legs.parquet + data/panel_K*.parquet")
