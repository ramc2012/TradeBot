"""IB / range-extension SCALPS on BANKNIFTY 30m bars, judged out-of-sample.

THE FAMILY. Everything here is an intraday trade that is opened at the close of
a 30-minute bar between 10:15 and 14:15 (bar index 2..10, i.e. after the initial
balance has completed at 10:15 and with at least two bars left) and closed the
same session. Two exits are measured: the session close (r_eod, 15:15) and a
fixed two-bar horizon (60 minutes).

THE CANDIDATE SET IS FIXED AT TEN RULES AND IS STATED HERE BEFORE ANY RESULT.
No rule was added after seeing a number; no threshold is tuned inside a fold.

  1  ibc         IB BREAK CONTINUATION. First bar of the session whose CLOSE is
                 beyond an IB extreme. Traded in the break direction.
  2  ibc_narrow  (1) restricted to narrow-IB sessions
  3  ibc_wide    (1) restricted to wide-IB sessions
                 narrow/wide = ib_width below/above its own trailing 60-session
                 median, computed on sessions STRICTLY BEFORE the current one.
  4  ibf         IB BREAK FAILURE. A bar that closes back INSIDE the IB after a
                 previous bar had closed beyond it. Faded: traded AGAINST the
                 original break direction.
  5  ibf_narrow  (4) on narrow-IB sessions
  6  ibf_wide    (4) on wide-IB sessions
  7  ext05       EXTENSION EXHAUSTION 0.5. Range extension reached >= 0.50 IB
                 widths beyond the broken extreme, then the first bar whose
                 extension is SMALLER than the prior bar's. Faded.
  8  ext10       EXTENSION EXHAUSTION 1.0. Same with a 1.00 IB-width trigger.
  9  odrive      OPEN DRIVE. Bar 0 closes in the top (bottom) 20% of its own
                 range and bar 1 closes above (below) bar 0's close. Traded as
                 continuation.
 10  orej        OPEN REJECTION REVERSE. Bar 0 opens in the top third of its own
                 range and closes in the bottom third (or the mirror image).
                 Traded in the direction of the reversal.

  Rules 9 and 10 are known at the close of bar 1 (10:15). The mandated entry
  window starts at bar 2, so they are entered at the close of bar 2 (10:45).
  That is a real half-hour of slippage against the idea and is reported as such.

  At most ONE trade per session per rule (the first firing bar), so trades never
  overlap within a rule and the t-statistic is over independent sessions.

MECHANICS. Each rule contributes its own rows to a stacked trade frame carrying
the SIGNED return (short trades negated), so one walk-forward over one return
column can choose between long and short rules honestly. Selection is by
in-sample mean on an expanding 18-month window; the next 6 months are traded
with that choice; only those out-of-sample stretches are concatenated.

COSTS. BANKNIFTY futures round trip is 3-5bp. Everything is reported gross and
net of 4bp (0.04% deducted per trade), and the walk-forward is run a second time
where the SELECTION itself sees the net return -- a rule that only wins before
costs should not be chosen.

    docker exec nomadcurie_vanguard_cycle python /vanguard/research/mp_ib_scalp.py
"""
from __future__ import annotations

import argparse
import os
import pickle
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.mp_auction import dsn  # noqa: E402
from research.mp_intraday import load_intraday  # noqa: E402
from research.mp_walkforward import HEADER, report, walk_forward  # noqa: E402

COST = 0.04          # 4bp round trip, in percent
ENTRY_LO, ENTRY_HI = 2, 10
CACHE = "/tmp/mp_ib_scalp_bars.pkl"

RULES = ["ibc", "ibc_narrow", "ibc_wide", "ibf", "ibf_narrow", "ibf_wide",
         "ext05", "ext10", "odrive", "orej"]


def t_of(x: pd.Series) -> float:
    x = x.dropna()
    if len(x) < 5 or x.std(ddof=1) == 0:
        return np.nan
    return x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))


def load_bars(args) -> pd.DataFrame:
    if os.path.exists(CACHE) and not args.refresh:
        with open(CACHE, "rb") as fh:
            return pickle.load(fh)
    start = date.today() - timedelta(days=int(args.years * 365.25))
    connection = psycopg2.connect(args.dsn)
    try:
        f = load_intraday(connection, [args.symbol], start)
    finally:
        connection.close()
    with open(CACHE, "wb") as fh:
        pickle.dump(f, fh)
    return f


def add_horizon(f: pd.DataFrame) -> pd.DataFrame:
    """Two-bar (60 minute) forward return, truncated at the session close."""
    out = []
    for _, g in f.groupby(["underlying", "dt"], sort=False):
        g = g.sort_values("bar").reset_index(drop=True)
        c = g["close"].values
        n = len(c)
        idx = np.minimum(np.arange(n) + 2, n - 1)
        g["r_h2"] = (c[idx] / c - 1) * 100
        out.append(g)
    return pd.concat(out, ignore_index=True)


def signals(f: pd.DataFrame) -> pd.DataFrame:
    """One row per (session, rule) that fired, with the entry bar and side."""
    # causal narrow/wide classification of the session's IB
    sess = f.groupby("dt")["ib_width"].first().sort_index()
    trail = sess.rolling(60, min_periods=20).median().shift(1)
    narrow = (sess < trail)
    known = trail.notna()

    rows = []
    for dt, g in f.groupby("dt", sort=True):
        g = g.sort_values("bar").reset_index(drop=True)
        n = len(g)
        bar = g["bar"].values
        cl = g["close"].values
        op = g["open"].values
        hi = g["high"].values
        lo = g["low"].values
        brk = g["ib_broken"].values.astype(bool)
        side = g["ib_side"].values.astype(int)
        ext = g["ext_ib"].values.astype(float)
        ib_hi, ib_lo = g["ib_hi"].iloc[0], g["ib_lo"].iloc[0]
        ok = (bar >= ENTRY_LO) & (bar <= ENTRY_HI)
        is_narrow = bool(narrow.get(dt, False)) if bool(known.get(dt, False)) else None

        def emit(rule, k, s):
            rows.append({"dt": dt, "rule": rule, "bar": int(bar[k]), "side": int(s),
                         "r_eod": float(g["r_eod"].iloc[k]) * s,
                         "r_h2": float(g["r_h2"].iloc[k]) * s})

        # --- 1..3  IB BREAK CONTINUATION -----------------------------------
        first_brk = None
        for k in range(n):
            if brk[k] and (k == 0 or not brk[k - 1]):
                first_brk = k
                break
        if first_brk is not None and ok[first_brk]:
            s = side[first_brk]
            emit("ibc", first_brk, s)
            if is_narrow is True:
                emit("ibc_narrow", first_brk, s)
            elif is_narrow is False:
                emit("ibc_wide", first_brk, s)

        # --- 4..6  IB BREAK FAILURE (close back inside the IB) --------------
        if first_brk is not None:
            s = side[first_brk]
            for k in range(first_brk + 1, n):
                inside = (ib_lo <= cl[k] <= ib_hi)
                was_out = not (ib_lo <= cl[k - 1] <= ib_hi)
                if inside and was_out:
                    if ok[k]:
                        emit("ibf", k, -s)
                        if is_narrow is True:
                            emit("ibf_narrow", k, -s)
                        elif is_narrow is False:
                            emit("ibf_wide", k, -s)
                    break

        # --- 7..8  EXTENSION EXHAUSTION -------------------------------------
        if first_brk is not None:
            s = side[first_brk]
            for thr, name in ((0.5, "ext05"), (1.0, "ext10")):
                peak = -np.inf
                for k in range(first_brk, n):
                    if peak >= thr and ext[k] < ext[k - 1]:
                        if ok[k]:
                            emit(name, k, -s)
                        break
                    peak = max(peak, ext[k])

        # --- 9  OPEN DRIVE ---------------------------------------------------
        entry = int(np.argmax(bar == ENTRY_LO)) if (bar == ENTRY_LO).any() else None
        if entry is not None and n > 2:
            rng0 = max(hi[0] - lo[0], 1e-9)
            pos0 = (cl[0] - lo[0]) / rng0
            if pos0 >= 0.80 and cl[1] > cl[0]:
                emit("odrive", entry, +1)
            elif pos0 <= 0.20 and cl[1] < cl[0]:
                emit("odrive", entry, -1)

            # --- 10  OPEN REJECTION REVERSE ----------------------------------
            opos = (op[0] - lo[0]) / rng0
            if opos >= 2 / 3 and pos0 <= 1 / 3:
                emit("orej", entry, -1)
            elif opos <= 1 / 3 and pos0 >= 2 / 3:
                emit("orej", entry, +1)

    return pd.DataFrame(rows)


def stack(sig: pd.DataFrame) -> pd.DataFrame:
    """Sorted trade frame with a UNIQUE monotone dt (walk_forward re-sorts)."""
    t = sig.sort_values(["dt", "rule"]).reset_index(drop=True)
    base = pd.to_datetime(t["dt"])
    t["dt"] = base + pd.to_timedelta(np.arange(len(t)), unit="ns")
    for c in ("r_eod", "r_h2"):
        t[c + "_net"] = t[c] - COST
    return t


def insample_table(t: pd.DataFrame, col: str) -> None:
    print(f"   {'rule':<14}{'n':>6}{'long%':>7}{'mean %':>9}{'median':>9}"
          f"{'win':>6}{'t':>7}{'net mean':>10}{'net t':>8}")
    for r in RULES:
        d = t[t["rule"] == r]
        x = d[col].dropna()
        if len(x) < 5:
            print(f"   {r:<14}{len(x):>6}   too few")
            continue
        xn = x - COST
        print(f"   {r:<14}{len(x):>6}{(d['side'] > 0).mean() * 100:>6.0f}%"
              f"{x.mean():>+9.3f}{x.median():>+9.3f}{(x > 0).mean() * 100:>5.0f}%"
              f"{t_of(x):>+7.2f}{xn.mean():>+10.3f}{t_of(xn):>+8.2f}")


def bench(f: pd.DataFrame) -> None:
    """Unconditional benchmark: hold long from every admissible bar close."""
    d = f[(f["bar"] >= ENTRY_LO) & (f["bar"] <= ENTRY_HI)]
    for col, lab in (("r_eod", "to close"), ("r_h2", "2 bars")):
        x = d[col].dropna()
        print(f"   long every bar 2..10, {lab:<9} n={len(x):>6} mean={x.mean():>+7.4f}"
              f"%  win={(x > 0).mean() * 100:>4.1f}%  t={t_of(x):>+6.2f}")
    e = f[f["bar"] == ENTRY_LO]["r_eod"].dropna()
    print(f"   long bar 2 only, to close        n={len(e):>6} mean={e.mean():>+7.4f}"
          f"%  win={(e > 0).mean() * 100:>4.1f}%  t={t_of(e):>+6.2f}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="BANKNIFTY")
    p.add_argument("--years", type=float, default=5.4)
    p.add_argument("--dsn", default=dsn())
    p.add_argument("--refresh", action="store_true")
    args = p.parse_args()

    f = add_horizon(load_bars(args))
    print(f"\n{args.symbol}: {len(f):,} bars / {f['dt'].nunique():,} sessions "
          f"{f['dt'].min().date()} .. {f['dt'].max().date()}")
    print(f"   entry window bar {ENTRY_LO}..{ENTRY_HI}, cost {COST:.2f}% round trip, "
          f"{len(RULES)} candidate rules fixed up front\n")

    print("== BENCHMARK (no rule) ==")
    bench(f)

    t = stack(signals(f))
    print(f"\n== SIGNAL COUNTS ==  {len(t):,} rule-trades over "
          f"{t['dt'].dt.date.nunique():,} sessions")

    print("\n== IN-SAMPLE, FULL PERIOD, exit at session close (context only) ==")
    insample_table(t, "r_eod")
    print("\n== IN-SAMPLE, FULL PERIOD, exit after 2 bars (context only) ==")
    insample_table(t, "r_h2")

    cand = {r: (t["rule"] == r) for r in RULES}
    print("\n== WALK-FORWARD, OUT OF SAMPLE (18m train / 6m test, anchored) ==")
    print(HEADER)
    results = {}
    for col, lab in (("r_eod", "eod gross"), ("r_eod_net", "eod NET 4bp"),
                     ("r_h2", "2bar gross"), ("r_h2_net", "2bar NET 4bp")):
        res = walk_forward(t, cand, col, train_m=18, test_m=6, anchored=True)
        results[col] = res
        report(lab, res)

    for col in ("r_eod", "r_eod_net"):
        res = results[col]
        if "error" in res:
            continue
        print(f"\n== FOLDS: {col} ==")
        print(res["folds"].to_string(index=False))
        print(f"   picks: {res['picks']}")

    # per-year OOS of the net-selected curve, to see decay
    res = results["r_eod_net"]
    if "error" not in res:
        o = res["oos"].copy()
        o["yr"] = pd.to_datetime(o["dt"]).dt.year
        print("\n== OOS BY YEAR (net-selected, exit at close) ==")
        for yr, g in o.groupby("yr"):
            x = g["r_eod_net"]
            print(f"   {yr}  n={len(x):>4}  mean={x.mean():>+7.3f}%  "
                  f"win={(x > 0).mean() * 100:>4.0f}%  t={t_of(x):>+6.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
