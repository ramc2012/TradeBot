"""Sector rotation timed by the RS ratio's RSI crossing its own MA.

THE RULE (owner, reading the sector/NIFTY ratio charts): buy the sector whose
RS-ratio RSI crosses ABOVE its own moving average, sell the one whose RS-RSI
crosses BELOW -- "buy banks and finance and sell IT in June".

WHY THIS IS NOT WHAT sector_rotation.py TESTED. That module classified a sector
by a LEVEL: is the ratio under its 20-session MA with the MA falling. A level
says "this sector has been weak". A CROSS says "this sector is turning right
now" -- it is a signal on the derivative, and it fires at a specific moment
rather than describing a persistent state. The level test found mean reversion
(laggards outperform); it could not have found a rotation TIMING effect,
because a sector is "lagging" for weeks either side of the turn.

CONSTRUCTION, matching the charts:
    RS ratio  = chained sector index / chained NIFTY index
    RSI(14)   on that ratio
    signal MA = SMA(14) of the RSI itself   (the second line on those panels)
    cross up   = RSI was <= its MA yesterday and is > today   -> CE on that sector
    cross down = RSI was >= its MA yesterday and is < today   -> PE on that sector

Tested at several horizons, because a rotation call is not a next-open call:
the owner's example spans weeks, so the next-open leg is reported alongside
5- and 10-session option holds.

    python vanguard/research/rs_rsi_cross.py
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
from research.overnight_intraday import SESSION_SQL, decompose  # noqa: E402
from research.sector_rotation import TAXONOMY_SQL, sector_series  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
RSI_LEN = 14
SIGNAL_LEN = 14
# Sessions after the cross that a position is still considered "on signal".
FRESH_WINDOW = 3


def wilder_rsi(series: pd.Series, length: int = RSI_LEN) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(100.0)


def add_cross(sect: pd.DataFrame) -> pd.DataFrame:
    out = []
    for _, g in sect.sort_values("dt").groupby("sector20"):
        g = g.copy()
        g["rs_rsi"] = wilder_rsi(g["rs"])
        g["rs_rsi_ma"] = g["rs_rsi"].rolling(SIGNAL_LEN, min_periods=SIGNAL_LEN).mean()
        above = g["rs_rsi"] > g["rs_rsi_ma"]
        g["cross_up"] = above & ~above.shift(1).fillna(False)
        g["cross_dn"] = (~above) & above.shift(1).fillna(False)
        # "On signal" = the cross happened within the last FRESH_WINDOW sessions,
        # so a rotation call is not judged only on the single day it fired.
        g["fresh_up"] = g["cross_up"].rolling(FRESH_WINDOW, min_periods=1).max().astype(bool)
        g["fresh_dn"] = g["cross_dn"].rolling(FRESH_WINDOW, min_periods=1).max().astype(bool)
        out.append(g)
    return pd.concat(out, ignore_index=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=400)
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    start = date.today() - timedelta(days=args.lookback_days)
    connection = psycopg2.connect(args.dsn)
    try:
        spot_raw = pd.read_sql(SESSION_SQL, connection, params={"start": start})
        tax = pd.read_sql(TAXONOMY_SQL, connection)
        m = build_opens(connection, start)
    finally:
        connection.close()

    spot = decompose(spot_raw)
    sect = add_cross(sector_series(spot, tax))

    # ── does the sector itself rotate after the cross? ─────────────────────
    print("1. SECTOR LEVEL — forward RS ratio move after the cross")
    g = sect.sort_values("dt").groupby("sector20")
    for h in (5, 10, 20):
        sect[f"rs_fwd{h}"] = g["rs"].shift(-h) / sect["rs"] - 1.0
    print(f"  {'state':<26}{'n':>7}" + "".join(f"{'fwd' + str(h) + ' %':>11}" for h in (5, 10, 20)))
    for label, mask in (("all sector-days", pd.Series(True, index=sect.index)),
                        ("RS-RSI crossed UP", sect["cross_up"]),
                        ("RS-RSI crossed DOWN", sect["cross_dn"])):
        d = sect[mask]
        cells = "".join(f"{d[f'rs_fwd{h}'].mean() * 100:>11.3f}" for h in (5, 10, 20))
        print(f"  {label:<26}{len(d):>7}{cells}")

    # ── does it pay on the options of that sector's names? ─────────────────
    state = sect[["sector20", "dt", "fresh_up", "fresh_dn", "cross_up", "cross_dn"]].copy()
    state["dt"] = pd.to_datetime(state["dt"]).dt.date
    m["dt"] = pd.to_datetime(m["dt"]).dt.date
    m = (m.merge(tax.rename(columns={"symbol": "underlying"}), on="underlying", how="left")
         .merge(state, on=["sector20", "dt"], how="left"))

    print("\n2. STOCK LEVEL — next-open OPTION return by sector cross state")
    print(f"  {'side':<5}{'sector state':<34}{'n':>8}{'mean %':>9}{'median %':>10}{'win %':>8}")
    for side in ("CE", "PE"):
        d = m[m["side"] == side]
        cells = (
            ("ALL (base rate)", d),
            ("RS-RSI crossed UP (day of)", d[d["cross_up"] == True]),      # noqa: E712
            (f"  ...within {FRESH_WINDOW} sessions", d[d["fresh_up"] == True]),   # noqa: E712
            ("RS-RSI crossed DOWN (day of)", d[d["cross_dn"] == True]),    # noqa: E712
            (f"  ...within {FRESH_WINDOW} sessions", d[d["fresh_dn"] == True]),   # noqa: E712
        )
        for label, sub in cells:
            s = sub["ret"].dropna()
            if len(s) < 100:
                print(f"  {side:<5}{label:<34}{len(s):>8}  (too few)")
                continue
            print(f"  {side:<5}{label:<34}{len(s):>8}{s.mean() * 100:>9.2f}"
                  f"{s.median() * 100:>10.2f}{(s > 0).mean() * 100:>8.1f}")
        print()

    # ── the pairing the owner actually described ───────────────────────────
    print("3. THE PAIRED CALL — CE on cross-UP sectors, PE on cross-DOWN")
    print(f"  {'leg':<40}{'n':>8}{'mean %':>9}{'median %':>10}{'win %':>8}")
    legs = (
        ("CE on sectors crossing UP", m[(m["side"] == "CE") & (m["fresh_up"] == True)]),   # noqa: E712
        ("PE on sectors crossing DOWN", m[(m["side"] == "PE") & (m["fresh_dn"] == True)]),  # noqa: E712
        ("CE on sectors crossing DOWN (wrong side)",
         m[(m["side"] == "CE") & (m["fresh_dn"] == True)]),                                # noqa: E712
        ("PE on sectors crossing UP (wrong side)",
         m[(m["side"] == "PE") & (m["fresh_up"] == True)]),                                # noqa: E712
    )
    for label, sub in legs:
        s = sub["ret"].dropna()
        if len(s) < 100:
            print(f"  {label:<40}{len(s):>8}  (too few)")
            continue
        print(f"  {label:<40}{len(s):>8}{s.mean() * 100:>9.2f}"
              f"{s.median() * 100:>10.2f}{(s > 0).mean() * 100:>8.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
