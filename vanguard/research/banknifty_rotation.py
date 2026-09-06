"""BANKNIFTY first: trade the index option, or the strongest name within it?

THE PLAN (owner): test on BANKNIFTY, because it is the one sector index actually
in this database. When BANKNIFTY shows strength we do NOT trade every bank --
we either trade the BANKNIFTY option itself, or pick the single stock showing
strength RELATIVE TO BANKNIFTY.

So there are two nested relative-strength questions and they must not be
confused:

    level 1   BANKNIFTY / NIFTY   -- is the sector turning?  (the RS-RSI cross)
    level 2   STOCK / BANKNIFTY   -- which name is leading the sector?

and three ways to act on level 1, compared head to head:

    (a) buy the BANKNIFTY ATM option
    (b) buy the ATM option of the bank with the STRONGEST stock/BANKNIFTY RS
    (c) buy every bank -- the thing the owner explicitly does not want to do,
        included only as the benchmark (a) and (b) have to beat

(b) IS TESTED BOTH WAYS. Everything measured in this research series so far --
sector level, sector cross, option RSI -- has come back mean-reverting rather
than trend-following, so the WEAKEST-RS bank is tested alongside the strongest.
Assuming the intuitive direction is how the earlier "buy the leading sector"
reading got inverted.

UNIVERSE NOTE: BANKNIFTY's true constituent list is not stored here, so the 16
banking names present in the spot table are used and listed explicitly below.
That is a superset of the real index (which carries ~12), so "the strongest
bank" may occasionally be a name the index itself does not hold.

    python vanguard/research/banknifty_rotation.py
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
from research.rs_rsi_cross import FRESH_WINDOW, SIGNAL_LEN, wilder_rsi  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
BANKS = ("HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK", "INDUSINDBK",
         "BANKBARODA", "PNB", "FEDERALBNK", "IDFCFIRSTB", "AUBANK", "CANBK",
         "BANKINDIA", "UNIONBANK", "RBLBANK", "YESBANK")
RS_MOM = 20        # sessions of stock/BANKNIFTY momentum for the level-2 rank


def level1(spot: pd.DataFrame) -> pd.DataFrame:
    """BANKNIFTY/NIFTY ratio, its RSI, and the cross state."""
    wide = spot.pivot_table(index="dt", columns="underlying", values="close_last")
    d = pd.DataFrame({"rs": (wide["BANKNIFTY"] / wide["NIFTY"]).dropna()})
    d["rsi"] = wilder_rsi(d["rs"])
    d["ma"] = d["rsi"].rolling(SIGNAL_LEN, min_periods=SIGNAL_LEN).mean()
    above = d["rsi"] > d["ma"]
    d["cross_up"] = above & ~above.shift(1).fillna(False)
    d["cross_dn"] = (~above) & above.shift(1).fillna(False)
    d["fresh_up"] = d["cross_up"].rolling(FRESH_WINDOW, min_periods=1).max().astype(bool)
    d["fresh_dn"] = d["cross_dn"].rolling(FRESH_WINDOW, min_periods=1).max().astype(bool)
    d["above"] = above
    return d.reset_index()


def level2(spot: pd.DataFrame) -> pd.DataFrame:
    """Each bank's strength RELATIVE TO BANKNIFTY, ranked per session."""
    wide = spot.pivot_table(index="dt", columns="underlying", values="close_last")
    bn = wide["BANKNIFTY"]
    rows = []
    for name in BANKS:
        if name not in wide:
            continue
        rs = (wide[name] / bn).dropna()
        mom = rs / rs.shift(RS_MOM) - 1.0
        rows.append(pd.DataFrame({"dt": rs.index, "underlying": name,
                                  "rs_vs_bn": rs.values, "rs_mom": mom.values}))
    out = pd.concat(rows, ignore_index=True).dropna(subset=["rs_mom"])
    # Rank WITHIN each session: 1.0 = strongest bank relative to the index.
    out["rs_rank"] = out.groupby("dt")["rs_mom"].rank(pct=True)
    return out


def summarise(label: str, s: pd.Series) -> None:
    if len(s) < 30:
        print(f"  {label:<46}{len(s):>7}  (too few)")
        return
    print(f"  {label:<46}{len(s):>7}{s.mean() * 100:>9.2f}"
          f"{s.median() * 100:>10.2f}{(s > 0).mean() * 100:>8.1f}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=560)
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    start = date.today() - timedelta(days=args.lookback_days)
    connection = psycopg2.connect(args.dsn)
    try:
        spot_raw = pd.read_sql(SESSION_SQL, connection, params={"start": start})
        m = build_opens(connection, start)
    finally:
        connection.close()

    spot = decompose(spot_raw)
    l1 = level1(spot)
    l2 = level2(spot)
    for f in (l1, l2):
        f["dt"] = pd.to_datetime(f["dt"]).dt.date
    m["dt"] = pd.to_datetime(m["dt"]).dt.date

    m = m.merge(l1[["dt", "fresh_up", "fresh_dn", "above"]], on="dt", how="left") \
         .merge(l2[["dt", "underlying", "rs_rank", "rs_mom"]],
                on=["dt", "underlying"], how="left")

    ce = m[m["side"] == "CE"]
    pe = m[m["side"] == "PE"]
    bank_ce = ce[ce["underlying"].isin(BANKS)]
    index_ce = ce[ce["underlying"] == "BANKNIFTY"]

    print(f"window {m['dt'].min()} .. {m['dt'].max()}  sessions={m['dt'].nunique()}")
    print(f"BANKNIFTY option nights={len(index_ce):,}   bank-stock option nights={len(bank_ce):,}")
    print(f"BANKNIFTY/NIFTY cross-up days in window: "
          f"{int(l1['cross_up'].sum())}   cross-down: {int(l1['cross_dn'].sum())}")
    print("\nAll figures are next-open OPTION returns, % of premium.\n")

    print(f"  {'leg':<46}{'n':>7}{'mean %':>9}{'median %':>10}{'win %':>8}")
    print("  -- benchmarks --")
    summarise("field: every name, CE", ce["ret"])
    summarise("every bank stock, CE (what we do NOT want)", bank_ce["ret"])
    summarise("BANKNIFTY index option, CE", index_ce["ret"])

    print("\n  -- level 1 ON: BANKNIFTY/NIFTY RS-RSI crossed UP --")
    sig = bank_ce[bank_ce["fresh_up"] == True]                       # noqa: E712
    summarise("(a) BANKNIFTY index option", index_ce[index_ce["fresh_up"] == True]["ret"])  # noqa: E712
    summarise("(c) every bank stock", sig["ret"])
    summarise("(b) STRONGEST bank vs BANKNIFTY (top 20%)",
              sig[sig["rs_rank"] >= 0.8]["ret"])
    summarise("(b') WEAKEST bank vs BANKNIFTY (bottom 20%)",
              sig[sig["rs_rank"] <= 0.2]["ret"])

    print("\n  -- level 1 OFF: crossed DOWN (the 'sell' leg, via PE) --")
    bank_pe = pe[pe["underlying"].isin(BANKS)]
    index_pe = pe[pe["underlying"] == "BANKNIFTY"]
    sigd = bank_pe[bank_pe["fresh_dn"] == True]                      # noqa: E712
    summarise("BANKNIFTY index option, PE", index_pe[index_pe["fresh_dn"] == True]["ret"])  # noqa: E712
    summarise("every bank stock, PE", sigd["ret"])
    summarise("WEAKEST bank vs BANKNIFTY, PE (bottom 20%)",
              sigd[sigd["rs_rank"] <= 0.2]["ret"])
    summarise("STRONGEST bank vs BANKNIFTY, PE (top 20%)",
              sigd[sigd["rs_rank"] >= 0.8]["ret"])

    print("\n  -- does level 2 work at all, independent of level 1? --")
    summarise("CE, strongest-RS bank (any regime)", bank_ce[bank_ce["rs_rank"] >= 0.8]["ret"])
    summarise("CE, weakest-RS bank (any regime)", bank_ce[bank_ce["rs_rank"] <= 0.2]["ret"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
