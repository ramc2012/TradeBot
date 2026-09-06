"""Validate the two candidate refinements against the RIGHT null.

The search in mp_strong_refine.py reported each filter's t against ZERO. That is
the wrong null. The base strong-close night already earns +72.8 points, so the
question is never "is this positive" -- it is "is this BETTER THAN THE NIGHTS IT
EXCLUDES". Those are different tests and only the second one justifies a filter.

The two candidates:
    TREND DAY     strong close on a day whose range exceeded 2x its IB with a
                  one-sided extension -- Dalton's trend day, and the strongest
                  unfinished-auction read there is. +142.3 pts/night, both halves
                  strong, but n=52.
    FRIDAY        the weekend hold. +110.2 pts/night. Mechanism is plausible on
                  its face: three calendar days of news accumulate into one gap.

Both had t near +2.4-2.6 against zero, and 26 conditions were tried, so the
expected largest |t| among pure nulls is around 2.5. Neither clears that bar on
its own. A Welch difference test against the excluded nights, plus a permutation
test that respects how many filters were searched, is what decides it.

Note the redundancy: a trend day is DEFINED partly by closing near its extreme,
which is most of what close_pos already measures. The non-redundant part is the
range extension, so range_over_ib is tested separately as the cleaner statement
of the same idea.

    python vanguard/research/mp_strong_validate.py
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.mp_auction import dsn  # noqa: E402
from research.mp_strong_refine import build  # noqa: E402

N_SEARCHED = 26
N_PERM = 4000


def welch(a: pd.Series, b: pd.Series) -> tuple[float, float]:
    a, b = a.dropna(), b.dropna()
    diff = a.mean() - b.mean()
    se = np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
    return diff, diff / se if se > 0 else np.nan


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BANKNIFTY")
    parser.add_argument("--years", type=float, default=5.2)
    parser.add_argument("--dsn", default=dsn())
    args = parser.parse_args()

    d = build(args.symbol, args.years, args.dsn)
    strong = d["cp_rank"] >= 2 / 3
    b = d[strong].reset_index(drop=True)
    print(f"{args.symbol}  base: {len(b)} strong-close nights, "
          f"{b['gap_pts'].mean():+.1f} pts/night, {b['gap_pts'].sum():+,.0f} total")

    CANDS = {
        "trend day": b["day_type"] == "trend",
        "range/IB >= 2": b["range_over_ib"] >= 2.0,
        "Friday": b["dow"] == 4,
        "wide IB": b["ib_pct_rank"] >= 0.67,
        "value overlapping": b["value_shift"] == "overlapping",
    }

    print(f"\n1. IN vs OUT — is the filter better than the nights it DROPS?")
    print(f"   {'filter':<22}{'in n':>6}{'in pts':>9}{'out n':>7}{'out pts':>9}"
          f"{'diff':>9}{'Welch t':>9}{'perm p':>9}")
    rng = np.random.default_rng(7)
    for name, m in CANDS.items():
        a, c = b.loc[m, "gap_pts"], b.loc[~m, "gap_pts"]
        if len(a) < 30:
            continue
        diff, t = welch(a, c)
        # permutation: shuffle the labels, keep the group size, and ask how
        # often a random split of the SAME size beats this difference
        vals, k = b["gap_pts"].values, int(m.sum())
        null = np.array([rng.permutation(vals)[:k].mean() for _ in range(N_PERM)])
        p = float((null >= a.mean()).mean())
        print(f"   {name:<22}{len(a):>6}{a.mean():>+9.1f}{len(c):>7}{c.mean():>+9.1f}"
              f"{diff:>+9.1f}{t:>+9.2f}{p:>9.4f}")
    print(f"   perm p is the chance a RANDOM subset of the same size does this well.\n"
          f"   With {N_SEARCHED} filters searched, treat p < {0.05 / N_SEARCHED:.4f} "
          f"(Bonferroni) as the bar,\n   and anything between that and 0.05 as suggestive only.")

    print(f"\n2. IS 'TREND DAY' JUST close_pos AGAIN?")
    print(f"   A trend day requires a close near the extreme, which close_pos already\n"
          f"   measures. The separable part is the RANGE EXTENSION.")
    print(f"   {'cohort':<38}{'n':>6}{'pts/nt':>9}{'median':>9}{'win':>7}")
    for label, m in (("strong + range/IB >= 2", b["range_over_ib"] >= 2.0),
                     ("strong + range/IB 1.5-2", (b["range_over_ib"] >= 1.5)
                      & (b["range_over_ib"] < 2.0)),
                     ("strong + range/IB < 1.5", b["range_over_ib"] < 1.5)):
        g = b[m]
        print(f"   {label:<38}{len(g):>6}{g['gap_pts'].mean():>+9.1f}"
              f"{g['gap_pts'].median():>+9.1f}{(g['gap_pts'] > 0).mean() * 100:>6.0f}%")
    rho = b[["range_over_ib", "gap_pts"]].dropna().corr(method="spearman").iloc[0, 1]
    print(f"   rank corr(range/IB, gap) within strong closes: {rho:+.3f}"
          f"   corr(close_pos, range/IB): "
          f"{b[['close_pos', 'range_over_ib']].corr(method='spearman').iloc[0, 1]:+.3f}")

    print(f"\n3. FRIDAY: is it the weekend, or is it Friday?")
    print(f"   {'weekday':<22}{'n':>6}{'pts/nt':>9}{'median':>9}{'win':>7}{'cal days':>10}")
    names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri"}
    for k, g in b.groupby("dow"):
        if k not in names:
            continue
        days = (g["dt"].shift(-1) - g["dt"]).dt.days.median()
        print(f"   {names[k]:<22}{len(g):>6}{g['gap_pts'].mean():>+9.1f}"
              f"{g['gap_pts'].median():>+9.1f}{(g['gap_pts'] > 0).mean() * 100:>6.0f}%"
              f"{'3 (wknd)' if k == 4 else '1':>10}")
    print("   If the whole effect is Friday and the other four are flat, that is a\n"
          "   calendar artefact on ~80 observations. If Monday-Thursday are broadly\n"
          "   similar and Friday is simply larger, the weekend-news story is coherent.")

    # ── 4. what a book actually earns: pts/night vs TOTAL ──────────────────
    print(f"\n4. TOTAL POINTS, which is what a book is paid")
    print(f"   {'rule':<38}{'nights':>7}{'pts/nt':>9}{'total':>10}{'% of base':>11}")
    base_total = b["gap_pts"].sum()
    RULES = {
        "cp_rank >= 0.50 (loosest)": d["cp_rank"] >= 0.50,
        "cp_rank >= 0.67 (BASE)": strong,
        "cp_rank >= 0.75": d["cp_rank"] >= 0.75,
        "strong + range/IB >= 2": strong & (d["range_over_ib"] >= 2.0),
        "strong + Friday": strong & (d["dow"] == 4),
        "strong + (range/IB>=2 OR Friday)": strong & ((d["range_over_ib"] >= 2.0)
                                                      | (d["dow"] == 4)),
        "cp>=0.50 + (range/IB>=2 OR Fri)": (d["cp_rank"] >= 0.50)
                                           & ((d["range_over_ib"] >= 2.0) | (d["dow"] == 4)),
    }
    for name, m in RULES.items():
        g = d[m]
        print(f"   {name:<38}{len(g):>7}{g['gap_pts'].mean():>+9.1f}"
              f"{g['gap_pts'].sum():>+10.0f}{g['gap_pts'].sum() / base_total * 100:>10.0f}%")
    print("   A filter that doubles pts/night but keeps an eighth of the nights earns\n"
          "   a quarter of the money. Efficiency per night and total return pull in\n"
          "   opposite directions; which one matters depends on whether capital or\n"
          "   opportunity is the binding constraint.")

    print(f"\n5. YEAR BY YEAR for the leading rule (strong + range/IB >= 2)")
    print(f"   {'year':<8}{'nights':>8}{'pts/nt':>10}{'total':>10}{'win':>7}")
    lead = d[strong & (d["range_over_ib"] >= 2.0)]
    for y, g in lead.groupby(lead["dt"].dt.year):
        print(f"   {y:<8}{len(g):>8}{g['gap_pts'].mean():>+10.1f}"
              f"{g['gap_pts'].sum():>+10.0f}{(g['gap_pts'] > 0).mean() * 100:>6.0f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
