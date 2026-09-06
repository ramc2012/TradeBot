"""BANKNIFTY rotation HELD THROUGH THE PERIOD — futures leg vs options leg.

WHAT banknifty_rotation.py GOT WRONG. It measured ONE OVERNIGHT return per
signal: buy at the close of the cross day, sell at the next open. A rotation
call does not resolve overnight -- the owner's own example ("buy banks and
finance in June") spans weeks. Measuring a multi-week thesis with a one-night
window answers a question nobody asked, and it also throws away the part that
decides the trade: THETA. An option held one night barely decays; the same
option held twenty sessions can lose more to time than the underlying move ever
pays.

So this holds the position and reports BOTH expressions side by side:

    FUTURES LEG   the underlying's own return over the hold. Stock futures are
                  not in this database, so spot return is the stand-in -- for a
                  multi-day directional hold the two differ only by cost of
                  carry and the basis, which are small next to the moves here.
                  Labelled "spot/futures" throughout so it is never read as a
                  precise futures P&L.
    OPTIONS LEG   the ATM CE bought at the cross and HELD, same contract
                  throughout. The contract must outlive the hold, so entries are
                  restricted to DTE >= hold + EXPIRY_BUFFER; without that the
                  long holds would silently keep only the trades whose contract
                  happened to survive.

Exits tested: fixed 5 / 10 / 20 sessions, and SIGNAL-TO-SIGNAL (hold until the
RS-RSI crosses back down), which is what the rule actually implies.

    python vanguard/research/banknifty_hold.py
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
from research.banknifty_rotation import BANKS, level1, level2  # noqa: E402
from research.overnight_intraday import SESSION_SQL, decompose  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
HOLDS = (5, 10, 20)
EXPIRY_BUFFER = 3          # sessions of slack so the contract outlives the hold
MAX_SIGNAL_HOLD = 30       # cap on the signal-to-signal hold


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=560)
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    start = date.today() - timedelta(days=args.lookback_days)
    connection = psycopg2.connect(args.dsn)
    try:
        spot_raw = pd.read_sql(SESSION_SQL, connection, params={"start": start})
        daily = load(connection, start)
    finally:
        connection.close()

    spot = decompose(spot_raw)
    l1, l2 = level1(spot), level2(spot)
    atm = pick_atm(clean(daily))
    for f in (l1, l2, atm, spot):
        f["dt"] = pd.to_datetime(f["dt"]).dt.date

    cal = {d: i for i, d in enumerate(sorted(spot["dt"].unique()))}
    inv = {i: d for d, i in cal.items()}

    # ── the futures/spot leg: forward return of the UNDERLYING ─────────────
    spot = spot.sort_values(["underlying", "dt"]).copy()
    spot["idx"] = spot["dt"].map(cal)
    for h in HOLDS:
        spot[f"spot_ret{h}"] = (spot.groupby("underlying")["close_last"].shift(-h)
                                / spot["close_last"] - 1.0)

    # ── the options leg: same ATM contract, held h sessions ────────────────
    key = ["underlying", "expiry", "strike", "side"]
    daily = daily.copy()
    daily["dt"] = pd.to_datetime(daily["dt"]).dt.date
    daily["idx"] = daily["dt"].map(cal)
    atm["idx"] = atm["dt"].map(cal)
    lookup = daily[key + ["idx", "premium"]].rename(columns={"premium": "fwd"})
    for h in HOLDS:
        sh = lookup.copy()
        sh["idx"] = sh["idx"] - h
        merged = atm.merge(sh, on=key + ["idx"], how="left")
        atm[f"opt_ret{h}"] = merged["fwd"].values / atm["premium"].values - 1.0

    ce = atm[atm["side"] == "CE"].merge(
        l1[["dt", "cross_up", "cross_dn"]], on="dt", how="left").merge(
        l2[["dt", "underlying", "rs_rank"]], on=["dt", "underlying"], how="left")
    ce = ce[ce["underlying"].isin(BANKS)]

    # Entries: the cross day itself, contract must outlive the longest hold.
    sig = ce[(ce["cross_up"] == True)].copy()                       # noqa: E712

    print(f"window {spot['dt'].min()} .. {spot['dt'].max()}   "
          f"cross-up events={int(l1['cross_up'].sum())}")
    print("\nHELD THROUGH THE PERIOD. 'spot/futures' is the underlying's own\n"
          "return; stock futures are absent from this DB, so it stands in for\n"
          "the futures leg and excludes carry/basis.\n")
    print(f"{'hold':>6}{'leg':<34}{'n':>6}{'mean %':>9}{'median %':>10}{'win %':>8}")

    spot_idx = spot.set_index(["underlying", "dt"])
    for h in HOLDS:
        pool = sig[sig["dte"] >= h + EXPIRY_BUFFER]
        strong = pool[pool["rs_rank"] >= 0.8]
        if len(strong) < 20:
            print(f"{h:>6}  (too few entries with DTE >= {h + EXPIRY_BUFFER})")
            continue
        # futures/spot leg for the same picks
        sr = spot_idx.reindex(
            pd.MultiIndex.from_arrays([strong["underlying"], strong["dt"]])
        )[f"spot_ret{h}"]
        for label, s in ((f"spot/futures, strongest bank", sr.dropna()),
                         (f"ATM CE option, strongest bank", strong[f"opt_ret{h}"].dropna()),
                         (f"ATM CE option, ALL banks",
                          pool[f"opt_ret{h}"].dropna())):
            if len(s) < 20:
                print(f"{h:>6}{label:<34}{len(s):>6}  (too few)")
                continue
            print(f"{h:>6}{label:<34}{len(s):>6}{s.mean() * 100:>9.2f}"
                  f"{s.median() * 100:>10.2f}{(s > 0).mean() * 100:>8.1f}")
        print()

    # ── signal-to-signal: hold until the RS-RSI crosses back down ──────────
    downs = sorted(l1[l1["cross_dn"] == True]["dt"])                # noqa: E712
    def exit_idx(entry_dt):
        ei = cal.get(entry_dt)
        for d in downs:
            if cal.get(d, -1) > ei:
                return min(cal[d], ei + MAX_SIGNAL_HOLD)
        return None

    rows = []
    for _, r in sig[sig["rs_rank"] >= 0.8].iterrows():
        xi = exit_idx(r["dt"])
        if xi is None:
            continue
        held = xi - cal[r["dt"]]
        xd = inv.get(xi)
        srow = spot_idx.reindex([(r["underlying"], xd)])["close_last"]
        erow = spot_idx.reindex([(r["underlying"], r["dt"])])["close_last"]
        opt = daily[(daily["underlying"] == r["underlying"]) & (daily["expiry"] == r["expiry"])
                    & (daily["strike"] == r["strike"]) & (daily["side"] == "CE")
                    & (daily["idx"] == xi)]["premium"]
        rows.append({
            "held": held,
            "spot": float(srow.iloc[0]) / float(erow.iloc[0]) - 1.0
            if srow.notna().all() and erow.notna().all() else np.nan,
            "opt": float(opt.iloc[0]) / r["premium"] - 1.0 if len(opt) else np.nan,
        })
    sig2 = pd.DataFrame(rows)
    print("SIGNAL-TO-SIGNAL (hold until RS-RSI crosses back down, "
          f"capped {MAX_SIGNAL_HOLD} sessions):")
    if not sig2.empty:
        print(f"  median hold = {sig2['held'].median():.0f} sessions   n={len(sig2)}")
        for label, col in (("spot/futures", "spot"), ("ATM CE option", "opt")):
            s = sig2[col].dropna()
            if len(s) < 20:
                print(f"  {label:<34}{len(s):>6}  (too few)")
                continue
            print(f"  {label:<34}{len(s):>6}{s.mean() * 100:>9.2f}"
                  f"{s.median() * 100:>10.2f}{(s > 0).mean() * 100:>8.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
