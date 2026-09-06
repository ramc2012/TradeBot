"""The two-sided IB break: CE on the up-break, PE on the down-break.

Built on mp_profile.py's real first-hour Initial Balance from 30m bars.

THREE QUESTIONS, in the order they have to be answered:

  1. IS THERE A MOVE AT ALL? How often does the first hour's range get broken,
     which way, and how far does price travel IN THE BREAK DIRECTION -- intraday
     and out to 3 sessions. Reported in percent AND in IB multiples, because the
     monthly study showed a multiple is inflated by a small denominator and only
     the percent is paid in rupees.

  2. IS THE DOWN-BREAK AS GOOD AS THE UP-BREAK? Every earlier study scored
     signed return, which buries the short side. Scored as MAGNITUDE, a
     down-break is a PE and deserves its own row. Overnight drift is positive in
     equities, so the down side must clear a HIGHER bar to be worth trading, not
     an equal one.

  3. CAN THE BIG MOVE BE PICKED IN ADVANCE? This is where every previous attempt
     died, so the controls are set before looking:
       - scored on MFE, a magnitude, which volatility has a right to rank
       - DEMEANED BY SESSION, since 17 bank names move together and a raw cohort
         gap mostly measures which days the cohort sat in
       - benchmarked against atr20, plain trailing volatility. A profile feature
         that cannot beat trailing ATR has told us nothing, because ATR is free
         AND already in the option's price
       - split-half and drop-2 on anything that survives

    python vanguard/research/mp_two_sided.py
    python vanguard/research/mp_two_sided.py --universe indices --lookback-days 1800
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
from research.mp_profile import FWD_SESSIONS, dsn, load  # noqa: E402

BANK_UNIVERSE = ("BANKNIFTY",) + BANKS
INDEX_UNIVERSE = ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "MIDCPNIFTY")
FEATURES = ["ib_vs_atr", "ib_width", "atr20", "break_frac", "gap",
            "open_vs_prev_vah", "ib_vs_prev_poc"]


def t_of(x: pd.Series) -> float:
    x = x.dropna()
    if len(x) < 3 or x.std(ddof=1) == 0:
        return np.nan
    return x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))


def base_rates(s: pd.DataFrame) -> None:
    n = len(s)
    print(f"\n1. DOES THE FIRST HOUR'S RANGE GET BROKEN?   sessions={n:,}")
    for label, m in (("broke UP first", s["side"] == 1),
                     ("broke DOWN first", s["side"] == -1),
                     ("never broke the IB", s["side"] == 0)):
        print(f"   {label:<24}{m.sum():>7,}  ({m.mean() * 100:>4.1f}%)")
    print(f"   median IB width          {s['ib_width'].median() * 100:>6.2f}% of price"
          f"   (median session range {(  (s['high'] - s['low']) / s['close']).median() * 100:.2f}%)")


def move_table(s: pd.DataFrame) -> None:
    b = s[s["side"] != 0].copy()
    b["ib_mult"] = b["mfe_total"] * b["ib_ref"] / (b["ib_hi"] - b["ib_lo"])
    print(f"\n2. HOW FAR DOES IT TRAVEL IN THE BREAK DIRECTION")
    print(f"   {'cohort':<26}{'n':>7}{'MFE intra':>11}{'MFE 3d':>9}"
          f"{'MAE 3d':>9}{'ret 3d':>9}{'IB mult':>9}{'P(>2%)':>8}{'P(>5%)':>8}")
    rows = [("ALL breaks", b),
            ("UP break  -> CE", b[b["side"] == 1]),
            ("DOWN break -> PE", b[b["side"] == -1])]
    for label, d in rows:
        if len(d) < 50:
            continue
        tot = d["mfe_total"].dropna()
        print(f"   {label:<26}{len(d):>7,}"
              f"{d['mfe_intraday'].median() * 100:>10.2f}%"
              f"{d[f'mfe_{FWD_SESSIONS}d'].median() * 100:>8.2f}%"
              f"{d[f'mae_{FWD_SESSIONS}d'].median() * 100:>8.2f}%"
              f"{d[f'ret_{FWD_SESSIONS}d'].median() * 100:>8.2f}%"
              f"{d['ib_mult'].median():>9.2f}"
              f"{(tot >= 0.02).mean() * 100:>7.0f}%{(tot >= 0.05).mean() * 100:>7.0f}%")
    # An option buyer pays for the move and eats the heat; MFE alone flatters.
    print("\n   MFE is the best exit available, not a realistic one. 'ret 3d' is the\n"
          "   hold-to-horizon result and is the honest one; the gap between them is\n"
          "   what exit discipline is worth.")


def rank_features(s: pd.DataFrame, target: str) -> None:
    """Session-demeaned rank IC of each feature against the move size."""
    b = s[s["side"] != 0].dropna(subset=[target]).copy()
    # A cross-sectional rank needs a cross-section: sessions carrying fewer than
    # 6 names are dropped below, which silently discards the whole pre-2025-03
    # era when only the index had 30m data. Report the window the ICs are
    # ACTUALLY computed on, not the window the descriptive stats used.
    wide = b.groupby("dt").size()
    wide = wide[wide >= 6]
    print(f"\n3. WHAT RANKS THE MOVE?   target={target}   n={len(b):,}")
    if len(wide):
        used = b[b["dt"].isin(wide.index)]
        print(f"   ICs are computed on {len(wide)} sessions with >=6 names "
              f"({used['dt'].min().date()} .. {used['dt'].max().date()}, "
              f"{len(used):,} name-days), NOT the full descriptive window above.")
    print(f"   {'feature':<20}{'rank IC':>9}{'t':>7}{'IC>0':>7}"
          f"{'top3 edge':>11}{'t':>7}{'1st half':>10}{'2nd half':>10}")
    for f in FEATURES:
        d = b.dropna(subset=[f])
        if len(d) < 500:
            continue
        ics, edges, days = [], [], []
        for dt, g in d.groupby("dt"):
            if len(g) < 6:
                continue
            ic = g[f].corr(g[target], method="spearman")
            if pd.notna(ic):
                ics.append(ic)
                days.append(dt)
                edges.append(g.nlargest(3, f)[target].mean() - g[target].mean())
        if len(ics) < 30:
            continue
        ics, edges = pd.Series(ics), pd.Series(edges)
        h = len(edges) // 2
        star = " *" if abs(t_of(ics)) >= 2 else ""
        print(f"   {f:<20}{ics.mean():>+9.3f}{t_of(ics):>+7.2f}"
              f"{(ics > 0).mean() * 100:>6.0f}%{edges.mean() * 100:>+11.2f}"
              f"{t_of(edges):>+7.2f}{edges.iloc[:h].mean() * 100:>+10.2f}"
              f"{edges.iloc[h:].mean() * 100:>+10.2f}{star}")
    print("   'top3 edge' = the 3 highest-ranked names' move minus the session mean,\n"
          "   in percentage points. atr20 is the free benchmark: a profile feature\n"
          "   that does not beat it has added nothing an option price lacks.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", choices=("banks", "indices"), default="banks")
    parser.add_argument("--lookback-days", type=int, default=700)
    parser.add_argument("--dsn", default=dsn())
    args = parser.parse_args()

    names = list(BANK_UNIVERSE if args.universe == "banks" else INDEX_UNIVERSE)
    start = date.today() - timedelta(days=args.lookback_days)
    connection = psycopg2.connect(args.dsn)
    try:
        s = load(connection, names, start)
    finally:
        connection.close()
    if s.empty:
        print("no sessions built")
        return 1

    print(f"universe={args.universe}  names={s['underlying'].nunique()}  "
          f"window {s['dt'].min().date()} .. {s['dt'].max().date()}")
    base_rates(s)
    move_table(s)
    if s["underlying"].nunique() >= 6:
        rank_features(s, "mfe_total")
        rank_features(s, f"ret_{FWD_SESSIONS}d")
    else:
        print("\n3. skipped: a cross-sectional rank needs more names than this universe has")

    # Per-name breakdown so a single dominant symbol cannot carry the result.
    b = s[s["side"] != 0]
    print(f"\nPER-NAME (median MFE over {FWD_SESSIONS} sessions, break direction)")
    per = b.groupby("underlying").agg(
        n=("mfe_total", "size"), up=("side", lambda x: (x == 1).mean()),
        mfe=("mfe_total", "median"), ret=(f"ret_{FWD_SESSIONS}d", "median"),
        ibw=("ib_width", "median")).sort_values("mfe", ascending=False)
    print(f"   {'name':<14}{'n':>6}{'up%':>7}{'MFE':>9}{'ret3d':>9}{'IB width':>10}")
    for name, r in per.iterrows():
        print(f"   {name:<14}{int(r['n']):>6}{r['up'] * 100:>6.0f}%"
              f"{r['mfe'] * 100:>8.2f}%{r['ret'] * 100:>8.2f}%{r['ibw'] * 100:>9.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
