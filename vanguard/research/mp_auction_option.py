"""The auction read, expressed in BANKNIFTY CE and PE.

WHY THIS TEST AND NOT ANOTHER. mp_auction_test.py ran the full metric set over
707 sessions and found direction in exactly one place: metrics known at 15:15
predict the NEXT OPEN, not the next close --

    close_pos      -> next_open_ret  rho +0.141  t +3.79
    tail_low       -> next_open_ret  rho +0.094  t +2.50   (a buying tail)
    poc_migration  -> next_open_ret  rho +0.093  t +2.47

and nothing at all predicts the rest of the session it belongs to (every
IB-close metric vs rest_ret: |t| <= 1.74). An overnight signal is the one shape
that can survive an option: the gap is a jump, so almost no theta is paid for it.
That is the same overnight-vs-intraday split found earlier in this project, now
arriving through the profile.

THE TRADE: at the 15:15 close, buy the ATM CE when the session closed strong (or
left a buying tail); buy the ATM PE on the mirror. Exit at the 09:15 open.
A same-day-close exit is reported alongside so the overnight portion can be
separated from the intraday portion that historically fights it.

DATA LIMIT, STATED PLAINLY. The owner asked for three years on BANKNIFTY and its
CE/PE. Three years exists for the SPOT profile and is used above. It does NOT
exist for the options: option_premium_candles starts 2025-01 for BANKNIFTY and
carries only 1-6 strikes per month until 2026-03, so a consistent ATM contract is
resolvable for roughly 18 months and a wide chain for about 6. Every option
number here therefore rests on a much shorter sample than the spot work, and is
reported with its own n rather than blended into the three-year figures.

    python vanguard/research/mp_auction_option.py
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

MIN_PREMIUM = 1.0
MAX_ATM_DIST = 0.01
MIN_EXPIRY_DAYS = 3

# ATM CE and PE at the 15:15 bar of the signal session.
ENTRY_SQL = """
SELECT DISTINCT ON (e.underlying, e.dt, o.option_type)
       e.underlying, e.dt, e.spot, o.option_type, o.expiry, o.strike,
       o.close AS prem, o.iv, o.delta, o.volume,
       o.time_to_expiry_years AS tte
FROM ev e
JOIN option_premium_candles o
  ON o.underlying = e.underlying AND o.interval = '30minute'
 AND date(o.time AT TIME ZONE 'Asia/Kolkata') = e.dt
 AND (o.time AT TIME ZONE 'Asia/Kolkata')::time = '15:15'
 AND o.expiry >= e.dt + %(min_exp)s
 AND o.close >= %(min_prem)s AND o.volume > 0
 AND ABS(o.strike - e.spot) / e.spot <= %(max_dist)s
ORDER BY e.underlying, e.dt, o.option_type, o.expiry ASC,
         ABS(o.strike - e.spot) ASC
"""

# The same contract's 09:15 open and 15:15 close on the FOLLOWING session.
EXIT_SQL = """
SELECT c.underlying, c.dt AS ev_dt, c.option_type,
       date(o.time AT TIME ZONE 'Asia/Kolkata') AS dt,
       MAX(o.open)  FILTER (WHERE (o.time AT TIME ZONE 'Asia/Kolkata')::time = '09:15') AS nxt_open,
       MAX(o.close) FILTER (WHERE (o.time AT TIME ZONE 'Asia/Kolkata')::time = '15:15') AS nxt_close
FROM con c
JOIN option_premium_candles o
  ON o.underlying = c.underlying AND o.interval = '30minute'
 AND o.expiry = c.expiry AND o.strike = c.strike AND o.option_type = c.option_type
 AND date(o.time AT TIME ZONE 'Asia/Kolkata') > c.dt
 AND date(o.time AT TIME ZONE 'Asia/Kolkata') <= c.dt + 5
GROUP BY 1, 2, 3, 4
"""


def describe(label: str, d: pd.DataFrame, col: str, floor: int = 25) -> None:
    r = d[col].dropna()
    if len(r) < floor:
        print(f"   {label:<34}{len(r):>6}   (too few)")
        return
    t = r.mean() / (r.std(ddof=1) / np.sqrt(len(r))) if r.std(ddof=1) > 0 else np.nan
    print(f"   {label:<34}{len(r):>6}{r.mean() * 100:>+9.1f}{r.median() * 100:>+9.1f}"
          f"{t:>+7.2f}{(r > 0).mean() * 100:>7.0f}%{r.quantile(0.9) * 100:>+9.0f}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BANKNIFTY")
    parser.add_argument("--years", type=float, default=3.0)
    parser.add_argument("--cost", type=float, default=0.02)
    parser.add_argument("--dsn", default=dsn())
    args = parser.parse_args()

    start = date.today() - timedelta(days=int(args.years * 365.25))
    connection = psycopg2.connect(args.dsn)
    try:
        s = load(connection, [args.symbol], start).dropna(subset=["prev_poc"])
        ev = s[["underlying", "dt", "close"]].rename(columns={"close": "spot"})
        cur = connection.cursor()
        cur.execute("CREATE TEMP TABLE ev (underlying text, dt date, spot numeric)"
                    " ON COMMIT DROP")
        execute_values(cur, "INSERT INTO ev VALUES %s",
                       [(r.underlying, r.dt.date(), float(r.spot))
                        for r in ev.itertuples()])
        cur.execute(f"CREATE TEMP TABLE con ON COMMIT DROP AS {ENTRY_SQL}",
                    {"min_exp": timedelta(days=MIN_EXPIRY_DAYS),
                     "min_prem": MIN_PREMIUM, "max_dist": MAX_ATM_DIST})
        con = pd.read_sql("SELECT * FROM con", connection)
        exits = pd.read_sql(EXIT_SQL, connection)
        cur.close()
    finally:
        connection.rollback()
        connection.close()

    print(f"{args.symbol} spot sessions {len(s):,} "
          f"({s['dt'].min().date()} .. {s['dt'].max().date()})")
    if con.empty:
        print("no ATM contracts resolved -- option coverage too thin")
        return 1
    for c in ("prem", "spot", "strike", "iv", "delta", "tte"):
        con[c] = pd.to_numeric(con[c], errors="coerce")
    con["dt"] = pd.to_datetime(con["dt"])
    exits["ev_dt"], exits["dt"] = pd.to_datetime(exits["ev_dt"]), pd.to_datetime(exits["dt"])
    for c in ("nxt_open", "nxt_close"):
        exits[c] = pd.to_numeric(exits[c], errors="coerce")
    # the NEXT session that actually traded this contract
    nxt = (exits.sort_values(["underlying", "ev_dt", "option_type", "dt"])
           .groupby(["underlying", "ev_dt", "option_type"]).first().reset_index()
           # both frames carry `dt`; keeping two would suffix the join key and
           # break the later merge onto the spot metrics
           .rename(columns={"dt": "nxt_dt"}))

    t = con.merge(nxt, left_on=["underlying", "dt", "option_type"],
                  right_on=["underlying", "ev_dt", "option_type"], how="left")
    t = t.merge(s[["dt", "close_pos", "tail_low", "tail_high", "poc_migration",
                   "day_type", "value_shift", "next_open_ret", "fwd1",
                   "ib_pct_rank"]], on="dt", how="left")
    t["r_overnight"] = t["nxt_open"] / t["prem"] - 1.0 - args.cost
    t["r_nextclose"] = t["nxt_close"] / t["prem"] - 1.0 - args.cost

    print(f"ATM contracts resolved on {t['dt'].nunique():,} sessions "
          f"({t['dt'].min().date()} .. {t['dt'].max().date()}) "
          f"-- NOT the full spot window; option data starts 2025-01 and the "
          f"chain only widens in 2026-03")
    print(f"median premium {t['prem'].median():.1f}  "
          f"premium/spot {(t['prem'] / t['spot']).median() * 100:.2f}%  "
          f"days to expiry {(t['tte'] * 365).median():.0f}")

    ce, pe = t[t["option_type"] == "CE"], t[t["option_type"] == "PE"]
    hdr = (f"   {'cohort':<34}{'n':>6}{'mean%':>9}{'med%':>9}{'t':>7}"
           f"{'win':>7}{'p90%':>9}")

    print(f"\nUNCONDITIONAL: buy at 15:15, sell at the next 09:15 "
          f"(cost {args.cost * 100:.0f}%)")
    print(hdr)
    describe("ATM CE, overnight", ce, "r_overnight")
    describe("ATM PE, overnight", pe, "r_overnight")
    describe("ATM CE, hold to next close", ce, "r_nextclose")
    describe("ATM PE, hold to next close", pe, "r_nextclose")

    # THE SIGNAL: strong close -> CE, weak close -> PE. Top/bottom third by
    # close_pos, which is known at 15:15 and was the only directional metric.
    print(f"\nCONDITIONED ON close_pos (the one metric with a directional t)")
    print(hdr)
    hi = t["close_pos"] >= t["close_pos"].quantile(0.67)
    lo = t["close_pos"] <= t["close_pos"].quantile(0.33)
    describe("closed STRONG -> CE, overnight", t[hi & (t["option_type"] == "CE")], "r_overnight")
    describe("closed WEAK   -> PE, overnight", t[lo & (t["option_type"] == "PE")], "r_overnight")
    describe("  control: closed STRONG -> PE", t[hi & (t["option_type"] == "PE")], "r_overnight")
    describe("  control: closed WEAK   -> CE", t[lo & (t["option_type"] == "CE")], "r_overnight")

    print(f"\nCONDITIONED ON a buying tail at the low (tail_low > 0) -> CE")
    print(hdr)
    bt = t["tail_low"] > 0
    st = t["tail_high"] > 0
    describe("buying tail -> CE, overnight", t[bt & (t["option_type"] == "CE")], "r_overnight")
    describe("selling tail -> PE, overnight", t[st & (t["option_type"] == "PE")], "r_overnight")

    # What the SPOT overnight move was on the same sessions -- the option has to
    # clear its own spread and the gap has to be big enough to matter.
    print(f"\nTHE SPOT MOVE BEHIND IT (why the option may still not pay)")
    sp = t.drop_duplicates("dt")
    print(f"   median |overnight spot move| {sp['next_open_ret'].abs().median() * 100:.2f}%"
          f"   median ATM premium {(sp['prem'] / sp['spot']).median() * 100:.2f}% of spot"
          f"   median |delta| {t['delta'].abs().median():.2f}")
    need = (sp["prem"] / sp["spot"]).median() / max(t["delta"].abs().median(), 1e-9)
    print(f"   breakeven overnight gap needed ~{need * 100:.2f}%  vs typical gap "
          f"{sp['next_open_ret'].abs().median() * 100:.2f}%"
          f"   -> gaps clearing it: "
          f"{(sp['next_open_ret'].abs() > need).mean() * 100:.0f}% of sessions")

    print(f"\nSPLIT-HALF, strong-close CE overnight")
    d = t[hi & (t["option_type"] == "CE")].sort_values("dt")
    h = len(d) // 2
    a, b = d.iloc[:h]["r_overnight"].dropna(), d.iloc[h:]["r_overnight"].dropna()
    if len(a) > 10 and len(b) > 10:
        print(f"   1st half {a.mean() * 100:+6.1f}% (n={len(a)})   "
              f"2nd half {b.mean() * 100:+6.1f}% (n={len(b)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
