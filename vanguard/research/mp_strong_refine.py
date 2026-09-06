"""Can the strong-close pick be sharpened to earn more points per night?

BASE: close_pos in the top tertile of its trailing 120 sessions -> +72.8 points
per night over 409 nights. The question is whether a second condition, also known
at 15:15, raises points per night without simply taking fewer, luckier nights.

TWO TRAPS THIS MODULE IS BUILT TO AVOID.

  1. VOLATILITY IS NOT EFFICIENCY. Points scale with the index level and with the
     day's volatility, so ANY filter that selects volatile nights will show more
     points per night while offering no better odds. Every candidate is therefore
     scored twice: raw points, and points divided by that session's ATR in points.
     A filter that improves the first but not the second is levering, not picking,
     and levering is available for free by simply trading more lots.

  2. MULTIPLE TESTING. Roughly two dozen conditions are tried. The best of 24
     independent nulls has an expected |t| near 2.5, so a t of 2 on the winner is
     evidence of nothing. Every candidate is reported -- not just the survivors --
     the count is printed, and anything that looks good must also hold in BOTH
     halves of the sample before it is worth a second look.

Filters are only counted if they are knowable at the 15:15 close, which is when
the trade is placed.

    python vanguard/research/mp_strong_refine.py
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


def build(symbol: str, years: float, dsn_: str) -> pd.DataFrame:
    connection = psycopg2.connect(dsn_)
    try:
        s = load(connection, [symbol], date.today() - timedelta(days=int(years * 365.25)))
    finally:
        connection.close()
    s = s.sort_values("dt").reset_index(drop=True)
    s["cp_rank"] = (s["close_pos"].rolling(RANK_WINDOW, min_periods=60)
                    .apply(lambda w: (w[-1] > w[:-1]).mean(), raw=True))
    s["gap_pts"] = s["close"] * s["next_open_ret"]
    s["atr_pts"] = s["atr20"] * s["close"]
    # points earned per point of daily range -- the risk-normalised unit
    s["gap_per_atr"] = s["gap_pts"] / s["atr_pts"].replace(0, np.nan)
    s["ema20"] = s["close"].ewm(span=20, adjust=False).mean()
    s["above_ema20"] = s["close"] > s["ema20"]
    s["prev_strong"] = (s["cp_rank"].shift(1) >= 2 / 3)
    s["dow"] = s["dt"].dt.dayofweek
    s["closed_above_prev_vah"] = s["close"] > s["prev_vah"]
    s["poc_up"] = s["poc_migration"] > 0
    s["atr_hi"] = s["atr20"] > s["atr20"].rolling(250, min_periods=100).median()
    return s.dropna(subset=["cp_rank", "gap_pts"]).reset_index(drop=True)


def score(d: pd.DataFrame, base: pd.Series, mask: pd.Series, label: str,
          rows: list) -> None:
    m = base & mask
    g = d[m]
    if len(g) < 40:
        rows.append({"filter": label, "n": int(m.sum()), "skip": True})
        return
    r, ra = g["gap_pts"], g["gap_per_atr"].dropna()
    t = r.mean() / (r.std(ddof=1) / np.sqrt(len(r))) if r.std(ddof=1) > 0 else np.nan
    h = len(g) // 2
    rows.append({
        "filter": label, "n": len(g), "skip": False,
        "pts": r.mean(), "med": r.median(), "t": t, "win": (r > 0).mean(),
        "per_atr": ra.mean(), "total": r.sum(),
        "h1": g.iloc[:h]["gap_pts"].mean(), "h2": g.iloc[h:]["gap_pts"].mean(),
    })


def report(rows: list, base_pts: float, base_atr: float, base_n: int) -> None:
    print(f"   {'filter (added to strong close)':<38}{'n':>5}{'pts/nt':>9}"
          f"{'median':>8}{'t':>7}{'win':>6}{'/ATR':>8}{'total':>9}"
          f"{'1st half':>10}{'2nd half':>10}")
    print(f"   {'BASE: strong close only':<38}{base_n:>5}{base_pts:>+9.1f}"
          f"{'':>8}{'':>7}{'':>6}{base_atr:>+8.3f}")
    for r in sorted([x for x in rows if not x["skip"]],
                    key=lambda x: -x["pts"]):
        flag = " *" if (r["pts"] > base_pts and r["per_atr"] > base_atr
                        and min(r["h1"], r["h2"]) > 0) else ""
        print(f"   {r['filter']:<38}{r['n']:>5}{r['pts']:>+9.1f}{r['med']:>+8.1f}"
              f"{r['t']:>+7.2f}{r['win'] * 100:>5.0f}%{r['per_atr']:>+8.3f}"
              f"{r['total']:>+9.0f}{r['h1']:>+10.1f}{r['h2']:>+10.1f}{flag}")
    skipped = [x["filter"] for x in rows if x["skip"]]
    if skipped:
        print(f"   too few nights to score: {', '.join(skipped)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BANKNIFTY")
    parser.add_argument("--years", type=float, default=5.2)
    parser.add_argument("--dsn", default=dsn())
    args = parser.parse_args()

    d = build(args.symbol, args.years, args.dsn)
    strong = d["cp_rank"] >= 2 / 3
    b = d[strong]
    base_pts, base_atr, base_n = (b["gap_pts"].mean(),
                                  b["gap_per_atr"].mean(), len(b))
    print(f"{args.symbol}  {len(d):,} sessions  {d['dt'].min().date()} .. "
          f"{d['dt'].max().date()}   base strong-close {base_n} nights "
          f"{base_pts:+.1f} pts/night")

    # ── A. does a HARDER close_pos threshold help? ─────────────────────────
    print(f"\nA. TIGHTENING THE THRESHOLD (fewer nights, better nights?)")
    print(f"   {'threshold':<38}{'n':>5}{'pts/nt':>9}{'median':>8}{'t':>7}"
          f"{'win':>6}{'/ATR':>8}{'total':>9}")
    for q in (0.50, 0.60, 2 / 3, 0.75, 0.80, 0.90):
        g = d[d["cp_rank"] >= q]
        if len(g) < 40:
            continue
        r = g["gap_pts"]
        t = r.mean() / (r.std(ddof=1) / np.sqrt(len(r)))
        print(f"   {'cp_rank >= ' + format(q, '.2f'):<38}{len(g):>5}{r.mean():>+9.1f}"
              f"{r.median():>+8.1f}{t:>+7.2f}{(r > 0).mean() * 100:>5.0f}%"
              f"{g['gap_per_atr'].mean():>+8.3f}{r.sum():>+9.0f}")
    print("   Tightening trades nights for quality. If pts/night barely moves, the\n"
          "   signal is a plateau and the top tertile is already the right cut.")

    # ── B. a second condition, all knowable at 15:15 ───────────────────────
    print(f"\nB. ADDING A SECOND CONDITION")
    rows: list = []
    C = [
        ("+ buying tail at the low", d["tail_low"] > 0),
        ("+ no selling tail at the high", d["tail_high"] <= 0),
        ("+ POC migrated up", d["poc_up"]),
        ("+ closed above prior VAH", d["closed_above_prev_vah"]),
        ("+ value shifted higher_outside", d["value_shift"] == "higher_outside"),
        ("+ value overlapping (balance)", d["value_shift"] == "overlapping"),
        ("+ day type = trend", d["day_type"] == "trend"),
        ("+ day type = normal_variation", d["day_type"] == "normal_variation"),
        ("+ day type = neutral_extreme", d["day_type"] == "neutral_extreme"),
        ("+ NOT a normal (balanced) day", d["day_type"] != "normal"),
        ("+ range/IB >= 2 (extended)", d["range_over_ib"] >= 2.0),
        ("+ range/IB < 1.5 (contained)", d["range_over_ib"] < 1.5),
        ("+ narrow IB (rank <= 0.33)", d["ib_pct_rank"] <= 0.33),
        ("+ wide IB (rank >= 0.67)", d["ib_pct_rank"] >= 0.67),
        ("+ high-vol regime", d["atr_hi"]),
        ("+ low-vol regime", ~d["atr_hi"]),
        ("+ above 20-EMA (uptrend)", d["above_ema20"]),
        ("+ below 20-EMA (counter)", ~d["above_ema20"]),
        ("+ today gapped up", d["gap"] > 0),
        ("+ today gapped down", d["gap"] < 0),
        ("+ prior session also strong", d["prev_strong"]),
        ("+ prior session NOT strong", ~d["prev_strong"]),
        ("+ poor high (unfinished)", d["poor_high"]),
        ("+ Friday (weekend hold)", d["dow"] == 4),
        ("+ not Friday", d["dow"] != 4),
        ("+ close_pos >= 0.90 absolute", d["close_pos"] >= 0.90),
    ]
    for label, m in C:
        score(d, strong, m, label, rows)
    report(rows, base_pts, base_atr, base_n)
    print(f"\n   {len(C)} conditions tested. The best of {len(C)} independent nulls has an\n"
          f"   expected |t| near 2.5, so a single t above 2 here is evidence of nothing.\n"
          f"   '*' marks the few that beat the base on BOTH raw points AND points-per-ATR\n"
          f"   while staying positive in both halves — the only ones worth a second look.")

    # ── C. combine whatever survived, and check it honestly ────────────────
    survivors = [r for r in rows if not r["skip"] and r["pts"] > base_pts
                 and r["per_atr"] > base_atr and min(r["h1"], r["h2"]) > 0]
    print(f"\nC. THE SURVIVORS COMBINED ({len(survivors)} of {len(C)} passed)")
    if not survivors:
        print("   none — no second condition beats the plain strong close on both\n"
              "   measures while holding in both halves. The base filter stands.")
        return 0
    names = [s["filter"] for s in survivors[:3]]
    print(f"   combining: {', '.join(names)}")
    combo = strong.copy()
    for label, m in C:
        if label in names:
            combo &= m
    g = d[combo]
    if len(g) >= 40:
        r = g["gap_pts"]
        h = len(g) // 2
        t = r.mean() / (r.std(ddof=1) / np.sqrt(len(r)))
        print(f"   {'combined':<38}{len(g):>5}{r.mean():>+9.1f}{r.median():>+8.1f}"
              f"{t:>+7.2f}{(r > 0).mean() * 100:>5.0f}%"
              f"{g['gap_per_atr'].mean():>+8.3f}{r.sum():>+9.0f}"
              f"{g.iloc[:h]['gap_pts'].mean():>+10.1f}{g.iloc[h:]['gap_pts'].mean():>+10.1f}")
        print(f"   vs base {base_pts:+.1f} pts/night on {base_n} nights "
              f"({b['gap_pts'].sum():+,.0f} points total)")
        print(f"   TOTAL points is the number that matters for a book: a filter that\n"
              f"   lifts pts/night but halves the nights can easily earn LESS overall.")
    else:
        print(f"   combined leaves only {len(g)} nights — too few to trust")
    return 0


if __name__ == "__main__":
    sys.exit(main())
