"""Can the month's best name be selected in advance?

bank_monthly.py established the prize: within the bank group the median
best-worst spread is 21.6% a month against a 2.1% sector move, and the best name
beat BANKNIFTY in 16 of 16 months. So the question is no longer whether
selection is worth more than timing -- it is whether the winner is PICKABLE.

METHOD. Every feature is computed at the PRIOR month's close and the target is
the CURRENT month's return, so nothing in the ranking is known after the fact.
Reported as rank IC within each month's cross-section (does the feature ORDER
the names correctly), with the SE clustered by MONTH, because names in one month
share a market shock and are not independent observations.

RUN ON TWO UNIVERSES. The bank group is the thread, but 16 names x 24 months is
~380 observations and cannot separate much. The full F&O universe gives ~200
names x 24 months, and if a feature works it should work in both -- a feature
that only works on 16 banks is a story about 16 banks.

FEATURES, chosen to span the documented styles rather than to be exhaustive:
  mom_1m/3m/12m   short-term reversal (Jegadeesh) vs medium momentum
  rs_sector       the name against its own sector -- what the owner's plan uses
  range_pos       52-week-high proximity (George & Hwang)
  vol_20d         realised volatility; high-beta names dominated the winners
  turnover        liquidity/size proxy
  rsi_14          the standard oscillator, on the daily
  prem_pctile     option premium vs its own year -- cheap or dear
  pcr_oi          put/call OI ratio on the front expiry

    python vanguard/research/monthly_pick.py
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
from research.banknifty_rotation import BANKS  # noqa: E402
from research.cross_section_ic import aggregate_session_ics, bar_ic  # noqa: E402
from research.overnight_intraday import SESSION_SQL, decompose  # noqa: E402
from research.two_x_features import CHAIN_SQL  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
TAXONOMY_SQL = "SELECT symbol, sector20 FROM sector_taxonomy WHERE instrument_type='Equity'"
FEATURES = ["mom_1m", "mom_3m", "mom_12m", "rs_sector", "range_pos",
            "vol_20d", "turnover", "rsi_14", "prem_pctile", "pcr_oi"]


def rsi(series: pd.Series, n: int = 14) -> pd.Series:
    d = series.diff()
    g = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    l = d.clip(upper=0).abs().ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))


def build(spot: pd.DataFrame, chain: pd.DataFrame, tax: pd.DataFrame) -> pd.DataFrame:
    spot = spot.sort_values(["underlying", "dt"]).copy()
    spot["dt"] = pd.to_datetime(spot["dt"])
    g = spot.groupby("underlying")["close_last"]
    spot["rsi_14"] = g.transform(rsi)
    spot["vol_20d"] = g.transform(lambda s: s.pct_change().rolling(20, min_periods=10).std())
    hi = g.transform(lambda s: s.rolling(250, min_periods=60).max())
    spot["range_pos"] = spot["close_last"] / hi
    spot["turnover"] = spot["close_last"] * spot["volume"]

    # Month-end snapshot per name: features AS OF that close.
    spot["mo"] = spot["dt"].dt.to_period("M")
    me = spot.sort_values("dt").groupby(["underlying", "mo"], as_index=False).last()
    me = me.sort_values(["underlying", "mo"])
    g2 = me.groupby("underlying")["close_last"]
    me["mom_1m"] = g2.pct_change(1)
    me["mom_3m"] = g2.pct_change(3)
    me["mom_12m"] = g2.pct_change(12)
    # THE TARGET: next month's return, i.e. strictly after every feature above.
    me["fwd"] = g2.shift(-1) / me["close_last"] - 1.0

    me = me.merge(tax.rename(columns={"symbol": "underlying"}), on="underlying", how="left")
    sect = me.groupby(["sector20", "mo"])["mom_1m"].transform("median")
    me["rs_sector"] = me["mom_1m"] - sect

    # option-side context, month-end
    if not chain.empty:
        chain = chain.copy()
        chain["dt"] = pd.to_datetime(chain["dt"])
        chain["mo"] = chain["dt"].dt.to_period("M")
        for c in ("ce_oi", "pe_oi"):
            chain[c] = chain[c].astype(float)
        chain["pcr_oi"] = chain["pe_oi"] / chain["ce_oi"].replace(0, np.nan)
        cm = chain.sort_values("dt").groupby(["underlying", "mo"], as_index=False).last()
        me = me.merge(cm[["underlying", "mo", "pcr_oi"]], on=["underlying", "mo"], how="left")
    else:
        me["pcr_oi"] = np.nan
    me["prem_pctile"] = np.nan          # filled by caller if option premia available
    return me


def ic_table(frame: pd.DataFrame, label: str) -> None:
    print(f"\n  --- {label} (n={frame['underlying'].nunique()} names, "
          f"{frame['mo'].nunique()} months) ---")
    print(f"  {'feature':<14}{'mean IC':>10}{'t':>8}{'months':>8}")
    scored = []
    for f in FEATURES:
        if f not in frame or frame[f].notna().sum() < 100:
            continue
        per = []
        for _, mg in frame.groupby("mo"):
            ic = bar_ic(mg[f], mg["fwd"])
            if ic is not None:
                per.append(ic)
        agg = aggregate_session_ics(per)
        if agg["mean_ic"] is None:
            continue
        t = agg["t_stat"]
        print(f"  {f:<14}{agg['mean_ic']:>+10.4f}"
              f"{(f'{t:+.2f}' if t is not None else 'n/a'):>8}{agg['n_sessions']:>8}")
        scored.append((abs(agg["mean_ic"]), f, agg["mean_ic"]))
    if scored:
        best = max(scored)
        print(f"  strongest: {best[1]} ({best[2]:+.4f}) — "
              f"{'HIGH' if best[2] > 0 else 'LOW'} values win")


def backtest(frame: pd.DataFrame, feature: str, top: int, ascending: bool) -> dict:
    """Pick `top` names each month by `feature`; report realised next-month return."""
    picks = []
    for _, mg in frame.dropna(subset=[feature, "fwd"]).groupby("mo"):
        if len(mg) < 5:
            continue
        sel = mg.sort_values(feature, ascending=ascending).head(top)
        picks.append(sel["fwd"].mean())
    s = pd.Series(picks)
    return {"n": len(s), "mean": s.mean() * 100, "median": s.median() * 100,
            "win": (s > 0).mean() * 100}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=900)
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    start = date.today() - timedelta(days=args.lookback_days)
    connection = psycopg2.connect(args.dsn)
    try:
        spot_raw = pd.read_sql(SESSION_SQL, connection, params={"start": start})
        tax = pd.read_sql(TAXONOMY_SQL, connection)
        chain = pd.read_sql(CHAIN_SQL, connection, params={"start": start})
    finally:
        connection.close()

    me = build(decompose(spot_raw), chain, tax)
    universe = me.dropna(subset=["fwd"])
    banks = universe[universe["underlying"].isin(BANKS)]

    print(f"window {universe['mo'].min()} .. {universe['mo'].max()}")
    print("Rank IC of each month-end feature against the FOLLOWING month's return.")
    ic_table(universe, "FULL UNIVERSE")
    ic_table(banks, "BANKS ONLY")

    print(f"\n  --- what a top-{args.top} monthly pick actually returns (%/month) ---")
    print(f"  {'universe':<10}{'rule':<28}{'months':>8}{'mean':>8}{'median':>9}{'win %':>8}")
    for label, uni in (("all", universe), ("banks", banks)):
        eq = uni.groupby("mo")["fwd"].mean()
        print(f"  {label:<10}{'equal-weight (benchmark)':<28}{len(eq):>8}"
              f"{eq.mean() * 100:>8.2f}{eq.median() * 100:>9.2f}"
              f"{(eq > 0).mean() * 100:>8.0f}")
        for feat in ("mom_1m", "mom_12m", "rs_sector", "rsi_14", "range_pos"):
            if feat not in uni or uni[feat].notna().sum() < 100:
                continue
            for asc, tag in ((False, "high"), (True, "low")):
                r = backtest(uni, feat, args.top, asc)
                if r["n"] < 6:
                    continue
                print(f"  {label:<10}{f'{feat} ({tag})':<28}{r['n']:>8}"
                      f"{r['mean']:>8.2f}{r['median']:>9.2f}{r['win']:>8.0f}")
        ceiling = uni.groupby("mo")["fwd"].max()
        print(f"  {label:<10}{'PERFECT pick (ceiling)':<28}{len(ceiling):>8}"
              f"{ceiling.mean() * 100:>8.2f}{ceiling.median() * 100:>9.2f}"
              f"{(ceiling > 0).mean() * 100:>8.0f}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
