"""Sixteen separate books: each bank trades its own strong close and compounds alone.

DIFFERENT FROM THE PORTFOLIO VERSION, AND LEGITIMATELY SO. mp_banks_overnight.py
equal-weighted the signalling names each night, which forces one basket and gives
a sparse two-name night the same weight as a crowded twelve-name night. Running
each symbol as its OWN book removes that: a name is either in its own trade or
flat, and nothing about how many siblings signalled changes its result.

The two structures genuinely disagree -- night-equal-weight returned -1.3bp,
per-symbol averages +8.6bp -- and the difference is the crowding effect, not an
error in either. Which one is right depends on whether capital is shared.

WHAT THIS STRUCTURE COSTS, and it must be priced honestly:
  SIXTEEN BOOKS NEED SIXTEEN UNITS OF CAPITAL, and each sits idle roughly seven
  nights in ten. Return per symbol flatters; return on the capital that had to be
  posted across all sixteen is the number a desk is judged on. Both are reported.
  COSTS BITE HARDER ON STOCKS than on index futures -- spread plus impact plus
  STT. The per-symbol edge is single-digit basis points, so the cost ladder is
  the test, not a footnote.
  THE t IS NOT WHAT IT LOOKS LIKE. 1,567 name-nights are not 1,567 independent
  observations: they cluster into ~252 sessions of correlated banks. A naive t
  overstates significance by roughly sqrt(names per night). Session-clustered
  errors are reported next to the naive ones so the inflation is visible.

    python vanguard/research/mp_banks_per_symbol.py
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
from research.mp_auction import dsn, load  # noqa: E402

RANK_WINDOW = 120
MIN_PERIODS = 60
COSTS_BPS = (0, 5, 10, 20)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=float, default=3.0)
    parser.add_argument("--dsn", default=dsn())
    args = parser.parse_args()

    connection = psycopg2.connect(args.dsn)
    try:
        s = load(connection, list(BANKS),
                 date.today() - timedelta(days=int(args.years * 365.25)))
    finally:
        connection.close()
    s = s.sort_values(["underlying", "dt"]).reset_index(drop=True)
    s["cp_rank"] = (s.groupby("underlying")["close_pos"]
                    .transform(lambda x: x.rolling(RANK_WINDOW, min_periods=MIN_PERIODS)
                               .apply(lambda w: (w[-1] > w[:-1]).mean(), raw=True)))
    s = s.dropna(subset=["cp_rank", "next_open_ret"])
    s["gap"] = s["next_open_ret"]
    s["gap_pts"] = s["close"] * s["gap"]
    tr = s[s["cp_rank"] >= 2 / 3].copy()
    span = (s["dt"].max() - s["dt"].min()).days / 365.25
    sess = s["dt"].nunique()

    print(f"{tr['underlying'].nunique()} banks, each ranked against its OWN trailing "
          f"{RANK_WINDOW} sessions")
    print(f"{s['dt'].min().date()} .. {s['dt'].max().date()}  ({span:.2f} years, "
          f"{sess} sessions)   {len(tr):,} trades total")

    # ── per-symbol books ────────────────────────────────────────────────────
    print(f"\nEACH BANK AS ITS OWN BOOK  (compounded over its own signal nights)")
    print(f"   {'name':<13}{'trades':>7}{'bp/nt':>8}{'median':>8}{'win':>6}"
          f"{'total%':>9}{'CAGR%':>8}{'maxDD%':>9}{'Sharpe':>8}{'t':>7}"
          f"{'@10bp%':>9}")
    rows = []
    for name, g in tr.groupby("underlying"):
        r = g["gap"].dropna()
        if len(r) < 20:
            continue
        eq = (1 + r).cumprod()
        dd = (eq / eq.cummax() - 1).min()
        sd = r.std(ddof=1)
        t = r.mean() / (sd / np.sqrt(len(r))) if sd > 0 else np.nan
        tot10 = (1 + r - 0.001).prod() - 1.0
        rows.append({"name": name, "n": len(r), "bp": r.mean() * 1e4,
                     "med": r.median() * 1e4, "win": (r > 0).mean(),
                     "tot": eq.iloc[-1] - 1.0,
                     "cagr": eq.iloc[-1] ** (1 / span) - 1.0, "dd": dd,
                     "sharpe": r.mean() / sd * np.sqrt(len(r) / span) if sd > 0 else np.nan,
                     "t": t, "tot10": tot10, "eq": eq, "r": r,
                     "pts": g["gap_pts"].mean()})
    for p in sorted(rows, key=lambda x: -x["tot"]):
        print(f"   {p['name']:<13}{p['n']:>7}{p['bp']:>+8.1f}{p['med']:>+8.1f}"
              f"{p['win'] * 100:>5.0f}%{p['tot'] * 100:>+9.1f}{p['cagr'] * 100:>+8.1f}"
              f"{p['dd'] * 100:>+9.1f}{p['sharpe']:>+8.2f}{p['t']:>+7.2f}"
              f"{p['tot10'] * 100:>+9.1f}")
    pos = sum(1 for p in rows if p["tot"] > 0)
    pos10 = sum(1 for p in rows if p["tot10"] > 0)
    from math import comb
    n = len(rows)
    pbin = float(sum(comb(n, i) * 0.5 ** n for i in range(pos, n + 1)))
    print(f"   {pos} of {n} books positive at 0bp  (binomial P>= = {pbin:.3f}),"
          f"  {pos10} of {n} at 10bp")

    # ── the aggregate, three ways ───────────────────────────────────────────
    print(f"\nTHE SIXTEEN BOOKS TOGETHER")
    avg_tot = np.mean([p["tot"] for p in rows])
    avg_bp = np.mean([p["bp"] for p in rows])
    print(f"   average book, 0bp        total {avg_tot * 100:+6.2f}%   "
          f"CAGR {((1 + avg_tot) ** (1 / span) - 1) * 100:+6.2f}%   "
          f"mean {avg_bp:+.1f} bp/night")
    for bps in COSTS_BPS:
        tots = [(1 + p["r"] - bps / 1e4).prod() - 1.0 for p in rows]
        npos = sum(1 for x in tots if x > 0)
        print(f"   average book, {bps:>2}bp       total {np.mean(tots) * 100:+6.2f}%   "
              f"CAGR {((1 + np.mean(tots)) ** (1 / span) - 1) * 100:+6.2f}%   "
              f"books positive {npos}/{n}")

    # capital actually posted: 16 books, each idle most nights
    util = len(tr) / (n * sess)
    print(f"\n   CAPITAL VIEW — each book is deployed on {len(tr) / n:.0f} of {sess} "
          f"sessions ({util * 100:.0f}% utilisation).")
    print(f"   Holding all {n} books, the return on the FULL {n}-unit capital base is the\n"
          f"   average book return above; the return on capital actually AT RISK is\n"
          f"   roughly {avg_bp:+.1f}bp per deployed night, which is the honest per-trade edge.")

    # ── the t-statistic, naive vs session-clustered ─────────────────────────
    print(f"\nSIGNIFICANCE: naive vs session-clustered")
    r_all = tr["gap"].dropna()
    naive_t = r_all.mean() / (r_all.std(ddof=1) / np.sqrt(len(r_all)))
    per_sess = tr.groupby("dt")["gap"].mean()
    clus_t = per_sess.mean() / (per_sess.std(ddof=1) / np.sqrt(len(per_sess)))
    k = len(tr) / tr["dt"].nunique()
    print(f"   pooled name-nights   n={len(r_all):,}  mean {r_all.mean() * 1e4:+.1f}bp"
          f"   t={naive_t:+.2f}   <- treats correlated banks as independent")
    print(f"   clustered by session n={len(per_sess):,}  mean "
          f"{per_sess.mean() * 1e4:+.1f}bp   t={clus_t:+.2f}   <- the honest one")
    print(f"   mean {k:.1f} names per signalling night; naive/clustered ratio "
          f"{naive_t / clus_t if clus_t else float('nan'):.2f} "
          f"(sqrt of names per night = {np.sqrt(k):.2f})")

    # ── robustness on the per-symbol structure ──────────────────────────────
    print(f"\nSPLIT-HALF PER BOOK (0bp, bp per night)")
    print(f"   {'name':<13}{'1st half':>10}{'2nd half':>10}{'both +?':>9}")
    both = 0
    for p in sorted(rows, key=lambda x: -x["bp"]):
        h = len(p["r"]) // 2
        a, b = p["r"].iloc[:h].mean() * 1e4, p["r"].iloc[h:].mean() * 1e4
        ok = a > 0 and b > 0
        both += ok
        print(f"   {p['name']:<13}{a:>+10.1f}{b:>+10.1f}{'yes' if ok else '':>9}")
    print(f"   {both} of {n} books positive in BOTH halves "
          f"(chance alone would give ~{n * 0.25:.0f} if each half were a coin flip)")

    out = os.environ.get("SYMS_OUT")
    if out:
        recs = []
        for p in rows:
            eq = (1 + p["r"] - 0.0005).cumprod()
            recs.append({"name": p["name"], "n": len(eq),
                         "eq": ",".join(f"{v:.4f}" for v in eq.values),
                         "total5": eq.iloc[-1] - 1.0, "bp": p["bp"]})
        pd.DataFrame(recs).to_csv(out, index=False)
        print(f"\nper-symbol curves written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
