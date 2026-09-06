"""How often does the ATM proxy TOUCH 2x, within an hour to three days?

TOUCH, NOT ENDPOINT. A desk targeting 2x exits at 2x -- so the question is
whether the premium ever REACHES twice the entry inside the window, not where it
happens to sit when the window closes. Endpoint return understates a target
strategy badly: a contract that doubles on day one and gives it all back scores
0% on an endpoint measure and +100% on a touch measure, and the second is what
the trade would have banked.

Touch uses the bar HIGH, so it is the honest best case for a resting limit at
2x. It is also an OPTIMISTIC bound -- a high is one print, and it assumes the
exit filled there. Read it as a ceiling on what a 2x target can capture.

HORIZONS are in 30-MINUTE BARS, sized to the owner's framing that a 2x happens
in an hour to two or three days: 2 bars (1h), 13 (one session), 26 (two), 39
(three).

ENTRY is the ATM contract at a session close, chosen by the same cleaned,
distance-capped rule as atm_tail_study (see the DATA QUALITY FLOORS there --
without them "ATM" includes 12%-OTM strikes and Rs 0.10 prints, both of which
manufacture fake multiples).

    python vanguard/research/two_x_study.py
    python vanguard/research/two_x_study.py --lookback-days 200
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
from research.atm_tail_study import clean, load, pick_atm  # noqa: E402
from research.option_momentum_ic import RSI_PERIOD, macd_and_rsi  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
BAR_HORIZONS = {"1h": 2, "1d": 13, "2d": 26, "3d": 39}
TOUCH_MULTIPLES = (1.5, 2.0, 3.0)

# Forward max per contract, computed IN POSTGRES. Doing it in pandas meant
# materialising every 30-minute bar for every ATM contract and then building a
# reversed rolling max per group -- which OOM-killed the process. A window
# function over `ROWS BETWEEN 1 FOLLOWING AND n FOLLOWING` is one pass, and only
# the entry rows come back over the wire.
#
# `1 FOLLOWING` matters: the entry bar's own high must not count as the exit.
TOUCH_SQL = """
WITH bars AS (
    SELECT underlying, expiry, strike, option_type AS side, time AS ts,
           date(time AT TIME ZONE 'Asia/Kolkata') AS dt, high, close
    FROM option_premium_candles
    WHERE interval = '30minute' AND high IS NOT NULL AND close IS NOT NULL
      AND time >= %(start)s
), fwd AS (
    SELECT b.*,
           {maxcols}
           ROW_NUMBER() OVER (PARTITION BY underlying, expiry, strike, side, dt
                              ORDER BY ts DESC) AS rn
    FROM bars b
)
SELECT underlying, expiry, strike, side, dt, close AS entry_close, {outcols}
FROM fwd WHERE rn = 1
"""


def load_touch(connection, start: date) -> pd.DataFrame:
    maxcols = "".join(
        f"MAX(high) OVER (PARTITION BY underlying, expiry, strike, side "
        f"ORDER BY ts ROWS BETWEEN 1 FOLLOWING AND {n} FOLLOWING) AS maxfwd_{k},\n           "
        # The ENDPOINT too: P(touch) says how often the target is reachable,
        # never what the position earns when it is not. Both are needed before
        # calling anything tradeable.
        f"LEAD(close, {n}) OVER (PARTITION BY underlying, expiry, strike, side "
        f"ORDER BY ts) AS endfwd_{k},\n           "
        for k, n in BAR_HORIZONS.items())
    outcols = ", ".join(f"maxfwd_{k}, endfwd_{k}" for k in BAR_HORIZONS)
    frame = pd.read_sql(TOUCH_SQL.format(maxcols=maxcols, outcols=outcols),
                        connection, params={"start": start})
    for col in (["strike", "entry_close"]
                + [f"maxfwd_{k}" for k in BAR_HORIZONS]
                + [f"endfwd_{k}" for k in BAR_HORIZONS]):
        frame[col] = frame[col].astype(float)
    return frame


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=200)
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    start = date.today() - timedelta(days=args.lookback_days)
    connection = psycopg2.connect(args.dsn)
    try:
        daily = load(connection, start)
        entries = pick_atm(clean(daily))
        touch = load_touch(connection, start)
    finally:
        connection.close()

    key = ["underlying", "expiry", "strike", "side"]
    entries["dt"] = pd.to_datetime(entries["dt"]).dt.date
    touch["dt"] = pd.to_datetime(touch["dt"]).dt.date
    carry = ([f"maxfwd_{k}" for k in BAR_HORIZONS]
             + [f"endfwd_{k}" for k in BAR_HORIZONS])
    entries = entries.merge(touch[key + ["dt"] + carry], on=key + ["dt"], how="left")

    # RSI on the rolling-ATM series, same construction as the other studies.
    out = []
    for _, g in entries.sort_values("dt").groupby(["underlying", "side"]):
        g = g.reset_index(drop=True)
        b = pd.concat([g, macd_and_rsi(g["premium"])], axis=1)
        b.loc[: RSI_PERIOD - 1, ["rsi", "macd", "macd_hist"]] = np.nan
        out.append(b)
    feat = pd.concat(out, ignore_index=True)

    print(f"window {entries['dt'].min()} .. {entries['dt'].max()}  "
          f"entries={len(entries):,}  underlyings={entries['underlying'].nunique()}")
    print("\nP(premium TOUCHES the multiple) — base rate, all entries:")
    print(f"{'horizon':>9}{'n':>9}" + "".join(f"{f'P>={m:g}x':>10}" for m in TOUCH_MULTIPLES))
    for label in BAR_HORIZONS:
        col = f"maxfwd_{label}"
        d = feat[[col, "premium"]].dropna()
        if d.empty:
            continue
        ratio = d[col] / d["premium"]
        print(f"{label:>9}{len(d):>9,}"
              + "".join(f"{(ratio >= m).mean() * 100:>10.3f}" for m in TOUCH_MULTIPLES))

    # ── EXPECTANCY of an actual 2x-target rule ────────────────────────────
    # Buy at the close, exit at 2x if it is ever touched, otherwise exit at the
    # horizon. P(touch) is only half the story: a 33% hit rate paired with a
    # -70% average on the misses is still a losing strategy, and near-expiry
    # ATM decays hard. The touch leg is credited at exactly +100%, which
    # assumes the limit filled -- optimistic, and stated as such.
    print("\nEXPECTANCY of 'exit at 2x, else exit at horizon' (per trade, %):")
    print(f"{'dte':>8}{'q':>3}{'n':>7}" + "".join(f"{h + ' EV':>10}{h + ' hit':>9}"
                                                  for h in ("1d", "3d")))
    for lo, hi in ((0, 7), (8, 20), (21, 60)):
        sub = feat[(feat["dte"] >= lo) & (feat["dte"] <= hi)].dropna(subset=["rsi"])
        if len(sub) < 500:
            continue
        sub = sub.copy()
        sub["q"] = pd.qcut(sub["rsi"].rank(method="first"), 5, labels=False, duplicates="drop")
        for q, g in sub.groupby("q"):
            cells = ""
            for label in ("1d", "3d"):
                d = g[[f"maxfwd_{label}", f"endfwd_{label}", "premium"]].dropna()
                if d.empty:
                    cells += f"{'-':>10}{'-':>9}"
                    continue
                hit = (d[f"maxfwd_{label}"] / d["premium"]) >= 2.0
                ret = np.where(hit, 1.0, d[f"endfwd_{label}"] / d["premium"] - 1.0)
                cells += f"{ret.mean() * 100:>10.1f}{hit.mean() * 100:>9.1f}"
            print(f"{f'{lo}-{hi}d':>8}{int(q):>3}{len(g):>7,}" + cells)

    # ── STABILITY of the one cell with positive EV ────────────────────────
    # A pooled EV is not a finding until it survives a subperiod split -- an
    # earlier study in this series reported an h=1 result that turned out to
    # live entirely in one half of its window.
    best = feat[(feat["dte"] >= 21) & (feat["dte"] <= 60)].dropna(subset=["rsi"]).copy()
    best["q"] = pd.qcut(best["rsi"].rank(method="first"), 5, labels=False, duplicates="drop")
    best = best[best["q"] == 0]
    best["m"] = pd.to_datetime(best["dt"]).dt.to_period("M")
    print("\nSTABILITY of 21-60 DTE + lowest-RSI (the only positive-EV cell), 3d:")
    print(f"{'month':>9}{'n':>7}{'EV %':>8}{'hit %':>8}")
    for m, g in best.groupby("m"):
        d = g[["maxfwd_3d", "endfwd_3d", "premium"]].dropna()
        if len(d) < 50:
            print(f"{str(m):>9}{len(d):>7}   (too few)")
            continue
        hit = (d["maxfwd_3d"] / d["premium"]) >= 2.0
        ret = np.where(hit, 1.0, d["endfwd_3d"] / d["premium"] - 1.0)
        print(f"{str(m):>9}{len(d):>7}{ret.mean() * 100:>8.1f}{hit.mean() * 100:>8.1f}")

    # ── COST SENSITIVITY: the number that decides everything ──────────────
    # A +5% EV against Indian single-stock option spreads of 1-3% EACH WAY is
    # not obviously a strategy. Costs are applied as a round-trip haircut on
    # the premium: you buy above mid and sell below it, on both the winners and
    # the losers.
    print("\nCOST SENSITIVITY of 21-60 DTE + lowest-RSI, 3d hold:")
    print(f"{'round-trip cost':>17}{'EV %':>9}")
    d = best[["maxfwd_3d", "endfwd_3d", "premium"]].dropna()
    hit = (d["maxfwd_3d"] / d["premium"]) >= 2.0
    for cost in (0.0, 0.01, 0.02, 0.03, 0.05, 0.08):
        # entry paid up by cost/2, exit received down by cost/2
        entry = d["premium"] * (1 + cost / 2)
        exit_px = np.where(hit, d["premium"] * 2.0, d["endfwd_3d"]) * (1 - cost / 2)
        print(f"{cost * 100:>15.0f}%{(exit_px / entry - 1).mean() * 100:>9.1f}")

    print("\nP(TOUCH 2x) by RSI quintile x DTE — does selection lift it?")
    print(f"{'dte':>8}{'q':>3}{'n':>8}" + "".join(f"{h:>9}" for h in BAR_HORIZONS))
    for lo, hi in ((0, 7), (8, 20), (21, 60)):
        sub = feat[(feat["dte"] >= lo) & (feat["dte"] <= hi)].dropna(subset=["rsi"])
        if len(sub) < 500:
            continue
        sub = sub.copy()
        sub["q"] = pd.qcut(sub["rsi"].rank(method="first"), 5, labels=False, duplicates="drop")
        for q, g in sub.groupby("q"):
            cells = []
            for label in BAR_HORIZONS:
                d = g[[f"maxfwd_{label}", "premium"]].dropna()
                cells.append(f"{((d[f'maxfwd_{label}'] / d['premium']) >= 2.0).mean() * 100:>9.2f}"
                             if len(d) else f"{'-':>9}")
            print(f"{f'{lo}-{hi}d':>8}{int(q):>3}{len(g):>8,}" + "".join(cells))
    return 0


if __name__ == "__main__":
    sys.exit(main())
