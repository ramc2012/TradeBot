"""Monthly view of the bank complex — was the opportunity there and mistimed?

THE OBSERVATION (owner): the RS-RSI cross did not deliver, yet the ledger shows
the bank instruments themselves moving well over those same periods -- YESBANK
+12.1% and +9.1%, RBLBANK +4.4%. If the moves are real and the signal missed
them, the fault is in the TIMING DEVICE, not in the sector.

So this drops the signal entirely and looks at the raw monthly structure:

  1. MONTHLY GRID -- BANKNIFTY, NIFTY, the RS ratio, and the bank cross-section
     (best / median / worst name) month by month. Read directly: were there
     sustained months, or is the move always a few days inside a flat month?
  2. PERSISTENCE -- does a good month for banks follow a good month? That is
     what decides whether a monthly-horizon rule can exist at all.
  3. DISPERSION -- how far apart are the best and worst bank in a month. This
     is what stock SELECTION is worth; if dispersion is small, picking the right
     bank cannot matter however good the pick.
  4. WHAT WAS LEFT ON THE TABLE -- the ledger's realised spot returns against
     simply holding the month. If holding beats the signal, the signal is
     subtracting value rather than adding it.

    python vanguard/research/bank_monthly.py
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
from research.banknifty_rotation import BANKS, level1  # noqa: E402
from research.overnight_intraday import SESSION_SQL, decompose  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"


def monthly(series: pd.Series) -> pd.Series:
    """Month-end to month-end return of a daily close series."""
    m = series.resample("ME").last()
    return m / m.shift(1) - 1.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=760)
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    start = date.today() - timedelta(days=args.lookback_days)
    connection = psycopg2.connect(args.dsn)
    try:
        raw = pd.read_sql(SESSION_SQL, connection, params={"start": start})
    finally:
        connection.close()

    spot = decompose(raw)
    l1 = level1(spot)
    wide = spot.pivot_table(index="dt", columns="underlying", values="close_last")
    wide.index = pd.to_datetime(wide.index)

    banks = [b for b in BANKS if b in wide]
    bn = monthly(wide["BANKNIFTY"]) * 100
    nf = monthly(wide["NIFTY"]) * 100
    bank_m = pd.DataFrame({b: monthly(wide[b]) * 100 for b in banks})

    # Which months carried a cross-up signal, for the "was it timed" column.
    sig = l1[l1["cross_up"] == True].copy()                          # noqa: E712
    sig["mo"] = pd.to_datetime(sig["dt"]).dt.to_period("M")
    sig_months = sig.groupby("mo").size()

    print(f"window {wide.index.min().date()} .. {wide.index.max().date()}   "
          f"banks={len(banks)}\n")
    print("1. MONTHLY GRID (%). RS = BANKNIFTY minus NIFTY, i.e. the ratio's move.")
    print(f"{'month':<9}{'BANKNIFTY':>10}{'NIFTY':>8}{'RS':>8}"
          f"{'best bank':>11}{'median':>8}{'worst':>8}{'spread':>8}{'signals':>8}")
    grid = []
    for ts in bn.dropna().index:
        row_b = bank_m.loc[ts].dropna()
        if row_b.empty:
            continue
        mo = ts.to_period("M")
        rs = bn.loc[ts] - nf.loc[ts]
        grid.append({"mo": mo, "bn": bn.loc[ts], "nf": nf.loc[ts], "rs": rs,
                     "best": row_b.max(), "med": row_b.median(),
                     "worst": row_b.min(), "spread": row_b.max() - row_b.min(),
                     "sig": int(sig_months.get(mo, 0)),
                     "best_name": row_b.idxmax()})
        g = grid[-1]
        print(f"{str(mo):<9}{g['bn']:>10.1f}{g['nf']:>8.1f}{g['rs']:>8.1f}"
              f"{g['best']:>11.1f}{g['med']:>8.1f}{g['worst']:>8.1f}"
              f"{g['spread']:>8.1f}{g['sig']:>8}")
    gd = pd.DataFrame(grid)

    # ── 2. persistence ─────────────────────────────────────────────────────
    print("\n2. PERSISTENCE — does a good bank month follow a good one?")
    gd["rs_prev"] = gd["rs"].shift(1)
    gd["bn_prev"] = gd["bn"].shift(1)
    for label, prev, cur in (("RS(t-1) > 0  -> RS(t)", "rs_prev", "rs"),
                             ("BANKNIFTY(t-1) > 0 -> BANKNIFTY(t)", "bn_prev", "bn")):
        d = gd.dropna(subset=[prev, cur])
        up = d[d[prev] > 0][cur]
        dn = d[d[prev] <= 0][cur]
        print(f"  {label:<36} after UP  n={len(up):<3} mean={up.mean():+6.2f}%"
              f"   after DOWN n={len(dn):<3} mean={dn.mean():+6.2f}%")
    if len(gd) > 2:
        print(f"  month-over-month autocorrelation of RS: {gd['rs'].autocorr():+.3f}")

    # ── 3. dispersion: what is stock selection worth? ──────────────────────
    print("\n3. DISPERSION — what picking the right bank is worth per month")
    print(f"  median best-worst spread : {gd['spread'].median():.1f}%")
    print(f"  median |BANKNIFTY move|  : {gd['bn'].abs().median():.1f}%")
    print(f"  ratio (selection : direction) = "
          f"{gd['spread'].median() / max(gd['bn'].abs().median(), 1e-9):.1f}x")
    print(f"  best bank beat BANKNIFTY in {(gd['best'] > gd['bn']).mean() * 100:.0f}% of months;"
          f" median outperformance {(gd['best'] - gd['bn']).median():+.1f}%")
    top = gd["best_name"].value_counts().head(5)
    print("  most frequent monthly winner: "
          + ", ".join(f"{k}({v})" for k, v in top.items()))

    # ── 4. what a monthly hold would have made ─────────────────────────────
    print("\n4. LEFT ON THE TABLE — simple monthly holds vs the RS-cross ledger")
    print(f"  hold BANKNIFTY every month     mean={gd['bn'].mean():+.2f}%/mo  "
          f"win={(gd['bn'] > 0).mean() * 100:.0f}%")
    print(f"  hold the bank basket (median)  mean={gd['med'].mean():+.2f}%/mo  "
          f"win={(gd['med'] > 0).mean() * 100:.0f}%")
    print(f"  perfect monthly bank pick      mean={gd['best'].mean():+.2f}%/mo  "
          f"(ceiling, not achievable)")
    sigm = gd[gd["sig"] > 0]
    nosig = gd[gd["sig"] == 0]
    print(f"  months WITH a cross-up signal  n={len(sigm):<3} BANKNIFTY mean="
          f"{sigm['bn'].mean():+.2f}%   bank median={sigm['med'].mean():+.2f}%")
    print(f"  months WITHOUT one             n={len(nosig):<3} BANKNIFTY mean="
          f"{nosig['bn'].mean():+.2f}%   bank median={nosig['med'].mean():+.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
