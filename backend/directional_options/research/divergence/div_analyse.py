"""(D) SPOT-LEVEL RESULTS: setup vs controls, element ablations, crossover
strength conditioning, per-quarter, ex-top-3, PNB concentration, multiplicity.

Statistics reused verbatim from ../cascade/run_cascade.py:
  cluster_boot_diff  cluster bootstrap by UNDERLYING (2000 draws)
  bh                 Benjamini-Hochberg over every comparison made

Every comparison made anywhere in this file is registered in one list, so the
Bonferroni and BH columns are computed over the WHOLE grid, not per-table.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "cascade"))
sys.path.insert(0, os.path.join(HERE, "..", "setups_2d3d"))

import div_defs as D  # noqa: E402
import run_cascade as rc  # noqa: E402

TESTS: list[dict] = []
METRICS = ("large", "term_atr", "ret10")


def compare(df: pd.DataFrame, a: str, b: str, col: str, label: str) -> dict:
    sub = df[df["arm"].isin([a, b])].dropna(subset=[col])
    if sub.empty or sub["arm"].nunique() < 2:
        r = {"n_a": 0, "n_b": 0, "mean_a": np.nan, "mean_b": np.nan, "diff": np.nan,
             "lo": np.nan, "hi": np.nan, "p": np.nan,
             "label": label, "arm": a, "vs": b, "metric": col}
        TESTS.append(r)
        return r
    r = rc.cluster_boot_diff(sub, col, (sub["arm"] == a).to_numpy(),
                             (sub["arm"] == b).to_numpy())
    r.update({"label": label, "arm": a, "vs": b, "metric": col})
    TESTS.append(r)
    return r


def main() -> None:
    ep = pd.read_parquet(os.path.join(DATA, "episodes.parquet"))
    ep = ep[ep["mkt"].isin(["stock", "index"])].copy()
    st = ep[ep["mkt"] == "stock"]

    print("=" * 78)
    print("1. SETUP INVENTORY (stocks + indices, 2025-03-28 .. 2026-07-20)")
    print("=" * 78)
    inv = (ep.groupby("arm")
             .agg(n=("large", "size"), names=("underlying", "nunique"),
                  p_large=("large", "mean"), term=("term_atr", "mean"),
                  ret10=("ret10", "mean"), mfe=("mfe_atr", "mean"),
                  mae=("mae_atr", "mean"), trunc=("truncated", "mean"))
             .sort_values("n", ascending=False))
    print(inv.to_string(float_format=lambda v: f"{v:.4f}"))
    print("\nstocks only:")
    print(st.groupby("arm").agg(n=("large", "size"), names=("underlying", "nunique"),
                                p_large=("large", "mean"), term=("term_atr", "mean"),
                                ret10=("ret10", "mean")).to_string(
        float_format=lambda v: f"{v:.4f}"))

    print("\n" + "=" * 78)
    print("2. SETUP vs CONTROLS  (cluster bootstrap by underlying, 2000 draws)")
    print("=" * 78)
    arms = [a for a in D.ARMS if a in set(ep["arm"])]
    ctrls = ["ctrl_unconditional", "ctrl_random"]
    for a in arms:
        for c in ctrls:
            for m in METRICS:
                compare(ep, a, c, m, f"{a} vs {c}")
    for a in (D.PRIMARY_ARM, D.FULL_ARM):
        c = "ctrl_matched_" + a
        if c in set(ep["arm"]):
            for m in METRICS:
                compare(ep, a, c, m, f"{a} vs {c}")

    print("\n" + "=" * 78)
    print("3. ELEMENT ABLATIONS  (each element removed from the full setup)")
    print("=" * 78)
    for a in ("abl_no_div", "abl_no_tl", "abl_no_hl", "abl_no_cross"):
        if a in set(ep["arm"]):
            for m in METRICS:
                compare(ep, D.FULL_ARM, a, m, f"full vs {a}")
    # marginal contribution of the divergence element on its own
    for m in METRICS:
        compare(ep, "cross_div", "cross", m, "cross_div vs cross (divergence alone)")

    # ---- multiplicity over the WHOLE grid --------------------------------
    tdf = pd.DataFrame(TESTS)
    tdf["p_bonf"] = np.minimum(1.0, tdf["p"] * len(tdf))
    tdf["q_bh"] = rc.bh(list(tdf["p"]))
    tdf = tdf[["label", "metric", "n_a", "n_b", "mean_a", "mean_b", "diff",
               "lo", "hi", "p", "p_bonf", "q_bh"]]
    tdf.to_csv(os.path.join(DATA, "tests.csv"), index=False)
    show = tdf[tdf["metric"].isin(["large", "term_atr"])]
    print(show.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    print(f"\ncomparisons registered: {len(tdf)}  (Bonferroni factor {len(tdf)})")
    print(f"survive q_BH<0.10: {int((tdf['q_bh'] < 0.10).sum())}   "
          f"survive p_bonf<0.05: {int((tdf['p_bonf'] < 0.05).sum())}")

    print("\n" + "=" * 78)
    print("4. CROSSOVER STRENGTH CONDITIONING  (monotone across deciles?)")
    print("=" * 78)
    base = ep[ep["arm"] == "cross"].copy()          # widest sample -> most power
    for meas in ("str_hist", "str_slope", "str_below0", "str_thrust", "str_volz",
                 "str_div_macd"):
        d = base.dropna(subset=[meas])
        if meas == "str_div_macd":
            d = ep[ep["arm"] == "cross_divany"].dropna(subset=[meas])
        if len(d) < 100:
            print(f"{meas}: n={len(d)} too few")
            continue
        d = d.copy()
        d["dec"] = pd.qcut(d[meas], 10, labels=False, duplicates="drop")
        g = d.groupby("dec").agg(n=("large", "size"), p=("large", "mean"),
                                 term=("term_atr", "mean"), val=(meas, "median"))
        rho = d[[meas, "large"]].corr(method="spearman").iloc[0, 1]
        rho_t = d[[meas, "term_atr"]].corr(method="spearman").iloc[0, 1]
        # monotonicity: Spearman of decile-mean vs decile index
        mono = pd.Series(g["p"].to_numpy()).corr(
            pd.Series(np.arange(len(g)), dtype=float), method="spearman")
        print(f"\n{meas}  n={len(d)}  spearman(x,P(large))={rho:+.3f}  "
              f"spearman(x,term_atr)={rho_t:+.3f}  decile-monotonicity={mono:+.3f}")
        print(g.to_string(float_format=lambda v: f"{v:.4f}"))

    print("\n" + "=" * 78)
    print("5. PER NON-OVERLAPPING QUARTER  (primary + full arm)")
    print("=" * 78)
    for a in (D.PRIMARY_ARM, "cross_divany", D.FULL_ARM, "full_any", "cross",
              "ctrl_unconditional"):
        sub = ep[ep["arm"] == a]
        q = sub.groupby("quarter").agg(n=("large", "size"), p_large=("large", "mean"),
                                       term=("term_atr", "mean"))
        print(f"\n--- {a}")
        print(q.to_string(float_format=lambda v: f"{v:.4f}"))

    print("\n" + "=" * 78)
    print("6. CONCENTRATION: ex-top-3 winners, and how much rests on PNB")
    print("=" * 78)
    for a in (D.PRIMARY_ARM, "cross_divany", D.FULL_ARM, "full_any", "cross_divany_hl"):
        sub = ep[ep["arm"] == a].sort_values("term_atr", ascending=False)
        if sub.empty:
            continue
        top3 = sub.head(3)
        ex = sub.iloc[3:]
        print(f"\n--- {a}  n={len(sub)}")
        print("  top-3 by term_atr:",
              ", ".join(f"{r.underlying}@{r.session_entry} {r.term_atr:+.2f}"
                        for r in top3.itertuples()))
        print(f"  term_atr  all={sub['term_atr'].mean():+.4f}  "
              f"ex-top3={ex['term_atr'].mean():+.4f}  median={sub['term_atr'].median():+.4f}")
        print(f"  P(large)  all={sub['large'].mean():.4f}  ex-top3={ex['large'].mean():.4f}")
        pnb = sub[sub["underlying"] == "PNB"]
        print(f"  PNB episodes in this arm: {len(pnb)} "
              f"({len(pnb)/len(sub):.1%} of n)  term_atr {list(np.round(pnb['term_atr'],2))}")
        topn = sub.groupby("underlying")["term_atr"].sum().sort_values(ascending=False)
        print(f"  top-5 names by summed term_atr: "
              f"{', '.join(f'{k} {v:+.1f}' for k, v in topn.head(5).items())}")
        print(f"  share of total positive term_atr from the top 5 names: "
              f"{topn.head(5).sum() / max(topn[topn > 0].sum(), 1e-9):.1%}")

    print("\n" + "=" * 78)
    print("7. INDEX vs STOCK split (primary arm)")
    print("=" * 78)
    for a in (D.PRIMARY_ARM, "cross_divany", "cross"):
        sub = ep[ep["arm"] == a]
        print(f"\n--- {a}")
        print(sub.groupby("mkt").agg(n=("large", "size"), p_large=("large", "mean"),
                                     term=("term_atr", "mean")).to_string(
            float_format=lambda v: f"{v:.4f}"))


if __name__ == "__main__":
    main()
