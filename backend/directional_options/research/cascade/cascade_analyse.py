"""(C-cascade) Analysis — conditional structure of the two-stage cascade.

Reads data/episodes.parquet (one row per instrument x stage-1 episode) and
data/s2_only.parquet, and prints the whole answer set. Every bootstrap
comparison it runs is appended to a single multiplicity family so Bonferroni
and Benjamini-Hochberg can be applied over the TOTAL count actually made.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from run_cascade import DATA, bh, cluster_boot_diff  # noqa: E402
from stages import (  # noqa: E402
    EPISODE_GAP_SESSIONS, LARGE_HORIZON_SESSIONS, LARGE_STP_ATR, LARGE_TGT_ATR,
    S1_VARIANTS, S2_VARIANTS, S2_WINDOW_SESSIONS,
)

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 40)
BAR = "=" * 118
TESTS: list[dict] = []


def T(label: str, df: pd.DataFrame, col: str, a: np.ndarray, b: np.ndarray) -> dict:
    r = cluster_boot_diff(df, col, a, b)
    r["label"] = label
    r["metric"] = col
    TESTS.append(r)
    return r


def line(r: dict) -> str:
    return (f"  {r['label']:<52s} A={r['mean_a']:+.4f} (n={r['n_a']:5d})  "
            f"B={r['mean_b']:+.4f} (n={r['n_b']:5d})  diff={r['diff']:+.4f} "
            f"[{r['lo']:+.4f},{r['hi']:+.4f}] p={r['p']:.4f}")


def main() -> None:
    ep = pd.read_parquet(os.path.join(DATA, "episodes.parquet"))
    s2o = pd.read_parquet(os.path.join(DATA, "s2_only.parquet"))
    ep["entry_time"] = pd.to_datetime(ep["entry_time"], utc=True)

    print(BAR)
    print("0. DESIGN (fixed a priori in stages.py -- nothing swept)")
    print(BAR)
    print(f"""
  stage-1 (LOWER timeframe = 30-minute), primary definition:
      fresh MACD(12,26,9) signal-line cross in the trade direction on the 30m bar
      AND 30m EMA20/EMA50 already ordered in that direction
      AND 30m ADX(14) > 20
      AND the DAILY state is NOT yet confirmed as of the last CLOSED daily bar
          (session s-1) -- the sequence requires an unconfirmed higher timeframe.
      Actionable at the OPEN of the next 30m bar, same session (decision bars
      restricted to <= 14:45 IST).

  stage-2 (HIGHER timeframe = DAILY), primary definition:
      a False->True transition of the daily state
          daily MACD histogram sign = side
          AND close on the correct side of daily SMA20
          AND daily ADX(14) > 20 and rising vs 3 sessions ago
      in sessions [s0 .. s0+{S2_WINDOW_SESSIONS}] where s0 = the stage-1 entry session.
      Actionable at the OPEN of the first 30m bar of the session AFTER the
      confirming daily bar (that daily bar is not closed until 15:30 IST).

  "sustained large move" (label, defined independently of the signal):
      from the entry bar open, side-signed spot reaches +{LARGE_TGT_ATR:.1f} x daily ATR14
      BEFORE it reaches -{LARGE_STP_ATR:.1f} x daily ATR14 against, within {LARGE_HORIZON_SESSIONS} sessions,
      monitored on 30m highs/lows; both touched in one bar scores as the STOP.

  episode clustering: stage-1 fires <= {EPISODE_GAP_SESSIONS} sessions apart, same instrument and
      side, collapse to ONE observation (the first bar). Controls identical.

  alternates: stage-1 in {list(S1_VARIANTS)}, stage-2 in {list(S2_VARIANTS)}
""")

    print(BAR)
    print("1. DATASET")
    print(BAR)
    print("  period", ep["entry_time"].min(), "->", ep["entry_time"].max())
    print("  instruments", ep["underlying"].nunique())
    print("\n  episodes by family x stage-2 variant:")
    print(pd.crosstab(ep["family"], ep["s2_variant"]).to_string())
    P = ep[ep["s2_variant"] == "primary"].copy()
    print("\n  primary cell (s2=primary) -- episodes by family x side:")
    print(pd.crosstab(P["family"], P["side"]).to_string())

    S1 = P[P["family"] == "s1_primary"]
    CL = P[P["family"] == "ctrl_long"]
    CS = P[P["family"] == "ctrl_short"]
    CR = P[P["family"] == "ctrl_random"]
    CTRL = P[P["family"].isin(["ctrl_long", "ctrl_short"])]

    print("\n" + BAR)
    print("2. P(stage-2 confirm | stage-1 confirm)  and the LAG distribution")
    print(BAR)
    tab = (P.groupby(["family", "side"])
             .agg(n=("s2", "size"), p_s2=("s2", "mean"),
                  lag_mean=("s2_lag", lambda v: v[v >= 0].mean()),
                  lag_med=("s2_lag", lambda v: v[v >= 0].median()))
             .round(4))
    print(tab.to_string())
    print("\n  lag distribution in sessions (0 = the stage-1 session's own daily bar confirms):")
    lg = (P[P["s2"] == 1].groupby("family")["s2_lag"]
          .value_counts(normalize=True).unstack().round(3))
    print(lg.to_string())
    print("\n  matched-control comparisons (cluster bootstrap by instrument):")
    print(line(T("P(s2|s1) long vs P(s2|ctrl_long)", P, "s2",
                 ((P["family"] == "s1_primary") & (P["side"] == 1)).to_numpy(),
                 (P["family"] == "ctrl_long").to_numpy())))
    print(line(T("P(s2|s1) short vs P(s2|ctrl_short)", P, "s2",
                 ((P["family"] == "s1_primary") & (P["side"] == -1)).to_numpy(),
                 (P["family"] == "ctrl_short").to_numpy())))
    print(line(T("P(s2|s1) pooled vs P(s2|ctrl pooled)", P, "s2",
                 (P["family"] == "s1_primary").to_numpy(),
                 P["family"].isin(["ctrl_long", "ctrl_short"]).to_numpy())))

    print("\n" + BAR)
    print("3. P(sustained large move) -- cascade vs stage-1 alone vs unconditional")
    print(BAR)

    def arms(d: pd.DataFrame, tag: str) -> pd.DataFrame:
        rows = []
        for name, m in (("all (stage-1 alone)", np.ones(len(d), bool)),
                        ("cascade (s2 confirmed)", (d["s2"] == 1).to_numpy()),
                        ("failed (no s2)", (d["s2"] == 0).to_numpy())):
            g = d[m]
            if not len(g):
                continue
            rows.append({"arm": f"{tag} :: {name}", "n": len(g),
                         "P(large)": g["large"].mean(),
                         "P(stop_first)": (g["hit"] == "stop").mean(),
                         "mfe_atr": g["mfe_atr"].mean(), "mae_atr": g["mae_atr"].mean(),
                         "term_atr": g["term_atr"].mean()})
        return pd.DataFrame(rows)

    print(pd.concat([arms(S1, "s1_primary"), arms(CL, "ctrl_long"),
                     arms(CS, "ctrl_short"), arms(CR, "ctrl_random")],
                    ignore_index=True).round(4).to_string(index=False))

    print("\n  key comparisons (all measured FROM THE STAGE-1 ENTRY BAR):")
    print(line(T("large: s1&s2 vs s1&no-s2", S1, "large",
                 (S1["s2"] == 1).to_numpy(), (S1["s2"] == 0).to_numpy())))
    print(line(T("large: s1&s2 vs s1 all (stage-1 alone)", S1, "large",
                 (S1["s2"] == 1).to_numpy(), np.ones(len(S1), bool))))
    print(line(T("large: s1&s2 vs CTRL&s2 [MATCHED]", P, "large",
                 ((P["family"] == "s1_primary") & (P["s2"] == 1)).to_numpy(),
                 (P["family"].isin(["ctrl_long", "ctrl_short"]) & (P["s2"] == 1)).to_numpy())))
    print(line(T("large: s1 all vs CTRL all [MATCHED]", P, "large",
                 (P["family"] == "s1_primary").to_numpy(),
                 P["family"].isin(["ctrl_long", "ctrl_short"]).to_numpy())))
    print(line(T("large: CTRL&s2 vs CTRL&no-s2 (s2 alone)", CTRL, "large",
                 (CTRL["s2"] == 1).to_numpy(), (CTRL["s2"] == 0).to_numpy())))
    print(line(T("term_atr: s1&s2 vs CTRL&s2 [MATCHED]", P, "term_atr",
                 ((P["family"] == "s1_primary") & (P["s2"] == 1)).to_numpy(),
                 (P["family"].isin(["ctrl_long", "ctrl_short"]) & (P["s2"] == 1)).to_numpy())))

    print("\n  by market and side (cascade arm only):")
    print(P[P["s2"] == 1].groupby(["family", "mkt", "side"])["large"]
          .agg(["size", "mean"]).round(4).to_string())

    print("\n" + BAR)
    print("4. THE SECOND TRANCHE -- outcome measured from the stage-2 entry bar")
    print(BAR)
    t2 = P[P["t2_large"].notna()]
    print(t2.groupby("family")[["t2_large", "t2_mfe_atr", "t2_term_atr"]]
          .agg(["size", "mean"]).round(4).to_string())
    print(f"\n  stage-2 confirmations with NO preceding stage-1 (skip-the-early-tranche): "
          f"n={len(s2o)}  P(large)={s2o['large'].mean():.4f}  term_atr={s2o['term_atr'].mean():+.4f}")
    if len(t2):
        print(line(T("t2_large: cascade vs CTRL-cascade [MATCHED]", t2, "t2_large",
                     (t2["family"] == "s1_primary").to_numpy(),
                     t2["family"].isin(["ctrl_long", "ctrl_short"]).to_numpy())))
        both = pd.concat([
            t2[t2["family"] == "s1_primary"][["underlying", "t2_large"]]
            .rename(columns={"t2_large": "large"}).assign(arm="cascade"),
            s2o[["underlying", "large"]].assign(arm="s2_only")], ignore_index=True)
        print(line(T("large: cascade-t2 vs stage-2-only (no s1)", both, "large",
                     (both["arm"] == "cascade").to_numpy(),
                     (both["arm"] == "s2_only").to_numpy())))

    print("\n" + BAR)
    print("5. THE FAILED FIRST TRANCHE (stage-1 fired, higher timeframe never confirmed)")
    print(BAR)
    f = S1[S1["s2"] == 0]
    print(f"  n={len(f)}  ({len(f)/max(len(S1),1):.1%} of stage-1 episodes)")
    print("  managed exit at the end of the 3-session window "
          "(stop at -1 ATR if touched first, else mark-out), in ATR units:")
    print(f["t1_exit_atr"].describe(percentiles=[.05, .25, .5, .75, .95]).round(4).to_string())
    print("\n  how the failed tranche ends:")
    print(f["t1_exit_how"].value_counts(normalize=True).round(4).to_string())
    fc = CTRL[CTRL["s2"] == 0]
    print(f"\n  matched controls that never confirmed: n={len(fc)}  "
          f"mean exit_atr={fc['t1_exit_atr'].mean():+.4f} median={fc['t1_exit_atr'].median():+.4f}")
    print(line(T("failed-tranche exit_atr: s1 vs CTRL [MATCHED]", P, "t1_exit_atr",
                 ((P["family"] == "s1_primary") & (P["s2"] == 0)).to_numpy(),
                 (P["family"].isin(["ctrl_long", "ctrl_short"]) & (P["s2"] == 0)).to_numpy())))
    print("\n  if the failed tranche were held the full 10 sessions anyway (unmanaged):")
    print(f"    stage-1 failed : P(large)={f['large'].mean():.4f}  term_atr={f['term_atr'].mean():+.4f}")
    print(f"    control failed : P(large)={fc['large'].mean():.4f}  term_atr={fc['term_atr'].mean():+.4f}")

    print("\n" + BAR)
    print("6. GRID -- every (stage-1 x stage-2) definition pair, matched-control lift")
    print(BAR)
    grid = []
    for s2v in S2_VARIANTS:
        d = ep[ep["s2_variant"] == s2v]
        c = d[d["family"].isin(["ctrl_long", "ctrl_short"])]
        for s1v in S1_VARIANTS:
            s = d[d["family"] == f"s1_{s1v}"]
            dd = pd.concat([s.assign(arm="s1"), c.assign(arm="ctrl")], ignore_index=True)
            r1 = T(f"[grid] P(s2|s1) s1={s1v} s2={s2v}", dd, "s2",
                   (dd["arm"] == "s1").to_numpy(), (dd["arm"] == "ctrl").to_numpy())
            cas = dd[dd["s2"] == 1]
            r2 = T(f"[grid] P(large|cascade) s1={s1v} s2={s2v}", cas, "large",
                   (cas["arm"] == "s1").to_numpy(), (cas["arm"] == "ctrl").to_numpy())
            grid.append({"s1": s1v, "s2": s2v, "n_s1": len(s), "P(s2|s1)": r1["mean_a"],
                         "P(s2|ctrl)": r1["mean_b"], "d_s2": r1["diff"], "p_s2": r1["p"],
                         "n_casc": r2["n_a"], "P(large|casc)": r2["mean_a"],
                         "P(large|ctrl_casc)": r2["mean_b"], "d_large": r2["diff"],
                         "p_large": r2["p"]})
    print(pd.DataFrame(grid).round(4).to_string(index=False))

    print("\n" + BAR)
    print("7. ROBUSTNESS -- by non-overlapping quarter")
    print(BAR)
    q = (P[P["family"].isin(["s1_primary", "ctrl_long", "ctrl_short"])]
         .groupby(["quarter", "family"])
         .agg(n=("large", "size"), p_s2=("s2", "mean"), p_large=("large", "mean")).round(3))
    print(q.to_string())
    print("\n  cascade-arm P(large) by quarter:")
    print(P[P["s2"] == 1].groupby(["quarter", "family"])["large"]
          .agg(["size", "mean"]).round(3).to_string())

    print("\n" + BAR)
    print("8. MULTIPLICITY -- every comparison made in this run")
    print(BAR)
    t = pd.DataFrame(TESTS)
    K = len(t)
    t["p_bonf"] = (t["p"] * K).clip(upper=1.0)
    t["q_bh"] = bh(list(t["p"]))
    print(f"  K = {K} comparisons")
    print(t[["label", "metric", "n_a", "n_b", "mean_a", "mean_b", "diff", "lo", "hi",
             "p", "p_bonf", "q_bh"]].round(4).to_string(index=False))
    surv = t[t["q_bh"] < 0.05]
    print("\n  survivors at BH q<0.05:")
    print(surv[["label", "diff", "p", "q_bh"]].round(4).to_string(index=False)
          if len(surv) else "  (none)")


if __name__ == "__main__":
    main()
