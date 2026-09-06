"""Top-down: does picking the LEADING SECTOR first improve stock selection?

THE PLAN (owner, from the sector/NIFTY ratio charts): in specific periods
specific sectors outperform, so choose the sector on the ratio chart first and
then pick names from within it on a lower timeframe.

WHY THIS IS A REAL CRITICISM OF THE EARLIER WORK. best_opens.py ranked all 217
names against EACH OTHER globally, with no notion of which sector was leading.
If leadership rotates and matters, that ranking pools a leading name with a
lagging one and reports the average -- which is exactly the shape of the flat
winner profile it produced. Conditioning first is a different question, not a
refinement of the same one.

CONSTRUCTION. Sector indices (CNXIT, CNXMETAL, CNXAUTO...) are NOT in this
database -- only BANKNIFTY/NIFTY/FINNIFTY/MIDCPNIFTY. So each sector index is
built from its own constituents in `sector_taxonomy`: an EQUAL-WEIGHTED daily
return, chained. Equal weight, not cap weight, because cap weights are not
stored and a fabricated weighting would quietly become a size factor.

    RS ratio = sector index / NIFTY index      (what the ratio charts plot)
    leading  = ratio above its 20-session MA AND that MA rising

THREE TESTS, in the order that matters:
  1. PERSISTENCE -- does this period's leading sector keep leading? If sector
     leadership does not persist, the whole top-down frame is describing the
     past and there is nothing to select on.
  2. CONDITIONING -- do stocks IN a leading sector open better than the field?
  3. WITHIN-SECTOR SELECTION -- inside a leading sector, does the low-timeframe
     setup (hourly MA reversal) separate names better than it did globally?
     This is the owner's plan end to end.

CAVEAT ON THE TAXONOMY: sector20's largest bucket is "Banking + 4 more" with 69
of ~210 names -- a third of the universe in one lumped sector. Rotation cannot
be measured cleanly inside a bucket that broad, and that limits test 3.

    python vanguard/research/sector_rotation.py
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
from research.mtf_reversal import HOURLY_SQL, daily_trend, hourly_features  # noqa: E402
from research.overnight_intraday import SESSION_SQL, decompose  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
RS_MA = 20
MIN_SECTOR_NAMES = 5

TAXONOMY_SQL = "SELECT symbol, sector20 FROM sector_taxonomy WHERE instrument_type = 'Equity'"


def sector_series(spot: pd.DataFrame, taxonomy: pd.DataFrame) -> pd.DataFrame:
    """Equal-weighted sector index and its RS ratio against NIFTY."""
    spot = spot.copy()
    spot["ret"] = spot.groupby("underlying")["close_last"].pct_change()

    bench = (spot[spot["underlying"] == "NIFTY"][["dt", "ret"]]
             .rename(columns={"ret": "bench_ret"}))
    if bench.empty:
        raise RuntimeError("NIFTY not present in the spot table -- no benchmark to divide by")

    members = spot.merge(taxonomy, left_on="underlying", right_on="symbol", how="inner")
    sect = (members.groupby(["sector20", "dt"], as_index=False)
            .agg(ret=("ret", "mean"), n=("ret", "size")))
    sect = sect[sect["n"] >= MIN_SECTOR_NAMES].merge(bench, on="dt", how="inner")

    out = []
    for sector, g in sect.sort_values("dt").groupby("sector20"):
        g = g.copy()
        # The ratio chart is sector index / benchmark index, so both legs are
        # chained BEFORE dividing -- dividing daily returns is a different and
        # much noisier object.
        g["sect_idx"] = (1 + g["ret"].fillna(0)).cumprod()
        g["bench_idx"] = (1 + g["bench_ret"].fillna(0)).cumprod()
        g["rs"] = g["sect_idx"] / g["bench_idx"]
        g["rs_ma"] = g["rs"].rolling(RS_MA, min_periods=RS_MA).mean()
        g["rs_ma_prev"] = g["rs_ma"].shift(5)
        g["leading"] = (g["rs"] > g["rs_ma"]) & (g["rs_ma"] > g["rs_ma_prev"])
        g["lagging"] = (g["rs"] < g["rs_ma"]) & (g["rs_ma"] < g["rs_ma_prev"])
        g["rs_fwd_5"] = g["rs"].shift(-5) / g["rs"] - 1.0
        g["rs_chg_20"] = g["rs"] / g["rs"].shift(20) - 1.0
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
        taxonomy = pd.read_sql(TAXONOMY_SQL, connection)
        m = build_opens(connection, start)
        hourly_raw = pd.read_sql(HOURLY_SQL, connection, params={"start": start})
    finally:
        connection.close()

    spot = decompose(spot_raw)
    sect = sector_series(spot, taxonomy)
    print(f"window {sect['dt'].min()} .. {sect['dt'].max()}  "
          f"sectors={sect['sector20'].nunique()}  sessions={sect['dt'].nunique()}")

    # ── 1. does sector leadership persist? ─────────────────────────────────
    print("\n1. PERSISTENCE — does the leading sector keep leading?")
    d = sect.dropna(subset=["rs_chg_20", "rs_fwd_5"])
    d = d.copy()
    d["q"] = d.groupby("dt")["rs_chg_20"].rank(pct=True)
    print(f"  {'trailing 20d RS bucket':<30}{'n':>8}{'fwd 5d RS %':>14}{'win %':>8}")
    for label, mask in (("top 20% (leaders)", d["q"] >= 0.8),
                        ("middle", d["q"].between(0.2, 0.8)),
                        ("bottom 20% (laggards)", d["q"] <= 0.2)):
        s = d[mask]["rs_fwd_5"]
        print(f"  {label:<30}{len(s):>8}{s.mean() * 100:>14.3f}{(s > 0).mean() * 100:>8.1f}")

    # ── 2. do stocks in a leading sector open better? ──────────────────────
    print("\n2. CONDITIONING — next-open OPTION return by sector state")
    tax = taxonomy.rename(columns={"symbol": "underlying"})
    state = sect[["sector20", "dt", "leading", "lagging", "rs_chg_20"]]
    for f in (m,):
        f["dt"] = pd.to_datetime(f["dt"]).dt.date
    state = state.copy()
    state["dt"] = pd.to_datetime(state["dt"]).dt.date
    m = m.merge(tax, on="underlying", how="left").merge(
        state, on=["sector20", "dt"], how="left")

    print(f"  {'side':<5}{'sector state':<28}{'n':>8}{'mean %':>9}{'median %':>10}{'win %':>8}")
    for side in ("CE", "PE"):
        d2 = m[m["side"] == side]
        for label, sub in (("ALL (base rate)", d2),
                           ("sector LEADING", d2[d2["leading"] == True]),      # noqa: E712
                           ("sector LAGGING", d2[d2["lagging"] == True])):     # noqa: E712
            s = sub["ret"].dropna()
            if len(s) < 200:
                print(f"  {side:<5}{label:<28}{len(s):>8}  (too few)")
                continue
            print(f"  {side:<5}{label:<28}{len(s):>8}{s.mean() * 100:>9.2f}"
                  f"{s.median() * 100:>10.2f}{(s > 0).mean() * 100:>8.1f}")

    # ── 3. within a leading sector, does the low-timeframe setup separate? ──
    print("\n3. WITHIN-SECTOR + LOW TIMEFRAME — the plan end to end")
    hourly = hourly_features(hourly_raw)
    trend = daily_trend(spot.rename(columns={"close_last": "close_last"}))
    for f in (hourly, trend):
        f["dt"] = pd.to_datetime(f["dt"]).dt.date
    m = m.merge(hourly, on=["underlying", "dt"], how="left").merge(
        trend, on=["underlying", "dt"], how="left")
    m["setup_ce"] = m["up_trend"].fillna(False) & m["touch_lo20"].fillna(False)
    m["setup_pe"] = m["down_trend"].fillna(False) & m["touch_hi20"].fillna(False)

    print(f"  {'side':<5}{'cell':<40}{'n':>8}{'mean %':>9}{'median %':>10}{'win %':>8}")
    # NOTE the pairing. Test 1 showed sector leadership MEAN-REVERTS (laggard
    # sectors have the higher forward RS), and test 2 confirmed it at the stock
    # level: calls on names in LAGGING sectors returned +1.06% against +0.03%
    # in leading ones. So the CE case is paired with LAGGING, not leading --
    # pairing it the intuitive way would test a hypothesis the data has already
    # rejected.
    for side, setup, lead_col in (("CE", "setup_ce", "lagging"),
                                  ("PE", "setup_pe", "leading")):
        d2 = m[m["side"] == side]
        cells = (
            ("ALL", d2),
            (f"sector {'LAGGING' if side == 'CE' else 'LEADING'} only",
             d2[d2[lead_col] == True]),                                       # noqa: E712
            ("low-TF setup only (any sector)", d2[d2[setup]]),
            ("BOTH: sector state + low-TF setup",
             d2[(d2[lead_col] == True) & d2[setup]]),                         # noqa: E712
        )
        for label, sub in cells:
            s = sub["ret"].dropna()
            if len(s) < 150:
                print(f"  {side:<5}{label:<40}{len(s):>8}  (too few)")
                continue
            print(f"  {side:<5}{label:<40}{len(s):>8}{s.mean() * 100:>9.2f}"
                  f"{s.median() * 100:>10.2f}{(s > 0).mean() * 100:>8.1f}")
    return 0



    sys.exit(main())

    # ── 4. STABILITY of the one cell worth trading ────────────────────────
    # Two headline numbers already collapsed under a subperiod split this week,
    # so nothing gets reported from here without one.
    print("\n4. STABILITY — CE, sector LAGGING, by month")
    d3 = m[(m["side"] == "CE") & (m["lagging"] == True)].dropna(subset=["ret"])  # noqa: E712
    d3 = d3.copy()
    d3["mo"] = pd.to_datetime(d3["dt"]).dt.to_period("M")
    print(f"  {'month':>9}{'n':>7}{'mean %':>9}{'median %':>10}{'win %':>8}")
    for mo, g in d3.groupby("mo"):
        if len(g) < 50:
            print(f"  {str(mo):>9}{len(g):>7}   (too few)")
            continue
        r = g["ret"]
        print(f"  {str(mo):>9}{len(g):>7}{r.mean() * 100:>9.2f}"
              f"{r.median() * 100:>10.2f}{(r > 0).mean() * 100:>8.1f}")
    r = d3["ret"]
    print(f"  FULL n={len(r):,}  mean={r.mean() * 100:+.2f}%  median={r.median() * 100:+.2f}%  "
          f"drop-5-best mean={r.sort_values().iloc[:-5].mean() * 100:+.2f}%")
    print(f"  distinct sectors={d3['sector20'].nunique()}  "
          f"symbols={d3['underlying'].nunique()}  dates={d3['dt'].nunique()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
