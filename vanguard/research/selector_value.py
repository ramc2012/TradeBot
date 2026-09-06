"""Does the REST of the decision matrix add anything over RSI + DTE alone?

THE QUESTION. An unconditional mean over every symbol at every bar answers "is
buying options profitable in general", which is theta and always no. The lane
does not trade everything -- it SELECTS. So the only thing worth measuring is
whether a conditioned subset beats the base rate, and specifically whether
flow_score, GEX regime, timing state and sector RS earn their place on top of
the two cheap signals (option RSI and days-to-expiry) that already work.

Every selector is compared INSIDE THE SAME WINDOW as its own base rate. A
selector measured on a different period than its benchmark is measuring the
period.

POWER WARNING, stated up front because it bounds every conclusion here:
features_flow spans ~48 sessions, and a series-life hold needs entries whose
expiry has already passed, so the fully-joined sample is small and the 20x
column will often be 0 or 1 events. Treat P>=10x as the deepest tail this
sample can speak to, and P>=20x as indicative only.

    python vanguard/research/selector_value.py
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.atm_tail_study import (  # noqa: E402
    MULTIPLES, attach_forward, clean, indicators, load, pick_atm,
)

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"

SIGNALS_SQL = """
SELECT f.ts::date AS dt, f.symbol AS underlying,
       f.flow_score, f.n_ingredients,
       r.regime,
       t.timing_state, t.timing_score, t.rvol
FROM features_flow f
LEFT JOIN LATERAL (
    SELECT regime FROM regime r2
    WHERE r2.symbol = f.symbol AND r2.ts::date <= f.ts::date
    ORDER BY r2.ts DESC LIMIT 1) r ON true
LEFT JOIN LATERAL (
    SELECT timing_state, timing_score, rvol FROM timing t2
    WHERE t2.symbol = f.symbol AND t2.ts::date <= f.ts::date
    ORDER BY t2.ts DESC LIMIT 1) t ON true
"""


def report(rows: list[tuple[str, pd.DataFrame]]) -> None:
    print(f"  {'selector':<40}{'n':<8}{'median %':>9}{'mean %':>9}"
          + "".join(f"{f'P>={m:g}x':>9}" for m in MULTIPLES))
    for label, d in rows:
        if len(d) < 100:
            print(f"  {label:<40} n={len(d):<6} (too few to read)")
            continue
        r = d["ret_life"]
        print(f"  {label:<40}{len(d):<8}{r.median() * 100:>9.1f}{r.mean() * 100:>9.1f}"
              + "".join(f"{(r >= m - 1.0).mean() * 100:>9.3f}" for m in MULTIPLES))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=500)
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    start = date.today() - timedelta(days=args.lookback_days)
    connection = psycopg2.connect(args.dsn)
    try:
        daily = load(connection, start)
        signals = pd.read_sql(SIGNALS_SQL, connection)
    finally:
        connection.close()

    feat = indicators(attach_forward(pick_atm(clean(daily)), daily))
    feat = feat.dropna(subset=["ret_life", "rsi"])
    feat = feat[feat["life_sessions"] >= 5]

    signals["dt"] = pd.to_datetime(signals["dt"]).dt.date
    feat["dt"] = pd.to_datetime(feat["dt"]).dt.date
    joined = feat.merge(signals, on=["dt", "underlying"], how="inner")
    if joined.empty:
        print("no overlap between option entries and the signal tables")
        return 1

    print(f"joined window {joined['dt'].min()} .. {joined['dt'].max()}   "
          f"entries={len(joined):,}  underlyings={joined['underlying'].nunique()}  "
          f"sessions={joined['dt'].nunique()}")
    print("\nEvery row below is the SAME window, so the base rate is comparable.\n")

    rsi_lo = joined["rsi"].quantile(0.2)
    dte_ok = joined["dte"].between(21, 60)
    base = joined[dte_ok]
    cheap = joined[dte_ok & (joined["rsi"] <= rsi_lo)]

    rows = [
        ("ALL (trade everything, this window)", joined),
        ("DTE 21-60 only", base),
        ("+ RSI bottom 20%  [the cheap pair]", cheap),
    ]
    # Does each extra signal improve on the cheap pair?
    if cheap["flow_score"].notna().any():
        rows.append(("  + |flow_score| >= 60", cheap[cheap["flow_score"].abs() >= 60]))
        rows.append(("  + n_ingredients >= 2", cheap[cheap["n_ingredients"] >= 2]))
    if cheap["regime"].notna().any():
        rows.append(("  + regime NEG/STRONG_NEG/NEUTRAL",
                     cheap[cheap["regime"].isin(["NEG", "STRONG_NEG", "NEUTRAL"])]))
    if cheap["timing_state"].notna().any():
        rows.append(("  + timing IGNITION", cheap[cheap["timing_state"] == "IGNITION"]))
        rows.append(("  + rvol >= 1.5", cheap[cheap["rvol"] >= 1.5]))
    report(rows)

    print("\nAnd the same extra signals WITHOUT the cheap pair, so their own\n"
          "contribution is visible rather than credited to RSI+DTE:")
    solo = [("ALL (trade everything, this window)", joined)]
    if joined["flow_score"].notna().any():
        solo.append(("|flow_score| >= 60 alone", joined[joined["flow_score"].abs() >= 60]))
    if joined["regime"].notna().any():
        solo.append(("regime NEG/STRONG_NEG/NEUTRAL alone",
                     joined[joined["regime"].isin(["NEG", "STRONG_NEG", "NEUTRAL"])]))
    if joined["timing_state"].notna().any():
        solo.append(("timing IGNITION alone", joined[joined["timing_state"] == "IGNITION"]))
        solo.append(("rvol >= 1.5 alone", joined[joined["rvol"] >= 1.5]))
    report(solo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
