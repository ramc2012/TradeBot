"""The base strong-close rule, applied to the bank STOCKS, with an equity curve.

RULE UNCHANGED, as instructed: close_pos in the top tertile of that name's own
trailing 120 sessions; long overnight from the 15:15 close to the next 09:15
open; no second condition, no re-tuned threshold.

THE WINDOW IS 17 MONTHS, NOT THREE YEARS, AND CANNOT BE MADE LONGER. The bank
stocks exist in underlying_spot_candles only from 2025-03-28. There is no daily
OHLC source to fall back on -- bhavcopy_delivery holds five sessions, and no other
table in this database carries stock prices. So the three-year request is not
refusable-by-choice, it is unavailable, and every number here rests on ~350
sessions per name with a 120-session warm-up consumed by the ranking window.

WORSE, IT IS THE HARDEST AVAILABLE WINDOW. On BANKNIFTY the same signal was
markedly weaker over exactly this period (median gap +44.7 points against +74.3
across five years). So this is an out-of-sample test on the least favourable
stretch, not a like-for-like comparison with the index result.

PORTFOLIO, NOT A POOL OF TRADES. Averaging every signalling name-night together
would silently assume unlimited capital and would let one night with fourteen
signals count fourteen times. Instead each night is EQUAL-WEIGHTED across
whichever names signalled, so the curve is what one book actually earned, and a
night with one signal counts as much as a night with twelve.

COSTS ARE HIGHER ON STOCKS than on index futures -- wider spreads, and stock
futures carry real impact. The ladder runs to 20bp for that reason.

    python vanguard/research/mp_banks_overnight.py
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
from research.banknifty_rotation import BANKS  # noqa: E402
from research.mp_auction import dsn, load  # noqa: E402

RANK_WINDOW = 120
MIN_PERIODS = 60
COSTS_BPS = (0, 5, 10, 20)


def curve_stats(r: pd.Series, span: float) -> dict:
    r = r.dropna()
    if len(r) < 20:
        return {}
    eq = (1 + r).cumprod()
    dd = eq / eq.cummax() - 1.0
    sd = r.std(ddof=1)
    return {"n": len(r), "total": eq.iloc[-1] - 1.0,
            "cagr": eq.iloc[-1] ** (1 / span) - 1.0, "mean": r.mean(),
            "t": r.mean() / (sd / np.sqrt(len(r))) if sd > 0 else np.nan,
            "maxdd": dd.min(), "win": (r > 0).mean(),
            "sharpe": r.mean() / sd * np.sqrt(len(r) / span) if sd > 0 else np.nan,
            "eq": eq}


def row(label: str, c: dict) -> None:
    if not c:
        print(f"   {label:<34}   (too few)")
        return
    print(f"   {label:<34}{c['n']:>6}{c['total'] * 100:>+10.1f}{c['cagr'] * 100:>+9.1f}"
          f"{c['mean'] * 10000:>+9.1f}{c['maxdd'] * 100:>+9.1f}"
          f"{c['sharpe']:>+8.2f}{c['win'] * 100:>6.0f}%{c['t']:>+7.2f}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=float, default=3.0)
    parser.add_argument("--dsn", default=dsn())
    args = parser.parse_args()

    start = date.today() - timedelta(days=int(args.years * 365.25))
    connection = psycopg2.connect(args.dsn)
    try:
        s = load(connection, list(BANKS) + ["BANKNIFTY"], start)
    finally:
        connection.close()

    s = s.sort_values(["underlying", "dt"]).reset_index(drop=True)
    s["cp_rank"] = (s.groupby("underlying")["close_pos"]
                    .transform(lambda x: x.rolling(RANK_WINDOW, min_periods=MIN_PERIODS)
                               .apply(lambda w: (w[-1] > w[:-1]).mean(), raw=True)))
    s["gap"] = s["next_open_ret"]
    s["gap_pts"] = s["close"] * s["gap"]
    s = s.dropna(subset=["cp_rank", "gap"])
    stocks = s[s["underlying"] != "BANKNIFTY"].copy()
    index = s[s["underlying"] == "BANKNIFTY"].copy()
    stocks["strong"] = stocks["cp_rank"] >= 2 / 3
    index["strong"] = index["cp_rank"] >= 2 / 3

    span = (stocks["dt"].max() - stocks["dt"].min()).days / 365.25
    print(f"bank STOCKS: {stocks['underlying'].nunique()} names, "
          f"{stocks['dt'].nunique():,} sessions, "
          f"{stocks['dt'].min().date()} .. {stocks['dt'].max().date()} "
          f"({span:.2f} years)")
    print(f"REQUESTED 3 YEARS; {span:.2f} IS ALL THAT EXISTS -- 30m stock data starts "
          f"2025-03-28\nand no daily OHLC source covers these names. Treat every figure "
          f"below accordingly.")
    print(f"signal nights: {int(stocks['strong'].sum()):,} name-nights on "
          f"{stocks.loc[stocks['strong'], 'dt'].nunique():,} distinct sessions "
          f"(mean {stocks.loc[stocks['strong']].groupby('dt').size().mean():.1f} "
          f"names per signalling night)")

    # ── portfolio: equal weight across the names signalling that night ──────
    daily = (stocks[stocks["strong"]].groupby("dt")["gap"].mean().sort_index())
    every = stocks.groupby("dt")["gap"].mean().sort_index()
    bh = (stocks.groupby(["dt"])["total"].mean().sort_index()
          if "total" in stocks else None)

    print(f"\nEQUITY CURVE — equal-weight portfolio of the signalling names")
    print(f"   {'strategy':<34}{'n':>6}{'total%':>10}{'CAGR%':>9}{'bp/night':>9}"
          f"{'maxDD%':>9}{'Sharpe':>8}{'win':>6}{'t':>7}")
    row("STRONG close, banks (0bp)", curve_stats(daily, span))
    for bps in COSTS_BPS[1:]:
        row(f"STRONG close, banks ({bps}bp)", curve_stats(daily - bps / 1e4, span))
    row("every name every night (0bp)", curve_stats(every, span))
    # pooled over NAME-NIGHTS rather than nights: this weights a session with
    # twelve signals twelve times, which no single book does, but the gap
    # between the two says whether crowded nights behave differently
    row("pooled name-nights (not a book)",
        curve_stats(stocks.loc[stocks["strong"], "gap"], span))
    # the index restricted to the STOCKS' window -- the earlier version compared
    # against BANKNIFTY's full three years and mislabelled it "same window"
    lo, hi = stocks["dt"].min(), stocks["dt"].max()
    idx_w = index[(index["dt"] >= lo) & (index["dt"] <= hi)]
    row("BANKNIFTY strong close, SAME window",
        curve_stats(idx_w.loc[idx_w["strong"], "gap"], span))
    row("BANKNIFTY every night, SAME window", curve_stats(idx_w["gap"], span))

    # buy and hold the basket, for the same period, 24h exposure
    px = (stocks.pivot_table(index="dt", columns="underlying", values="close")
          .sort_index().ffill())
    bh_ret = px.pct_change().mean(axis=1).dropna()
    bh_eq = (1 + bh_ret).cumprod()
    print(f"   {'buy & hold basket (24h)':<34}{len(bh_ret):>6}"
          f"{(bh_eq.iloc[-1] - 1) * 100:>+10.1f}"
          f"{(bh_eq.iloc[-1] ** (1 / span) - 1) * 100:>+9.1f}")

    # ── per name ────────────────────────────────────────────────────────────
    print(f"\nPER NAME (its own trailing tertile; points are that stock's own points)")
    print(f"   {'name':<14}{'nights':>7}{'bp/night':>10}{'median bp':>11}"
          f"{'win':>7}{'total%':>9}{'pts/night':>11}")
    per = []
    for name, g in stocks[stocks["strong"]].groupby("underlying"):
        r = g["gap"].dropna()
        if len(r) < 20:
            continue
        per.append({"name": name, "n": len(r), "bp": r.mean() * 1e4,
                    "med": r.median() * 1e4, "win": (r > 0).mean(),
                    "tot": (1 + r).prod() - 1.0, "pts": g["gap_pts"].mean()})
    for p in sorted(per, key=lambda x: -x["bp"]):
        print(f"   {p['name']:<14}{p['n']:>7}{p['bp']:>+10.1f}{p['med']:>+11.1f}"
              f"{p['win'] * 100:>6.0f}%{p['tot'] * 100:>+9.1f}{p['pts']:>+11.1f}")
    pos = sum(1 for p in per if p["bp"] > 0)
    print(f"   {pos} of {len(per)} names positive")

    print(f"\nBY YEAR (portfolio, 10bp)")
    print(f"   {'year':<8}{'nights':>8}{'return%':>10}{'bp/night':>11}{'win':>7}")
    dn = (daily - 10 / 1e4).to_frame("r")
    for y, g in dn.groupby(dn.index.year):
        print(f"   {y:<8}{len(g):>8}{((1 + g['r']).prod() - 1) * 100:>+10.1f}"
              f"{g['r'].mean() * 1e4:>+11.1f}{(g['r'] > 0).mean() * 100:>6.0f}%")

    # Why the book and the name-average disagree: if crowded nights are the bad
    # ones, equal-weighting each NIGHT (what a book does) underperforms the
    # per-name mean, and the signal is picking up market-wide gap risk rather
    # than anything name-specific.
    print(f"\nDOES SIGNAL CROWDING PREDICT THE NIGHT? (portfolio, 0bp)")
    cnt = stocks[stocks["strong"]].groupby("dt").size().rename("k")
    j = pd.concat([daily.rename("r"), cnt], axis=1).dropna()
    print(f"   {'names signalling that night':<34}{'nights':>8}{'bp/night':>10}{'win':>7}")
    for lab, m in (("1-3 names", j["k"] <= 3), ("4-7 names", (j["k"] >= 4) & (j["k"] <= 7)),
                   ("8+ names (crowded)", j["k"] >= 8)):
        g = j[m]
        if len(g) < 15:
            continue
        print(f"   {lab:<34}{len(g):>8}{g['r'].mean() * 1e4:>+10.1f}"
              f"{(g['r'] > 0).mean() * 100:>6.0f}%")
    print(f"   rank corr(names signalling, portfolio return) "
          f"{j['k'].corr(j['r'], method='spearman'):+.3f}")

    print(f"\nSPLIT-HALF (portfolio, 10bp)")
    h = len(dn) // 2
    a, b = dn.iloc[:h]["r"], dn.iloc[h:]["r"]
    print(f"   1st half {a.mean() * 1e4:+.1f}bp (n={len(a)})   "
          f"2nd half {b.mean() * 1e4:+.1f}bp (n={len(b)})")
    d2 = daily.drop(daily.nlargest(2).index)
    print(f"   drop 2 best nights: {daily.mean() * 1e4:+.1f}bp -> {d2.mean() * 1e4:+.1f}bp"
          f"   median unchanged at {daily.median() * 1e4:+.1f}bp")

    out = os.environ.get("BANKS_OUT")
    if out:
        e = pd.DataFrame({"dt": daily.index, "strong_0bp": (1 + daily).cumprod().values,
                          "strong_10bp": (1 + daily - 10 / 1e4).cumprod().values})
        e = e.merge(pd.DataFrame({"dt": every.index,
                                  "every_0bp": (1 + every).cumprod().values}),
                    on="dt", how="outer")
        e = e.merge(pd.DataFrame({"dt": bh_eq.index, "bh": bh_eq.values}),
                    on="dt", how="outer").sort_values("dt")
        e.to_csv(out, index=False)
        print(f"\ncurve written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
