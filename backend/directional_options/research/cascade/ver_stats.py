"""(VERIFY 3,4,5,6) Statistics on the REPAIRED option tape, recomputed from
scratch rather than accepted from the shipped pass.

  * episode-clustered bootstrap by UNDERLYING (instruments are the independent
    unit; a multi-timeframe rule fires many times on one name);
  * every comparison counted, Bonferroni and Benjamini-Hochberg applied over
    the FULL grid actually reported here;
  * headline re-run without the top-3 episodes;
  * per NON-OVERLAPPING quarter;
  * +1 entry/exit bar lag on both tranches;
  * liquidity stress (fills only at bars that actually traded);
  * costs restated as a % of gross premium turnover.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
RNG = np.random.default_rng(20260721)
NBOOT = 4000
OUT = []


def P(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    OUT.append(s)


def boot_mean(vals: np.ndarray, clus: np.ndarray, nboot=NBOOT):
    """Cluster bootstrap of the mean; returns (mean, lo, hi, p_two_sided_vs_0)."""
    uq, inv = np.unique(clus, return_inverse=True)
    idx = [np.where(inv == i)[0] for i in range(len(uq))]
    m = float(np.mean(vals))
    bs = np.empty(nboot)
    for b in range(nboot):
        pick = RNG.integers(0, len(uq), len(uq))
        bs[b] = float(np.mean(vals[np.concatenate([idx[i] for i in pick])]))
    lo, hi = np.percentile(bs, [2.5, 97.5])
    p = 2.0 * min((bs <= 0).mean(), (bs >= 0).mean())
    return m, lo, hi, max(p, 1.0 / nboot)


def boot_diff(a_v, a_c, b_v, b_c, nboot=NBOOT):
    """Cluster bootstrap of mean(a)-mean(b), resampling the SHARED cluster set."""
    uq = np.unique(np.concatenate([a_c, b_c]))
    ai = {u: np.where(a_c == u)[0] for u in uq}
    bi = {u: np.where(b_c == u)[0] for u in uq}
    d = float(np.mean(a_v) - np.mean(b_v))
    bs = np.empty(nboot)
    for k in range(nboot):
        pick = uq[RNG.integers(0, len(uq), len(uq))]
        A = np.concatenate([ai[u] for u in pick]) if len(pick) else np.array([], int)
        B = np.concatenate([bi[u] for u in pick]) if len(pick) else np.array([], int)
        bs[k] = (np.mean(a_v[A]) if len(A) else np.nan) - \
                (np.mean(b_v[B]) if len(B) else np.nan)
    bs = bs[np.isfinite(bs)]
    lo, hi = np.percentile(bs, [2.5, 97.5])
    p = 2.0 * min((bs <= 0).mean(), (bs >= 0).mean())
    return d, lo, hi, max(p, 1.0 / len(bs))


def bh(ps):
    p = np.asarray(ps, float)
    n = len(p)
    o = np.argsort(p)
    q = np.empty(n)
    prev = 1.0
    for r in range(n - 1, -1, -1):
        prev = min(prev, p[o[r]] * n / (r + 1))
        q[o[r]] = prev
    return q


def main() -> None:
    t = pd.read_parquet(os.path.join(DATA, "ver_repair.parquet"))
    t["ep"] = t["underlying"] + "|" + t["entry_time"].astype(str) + "|" + t["side"].astype(str)
    P("=" * 78)
    P("VERIFICATION — pyramid economics on the REPAIRED option tape")
    P("=" * 78)
    P(f"rows {len(t)}   episodes {t['ep'].nunique()}   underlyings {t['underlying'].nunique()}")

    # ---- episode clustering is real? -----------------------------------
    P("\n[4a] EPISODE CLUSTERING — is one row really one episode?")
    d = t[(t.family == "s1_primary") & (t.arm == "pyramid") & (t.band == "deep_itm")]
    P(f"  deep_itm pyramid signal rows={len(d)} distinct (underlying,entry_time,side)"
      f"={d['ep'].nunique()}  -> duplicates: {len(d) - d['ep'].nunique()}")
    pe = d.groupby("underlying").size()
    P(f"  episodes per underlying: median {pe.median():.0f}  max {pe.max()} "
      f"({pe.idxmax()})  n_underlyings {len(pe)}")
    gap = (d.sort_values(["underlying", "entry_time"])
           .groupby("underlying")["entry_time"].diff().dt.days.dropna())
    P(f"  calendar gap between consecutive episodes of the SAME name: "
      f"min {gap.min():.0f}d  p10 {gap.quantile(.1):.0f}d  median {gap.median():.0f}d")
    P(f"  -> clustering by underlying (not by row) is therefore the right unit; "
      f"{(gap <= 10).mean():.1%} of consecutive episodes overlap a 10-session hold.")

    # ---- headline grid --------------------------------------------------
    comps = []
    P("\n[3] HEADLINE — mean return on ALLOCATED capital (Rs 75k), base cost")
    P(f"{'band':11s} {'arm':11s} {'family':12s} {'n':>5s} {'SHIPPED':>9s} "
      f"{'REPAIRED':>9s} {'95% CI':>19s} {'p':>7s} {'lag1':>8s} {'liq':>8s}")
    for band in ("deep_itm", "slight_itm"):
        for arm in ("pyramid", "fixed_t1", "fixed_hold", "s2_only"):
            for fam in ("s1_primary", "ctrl_long", "ctrl_short", "ctrl_random"):
                g = t[(t.band == band) & (t.arm == arm) & (t.family == fam)]
                if len(g) < 20:
                    continue
                m, lo, hi, p = boot_mean(g["roc_new"].to_numpy(),
                                         g["underlying"].to_numpy())
                comps.append({"kind": "vs_zero", "band": band, "arm": arm,
                              "fam": fam, "n": len(g), "est": m, "lo": lo,
                              "hi": hi, "p": p})
                P(f"{band:11s} {arm:11s} {fam:12s} {len(g):5d} "
                  f"{g['roc_old'].mean():+9.4f} {m:+9.4f} "
                  f"[{lo:+7.4f},{hi:+7.4f}] {p:7.4f} "
                  f"{g['roc_new_lag1'].mean():+8.4f} {g['roc_liq'].mean():+8.4f}")

    # ---- signal vs matched control -------------------------------------
    P("\n[3b] SIGNAL vs MATCHED CONTROL (repaired tape, episode-clustered)")
    P(f"{'band':11s} {'arm':11s} {'vs':12s} {'diff':>9s} {'95% CI':>19s} {'p':>7s}")
    for band in ("deep_itm", "slight_itm"):
        for arm in ("pyramid", "fixed_t1", "fixed_hold", "s2_only"):
            a = t[(t.band == band) & (t.arm == arm) & (t.family == "s1_primary")]
            if len(a) < 20:
                continue
            for fam in ("ctrl_long", "ctrl_short", "ctrl_random"):
                b = t[(t.band == band) & (t.arm == arm) & (t.family == fam)]
                if len(b) < 20:
                    continue
                dd, lo, hi, p = boot_diff(a["roc_new"].to_numpy(),
                                          a["underlying"].to_numpy(),
                                          b["roc_new"].to_numpy(),
                                          b["underlying"].to_numpy())
                comps.append({"kind": "vs_ctrl", "band": band, "arm": arm,
                              "fam": fam, "n": len(a), "est": dd, "lo": lo,
                              "hi": hi, "p": p})
                P(f"{band:11s} {arm:11s} {fam:12s} {dd:+9.4f} "
                  f"[{lo:+7.4f},{hi:+7.4f}] {p:7.4f}")

    # ---- pyramid vs fixed sizing ---------------------------------------
    P("\n[3c] PYRAMID vs FIXED SIZING on the same signal (does the schedule help?)")
    for band in ("deep_itm", "slight_itm"):
        a = t[(t.band == band) & (t.arm == "pyramid") & (t.family == "s1_primary")]
        for arm in ("fixed_t1", "fixed_hold", "s2_only"):
            b = t[(t.band == band) & (t.arm == arm) & (t.family == "s1_primary")]
            if len(b) < 20:
                continue
            dd, lo, hi, p = boot_diff(a["roc_new"].to_numpy(), a["underlying"].to_numpy(),
                                      b["roc_new"].to_numpy(), b["underlying"].to_numpy())
            comps.append({"kind": "arm_vs_arm", "band": band, "arm": "pyramid",
                          "fam": arm, "n": len(a), "est": dd, "lo": lo, "hi": hi, "p": p})
            P(f"  {band:11s} pyramid - {arm:11s} {dd:+9.4f} "
              f"[{lo:+7.4f},{hi:+7.4f}] p={p:.4f}")

    # ---- multiplicity ----------------------------------------------------
    C = pd.DataFrame(comps)
    K = len(C)
    C["bonf"] = np.minimum(1.0, C["p"] * K)
    C["q"] = bh(C["p"].to_numpy())
    P(f"\n[4b] MULTIPLICITY — K = {K} comparisons reported above.")
    P(f"     Bonferroni threshold alpha=0.05 -> raw p must be < {0.05/K:.5f}")
    sig = C[C["q"] < 0.05].sort_values("p")
    P(f"     comparisons surviving BH q<0.05: {len(sig)}")
    P(C.sort_values("p").head(18)[["kind", "band", "arm", "fam", "n", "est",
                                   "p", "bonf", "q"]].round(4).to_string(index=False))
    P("\n     of those, the ones that are SIGNAL-BEATS-CONTROL (the only kind "
      "that could justify wiring):")
    sc = C[(C["kind"] == "vs_ctrl") & (C["q"] < 0.05)]
    P("     " + ("NONE" if sc.empty else
                 sc.round(4).to_string(index=False).replace("\n", "\n     ")))

    # ---- top-3 removal ---------------------------------------------------
    P("\n[5a] HEADLINE WITHOUT THE TOP-3 EPISODES (repaired tape)")
    P(f"{'band':11s} {'arm':11s} {'fam':12s} {'n':>5s} {'all':>9s} {'ex-top3':>9s} "
      f"{'ex-top5%':>9s} {'winrate':>8s} {'top5%share':>11s}")
    for band in ("deep_itm", "slight_itm"):
        for arm in ("pyramid", "fixed_t1"):
            for fam in ("s1_primary", "ctrl_long"):
                g = t[(t.band == band) & (t.arm == arm) & (t.family == fam)]
                if len(g) < 20:
                    continue
                v = np.sort(g["roc_new"].to_numpy())[::-1]
                k5 = max(1, int(round(0.05 * len(v))))
                gains = v[v > 0].sum()
                P(f"{band:11s} {arm:11s} {fam:12s} {len(v):5d} {v.mean():+9.4f} "
                  f"{v[3:].mean():+9.4f} {v[k5:].mean():+9.4f} "
                  f"{(v > 0).mean():8.3f} {v[:k5].sum()/gains if gains>0 else np.nan:11.3f}")

    # ---- quarters --------------------------------------------------------
    P("\n[5b] PER NON-OVERLAPPING QUARTER (repaired, mean roc, n in brackets)")
    for band in ("deep_itm", "slight_itm"):
        for arm in ("pyramid", "fixed_t1"):
            g = t[(t.band == band) & (t.arm == arm)]
            piv = g.pivot_table(index="quarter", columns="family",
                                values="roc_new", aggfunc="mean")
            cnt = g.pivot_table(index="quarter", columns="family",
                                values="roc_new", aggfunc="size")
            P(f"\n  {band} / {arm}")
            for q in piv.index:
                if cnt.loc[q].get("s1_primary", 0) < 10:
                    continue
                def _g(col):
                    v = piv.loc[q].get(col, np.nan)
                    return f"{v:+7.4f}" if np.isfinite(v) else "    n/a"

                def _n(col):
                    v = cnt.loc[q].get(col, 0)
                    return int(v) if np.isfinite(v) else 0
                P(f"    {q}  signal {_g('s1_primary')} [{_n('s1_primary'):3d}]   "
                  f"ctrl_long {_g('ctrl_long')} [{_n('ctrl_long'):3d}]   "
                  f"ctrl_short {_g('ctrl_short')}")
            sq = g[g.family == "s1_primary"].groupby("quarter")["roc_new"].agg(["mean", "size"])
            sq = sq[sq["size"] >= 10]
            P(f"    -> signal positive in {int((sq['mean'] > 0).sum())} of {len(sq)} quarters")

    # ---- costs as % of gross --------------------------------------------
    P("\n[3d] COSTS MATERIALLY APPLIED?")
    for band in ("deep_itm", "slight_itm"):
        for arm in ("pyramid", "fixed_t1"):
            g = t[(t.band == band) & (t.arm == arm) & (t.family == "s1_primary")]
            gross_turn = g["units"].sum() * 25_000.0
            cost = 0.016 * gross_turn
            gross_pnl = g["pnl_new"].sum() + cost
            P(f"  {band:11s} {arm:11s} premium turnover Rs {gross_turn:,.0f}   "
              f"cost Rs {cost:,.0f}  = {100*cost/gross_turn:.2f}% of turnover, "
              f"{abs(100*cost/gross_pnl) if gross_pnl else np.nan:.1f}% of |gross P&L| "
              f"(gross {gross_pnl:+,.0f} -> net {g['pnl_new'].sum():+,.0f})")

    with open(os.path.join(HERE, "ver_results.txt"), "w") as fh:
        fh.write("\n".join(OUT) + "\n")


if __name__ == "__main__":
    main()
