"""What is left after the selection step is shown to add nothing.

mp_swing_wf2.py established two things. The walk-forward's +0.795% is a
TRADE-COUNT artefact -- fold 1 contributed 17 of the 35 trades and fold 3 one --
and, measured fold by fold, the chosen rule LOST to the average eligible
candidate (-0.069% per fold, 3 of 6 folds won). Selection added nothing. What
the anchored walk-forward actually measured was the drift of the long side of
the family.

So the only remaining question worth asking is whether any single rule, held
fixed with no selection at all, beats simply being long BANKNIFTY. That is a
weaker claim than the walk-forward was making, and it has to clear a harder bar:
these rules are long-biased in an index that rose 41% over the window, so the
comparison must be against the CONDITIONAL base rate, not against zero.

  A. DIFFERENCE IN MEANS. Rule days vs every other session, Welch t. A rule that
     beats zero but not the complement is selling index beta.
  B. NEWEY-WEST on the standalone series at lag h-1, because h=4 holds entered
     on consecutive sessions overlap by three days.
  C. YEAR BY YEAR, because the walk-forward's whole failure mode was one good year.
  D. THE INCUMBENT AT h=8, which was the single strongest number in the study
     and needs to be reported with its overlap correction rather than raw.

    docker exec nomadcurie_vanguard_cycle python /vanguard/research/mp_swing_wf3.py
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.mp_auction import dsn  # noqa: E402
from research.mp_multi_tf import load_mtf, targets  # noqa: E402
from research.mp_swing_wf import (  # noqa: E402
    COST_PCT, HORIZONS, build_state, nw_t, plain_t, rule_defs)


def welch(a: pd.Series, b: pd.Series) -> float:
    a, b = a.dropna(), b.dropna()
    if len(a) < 3 or len(b) < 3:
        return np.nan
    va, vb = a.var(ddof=1) / len(a), b.var(ddof=1) / len(b)
    return (a.mean() - b.mean()) / np.sqrt(va + vb) if va + vb > 0 else np.nan


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=dsn())
    args = ap.parse_args()
    conn = psycopg2.connect(args.dsn)
    try:
        d = load_mtf(conn, ["BANKNIFTY"], date(2021, 1, 1))
    finally:
        conn.close()
    d["dt"] = pd.to_datetime(d["dt"])
    d = targets(d, horizons=HORIZONS)
    d = build_state(d)
    d = d[d["m_val"].notna() & d["w_val"].notna()].reset_index(drop=True)
    defs = rule_defs(d)

    print("=" * 112)
    print("A/B. EVERY RULE HELD FIXED, FULL SAMPLE, h=4 -- vs the COMPLEMENT, not vs zero")
    print("=" * 112)
    print(f"   {'rule':<34}{'n':>6}{'mean %':>9}{'base %':>9}{'excess':>9}"
          f"{'Welch t':>9}{'NW t':>7}{'net vs base':>13}")
    rows = []
    for name, (side, mask) in defs.items():
        r = (side * d.loc[mask, "cc4"]).dropna()
        comp = (side * d.loc[~mask, "cc4"]).dropna()
        if len(r) < 10:
            continue
        w = welch(r, comp)
        nw = nw_t(r.sort_index().values, 3)
        rows.append((name, side, mask, len(r), r.mean(), comp.mean(), w, nw))
        print(f"   {name:<34}{len(r):>6}{r.mean():>+9.3f}{comp.mean():>+9.3f}"
              f"{r.mean()-comp.mean():>+9.3f}{w:>+9.2f}{nw:>+7.2f}"
              f"{r.mean()-comp.mean()-COST_PCT:>+13.3f}")
    allr = d["cc4"].dropna()
    print(f"   {'-- unconditional long':<34}{len(allr):>6}{allr.mean():>+9.3f}"
          f"{'':>9}{'':>9}{'':>9}{nw_t(allr.values, 3):>+7.2f}")

    print("\n" + "=" * 112)
    print("C. YEAR BY YEAR, the three rules with the largest excess and n>150")
    print("=" * 112)
    big = [r for r in rows if r[3] > 150]
    big.sort(key=lambda r: -(r[4] - r[5]))
    d["yr"] = d["dt"].dt.year
    for name, side, mask, n, mu, bs, w, nw in big[:3]:
        print(f"\n   {name}   (full sample n {n}, excess {mu-bs:+.3f}%)")
        print(f"      {'year':<8}{'n':>6}{'mean %':>9}{'base %':>9}{'excess':>9}{'win':>7}")
        for yr, g in d.groupby("yr"):
            m = mask.reindex(g.index).fillna(False)
            r = (side * g.loc[m, "cc4"]).dropna()
            b = (side * g["cc4"]).dropna()
            if len(r) < 5:
                continue
            print(f"      {yr:<8}{len(r):>6}{r.mean():>+9.3f}{b.mean():>+9.3f}"
                  f"{r.mean()-b.mean():>+9.3f}{(r > 0).mean()*100:>6.0f}%")
    unc = d.groupby("yr")["cc4"].agg(["count", "mean"])
    print(f"\n   unconditional long by year: "
          + "  ".join(f"{y} {r['mean']:+.3f}%" for y, r in unc.iterrows()))

    print("\n" + "=" * 112)
    print("D. THE INCUMBENT L01 (below day+week+month value) at every horizon, "
          "with the overlap correction")
    print("=" * 112)
    m = defs["L01 below day+week+month VA"][1]
    print(f"   {'h':<5}{'n':>6}{'mean %':>9}{'base %':>9}{'excess':>9}{'win':>7}"
          f"{'plain t':>9}{'Welch t':>9}{'NW t':>7}")
    for h in HORIZONS:
        r = d.loc[m, f"cc{h}"].dropna()
        c = d.loc[~m, f"cc{h}"].dropna()
        print(f"   {h:<5}{len(r):>6}{r.mean():>+9.3f}{c.mean():>+9.3f}"
              f"{r.mean()-c.mean():>+9.3f}{(r > 0).mean()*100:>6.0f}%"
              f"{plain_t(r):>+9.2f}{welch(r, c):>+9.2f}{nw_t(r.sort_index().values, h-1):>+7.2f}")
    print("\n   L01 by year (h=8):")
    for yr, g in d.groupby("yr"):
        mm = m.reindex(g.index).fillna(False)
        r = g.loc[mm, "cc8"].dropna()
        if len(r) < 3:
            print(f"      {yr}   n {len(r):>3}   (too few)")
            continue
        print(f"      {yr}   n {len(r):>3}   mean {r.mean():>+7.3f}%   "
              f"base {g['cc8'].mean():>+7.3f}%   win {(r > 0).mean()*100:>3.0f}%")

    print("\n" + "=" * 112)
    print("E. RISK COMPARISON over the full sample: rules vs staying long")
    print("=" * 112)
    print(f"   {'series':<34}{'n':>6}{'mean %':>9}{'sd %':>8}{'mean/sd':>9}")
    for name, side, mask, n, mu, bs, w, nw in big[:3] + [
            (nm, sd, mk, nn, mm2, bb, ww, nn2) for nm, sd, mk, nn, mm2, bb, ww, nn2
            in rows if nm.startswith("L01")]:
        r = (side * d.loc[mask, "cc4"]).dropna()
        print(f"   {name:<34}{len(r):>6}{r.mean():>+9.3f}{r.std(ddof=1):>8.2f}"
              f"{r.mean()/r.std(ddof=1):>9.3f}")
    print(f"   {'-- unconditional long':<34}{len(allr):>6}{allr.mean():>+9.3f}"
          f"{allr.std(ddof=1):>8.2f}{allr.mean()/allr.std(ddof=1):>9.3f}")


if __name__ == "__main__":
    main()
