"""Futures OI baselines from the stitched front-contract daily series.

Reads `stock_futures_daily` (ingest/futures_oi.py), stitches a per-symbol
front-contract series (nearest unexpired expiry per session — the
research/mp_futures.py ROW_NUMBER pattern), and writes `futures_oi_baselines`:

  - d_oi / d_oi_pct / d_price_pct across consecutive sessions SHARING an
    expiry; an expiry change is a rollover row (is_rollover=true, deltas
    NULL) — consecutive OI levels across a roll are incomparable
    (m2_flow doctrine).
  - Rolling z-scores over BASELINE_WINDOW non-rollover sessions
    (d_oi_pct_z, volume_z, oi_z) and a rolling OI percentile. Every signal
    threshold is z/percentile-relative; no absolute OI level is a gate.
  - oi_state via features/m2_flow.classify_oi_state (the lane's canonical
    4-state buildup classifier).
  - activity_surge: OI build WITH participation — both d_oi_pct_z and
    volume_z at or above SURGE_Z.

    python vanguard/features/m_futures_oi.py --lookback-days 90
    python vanguard/features/m_futures_oi.py --backfill   # full history
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
from features.m2_flow import classify_oi_state  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"

BASELINE_WINDOW = 60      # trading sessions in the rolling z / percentile window
MIN_WINDOW = 20           # below this many observations, z-scores stay NULL
SURGE_Z = 1.5             # both d_oi_pct_z and volume_z must reach this

FRONT_SERIES_SQL = """
WITH ranked AS (
    SELECT symbol, ts, expiry, close, volume, oi,
           ROW_NUMBER() OVER (
               PARTITION BY symbol, ts
               ORDER BY (expiry < ts), expiry
           ) AS rn
    FROM stock_futures_daily
    WHERE ts >= %(start)s AND ts <= %(end)s
      AND close IS NOT NULL AND oi IS NOT NULL AND oi > 0
)
SELECT symbol, ts, expiry, close, volume, oi
FROM ranked WHERE rn = 1
ORDER BY symbol, ts
"""

UPSERT_SQL = """
INSERT INTO futures_oi_baselines
    (symbol, ts, expiry, close, d_price_pct, oi, d_oi, d_oi_pct, d_oi_pct_z,
     oi_z, volume_z, oi_pctile, oi_state, activity_surge, is_rollover,
     lookback_sessions)
VALUES %s
ON CONFLICT (symbol, ts) DO UPDATE SET
    expiry = EXCLUDED.expiry, close = EXCLUDED.close,
    d_price_pct = EXCLUDED.d_price_pct, oi = EXCLUDED.oi,
    d_oi = EXCLUDED.d_oi, d_oi_pct = EXCLUDED.d_oi_pct,
    d_oi_pct_z = EXCLUDED.d_oi_pct_z, oi_z = EXCLUDED.oi_z,
    volume_z = EXCLUDED.volume_z, oi_pctile = EXCLUDED.oi_pctile,
    oi_state = EXCLUDED.oi_state, activity_surge = EXCLUDED.activity_surge,
    is_rollover = EXCLUDED.is_rollover,
    lookback_sessions = EXCLUDED.lookback_sessions,
    computed_at = now()
"""


def _rolling_z(series: pd.Series, window: int, min_periods: int) -> pd.Series:
    """Z of the CURRENT value against the TRAILING window (shifted: the value
    being scored is excluded from its own baseline)."""
    mean = series.shift(1).rolling(window, min_periods=min_periods).mean()
    std = series.shift(1).rolling(window, min_periods=min_periods).std(ddof=0)
    return (series - mean) / std.replace(0.0, np.nan)


def _rolling_pctile(series: pd.Series, window: int, min_periods: int) -> pd.Series:
    return series.rolling(window, min_periods=min_periods).apply(
        lambda w: float((w <= w[-1]).mean()), raw=True)


def compute_baselines(frame: pd.DataFrame,
                      window: int = BASELINE_WINDOW,
                      min_window: int = MIN_WINDOW,
                      surge_z: float = SURGE_Z) -> pd.DataFrame:
    """frame: front-contract rows for ONE symbol, ascending ts."""
    frame = frame.sort_values("ts").reset_index(drop=True).copy()
    rollover = frame["expiry"].ne(frame["expiry"].shift(1)) & frame.index.to_series().gt(0)
    frame["is_rollover"] = rollover

    d_oi = frame["oi"].diff()
    d_price = frame["close"].diff()
    d_oi[rollover] = np.nan
    d_price[rollover] = np.nan
    frame["d_oi"] = d_oi
    frame["d_oi_pct"] = d_oi / frame["oi"].shift(1).replace(0, np.nan) * 100.0
    frame["d_price_pct"] = d_price / frame["close"].shift(1).replace(0, np.nan) * 100.0

    frame["d_oi_pct_z"] = _rolling_z(frame["d_oi_pct"], window, min_window)
    frame["volume_z"] = _rolling_z(frame["volume"].astype(float), window, min_window)
    frame["oi_z"] = _rolling_z(frame["oi"].astype(float), window, min_window)
    frame["oi_pctile"] = _rolling_pctile(frame["oi"].astype(float), window, min_window)

    frame["oi_state"] = [
        None if is_roll else classify_oi_state(
            None if pd.isna(doi) else float(doi),
            None if pd.isna(dp) else float(dp))
        for is_roll, doi, dp in zip(frame["is_rollover"], frame["d_oi"], d_price)
    ]
    frame["activity_surge"] = (
        (frame["d_oi_pct_z"] >= surge_z) & (frame["volume_z"] >= surge_z)
    ).fillna(False)
    frame["lookback_sessions"] = (frame["d_oi_pct"].shift(1).rolling(
        window, min_periods=0).count().fillna(0).astype(int))
    return frame


def run(dsn: str, lookback_days: int, backfill: bool) -> dict:
    end = date.today()
    # The z-window needs BASELINE_WINDOW sessions of warm-up history before
    # the first scored row, so always load extra calendar days behind the
    # write floor.
    write_floor = date(2000, 1, 1) if backfill else end - timedelta(days=lookback_days)
    load_start = date(2000, 1, 1) if backfill else write_floor - timedelta(days=200)

    connection = psycopg2.connect(dsn)
    try:
        frame = pd.read_sql(FRONT_SERIES_SQL, connection,
                            params={"start": load_start, "end": end})
        if frame.empty:
            print("[m-futures-oi] no rows in stock_futures_daily for the window; "
                  "run ingest/futures_oi.py first")
            return {"written": 0, "symbols": 0}

        out_frames = [compute_baselines(g) for _, g in frame.groupby("symbol", sort=True)]
        result = pd.concat(out_frames, ignore_index=True)
        result = result[result["ts"] >= write_floor]

        def _n(v):
            return None if pd.isna(v) else v

        values = [
            (r.symbol, r.ts, r.expiry, _n(r.close), _n(r.d_price_pct), _n(r.oi),
             None if pd.isna(r.d_oi) else int(r.d_oi), _n(r.d_oi_pct),
             _n(r.d_oi_pct_z), _n(r.oi_z), _n(r.volume_z), _n(r.oi_pctile),
             r.oi_state, bool(r.activity_surge), bool(r.is_rollover),
             int(r.lookback_sessions))
            for r in result.itertuples()
        ]
        with connection.cursor() as cur:
            psycopg2.extras.execute_values(cur, UPSERT_SQL, values, page_size=1000)
        connection.commit()
        return {"written": len(values), "symbols": result["symbol"].nunique(),
                "latest": result["ts"].max(), "result": result}
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--backfill", action="store_true",
                        help="recompute the full history instead of the recent window")
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    summary = run(args.dsn, args.lookback_days, args.backfill)
    if summary["written"] == 0:
        return 0
    result = summary.pop("result")
    latest = result[result["ts"] == result["ts"].max()]
    states = latest["oi_state"].value_counts(dropna=True).to_dict()
    surges = latest[latest["activity_surge"]]

    print(f"[m-futures-oi] wrote {summary['written']:,} rows, "
          f"{summary['symbols']} symbols, latest session {summary['latest']}")
    print(f"latest-session states: {states}")
    print(f"activity surges today: {len(surges)}"
          + (f" -> {sorted(surges['symbol'].tolist())[:15]}" if len(surges) else ""))
    top = latest.reindex(latest["d_oi_pct_z"].abs().sort_values(ascending=False).index).head(8)
    cols = ["symbol", "close", "d_price_pct", "oi", "d_oi_pct", "d_oi_pct_z",
            "volume_z", "oi_pctile", "oi_state", "activity_surge"]
    with pd.option_context("display.width", 180):
        print(top[cols].to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
