"""Does weekly / monthly value context help hit a move target?

Three targets, each scaled to what the instrument can actually deliver rather
than to a round number:

    NIFTY        1% in 3-4 sessions   (its 2% base rate was only 20.9% in 4d)
    BANKNIFTY    2% in 3-4 sessions   (the earlier benchmark, carried for
                                       comparison)
    BANK STOCKS  5% in 3-4 sessions   (single names move far more than an index,
                                       so 2% there is nearly the base case)

WHAT IS BEING ADDED. Every earlier test judged the close against the SAME day's
value area. Here the daily close is also placed against the prior completed
WEEK'S and MONTH'S value areas, and against the alignment of all three. MP's
claim is that a close clearing daily, weekly and monthly value together is a
different event from one clearing only the day's -- the longer-timeframe buyer
has to be involved, and that buyer is the one who moves price over three or four
days.

THE CONTROL THAT DECIDES IT. The daily-only condition is already known to work.
So the weekly and monthly layers are scored on what they ADD: P(target) given
day-only, versus day+week, versus all three. If "above all three" does not beat
"above the day alone", the composite profile is decoration and should be dropped.

    python vanguard/research/mp_mtf_targets.py
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
from research.mp_auction import dsn  # noqa: E402
from research.mp_multi_tf import load_mtf, targets  # noqa: E402

CASES = [("NIFTY", ["NIFTY"], 1.0), ("BANKNIFTY", ["BANKNIFTY"], 2.0),
         ("BANK STOCKS", list(BANKS), 5.0)]


def block(d: pd.DataFrame, T: float, label: str) -> None:
    up4, dn4 = d["up4"] >= T, d["dn4"] <= -T
    bu, be = up4.mean(), (up4 | dn4).mean()
    print(f"\n{label}: {len(d):,} sessions, target {T:.0f}%")
    print(f"   base rates — UP in 3d {(d['up3'] >= T).mean() * 100:.1f}%"
          f"   in 4d {bu * 100:.1f}%   either side 4d {be * 100:.1f}%"
          f"   close-to-close 4d {(d['cc4'] >= T).mean() * 100:.1f}%")
    print(f"   {'condition':<34}{'n':>7}{'P(up)':>8}{'lift':>7}{'P(either)':>11}"
          f"{'lift':>7}{'mean cc4':>10}{'t':>7}")
    conds = [
        ("every session (base)", pd.Series(True, index=d.index)),
        ("-- daily only --", None),
        ("close above DAY value", d["d_above"]),
        ("close below DAY value", d["close"] < d["val"]),
        ("-- adding week and month --", None),
        ("close above WEEK value", d["w_above"]),
        ("close above MONTH value", d["m_above"]),
        ("above day + week", d["d_above"] & d["w_above"]),
        ("above day + month", d["d_above"] & d["m_above"]),
        ("ABOVE ALL THREE", d["d_above"] & d["w_above"] & d["m_above"]),
        ("below all three", (d["close"] < d["val"]) & d["w_below"] & d["m_below"]),
        ("inside week value (balance)", ~d["w_above"] & ~d["w_below"]),
        ("above week, inside month", d["w_above"] & ~d["m_above"]),
    ]
    for lab, m in conds:
        if m is None:
            print(f"   {lab}")
            continue
        m = m.fillna(False)
        if m.sum() < 25:
            print(f"   {lab:<34}{int(m.sum()):>7}   (too few)")
            continue
        cc = d.loc[m, "cc4"].dropna()
        t = cc.mean() / (cc.std(ddof=1) / np.sqrt(len(cc))) if cc.std(ddof=1) > 0 else np.nan
        pu, pe = up4[m].mean(), (up4 | dn4)[m].mean()
        print(f"   {lab:<34}{int(m.sum()):>7}{pu * 100:>7.0f}%{pu / bu:>7.2f}"
              f"{pe * 100:>10.0f}%{pe / be:>7.2f}{cc.mean():>+10.3f}{t:>+7.2f}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=float, default=5.2)
    parser.add_argument("--dsn", default=dsn())
    args = parser.parse_args()
    start = date.today() - timedelta(days=int(args.years * 365.25))

    connection = psycopg2.connect(args.dsn)
    try:
        for label, names, T in CASES:
            d = targets(load_mtf(connection, names, start))
            d = d.dropna(subset=["up4", "dn4", "cc4", "w_vah", "m_vah"]).reset_index(drop=True)
            if d.empty:
                print(f"\n{label}: no data")
                continue
            block(d, T, label)
            base = d["d_above"].fillna(False)
            if base.sum() > 40:
                up4 = d["up4"] >= T
                a = up4[base].mean()
                b = up4[base & d["w_above"].fillna(False)].mean()
                c = up4[base & d["w_above"].fillna(False)
                        & d["m_above"].fillna(False)].mean()
                print(f"   INCREMENT over 'above day value' ({a * 100:.0f}%): "
                      f"+week {b * 100:.0f}%   +week+month {c * 100:.0f}%")
    finally:
        connection.close()
    print("\n   'lift' is versus that instrument's own base rate. A weekly/monthly layer\n"
          "   earns its place only if 'ABOVE ALL THREE' beats 'close above DAY value'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
