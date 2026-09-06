"""Two things the per-symbol run surfaced, tested properly.

  1. THE SIGNIFICANCE. The pooled per-trade mean is +8.5bp on 1,567 trades
     (t +3.44), but those trades are not independent -- they cluster into 252
     sessions of banks that move together. The earlier module printed a
     session-MEAN of -1.3bp beside it and called that the honest version, which
     confused two different things: the session-mean answers "what does one
     basket earn", while the per-symbol book structure genuinely earns the
     TRADE-weighted mean. The right test keeps the trade-weighted mean and
     computes a CLUSTER-ROBUST standard error by session.

  2. THE ORDERING IS NOT RANDOM. The six PSU banks -- SBIN, UNIONBANK, CANBK,
     BANKBARODA, BANKINDIA, PNB -- occupy the top six places of sixteen. If book
     quality were random across names, one particular set of six taking the top
     six has probability 1/C(16,6) = 1/8008. That is worth naming and testing
     rather than reading off a sorted table, which is exactly how spurious
     groupings get discovered.

     THE HONEST CAVEAT: the group was identified BY LOOKING at this sorted
     table, so its p-value is not a clean out-of-sample test. What can be done is
     to state the prior mechanism (PSU banks are higher-beta, less liquid and
     more gap-prone than the large private banks), check it holds in BOTH halves,
     and check it is not simply a volatility effect.

    python vanguard/research/mp_banks_psu.py
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from math import comb

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.banknifty_rotation import BANKS  # noqa: E402
from research.mp_auction import dsn, load  # noqa: E402

RANK_WINDOW, MIN_PERIODS = 120, 60
PSU = {"SBIN", "UNIONBANK", "CANBK", "BANKBARODA", "BANKINDIA", "PNB"}


def cluster_t(df: pd.DataFrame, col: str, by: str) -> tuple[float, float, int]:
    """Trade-weighted mean with a standard error clustered on `by`."""
    d = df[[col, by]].dropna()
    n = len(d)
    if n < 30:
        return np.nan, np.nan, n
    mu = d[col].mean()
    # cluster-robust variance of the mean: sum of squared cluster sums / n^2
    g = (d[col] - mu).groupby(d[by]).sum()
    se = np.sqrt((g ** 2).sum()) / n
    return mu, (mu / se if se > 0 else np.nan), n


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=float, default=3.0)
    parser.add_argument("--dsn", default=dsn())
    args = parser.parse_args()

    connection = psycopg2.connect(args.dsn)
    try:
        s = load(connection, list(BANKS),
                 date.today() - timedelta(days=int(args.years * 365.25)))
    finally:
        connection.close()
    s = s.sort_values(["underlying", "dt"]).reset_index(drop=True)
    s["cp_rank"] = (s.groupby("underlying")["close_pos"]
                    .transform(lambda x: x.rolling(RANK_WINDOW, min_periods=MIN_PERIODS)
                               .apply(lambda w: (w[-1] > w[:-1]).mean(), raw=True)))
    s = s.dropna(subset=["cp_rank", "next_open_ret"])
    s["gap"] = s["next_open_ret"]
    tr = s[s["cp_rank"] >= 2 / 3].copy()
    tr["psu"] = tr["underlying"].isin(PSU)
    span = (s["dt"].max() - s["dt"].min()).days / 365.25

    print(f"{len(tr):,} trades, {tr['dt'].nunique()} sessions, "
          f"{tr['underlying'].nunique()} banks, {span:.2f} years")

    print(f"\n1. THE PER-TRADE EDGE, WITH THE RIGHT STANDARD ERROR")
    print(f"   {'cohort':<26}{'trades':>8}{'bp/trade':>10}{'naive t':>9}"
          f"{'clustered t':>13}{'sessions':>10}")
    for label, d in (("all 16 banks", tr), ("PSU banks (6)", tr[tr["psu"]]),
                     ("private banks (10)", tr[~tr["psu"]])):
        mu, ct, n = cluster_t(d, "gap", "dt")
        sd = d["gap"].std(ddof=1)
        nt = mu / (sd / np.sqrt(n)) if sd > 0 else np.nan
        print(f"   {label:<26}{n:>8,}{mu * 1e4:>+10.1f}{nt:>+9.2f}{ct:>+13.2f}"
              f"{d['dt'].nunique():>10}")
    print("   Clustering by session is what corrects for banks moving together. It\n"
          "   keeps the trade-weighted mean and only widens the error bar.")

    print(f"\n2. PSU vs PRIVATE — is the split real?")
    a, b = tr[tr["psu"]]["gap"], tr[~tr["psu"]]["gap"]
    diff = a.mean() - b.mean()
    # difference of two clustered means: cluster the differenced series by date
    daily = tr.groupby(["dt", "psu"])["gap"].mean().unstack()
    daily = daily.dropna()
    dd = daily[True] - daily[False]
    dt_ = dd.mean() / (dd.std(ddof=1) / np.sqrt(len(dd)))
    print(f"   PSU {a.mean() * 1e4:+.1f}bp vs private {b.mean() * 1e4:+.1f}bp   "
          f"diff {diff * 1e4:+.1f}bp")
    print(f"   PAIRED BY SESSION (both groups traded the same night): "
          f"{dd.mean() * 1e4:+.1f}bp   t={dt_:+.2f}   n={len(dd)} sessions")
    print(f"   P(one particular set of 6 takes the top 6 of 16 by chance) = "
          f"1/{comb(16, 6):,} = {1 / comb(16, 6):.5f}")
    print("   -- but the group was read off a SORTED table, so treat that as a\n"
          "   description of how striking the ordering is, not as a clean p-value.")

    print(f"\n3. IS IT JUST VOLATILITY? (PSU banks are higher-beta)")
    print(f"   {'cohort':<26}{'bp/trade':>10}{'ATR%':>9}{'bp per ATR%':>13}{'win':>7}")
    for label, d in (("PSU banks", tr[tr["psu"]]), ("private banks", tr[~tr["psu"]])):
        atr = d["atr20"].mean() * 100
        print(f"   {label:<26}{d['gap'].mean() * 1e4:>+10.1f}{atr:>9.2f}"
              f"{d['gap'].mean() * 1e4 / atr:>+13.1f}"
              f"{(d['gap'] > 0).mean() * 100:>6.0f}%")
    print("   If bp-per-ATR is also higher for PSU, the split is not merely that they\n"
          "   are more volatile — volatility is available for free by sizing up.")

    print(f"\n4. BOTH HALVES, AND COSTS")
    h = tr["dt"].nunique() // 2
    cut = sorted(tr["dt"].unique())[h]
    print(f"   {'cohort':<26}{'1st half':>11}{'2nd half':>11}{'@5bp':>9}{'@10bp':>9}")
    for label, d in (("PSU banks", tr[tr["psu"]]), ("private banks", tr[~tr["psu"]]),
                     ("all 16", tr)):
        a1 = d[d["dt"] < cut]["gap"].mean() * 1e4
        a2 = d[d["dt"] >= cut]["gap"].mean() * 1e4
        print(f"   {label:<26}{a1:>+11.1f}{a2:>+11.1f}"
              f"{d['gap'].mean() * 1e4 - 5:>+9.1f}{d['gap'].mean() * 1e4 - 10:>+9.1f}")

    print(f"\n5. A PSU-ONLY BOOK SET (6 books), compounded")
    print(f"   {'cost':<12}{'avg book total%':>18}{'CAGR%':>9}{'books +':>10}")
    for bps in (0, 5, 10, 20):
        tots = []
        for name, g in tr[tr["psu"]].groupby("underlying"):
            tots.append((1 + g["gap"] - bps / 1e4).prod() - 1.0)
        m = float(np.mean(tots))
        print(f"   {bps:>2}bp{'':<8}{m * 100:>+18.2f}"
              f"{((1 + m) ** (1 / span) - 1) * 100:>+9.2f}"
              f"{sum(1 for x in tots if x > 0):>7}/6")
    return 0


if __name__ == "__main__":
    sys.exit(main())
