"""(A) Build causal DAILY spot bars for the monthly-move study.

Source: 30-minute `underlying_spot_candles` already extracted to CSV by the
panel_2d3d pass (read-only reuse -- this study adds ZERO new PG load; the
extraction those files came from already obeyed the chunk-exclusion rule,
bounding `time` directly with literal UTC timestamps).

If those CSVs are absent, run `extract.py` in this directory instead, which
re-pulls the same thing month-by-month under the same rule.

Daily aggregation: session date = (UTC timestamp + 5h30m).date(), i.e. IST.
open = first bar's open, high = max high, low = min low, close = last bar's
close, volume = sum. Sessions with < 6 thirty-minute bars (a full NSE session
is 13) are dropped as partial/unreliable.
"""
from __future__ import annotations

import glob
import os

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
SRC = os.path.abspath(os.path.join(HERE, "..", "panel_2d3d", "data"))
MIN_BARS_PER_SESSION = 6

os.makedirs(DATA, exist_ok=True)


def build() -> pd.DataFrame:
    files = sorted(glob.glob(os.path.join(SRC, "spot_*.csv")))
    if not files:
        raise SystemExit(f"no spot csvs under {SRC}; run extract.py")
    frames = []
    for f in files:
        df = pd.read_csv(f, parse_dates=["time"])
        frames.append(df)
        print("read", os.path.basename(f), len(df))
    raw = pd.concat(frames, ignore_index=True)
    raw = raw.drop_duplicates(subset=["underlying", "time"])
    raw["session"] = (raw["time"] + pd.Timedelta(hours=5, minutes=30)).dt.date
    raw = raw.sort_values(["underlying", "time"])

    g = raw.groupby(["underlying", "session"], sort=True)
    daily = g.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        nbars=("close", "size"),
    ).reset_index()
    before = len(daily)
    daily = daily[daily["nbars"] >= MIN_BARS_PER_SESSION].copy()
    print(f"sessions {before} -> {len(daily)} after nbars>={MIN_BARS_PER_SESSION}")
    daily["session"] = pd.to_datetime(daily["session"])
    daily = daily.sort_values(["underlying", "session"]).reset_index(drop=True)
    return daily


if __name__ == "__main__":
    d = build()
    out = os.path.join(DATA, "daily.parquet")
    d.to_parquet(out, index=False)
    print("wrote", out, len(d), "rows,", d["underlying"].nunique(), "names")
    print(d["session"].min(), "->", d["session"].max())
