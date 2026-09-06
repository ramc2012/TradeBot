"""Equity curves for four named banks, zero cost, with the exact data span stated.

The four were chosen by the owner and they span the range deliberately: SBIN was
the best book of sixteen, AUBANK middling-positive, ICICIBANK barely positive,
FEDERALBNK negative. So this is not a highlight reel.

COSTS ARE OMITTED, as instructed -- they will be settled in paper testing. Every
figure below is therefore an upper bound, and the earlier finding stands in the
background: the whole sixteen-book set turns negative at 10bp while the PSU six
survive it.

ON THE SPAN. The raw 30-minute history for these names starts 2025-03-28. The
base rule ranks each close against the name's own trailing 120 sessions and will
not emit a signal until 60 have accumulated, so the first tradeable night is late
June 2025 and the usable window is SHORTER than the raw one. Both are printed,
because "18 months of data" and "14 months of trades" are different claims and
only the second one bounds what can be concluded.

    python vanguard/research/mp_four_books.py
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

FOUR = ["SBIN", "AUBANK", "FEDERALBNK", "ICICIBANK"]
RANK_WINDOW, MIN_PERIODS = 120, 60


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=float, default=3.0)
    parser.add_argument("--dsn", default=dsn())
    args = parser.parse_args()

    connection = psycopg2.connect(args.dsn)
    try:
        s = load(connection, FOUR,
                 date.today() - timedelta(days=int(args.years * 365.25)))
    finally:
        connection.close()
    s = s.sort_values(["underlying", "dt"]).reset_index(drop=True)

    print("DATA SPAN — raw history vs tradeable history")
    print(f"   {'name':<13}{'raw first':>12}{'raw last':>12}{'raw sess':>10}"
          f"{'1st signal':>13}{'usable sess':>13}")
    s["cp_rank"] = (s.groupby("underlying")["close_pos"]
                    .transform(lambda x: x.rolling(RANK_WINDOW, min_periods=MIN_PERIODS)
                               .apply(lambda w: (w[-1] > w[:-1]).mean(), raw=True)))
    for name, g in s.groupby("underlying"):
        u = g.dropna(subset=["cp_rank", "next_open_ret"])
        print(f"   {name:<13}{g['dt'].min().date()!s:>12}{g['dt'].max().date()!s:>12}"
              f"{len(g):>10}{u['dt'].min().date()!s:>13}{len(u):>13}")
    raw_m = ((s["dt"].max() - s["dt"].min()).days / 30.44)
    u_all = s.dropna(subset=["cp_rank", "next_open_ret"])
    use_m = ((u_all["dt"].max() - u_all["dt"].min()).days / 30.44)
    print(f"   RAW span {raw_m:.1f} months; TRADEABLE span {use_m:.1f} months "
          f"({u_all['dt'].nunique()} sessions).")
    print(f"   The 60-session minimum for the ranking window consumes the first "
          f"{raw_m - use_m:.1f} months.")

    tr = u_all[u_all["cp_rank"] >= 2 / 3].copy()
    span = (u_all["dt"].max() - u_all["dt"].min()).days / 365.25

    print(f"\nEACH BOOK, ZERO COST (long overnight, 15:15 -> next 09:15)")
    print(f"   {'name':<13}{'trades':>8}{'bp/nt':>8}{'median':>8}{'win':>6}"
          f"{'total%':>9}{'CAGR%':>8}{'maxDD%':>9}{'Sharpe':>8}{'t':>7}"
          f"{'best':>8}{'worst':>8}")
    curves = {}
    for name in FOUR:
        g = tr[tr["underlying"] == name]
        r = g["next_open_ret"].dropna()
        if len(r) < 20:
            print(f"   {name:<13}{len(r):>8}   (too few)")
            continue
        eq = (1 + r).cumprod()
        sd = r.std(ddof=1)
        curves[name] = pd.DataFrame({"dt": g.loc[r.index, "dt"].values,
                                     "eq": eq.values, "r": r.values})
        print(f"   {name:<13}{len(r):>8}{r.mean() * 1e4:>+8.1f}{r.median() * 1e4:>+8.1f}"
              f"{(r > 0).mean() * 100:>5.0f}%{(eq.iloc[-1] - 1) * 100:>+9.1f}"
              f"{(eq.iloc[-1] ** (1 / span) - 1) * 100:>+8.1f}"
              f"{(eq / eq.cummax() - 1).min() * 100:>+9.1f}"
              f"{r.mean() / sd * np.sqrt(len(r) / span):>+8.2f}"
              f"{r.mean() / (sd / np.sqrt(len(r))):>+7.2f}"
              f"{r.max() * 100:>+8.2f}{r.min() * 100:>+8.2f}")

    # buy and hold each name over the same window, for reference
    print(f"\n   for reference, buy & hold over the same tradeable window (24h exposure)")
    print(f"   {'name':<13}{'total%':>9}{'CAGR%':>8}{'maxDD%':>9}")
    for name in FOUR:
        g = u_all[u_all["underlying"] == name].sort_values("dt")
        if len(g) < 20:
            continue
        px = g["close"] / g["close"].iloc[0]
        print(f"   {name:<13}{(px.iloc[-1] - 1) * 100:>+9.1f}"
              f"{(px.iloc[-1] ** (1 / span) - 1) * 100:>+8.1f}"
              f"{(px / px.cummax() - 1).min() * 100:>+9.1f}")

    print(f"\nMONTH BY MONTH (return %, zero cost; '.' = no trades that month)")
    months = sorted(tr["dt"].dt.to_period("M").unique())
    print(f"   {'name':<13}" + "".join(f"{str(m)[2:]:>8}" for m in months))
    for name in FOUR:
        g = tr[tr["underlying"] == name]
        cells = ""
        for m in months:
            mm = g[g["dt"].dt.to_period("M") == m]["next_open_ret"].dropna()
            cells += f"{'.':>8}" if len(mm) == 0 else f"{((1 + mm).prod() - 1) * 100:>+8.1f}"
        print(f"   {name:<13}{cells}")
    print(f"   {'trades':<13}" + "".join(
        f"{len(tr[tr['dt'].dt.to_period('M') == m]) // 4:>8}" for m in months)
        + "  (avg per name)")

    print(f"\nSPLIT-HALF and CONCENTRATION (zero cost)")
    print(f"   {'name':<13}{'1st half':>11}{'2nd half':>11}{'drop 2 best':>13}"
          f"{'median bp':>12}")
    for name in FOUR:
        if name not in curves:
            continue
        r = curves[name]["r"]
        h = len(r) // 2
        d2 = r.drop(r.nlargest(2).index)
        print(f"   {name:<13}{r.iloc[:h].mean() * 1e4:>+11.1f}"
              f"{r.iloc[h:].mean() * 1e4:>+11.1f}{d2.mean() * 1e4:>+13.1f}"
              f"{r.median() * 1e4:>+12.1f}")

    out = os.environ.get("FOUR_OUT")
    if out:
        recs = []
        for name, c in curves.items():
            recs.append({"name": name, "n": len(c),
                         "dts": ",".join(str(x)[:10] for x in c["dt"]),
                         "eq": ",".join(f"{v:.4f}" for v in c["eq"])})
        pd.DataFrame(recs).to_csv(out, index=False)
        print(f"\ncurves written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
