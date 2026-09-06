"""The overnight edge in INDEX POINTS, and whether the strong close predicts the tails.

TWO QUESTIONS FROM THE OWNER, both sharp:

  1. "earlier u told strong close of 260 sessions. now 409 sessions."
     They are different samples and the earlier report did not say so clearly.
     409 = strong-close nights in the FIVE-YEAR SPOT sample (of 1,190 sessions).
     260 = sessions where an ATM OPTION CONTRACT could be resolved at all, which
     is limited by option coverage starting 2025-03, and of those only ~85 were
     strong-close. The option test therefore ran on a fifth of the spot sample.
     This module prints the reconciliation instead of leaving it implicit.

  2. "If those fat tails were predicted by strong close no issues."
     Exactly the right test. Concentration in five nights is only a defect if the
     strategy was in those nights BY LUCK. If a strong close genuinely raises the
     odds of a large UP gap, the concentration is the edge working, not a fragile
     accident. So: of the largest up-gaps in five years, what share landed on
     strong-close nights, against the 34% base rate of being in at all? And the
     mirror -- did strong closes AVOID the large down-gaps?

POINTS, NOT PERCENT. A trader carries lots, and a lot pays index points. The
index ran from roughly 35,000 to 57,000 over the window, so the same percentage
is worth far more points in 2026 than in 2021; summing points is what a fixed
one-lot position actually earns, and is reported alongside the compounded percent
rather than instead of it. Lot size is deliberately NOT assumed -- multiply by
whatever the lot is for the period you care about.

    python vanguard/research/mp_overnight_points.py
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.mp_auction import dsn, load  # noqa: E402

RANK_WINDOW = 120
TOP_NS = (5, 10, 20, 30, 50)


def binom_p(k: int, n: int, p: float) -> float:
    """One-sided P(X >= k) for X ~ Binomial(n, p), exact."""
    from math import comb
    return float(sum(comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BANKNIFTY")
    parser.add_argument("--years", type=float, default=5.2)
    parser.add_argument("--cost-pts", type=float, default=0.0,
                        help="round-trip cost in index points")
    parser.add_argument("--dsn", default=dsn())
    args = parser.parse_args()

    start = date.today() - timedelta(days=int(args.years * 365.25))
    connection = psycopg2.connect(args.dsn)
    try:
        s = load(connection, [args.symbol], start)
    finally:
        connection.close()
    s = s.sort_values("dt").reset_index(drop=True)
    s["cp_rank"] = (s["close_pos"].rolling(RANK_WINDOW, min_periods=60)
                    .apply(lambda w: (w[-1] > w[:-1]).mean(), raw=True))
    d = s.dropna(subset=["cp_rank", "next_open_ret"]).reset_index(drop=True)
    # THE POINT MOVE: next session's open minus this session's close
    d["gap_pts"] = d["close"] * d["next_open_ret"]
    d["strong"] = d["cp_rank"] >= 2 / 3
    d["weak"] = d["cp_rank"] <= 1 / 3
    base = d["strong"].mean()

    print(f"{args.symbol}  {len(d):,} sessions  {d['dt'].min().date()} .. "
          f"{d['dt'].max().date()}   index {d['close'].iloc[0]:,.0f} -> "
          f"{d['close'].iloc[-1]:,.0f}")

    # ── 1. RECONCILE THE TWO SAMPLE SIZES ───────────────────────────────────
    print("\n1. WHY 409 AND 260 ARE DIFFERENT NUMBERS")
    print(f"   spot sessions with a trailing rank      {len(d):>6,}")
    print(f"   ... of which STRONG close (top tertile) {int(d['strong'].sum()):>6,}"
          f"   <- the 409 in the equity curve")
    opt = d[d["dt"] >= "2025-03-24"]
    print(f"   sessions from 2025-03-24 (option data)  {len(opt):>6,}"
          f"   <- the 260 in the option test")
    print(f"   ... of which STRONG close               {int(opt['strong'].sum()):>6,}"
          f"   <- the 85 CE trades")
    print("   The option test ran on the last 18 months only, because "
          "option_premium_candles\n   starts 2025-01 and carries a usable ATM chain "
          "from 2025-03. Same signal,\n   a fifth of the sample -- they were never the "
          "same population.")

    # ── 2. THE EDGE IN POINTS ───────────────────────────────────────────────
    print("\n2. THE EDGE IN INDEX POINTS (one lot = one unit of this; multiply by lot size)")
    print(f"   {'cohort':<26}{'n':>6}{'mean':>9}{'median':>9}{'sum':>10}"
          f"{'best':>9}{'worst':>9}{'win':>7}")
    for label, m in (("STRONG close", d["strong"]), ("mid", ~d["strong"] & ~d["weak"]),
                     ("WEAK close", d["weak"]), ("ALL sessions", pd.Series(True, index=d.index))):
        g = d.loc[m, "gap_pts"].dropna() - args.cost_pts
        print(f"   {label:<26}{len(g):>6}{g.mean():>+9.1f}{g.median():>+9.1f}"
              f"{g.sum():>+10.0f}{g.max():>+9.0f}{g.min():>+9.0f}"
              f"{(g > 0).mean() * 100:>6.0f}%")
    st = d.loc[d["strong"], "gap_pts"].dropna()
    print(f"   strong-minus-weak per night: "
          f"{st.mean() - d.loc[d['weak'], 'gap_pts'].mean():+.1f} points")
    print(f"   cumulative points, strong-close long only: {st.sum():+,.0f} points "
          f"over {len(st)} nights")

    print(f"\n   points per year, strong-close long (index level rises, so later "
          f"years are worth more points for the same %)")
    print(f"   {'year':<8}{'nights':>8}{'points':>10}{'pts/night':>11}"
          f"{'avg index':>11}{'win':>7}")
    for y, g in d[d["strong"]].groupby(d["dt"].dt.year):
        gp = g["gap_pts"] - args.cost_pts
        print(f"   {y:<8}{len(g):>8}{gp.sum():>+10.0f}{gp.mean():>+11.1f}"
              f"{g['close'].mean():>11,.0f}{(gp > 0).mean() * 100:>6.0f}%")

    # ── 3. DID THE STRONG CLOSE PREDICT THE FAT TAILS? ──────────────────────
    print(f"\n3. DID THE STRONG CLOSE PREDICT THE BIG GAPS?"
          f"   (base rate of being in = {base * 100:.0f}%)")
    print(f"   {'cohort':<28}{'strong':>8}{'expected':>10}{'share':>8}"
          f"{'P(>=k)':>10}{'points':>10}")
    up = d.nlargest(max(TOP_NS), "gap_pts")
    dn = d.nsmallest(max(TOP_NS), "gap_pts")
    for n in TOP_NS:
        g = up.head(n)
        k = int(g["strong"].sum())
        print(f"   top {n:<3} UP gaps{'':<12}{k:>8}{n * base:>10.1f}"
              f"{k / n * 100:>7.0f}%{binom_p(k, n, base):>10.4f}"
              f"{g.loc[g['strong'], 'gap_pts'].sum():>+10.0f}")
    print()
    for n in TOP_NS:
        g = dn.head(n)
        k = int(g["strong"].sum())
        # for DOWN gaps a GOOD signal is UNDER-represented, so report P(X <= k)
        p_le = 1.0 - binom_p(k + 1, n, base) if k < n else 1.0
        print(f"   top {n:<3} DOWN gaps{'':<10}{k:>8}{n * base:>10.1f}"
              f"{k / n * 100:>7.0f}%{p_le:>10.4f}"
              f"{g.loc[g['strong'], 'gap_pts'].sum():>+10.0f}")
    print("   For UP gaps the test is over-representation, P(X>=k); for DOWN gaps a\n"
          "   useful signal should be UNDER-represented, so P(X<=k) is shown instead.")

    # the specific five nights that carry the curve
    print(f"\n   THE FIVE NIGHTS THAT CARRY THE CURVE — were they signalled?")
    print(f"   {'date':<13}{'gap pts':>10}{'gap %':>9}{'close_pos':>11}"
          f"{'rank':>8}{'in trade?':>11}")
    five = d[d["strong"]].nlargest(5, "gap_pts")
    for r in five.itertuples():
        print(f"   {r.dt.date()!s:<13}{r.gap_pts:>+10.0f}"
              f"{r.next_open_ret * 100:>+8.2f}%{r.close_pos:>11.2f}"
              f"{r.cp_rank:>8.2f}{'yes':>11}")
    print(f"   biggest up-gaps the signal MISSED (weak or mid close):")
    print(f"   {'date':<13}{'gap pts':>10}{'gap %':>9}{'close_pos':>11}{'rank':>8}")
    for r in d[~d["strong"]].nlargest(5, "gap_pts").itertuples():
        print(f"   {r.dt.date()!s:<13}{r.gap_pts:>+10.0f}"
              f"{r.next_open_ret * 100:>+8.2f}%{r.close_pos:>11.2f}{r.cp_rank:>8.2f}")

    # ── 4. IS THE EDGE ONLY IN THE TAIL? ────────────────────────────────────
    print(f"\n4. WHERE IN THE DISTRIBUTION THE EDGE LIVES")
    print(f"   {'gap decile (all sessions)':<28}{'n':>6}{'strong%':>10}"
          f"{'mean pts':>11}")
    d["dec"] = pd.qcut(d["gap_pts"], 10, labels=False, duplicates="drop")
    for q, g in d.groupby("dec"):
        print(f"   D{int(q) + 1:<2} {'(most negative)' if q == 0 else '(most positive)' if q == 9 else '':<23}"
              f"{len(g):>6}{g['strong'].mean() * 100:>9.0f}%{g['gap_pts'].mean():>+11.0f}")
    print(f"\n   trimmed means, strong-close nights (points per night)")
    for trim in (0, 1, 2, 5, 10):
        g = st.sort_values()
        gg = g.iloc[trim:len(g) - trim] if trim else g
        print(f"   drop {trim:>2} each tail   n={len(gg):>4}   "
              f"mean {gg.mean():>+6.1f} pts   median {gg.median():>+6.1f} pts   "
              f"sum {gg.sum():>+7.0f} pts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
