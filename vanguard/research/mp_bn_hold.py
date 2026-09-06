"""BANKNIFTY only: the acceptance signal held for 0, 1, 2 and 3 days.

SIGNAL, unchanged from the bank-stock work: the session closes ABOVE its
value-area high AND in the 0.70-0.90 band of the day's range -- acceptance above
value, not a spike at the extreme.

WHY BANKNIFTY IS THE BETTER TEST. The bank stocks carry 349 sessions; BANKNIFTY
carries roughly 1,290 back to June 2021. That is four times the sample and spans
2022's drawdown as well as the 2023-26 advance, so a result here is not a single
regime. The index also has zero volume in this table, which is why the profile is
TPO-based -- the correct MP construction anyway.

THE OVERLAP PROBLEM, WHICH DOES NOT ARISE FOR A BASKET. Sixteen stocks can each
hold their own position. ONE instrument cannot: if a signal fires on day t and
again on t+1 while a 3-day hold is still open, compounding both as separate trades
quietly assumes two positions and double the capital. So every multi-day hold is
reported twice:

    per-trade      every signal compounded independently -- comparable to the
                   stock work, and an upper bound
    NON-OVERLAP    a single position at a time; a signal arriving while the book
                   is already long is SKIPPED. This is what one account does, and
                   it is the honest curve.

    python vanguard/research/mp_bn_hold.py
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

CASES = [("gap only (sell 09:15 open)", "r_gap", 0),
         ("hold to day 1 close", "r_d1", 1),
         ("hold to day 2 close", "r_d2", 2),
         ("hold to day 3 close", "r_d3", 3)]


def stats(r: pd.Series, span: float, held: int) -> dict:
    r = r.dropna()
    if len(r) < 15:
        return {}
    eq = (1 + r / 100).cumprod()
    dd = (eq / eq.cummax() - 1).min()
    sd = r.std(ddof=1)
    return {"n": len(r), "mean": r.mean(), "med": r.median(), "win": (r > 0).mean(),
            "t": r.mean() / (sd / np.sqrt(len(r))), "sd": sd,
            "total": eq.iloc[-1] - 1, "cagr": eq.iloc[-1] ** (1 / span) - 1,
            "dd": dd, "sharpe": r.mean() / sd * np.sqrt(len(r) / span),
            "eq": eq, "held": max(held, 1)}


def line(lab: str, s: dict) -> None:
    if not s:
        print(f"   {lab:<32}   (too few)")
        return
    print(f"   {lab:<32}{s['n']:>6}{s['mean']:>+9.3f}{s['med']:>+9.3f}"
          f"{s['win'] * 100:>5.0f}%{s['t']:>+7.2f}{s['total'] * 100:>+10.1f}"
          f"{s['cagr'] * 100:>+8.1f}{s['dd'] * 100:>+8.1f}{s['sharpe']:>+8.2f}")


def non_overlap(d: pd.DataFrame, col: str, hold: int) -> pd.Series:
    """Take a signal only when the book is flat; a hold of h blocks h sessions."""
    idx = d.index.to_numpy()
    take, busy_until = [], -1
    for i, pos in enumerate(idx):
        if pos <= busy_until:
            continue
        take.append(pos)
        busy_until = pos + max(hold, 0)
    return d.loc[take, col]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BANKNIFTY")
    parser.add_argument("--years", type=float, default=5.2)
    parser.add_argument("--dsn", default=dsn())
    args = parser.parse_args()

    connection = psycopg2.connect(args.dsn)
    try:
        s = load(connection, [args.symbol],
                 date.today() - timedelta(days=int(args.years * 365.25)))
    finally:
        connection.close()
    s = s.sort_values("dt").reset_index(drop=True)
    for k in (1, 2, 3):
        s[f"c{k}"], s[f"h{k}"], s[f"l{k}"] = (s["close"].shift(-k), s["high"].shift(-k),
                                              s["low"].shift(-k))
    s["o1"] = s["open"].shift(-1)
    c0 = s["close"]
    s["r_gap"] = (s["o1"] / c0 - 1) * 100
    s["r_d1"] = (s["c1"] / c0 - 1) * 100
    s["r_d2"] = (s["c2"] / c0 - 1) * 100
    s["r_d3"] = (s["c3"] / c0 - 1) * 100
    s["leg_intra1"] = (s["c1"] / s["o1"] - 1) * 100
    s["leg_day2"] = (s["c2"] / s["c1"] - 1) * 100
    s["leg_day3"] = (s["c3"] / s["c2"] - 1) * 100
    s["mfe3"] = (s[["h1", "h2", "h3"]].max(axis=1) / c0 - 1) * 100
    s["mae3"] = (s[["l1", "l2", "l3"]].min(axis=1) / c0 - 1) * 100
    s = s.dropna(subset=["vah", "r_gap"]).reset_index(drop=True)

    sig = s[(s["close"] > s["vah"]) & s["close_pos"].between(.70, .90)]
    span = (s["dt"].max() - s["dt"].min()).days / 365.25
    print(f"{args.symbol}  {len(s):,} sessions  {s['dt'].min().date()} .. "
          f"{s['dt'].max().date()}  ({span:.1f} years)")
    print(f"signals {len(sig)} ({len(sig) / len(s) * 100:.1f}% of sessions), "
          f"~{len(sig) / span:.0f} per year")

    hdr = (f"   {'case':<32}{'n':>6}{'mean %':>9}{'median':>9}{'win':>5}{'t':>7}"
           f"{'total %':>10}{'CAGR %':>8}{'maxDD':>8}{'Sharpe':>8}")
    print(f"\n1. PER-TRADE COMPOUNDING (upper bound; assumes overlaps can be held)")
    print(hdr)
    per_trade = {}
    for lab, col, hold in CASES:
        per_trade[col] = stats(sig[col], span, hold)
        line(lab, per_trade[col])

    print(f"\n2. NON-OVERLAPPING — one position at a time (the honest curve)")
    print(hdr)
    no = {}
    for lab, col, hold in CASES:
        r = non_overlap(sig, col, hold)
        no[col] = stats(r, span, hold)
        line(f"{lab}", no[col])
    for lab, col, hold in CASES:
        if hold:
            kept = len(non_overlap(sig, col, hold))
            print(f"   {lab}: {kept} of {len(sig)} signals taken "
                  f"({kept / len(sig) * 100:.0f}%) once overlaps are excluded")

    print(f"\n3. BUY & HOLD BANKNIFTY over the same window")
    px = s["close"] / s["close"].iloc[0]
    print(f"   total {(px.iloc[-1] - 1) * 100:+.1f}%   "
          f"CAGR {(px.iloc[-1] ** (1 / span) - 1) * 100:+.1f}%   "
          f"maxDD {(px / px.cummax() - 1).min() * 100:+.1f}%   "
          f"(24h exposure, every session)")

    print(f"\n4. WHAT EACH ADDED LEG CONTRIBUTES (signal vs every session)")
    print(f"   {'leg':<32}{'signal':>10}{'all sessions':>15}{'ratio':>8}{'t (signal)':>12}")
    for lab, col in (("the gap itself", "r_gap"),
                     ("day 1 intraday (open->close)", "leg_intra1"),
                     ("day 2 (close->close)", "leg_day2"),
                     ("day 3 (close->close)", "leg_day3")):
        a, b = sig[col].dropna(), s[col].dropna()
        t = a.mean() / (a.std(ddof=1) / np.sqrt(len(a)))
        print(f"   {lab:<32}{a.mean():>+10.3f}{b.mean():>+15.3f}"
              f"{a.mean() / b.mean() if b.mean() else np.nan:>8.2f}{t:>+12.2f}")

    print(f"\n5. TAIL CAPTURE BY HORIZON")
    print(f"   {'exit':<32}{'P>1% sig':>10}{'P>1% all':>10}{'lift':>7}"
          f"{'P>2% sig':>10}{'P>2% all':>10}{'lift':>7}")
    for lab, col, _ in CASES + [("best point in 3 days", "mfe3", 3)]:
        a, b = sig[col].dropna(), s[col].dropna()
        p1a, p1b = (a > 1).mean(), (b > 1).mean()
        p2a, p2b = (a > 2).mean(), (b > 2).mean()
        print(f"   {lab:<32}{p1a * 100:>9.1f}%{p1b * 100:>9.1f}%"
              f"{p1a / p1b if p1b else np.nan:>7.2f}{p2a * 100:>9.1f}%"
              f"{p2b * 100:>9.1f}%{p2a / p2b if p2b else np.nan:>7.2f}")

    print(f"\n6. BY YEAR (non-overlapping, mean % per trade)")
    yrs = sorted(sig["dt"].dt.year.unique())
    print(f"   {'case':<32}" + "".join(f"{y:>9}" for y in yrs))
    for lab, col, hold in CASES:
        r = non_overlap(sig, col, hold)
        d = sig.loc[r.index]
        cells = ""
        for y in yrs:
            v = r[d["dt"].dt.year == y]
            cells += f"{'.':>9}" if len(v) < 3 else f"{v.mean():>+9.3f}"
        print(f"   {lab:<32}{cells}")
    print(f"   {'trades':<32}" + "".join(
        f"{int((sig['dt'].dt.year == y).sum()):>9}" for y in yrs))

    out = os.environ.get("BN_OUT")
    if out:
        recs = []
        for lab, col, hold in CASES:
            r = non_overlap(sig, col, hold).dropna()
            d = sig.loc[r.index]
            eq = (1 + r / 100).cumprod()
            recs.append({"case": lab, "dts": ",".join(str(x)[:10] for x in d["dt"]),
                         "eq": ",".join(f"{v:.4f}" for v in eq)})
        bh = s[["dt", "close"]].copy()
        recs.append({"case": "buy & hold",
                     "dts": ",".join(str(x)[:10] for x in bh["dt"]),
                     "eq": ",".join(f"{v:.4f}" for v in bh["close"] / bh["close"].iloc[0])})
        pd.DataFrame(recs).to_csv(out, index=False)
        print(f"\ncurves written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
