"""Monthly name selection, wider feature set — EMA, Bollinger, ATR, volume, Darvas.

Extends monthly_pick.py, which tested momentum/RS/RSI/PCR and found only pcr_oi
significant. Adds the classical technical family the owner asked for, plus a
Darvas-box implementation, and prints the month-by-month ACTUAL WINNER against
the PICKED name so the rule can be judged case by case rather than on a mean.

DARVAS BOX, as implemented. Darvas bought a stock making new highs once it
settled into a range beneath that high, entering on the break of the range top:

    box_top      the highest high of the last BOX_LOOK sessions
    consolidating box_top has NOT been exceeded for the last BOX_QUIET sessions
                 -- i.e. the stock has stopped making new highs, which is what
                 forms the box rather than merely being near a high
    box_height   (box_top - lowest low since the top was set) / close, so a
                 TIGHT box scores low; Darvas wanted tight boxes
    breakout     close is above the PRIOR box_top -- measured against the top as
                 it stood before this session, or the feature would trivially be
                 true every time a new high prints

`in_box` and `breakout` are mutually exclusive by construction and are tested
separately: the theory makes a claim about the breakout, but "sitting in a tight
box near the highs" is the state a selection system would actually rank on at a
month boundary, and the two need not behave alike.

ALL FEATURES ARE AS OF THE PRIOR MONTH'S CLOSE; the target is the following
month's return.

    python vanguard/research/monthly_pick_v2.py
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
from research.cross_section_ic import aggregate_session_ics, bar_ic  # noqa: E402
from research.monthly_pick import TAXONOMY_SQL, rsi  # noqa: E402
from research.overnight_intraday import SESSION_SQL, decompose  # noqa: E402
from research.two_x_features import CHAIN_SQL  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
BOX_LOOK, BOX_QUIET = 20, 5
# The spot table carries INDICES alongside stocks. Left in, the ranking happily
# "picks" NIFTYNXT50 as a name to trade -- which it is not, and which also drags
# every cross-sectional statistic toward an index's much lower dispersion.
INDICES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX"}

FEATURES = [
    "mom_1m", "mom_3m", "rs_sector", "rsi_14",
    "ema_trend", "px_vs_ema50",
    "bb_pctb", "bb_width",
    "atr_pct", "atr_expand",
    "rvol", "vol_trend",
    "darvas_in_box", "darvas_breakout", "box_height",
    "pcr_oi",
]


def daily_features(spot: pd.DataFrame) -> pd.DataFrame:
    spot = spot.sort_values(["underlying", "dt"]).copy()
    spot["dt"] = pd.to_datetime(spot["dt"])
    out = []
    for _, g in spot.groupby("underlying", sort=False):
        g = g.copy()
        c, h, l, v = g["close_last"], g["high"], g["low"], g["volume"]

        ema20 = c.ewm(span=20, adjust=False).mean()
        ema50 = c.ewm(span=50, adjust=False).mean()
        g["ema_trend"] = ema20 / ema50 - 1.0
        g["px_vs_ema50"] = c / ema50 - 1.0

        ma20 = c.rolling(20, min_periods=15).mean()
        sd20 = c.rolling(20, min_periods=15).std()
        upper, lower = ma20 + 2 * sd20, ma20 - 2 * sd20
        g["bb_pctb"] = (c - lower) / (upper - lower).replace(0, np.nan)
        g["bb_width"] = (upper - lower) / ma20.replace(0, np.nan)

        prev_c = c.shift(1)
        tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
        atr14 = tr.ewm(alpha=1 / 14, adjust=False).mean()
        g["atr_pct"] = atr14 / c
        g["atr_expand"] = atr14 / tr.ewm(alpha=1 / 50, adjust=False).mean().replace(0, np.nan)

        med20 = v.rolling(20, min_periods=10).median()
        g["rvol"] = v / med20.replace(0, np.nan)
        g["vol_trend"] = med20 / v.rolling(60, min_periods=30).median().replace(0, np.nan)

        # ── Darvas box ──────────────────────────────────────────────────────
        box_top = h.rolling(BOX_LOOK, min_periods=BOX_LOOK).max()
        prior_top = box_top.shift(1)
        # Quiet = the box top has not moved for BOX_QUIET sessions, i.e. no new
        # high has printed. That is what turns "near the high" into a BOX.
        quiet = (box_top.diff().abs() < 1e-9).rolling(BOX_QUIET, min_periods=BOX_QUIET).min()
        box_low = l.rolling(BOX_LOOK, min_periods=BOX_LOOK).min()
        g["box_height"] = (box_top - box_low) / c.replace(0, np.nan)
        g["darvas_in_box"] = ((quiet == 1) & (c <= prior_top) & (c >= box_low)).astype(float)
        g["darvas_breakout"] = ((c > prior_top) & (quiet.shift(1) == 1)).astype(float)

        g["rsi_14"] = rsi(c)
        out.append(g)
    return pd.concat(out, ignore_index=True)


def monthly_frame(spot: pd.DataFrame, chain: pd.DataFrame, tax: pd.DataFrame) -> pd.DataFrame:
    spot["mo"] = spot["dt"].dt.to_period("M")
    me = spot.sort_values("dt").groupby(["underlying", "mo"], as_index=False).last()
    me = me.sort_values(["underlying", "mo"])
    g = me.groupby("underlying")["close_last"]
    me["mom_1m"] = g.pct_change(1)
    me["mom_3m"] = g.pct_change(3)
    me["fwd"] = g.shift(-1) / me["close_last"] - 1.0

    me = me.merge(tax.rename(columns={"symbol": "underlying"}), on="underlying", how="left")
    me["rs_sector"] = me["mom_1m"] - me.groupby(["sector20", "mo"])["mom_1m"].transform("median")

    if not chain.empty:
        chain = chain.copy()
        chain["dt"] = pd.to_datetime(chain["dt"])
        chain["mo"] = chain["dt"].dt.to_period("M")
        for c in ("ce_oi", "pe_oi"):
            chain[c] = chain[c].astype(float)
        chain["pcr_oi"] = chain["pe_oi"] / chain["ce_oi"].replace(0, np.nan)
        cm = chain.sort_values("dt").groupby(["underlying", "mo"], as_index=False).last()
        me = me.merge(cm[["underlying", "mo", "pcr_oi"]], on=["underlying", "mo"], how="left")
    return me


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

    me = monthly_frame(daily_features(decompose(spot_raw)), chain, tax)
    uni = me[~me["underlying"].isin(INDICES)].dropna(subset=["fwd"])
    print(f"window {uni['mo'].min()} .. {uni['mo'].max()}   names={uni['underlying'].nunique()}")

    print("\n1. RANK IC vs NEXT month's return (SE clustered by month)")
    print(f"  {'feature':<17}{'mean IC':>10}{'t':>8}{'months':>8}")
    scored = []
    for f in FEATURES:
        if f not in uni or uni[f].notna().sum() < 200:
            continue
        per = [ic for _, mg in uni.groupby("mo")
               if (ic := bar_ic(mg[f], mg["fwd"])) is not None]
        agg = aggregate_session_ics(per)
        if agg["mean_ic"] is None:
            continue
        t = agg["t_stat"]
        star = " *" if t is not None and abs(t) >= 2 else ""
        print(f"  {f:<17}{agg['mean_ic']:>+10.4f}"
              f"{(f'{t:+.2f}' if t is not None else 'n/a'):>8}{agg['n_sessions']:>8}{star}")
        if t is not None:
            scored.append((abs(t), f, agg["mean_ic"]))

    print("\n2. TOP-{n} MONTHLY PICK (%/month)".replace("{n}", str(args.top)))
    print(f"  {'rule':<30}{'months':>8}{'mean':>8}{'median':>9}{'win %':>8}")
    eq = uni.groupby("mo")["fwd"].mean()
    print(f"  {'equal-weight (benchmark)':<30}{len(eq):>8}{eq.mean() * 100:>8.2f}"
          f"{eq.median() * 100:>9.2f}{(eq > 0).mean() * 100:>8.0f}")
    results = {}
    for _, f, ic in sorted(scored, reverse=True)[:8]:
        asc = ic < 0
        picks = []
        for mo, mg in uni.dropna(subset=[f, "fwd"]).groupby("mo"):
            if len(mg) < 10:
                continue
            picks.append(mg.sort_values(f, ascending=asc).head(args.top)["fwd"].mean())
        s = pd.Series(picks)
        if len(s) < 6:
            continue
        results[f] = s
        print(f"  {f + (' (low)' if asc else ' (high)'):<30}{len(s):>8}"
              f"{s.mean() * 100:>8.2f}{s.median() * 100:>9.2f}{(s > 0).mean() * 100:>8.0f}")
    ceil = uni.groupby("mo")["fwd"].max()
    print(f"  {'PERFECT pick (ceiling)':<30}{len(ceil):>8}{ceil.mean() * 100:>8.2f}"
          f"{ceil.median() * 100:>9.2f}{(ceil > 0).mean() * 100:>8.0f}")

    # ── 3. month-by-month: who actually won, and who did the rule pick ──────
    best_f = max(scored)[1] if scored else "pcr_oi"
    asc = dict((f, ic < 0) for _, f, ic in scored).get(best_f, True)
    print(f"\n3. MONTH BY MONTH — actual winner vs the pick ({best_f}"
          f"{' low' if asc else ' high'})")
    print(f"  {'month':<9}{'ACTUAL WINNER':<14}{'ret %':>8}   "
          f"{'PICKED':<14}{'ret %':>8}{'rank':>7}{'bench':>8}")
    for mo, mg in uni.dropna(subset=[best_f, "fwd"]).groupby("mo"):
        if len(mg) < 10:
            continue
        win = mg.loc[mg["fwd"].idxmax()]
        sel = mg.sort_values(best_f, ascending=asc).head(1).iloc[0]
        rank = int((mg["fwd"] > sel["fwd"]).sum()) + 1
        print(f"  {str(mo):<9}{win['underlying']:<14}{win['fwd'] * 100:>8.1f}   "
              f"{sel['underlying']:<14}{sel['fwd'] * 100:>8.1f}"
              f"{rank:>5}/{len(mg):<3}{mg['fwd'].mean() * 100:>7.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
