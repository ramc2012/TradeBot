"""ACCEPTANCE, not a spike: above value but NOT at the day's high.

THE MARGIN TEST INVERTED THE HYPOTHESIS. Marginal closes above the value-area high
were supposed to be bin-boundary noise. They are the opposite: as the margin
grows, the win rate falls monotonically 59% -> 52% -> 48% -> 42% -> 36%, and the
session-clustered t falls with it (+2.00 down to +0.70). Requiring a bigger margin
made every number worse. A close far above value has already made the move; a
close just past it has not.

THAT POINTED AT THE REAL SPLIT, which is MP's own distinction:

    ACCEPTANCE   the close is ABOVE the value area but NOT at the extreme of the
                 session. The auction traded up and stayed there. Unfinished.
    SPIKE        the close is at the day's high. The move happened in the last
                 stretch and nothing has yet accepted it.

Split that way on the earlier data: above VAH but NOT in the top fifth of the
range returned +0.206% a night on 259 trades (t +2.37, 56% win), while merely
closing near the high without clearing value returned +0.124% (t +1.51), and
doing BOTH returned +0.124% (t +1.56). The profile carries information that range
location does not -- and the two conditions actually work AGAINST each other.

COUNT THE SEARCH. Twelve definitions, eight strength filters, five margin rules
and four cohorts is about twenty-nine tests. A t of +2.37 does not survive that
arithmetic on its own. What earns this a second look is that the split is a
pre-existing MP concept with a stated mechanism, and that it was found by a test
designed to prove the opposite.

    python vanguard/research/mp_btst_acceptance.py
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

SPIKE_CUT = 0.80


def cluster_t(d: pd.DataFrame, col: str = "gap") -> float:
    d = d[[col, "dt"]].dropna()
    if len(d) < 30:
        return np.nan
    mu = d[col].mean()
    g = (d[col] - mu).groupby(d["dt"]).sum()
    se = np.sqrt((g ** 2).sum()) / len(d)
    return mu / se if se > 0 else np.nan


def line(lab: str, d: pd.DataFrame) -> None:
    if len(d) < 40:
        print(f"   {lab:<34}{len(d):>7}   (too few)")
        return
    r = d["gap"]
    h = d["dt"].nunique() // 2
    cut = sorted(d["dt"].unique())[h]
    d2 = r.drop(r.nlargest(2).index)
    print(f"   {lab:<34}{len(d):>7}{r.mean():>+9.3f}{r.median():>+10.3f}"
          f"{(r > 0).mean() * 100:>5.0f}%{cluster_t(d):>+8.2f}"
          f"{d[d['dt'] < cut]['gap'].mean():>+10.3f}{d[d['dt'] >= cut]['gap'].mean():>+10.3f}"
          f"{d2.mean():>+9.3f}")


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
    s["gap"] = s["next_open_ret"] * 100.0
    s["above"] = s["close"] > s["vah"]
    s["spike"] = s["close_pos"] >= SPIKE_CUT
    s["accept"] = s["above"] & ~s["spike"]
    rs = s["rs_rank"] >= 0.5

    print(f"{s['underlying'].nunique()} banks, {s['dt'].nunique()} sessions, "
          f"{len(s):,} name-sessions   (spike = close in the top "
          f"{(1 - SPIKE_CUT) * 100:.0f}% of the day's range)")
    print(f"\n1. ACCEPTANCE vs SPIKE")
    print(f"   {'cohort':<34}{'trades':>7}{'mean %':>9}{'median %':>10}{'win':>6}"
          f"{'clus t':>8}{'1st half':>10}{'2nd half':>10}{'drop2':>9}")
    line("ACCEPT: above VAH, not at high", s[s["accept"]])
    line("SPIKE: at high, not above VAH", s[s["spike"] & ~s["above"]])
    line("both above VAH and at high", s[s["above"] & s["spike"]])
    line("neither (the other 60%)", s[~s["above"] & ~s["spike"]])
    line("ALL name-sessions", s)

    print(f"\n2. ACCEPTANCE + relative strength")
    line("ACCEPT only", s[s["accept"]])
    line("ACCEPT + RS top half", s[s["accept"] & rs])
    line("ACCEPT + RS top third", s[s["accept"] & (s["rs_rank"] >= 2 / 3)])
    line("ACCEPT + RS bottom half (control)", s[s["accept"] & ~rs])

    print(f"\n3. WHERE IN THE RANGE DOES THE CLOSE WANT TO BE? (above VAH only)")
    ab = s[s["above"]].copy()
    ab["b"] = pd.cut(ab["close_pos"], [0, .6, .7, .8, .9, 1.001],
                     labels=["<0.60", "0.60-0.70", "0.70-0.80", "0.80-0.90", "0.90-1.00"])
    print(f"   {'close_pos bucket':<34}{'trades':>7}{'mean %':>9}{'median %':>10}"
          f"{'win':>6}{'clus t':>8}")
    for b, d in ab.groupby("b", observed=True):
        if len(d) < 40:
            print(f"   {str(b):<34}{len(d):>7}   (too few)")
            continue
        r = d["gap"]
        print(f"   {str(b):<34}{len(d):>7}{r.mean():>+9.3f}{r.median():>+10.3f}"
              f"{(r > 0).mean() * 100:>5.0f}%{cluster_t(d):>+8.2f}")
    print("   A close above value but mid-range is the auction holding its gains.\n"
          "   A close above value AND at the high is a late spike with nothing\n"
          "   accepted behind it — MP says that is the weaker of the two, and it is.")

    print(f"\n4. PER SYMBOL — ACCEPT + RS top half (zero cost)")
    sel = s[s["accept"] & rs]
    print(f"   {'name':<13}{'trades':>8}{'mean %':>9}{'median %':>10}{'win':>6}"
          f"{'total %':>9}{'maxDD %':>9}")
    tot = []
    for name, d in sel.groupby("underlying"):
        r = d["gap"]
        if len(r) < 8:
            print(f"   {name:<13}{len(r):>8}   (too few)")
            continue
        eq = (1 + r / 100).cumprod()
        tot.append(eq.iloc[-1] - 1)
        print(f"   {name:<13}{len(r):>8}{r.mean():>+9.3f}{r.median():>+10.3f}"
              f"{(r > 0).mean() * 100:>5.0f}%{(eq.iloc[-1] - 1) * 100:>+9.1f}"
              f"{(eq / eq.cummax() - 1).min() * 100:>+9.1f}")
    r = sel["gap"]
    print(f"   {'POOLED':<13}{len(r):>8}{r.mean():>+9.3f}{r.median():>+10.3f}"
          f"{(r > 0).mean() * 100:>5.0f}%")
    print(f"   average book {np.mean(tot) * 100:+.2f}%   "
          f"{sum(1 for x in tot if x > 0)}/{len(tot)} positive")
    print(f"\n   at a 3-5bp futures cost the pooled {r.mean():+.3f}% becomes "
          f"{r.mean() - 0.04:+.3f}%; at the ~22bp cash-equity floor, "
          f"{r.mean() - 0.22:+.3f}%.")

    out = os.environ.get("ACC_OUT")
    if out:
        recs = []
        for name, d in sel.groupby("underlying"):
            if len(d) < 8:
                continue
            eq = (1 + d["gap"] / 100).cumprod()
            recs.append({"name": name, "dts": ",".join(str(x)[:10] for x in d["dt"]),
                         "eq": ",".join(f"{v:.4f}" for v in eq)})
        pd.DataFrame(recs).to_csv(out, index=False)
        print(f"\ncurves written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
