"""What did the BEST next-open candidates actually look like?

THE OBJECTIVE, stated the way the desk states it: each evening, choose the
candidate most likely to pay on tomorrow's OPEN. That is a RANKING problem, and
it is answered better by looking at the winners than by testing one hypothesis
at a time.

So this works backwards. Every session, every ATM contract (CE and PE) is scored
on its ACTUAL overnight option return -- entry at the session's last print, exit
at the next session's first print. The daily top performers are then
characterised: what did the best opens have in common, and how did they differ
from the field they were drawn from?

EVERYTHING HERE IS AN OPTION RETURN, in percent of premium paid. Spot moves are
reported only where labelled, because mixing the two is how a 16 bps spot gap
and a 2% option move end up in the same sentence.

THREE THINGS IT COMPILES
  1. THE OPPORTUNITY SET -- what the best 1/3/10 candidates per session actually
     returned. This is the ceiling any ranking is competing for; without it a
     "+1.2% edge" has no scale to be judged against.
  2. THE WINNER PROFILE -- mean characteristics of the daily top decile against
     the field, per side. This is the reference table.
  3. CAPTURE -- what a simple ex-ante rule would have picked up out of that
     ceiling, which is the only number that says whether selection is working.

    python vanguard/research/best_opens.py
    python vanguard/research/best_opens.py --lookback-days 400 --top 3
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
from research.btst_option_leg import OPT_OVERNIGHT_SQL  # noqa: E402
from research.option_momentum_ic import RSI_PERIOD, macd_and_rsi  # noqa: E402
from research.overnight_intraday import SESSION_SQL, decompose  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"

# Characteristics compiled for the winner profile. Each is knowable at the
# session close, i.e. before the trade -- a profile built on anything else
# describes the outcome, not the setup.
PROFILE = ["close_loc", "rvol", "range_exp", "drift", "dte", "prem_pctile",
           "rsi", "moneyness", "spot_ret", "opt_ret_today"]


def build(connection, start: date) -> pd.DataFrame:
    daily = load(connection, start)
    entries = pick_atm(clean(daily))
    opt = pd.read_sql(OPT_OVERNIGHT_SQL, connection, params={"start": start})
    spot_raw = pd.read_sql(SESSION_SQL, connection, params={"start": start})

    for col in ("close_last", "open_first"):
        opt[col] = opt[col].astype(float)
    key = ["underlying", "expiry", "strike", "side"]
    opt = opt.sort_values("dt")
    opt["next_open"] = opt.groupby(key)["open_first"].shift(-1)
    opt["next_dt"] = opt.groupby(key)["dt"].shift(-1)
    # THE TARGET: option return over the open, in percent of premium.
    opt["ret"] = opt["next_open"] / opt["close_last"] - 1.0
    # Today's own option move, as a setup characteristic.
    opt["opt_ret_today"] = opt["close_last"] / opt["open_first"] - 1.0

    spot = decompose(spot_raw)
    spot["spot_ret"] = spot["total"]
    for f in (entries, opt, spot):
        f["dt"] = pd.to_datetime(f["dt"]).dt.date
    opt["next_dt"] = pd.to_datetime(opt["next_dt"]).dt.date

    m = (entries.merge(opt[key + ["dt", "ret", "opt_ret_today", "next_dt"]],
                       on=key + ["dt"], how="inner")
         .merge(spot[["underlying", "dt", "close_loc", "rvol", "range_exp",
                      "drift", "spot_ret"]], on=["underlying", "dt"], how="left"))
    m = m.dropna(subset=["ret"])
    cal = {d: i for i, d in enumerate(sorted(spot["dt"].unique()))}
    m = m[m.apply(lambda r: cal.get(r["next_dt"], -99) - cal.get(r["dt"], 0) == 1, axis=1)]

    blocks = []
    for _, g in m.sort_values("dt").groupby(["underlying", "side"]):
        g = g.reset_index(drop=True)
        b = pd.concat([g, macd_and_rsi(g["premium"])], axis=1)
        b.loc[: RSI_PERIOD - 1, ["rsi", "macd", "macd_hist"]] = np.nan
        blocks.append(b)
    m = pd.concat(blocks, ignore_index=True)
    m["prem_norm"] = m["premium"] / m["spot"]
    m["prem_pctile"] = m.groupby(["underlying", "side"])["prem_norm"].transform(
        lambda s: s.rolling(60, min_periods=20).rank(pct=True))
    return m


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=400)
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    connection = psycopg2.connect(args.dsn)
    try:
        m = build(connection, date.today() - timedelta(days=args.lookback_days))
    finally:
        connection.close()

    print(f"window {m['dt'].min()} .. {m['dt'].max()}   sessions={m['dt'].nunique()}   "
          f"names={m['underlying'].nunique()}   candidate-nights={len(m):,}")
    print("ALL RETURNS BELOW ARE OPTION RETURNS, % of premium paid.\n")

    # ── 1. the opportunity set ─────────────────────────────────────────────
    print("1. OPPORTUNITY SET — what the best candidates actually returned")
    print(f"{'':>26}{'mean %':>10}{'median %':>11}{'win %':>8}")
    for label, n in (("best 1 per session", 1), (f"best {args.top} per session", args.top),
                     ("best 10 per session", 10)):
        top = m.sort_values("ret", ascending=False).groupby("dt").head(n)
        print(f"  {label:<24}{top['ret'].mean() * 100:>10.2f}"
              f"{top['ret'].median() * 100:>11.2f}{(top['ret'] > 0).mean() * 100:>8.1f}")
    for label, side in (("field average (all candidates)", None),
                        ("  field, CE only", "CE"), ("  field, PE only", "PE")):
        d = m if side is None else m[m["side"] == side]
        print(f"  {label:<24}{d['ret'].mean() * 100:>10.2f}"
              f"{d['ret'].median() * 100:>11.2f}{(d['ret'] > 0).mean() * 100:>8.1f}")
    worst = m.sort_values("ret").groupby("dt").head(args.top)
    print(f"  {'worst ' + str(args.top) + ' per session':<24}{worst['ret'].mean() * 100:>10.2f}"
          f"{worst['ret'].median() * 100:>11.2f}{(worst['ret'] > 0).mean() * 100:>8.1f}")

    # ── 2. the winner profile ──────────────────────────────────────────────
    print("\n2. WINNER PROFILE — daily TOP DECILE vs the field it came from")
    m = m.copy()
    m["rank_pct"] = m.groupby(["dt", "side"])["ret"].rank(pct=True)
    for side in ("CE", "PE"):
        d = m[m["side"] == side]
        win = d[d["rank_pct"] >= 0.9]
        lose = d[d["rank_pct"] <= 0.1]
        print(f"\n  --- {side} ---  (winners n={len(win):,}, field n={len(d):,})")
        print(f"  {'characteristic':<16}{'winners':>10}{'field':>10}{'losers':>10}{'w-l gap':>10}")
        for col in PROFILE:
            if col not in d or d[col].notna().sum() < 500:
                continue
            w, f, l = win[col].mean(), d[col].mean(), lose[col].mean()
            print(f"  {col:<16}{w:>10.3f}{f:>10.3f}{l:>10.3f}{w - l:>+10.3f}")

    # ── 3. capture ─────────────────────────────────────────────────────────
    print("\n3. CAPTURE — what an ex-ante rule picks out of that ceiling")
    print(f"{'rule (top {n} per session by score)':<44}{'mean %':>9}{'median %':>10}{'win %':>8}"
          .replace("{n}", str(args.top)))
    hot = (m["rvol"] >= 2.0) & (m["range_exp"] >= 1.2)
    rules = {
        "random / field average": m.assign(score=0.0),
        "close_loc (CE) — BTST trigger": m[m["side"] == "CE"].assign(
            score=lambda d: d["close_loc"]),
        "close_loc DESC (PE) — STBT trigger": m[m["side"] == "PE"].assign(
            score=lambda d: -d["close_loc"]),
        "BTST cell then drift (CE)": m[hot & (m["side"] == "CE")].assign(
            score=lambda d: d["close_loc"] + d["drift"].fillna(0) * 100),
        "STBT cell then -drift (PE)": m[hot & (m["side"] == "PE")].assign(
            score=lambda d: -d["close_loc"] - d["drift"].fillna(0) * 100),
        "cheap premium + low rsi (CE)": m[m["side"] == "CE"].assign(
            score=lambda d: -d["prem_pctile"].fillna(0.5) - d["rsi"].fillna(50) / 100),
    }
    for label, d in rules.items():
        if d.empty:
            continue
        picks = d.sort_values("score", ascending=False).groupby("dt").head(args.top)
        if len(picks) < 100:
            print(f"  {label:<42}{len(picks):>9}  (too few)")
            continue
        print(f"  {label:<42}{picks['ret'].mean() * 100:>9.2f}"
              f"{picks['ret'].median() * 100:>10.2f}{(picks['ret'] > 0).mean() * 100:>8.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
