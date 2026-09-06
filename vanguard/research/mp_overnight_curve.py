"""Does the strong-close overnight edge ACCUMULATE, or is it only a mean?

THE CRITICISM (owner): the +0.189% strong-minus-weak figure is an AVERAGE of
per-session gaps. An average says nothing about whether capital actually grows.
A mean that size sitting on fat tails can compound to nothing, or to less than
nothing -- the arithmetic mean always exceeds the geometric one, and the gap is
volatility drag, which is exactly what a gap-return series has plenty of.

So this compounds the series sequentially and looks at the path, not the moment:

    strategy      hold BANKNIFTY overnight ONLY -- in at the 15:15 close, out at
                  the 09:15 open -- on sessions whose close_pos is in the top
                  tertile of its own trailing history. No intraday exposure.
    accumulation  cumulative PRODUCT of (1 + gap), in sequence, so drag and
                  ordering are both included.
    benchmarks    every-night, weak-close-short, the long/short combination,
                  and buy-and-hold, on the identical sessions.

TERTILE BOUNDARIES ARE TRAILING, not full-sample. Ranking close_pos against the
whole history would decide today's trade using next year's distribution; the
cutoff is computed from the prior RANK_WINDOW sessions only. This costs some
sample at the start and is the difference between a backtest and a description.

COST IS THE WHOLE ARGUMENT AT THIS SIZE. The edge is ~15-19bp per event. BANKNIFTY
futures cost roughly 1.25bp of STT on the sell side plus brokerage plus the
spread, so a realistic round trip is a meaningful fraction of the signal itself.
A cost ladder is printed rather than one flattering assumption.

    python vanguard/research/mp_overnight_curve.py
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

RANK_WINDOW = 120          # trailing sessions used to place today's close_pos
COSTS_BPS = (0, 2, 5, 10)


def curve(r: pd.Series) -> dict:
    """Compound a return series in sequence and describe the path."""
    r = r.dropna()
    if len(r) < 30:
        return {}
    eq = (1 + r).cumprod()
    peak = eq.cummax()
    dd = eq / peak - 1.0
    # longest run of losing events
    streak = worst = 0
    for x in r:
        streak = streak + 1 if x <= 0 else 0
        worst = max(worst, streak)
    return {
        "n": len(r), "total": eq.iloc[-1] - 1.0,
        "arith": r.mean(), "geom": eq.iloc[-1] ** (1 / len(r)) - 1.0,
        "sd": r.std(ddof=1), "maxdd": dd.min(), "win": (r > 0).mean(),
        "streak": worst, "best": r.max(), "worst": r.min(),
        "eq": eq,
    }


def row(label: str, c: dict, years: float) -> None:
    if not c:
        print(f"   {label:<30}   (too few)")
        return
    cagr = (1 + c["total"]) ** (1 / years) - 1.0 if c["total"] > -1 else np.nan
    sharpe = (c["arith"] / c["sd"] * np.sqrt(c["n"] / years)) if c["sd"] > 0 else np.nan
    print(f"   {label:<30}{c['n']:>6}{c['total'] * 100:>+10.1f}{cagr * 100:>+8.1f}"
          f"{c['arith'] * 10000:>+9.1f}{c['geom'] * 10000:>+8.1f}"
          f"{c['maxdd'] * 100:>+9.1f}{sharpe:>+8.2f}{c['win'] * 100:>6.0f}%{c['streak']:>7}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BANKNIFTY")
    parser.add_argument("--years", type=float, default=5.2)
    parser.add_argument("--dsn", default=dsn())
    args = parser.parse_args()

    start = date.today() - timedelta(days=int(args.years * 365.25))
    connection = psycopg2.connect(args.dsn)
    try:
        s = load(connection, [args.symbol], start)
    finally:
        connection.close()
    s = s.sort_values("dt").reset_index(drop=True)

    # TRAILING tertile placement -- today's rank uses only prior sessions
    s["cp_rank"] = (s["close_pos"].rolling(RANK_WINDOW, min_periods=60)
                    .apply(lambda w: (w[-1] > w[:-1]).mean(), raw=True))
    d = s.dropna(subset=["cp_rank", "next_open_ret"]).reset_index(drop=True)
    span = (d["dt"].max() - d["dt"].min()).days / 365.25
    strong, weak = d["cp_rank"] >= 2 / 3, d["cp_rank"] <= 1 / 3
    print(f"{args.symbol}: {len(d):,} sessions with a trailing rank  "
          f"{d['dt'].min().date()} .. {d['dt'].max().date()}  ({span:.1f} years)")
    print(f"strong nights {strong.sum():,}   weak nights {weak.sum():,}   "
          f"exposure is OVERNIGHT ONLY\n")

    print("SEQUENTIALLY ACCUMULATED, zero cost")
    print(f"   {'strategy':<30}{'n':>6}{'total%':>10}{'CAGR%':>8}"
          f"{'arith bp':>9}{'geom bp':>8}{'maxDD%':>9}{'Sharpe':>8}{'win':>6}{'losses':>7}")
    variants = {
        "long overnight on STRONG": d.loc[strong, "next_open_ret"],
        "short overnight on WEAK": -d.loc[weak, "next_open_ret"],
        "long STRONG + short WEAK": pd.concat([d.loc[strong, "next_open_ret"],
                                               -d.loc[weak, "next_open_ret"]]).sort_index(),
        "long EVERY night (control)": d["next_open_ret"],
        "long overnight on WEAK": d.loc[weak, "next_open_ret"],
    }
    curves = {k: curve(v) for k, v in variants.items()}
    for k, c in curves.items():
        row(k, c, span)
    bh = d["close"].iloc[-1] / d["close"].iloc[0] - 1.0
    print(f"   {'buy & hold (24h exposure)':<30}{len(d):>6}{bh * 100:>+10.1f}"
          f"{((1 + bh) ** (1 / span) - 1) * 100:>+8.1f}")

    print("\nCOST LADDER, round trip per event (the edge is ~15-19bp)")
    print(f"   {'strategy / cost':<30}{'n':>6}{'total%':>10}{'CAGR%':>8}"
          f"{'arith bp':>9}{'geom bp':>8}{'maxDD%':>9}{'Sharpe':>8}{'win':>6}{'losses':>7}")
    for bps in COSTS_BPS:
        c = bps / 10000.0
        row(f"STRONG long, {bps}bp", curve(d.loc[strong, "next_open_ret"] - c), span)
    for bps in COSTS_BPS:
        c = bps / 10000.0
        row(f"long/short, {bps}bp",
            curve(pd.concat([d.loc[strong, "next_open_ret"],
                             -d.loc[weak, "next_open_ret"]]).sort_index() - c), span)

    print("\nYEAR BY YEAR, strong-close long overnight, 5bp cost")
    print(f"   {'year':<8}{'nights':>8}{'return%':>10}{'arith bp':>10}{'win':>7}")
    dd = d[strong].copy()
    dd["r"] = dd["next_open_ret"] - 0.0005
    for y, g in dd.groupby(dd["dt"].dt.year):
        tot = (1 + g["r"]).prod() - 1.0
        print(f"   {y:<8}{len(g):>8}{tot * 100:>+10.1f}{g['r'].mean() * 10000:>+10.1f}"
              f"{(g['r'] > 0).mean() * 100:>6.0f}%")

    out = os.environ.get("CURVE_OUT")
    if out:
        eq = pd.DataFrame({"dt": d.loc[strong, "dt"].values,
                           "r": d.loc[strong, "next_open_ret"].values})
        eq["strong_0bp"] = (1 + eq["r"]).cumprod()
        eq["strong_5bp"] = (1 + eq["r"] - 0.0005).cumprod()
        ls = pd.concat([d.loc[strong, ["dt", "next_open_ret"]],
                        d.loc[weak, ["dt"]].assign(
                            next_open_ret=-d.loc[weak, "next_open_ret"])]).sort_values("dt")
        ls["ls_5bp"] = (1 + ls["next_open_ret"] - 0.0005).cumprod()
        every = d[["dt", "next_open_ret"]].copy()
        every["every_0bp"] = (1 + every["next_open_ret"]).cumprod()
        bh_s = d[["dt", "close"]].copy()
        bh_s["bh"] = bh_s["close"] / bh_s["close"].iloc[0]
        eq.merge(ls[["dt", "ls_5bp"]], on="dt", how="outer") \
          .merge(every[["dt", "every_0bp"]], on="dt", how="outer") \
          .merge(bh_s[["dt", "bh"]], on="dt", how="outer") \
          .sort_values("dt").to_csv(out, index=False)
        print(f"\ncurve written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
