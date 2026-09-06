"""Daily trend + hourly reversal off the 20h/50h MA — do the best opens do this?

THE HYPOTHESIS (owner, from screen experience): the names that open well are
mostly TRENDING ON THE DAILY chart, and on the HOURLY chart they are turning back
up off their 20- or 50-hour moving average. That is the classic pullback-
continuation: trend intact on the higher timeframe, a lower-timeframe dip into
support, and a reversal off it.

WHY IT IS WORTH TESTING SEPARATELY. Every feature in best_opens.py was
SINGLE-timeframe -- where price closed in TODAY's range, TODAY's RVOL, TODAY's
range expansion. A pullback-to-MA setup is invisible to all of them: it is a
relationship between three timeframes (daily trend, hourly location, the MA), and
a close-location number cannot express it. So the flat winner profile that study
produced is not evidence against this pattern; it never looked.

CONSTRUCTION
  hourly bars   = two 30-minute bars, so the NSE session (09:15-15:30) is 6.25
                  hours and a 20-hour MA spans ~3 sessions, a 50-hour ~8.
  daily trend   = close vs the 20-session MA, and whether that MA is rising.
  hourly setup  = did the session TOUCH the MA (low within TOUCH_BAND) and then
                  CLOSE back on the trend side of it -- touch and reversal, not
                  merely "price is near a line".

Tested two ways, in this order:
  1. DESCRIPTIVE -- do the actual daily winners show the pattern more than the
     losers? This is the owner's claim, checked directly.
  2. PREDICTIVE -- does the pattern, known at the close, pay on the next open?
     A pattern can be true of winners and still useless if it is equally true of
     everything else.

    python vanguard/research/mtf_reversal.py
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
from research.best_opens import build as build_opens  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
TOUCH_BAND = 0.005      # within 0.5% of the MA counts as a touch
DAILY_MA = 20

HOURLY_SQL = """
SELECT underlying, time, open, high, low, close
FROM underlying_spot_candles
WHERE interval = '30minute' AND time >= %(start)s
  AND close IS NOT NULL AND low IS NOT NULL AND high IS NOT NULL
"""


def hourly_features(raw: pd.DataFrame) -> pd.DataFrame:
    """Per (underlying, session): MA distance, touch and reversal flags."""
    raw = raw.sort_values(["underlying", "time"]).copy()
    for col in ("open", "high", "low", "close"):
        raw[col] = raw[col].astype(float)
    raw["dt"] = pd.to_datetime(raw["time"]).dt.tz_convert("Asia/Kolkata").dt.date

    out = []
    for underlying, g in raw.groupby("underlying", sort=False):
        g = g.reset_index(drop=True)
        # Two 30m bars = one hourly bar, chained across sessions so the MA does
        # not restart every morning (a 20-hour MA that resets daily is a 6-hour
        # MA wearing the wrong name).
        idx = np.arange(len(g)) // 2
        hourly = g.groupby(idx).agg(dt=("dt", "last"), high=("high", "max"),
                                    low=("low", "min"), close=("close", "last"))
        hourly["ma20"] = hourly["close"].rolling(20, min_periods=20).mean()
        hourly["ma50"] = hourly["close"].rolling(50, min_periods=50).mean()
        for n in (20, 50):
            ma = hourly[f"ma{n}"]
            # Touch = the hourly LOW came within the band of the MA from above
            # (long case) or the HIGH from below (short case).
            hourly[f"touch_lo{n}"] = (hourly["low"] <= ma * (1 + TOUCH_BAND)) & \
                                     (hourly["close"] > ma)
            hourly[f"touch_hi{n}"] = (hourly["high"] >= ma * (1 - TOUCH_BAND)) & \
                                     (hourly["close"] < ma)
            hourly[f"dist{n}"] = hourly["close"] / ma - 1.0
        # Collapse to the session: did it happen at any hour of the day, and
        # where did the day finish relative to each MA.
        agg = hourly.groupby("dt").agg(
            **{f"touch_lo{n}": (f"touch_lo{n}", "max") for n in (20, 50)},
            **{f"touch_hi{n}": (f"touch_hi{n}", "max") for n in (20, 50)},
            **{f"dist{n}": (f"dist{n}", "last") for n in (20, 50)})
        agg["underlying"] = underlying
        out.append(agg.reset_index())
    return pd.concat(out, ignore_index=True)


def daily_trend(frame: pd.DataFrame) -> pd.DataFrame:
    """Daily-chart trend state, from the same session table."""
    d = frame[["underlying", "dt", "close_last"]].drop_duplicates().sort_values("dt").copy()
    g = d.groupby("underlying")["close_last"]
    d["dma"] = g.transform(lambda s: s.rolling(DAILY_MA, min_periods=DAILY_MA).mean())
    d["dma_prev"] = g.transform(
        lambda s: s.rolling(DAILY_MA, min_periods=DAILY_MA).mean().shift(5))
    d["up_trend"] = (d["close_last"] > d["dma"]) & (d["dma"] > d["dma_prev"])
    d["down_trend"] = (d["close_last"] < d["dma"]) & (d["dma"] < d["dma_prev"])
    return d[["underlying", "dt", "up_trend", "down_trend"]]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=400)
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    start = date.today() - timedelta(days=args.lookback_days)
    connection = psycopg2.connect(args.dsn)
    try:
        m = build_opens(connection, start)
        raw = pd.read_sql(HOURLY_SQL, connection, params={"start": start})
    finally:
        connection.close()

    hourly = hourly_features(raw)
    trend = daily_trend(
        m[["underlying", "dt"]].assign(close_last=m["spot"]).rename(
            columns={"spot": "close_last"}))
    for f in (hourly, trend):
        f["dt"] = pd.to_datetime(f["dt"]).dt.date
    m["dt"] = pd.to_datetime(m["dt"]).dt.date
    m = m.merge(hourly, on=["underlying", "dt"], how="left").merge(
        trend, on=["underlying", "dt"], how="left")

    # THE SETUP, per side. CE wants an uptrend pulling back INTO the MA and
    # closing back above it; PE the mirror.
    for n in (20, 50):
        m[f"setup_ce{n}"] = m["up_trend"].fillna(False) & m[f"touch_lo{n}"].fillna(False)
        m[f"setup_pe{n}"] = m["down_trend"].fillna(False) & m[f"touch_hi{n}"].fillna(False)

    m["rank_pct"] = m.groupby(["dt", "side"])["ret"].rank(pct=True)
    print(f"window {m['dt'].min()} .. {m['dt'].max()}  sessions={m['dt'].nunique()}  "
          f"candidate-nights={len(m):,}\n")

    print("1. DESCRIPTIVE — do the daily WINNERS show the pattern?")
    print(f"  {'side':<5}{'pattern':<26}{'winners %':>11}{'field %':>10}{'losers %':>10}")
    for side, cols in (("CE", ["up_trend", "touch_lo20", "touch_lo50",
                               "setup_ce20", "setup_ce50"]),
                       ("PE", ["down_trend", "touch_hi20", "touch_hi50",
                               "setup_pe20", "setup_pe50"])):
        d = m[m["side"] == side]
        win, lose = d[d["rank_pct"] >= 0.9], d[d["rank_pct"] <= 0.1]
        for col in cols:
            print(f"  {side:<5}{col:<26}{win[col].mean() * 100:>11.1f}"
                  f"{d[col].mean() * 100:>10.1f}{lose[col].mean() * 100:>10.1f}")

    print("\n2. PREDICTIVE — next-open OPTION return when the setup is present")
    print(f"  {'side':<5}{'cell':<30}{'n':>8}{'mean %':>9}{'median %':>10}{'win %':>8}")
    for side, setups in (("CE", ["setup_ce20", "setup_ce50"]),
                         ("PE", ["setup_pe20", "setup_pe50"])):
        d = m[m["side"] == side]
        base = d["ret"]
        print(f"  {side:<5}{'ALL (base rate)':<30}{len(base):>8}"
              f"{base.mean() * 100:>9.2f}{base.median() * 100:>10.2f}"
              f"{(base > 0).mean() * 100:>8.1f}")
        for col in setups:
            s = d[d[col]]["ret"]
            if len(s) < 100:
                print(f"  {side:<5}{col:<30}{len(s):>8}  (too few)")
                continue
            print(f"  {side:<5}{col:<30}{len(s):>8}{s.mean() * 100:>9.2f}"
                  f"{s.median() * 100:>10.2f}{(s > 0).mean() * 100:>8.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
