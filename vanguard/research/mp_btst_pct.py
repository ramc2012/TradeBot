"""Per symbol, the overnight gap expressed as a PERCENT per night, not basis points.

Same rule as mp_btst_v2.py: long overnight when the session closes above its own
value-area high AND the name sits in the top half of the sixteen banks by
trailing 20-session return.

WHY THE UNIT MATTERS. +16.5 bp reads as nothing; it is 0.165% per night, and the
typical night MOVES far more than that in either direction. The mean is a small
residual left over from a large two-sided distribution, so this prints the mean
alongside the SIZE of a typical gap and the share of nights that clear useful
thresholds. A rule whose average is +0.17% but whose median night is +0.05% and
whose typical absolute move is 0.9% is a coin-flip with a slight tilt, and that
should be visible in the same table rather than inferred.

Rupees per share are given at each name's own average price so the figure can be
scaled by whatever lot size applies; lot sizes are not assumed here.

    python vanguard/research/mp_btst_pct.py
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
    s["ret20"] = g["close"].transform(lambda x: x / x.shift(20) - 1.0)
    s["rs_rank"] = s.groupby("dt")["ret20"].rank(pct=True)
    s = s.dropna(subset=["next_open_ret", "vah", "ret20", "rs_rank"]).reset_index(drop=True)
    s["gap"] = s["next_open_ret"] * 100.0                     # PERCENT
    s["gap_rs"] = s["next_open_ret"] * s["close"]             # rupees per share
    sig = s[(s["close"] > s["vah"]) & (s["rs_rank"] >= 0.5)]

    print(f"rule: close ABOVE the value area, name in the top half of the 16 banks "
          f"by trailing 20d return")
    print(f"{sig['underlying'].nunique()} banks, {len(sig):,} trades, "
          f"{sig['dt'].min().date()} .. {sig['dt'].max().date()}\n")

    print("PER SYMBOL — overnight gap in PERCENT per night")
    print(f"   {'name':<13}{'trades':>7}{'avg px':>9}{'mean %':>9}{'median %':>10}"
          f"{'typical |%|':>12}{'Rs/share':>10}{'win':>6}"
          f"{'P>0.5%':>8}{'P>1%':>7}{'total %':>9}")
    rows = []
    for name, d in sig.groupby("underlying"):
        r = d["gap"]
        if len(r) < 15:
            continue
        rows.append({
            "name": name, "n": len(r), "px": d["close"].mean(),
            "mean": r.mean(), "med": r.median(), "abs": r.abs().median(),
            "rs": d["gap_rs"].mean(), "win": (r > 0).mean(),
            "p05": (r > 0.5).mean(), "p1": (r > 1.0).mean(),
            "tot": ((1 + r / 100).prod() - 1) * 100})
    for p in sorted(rows, key=lambda x: -x["mean"]):
        print(f"   {p['name']:<13}{p['n']:>7}{p['px']:>9.0f}{p['mean']:>+9.3f}"
              f"{p['med']:>+10.3f}{p['abs']:>12.3f}{p['rs']:>+10.2f}"
              f"{p['win'] * 100:>5.0f}%{p['p05'] * 100:>7.0f}%{p['p1'] * 100:>6.0f}%"
              f"{p['tot']:>+9.1f}")
    r = sig["gap"]
    print(f"   {'POOLED':<13}{len(r):>7}{'':>9}{r.mean():>+9.3f}{r.median():>+10.3f}"
          f"{r.abs().median():>12.3f}{sig['gap_rs'].mean():>+10.2f}"
          f"{(r > 0).mean() * 100:>5.0f}%{(r > 0.5).mean() * 100:>7.0f}%"
          f"{(r > 1.0).mean() * 100:>6.0f}%")

    print(f"\n   'typical |%|' is the MEDIAN ABSOLUTE gap — how far a signal night moves\n"
          f"   in either direction. The mean is the small net tilt left after those\n"
          f"   two-sided moves cancel, which is why it is an order of magnitude smaller.")

    # the distribution the mean is drawn from
    print(f"\nWHAT THE DISTRIBUTION LOOKS LIKE (all {len(r):,} signal nights, %)")
    qs = [1, 5, 10, 25, 50, 75, 90, 95, 99]
    print("   percentile  " + "".join(f"{q:>8}" for q in qs))
    print("   gap %       " + "".join(f"{np.percentile(r, q):>+8.2f}" for q in qs))
    print(f"   mean {r.mean():+.3f}%   sd {r.std(ddof=1):.3f}%   "
          f"mean/sd {r.mean() / r.std(ddof=1):.3f}   "
          f"P(gap > 0) {(r > 0).mean() * 100:.0f}%")

    # for comparison, the unconditional night
    allg = s["gap"]
    print(f"\n   for contrast, EVERY name-night (no signal): mean {allg.mean():+.3f}%   "
          f"median {allg.median():+.3f}%   typical |{allg.abs().median():.3f}|%   "
          f"P(>0) {(allg > 0).mean() * 100:.0f}%")
    print(f"   the signal adds {r.mean() - allg.mean():+.3f} percentage points to the "
          f"average night")

    # what it is worth per trade at each name's price level
    print(f"\nWHAT ONE SHARE EARNS PER SIGNAL NIGHT (mean, rupees)")
    tot_rs = sum(p["rs"] * p["n"] for p in rows)
    print(f"   pooled mean {sig['gap_rs'].mean():+.2f} Rs/share/night; "
          f"across all {len(sig):,} trades that is {tot_rs:+,.0f} Rs per share held")
    print(f"   highest: " + ", ".join(
        f"{p['name']} {p['rs']:+.2f}" for p in sorted(rows, key=lambda x: -x["rs"])[:4]))
    print(f"   lowest:  " + ", ".join(
        f"{p['name']} {p['rs']:+.2f}" for p in sorted(rows, key=lambda x: x["rs"])[:3]))
    print(f"\n   Multiply by lot size for a futures position. At the ~22bp cash-equity\n"
          f"   cost floor the pooled {r.mean():+.3f}% becomes {r.mean() - 0.22:+.3f}%; at a\n"
          f"   3-5bp futures cost it becomes {r.mean() - 0.04:+.3f}%.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
