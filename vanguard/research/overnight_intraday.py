"""Overnight vs intraday return decomposition, and the BTST/STBT screen built on it.

THE ORGANIZING RESULT (Lou, Polk & Skouras, JFE 2019; Cooper et al. earlier):
close-to-open and open-to-close returns behave like different assets. Nearly all
momentum profit accrues OVERNIGHT; intraday flow often fights it. That is why
BTST is a category rather than folklore -- it is a targeted harvest of the
overnight component in names where that drift is strongest.

The 30-minute spot table makes this directly measurable per symbol:

    overnight = first bar's OPEN / previous session's CLOSE - 1
    intraday  = session CLOSE / first bar's OPEN - 1
    total     = (1 + overnight)(1 + intraday) - 1

225 underlyings, 1,349 sessions, back to 2021-06.

WHAT THIS MODULE ANSWERS, in order:
  1. DECOMPOSITION -- how is total return actually split, per name and overall.
  2. PERSISTENCE -- does a name's trailing overnight drift predict its NEXT
     overnight return? That is the edge screen: ranking names by persistent
     positive overnight drift. If it does not predict, BTST name-selection has
     no base to stand on and the rest is timing noise.
  3. THE BTST TRIGGER -- close-location in an expanded range, which the brief
     ranks as the highest-evidence entry: a high-RVOL day closing in the top
     10-15% of its range is unfinished auction business.
  4. STBT -- the mirror, tested separately rather than assumed symmetric.
     Overnight drift is positive on average in equities, so a short-side signal
     must clear a HIGHER bar than its long-side twin, and pooling them would
     hide that.

NOT TESTED HERE, and why: delivery-percentage spike (bhavcopy_delivery holds 5
sessions, 2026-08-20..26 -- unusable) and futures long-buildup confirmation
(index_futures_candles carries 3 index series, no single stocks). Both are real
legs of the brief's stack; neither has data behind it in this database yet.

    python vanguard/research/overnight_intraday.py
    python vanguard/research/overnight_intraday.py --lookback-days 900
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
import psycopg2
from scipy import stats

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.cross_section_ic import aggregate_session_ics, bar_ic  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
DRIFT_WINDOW = 60          # sessions of trailing overnight drift for the screen
RVOL_WINDOW = 20

SESSION_SQL = """
WITH bars AS (
    SELECT underlying, date(time AT TIME ZONE 'Asia/Kolkata') AS dt, time,
           open, high, low, close, volume,
           ROW_NUMBER() OVER (PARTITION BY underlying,
                              date(time AT TIME ZONE 'Asia/Kolkata')
                              ORDER BY time ASC) AS rn_first,
           ROW_NUMBER() OVER (PARTITION BY underlying,
                              date(time AT TIME ZONE 'Asia/Kolkata')
                              ORDER BY time DESC) AS rn_last
    FROM underlying_spot_candles
    WHERE interval = '30minute' AND time >= %(start)s
      AND open IS NOT NULL AND close IS NOT NULL
)
SELECT underlying, dt,
       MAX(open)  FILTER (WHERE rn_first = 1) AS open_first,
       MAX(close) FILTER (WHERE rn_last = 1)  AS close_last,
       MAX(high) AS high, MIN(low) AS low, SUM(volume) AS volume,
       COUNT(*) AS bars
FROM bars GROUP BY 1, 2
"""


def decompose(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.sort_values(["underlying", "dt"]).copy()
    for col in ("open_first", "close_last", "high", "low", "volume"):
        frame[col] = frame[col].astype(float)
    # A session with too few bars is a holiday stub or a feed gap, not a day.
    frame = frame[frame["bars"] >= 8]
    g = frame.groupby("underlying")
    frame["prev_close"] = g["close_last"].shift(1)
    frame = frame[frame["prev_close"] > 0]

    frame["overnight"] = frame["open_first"] / frame["prev_close"] - 1.0
    frame["intraday"] = frame["close_last"] / frame["open_first"] - 1.0
    frame["total"] = frame["close_last"] / frame["prev_close"] - 1.0

    g = frame.groupby("underlying")
    # THE TRIGGER: where in its own range did it close, and was the range busy.
    span = (frame["high"] - frame["low"]).replace(0, np.nan)
    frame["close_loc"] = (frame["close_last"] - frame["low"]) / span
    # A zero trailing median volume makes rvol infinite, which then poisons any
    # mean computed over it. Zero is "no baseline", i.e. unmeasurable, not 0.
    med_vol = g["volume"].transform(
        lambda s: s.rolling(RVOL_WINDOW, min_periods=10).median()).replace(0, np.nan)
    frame["rvol"] = frame["volume"] / med_vol
    med_span = g.apply(
        lambda d: (d["high"] - d["low"]).rolling(RVOL_WINDOW, min_periods=10).median()
    ).reset_index(level=0, drop=True).replace(0, np.nan)
    frame["range_exp"] = span / med_span

    # THE SCREEN: trailing overnight drift, strictly prior sessions only.
    frame["drift"] = g["overnight"].transform(
        lambda s: s.shift(1).rolling(DRIFT_WINDOW, min_periods=30).mean())
    frame["drift_t"] = g["overnight"].transform(
        lambda s: s.shift(1).rolling(DRIFT_WINDOW, min_periods=30).mean()
        / s.shift(1).rolling(DRIFT_WINDOW, min_periods=30).std()
        * np.sqrt(DRIFT_WINDOW))
    # Target: the NEXT session's overnight leg -- what a BTST position earns.
    frame["next_overnight"] = g["overnight"].shift(-1)
    frame["next_total"] = g["total"].shift(-1)
    return frame


def session_ic(frame: pd.DataFrame, feature: str, target: str) -> dict:
    per_session = []
    for _, day in frame.groupby("dt"):
        ic = bar_ic(day[feature], day[target])
        if ic is not None:
            per_session.append(ic)
    return aggregate_session_ics(per_session)


def show_ic(label: str, agg: dict) -> None:
    if agg["mean_ic"] is None:
        print(f"  {label:<44} (no usable cross-sections)")
        return
    t = f"{agg['t_stat']:+.2f}" if agg["t_stat"] is not None else "  n/a"
    print(f"  {label:<44} IC={agg['mean_ic']:+.4f}  t={t:>6}  n={agg['n_sessions']}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=900)
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    start = date.today() - timedelta(days=args.lookback_days)
    connection = psycopg2.connect(args.dsn)
    try:
        raw = pd.read_sql(SESSION_SQL, connection, params={"start": start})
    finally:
        connection.close()
    frame = decompose(raw)
    print(f"window {frame['dt'].min()} .. {frame['dt'].max()}  "
          f"sessions={frame['dt'].nunique()}  names={frame['underlying'].nunique()}  "
          f"rows={len(frame):,}")

    # ── 1. the decomposition ───────────────────────────────────────────────
    print("\n1. DECOMPOSITION — where does the return actually come from?")
    for label, col in (("overnight (close->open)", "overnight"),
                       ("intraday  (open->close)", "intraday"),
                       ("total     (close->close)", "total")):
        s = frame[col].dropna()
        ann = (1 + s.mean()) ** 250 - 1
        t = s.mean() / (s.std() / np.sqrt(len(s)))
        print(f"  {label:<26} mean={s.mean() * 100:+.4f}%/session  "
              f"t={t:+7.1f}  ann~{ann * 100:+.1f}%  share_pos={(s > 0).mean() * 100:.1f}%")

    # ── 2. does trailing overnight drift predict the NEXT overnight leg? ────
    print("\n2. PERSISTENCE — is the overnight drift a real, rankable screen?")
    d = frame.dropna(subset=["drift", "next_overnight"])
    show_ic("trailing 60d overnight drift -> next overnight", session_ic(d, "drift", "next_overnight"))
    show_ic("  ...same drift -> next TOTAL return", session_ic(d, "drift", "next_total"))
    dt_ = frame.dropna(subset=["drift_t", "next_overnight"])
    show_ic("drift t-stat -> next overnight", session_ic(dt_, "drift_t", "next_overnight"))

    print("\n   drift quintile -> mean next-overnight return (bps):")
    d = d.copy()
    d["q"] = d.groupby("dt")["drift"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop")
        if s.nunique() >= 5 else np.nan)
    prof = d.dropna(subset=["q"]).groupby("q")["next_overnight"].agg(["mean", "count"])
    print("   " + "  ".join(f"q{int(i)}:{r['mean'] * 10000:+.1f}" for i, r in prof.iterrows())
          + f"   (n={int(prof['count'].sum()):,})")

    # ── 3. the BTST trigger ────────────────────────────────────────────────
    print("\n3. BTST TRIGGER — close-location in an expanded range -> next overnight")
    b = frame.dropna(subset=["close_loc", "rvol", "range_exp", "next_overnight"])
    show_ic("close_loc (all days)", session_ic(b, "close_loc", "next_overnight"))
    hot = b[(b["rvol"] >= 2.0) & (b["range_exp"] >= 1.2)]
    show_ic("close_loc | RVOL>=2 AND range expanded", session_ic(hot, "close_loc", "next_overnight"))

    print("\n   BTST cell (CE side) vs STBT cell (PE side), next-overnight bps:")
    print(f"   {'cell':<46}{'n':>8}{'mean bps':>11}{'win %':>8}")
    for label, sub in (
        ("ALL days (base rate)", b),
        ("RVOL>=2 & range exp & close in top 15%",
         hot[hot["close_loc"] >= 0.85]),
        ("  ...and positive trailing overnight drift",
         hot[(hot["close_loc"] >= 0.85) & (hot["drift"] > 0)]),
        ("RVOL>=2 & range exp & close in bottom 15%  [STBT]",
         hot[hot["close_loc"] <= 0.15]),
        ("  ...and negative trailing overnight drift  [STBT]",
         hot[(hot["close_loc"] <= 0.15) & (hot["drift"] < 0)]),
    ):
        s = sub["next_overnight"].dropna()
        if len(s) < 100:
            print(f"   {label:<46}{len(s):>8} (too few)")
            continue
        t = s.mean() / (s.std() / np.sqrt(len(s)))
        print(f"   {label:<46}{len(s):>8}{s.mean() * 10000:>11.1f}"
              f"{(s > 0).mean() * 100:>8.1f}   t={t:+.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
