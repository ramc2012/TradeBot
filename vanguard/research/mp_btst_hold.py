"""Hold the gap-capture signal for 1, 2 or 3 days instead of selling the open.

THE SIGNAL, unchanged: at 15:15, the session closed ABOVE its value-area high and
in the 0.70-0.90 band of the day's range -- acceptance above value rather than a
spike at the high. It lands a >3% up gap 3.5x more often than a random night.

THE QUESTION: the current exit is the 09:15 open, which harvests the gap and
nothing else. What happens if the position is carried?

WHAT TO EXPECT, and why the decomposition matters more than the total. This
project already established that overnight and intraday behave like different
assets: overnight drift is strongly positive, intraday flow often fights it. So a
longer hold is not simply "more of the same trade" -- it adds a DIFFERENT return
stream on top of the gap, and that stream has historically been negative. The
total can rise while the added days lose money, if the gap is large enough to
carry them. So each day is reported SEPARATELY as well as cumulatively:

    gap          close(t) -> open(t+1)          the current trade
    day 1 intra  open(t+1) -> close(t+1)        what carrying past the open adds
    day 2, day 3 close-to-close thereafter
    MFE / MAE    the best and worst points reached during the hold, which bound
                 what any exit rule inside the window could have achieved

TAIL CAPTURE IS TRACKED AT EVERY HORIZON, because the objective is the big move,
not the average one. A longer hold that raises the mean while lowering P(>3%) is
not serving the stated goal.

    python vanguard/research/mp_btst_hold.py
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


def cluster_t(d: pd.DataFrame, col: str) -> float:
    d = d[[col, "dt"]].dropna()
    if len(d) < 30:
        return np.nan
    mu = d[col].mean()
    g = (d[col] - mu).groupby(d["dt"]).sum()
    se = np.sqrt((g ** 2).sum()) / len(d)
    return mu / se if se > 0 else np.nan


def row(lab: str, d: pd.DataFrame, col: str) -> None:
    r = d[col].dropna()
    if len(r) < 40:
        print(f"   {lab:<32}{len(r):>7}   (too few)")
        return
    print(f"   {lab:<32}{len(r):>7}{r.mean():>+9.3f}{r.median():>+10.3f}"
          f"{(r > 0).mean() * 100:>5.0f}%{cluster_t(d, col):>+8.2f}"
          f"{(r > 1).mean() * 100:>8.1f}{(r > 2).mean() * 100:>8.1f}"
          f"{(r > 3).mean() * 100:>8.1f}{r.std(ddof=1):>8.2f}")


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
    g = s.groupby("underlying")
    for k in (1, 2, 3):
        s[f"c{k}"] = g["close"].shift(-k)
        s[f"h{k}"] = g["high"].shift(-k)
        s[f"l{k}"] = g["low"].shift(-k)
    s["o1"] = g["open"].shift(-1)

    c0 = s["close"]
    s["r_gap"] = (s["o1"] / c0 - 1) * 100                      # the current trade
    s["r_d1"] = (s["c1"] / c0 - 1) * 100
    s["r_d2"] = (s["c2"] / c0 - 1) * 100
    s["r_d3"] = (s["c3"] / c0 - 1) * 100
    # each added leg on its own
    s["leg_intra1"] = (s["c1"] / s["o1"] - 1) * 100
    s["leg_day2"] = (s["c2"] / s["c1"] - 1) * 100
    s["leg_day3"] = (s["c3"] / s["c2"] - 1) * 100
    # best/worst inside the 3-day window, from the entry close
    s["mfe3"] = (s[["h1", "h2", "h3"]].max(axis=1) / c0 - 1) * 100
    s["mae3"] = (s[["l1", "l2", "l3"]].min(axis=1) / c0 - 1) * 100
    s["mfe1"] = (s["h1"] / c0 - 1) * 100

    s = s.dropna(subset=["vah", "r_gap"]).reset_index(drop=True)
    sig = s[(s["close"] > s["vah"]) & s["close_pos"].between(.70, .90)].copy()
    print(f"{s['underlying'].nunique()} banks, {s['dt'].nunique()} sessions   "
          f"signals {len(sig)} of {len(s):,} name-nights "
          f"({len(sig) / len(s) * 100:.1f}%)")

    hdr = (f"   {'exit':<32}{'n':>7}{'mean %':>9}{'median %':>10}{'win':>5}"
           f"{'clus t':>8}{'P>1%':>8}{'P>2%':>8}{'P>3%':>8}{'sd':>8}")
    print(f"\n1. HOLDING THE SIGNAL — cumulative from the 15:15 entry")
    print(hdr)
    row("sell at 09:15 open (current)", sig, "r_gap")
    row("hold to day 1 close", sig, "r_d1")
    row("hold to day 2 close", sig, "r_d2")
    row("hold to day 3 close", sig, "r_d3")
    row("best point in 3 days (MFE)", sig, "mfe3")
    row("worst point in 3 days (MAE)", sig, "mae3")

    print(f"\n2. WHAT EACH ADDED LEG CONTRIBUTES ON ITS OWN")
    print(hdr)
    row("the gap itself", sig, "r_gap")
    row("+ day 1 intraday (open->close)", sig, "leg_intra1")
    row("+ day 2 (close->close)", sig, "leg_day2")
    row("+ day 3 (close->close)", sig, "leg_day3")
    print("   If the added legs are negative, a longer hold is paying to keep a trade\n"
          "   whose edge was spent by 09:15.")

    print(f"\n3. THE SAME LEGS ON EVERY NIGHT (is the decay signal-specific?)")
    print(hdr)
    row("all nights: gap", s, "r_gap")
    row("all nights: day 1 intraday", s, "leg_intra1")
    row("all nights: day 2", s, "leg_day2")
    row("all nights: day 3", s, "leg_day3")

    print(f"\n4. TAIL CAPTURE BY HORIZON (the objective is the big move)")
    print(f"   {'exit':<32}{'P>2% sig':>10}{'P>2% all':>10}{'lift':>7}"
          f"{'P>3% sig':>10}{'P>3% all':>10}{'lift':>7}")
    for lab, col in (("09:15 open", "r_gap"), ("day 1 close", "r_d1"),
                     ("day 2 close", "r_d2"), ("day 3 close", "r_d3"),
                     ("best point in 3 days", "mfe3")):
        a, b = sig[col].dropna(), s[col].dropna()
        p2a, p2b = (a > 2).mean(), (b > 2).mean()
        p3a, p3b = (a > 3).mean(), (b > 3).mean()
        print(f"   {lab:<32}{p2a * 100:>9.1f}%{p2b * 100:>9.1f}%"
              f"{p2a / p2b if p2b else np.nan:>7.2f}"
              f"{p3a * 100:>9.1f}%{p3b * 100:>9.1f}%"
              f"{p3a / p3b if p3b else np.nan:>7.2f}")

    print(f"\n5. PER SYMBOL — gap vs 3-day hold (mean %)")
    print(f"   {'name':<13}{'n':>5}{'gap':>9}{'day1':>9}{'day2':>9}{'day3':>9}"
          f"{'MFE3':>9}{'MAE3':>9}")
    for name, d in sig.groupby("underlying"):
        if len(d) < 10:
            continue
        print(f"   {name:<13}{len(d):>5}{d['r_gap'].mean():>+9.3f}"
              f"{d['r_d1'].mean():>+9.3f}{d['r_d2'].mean():>+9.3f}"
              f"{d['r_d3'].mean():>+9.3f}{d['mfe3'].mean():>+9.3f}"
              f"{d['mae3'].mean():>+9.3f}")
    print(f"   {'POOLED':<13}{len(sig):>5}{sig['r_gap'].mean():>+9.3f}"
          f"{sig['r_d1'].mean():>+9.3f}{sig['r_d2'].mean():>+9.3f}"
          f"{sig['r_d3'].mean():>+9.3f}{sig['mfe3'].mean():>+9.3f}"
          f"{sig['mae3'].mean():>+9.3f}")

    print(f"\n6. RETURN PER UNIT OF RISK AND PER UNIT OF TIME")
    print(f"   {'exit':<32}{'mean %':>9}{'sd %':>8}{'mean/sd':>9}"
          f"{'nights held':>13}{'% per night':>13}")
    for lab, col, nights in (("09:15 open", "r_gap", 1), ("day 1 close", "r_d1", 1),
                             ("day 2 close", "r_d2", 2), ("day 3 close", "r_d3", 3)):
        r = sig[col].dropna()
        print(f"   {lab:<32}{r.mean():>+9.3f}{r.std(ddof=1):>8.2f}"
              f"{r.mean() / r.std(ddof=1):>9.3f}{nights:>13}"
              f"{r.mean() / nights:>+13.3f}")
    print("   'nights held' counts overnight sessions of exposure; the open-exit trade\n"
          "   holds one and is flat all day, the others carry intraday risk too.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
