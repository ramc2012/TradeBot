"""(E) Option-level results: does selection (RS / move-richness) beat an
unselected universe and a matched random-selection control?

Output: opt_results.txt
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

from opt_selection import (COST_GRID, COSTS, HORIZONS, OUT, cluster_stats,
                           deoverlap_stats)

pd.set_option("display.width", 200)
LINES: list[str] = []


def P(s: str = "") -> None:
    print(s)
    LINES.append(s)


def load() -> pd.DataFrame:
    b = pd.read_parquet(os.path.join(OUT, "opt_sel.parquet"))
    for h in HORIZONS:
        b[f"ret{h}"] = pd.to_numeric(b[f"ret{h}"], errors="coerce")
    return b


def arms(df: pd.DataFrame, side: str) -> dict[str, pd.Series]:
    """Boolean selection masks. CE = long side (high RS); PE = short side (low RS)."""
    hi = side == "CE"
    a = {
        "unselected": pd.Series(True, index=df.index),
        "RS_decile": (df["rs_ret_21_rank"] >= 0.90) if hi else (df["rs_ret_21_rank"] <= 0.10),
        "RS_quintile": (df["rs_ret_21_rank"] >= 0.80) if hi else (df["rs_ret_21_rank"] <= 0.20),
        "alpha_decile": (df["alpha_21_rank"] >= 0.90) if hi else (df["alpha_21_rank"] <= 0.10),
        "moverich_quintile": df["prev_n_moves_rank"] >= 0.80,
        "moverich_decile": df["prev_n_moves_rank"] >= 0.90,
        "rv_quintile": df["rv_21_rank"] >= 0.80,
        "RS_and_moverich": ((df["rs_ret_21_rank"] >= 0.80) if hi else (df["rs_ret_21_rank"] <= 0.20))
        & (df["prev_n_moves_rank"] >= 0.80),
        # PLACEBO: the deliberately WRONG-WAY RS arm (high RS -> PE, low RS -> CE).
        # If RS's apparent lift is DIRECTIONAL, this must be worse than unselected.
        # If it also lifts, the "RS effect" is a bleed/vol selection, not direction.
        "RS_decile_WRONGWAY": (df["rs_ret_21_rank"] <= 0.10) if hi else (df["rs_ret_21_rank"] >= 0.90),
        "rv_bottom_quintile": df["rv_21_rank"] <= 0.20,
    }
    return {k: v.fillna(False) for k, v in a.items()}


def random_control(sub: pd.DataFrame, mask: pd.Series, col: str,
                   n_draws: int = 300, seed: int = 7) -> dict:
    """Match the arm session-by-session on trade COUNT, drawing names at random
    from the same eligible pool that session. Returns the null distribution of
    the mean-of-session-means."""
    rng = np.random.default_rng(seed)
    d = sub[[col, "session"]].copy()
    d["sel"] = mask.reindex(sub.index).fillna(False).values
    d = d.dropna(subset=[col])
    counts = d.groupby("session")["sel"].sum()
    counts = counts[counts > 0]
    if len(counts) < 20:
        return dict(mean=np.nan, p05=np.nan, p95=np.nan, pct=np.nan, nclust=0)
    pools = {s: g[col].values for s, g in d.groupby("session") if s in counts.index}
    obs = d[d["sel"]].groupby("session")[col].mean().mean()
    draws = np.empty(n_draws)
    for i in range(n_draws):
        vals = []
        for s, k in counts.items():
            pool = pools[s]
            k = int(min(k, len(pool)))
            vals.append(rng.choice(pool, size=k, replace=False).mean())
        draws[i] = float(np.mean(vals))
    return dict(mean=float(draws.mean()), sd=float(draws.std(ddof=1)),
                p05=float(np.percentile(draws, 5)),
                p95=float(np.percentile(draws, 95)),
                pct=float((draws < obs).mean()), obs=float(obs),
                nclust=int(len(counts)))


def main() -> None:
    b = load()
    P("=" * 100)
    P("(E) OPTION-LEVEL TRANSLATION — selection value on the actual option panel")
    P("=" * 100)
    P()
    P("Panel: monthly contracts, DTE 8-22, premium >= Rs 1, EOD (15:15 IST) snapshot.")
    P("Bands: deep_ITM  = signed moneyness in [-10%, -3%)   (~0.65-0.8 delta)")
    P("       slight_ITM= signed moneyness in [-3%, -0.75%)")
    P("One contract per (underlying, session, side, band): closest to band centre.")
    P()
    for m in ("index", "stock"):
        s = b[b["market"] == m]
        P(f"{m:6s}: rows {len(s):7d}  underlyings {s['underlying'].nunique():4d}  "
          f"sessions {s['session'].nunique():4d}  "
          f"{s['session'].min().date()} -> {s['session'].max().date()}")
    P()
    P(f"Cost model (round-trip, % of premium): index {COSTS['index']*100:.1f}%  "
      f"stock {COSTS['stock']*100:.1f}%.  Sensitivity grid also reported: "
      + ", ".join(f"{c*100:.0f}%" for c in COST_GRID))
    P("The panel carries NO bid/ask, so spread is ASSUMED, not measured. Index")
    P("near-ATM monthly quoted spread ~0.3-0.8%/side; single-stock options on this")
    P("universe are far wider (the established ~10% round-trip figure). 8% is used")
    P("for stocks as a deliberately still-generous number.")
    P()

    # ---------------------------------------------------------------- 1 baseline
    P("=" * 100)
    P("1. BASELINE — the unselected universe, net of costs")
    P("=" * 100)
    P()
    P(f"{'market':6s} {'band':11s} {'side':4s} {'h':>3s} {'n':>6s} {'sess':>5s} "
      f"{'gross%':>8s} {'net%':>8s} {'med_net%':>9s} {'win%':>6s} {'t_clu':>7s} {'t_deov':>7s}")
    for m in ("index", "stock"):
        c = COSTS[m]
        for band in ("deep_ITM", "slight_ITM"):
            for side in ("CE", "PE"):
                for h in HORIZONS:
                    sub = b[(b["market"] == m) & (b["band"] == band)
                            & (b["option_type"] == side)].copy()
                    sub["net"] = sub[f"ret{h}"] - c
                    st = cluster_stats(sub, "net")
                    if not st.get("nclust"):
                        continue
                    gs = cluster_stats(sub, f"ret{h}")
                    dv = deoverlap_stats(sub, "net", h)
                    P(f"{m:6s} {band:11s} {side:4s} {h:3d} {st['n']:6d} {st['nclust']:5d} "
                      f"{gs['mean']*100:8.2f} {st['mean']*100:8.2f} {st['median']*100:9.2f} "
                      f"{st['win']*100:6.1f} {st['t']:7.2f} {dv['t']:7.2f}")
    P()

    # ---------------------------------------------------------------- 2 selection
    P("=" * 100)
    P("2. SELECTION ARMS vs UNSELECTED vs MATCHED RANDOM CONTROL")
    P("=" * 100)
    P()
    P("'lift' = arm net mean - unselected net mean (same cell). 'rand_pct' = the")
    P("arm's position in the count-matched random-selection null (300 draws);")
    P(">0.95 would be evidence the SELECTOR, not the trade count, did the work.")
    P()
    rows = []
    for m in ("index", "stock"):
        c = COSTS[m]
        for band in ("deep_ITM", "slight_ITM"):
            for side in ("CE", "PE"):
                for h in HORIZONS:
                    sub = b[(b["market"] == m) & (b["band"] == band)
                            & (b["option_type"] == side)].copy()
                    sub["net"] = sub[f"ret{h}"] - c
                    base = cluster_stats(sub, "net")
                    if not base.get("nclust") or base["n"] < 200:
                        continue
                    A = arms(sub, side)
                    for aname, mask in A.items():
                        s2 = sub[mask]
                        st = cluster_stats(s2, "net")
                        if not st.get("nclust") or st["n"] < 100:
                            continue
                        dv = deoverlap_stats(s2, "net", h)
                        rc = (dict(pct=np.nan, mean=np.nan) if aname == "unselected"
                              else random_control(sub, mask, "net"))
                        rows.append(dict(market=m, band=band, side=side, h=h,
                                         arm=aname, n=st["n"], sess=st["nclust"],
                                         net=st["mean"], med=st["median"],
                                         win=st["win"], t=st["t"],
                                         t_deov=dv["t"],
                                         lift=st["mean"] - base["mean"],
                                         rand_mean=rc["mean"], rand_pct=rc["pct"]))
    R = pd.DataFrame(rows)
    R.to_csv(os.path.join(OUT, "arms.csv"), index=False)
    for m in ("index", "stock"):
        for band in ("deep_ITM", "slight_ITM"):
            for side in ("CE", "PE"):
                for h in HORIZONS:
                    q = R[(R.market == m) & (R.band == band) & (R.side == side) & (R.h == h)]
                    if q.empty:
                        continue
                    P(f"--- {m} / {band} / {side} / h={h}")
                    P(f"    {'arm':20s} {'n':>6s} {'sess':>5s} {'net%':>8s} {'med%':>8s} "
                      f"{'win%':>6s} {'t_clu':>7s} {'t_deov':>7s} {'lift_pp':>8s} "
                      f"{'rand%':>7s} {'randpct':>8s}")
                    for _, r in q.iterrows():
                        P(f"    {r['arm']:20s} {r['n']:6.0f} {r['sess']:5.0f} "
                          f"{r['net']*100:8.2f} {r['med']*100:8.2f} {r['win']*100:6.1f} "
                          f"{r['t']:7.2f} {r['t_deov']:7.2f} {r['lift']*100:8.2f} "
                          f"{(r['rand_mean']*100 if pd.notna(r['rand_mean']) else float('nan')):7.2f} "
                          f"{r['rand_pct']:8.3f}")
                    P()

    # ------------------------------------------------- 2c is the LIFT significant?
    P("=" * 100)
    P("2c. IS THE SELECTION LIFT ITSELF SIGNIFICANT? (paired by session)")
    P("=" * 100)
    P()
    P("d_t = mean(arm net on session t) - mean(unselected net on session t).")
    P("Paired removes the common market move entirely, so this is the cleanest")
    P("test of 'does the selector add anything'. t is over sessions (clusters);")
    P("t_deov averages the phase-offset sub-samples spaced h apart.")
    P("RIGHT-WRONG = right-way RS decile minus wrong-way RS decile, the purest")
    P("directional statistic available (both arms are extreme-RS, so anything")
    P("common to 'extreme RS' cancels).")
    P()
    P(f"{'market':6s} {'band':11s} {'side':4s} {'h':>3s} {'arm':22s} {'sess':>5s} "
      f"{'lift_pp':>8s} {'t':>7s} {'t_deov':>7s}")
    lift_rows = []
    for m in ("index", "stock"):
        c = COSTS[m]
        for band in ("deep_ITM", "slight_ITM"):
            for side in ("CE", "PE"):
                for h in (3, 5):
                    sub = b[(b["market"] == m) & (b["band"] == band)
                            & (b["option_type"] == side)].copy()
                    sub["net"] = sub[f"ret{h}"] - c
                    sub = sub.dropna(subset=["net"])
                    if sub["session"].nunique() < 30:
                        continue
                    base = sub.groupby("session")["net"].mean()
                    A = arms(sub, side)
                    extra = {}
                    if "RS_decile" in A and "RS_decile_WRONGWAY" in A:
                        r1 = sub[A["RS_decile"]].groupby("session")["net"].mean()
                        r2 = sub[A["RS_decile_WRONGWAY"]].groupby("session")["net"].mean()
                        extra["RS_RIGHT_minus_WRONG"] = (r1 - r2).dropna()
                    for aname, mask in A.items():
                        if aname == "unselected":
                            continue
                        g = sub[mask].groupby("session")["net"].mean()
                        d = (g - base).dropna()
                        if len(d) < 30:
                            continue
                        extra[aname] = d
                    for aname, d in extra.items():
                        se = d.std(ddof=1) / np.sqrt(len(d))
                        t = d.mean() / se if se > 0 else np.nan
                        ts = []
                        dd = d.sort_index()
                        for off in range(h):
                            s3 = dd.iloc[off::h]
                            if len(s3) < 12:
                                continue
                            se3 = s3.std(ddof=1) / np.sqrt(len(s3))
                            if se3 > 0:
                                ts.append(s3.mean() / se3)
                        td = float(np.mean(ts)) if ts else np.nan
                        lift_rows.append(dict(market=m, band=band, side=side, h=h,
                                              arm=aname, sess=len(d),
                                              lift=d.mean(), t=t, t_deov=td))
                        P(f"{m:6s} {band:11s} {side:4s} {h:3d} {aname:22s} {len(d):5d} "
                          f"{d.mean()*100:8.2f} {t:7.2f} {td:7.2f}")
    L = pd.DataFrame(lift_rows)
    L.to_csv(os.path.join(OUT, "lifts.csv"), index=False)
    P()
    from scipy import stats as sps0
    LL = L.dropna(subset=["t_deov"]).copy()
    LL["p"] = 2 * (1 - sps0.norm.cdf(LL["t_deov"].abs()))
    kk = len(LL)
    LL = LL.sort_values("p")
    LL["rank"] = np.arange(1, kk + 1)
    LL["q"] = (LL["p"] * kk / LL["rank"])[::-1].cummin()[::-1].clip(upper=1)
    P(f"LIFT grid k = {kk}. Bonferroni alpha = {0.05/kk:.5f}")
    P(f"  positive lifts with de-overlapped p<0.05 raw: "
      f"{((LL['p']<0.05)&(LL['lift']>0)).sum()}")
    P(f"  positive lifts surviving BH-FDR 5%          : "
      f"{((LL['q']<0.05)&(LL['lift']>0)).sum()}")
    P(f"  positive lifts surviving Bonferroni         : "
      f"{((LL['p']<0.05/kk)&(LL['lift']>0)).sum()}")
    P()
    P("Top 12 lifts by |t_deov|:")
    P(LL.head(12)[["market", "band", "side", "h", "arm", "sess", "lift",
                   "t_deov", "p", "q"]].to_string(index=False))
    P()

    # ---------------------------------------------------------------- 3 overlap
    P("=" * 100)
    P("3. ARE RS-SELECTED AND MOVE-RICH NAMES THE SAME NAMES?")
    P("=" * 100)
    P()
    u = b[b["market"] == "stock"].drop_duplicates(["underlying", "session"])
    u = u.dropna(subset=["rs_ret_21_rank", "prev_n_moves_rank"])
    P(f"stock name-sessions with both selectors: {len(u)}")
    P(f"cross-sectional Spearman(RS rank, prior-month move-count rank): "
      f"{u['rs_ret_21_rank'].corr(u['prev_n_moves_rank'], method='spearman'):+.4f}")
    P(f"Spearman(|RS| rank, move-count rank): "
      f"{(u['rs_ret_21_rank']-0.5).abs().corr(u['prev_n_moves_rank'], method='spearman'):+.4f}")
    P(f"Spearman(rv_21 rank, move-count rank): "
      f"{u['rv_21_rank'].corr(u['prev_n_moves_rank'], method='spearman'):+.4f}")
    P()
    jac = []
    for s, g in u.groupby("session"):
        a = set(g.loc[g["rs_ret_21_rank"] >= 0.80, "underlying"])
        c2 = set(g.loc[g["prev_n_moves_rank"] >= 0.80, "underlying"])
        if a and c2:
            jac.append(len(a & c2) / len(a | c2))
    P(f"per-session Jaccard(RS top quintile, move-rich top quintile): "
      f"mean {np.mean(jac):.3f}  median {np.median(jac):.3f}  "
      f"(random-overlap expectation for two 20% sets ~= 0.111)")
    P()

    # ------------------------------------------------------- 4 realised excursion
    P("=" * 100)
    P("4. DO THE SELECTORS PICK NAMES WHOSE OPTIONS ACTUALLY MOVE?")
    P("=" * 100)
    P()
    P("Mean |option return| (gross) — dispersion is what a long-premium trade needs.")
    P(f"{'market':6s} {'band':11s} {'h':>3s} {'arm':20s} {'n':>6s} {'mean|ret|%':>11s}")
    for m in ("index", "stock"):
        for band in ("deep_ITM", "slight_ITM"):
            for h in HORIZONS:
                sub = b[(b["market"] == m) & (b["band"] == band)
                        & (b["option_type"] == "CE")].copy()
                sub["absr"] = sub[f"ret{h}"].abs()
                A = arms(sub, "CE")
                for aname, mask in A.items():
                    st = cluster_stats(sub[mask], "absr")
                    if not st.get("nclust") or st["n"] < 100:
                        continue
                    P(f"{m:6s} {band:11s} {h:3d} {aname:20s} {st['n']:6d} "
                      f"{st['mean']*100:11.2f}")
    P()

    # -------------------------------------------------- 4b what do arms select
    P("=" * 100)
    P("4b. WHAT ARE THE ARMS ACTUALLY SELECTING? (contract characteristics)")
    P("=" * 100)
    P()
    P("stock / slight_ITM / CE / h=3 cell. If an arm's lift comes from picking")
    P("lower-IV, lower-ATR names, the 'edge' is a bleed selection, not direction.")
    P(f"{'arm':22s} {'n':>6s} {'iv':>7s} {'atr_pct%':>9s} {'premium':>9s} {'|mny|%':>7s}")
    sub = b[(b["market"] == "stock") & (b["band"] == "slight_ITM")
            & (b["option_type"] == "CE") & b["ret3"].notna()]
    for aname, mask in arms(sub, "CE").items():
        s2 = sub[mask]
        if len(s2) < 100:
            continue
        P(f"{aname:22s} {len(s2):6d} {s2['iv'].mean():7.2f} "
          f"{s2['atr_pct'].mean()*100:9.2f} {s2['close'].mean():9.1f} "
          f"{s2['mny'].abs().mean()*100:7.2f}")
    P()

    # ------------------------------------------------------------ 5 cost grid
    P("=" * 100)
    P("5. COST SENSITIVITY — what would have to be true for anything to clear")
    P("=" * 100)
    P()
    P(f"{'market':6s} {'band':11s} {'side':4s} {'h':>3s} {'arm':20s} "
      + " ".join(f"{'net@'+str(int(c*100))+'%':>9s}" for c in COST_GRID))
    for m in ("index", "stock"):
        for band in ("deep_ITM", "slight_ITM"):
            for side in ("CE", "PE"):
                for h in HORIZONS:
                    sub = b[(b["market"] == m) & (b["band"] == band)
                            & (b["option_type"] == side)].copy()
                    A = arms(sub, side)
                    for aname in ("unselected", "RS_decile", "moverich_quintile"):
                        s2 = sub[A[aname]]
                        st = cluster_stats(s2, f"ret{h}")
                        if not st.get("nclust") or st["n"] < 100:
                            continue
                        cells = " ".join(
                            f"{(st['mean']-c)*100:9.2f}" for c in COST_GRID)
                        P(f"{m:6s} {band:11s} {side:4s} {h:3d} {aname:20s} {cells}")
    P()

    # ------------------------------------------------------------ 6 multiplicity
    P("=" * 100)
    P("6. MULTIPLICITY over the full selection grid")
    P("=" * 100)
    P()
    from scipy import stats as sps
    G = R[R["arm"] != "unselected"].dropna(subset=["t_deov"]).copy()
    G["p_raw"] = 2 * (1 - sps.norm.cdf(G["t_deov"].abs()))
    k = len(G)
    G = G.sort_values("p_raw")
    G["rank"] = np.arange(1, k + 1)
    # BH step-up: cummin taken from the LARGEST p downwards
    G["q_bh"] = (G["p_raw"] * k / G["rank"])[::-1].cummin()[::-1].clip(upper=1)
    P(f"grid size k = {k} selection cells (arm x market x band x side x horizon)")
    P(f"Bonferroni alpha = {0.05/k:.5f}")
    P(f"cells with de-overlapped p < 0.05 raw : {(G['p_raw']<0.05).sum()}")
    P(f"cells surviving BH-FDR 5%             : {(G['q_bh']<0.05).sum()}")
    P(f"cells surviving Bonferroni            : {(G['p_raw']<0.05/k).sum()}")
    P()
    P("Ten most extreme cells by de-overlapped p (note: NET returns, so a")
    P("significantly NEGATIVE cell is a confirmed loser, not a finding):")
    P(G.head(10)[["market", "band", "side", "h", "arm", "n", "net", "t_deov",
                  "p_raw", "q_bh"]].to_string(index=False))
    P()

    with open(os.path.join(os.path.dirname(OUT), "opt_results.txt"), "w") as f:
        f.write("\n".join(LINES))


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
