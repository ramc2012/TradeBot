"""A concrete strategy for a 2% BANKNIFTY move in 3-4 days, and its trade log.

WHAT THE BASE-RATE PASS ESTABLISHED, and it reshaped the design:

  THE OPPORTUNITY IS LARGE. An up-2% touch happens within 4 sessions from 28.2%
  of closes -- 68 chances a year. Either side, 53.5%, 130 a year. So the problem
  is never finding a move; it is beating a 28% base rate.

  COMPRESSION DOES NOT PRECEDE EXPANSION -- the classic MP teaching fails here.
  Narrow value area 0.87x, narrow IB 0.84x, low ATR 0.89x, narrow VA + low ATR
  0.86x, balance runs 0.89x. Every compression measure LOWERS the odds of a 2%
  move. Volatility clusters: a quiet day is followed by quiet days.

  THE TENSION THAT DEFINES THE STRATEGY. High ATR is much the strongest predictor
  of a 2% move (P(up) 40% vs 28%, lift 1.42) but is nearly directionless (skew
  1.15). The DIRECTIONAL conditions are trend day (skew 1.58), value shifted
  higher_outside (skew 1.43, t+2.64) and -- oddly -- low ATR (skew 1.30, and the
  highest signed t at +3.79) which produces the FEWEST 2% moves. Magnitude and
  direction come from opposite regimes, so the strategy has to buy one and pay
  for the other.

THE TRADE, fully specified so nothing is decided after the fact:
    entry   at the 15:30 close of the signal session
    target  +2% (a resting order; filled on any subsequent session's high)
    stop    -1% intraday
    expiry  exit at the close of session 4 if neither is hit
    when both the target and the stop fall inside the SAME session, the STOP is
    assumed first -- the conservative reading, and its frequency is reported so
    the result cannot rest on that assumption

    python vanguard/research/mp_bn_2pct_strategy.py
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


def simulate(s: pd.DataFrame, mask: pd.Series, target: float, stop: float,
             days: int) -> pd.DataFrame:
    """Walk each trade forward session by session; target and stop are percents."""
    out = []
    idx = np.flatnonzero(mask.to_numpy())
    hi, lo, cl = s["high"].to_numpy(), s["low"].to_numpy(), s["close"].to_numpy()
    for i in idx:
        if i + days >= len(s):
            continue
        entry = cl[i]
        tp, sl = entry * (1 + target / 100), entry * (1 + stop / 100)
        res, exit_day, amb = None, days, False
        for k in range(1, days + 1):
            hit_t, hit_s = hi[i + k] >= tp, lo[i + k] <= sl
            if hit_t and hit_s:
                amb = True
                res, exit_day = stop, k          # conservative: stop first
                break
            if hit_s:
                res, exit_day = stop, k
                break
            if hit_t:
                res, exit_day = target, k
                break
        if res is None:
            res = (cl[i + days] / entry - 1) * 100
        out.append({"i": i, "dt": s["dt"].iloc[i], "ret": res, "days": exit_day,
                    "ambiguous": amb, "hit_target": res == target,
                    "hit_stop": res == stop and not amb})
    return pd.DataFrame(out)


def report(lab: str, t: pd.DataFrame, span: float) -> None:
    if len(t) < 20:
        print(f"   {lab:<38}{len(t):>6}   (too few)")
        return
    r = t["ret"]
    eq = (1 + r / 100).cumprod()
    sd = r.std(ddof=1)
    print(f"   {lab:<38}{len(t):>6}{len(t) / span:>7.0f}{t['hit_target'].mean() * 100:>8.0f}%"
          f"{t['hit_stop'].mean() * 100:>7.0f}%{r.mean():>+9.3f}{r.median():>+8.2f}"
          f"{(r > 0).mean() * 100:>6.0f}%{r.mean() / (sd / np.sqrt(len(r))):>+7.2f}"
          f"{(eq.iloc[-1] - 1) * 100:>+10.1f}{(eq / eq.cummax() - 1).min() * 100:>+8.1f}"
          f"{t['days'].mean():>7.1f}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BANKNIFTY")
    parser.add_argument("--years", type=float, default=5.2)
    parser.add_argument("--target", type=float, default=2.0)
    parser.add_argument("--stop", type=float, default=-1.0)
    parser.add_argument("--days", type=int, default=4)
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
    s["atr_pct"] = s["atr20"] * 100
    s["atr_hi"] = s["atr_pct"] >= s["atr_pct"].rolling(120, min_periods=60).quantile(0.67)
    s["higher_outside"] = s["value_shift"] == "higher_outside"
    s["trend_day"] = s["day_type"] == "trend"
    s["above_vah"] = s["close"] > s["vah"]
    s = s.dropna(subset=["atr_pct", "vah"]).reset_index(drop=True)
    span = (s["dt"].max() - s["dt"].min()).days / 365.25

    print(f"{args.symbol}  {len(s):,} sessions  {s['dt'].min().date()} .. "
          f"{s['dt'].max().date()}  ({span:.1f}y)")
    print(f"trade: enter at the close, target +{args.target:.0f}%, stop "
          f"{args.stop:.0f}%, exit at the day-{args.days} close if neither hits")

    RULES = {
        "every session (base rate)": pd.Series(True, index=s.index),
        "high ATR regime": s["atr_hi"],
        "value shifted higher_outside": s["higher_outside"],
        "trend day": s["trend_day"],
        "closed above VAH": s["above_vah"],
        "high ATR + higher_outside": s["atr_hi"] & s["higher_outside"],
        "high ATR + trend day": s["atr_hi"] & s["trend_day"],
        "high ATR + above VAH": s["atr_hi"] & s["above_vah"],
        "higher_outside + above VAH": s["higher_outside"] & s["above_vah"],
        "high ATR + (higher_out OR trend)": s["atr_hi"] & (s["higher_outside"]
                                                           | s["trend_day"]),
    }
    print(f"\n   {'rule':<38}{'n':>6}{'/yr':>7}{'target':>8}{'stop':>7}"
          f"{'mean %':>9}{'median':>8}{'win':>6}{'t':>7}{'total %':>10}"
          f"{'maxDD':>8}{'days':>7}")
    results = {}
    for lab, m in RULES.items():
        t = simulate(s, m.fillna(False), args.target, args.stop, args.days)
        results[lab] = t
        report(lab, t, span)
    amb = pd.concat(results.values())["ambiguous"].mean()
    print(f"   target and stop inside the same session on {amb * 100:.1f}% of trades "
          f"(counted as stops)")

    # ── the best rule in detail ─────────────────────────────────────────────
    best = max((k for k in results if k != "every session (base rate)"),
               key=lambda k: results[k]["ret"].mean() if len(results[k]) >= 20 else -9)
    t = results[best]
    print(f"\nBEST RULE: {best}   ({len(t)} trades, {len(t) / span:.0f}/yr)")
    print(f"   hit the +{args.target:.0f}% target   {t['hit_target'].mean() * 100:.0f}%"
          f"   stopped out {t['hit_stop'].mean() * 100:.0f}%"
          f"   timed out {(1 - t['hit_target'].mean() - t['hit_stop'].mean()) * 100:.0f}%")
    print(f"   mean {t['ret'].mean():+.3f}%   median {t['ret'].median():+.2f}%   "
          f"avg holding {t['days'].mean():.1f} sessions")
    h = len(t) // 2
    print(f"   split-half {t['ret'].iloc[:h].mean():+.3f}% / "
          f"{t['ret'].iloc[h:].mean():+.3f}%   "
          f"drop 2 best {t['ret'].drop(t['ret'].nlargest(2).index).mean():+.3f}%")
    print(f"   by year:")
    for y, g in t.groupby(t["dt"].dt.year):
        print(f"      {y}  n={len(g):>3}  target hit {g['hit_target'].mean() * 100:>3.0f}%"
              f"   mean {g['ret'].mean():>+7.3f}%   total "
              f"{((1 + g['ret'] / 100).prod() - 1) * 100:>+6.1f}%")

    # ── does the target/stop pair matter? ───────────────────────────────────
    print(f"\nTARGET / STOP GRID on the best rule (mean % per trade)")
    m = RULES[best].fillna(False)
    hdr = "target / stop"
    print(f"   {hdr:<16}" + "".join(f"{f'-{x}%':>10}" for x in (0.75, 1.0, 1.5, 2.0)))
    for tgt in (1.5, 2.0, 2.5, 3.0):
        cells = ""
        for stp in (0.75, 1.0, 1.5, 2.0):
            tt = simulate(s, m, tgt, -stp, args.days)
            cells += f"{tt['ret'].mean():>+10.3f}" if len(tt) >= 20 else f"{'-':>10}"
        print(f"   +{tgt:<15.1f}{cells}")
    print(f"\n   A grid this size is 16 combinations; the best cell in a 16-cell grid\n"
          f"   beats zero by chance fairly often, so read the SHAPE — whether the\n"
          f"   surface is smooth and broadly positive — not the maximum.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
