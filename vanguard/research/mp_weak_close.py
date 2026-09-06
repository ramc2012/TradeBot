"""The WEAK-close short side, given the same treatment as the strong side.

Plus the reconciliation the option decomposition forced: the strong-close signal
in the 18-month option window is about HALF the strength of its five-year self,
which is why the option test lost money while the owner's arithmetic said it
should not.

THE CORRECTED OVERNIGHT ARITHMETIC (my earlier "3.30% breakeven" was wrong -- it
was the breakeven AT EXPIRY, where the whole premium must be recovered):

    overnight P&L  ~  delta x gap  -  theta x days  -  cost

    delta 0.50, theta 17.9 Rs/night, cost 8 Rs
    -> required gap = (17.9 + 8) / 0.50 = ~52 index points
    -> five-year median strong-close gap  = +74.3 points   CLEARS IT
    -> option-window median gap           = ~39 points     DOES NOT

Same signal, different regime. That is the whole discrepancy, and it is a
statement about the sample, not about the arithmetic.

    python vanguard/research/mp_weak_close.py
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
OPTION_START = pd.Timestamp("2025-03-24")


def stats(r: pd.Series, span: float) -> dict:
    r = r.dropna()
    eq = (1 + r).cumprod()
    dd = eq / eq.cummax() - 1.0
    t = r.mean() / (r.std(ddof=1) / np.sqrt(len(r))) if r.std(ddof=1) > 0 else np.nan
    return {"n": len(r), "total": eq.iloc[-1] - 1.0,
            "cagr": eq.iloc[-1] ** (1 / span) - 1.0,
            "mean": r.mean(), "t": t, "maxdd": dd.min(), "win": (r > 0).mean(),
            "sharpe": r.mean() / r.std(ddof=1) * np.sqrt(len(r) / span), "eq": eq}


def row(label: str, c: dict) -> None:
    print(f"   {label:<32}{c['n']:>6}{c['total'] * 100:>+10.1f}{c['cagr'] * 100:>+8.1f}"
          f"{c['maxdd'] * 100:>+9.1f}{c['sharpe']:>+8.2f}{c['win'] * 100:>6.0f}%{c['t']:>+7.2f}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BANKNIFTY")
    parser.add_argument("--years", type=float, default=5.2)
    parser.add_argument("--dsn", default=dsn())
    args = parser.parse_args()

    connection = psycopg2.connect(args.dsn)
    try:
        s = load(connection, [args.symbol],
                 date.today() - timedelta(days=int(args.years * 365.25)))
    finally:
        connection.close()
    s = s.sort_values("dt").reset_index(drop=True)
    s["cp_rank"] = (s["close_pos"].rolling(RANK_WINDOW, min_periods=60)
                    .apply(lambda w: (w[-1] > w[:-1]).mean(), raw=True))
    d = s.dropna(subset=["cp_rank", "next_open_ret"]).reset_index(drop=True)
    d["gap_pts"] = d["close"] * d["next_open_ret"]
    d["strong"] = d["cp_rank"] >= 2 / 3
    d["weak"] = d["cp_rank"] <= 1 / 3
    span = (d["dt"].max() - d["dt"].min()).days / 365.25

    print(f"{args.symbol}  {len(d):,} sessions  {d['dt'].min().date()} .. "
          f"{d['dt'].max().date()}  ({span:.1f}y)")

    # ── 1. THE WEAK-CLOSE SHORT, SAME TREATMENT AS THE STRONG SIDE ─────────
    print("\n1. WEAK CLOSE, SHORT OVERNIGHT — compounded")
    print(f"   {'strategy':<32}{'n':>6}{'total%':>10}{'CAGR%':>8}"
          f"{'maxDD%':>9}{'Sharpe':>8}{'win':>6}{'t':>7}")
    weak_short = stats(-d.loc[d["weak"], "next_open_ret"], span)
    strong_long = stats(d.loc[d["strong"], "next_open_ret"], span)
    row("SHORT on weak close", weak_short)
    row("LONG on strong close (ref)", strong_long)
    row("SHORT on weak, 5bp cost",
        stats(-d.loc[d["weak"], "next_open_ret"] - 0.0005, span))
    row("LONG on weak close (wrong way)", stats(d.loc[d["weak"], "next_open_ret"], span))

    w = d.loc[d["weak"], "gap_pts"]
    print(f"\n   in points: shorting weak closes earns {-w.sum():+,.0f} points over "
          f"{len(w)} nights ({-w.mean():+.1f}/night, median {-w.median():+.1f})")
    print(f"   the median weak night is {w.median():+.1f} points — POSITIVE. The short "
          f"side\n   makes its money in the left tail, not in the typical night, which is "
          f"the\n   opposite of how the long side works.")

    print(f"\n   weak-close short, by year")
    print(f"   {'year':<8}{'nights':>8}{'points':>10}{'pts/night':>11}{'win':>7}")
    for y, g in d[d["weak"]].groupby(d["dt"].dt.year):
        print(f"   {y:<8}{len(g):>8}{-g['gap_pts'].sum():>+10.0f}"
              f"{-g['gap_pts'].mean():>+11.1f}{(g['gap_pts'] < 0).mean() * 100:>6.0f}%")

    # does a weak close predict the big DOWN gaps, the way strong predicts up?
    base = d["weak"].mean()
    print(f"\n   does a WEAK close predict big DOWN gaps? (base rate {base * 100:.0f}%)")
    from math import comb
    dn = d.nsmallest(50, "gap_pts")
    for n in (5, 10, 20, 50):
        g = dn.head(n)
        k = int(g["weak"].sum())
        p = float(sum(comb(n, i) * base ** i * (1 - base) ** (n - i)
                      for i in range(k, n + 1)))
        print(f"   top {n:<3} DOWN gaps: {k:>2} weak of {n}  ({k / n * 100:>3.0f}% vs "
              f"{base * 100:.0f}% expected)  P(>=k)={p:.4f}  "
              f"points {-g.loc[g['weak'], 'gap_pts'].sum():>+7.0f}")

    # ── 2. WHY THE OPTION TEST DISAGREED WITH THE ARITHMETIC ───────────────
    print(f"\n2. THE SIGNAL IS HALF AS STRONG IN THE OPTION WINDOW")
    print(f"   {'window':<34}{'nights':>8}{'mean pts':>10}{'median':>9}"
          f"{'win':>7}{'index':>9}")
    for label, m in (("full 5 years", pd.Series(True, index=d.index)),
                     ("before option data (pre-2025-03)", d["dt"] < OPTION_START),
                     ("option window (2025-03 on)", d["dt"] >= OPTION_START)):
        g = d[m & d["strong"]]
        print(f"   {label:<34}{len(g):>8}{g['gap_pts'].mean():>+10.1f}"
              f"{g['gap_pts'].median():>+9.1f}{(g['gap_pts'] > 0).mean() * 100:>6.0f}%"
              f"{g['close'].mean():>9,.0f}")

    theta, cost, delta = 17.9, 8.0, 0.50
    need = (theta + cost) / delta
    print(f"\n   OVERNIGHT breakeven for an ATM option (NOT the expiry breakeven):")
    print(f"     delta x gap  >  theta x days + cost")
    print(f"     ({theta:.1f} + {cost:.0f}) / {delta:.2f} = {need:.0f} index points needed "
          f"for a 1-night hold")
    for label, m in (("full 5 years", pd.Series(True, index=d.index)),
                     ("option window", d["dt"] >= OPTION_START)):
        g = d[m & d["strong"]]["gap_pts"]
        print(f"     {label:<20} median gap {g.median():+6.1f} pts   "
              f"clears {need:.0f}pts on {(g > need).mean() * 100:.0f}% of nights   "
              f"expected P&L {(g.median() * delta - theta - cost):+.0f} Rs")
    print(f"   My earlier '3.30% breakeven' was the EXPIRY breakeven (premium/delta)\n"
          f"   and was the wrong test for a one-night hold. The corrected bar is ~"
          f"{need:.0f} points,\n   which the five-year median clears and the option-window "
          f"median does not.")

    out = os.environ.get("WEAK_OUT")
    if out:
        e = pd.DataFrame({
            "dt": d["dt"],
            "weak_short": np.where(d["weak"], -d["next_open_ret"], 0.0),
            "strong_long": np.where(d["strong"], d["next_open_ret"], 0.0)})
        e["weak_eq"] = (1 + e["weak_short"]).cumprod()
        e["strong_eq"] = (1 + e["strong_long"]).cumprod()
        e[["dt", "weak_eq", "strong_eq"]].to_csv(out, index=False)
        print(f"\ncurve written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
