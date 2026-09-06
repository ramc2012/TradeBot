"""Pullback depth sets the payoff: does turning at the 50-EMA beat the 20-EMA?

THE HYPOTHESIS (owner): these are TRENDING stocks resuming an uptrend after
pulling back to a moving average, and WHERE THEY TURN defines both the strength
and the horizon -- a turn off the 50-day gives a bigger move over a longer
period, a turn off the 20-day a smaller one over less time. Bollinger bands
should therefore be built around the 50-EMA rather than the usual 20-period SMA.

WHY THIS IS THE RIGHT REFINEMENT. run_anatomy.py found runs begin at a LOCAL DIP
in a high-volatility name, and that no feature separated runners from controls
at t-1. But it treated the dip as a single undifferentiated event. If depth
determines payoff, then "dip" was pooling two different setups and averaging
their outcomes -- the same mistake the calendar grid made with timing.

THIS IS PROSPECTIVE, unlike run_anatomy. A touch event is fully observable on
the day it happens, so forward returns from it are tradeable, not descriptive.

DEFINITIONS
    uptrend      close > EMA50 AND EMA50 rising over 10 sessions
    touch20      session LOW pierces the 20-EMA but stays ABOVE the 50-EMA
    touch50      session LOW pierces the 50-EMA (the deeper pullback)
                 -- the two are mutually exclusive by construction, so the
                 comparison is between depths rather than between overlapping
                 sets where every 50-touch is also a 20-touch
    bb50         Bollinger bands centred on the EMA50 with a 50-session SD,
                 as asked: %b50 places the close within that envelope

Only the FIRST touch in a stretch is kept (TOUCH_COOLOFF), or a stock sitting on
its average for a fortnight would register ten identical "events".

    python vanguard/research/ma_pullback.py
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
from research.monthly_pick_v2 import INDICES  # noqa: E402
from research.overnight_intraday import SESSION_SQL, decompose  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
HORIZONS = (5, 10, 20, 40)
TOUCH_COOLOFF = 10
BB_K = 2.0


def build(spot: pd.DataFrame) -> pd.DataFrame:
    spot = spot.sort_values(["underlying", "dt"]).copy()
    spot["dt"] = pd.to_datetime(spot["dt"])
    out = []
    for _, g in spot.groupby("underlying", sort=False):
        g = g.reset_index(drop=True)
        c, l = g["close_last"], g["low"]
        ema20 = c.ewm(span=20, adjust=False).mean()
        ema50 = c.ewm(span=50, adjust=False).mean()
        g["ema20"], g["ema50"] = ema20, ema50

        # BB around the 50-EMA, as asked.
        sd50 = c.rolling(50, min_periods=40).std()
        upper, lower = ema50 + BB_K * sd50, ema50 - BB_K * sd50
        g["bb50_pctb"] = (c - lower) / (upper - lower).replace(0, np.nan)
        g["bb50_width"] = (upper - lower) / ema50.replace(0, np.nan)

        g["uptrend"] = (c > ema50) & (ema50 > ema50.shift(10))
        # Depth of THIS session's dip, measured on the low.
        g["t20"] = (l <= ema20) & (l > ema50)
        g["t50"] = l <= ema50
        for h in HORIZONS:
            g[f"fwd{h}"] = c.shift(-h) / c - 1.0
        # Duration: sessions until the forward peak inside the longest horizon.
        fmax = c.shift(-1).rolling(max(HORIZONS), min_periods=1).max().shift(-max(HORIZONS) + 1)
        g["fwd_peak"] = fmax / c - 1.0
        out.append(g)
    return pd.concat(out, ignore_index=True)


def first_touches(frame: pd.DataFrame, col: str) -> pd.DataFrame:
    """Keep only the first touch in each stretch, per name."""
    keep = []
    for _, g in frame.groupby("underlying", sort=False):
        g = g.reset_index(drop=True)
        last = -TOUCH_COOLOFF - 1
        for i in np.flatnonzero(g[col].values & g["uptrend"].values):
            if i - last > TOUCH_COOLOFF:
                keep.append(g.iloc[i])
                last = i
    return pd.DataFrame(keep)


def report(label: str, d: pd.DataFrame) -> None:
    if len(d) < 30:
        print(f"  {label:<34}{len(d):>7}  (too few)")
        return
    cells = "".join(f"{d[f'fwd{h}'].mean() * 100:>9.2f}" for h in HORIZONS)
    wins = "".join(f"{(d[f'fwd{h}'] > 0).mean() * 100:>7.0f}" for h in HORIZONS)
    print(f"  {label:<34}{len(d):>7}{cells}{wins}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=900)
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    start = date.today() - timedelta(days=args.lookback_days)
    connection = psycopg2.connect(args.dsn)
    try:
        spot_raw = pd.read_sql(SESSION_SQL, connection, params={"start": start})
    finally:
        connection.close()

    feat = build(decompose(spot_raw))
    feat = feat[~feat["underlying"].isin(INDICES)]
    t20 = first_touches(feat, "t20")
    t50 = first_touches(feat, "t50")

    print(f"window {feat['dt'].min().date()} .. {feat['dt'].max().date()}   "
          f"names={feat['underlying'].nunique()}")
    print(f"touch events: 20-EMA={len(t20):,}   50-EMA={len(t50):,}\n")

    hdr = "".join(f"{('fwd' + str(h)):>9}" for h in HORIZONS)
    wh = "".join(f"{('w' + str(h)):>7}" for h in HORIZONS)
    print(f"  {'setup':<34}{'n':>7}{hdr}{wh}")
    report("ALL name-days (control)", feat.dropna(subset=["fwd5"]))
    report("uptrend, no touch", feat[feat["uptrend"] & ~feat["t20"] & ~feat["t50"]])
    report("TOUCH 20-EMA (shallow pullback)", t20)
    report("TOUCH 50-EMA (deep pullback)", t50)

    print("\n  peak move reached inside 40 sessions (the 'strength' claim):")
    for label, d in (("touch 20-EMA", t20), ("touch 50-EMA", t50)):
        s = d["fwd_peak"].dropna()
        if len(s) < 30:
            continue
        print(f"  {label:<34}{len(s):>7}  mean peak {s.mean() * 100:>6.2f}%   "
              f"median {s.median() * 100:>6.2f}%   P(>=10%) {(s >= 0.10).mean() * 100:>5.1f}%"
              f"   P(>=25%) {(s >= 0.25).mean() * 100:>5.1f}%")

    print("\n  split by bb50 %b at the touch (where in the 50-EMA envelope):")
    print(f"  {'setup':<34}{'n':>7}{hdr}{wh}")
    for label, d in (("touch50", t50), ("touch20", t20)):
        dd = d.dropna(subset=["bb50_pctb", "fwd20"])
        if len(dd) < 80:
            continue
        lo = dd[dd["bb50_pctb"] <= dd["bb50_pctb"].quantile(0.33)]
        hi = dd[dd["bb50_pctb"] >= dd["bb50_pctb"].quantile(0.67)]
        report(f"{label}, low %b50 (deep in envelope)", lo)
        report(f"{label}, high %b50", hi)

    print("\n  and by bb50_width (the volatility eligibility from run_anatomy):")
    print(f"  {'setup':<34}{'n':>7}{hdr}{wh}")
    for label, d in (("touch50", t50), ("touch20", t20)):
        dd = d.dropna(subset=["bb50_width", "fwd20"])
        if len(dd) < 80:
            continue
        wide = dd[dd["bb50_width"] >= dd["bb50_width"].quantile(0.67)]
        report(f"{label}, WIDE bb50", wide)
    return 0


if __name__ == "__main__":
    sys.exit(main())
