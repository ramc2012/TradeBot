"""Does the overnight edge survive being expressed as a CE/PE position?

overnight_intraday.py measures the edge in the UNDERLYING: +12.1 bps per
overnight session at the base rate, +16.1 in the BTST cell. But positions are
taken on options, and three things stand between a spot gap and an option P&L:

  * DELTA -- an ATM contract captures roughly half the spot move.
  * THETA -- one calendar night of decay, which for a near-dated option is
    routinely larger than a 16 bps delta-adjusted gap.
  * SPREAD -- crossed twice, on a premium that is a few percent of spot.

Estimating that chain is guesswork when the premiums are already in the
database, so this measures it: the ACTUAL overnight return of the ATM contract,
entry at the session's last print, exit at the next session's FIRST print --
which is what "buy today, sell tomorrow" means for the instrument.

CE and PE are measured separately and never assumed symmetric. The underlying's
overnight drift is strongly positive (+30.5% annualised), so a PE held overnight
is short that drift before theta is even counted -- the STBT-via-PE question is
whether any setup is bearish enough to overcome it.

    python vanguard/research/btst_option_leg.py
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
from research.atm_tail_study import clean, load, pick_atm  # noqa: E402
from research.overnight_intraday import SESSION_SQL, decompose  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"

# Entry = the ATM contract's LAST print of the session; exit = its FIRST print
# of the next session. Both legs are real prints of the SAME contract, so no
# roll or strike change contaminates the return.
OPT_OVERNIGHT_SQL = """
WITH bars AS (
    SELECT underlying, expiry, strike, option_type AS side,
           date(time AT TIME ZONE 'Asia/Kolkata') AS dt, time, open, close,
           ROW_NUMBER() OVER (PARTITION BY underlying, expiry, strike, option_type,
                              date(time AT TIME ZONE 'Asia/Kolkata')
                              ORDER BY time ASC) AS rn_first,
           ROW_NUMBER() OVER (PARTITION BY underlying, expiry, strike, option_type,
                              date(time AT TIME ZONE 'Asia/Kolkata')
                              ORDER BY time DESC) AS rn_last
    FROM option_premium_candles
    WHERE interval = '30minute' AND time >= %(start)s
      AND open IS NOT NULL AND close IS NOT NULL AND close > 0
)
SELECT underlying, expiry, strike, side, dt,
       MAX(close) FILTER (WHERE rn_last = 1)  AS close_last,
       MAX(open)  FILTER (WHERE rn_first = 1) AS open_first
FROM bars GROUP BY 1, 2, 3, 4, 5
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=400)
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    start = date.today() - timedelta(days=args.lookback_days)
    connection = psycopg2.connect(args.dsn)
    try:
        daily = load(connection, start)
        entries = pick_atm(clean(daily))
        opt = pd.read_sql(OPT_OVERNIGHT_SQL, connection, params={"start": start})
        spot_raw = pd.read_sql(SESSION_SQL, connection, params={"start": start})
    finally:
        connection.close()

    for col in ("close_last", "open_first"):
        opt[col] = opt[col].astype(float)
    key = ["underlying", "expiry", "strike", "side"]
    opt = opt.sort_values("dt")
    # Next session's first print of the SAME contract.
    opt["next_open"] = opt.groupby(key)["open_first"].shift(-1)
    opt["next_dt"] = opt.groupby(key)["dt"].shift(-1)
    opt["opt_overnight"] = opt["next_open"] / opt["close_last"] - 1.0

    spot = decompose(spot_raw)
    for f in (entries, opt, spot):
        f["dt"] = pd.to_datetime(f["dt"]).dt.date
    opt["next_dt"] = pd.to_datetime(opt["next_dt"]).dt.date

    merged = (entries.merge(opt[key + ["dt", "opt_overnight", "next_dt"]],
                            on=key + ["dt"], how="inner")
              .merge(spot[["underlying", "dt", "close_loc", "rvol", "range_exp",
                           "drift", "next_overnight"]],
                     on=["underlying", "dt"], how="left"))
    merged = merged.dropna(subset=["opt_overnight"])
    # Consecutive sessions only: a gap across a holiday or a data hole is more
    # than one night of theta and is not the trade being described.
    cal = {d: i for i, d in enumerate(sorted(spot["dt"].unique()))}
    merged = merged[merged.apply(
        lambda r: cal.get(r["next_dt"], -99) - cal.get(r["dt"], 0) == 1, axis=1)]

    print(f"window {merged['dt'].min()} .. {merged['dt'].max()}  "
          f"ATM overnight legs={len(merged):,}  names={merged['underlying'].nunique()}")
    print("\nATM option OVERNIGHT return (entry last print, exit next first print).")
    print("Underlying base rate for the same nights: +12.1 bps.\n")
    print(f"{'side':>5}{'cell':<44}{'n':>7}{'mean %':>9}{'median %':>10}{'win %':>8}{'t':>7}")

    hot = (merged["rvol"] >= 2.0) & (merged["range_exp"] >= 1.2)
    cells = [
        ("ALL nights (base rate)", pd.Series(True, index=merged.index)),
        ("BTST cell: RVOL>=2, range exp, close top 15%", hot & (merged["close_loc"] >= 0.85)),
        ("  ...and positive overnight drift", hot & (merged["close_loc"] >= 0.85)
         & (merged["drift"] > 0)),
        ("STBT cell: RVOL>=2, range exp, close bot 15%", hot & (merged["close_loc"] <= 0.15)),
        ("  ...and negative overnight drift", hot & (merged["close_loc"] <= 0.15)
         & (merged["drift"] < 0)),
    ]
    for side in ("CE", "PE"):
        for label, mask in cells:
            s = merged[mask & (merged["side"] == side)]["opt_overnight"].dropna()
            if len(s) < 50:
                print(f"{side:>5}{label:<44}{len(s):>7}  (too few)")
                continue
            t = s.mean() / (s.std() / np.sqrt(len(s)))
            print(f"{side:>5}{label:<44}{len(s):>7}{s.mean() * 100:>9.2f}"
                  f"{s.median() * 100:>10.2f}{(s > 0).mean() * 100:>8.1f}{t:>7.1f}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
