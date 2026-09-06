"""Money flow as a DRIVER, tested against the price features that only described.

THE CRITICISM (owner): everything studied so far -- RSI, MACD, Bollinger, ATR,
EMA, Darvas box -- is a transform of PRICE, so by construction it can only
describe a move that has already happened. The driver is volume / money flow.

That is right, and run_anatomy.py showed the cost of it: at t-1 before a 25%
run, every price feature sat within +/-0.4 control SDs and rvol at exactly
-0.00. But RAW VOLUME IS UNDIRECTED -- it counts shares, not intent. A money
flow measure has to carry a SIGN, and there are two honest ways to get one:

  FROM THE BAR      where in its range the session closed, weighted by volume.
                    Chaikin (CMF), Money Flow Index (MFI), OBV, A/D line. These
                    infer intent from close location, which is an assumption but
                    a standard and testable one.
  FROM THE CHAIN    rupees actually committed. d(OI) x premium is money that
                    changed hands to OPEN positions, per side, and its sign is
                    not inferred at all -- new call OI is money betting up. This
                    is the closest thing in this database to a true flow, and it
                    is the one measure here that is not a price transform.

Both are tested the same way the price features were, so the comparison is fair:
  1. SEPARATION at t-1 before a >=25%/20-session run, in control SDs.
  2. Forward-return rank IC at 5/10/20 sessions.

If money flow is the driver, it should clear the +/-0.4 SD band that every price
feature failed to.

    python vanguard/research/money_flow.py
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
from research.cross_section_ic import aggregate_session_ics, bar_ic  # noqa: E402
from research.monthly_pick_v2 import INDICES  # noqa: E402
from research.overnight_intraday import SESSION_SQL, decompose  # noqa: E402
from research.run_anatomy import find_runs  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
HORIZONS = (5, 10, 20)

# Rupees committed to OPENING positions, per side, per session. d(OI) is the
# contracts opened; multiplying by the premium turns it into money. Summed over
# the front expiry.
OPT_FLOW_SQL = """
WITH front AS (
    SELECT underlying, date(time AT TIME ZONE 'Asia/Kolkata') AS dt, MIN(expiry) AS expiry
    FROM option_premium_candles
    WHERE interval = '30minute' AND oi IS NOT NULL AND time >= %(start)s
      AND expiry >= date(time AT TIME ZONE 'Asia/Kolkata')
    GROUP BY 1, 2
), eod AS (
    SELECT o.underlying, date(o.time AT TIME ZONE 'Asia/Kolkata') AS dt,
           o.option_type, o.strike,
           (array_agg(o.oi    ORDER BY o.time DESC))[1] AS oi,
           (array_agg(o.close ORDER BY o.time DESC))[1] AS px
    FROM option_premium_candles o
    JOIN front f ON f.underlying = o.underlying
                AND f.dt = date(o.time AT TIME ZONE 'Asia/Kolkata')
                AND f.expiry = o.expiry
    WHERE o.interval = '30minute' AND o.oi IS NOT NULL AND o.close IS NOT NULL
      AND o.time >= %(start)s
    GROUP BY 1, 2, 3, 4
)
SELECT underlying, dt,
       SUM(oi * px) FILTER (WHERE option_type = 'CE') AS ce_value,
       SUM(oi * px) FILTER (WHERE option_type = 'PE') AS pe_value
FROM eod GROUP BY 1, 2
"""

FEATURES = ["cmf_20", "mfi_14", "obv_slope", "ad_slope", "turnover_z",
            "dollar_vol_trend", "ce_flow", "pe_flow", "net_opt_flow",
            "opt_flow_ratio"]


def bar_flows(spot: pd.DataFrame) -> pd.DataFrame:
    spot = spot.sort_values(["underlying", "dt"]).copy()
    spot["dt"] = pd.to_datetime(spot["dt"])
    out = []
    for _, g in spot.groupby("underlying", sort=False):
        g = g.reset_index(drop=True)
        c, h, l, v = g["close_last"], g["high"], g["low"], g["volume"]
        rng = (h - l).replace(0, np.nan)

        # Chaikin: close location within the range, volume weighted.
        mfm = ((c - l) - (h - c)) / rng
        mfv = mfm * v
        g["cmf_20"] = mfv.rolling(20, min_periods=15).sum() / v.rolling(
            20, min_periods=15).sum().replace(0, np.nan)

        # Money Flow Index: RSI on typical-price x volume, split up/down.
        tp = (h + l + c) / 3
        raw = tp * v
        up = raw.where(tp > tp.shift(1), 0.0).rolling(14, min_periods=10).sum()
        dn = raw.where(tp < tp.shift(1), 0.0).rolling(14, min_periods=10).sum()
        g["mfi_14"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))

        obv = (np.sign(c.diff()).fillna(0) * v).cumsum()
        g["obv_slope"] = obv.diff(20) / v.rolling(20, min_periods=10).mean().replace(0, np.nan)
        ad = mfv.cumsum()
        g["ad_slope"] = ad.diff(20) / v.rolling(20, min_periods=10).mean().replace(0, np.nan)

        dv = c * v                                    # rupee turnover
        g["turnover_z"] = ((dv - dv.rolling(60, min_periods=30).mean())
                           / dv.rolling(60, min_periods=30).std().replace(0, np.nan))
        g["dollar_vol_trend"] = (dv.rolling(10, min_periods=5).mean()
                                 / dv.rolling(60, min_periods=30).mean().replace(0, np.nan))
        out.append(g)
    return pd.concat(out, ignore_index=True)


def chain_flows(frame: pd.DataFrame, chain: pd.DataFrame) -> pd.DataFrame:
    if chain.empty:
        for c in ("ce_flow", "pe_flow", "net_opt_flow", "opt_flow_ratio"):
            frame[c] = np.nan
        return frame
    chain = chain.copy()
    chain["dt"] = pd.to_datetime(chain["dt"])
    for c in ("ce_value", "pe_value"):
        chain[c] = chain[c].astype(float)
    chain = chain.sort_values(["underlying", "dt"])
    g = chain.groupby("underlying")
    # CHANGE in open value = money committed to opening (or closing) positions.
    # Normalised by the level so a large name does not outrank a small one.
    chain["ce_flow"] = g["ce_value"].diff(5) / g["ce_value"].shift(5).replace(0, np.nan)
    chain["pe_flow"] = g["pe_value"].diff(5) / g["pe_value"].shift(5).replace(0, np.nan)
    chain["net_opt_flow"] = chain["ce_flow"] - chain["pe_flow"]
    chain["opt_flow_ratio"] = (chain["ce_value"]
                               / (chain["ce_value"] + chain["pe_value"]).replace(0, np.nan))
    keep = ["underlying", "dt", "ce_flow", "pe_flow", "net_opt_flow", "opt_flow_ratio"]
    return frame.merge(chain[keep], on=["underlying", "dt"], how="left")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=900)
    parser.add_argument("--run-window", type=int, default=20)
    parser.add_argument("--run-min", type=float, default=0.25)
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    start = date.today() - timedelta(days=args.lookback_days)
    connection = psycopg2.connect(args.dsn)
    try:
        spot_raw = pd.read_sql(SESSION_SQL, connection, params={"start": start})
        chain = pd.read_sql(OPT_FLOW_SQL, connection, params={"start": start})
    finally:
        connection.close()

    feat = chain_flows(bar_flows(decompose(spot_raw)), chain)
    feat = feat[~feat["underlying"].isin(INDICES)].sort_values(["underlying", "dt"])
    feat = feat.reset_index(drop=True)
    feat["row"] = feat.groupby("underlying").cumcount()
    for h in HORIZONS:
        feat[f"fwd{h}"] = (feat.groupby("underlying")["close_last"].shift(-h)
                           / feat["close_last"] - 1.0)

    runs = find_runs(feat, args.run_window, args.run_min)
    idx = feat.set_index(["underlying", "row"])
    near = {(r.underlying, r.i + o) for r in runs.itertuples()
            for o in range(-40, 21)}
    ctrl = feat[~pd.MultiIndex.from_arrays([feat["underlying"], feat["row"]]).isin(near)]

    print(f"window {feat['dt'].min().date()} .. {feat['dt'].max().date()}   "
          f"names={feat['underlying'].nunique()}   runs={len(runs):,}")
    print(f"option-flow coverage: {feat['net_opt_flow'].notna().sum():,} of "
          f"{len(feat):,} name-days\n")

    pre = []
    for r in runs.itertuples():
        try:
            pre.append(idx.loc[(r.underlying, r.i - 1)])
        except KeyError:
            continue
    pre = pd.DataFrame(pre)

    print("1. SEPARATION at t-1 before a run (price features all failed here:\n"
          "   best was bb_width +0.97, everything else within +/-0.4)")
    print(f"  {'feature':<18}{'runners':>11}{'control':>11}{'gap/sd':>9}")
    for f in FEATURES:
        if f not in pre or f not in ctrl:
            continue
        a, b = pre[f].dropna(), ctrl[f].dropna()
        if len(a) < 50 or len(b) < 500 or b.std() == 0:
            continue
        gap = (a.mean() - b.mean()) / b.std()
        star = " *" if abs(gap) >= 0.4 else ""
        print(f"  {f:<18}{a.mean():>11.3f}{b.mean():>11.3f}{gap:>+9.2f}{star}")

    print("\n2. FORWARD-RETURN RANK IC (SE clustered by session)")
    print(f"  {'feature':<18}" + "".join(f"{('IC' + str(h)):>10}{('t' + str(h)):>7}"
                                          for h in HORIZONS))
    for f in FEATURES:
        if f not in feat or feat[f].notna().sum() < 2000:
            continue
        cells = ""
        for h in HORIZONS:
            per = [ic for _, dg in feat.groupby("dt")
                   if (ic := bar_ic(dg[f], dg[f"fwd{h}"])) is not None]
            agg = aggregate_session_ics(per)
            if agg["mean_ic"] is None:
                cells += f"{'-':>10}{'-':>7}"
                continue
            t = agg["t_stat"]
            cells += f"{agg['mean_ic']:>+10.4f}" + (f"{t:>+7.1f}" if t is not None
                                                    else f"{'-':>7}")
        print(f"  {f:<18}{cells}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
