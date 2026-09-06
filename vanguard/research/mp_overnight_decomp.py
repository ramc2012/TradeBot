"""Why the overnight CE lost when the arithmetic says it should not.

THE OWNER'S OBJECTION, and it is correct: 0.25% of BANKNIFTY is ~145 points; at
delta 0.5 that is ~72 points on the option, while 5% of a 500-rupee premium is
25 rupees. The gain outlasts the cost. So why did the measured overnight CE
return -0.9%?

MY EARLIER FRAMING WAS WRONG. I quoted a "breakeven spot move" of premium/delta
~= 3.30%. That is the breakeven AT EXPIRY, where the whole premium must be
recovered. For a ONE-NIGHT hold nothing like that is at risk: the option keeps
its time value and only one day of theta is paid. The correct overnight condition
is much weaker --

    delta x gap   >   one day of theta   +   costs

which the numbers above clear comfortably. The empirical -0.9% was measured from
real premiums, so it stands as an observation; the EXPLANATION was wrong, and a
wrong explanation hides the real cause. This module finds it by attributing the
overnight premium change to its parts, using the greeks stored on each bar:

    actual dP       exit premium - entry premium
    delta leg       delta x (spot gap)               <- what the move earned
    theta leg       theta x (calendar days elapsed)  <- what waiting cost
    vega leg        vega x (IV change)               <- what the market repriced
    residual        gamma, second order, and mis-marks

THE PRIME SUSPECT IS THE EXIT PRICE. The earlier test exited at the option's
09:15 OPEN -- the first print of the day, when spreads on Indian index options
are at their widest and quotes are stale. That is a measurement artefact, not a
market. So the exit is measured three ways: the 09:15 open, the 09:15 bar's
close (09:45, half an hour in), and the theoretical delta-implied price.

COSTS ARE IN RUPEES, NOT PERCENT. A percentage-of-premium cost silently punishes
cheap options hardest, which is backwards -- the spread on an index option is a
few rupees wide regardless of whether the premium is 200 or 900.

    python vanguard/research/mp_overnight_decomp.py
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.mp_auction import dsn, load  # noqa: E402

RANK_WINDOW = 120
MAX_ATM_DIST = 0.005
MIN_PREMIUM = 20.0

ENTRY_SQL = """
SELECT DISTINCT ON (e.underlying, e.dt, o.option_type)
       e.underlying, e.dt, e.spot, e.side, o.option_type, o.expiry, o.strike,
       o.close AS prem, o.iv, o.delta, o.theta, o.vega,
       o.time_to_expiry_years AS tte
FROM ev e
JOIN option_premium_candles o
  ON o.underlying = e.underlying AND o.interval = '30minute'
 AND date(o.time AT TIME ZONE 'Asia/Kolkata') = e.dt
 AND (o.time AT TIME ZONE 'Asia/Kolkata')::time = '15:15'
 AND o.expiry > e.dt
 AND o.close >= %(min_prem)s AND o.volume > 0
 AND ABS(o.strike - e.spot) / e.spot <= %(max_dist)s
ORDER BY e.underlying, e.dt, o.option_type, o.expiry ASC,
         ABS(o.strike - e.spot) ASC
"""

# Only the 09:15 bar, over a short date range. The obvious formulation -- a
# correlated subquery for "the next session this contract traded" -- runs once
# per contract and never returns; picking the earliest session in pandas is the
# same answer in seconds.
EXIT_SQL = """
SELECT c.underlying, c.dt AS ev_dt, c.option_type,
       date(o.time AT TIME ZONE 'Asia/Kolkata') AS nxt_dt,
       o.open AS px_open, o.close AS px_0945,
       o.iv AS iv_open, o.underlying_price AS spot_open
FROM con c
JOIN option_premium_candles o
  ON o.underlying = c.underlying AND o.interval = '30minute'
 AND o.expiry = c.expiry AND o.strike = c.strike AND o.option_type = c.option_type
 AND o.time >= (c.dt + 1)::timestamp AT TIME ZONE 'Asia/Kolkata'
 AND o.time <  (c.dt + 7)::timestamp AT TIME ZONE 'Asia/Kolkata'
WHERE (o.time AT TIME ZONE 'Asia/Kolkata')::time = '09:15'
"""


def show(label: str, r: pd.Series, unit: str = "pts") -> None:
    r = r.dropna()
    if len(r) < 20:
        print(f"   {label:<38}{len(r):>5}   (too few)")
        return
    t = r.mean() / (r.std(ddof=1) / np.sqrt(len(r))) if r.std(ddof=1) > 0 else np.nan
    print(f"   {label:<38}{len(r):>5}{r.mean():>+10.1f}{r.median():>+10.1f}"
          f"{t:>+8.2f}{(r > 0).mean() * 100:>7.0f}%{r.sum():>+10.0f}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BANKNIFTY")
    parser.add_argument("--cost-rs", type=float, default=8.0,
                        help="round-trip cost in rupees per option")
    parser.add_argument("--dsn", default=dsn())
    args = parser.parse_args()

    connection = psycopg2.connect(args.dsn)
    try:
        s = load(connection, [args.symbol], date(2025, 1, 1))
        s = s.sort_values("dt").reset_index(drop=True)
        s["cp_rank"] = (s["close_pos"].rolling(RANK_WINDOW, min_periods=40)
                        .apply(lambda w: (w[-1] > w[:-1]).mean(), raw=True))
        s = s.dropna(subset=["cp_rank", "next_open_ret"])
        s["side"] = np.select([s["cp_rank"] >= 2 / 3, s["cp_rank"] <= 1 / 3], [1, -1], 0)
        ev = s[["underlying", "dt", "close", "side"]].rename(columns={"close": "spot"})
        cur = connection.cursor()
        cur.execute("CREATE TEMP TABLE ev (underlying text, dt date, spot numeric,"
                    " side int) ON COMMIT DROP")
        execute_values(cur, "INSERT INTO ev VALUES %s",
                       [(r.underlying, r.dt.date(), float(r.spot), int(r.side))
                        for r in ev.itertuples()])
        cur.execute(f"CREATE TEMP TABLE con ON COMMIT DROP AS {ENTRY_SQL}",
                    {"min_prem": MIN_PREMIUM, "max_dist": MAX_ATM_DIST})
        con = pd.read_sql("SELECT * FROM con", connection)
        exits = pd.read_sql(EXIT_SQL, connection)
        cur.close()
    finally:
        connection.rollback()
        connection.close()

    for c in ("prem", "spot", "strike", "iv", "delta", "theta", "vega", "tte"):
        con[c] = pd.to_numeric(con[c], errors="coerce")
    con["dt"] = pd.to_datetime(con["dt"])
    exits["ev_dt"] = pd.to_datetime(exits["ev_dt"])
    exits["nxt_dt"] = pd.to_datetime(exits["nxt_dt"])
    for c in ("px_open", "px_0945", "iv_open", "spot_open"):
        exits[c] = pd.to_numeric(exits[c], errors="coerce")

    # the EARLIEST session after the event on which this contract traded
    exits = (exits.sort_values(["underlying", "ev_dt", "option_type", "nxt_dt"])
             .groupby(["underlying", "ev_dt", "option_type"], as_index=False).first())
    t = con.merge(exits, left_on=["underlying", "dt", "option_type"],
                  right_on=["underlying", "ev_dt", "option_type"], how="inner")
    t = t.merge(s[["dt", "next_open_ret", "cp_rank"]], on="dt", how="left")
    t["days"] = (t["nxt_dt"] - t["dt"]).dt.days
    t["gap_pts"] = t["spot"] * t["next_open_ret"]
    t["dte"] = t["tte"] * 365

    # ── the attribution ─────────────────────────────────────────────────────
    t["dP_open"] = t["px_open"] - t["prem"]
    t["dP_0945"] = t["px_0945"] - t["prem"]
    t["leg_delta"] = t["delta"] * t["gap_pts"]
    # theta is stored per YEAR in this table if it is small, per day if large;
    # detect rather than assume, then scale by calendar days actually elapsed
    per_day = t["theta"].abs().median()
    t["theta_day"] = t["theta"] / (365.0 if per_day > 500 else 1.0)
    t["leg_theta"] = t["theta_day"] * t["days"]
    t["d_iv"] = t["iv_open"] - t["iv"]
    t["leg_vega"] = t["vega"] * t["d_iv"]
    t["resid"] = t["dP_open"] - t["leg_delta"] - t["leg_theta"] - t["leg_vega"]

    ce = t[(t["option_type"] == "CE") & (t["side"] == 1)]
    pe = t[(t["option_type"] == "PE") & (t["side"] == -1)]
    print(f"{args.symbol}   strong-close CE trades {len(ce)}   weak-close PE trades {len(pe)}"
          f"   {t['dt'].min().date()} .. {t['dt'].max().date()}")
    print(f"median premium {t['prem'].median():.0f}   median DTE {t['dte'].median():.0f}"
          f"   median |delta| {t['delta'].abs().median():.2f}"
          f"   theta units: {'per year' if per_day > 500 else 'per day'}"
          f"   median theta/day {t['theta_day'].median():.1f}")

    print(f"\nWHAT THE OVERNIGHT PREMIUM CHANGE IS MADE OF  (rupees per option)")
    print(f"   {'leg':<38}{'n':>5}{'mean':>10}{'median':>10}{'t':>8}{'win':>7}{'sum':>10}")
    for label, d in (("STRONG close -> ATM CE", ce), ("WEAK close -> ATM PE", pe)):
        print(f"   -- {label}")
        show("actual dP, exit at 09:15 OPEN", d["dP_open"])
        show("actual dP, exit at 09:45", d["dP_0945"])
        show("  delta leg  (the move)", d["leg_delta"])
        show("  theta leg  (the wait)", d["leg_theta"])
        show("  vega leg   (IV repricing)", d["leg_vega"])
        show("  residual   (gamma + marks)", d["resid"])
        print()

    print(f"IS THE 09:15 OPEN A REAL PRICE?  (the earlier test exited there)")
    for label, d in (("strong CE", ce), ("weak PE", pe)):
        g = d.dropna(subset=["px_open", "px_0945", "leg_delta"])
        if len(g) < 20:
            continue
        theo = g["prem"] + g["leg_delta"] + g["leg_theta"]
        print(f"   {label}: open vs delta-implied {(g['px_open'] - theo).mean():+.1f} Rs"
              f"   09:45 vs delta-implied {(g['px_0945'] - theo).mean():+.1f} Rs"
              f"   open-to-09:45 {(g['px_0945'] - g['px_open']).mean():+.1f} Rs")
    print(f"   median overnight IV change {t['d_iv'].median():+.4f}"
          f"   (vega {t['vega'].median():.1f} per IV point)")

    print(f"\nNET RESULT BY EXIT AND COST  (cost {args.cost_rs:.0f} Rs round trip)")
    print(f"   {'trade':<38}{'n':>5}{'mean':>10}{'median':>10}{'t':>8}{'win':>7}{'sum':>10}")
    for label, d in (("STRONG close -> ATM CE", ce), ("WEAK close -> ATM PE", pe)):
        show(f"{label}, exit 09:15", d["dP_open"] - args.cost_rs)
        show(f"{label}, exit 09:45", d["dP_0945"] - args.cost_rs)
    print(f"\n   as a PERCENT of premium (what the earlier report showed)")
    for label, d in (("STRONG close -> ATM CE", ce), ("WEAK close -> ATM PE", pe)):
        r = ((d["dP_open"] - args.cost_rs) / d["prem"]).dropna()
        r2 = ((d["dP_0945"] - args.cost_rs) / d["prem"]).dropna()
        print(f"   {label:<38}exit 09:15 {r.mean() * 100:+6.1f}%   "
              f"exit 09:45 {r2.mean() * 100:+6.1f}%")

    print(f"\nBY DAYS TO EXPIRY, strong-close CE, exit 09:45, cost {args.cost_rs:.0f} Rs")
    print(f"   {'bucket':<38}{'n':>5}{'mean':>10}{'median':>10}{'t':>8}{'win':>7}{'sum':>10}")
    for lo, hi in ((0, 3), (3, 7), (7, 14), (14, 25), (25, 999)):
        d = ce[(ce["dte"] >= lo) & (ce["dte"] < hi)]
        show(f"{lo}-{hi} DTE  (prem ~{d['prem'].median():.0f})",
             d["dP_0945"] - args.cost_rs)

    print(f"\nWEEKEND DRAG: theta paid over {'/'.join(str(x) for x in sorted(t['days'].unique())[:4])} calendar days")
    for dd in sorted(ce["days"].unique()):
        d = ce[ce["days"] == dd]
        if len(d) < 20:
            continue
        show(f"{dd} calendar day(s) held", d["dP_0945"] - args.cost_rs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
