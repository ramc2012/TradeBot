"""Equity curves for the best rule on NIFTY, BANKNIFTY and the bank stocks.

WHICH RULE IS "BEST", AND WHY. The contrarian condition -- a close BELOW daily,
weekly AND monthly value -- is the only one in this whole study that replicated
across three instruments with the same sign and comparable lift (1.24 / 1.70 /
1.44), and it is positive in essentially every calendar year on each. The
continuation rules did not survive that test: the winner flipped between
BANKNIFTY and NIFTY, which are ~0.9 correlated. So the contrarian rule is the
headline curve for all three, and BANKNIFTY's best continuation rule is carried
alongside as a comparison rather than as a recommendation.

NO STOP. The stop test showed a tight stop costs about half the edge -- the path
dips before it bounces, which is what an oversold entry is. So these hold to the
day-4 close. The drawdown column is where that decision shows up, and it is the
honest cost of removing the stop.

HOW THE EQUITY IS BUILT, because overlap matters and is usually fudged. Each
signal opens a position held for HOLD sessions. The portfolio is marked DAILY:
each session's return is the equal-weighted mean of whatever positions are open
that day, and zero when flat. That handles overlapping trades correctly for the
16-stock basket, keeps a single instrument from silently holding two positions,
and puts calendar time on the x-axis so the three curves are comparable.

    python vanguard/research/mp_best_curves.py
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

HOLD = 4


def daily_equity(d: pd.DataFrame, sig: pd.Series, hold: int = HOLD) -> pd.Series:
    """Mark-to-market: each day, the mean daily return of all open positions."""
    d = d.sort_values(["underlying", "dt"]).reset_index(drop=True)
    d["dret"] = d.groupby("underlying")["close"].pct_change() * 100
    # a signal on session i means positions are open on i+1 .. i+hold
    open_flag = pd.Series(False, index=d.index)
    for name, g in d.groupby("underlying", sort=False):
        pos = g.index.to_numpy()
        fired = pos[sig.reindex(pos).fillna(False).to_numpy()]
        for f in fired:
            lo = np.searchsorted(pos, f) + 1
            for j in pos[lo:lo + hold]:
                open_flag.at[j] = True
    live = d[open_flag]
    per_day = live.groupby("dt")["dret"].mean()
    allday = pd.Series(0.0, index=sorted(d["dt"].unique()))
    allday.update(per_day)
    return allday


def describe(lab: str, r: pd.Series, span: float, n_trades: int) -> dict:
    eq = (1 + r / 100).cumprod()
    dd = (eq / eq.cummax() - 1)
    exposure = (r != 0).mean()
    sd = r.std(ddof=1)
    print(f"   {lab:<30}{n_trades:>7}{exposure * 100:>8.0f}%"
          f"{(eq.iloc[-1] - 1) * 100:>+10.1f}{(eq.iloc[-1] ** (252 / len(r)) - 1) * 100:>+8.1f}"
          f"{dd.min() * 100:>+8.1f}{r.mean() * np.sqrt(252) / sd if sd > 0 else np.nan:>8.2f}")
    return {"eq": eq, "dd": dd}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=float, default=5.2)
    parser.add_argument("--dsn", default=dsn())
    args = parser.parse_args()
    start = date.today() - timedelta(days=int(args.years * 365.25))

    connection = psycopg2.connect(args.dsn)
    out = {}
    try:
        print(f"   {'curve':<30}{'trades':>7}{'expo':>8}{'total %':>10}"
              f"{'CAGR %':>8}{'maxDD':>8}{'Sharpe':>8}")
        for label, names in (("NIFTY", ["NIFTY"]), ("BANKNIFTY", ["BANKNIFTY"]),
                             ("BANK STOCKS", list(BANKS))):
            d = targets(load_mtf(connection, names, start))
            d = d.dropna(subset=["w_vah", "m_vah"]).reset_index(drop=True)
            below3 = ((d["close"] < d["val"]) & d["w_below"] & d["m_below"]).fillna(False)
            r = daily_equity(d, below3)
            out[f"{label}: below all three"] = describe(
                f"{label}: below all 3", r, args.years, int(below3.sum()))
            if label == "BANKNIFTY":
                cont = ((d["value_shift"] == "higher_outside")
                        & (d["close"] > d["vah"])).fillna(False)
                rc = daily_equity(d, cont)
                out[f"{label}: higher_outside+VAH"] = describe(
                    f"{label}: higher_out+VAH", rc, args.years, int(cont.sum()))
            # buy & hold reference on the same calendar
            px = d.groupby("dt")["close"].mean()
            bh = px / px.iloc[0]
            bdd = (bh / bh.cummax() - 1).min()
            # annualise over THIS instrument's own span -- the bank stocks cover
            # ~1.4 years, the indices ~5.1, and using one figure for both made
            # the stock comparison meaningless
            yrs = (pd.Timestamp(px.index[-1]) - pd.Timestamp(px.index[0])).days / 365.25
            print(f"   {label + ': buy & hold':<30}{'-':>7}{'100%':>8}"
                  f"{(bh.iloc[-1] - 1) * 100:>+10.1f}"
                  f"{(bh.iloc[-1] ** (1 / yrs) - 1) * 100:>+8.1f}{bdd * 100:>+8.1f}"
                  f"{'':>8}  ({yrs:.1f}y)")
            out[f"{label}: buy & hold"] = {"eq": bh, "dd": None}
    finally:
        connection.close()

    print("\n   'expo' is the share of sessions with a position open — these curves are\n"
          "   flat most of the time, which is why the drawdowns are small relative to\n"
          "   buy & hold despite carrying no stop.")

    dest = os.environ.get("BEST_OUT")
    if dest:
        recs = []
        for k, v in out.items():
            eq = v["eq"]
            recs.append({"case": k, "dts": ",".join(str(x)[:10] for x in eq.index),
                         "eq": ",".join(f"{x:.4f}" for x in eq.values)})
        pd.DataFrame(recs).to_csv(dest, index=False)
        print(f"\ncurves written to {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
