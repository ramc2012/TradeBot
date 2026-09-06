"""Market Profile + order-flow structure features, persisted per (name, session).

WHAT THIS IS. The research engine built on 2026-08-28 (research/mp_auction.py,
research/mp_multi_tf.py) run as a lane feature: one row per underlying per
session into `features_mp`, so fusion, research and the UI read profile state
from a table instead of each recomputing TPO profiles.

WHAT THE NUMBERS ARE ENTITLED TO MEAN -- the research verdicts travel with the
code so nobody re-learns them the expensive way:

  RANGE, NOT DIRECTION. ib_width / va_width / atr rank |moves| at IC ~+0.46 and
  signed moves at |t| < 1.8. Use these for SIZING and expectation-setting.
  THE TWO SURVIVING SIGNALS are stored as flags:
    sig_strong_close   close above the value area AND close_pos in [0.70,0.90]
                       -- acceptance, not a spike (the 0.90+ cohort was 43% win).
                       The validated expression is the OVERNIGHT GAP in futures;
                       held longer, the edge is spent by 09:15.
    sig_oversold_mtf   close below the day's AND prior week's AND prior month's
                       value -- the one condition that replicated on NIFTY,
                       BANKNIFTY and the bank stocks (lifts 1.24/1.70/1.44).
                       The path dips before it bounces: a tight stop halves it.
  OMITTED ON PURPOSE: "above all three" MTF alignment (tested: monotonically
  harmful), VWAP/absorption for indices (no volume exists -- fabrication).

ORDER-FLOW PROXIES are computed for names whose bars carry volume (the stocks)
and left NULL with of_available=FALSE elsewhere. They are proxies from 30m bars,
not tick data, and are labelled as such.

    python vanguard/features/m_market_profile.py --lookback-days 200 --write
    python vanguard/features/m_market_profile.py --lookback-days 900 --write   # backfill
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.mp_multi_tf import load_mtf  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"

# The lane's option universe plus the two index anchors. Names outside this list
# cost compute without a tradeable instrument behind them.
UNIVERSE_SQL = """
SELECT DISTINCT underlying FROM underlying_spot_candles
WHERE interval = '30minute' AND time >= NOW() - INTERVAL '30 days'
"""

COLUMNS = (
    "dt", "underlying", "poc", "vah", "val", "va_width_pct", "ib_width_pct",
    "ib_pct_rank", "range_over_ib", "close_pos", "day_type", "poor_high",
    "poor_low", "tail_high_pct", "tail_low_pct", "single_prints", "value_shift",
    "poc_migration_pct", "va_overlap", "failed_high", "failed_low", "w_loc",
    "m_loc", "of_available", "of_delta_share", "of_close_vs_vwap", "of_rvol20",
    "sig_strong_close", "sig_oversold_mtf", "exp_range_pct",
)


def order_flow_proxies(connection, names: list[str], start) -> pd.DataFrame:
    """Session-level flow proxies from 30m bars, volume-bearing names only."""
    q = """
    SELECT underlying, date(time AT TIME ZONE 'Asia/Kolkata') AS dt,
           SUM(CASE WHEN close >= open THEN volume ELSE 0 END) AS up_vol,
           SUM(CASE WHEN close <  open THEN volume ELSE 0 END) AS dn_vol,
           SUM(volume) AS vol,
           SUM(((high + low + close) / 3.0) * volume) AS pv,
           (array_agg(close ORDER BY time DESC))[1] AS last_close
    FROM underlying_spot_candles
    WHERE interval = '30minute' AND time >= %(start)s
      AND underlying = ANY(%(names)s)
      AND (time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15' AND '15:15'
    GROUP BY 1, 2
    """
    f = pd.read_sql(q, connection, params={"start": start, "names": names})
    for c in ("up_vol", "dn_vol", "vol", "pv", "last_close"):
        f[c] = pd.to_numeric(f[c], errors="coerce")
    f["dt"] = pd.to_datetime(f["dt"])
    f = f.sort_values(["underlying", "dt"])
    has_vol = f["vol"] > 0
    f["of_delta_share"] = np.where(
        has_vol, (f["up_vol"] - f["dn_vol"]) / f["vol"].replace(0, np.nan), np.nan)
    vwap = f["pv"] / f["vol"].replace(0, np.nan)
    f["of_close_vs_vwap"] = np.where(
        has_vol, (f["last_close"] / vwap - 1.0) * 100.0, np.nan)
    # trailing mean is LAGGED one session -- the .rolling-includes-its-own-row
    # bug has already cost this project one corrected IC
    trail = (f.groupby("underlying")["vol"]
             .transform(lambda x: x.rolling(20, min_periods=10).mean().shift(1)))
    f["of_rvol20"] = np.where(has_vol, f["vol"] / trail.replace(0, np.nan), np.nan)
    f["of_available"] = has_vol
    return f[["underlying", "dt", "of_available", "of_delta_share",
              "of_close_vs_vwap", "of_rvol20"]]


def build(connection, names: list[str], start) -> pd.DataFrame:
    d = load_mtf(connection, names, start)
    if d.empty:
        return d
    d = d.dropna(subset=["vah", "val", "close"]).copy()
    ref = d["close"].replace(0, np.nan)
    d["va_width_pct"] = (d["vah"] - d["val"]) / ref * 100
    d["ib_width_pct"] = d["ib_width"] * 100
    d["tail_high_pct"] = d["tail_high"] * 100
    d["tail_low_pct"] = d["tail_low"] * 100
    d["poc_migration_pct"] = d["poc_migration"] * 100
    d["exp_range_pct"] = d["atr20"] * 100          # already lagged in mp_auction

    # the two validated flags
    d["sig_strong_close"] = ((d["close"] > d["vah"])
                             & d["close_pos"].between(0.70, 0.90))
    d["sig_oversold_mtf"] = ((d["close"] < d["val"])
                             & d["w_below"].fillna(False)
                             & d["m_below"].fillna(False))

    of = order_flow_proxies(connection, names, start)
    d = d.merge(of, on=["underlying", "dt"], how="left")
    d["of_available"] = d["of_available"].fillna(False)

    out = d[[c for c in COLUMNS if c in d.columns]].copy()
    for c in COLUMNS:
        if c not in out.columns:
            out[c] = None
    # NaN -> None so psycopg2 writes SQL NULLs instead of the string 'NaN'
    out = out[list(COLUMNS)].astype(object).where(pd.notna(out[list(COLUMNS)]), None)
    out["dt"] = [x.date() if hasattr(x, "date") else x for x in d["dt"]]
    return out


def write(connection, frame: pd.DataFrame) -> int:
    rows = [tuple(rec) for rec in frame.itertuples(index=False)]
    if not rows:
        return 0
    with connection.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            f"""INSERT INTO features_mp ({", ".join(COLUMNS)}) VALUES %s
                ON CONFLICT (dt, underlying) DO UPDATE SET
                {", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS[2:])},
                computed_at = NOW()""",
            rows, page_size=500)
    connection.commit()
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=200,
                        help="History loaded; the trailing IB rank needs ~120 "
                             "sessions and the monthly profile a completed month.")
    parser.add_argument("--write-days", type=int, default=5,
                        help="Only the most recent N sessions are written; the "
                             "rest is warm-up for rolling windows.")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL",
                                                        DEFAULT_DSN))
    args = parser.parse_args()

    connection = psycopg2.connect(args.dsn)
    try:
        names = pd.read_sql(UNIVERSE_SQL, connection)["underlying"].tolist()
        start = date.today() - timedelta(days=args.lookback_days)
        frame = build(connection, names, start)
        if frame.empty:
            print("no sessions built")
            return 1
        cutoff = sorted(frame["dt"].unique())[-args.write_days:]
        recent = frame[frame["dt"].isin(cutoff)]
        n_sig = int(pd.Series(recent["sig_strong_close"]).fillna(False).sum())
        n_ovs = int(pd.Series(recent["sig_oversold_mtf"]).fillna(False).sum())
        print(f"built {len(frame):,} rows over {frame['dt'].nunique()} sessions "
              f"({len(names)} names); writing last {len(cutoff)} sessions = "
              f"{len(recent):,} rows | strong_close {n_sig}, oversold_mtf {n_ovs}")
        if args.write:
            print(f"wrote {write(connection, recent):,} rows to features_mp")
        else:
            print("dry run (pass --write)")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
