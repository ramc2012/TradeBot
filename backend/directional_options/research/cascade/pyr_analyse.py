"""(C-cascade, study 4) PYRAMID ECONOMICS — analysis.

Reads data/pyr_trades.parquet (written by pyr_run.py) and reports, for the
a-priori primary maturity rule and for the full rule grid:

  * arm vs arm vs matched control, episode-clustered bootstrap, BH/Bonferroni
    across EVERY comparison made;
  * per NON-OVERLAPPING quarter;
  * the +1 entry/exit bar lag variant;
  * vehicle sensitivity (index vs stock x deep-ITM vs slight-ITM);
  * the WINNER-CONCENTRATION PROFILE, reported as a description of the payoff
    shape (how many episodes carry the result, P&L without the top 3), not
    only as a robustness flag;
  * capital utilisation, because an arm that deploys less capital gets a
    flattering return-on-allocation and that must not be mistaken for edge.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "setups_2d3d"))

import run_cascade as rc  # noqa: E402
from pyr_run import ARMS, PRIMARY_RULE, UNIT, UNITS_MAX  # noqa: E402

DATA = os.path.join(HERE, "data")
L: list[str] = []
P = L.append
PV: list[float] = []
LAB: list[tuple] = []


def boot(df: pd.DataFrame, col: str, a: pd.Series, b: pd.Series) -> dict:
    return rc.cluster_boot_diff(df.reset_index(drop=True), col,
                                a.to_numpy(), b.to_numpy())


def cmp(tag: str, df: pd.DataFrame, col: str, a: pd.Series, b: pd.Series) -> dict:
    st = boot(df, col, a, b)
    PV.append(st["p"])
    LAB.append((tag, st))
    return st


def concentration(x: np.ndarray) -> dict:
    """Payoff-shape description of a P&L vector (rupees per episode)."""
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n == 0:
        return {}
    tot = x.sum()
    s = np.sort(x)[::-1]
    win = x > 0
    pos = x[win].sum()
    top3 = s[:3].sum()
    top5pct = s[:max(1, int(round(0.05 * n)))].sum()
    # how many of the LARGEST WINNERS account for half of all gross gains
    w = np.sort(x[win])[::-1]
    need = int(np.searchsorted(np.cumsum(w), 0.5 * pos)) + 1 if pos > 0 else 0
    return {"n": n, "total": tot, "mean": x.mean(), "median": float(np.median(x)),
            "winrate": float(win.mean()), "top3": top3, "ex_top3": tot - top3,
            "top5pct_share": (top5pct / pos) if pos > 0 else np.nan,
            "n_carry": need, "carry_pct": need / n, "best": s[0], "worst": s[-1],
            "gross_gain": pos, "top3_of_gain": (top3 / pos) if pos > 0 else np.nan}


def main() -> None:
    tr = pd.read_parquet(os.path.join(DATA, "pyr_trades.parquet"))
    tr["util"] = tr["units"] / UNITS_MAX
    tr["roc_dep_base"] = tr["pnl_base"] / (tr["units"] * UNIT)
    pri = tr[tr["rule"] == PRIMARY_RULE].copy()

    P("=" * 84)
    P("(4) PYRAMID ECONOMICS — the owner's structure on OPTIONS, net of costs")
    P("=" * 84)
    P(f"vehicle: MONTHLY expiry, DTE 8-22 at entry, ITM; unit = Rs {UNIT:,.0f} premium;")
    P(f"every arm allocates the same maximum Rs {UNITS_MAX*UNIT:,.0f}.")
    P(f"primary maturity rule (pre-registered from the owner's own vocabulary): "
      f"{PRIMARY_RULE}")
    P(f"rows {len(tr)}  primary-rule rows {len(pri)}  "
      f"span {pri['entry_time'].min()} .. {pri['entry_time'].max()}")
    P("")

    # ---------------------------------------------------------------- headline
    P("-" * 84)
    P("HEADLINE — return on ALLOCATED capital (roc_base), primary rule")
    P(f"{'band':<11}{'arm':<12}{'family':<13}{'n':>6}{'mean%':>8}{'med%':>8}"
      f"{'util':>7}{'ondep%':>8}{'opt%':>7}{'pess%':>7}{'lag1%':>7}")
    for band, g1 in pri.groupby("band"):
        for arm in ARMS:
            for fam in ("s1_primary", "ctrl_long", "ctrl_short", "ctrl_random"):
                g = g1[(g1["arm"] == arm) & (g1["family"] == fam)]
                if len(g) < 5:
                    continue
                P(f"{band:<11}{arm:<12}{fam:<13}{len(g):>6}"
                  f"{100*g['roc_base'].mean():>8.2f}{100*g['roc_base'].median():>8.2f}"
                  f"{g['util'].mean():>7.2f}{100*g['roc_dep_base'].mean():>8.2f}"
                  f"{100*g['roc_optimistic'].mean():>7.2f}"
                  f"{100*g['roc_pessimistic'].mean():>7.2f}"
                  f"{100*g['roc_base_lag1'].mean():>7.2f}")
    P("")
    P("NOTE on `util`: the pyramid deploys the full 3 units only when the higher")
    P("timeframe confirms (~1 episode in 5). Its return-on-ALLOCATION is therefore")
    P("mechanically closer to zero than a fixed-size arm's. `ondep%` divides by the")
    P("capital actually deployed and removes that flattery.")
    P("")

    # ------------------------------------------------------------- comparisons
    P("-" * 84)
    P("ARM COMPARISONS, episode-clustered bootstrap by underlying (2000 draws)")
    P("(a) the owner's structure vs the alternatives, on the SIGNAL")
    for band, g1 in pri.groupby("band"):
        s = g1[g1["family"] == "s1_primary"]
        for a, b in (("pyramid", "fixed_t1"), ("pyramid", "fixed_hold"),
                     ("pyramid", "s2_only"), ("fixed_t1", "s2_only")):
            d = s[s["arm"].isin([a, b])]
            if d["arm"].value_counts().min() if len(d) else 0:
                st = cmp(f"{band}|signal|{a}-vs-{b}", d, "roc_base",
                         d["arm"] == a, d["arm"] == b)
                P(f"  {band:<11}{a:<11}vs {b:<12}"
                  f"diff {100*st['diff']:>7.2f}pp  "
                  f"[{100*st['lo']:>7.2f},{100*st['hi']:>7.2f}]  p={st['p']:.4f}")
    P("")
    P("(b) the SAME arm, signal vs matched control (this is the test that matters)")
    for band, g1 in pri.groupby("band"):
        for arm in ARMS:
            for ctrl in ("ctrl_long", "ctrl_random", "ctrl_short"):
                d = g1[(g1["arm"] == arm) & (g1["family"].isin(["s1_primary", ctrl]))]
                if len(d) < 40:
                    continue
                st = cmp(f"{band}|{arm}|s1-vs-{ctrl}", d, "roc_base",
                         d["family"] == "s1_primary", d["family"] == ctrl)
                P(f"  {band:<11}{arm:<12}s1 vs {ctrl:<13}"
                  f"diff {100*st['diff']:>7.2f}pp  "
                  f"[{100*st['lo']:>7.2f},{100*st['hi']:>7.2f}]  p={st['p']:.4f}")
    P("")
    P("(c) is ANY arm's mean above zero? (one-sample: signal arm vs a zero column)")
    for band, g1 in pri.groupby("band"):
        for arm in ARMS:
            d = g1[(g1["arm"] == arm) & (g1["family"] == "s1_primary")]
            if len(d) < 30:
                continue
            z = pd.concat([d.assign(_v=d["roc_base"]), d.assign(_v=0.0)],
                          ignore_index=True)
            st = cmp(f"{band}|{arm}|vs-zero", z, "_v",
                     pd.Series(np.r_[np.ones(len(d), bool), np.zeros(len(d), bool)]),
                     pd.Series(np.r_[np.zeros(len(d), bool), np.ones(len(d), bool)]))
            P(f"  {band:<11}{arm:<12}mean {100*st['mean_a']:>7.2f}pp  "
              f"[{100*st['lo']:>7.2f},{100*st['hi']:>7.2f}]  p={st['p']:.4f}")
    P("")

    # ------------------------------------------- what the pyramid actually pays
    P("-" * 84)
    P("INSIDE THE PYRAMID — the completed pyramid vs the abandoned first tranche")
    P("(the owner's structure pays for ~4 dead first tranches per completed one;")
    P(" this is where that cost shows up in rupees)")
    P(f"  {'band':<11}{'family':<13}{'leg':<22}{'n':>6}{'meanRs':>10}{'medRs':>9}"
      f"{'win%':>7}{'meanRoc%':>10}")
    for band, g1 in pri[pri["arm"] == "pyramid"].groupby("band"):
        for fam in ("s1_primary", "ctrl_long"):
            g = g1[g1["family"] == fam]
            for lab, sub in (("completed (s1+s2, 3u)", g[g["s2"] == 1]),
                             ("abandoned (s1 only, 1u)", g[g["s2"] == 0])):
                if len(sub) < 5:
                    continue
                P(f"  {band:<11}{fam:<13}{lab:<22}{len(sub):>6}"
                  f"{sub['pnl_base'].mean():>10,.0f}{sub['pnl_base'].median():>9,.0f}"
                  f"{100*(sub['pnl_base']>0).mean():>7.1f}"
                  f"{100*sub['roc_dep_base'].mean():>10.2f}")
            if len(g[g["s2"] == 1]) >= 20 and len(g[g["s2"] == 0]) >= 20:
                st = cmp(f"{band}|pyr-{fam}|completed-vs-abandoned", g, "roc_dep_base",
                         g["s2"] == 1, g["s2"] == 0)
                P(f"    -> completed minus abandoned, on DEPLOYED capital: "
                  f"{100*st['diff']:.2f}pp  [{100*st['lo']:.2f},{100*st['hi']:.2f}]  "
                  f"p={st['p']:.4f}")
    P("")

    # ---------------------------------------------------------------- quarters
    P("-" * 84)
    P("PER NON-OVERLAPPING QUARTER — mean roc_base %, signal arms (n in brackets)")
    q = pri[pri["family"] == "s1_primary"]
    qs = sorted(x for x in q["quarter"].unique() if q[q["quarter"] == x].shape[0] >= 30)
    P(f"  {'band':<11}{'arm':<12}" + "".join(f"{x:>16}" for x in qs))
    for band, g1 in q.groupby("band"):
        for arm in ARMS:
            g = g1[g1["arm"] == arm]
            if len(g) < 30:
                continue
            cells = []
            for x in qs:
                gg = g[g["quarter"] == x]
                cells.append(f"{100*gg['roc_base'].mean():>10.2f}({len(gg):>3})"
                             if len(gg) >= 5 else f"{'-':>16}")
            P(f"  {band:<11}{arm:<12}" + "".join(cells))
    P("")

    # ------------------------------------------------------- vehicle sensitivity
    P("-" * 84)
    P("VEHICLE SENSITIVITY — mean roc_base %, signal families only")
    P(f"  {'band':<11}{'mkt':<7}{'arm':<12}{'n':>6}{'mean%':>9}{'med%':>9}"
      f"{'win%':>7}{'meanMny':>9}")
    for (band, mkt), g1 in q.groupby(["band", "mkt"]):
        for arm in ARMS:
            g = g1[g1["arm"] == arm]
            if len(g) < 5:
                continue
            P(f"  {band:<11}{mkt:<7}{arm:<12}{len(g):>6}"
              f"{100*g['roc_base'].mean():>9.2f}{100*g['roc_base'].median():>9.2f}"
              f"{100*(g['pnl_base']>0).mean():>7.1f}{100*g['mny'].mean():>9.2f}")
    P("")

    # -------------------------------------------------------- rule grid (K cells)
    P("-" * 84)
    P("MATURITY-RULE GRID for the scale-out (reported in full, not selected)")
    P(f"  {'band':<11}{'rule':<16}{'pyramid%':>10}{'fixed_t1%':>11}"
      f"{'fixed_hold%':>13}{'s2_only%':>10}{'ctrlL pyr%':>12}")
    for band, g1 in tr.groupby("band"):
        for rule, g2 in g1.groupby("rule"):
            row = f"  {band:<11}{rule:<16}"
            for arm in ARMS:
                g = g2[(g2["arm"] == arm) & (g2["family"] == "s1_primary")]
                row += f"{100*g['roc_base'].mean():>10.2f}  " if len(g) >= 5 else f"{'-':>12}"
            gc = g2[(g2["arm"] == "pyramid") & (g2["family"] == "ctrl_long")]
            row += f"{100*gc['roc_base'].mean():>10.2f}" if len(gc) >= 5 else f"{'-':>12}"
            P(row)
    P("")

    # ------------------------------------------------------------ concentration
    P("-" * 84)
    P("WINNER-CONCENTRATION PROFILE — the payoff SHAPE of each arm")
    P("(rupees, primary rule, signal family; `carry` = how many of the largest")
    P(" winners account for HALF of all gross gains; `top5%share` = share of gross")
    P(" gains delivered by the best 5% of episodes)")
    P(f"  {'band':<11}{'arm':<12}{'n':>5}{'totalRs':>12}{'meanRs':>9}{'medRs':>8}"
      f"{'win%':>7}{'top3Rs':>11}{'exTop3Rs':>11}{'carry':>7}{'top5%share':>11}")
    for band, g1 in q.groupby("band"):
        for arm in ARMS:
            g = g1[g1["arm"] == arm]
            if len(g) < 5:
                continue
            c = concentration(g["pnl_base"].to_numpy())
            P(f"  {band:<11}{arm:<12}{c['n']:>5}{c['total']:>12,.0f}{c['mean']:>9,.0f}"
              f"{c['median']:>8,.0f}{100*c['winrate']:>7.1f}{c['top3']:>11,.0f}"
              f"{c['ex_top3']:>11,.0f}{c['n_carry']:>7}{100*c['top5pct_share']:>11.1f}")
    P("")
    for band, g1 in q.groupby("band"):
        for arm in ("pyramid", "fixed_t1"):
            g = g1[g1["arm"] == arm]
            if len(g) < 5:
                continue
            c = concentration(g["pnl_base"].to_numpy())
            P(f"  {band}/{arm}: best episode Rs {c['best']:,.0f}, worst Rs {c['worst']:,.0f}; "
              f"total Rs {c['total']:,.0f}; without the top 3 Rs {c['ex_top3']:,.0f} "
              f"({100*(c['ex_top3']/(UNITS_MAX*UNIT*c['n'])):.2f}% of allocation)")
    P("")

    # ------------------------------------------------------------- multiplicity
    qv = rc.bh(PV)
    P("-" * 84)
    P(f"MULTIPLE TESTING — K = {len(PV)} comparisons in this study")
    P(f"  Bonferroni alpha = {0.05/max(len(PV),1):.5f}")
    P(f"  {'comparison':<44}{'diff(pp)':>10}{'p':>9}{'q(BH)':>9}{'sig':>5}")
    for (tag, st), qq in sorted(zip(LAB, qv), key=lambda z: z[1]):
        sig = "**" if qq < 0.05 else ("*" if st["p"] < 0.05 else "")
        P(f"  {tag:<44}{100*st['diff']:>10.2f}{st['p']:>9.4f}{qq:>9.4f}{sig:>5}")
    P("")
    txt = "\n".join(L)
    with open(os.path.join(HERE, "pyr_results.txt"), "w") as fh:
        fh.write(txt + "\n")
    print(txt)


if __name__ == "__main__":
    main()
