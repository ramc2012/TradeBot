"""Does the swing walk-forward result survive interrogation, or is it 2023?

THE THING THAT NEEDS EXPLAINING. mp_swing_wf.py returned +0.795% per trade,
t +2.07, over 35 out-of-sample trades at h=4. But 26 of those 35 trades land in
the FIRST TWO folds and the remaining six folds contribute nine trades between
them. A mean built that way is a statement about the first eight months of the
out-of-sample window, not about the strategy.

FIVE CHECKS, all declared before running, none of which changes a single rule
definition -- the candidate set stays exactly the 24 of mp_swing_wf.py:

  1. OOS BY YEAR. Where do the trades and the P&L actually sit.
  2. THE AVERAGE CANDIDATE. For each fold, the mean test-window return of EVERY
     eligible candidate, not just the chosen one. If the chosen rule does not
     beat the average candidate, the in-sample selection step added nothing and
     the whole apparatus is measuring family drift.
  3. EACH RULE STANDALONE over the same OOS window with NO selection. A rule
     that only works when picked is a fold artefact.
  4. PROTOCOL SENSITIVITY: anchored vs rolling, 12/18/24-month training, and
     min_trades 12 vs 25. min_trades is the lever that decides whether a rule
     firing 25 times in five years is allowed to win a fold on five signals.
  5. THE INCUMBENT ALONE. Rule L01 (below day+week+month value), fixed, no
     selection, over the full sample and over the OOS window.

    docker exec nomadcurie_vanguard_cycle python /vanguard/research/mp_swing_wf2.py
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
from research.mp_walkforward import HEADER, report, walk_forward  # noqa: E402
from research.mp_swing_wf import (  # noqa: E402
    COST_PCT, HORIZONS, build_state, candidates, nw_t, non_overlapping,
    plain_t, rule_defs, stack)

H = 4


def by_year(res: dict, h: int) -> None:
    o = res["oos"].copy()
    o["yr"] = pd.to_datetime(o["dt"]).dt.year
    print(f"   {'year':<8}{'trades':>8}{'mean %':>9}{'win':>6}{'sum %':>9}")
    for yr, g in o.groupby("yr"):
        r = g["ret"]
        print(f"   {yr:<8}{len(r):>8}{r.mean():>+9.3f}{(r > 0).mean()*100:>5.0f}%"
              f"{r.sum():>+9.2f}")
    r = o["ret"]
    print(f"   {'ALL':<8}{len(r):>8}{r.mean():>+9.3f}{(r > 0).mean()*100:>5.0f}%"
          f"{r.sum():>+9.2f}")
    first2 = o.head(26)["ret"]
    rest = o.iloc[26:]["ret"]
    if len(rest):
        print(f"\n   first 26 trades (folds 1-2)  mean {first2.mean():>+7.3f}%"
              f"   t {plain_t(first2):>+5.2f}")
        print(f"   everything after them        mean {rest.mean():>+7.3f}%"
              f"   t {plain_t(rest):>+5.2f}   n {len(rest)}")


def average_candidate(s: pd.DataFrame, cand: dict, train_m: int, test_m: int,
                      min_trades: int) -> None:
    """Fold by fold: chosen rule vs the mean of every eligible candidate."""
    f = s.sort_values("dt").reset_index(drop=True)
    months = f["dt"].dt.to_period("M")
    uniq = sorted(months.unique())
    print(f"   {'test window':<12}{'chosen':<32}{'chosen test':>12}"
          f"{'avg candidate':>15}{'#eligible':>11}")
    diffs, chosen_all, avg_all = [], [], []
    for i in range(train_m, len(uniq), test_m):
        tr_m, te_m = uniq[0:i], uniq[i:i + test_m]
        if not len(te_m):
            break
        tr, te = f[months.isin(tr_m)], f[months.isin(te_m)]
        best, best_mu, elig = None, -np.inf, []
        for name, mask in cand.items():
            m = mask.reindex(tr.index).fillna(False)
            r = tr.loc[m, "ret"].dropna()
            if len(r) < min_trades:
                continue
            elig.append(name)
            if r.mean() > best_mu:
                best, best_mu = name, r.mean()
        if best is None:
            continue
        tests = {}
        for name in elig:
            m = cand[name].reindex(te.index).fillna(False)
            rr = te.loc[m, "ret"].dropna()
            if len(rr):
                tests[name] = rr.mean()
        ch = tests.get(best, np.nan)
        av = np.nanmean(list(tests.values())) if tests else np.nan
        if np.isfinite(ch) and np.isfinite(av):
            diffs.append(ch - av)
            chosen_all.append(ch)
            avg_all.append(av)
        print(f"   {str(te_m[0]):<12}{best:<32}{ch:>+12.3f}{av:>+15.3f}{len(elig):>11}")
    if diffs:
        print(f"\n   folds where the chosen rule beat the average candidate: "
              f"{sum(d > 0 for d in diffs)}/{len(diffs)}")
        print(f"   mean edge of SELECTION over a random eligible candidate: "
              f"{np.mean(diffs):>+.3f}% per fold")
        print(f"   chosen mean {np.mean(chosen_all):+.3f}%   "
              f"average-candidate mean {np.mean(avg_all):+.3f}%")


def standalone(d: pd.DataFrame, defs: dict, h: int, lo, hi) -> None:
    w = d[(d["dt"] >= lo) & (d["dt"] <= hi)]
    print(f"   {'rule':<34}{'n OOS':>7}{'mean %':>9}{'win':>6}{'t':>7}"
          f"{'sum %':>9}{'n full':>8}{'full mean':>11}")
    for name, (side, mask) in defs.items():
        mw = mask.reindex(w.index).fillna(False)
        r = (side * w.loc[mw, f"cc{h}"]).dropna()
        rf = (side * d.loc[mask, f"cc{h}"]).dropna()
        if len(r) < 3:
            print(f"   {name:<34}{len(r):>7}")
            continue
        print(f"   {name:<34}{len(r):>7}{r.mean():>+9.3f}{(r > 0).mean()*100:>5.0f}%"
              f"{plain_t(r):>+7.2f}{r.sum():>+9.1f}{len(rf):>8}{rf.mean():>+11.3f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=dsn())
    ap.add_argument("--symbol", default="BANKNIFTY")
    args = ap.parse_args()

    conn = psycopg2.connect(args.dsn)
    try:
        d = load_mtf(conn, [args.symbol], date(2021, 1, 1))
    finally:
        conn.close()
    d["dt"] = pd.to_datetime(d["dt"])
    d = targets(d, horizons=HORIZONS)
    d = build_state(d)
    d = d[d["m_val"].notna() & d["w_val"].notna()].reset_index(drop=True)
    defs = rule_defs(d)
    sess_idx = {pd.Timestamp(t).normalize(): i for i, t in enumerate(d["dt"])}
    s = stack(d, H)
    cand = candidates(s, defs)

    base = walk_forward(s, cand, "ret", train_m=18, test_m=6, anchored=True)

    print("=" * 104)
    print(f"1. WHERE THE OUT-OF-SAMPLE P&L ACTUALLY SITS  (h={H}, anchored 18/6)")
    print("=" * 104)
    by_year(base, H)

    print("\n" + "=" * 104)
    print("2. DID THE SELECTION STEP ADD ANYTHING? chosen rule vs average eligible")
    print("=" * 104)
    average_candidate(s, cand, 18, 6, 12)

    lo = pd.to_datetime(base["oos"]["dt"]).min().normalize()
    hi = pd.to_datetime(base["oos"]["dt"]).max().normalize()
    print("\n" + "=" * 104)
    print(f"3. EVERY RULE STANDALONE, NO SELECTION, over the OOS window "
          f"{lo.date()}..{hi.date()}")
    print("=" * 104)
    standalone(d, defs, H, lo, hi)
    allr = d[(d["dt"] >= lo) & (d["dt"] <= hi)][f"cc{H}"].dropna()
    print(f"   {'-- every session long (control)':<34}{len(allr):>7}{allr.mean():>+9.3f}"
          f"{(allr > 0).mean()*100:>5.0f}%{plain_t(allr):>+7.2f}{allr.sum():>+9.1f}")

    print("\n" + "=" * 104)
    print("4. PROTOCOL SENSITIVITY -- same 24 rules, different walk-forward settings")
    print("=" * 104)
    print(HEADER)
    grid = [("anchored 18/6  min12", dict(train_m=18, test_m=6, anchored=True), 12),
            ("anchored 18/6  min25", dict(train_m=18, test_m=6, anchored=True), 25),
            ("anchored 12/6  min12", dict(train_m=12, test_m=6, anchored=True), 12),
            ("anchored 24/6  min12", dict(train_m=24, test_m=6, anchored=True), 12),
            ("anchored 24/12 min12", dict(train_m=24, test_m=12, anchored=True), 12),
            ("rolling  18/6  min12", dict(train_m=18, test_m=6, anchored=False), 12),
            ("rolling  24/6  min12", dict(train_m=24, test_m=6, anchored=False), 12),
            ("rolling  24/6  min25", dict(train_m=24, test_m=6, anchored=False), 25)]
    for label, kw, mt in grid:
        r = walk_forward(s, cand, "ret", min_trades=mt, **kw)
        report(label, r)
        if "error" not in r:
            nov = non_overlapping(r["oos"], sess_idx, H, "ret")
            print(f"      -> net of cost {r['mean']-COST_PCT:+.3f}%   "
                  f"NW t(lag {H-1}) {nw_t(r['oos']['ret'].values, H-1):+.2f}   "
                  f"non-overlapping n {len(nov)} mean {nov.mean():+.3f}% "
                  f"t {plain_t(nov):+.2f}")

    print("\n" + "=" * 104)
    print("5. THE INCUMBENT ALONE -- L01 below day+week+month value, no selection")
    print("=" * 104)
    m = defs["L01 below day+week+month VA"][1]
    for h in HORIZONS:
        r = d.loc[m, f"cc{h}"].dropna()
        rw = d[(d["dt"] >= lo) & (d["dt"] <= hi)]
        rr = rw.loc[m.reindex(rw.index).fillna(False), f"cc{h}"].dropna()
        print(f"   h={h}  full sample n {len(r):>3} mean {r.mean():>+7.3f}% "
              f"t {plain_t(r):>+5.2f}   |   OOS window n {len(rr):>3} "
              f"mean {rr.mean():>+7.3f}% t {plain_t(rr):>+5.2f}")
    print("   (up-move probability, not signed return, is the form this signal was")
    print("    originally established in; the signed test is the harder one.)")


if __name__ == "__main__":
    main()
