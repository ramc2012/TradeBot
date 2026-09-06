"""Does the auction show DIRECTION? BANKNIFTY, 3 years, full MP metric set.

TWO CLAIMS UNDER TEST, both from the owner:
  A. SMALL IB -> TREND DAY, LARGE IB -> BALANCE. If true, IB is not merely
     restated volatility: it tells you what KIND of session to expect, and a
     trend day is directional by definition.
  B. THE AUCTION, NOT ONE METRIC. POC, prior POC, value area and its extremes,
     developing value, poor highs/lows, failed auctions, initiative vs
     responsive, value migration and day type together, rather than the IB
     break alone.

THE DISCIPLINE THAT DECIDES WHETHER ANY OF IT IS A SIGNAL. Half the MP metric
set is only known at 15:15 -- day type, poor highs, tails, value migration, the
final POC. Those cannot predict the session that produced them; using them that
way is the single easiest way to manufacture a result here. So the tests are
split and the split is enforced:

    KNOWN AT 10:15 (IB close)  ->  may predict the REST OF THE SESSION
       ib_width, ib_pct_rank, ib_close_pos, gap, open_location, initiative,
       open_vs_prev_*, ib_above_prev_vah, dev_poc/dev_vah/dev_val, atr20
    KNOWN AT 15:15 (close)     ->  may only predict the NEXT session
       day_type, poor_high/low, tails, single_prints, value_shift,
       poc_migration, va_overlap, failed_high/low, close_pos

BANKNIFTY alone is one series, so there is no cross-section to demean; every
t-statistic here is a time-series t over independent sessions, and the forward
returns at h=1 do not overlap. Sign tests are reported alongside means because
one 2024-style outlier session can carry a mean and cannot carry a sign count.

    python vanguard/research/mp_auction_test.py
    python vanguard/research/mp_auction_test.py --years 5
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
from research.mp_auction import dsn, load  # noqa: E402

IB_KNOWN = ["ib_width", "ib_pct_rank", "ib_close_pos", "gap", "atr20",
            "open_vs_prev_vah", "open_vs_prev_poc", "ib_above_prev_vah",
            "ib_below_prev_val"]
CLOSE_KNOWN = ["close_pos", "poc_migration", "va_overlap", "tail_high",
               "tail_low", "single_prints", "range_over_ib"]


def t_of(x: pd.Series) -> float:
    x = x.dropna()
    if len(x) < 5 or x.std(ddof=1) == 0:
        return np.nan
    return x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))


def ic_block(d: pd.DataFrame, feats: list[str], targets: list[str]) -> None:
    print(f"   {'feature':<22}" + "".join(f"{t:>22}" for t in targets))
    print(f"   {'':<22}" + "".join(f"{'rho':>9}{'t':>7}{'n':>6}" for _ in targets))
    for f in feats:
        cells = ""
        for tg in targets:
            dd = d[[f, tg]].dropna()
            if len(dd) < 60:
                cells += f"{'-':>9}{'-':>7}{len(dd):>6}"
                continue
            rho = dd[f].corr(dd[tg], method="spearman")
            # t of a Spearman rho on n independent observations
            t = rho * np.sqrt((len(dd) - 2) / max(1 - rho ** 2, 1e-9))
            cells += f"{rho:>+9.3f}{t:>+7.2f}{len(dd):>6}"
        print(f"   {f:<22}{cells}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BANKNIFTY")
    parser.add_argument("--years", type=float, default=3.0)
    parser.add_argument("--dsn", default=dsn())
    args = parser.parse_args()

    start = date.today() - timedelta(days=int(args.years * 365.25))
    connection = psycopg2.connect(args.dsn)
    try:
        s = load(connection, [args.symbol], start)
    finally:
        connection.close()
    s = s.dropna(subset=["prev_poc"]).reset_index(drop=True)
    print(f"{args.symbol}: {len(s):,} sessions   "
          f"{s['dt'].min().date()} .. {s['dt'].max().date()}")
    print(f"volume present on {(s['volume'] > 0).mean() * 100:.0f}% of sessions "
          f"-> VWAP and absorption are NOT computable; the TPO profile mean is "
          f"used instead and is not the same thing.\n")

    # ── day type distribution, as a sanity check against the MP literature ──
    print("DAY TYPES (Dalton), with what each one did")
    print(f"   {'type':<22}{'n':>6}{'share':>8}{'range/IB':>10}"
          f"{'IB width':>10}{'|rest ret|':>11}{'rest ret':>10}{'close pos':>11}")
    for t, g in s.groupby("day_type"):
        print(f"   {t:<22}{len(g):>6}{len(g) / len(s) * 100:>7.1f}%"
              f"{g['range_over_ib'].median():>10.2f}{g['ib_width'].median() * 100:>9.2f}%"
              f"{g['rest_ret'].abs().median() * 100:>10.2f}%"
              f"{g['rest_ret'].median() * 100:>+9.2f}%{g['close_pos'].median():>11.2f}")

    # ── CLAIM A: does a small IB produce a trend day? ───────────────────────
    print("\nCLAIM A: small IB -> trend day, large IB -> balance")
    s["ib_q"] = pd.qcut(s["ib_pct_rank"], 5, labels=False, duplicates="drop")
    print(f"   {'IB quintile (own history)':<28}{'n':>6}{'IB width':>10}"
          f"{'range/IB':>10}{'P(trend)':>10}{'P(neutral)':>12}{'P(normal)':>11}"
          f"{'|rest ret|':>11}")
    for q, g in s.dropna(subset=["ib_q"]).groupby("ib_q"):
        dt_ = g["day_type"]
        print(f"   Q{int(q) + 1} {'(narrowest)' if q == 0 else '(widest)' if q == 4 else '':<24}"
              f"{len(g):>6}{g['ib_width'].median() * 100:>9.2f}%"
              f"{g['range_over_ib'].median():>10.2f}"
              f"{(dt_ == 'trend').mean() * 100:>9.0f}%"
              f"{dt_.isin(['neutral', 'neutral_extreme']).mean() * 100:>11.0f}%"
              f"{(dt_ == 'normal').mean() * 100:>10.0f}%"
              f"{g['rest_ret'].abs().median() * 100:>10.2f}%")
    nar = s[s["ib_pct_rank"] <= 0.2]
    wid = s[s["ib_pct_rank"] >= 0.8]
    if len(nar) > 30 and len(wid) > 30:
        pn, pw = (nar["day_type"] == "trend").mean(), (wid["day_type"] == "trend").mean()
        se = np.sqrt(pn * (1 - pn) / len(nar) + pw * (1 - pw) / len(wid))
        print(f"   narrow-IB P(trend) {pn * 100:.1f}% vs wide-IB {pw * 100:.1f}%"
              f"   diff {(pn - pw) * 100:+.1f}pp   z={(pn - pw) / max(se, 1e-9):+.2f}")
        print(f"   narrow-IB median range/IB {nar['range_over_ib'].median():.2f}"
              f" vs wide-IB {wid['range_over_ib'].median():.2f}"
              f"   |rest ret| {nar['rest_ret'].abs().median() * 100:.2f}%"
              f" vs {wid['rest_ret'].abs().median() * 100:.2f}%")

    # ── CLAIM B: does anything KNOWN AT 10:15 give DIRECTION? ───────────────
    print("\nCLAIM B-1: metrics known at the IB CLOSE vs the REST OF THE SESSION")
    print("   (signed direction is the point -- |rest ret| is magnitude only)")
    ic_block(s, IB_KNOWN, ["rest_ret", "rest_mfe", "rest_mae"])

    print("\nCLAIM B-2: metrics known at the SESSION CLOSE vs the NEXT session")
    print("   (these CANNOT be used on the session that produced them)")
    ic_block(s, CLOSE_KNOWN, ["fwd1", "next_open_ret", "fwd3"])

    # categorical MP reads, which is how a trader actually uses them
    print("\nCATEGORICAL AUCTION READS")
    for col, tgt, when in (("open_location", "rest_ret", "known 09:15"),
                           ("initiative", "rest_ret", "known 10:15"),
                           ("day_type", "fwd1", "known 15:15 -> next session"),
                           ("value_shift", "fwd1", "known 15:15 -> next session")):
        print(f"   {col} ({when}) vs {tgt}")
        for k, g in s.groupby(col):
            r = g[tgt].dropna()
            if len(r) < 30:
                continue
            print(f"      {str(k):<22}{len(r):>6}  mean {r.mean() * 100:>+6.2f}%"
                  f"  median {r.median() * 100:>+6.2f}%  t {t_of(r):>+6.2f}"
                  f"  P(up) {(r > 0).mean() * 100:>3.0f}%")
    for flag in ("poor_high", "poor_low", "failed_high", "failed_low", "double_dist"):
        a, b = s[s[flag]]["fwd1"].dropna(), s[~s[flag]]["fwd1"].dropna()
        if len(a) < 30:
            continue
        print(f"   {flag:<14} n={len(a):>4}  next-session mean {a.mean() * 100:>+6.2f}%"
              f"  t {t_of(a):>+6.2f}   vs rest {b.mean() * 100:>+6.2f}%")

    # The one directional signal, sized. rho says a relationship exists; only
    # basis points say which instrument can carry it.
    print("\nTHE ONE DIRECTIONAL SIGNAL, IN BASIS POINTS")
    print(f"   {'close_pos tertile':<26}{'n':>6}{'next open':>11}{'t':>7}"
          f"{'P(up)':>8}{'next close':>12}{'t':>7}")
    s["cp_t"] = pd.qcut(s["close_pos"], 3, labels=["weak", "mid", "strong"])
    for k, g in s.groupby("cp_t", observed=True):
        a, b = g["next_open_ret"].dropna(), g["fwd1"].dropna()
        print(f"   closed {str(k):<19}{len(g):>6}{a.mean() * 100:>+10.3f}%"
              f"{t_of(a):>+7.2f}{(a > 0).mean() * 100:>7.0f}%"
              f"{b.mean() * 100:>+11.3f}%{t_of(b):>+7.2f}")
    sp = s.dropna(subset=["cp_t"])
    strong = sp[sp["cp_t"] == "strong"]["next_open_ret"].dropna()
    weak = sp[sp["cp_t"] == "weak"]["next_open_ret"].dropna()
    spread = strong.mean() - weak.mean()
    se = np.sqrt(strong.var(ddof=1) / len(strong) + weak.var(ddof=1) / len(weak))
    print(f"   strong-minus-weak overnight spread {spread * 100:+.3f}%"
          f"   t={spread / max(se, 1e-9):+.2f}"
          f"   (median |overnight gap| {s['next_open_ret'].abs().median() * 100:.2f}%)")

    # split-half on whatever looked strongest, since one regime can carry it all
    print("\nSPLIT-HALF of the two headline reads")
    h = len(s) // 2
    for label, mask, tgt in (("initiative_buy", s["initiative"] == "initiative_buy", "rest_ret"),
                             ("initiative_sell", s["initiative"] == "initiative_sell", "rest_ret"),
                             ("open above value", s["open_location"] == "above_value", "rest_ret"),
                             ("open below value", s["open_location"] == "below_value", "rest_ret")):
        a = s[mask & (s.index < h)][tgt].dropna()
        b = s[mask & (s.index >= h)][tgt].dropna()
        if len(a) < 20 or len(b) < 20:
            continue
        print(f"   {label:<20} 1st half {a.mean() * 100:>+6.2f}% (n={len(a):>3})"
              f"   2nd half {b.mean() * 100:>+6.2f}% (n={len(b):>3})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
