"""A 2% BANKNIFTY move inside 3-4 days: how often, and what predicts it.

TWO QUESTIONS, in order. How many chances are there at all -- the base rate
bounds everything. Then, which conditions available at the 15:15 close raise the
probability above that base rate.

WHAT EVERYTHING SO FAR SAYS THE ANSWER WILL LOOK LIKE. Across this whole study,
BANKNIFTY's auction has predicted RANGE and not DIRECTION: every metric known at
the IB close scored |t| <= 1.74 against signed rest-of-session return, while the
same metrics predicted the up-excursion and the down-excursion with the same sign
and t above 10. The one directional edge found -- a strong close predicting the
next open -- is spent by 09:15 and the following days actively mean-revert
(-0.143% intraday against -0.006% on an average day). So a DIRECTIONAL 2% target
starts from a weak base, while a TWO-SIDED 2% target starts from a strong one.
Both are therefore measured, and the difference between them is the whole design
decision: it determines whether this is a futures trade or a long-volatility one.

THE UNTESTED MP IDEA THIS PUTS TO WORK. Compression precedes expansion -- a
narrow value area, a small IB, overlapping value day after day means the auction
is balanced and has nowhere left to go. That is the classic MP setup for a large
move and it has NOT been tested anywhere in this project, because everything so
far conditioned on strength rather than on balance. It is the one genuinely new
lever available.

    python vanguard/research/mp_bn_2pct.py
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

TARGET = 2.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BANKNIFTY")
    parser.add_argument("--years", type=float, default=5.2)
    parser.add_argument("--target", type=float, default=TARGET)
    parser.add_argument("--dsn", default=dsn())
    args = parser.parse_args()

    connection = psycopg2.connect(args.dsn)
    try:
        s = load(connection, [args.symbol],
                 date.today() - timedelta(days=int(args.years * 365.25)))
    finally:
        connection.close()
    s = s.sort_values("dt").reset_index(drop=True)
    c0 = s["close"]
    for h in (3, 4):
        hi = pd.concat([s["high"].shift(-k) for k in range(1, h + 1)], axis=1).max(axis=1)
        lo = pd.concat([s["low"].shift(-k) for k in range(1, h + 1)], axis=1).min(axis=1)
        s[f"up{h}"] = (hi / c0 - 1) * 100
        s[f"dn{h}"] = (lo / c0 - 1) * 100
        s[f"cc{h}"] = (s["close"].shift(-h) / c0 - 1) * 100

    # ---- compression / balance features, all known at the close ----
    s["va_pct"] = (s["vah"] - s["val"]) / c0 * 100
    s["va_narrow"] = s["va_pct"] < s["va_pct"].rolling(60, min_periods=30).quantile(0.33)
    s["ib_narrow"] = s["ib_pct_rank"] <= 0.33
    s["rng_pct"] = (s["high"] - s["low"]) / c0 * 100
    s["atr_pct"] = s["atr20"] * 100
    s["atr_low"] = s["atr_pct"] < s["atr_pct"].rolling(120, min_periods=60).quantile(0.33)
    s["overlap_hi"] = s["va_overlap"] > 0.6
    # consecutive balanced sessions: value overlapping the prior day's value
    run, runs = 0, []
    for v in s["overlap_hi"].fillna(False):
        run = run + 1 if v else 0
        runs.append(run)
    s["balance_run"] = runs
    s["inside_value"] = (s["close"] <= s["vah"]) & (s["close"] >= s["val"])
    s["accept"] = (s["close"] > s["vah"]) & s["close_pos"].between(.70, .90)

    d = s.dropna(subset=["up3", "dn3", "up4", "dn4", "va_pct", "atr_pct"]).reset_index(drop=True)
    span = (d["dt"].max() - d["dt"].min()).days / 365.25
    T = args.target
    print(f"{args.symbol}  {len(d):,} sessions  {d['dt'].min().date()} .. "
          f"{d['dt'].max().date()}  ({span:.1f} years)")

    # ── 1. HOW MANY CHANCES ARE THERE? ──────────────────────────────────────
    print(f"\n1. BASE RATES — how often does a {T:.0f}% move happen at all?")
    print(f"   {'event (from the 15:30 close)':<40}{'sessions':>10}{'rate':>8}{'per year':>10}")
    ev = [
        (f"UP {T:.0f}% touched within 3 days", d["up3"] >= T),
        (f"UP {T:.0f}% touched within 4 days", d["up4"] >= T),
        (f"DOWN {T:.0f}% touched within 3 days", d["dn3"] <= -T),
        (f"DOWN {T:.0f}% touched within 4 days", d["dn4"] <= -T),
        (f"EITHER side {T:.0f}% within 3 days", (d["up3"] >= T) | (d["dn3"] <= -T)),
        (f"EITHER side {T:.0f}% within 4 days", (d["up4"] >= T) | (d["dn4"] <= -T)),
        (f"BOTH sides {T:.0f}% within 4 days", (d["up4"] >= T) & (d["dn4"] <= -T)),
        (f"close-to-close +{T:.0f}% in 3 days", d["cc3"] >= T),
        (f"close-to-close +{T:.0f}% in 4 days", d["cc4"] >= T),
    ]
    for lab, m in ev:
        n = int(m.sum())
        print(f"   {lab:<40}{n:>10}{m.mean() * 100:>7.1f}%{n / span:>10.0f}")
    print(f"   'touched' uses the running high/low, so it is achievable with a\n"
          f"   resting target; close-to-close requires the move to still be there\n"
          f"   at the end, which is a much harder bar and roughly a third as common.")

    # ── 2. WHAT RAISES THE PROBABILITY? ─────────────────────────────────────
    up4 = d["up4"] >= T
    dn4 = d["dn4"] <= -T
    either = up4 | dn4
    print(f"\n2. CONDITIONS, scored on P(up {T:.0f}% in 4d) and P(either side)")
    print(f"   {'condition (known at the close)':<40}{'n':>6}{'P(up)':>8}{'lift':>7}"
          f"{'P(either)':>11}{'lift':>7}{'med up4':>9}{'med dn4':>9}")
    bu, be = up4.mean(), either.mean()
    conds = [
        ("(base rate: every session)", pd.Series(True, index=d.index)),
        ("-- compression / balance --", None),
        ("narrow value area (bottom third)", d["va_narrow"]),
        ("narrow IB (own bottom third)", d["ib_narrow"]),
        ("low ATR regime (bottom third)", d["atr_low"]),
        ("narrow VA + low ATR", d["va_narrow"] & d["atr_low"]),
        ("balanced 2+ sessions running", d["balance_run"] >= 2),
        ("balanced 3+ sessions running", d["balance_run"] >= 3),
        ("closed inside value", d["inside_value"]),
        ("inside value + narrow VA", d["inside_value"] & d["va_narrow"]),
        ("-- expansion / strength --", None),
        ("acceptance signal (close>VAH band)", d["accept"]),
        ("closed above VAH (any)", d["close"] > d["vah"]),
        ("closed below VAL", d["close"] < d["val"]),
        ("day type = trend", d["day_type"] == "trend"),
        ("range/IB >= 2", d["range_over_ib"] >= 2.0),
        ("high ATR regime (top third)", ~d["atr_low"] & (d["atr_pct"] >
                                                         d["atr_pct"].quantile(0.67))),
        ("poor high (unfinished at the top)", d["poor_high"]),
        ("poor low (unfinished at the bottom)", d["poor_low"]),
        ("value shifted higher_outside", d["value_shift"] == "higher_outside"),
        ("value shifted lower_outside", d["value_shift"] == "lower_outside"),
    ]
    for lab, m in conds:
        if m is None:
            print(f"   {lab}")
            continue
        m = m.fillna(False)
        if m.sum() < 30:
            print(f"   {lab:<40}{int(m.sum()):>6}   (too few)")
            continue
        g = d[m]
        pu, pe = up4[m].mean(), either[m].mean()
        print(f"   {lab:<40}{int(m.sum()):>6}{pu * 100:>7.0f}%{pu / bu:>7.2f}"
              f"{pe * 100:>10.0f}%{pe / be:>7.2f}{g['up4'].median():>+9.2f}"
              f"{g['dn4'].median():>+9.2f}")

    # ── 3. THE DIRECTIONAL vs TWO-SIDED CHOICE ──────────────────────────────
    print(f"\n3. IS THE EDGE DIRECTIONAL OR TWO-SIDED?")
    print(f"   {'condition':<40}{'P(up2)':>9}{'P(dn2)':>9}{'skew':>8}"
          f"{'mean cc4':>10}{'t':>7}")
    for lab, m in [(l, mm) for l, mm in conds if mm is not None]:
        m = m.fillna(False)
        if m.sum() < 30:
            continue
        pu, pd_ = up4[m].mean(), dn4[m].mean()
        cc = d.loc[m, "cc4"].dropna()
        t = cc.mean() / (cc.std(ddof=1) / np.sqrt(len(cc))) if cc.std(ddof=1) > 0 else np.nan
        print(f"   {lab:<40}{pu * 100:>8.0f}%{pd_ * 100:>8.0f}%"
              f"{pu / pd_ if pd_ else np.nan:>8.2f}{cc.mean():>+10.3f}{t:>+7.2f}")
    print("   'skew' is P(up)/P(down). Near 1.0 means the condition predicts a MOVE\n"
          "   but not its direction — tradeable with a straddle, not with futures.\n"
          "   'mean cc4' is the signed 4-day close-to-close return, the futures P&L.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
