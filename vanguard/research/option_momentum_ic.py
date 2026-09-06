"""Do MACD/RSI computed ON THE OPTION PREMIUM predict the premium's next move?

WHY THE TARGET IS THE PREMIUM, NOT THE UNDERLYING
--------------------------------------------------------------------------
A long option pays when ITS OWN premium rises -- a bought CE and a bought PE
both profit on the way up in their own price. So the tradeable question is not
"which way does the stock go", it is "which contract's premium rises next",
asked once per side. That collapses the direction problem the lane could not
solve: research/cross_section_ic.py measured signed_flow at ZERO (t -0.18)
against the underlying's signed return, and sector RS and timing significantly
NEGATIVE, so nothing available predicted the stock's direction. This asks a
different question of the same tape.

It also means CE and PE are not opposites here. Both series are scored on
"does this go up", and on a volatility expansion BOTH can rise at once -- which
is exactly the state a mirrored CE/PE model cannot represent, and which the
per-side OI states already showed happening (14 names with both sides
short_covering, 11 with both long_unwind, measured 2026-08-28).

THE SERIES
--------------------------------------------------------------------------
Per (underlying, side, 30-minute bar): the OI-WEIGHTED mean premium of the
FRONT expiry. Weighted, not a flat mean, because a chain carries many far-OTM
strikes trading at a few paise and a flat average lets that tail dominate a
number meant to say what the money in this side is worth.

ROLL GUARD: front-expiry premium is discontinuous at each expiry -- a new
contract starts at a different level entirely. Indicator values spanning a roll
are dropped rather than smoothed across it, the same guard build_oi_series
applies to delta_oi.

BID-ASK BOUNCE, AND WHY BOTH TARGETS ARE REPORTED
--------------------------------------------------------------------------
A close-to-close forward return shares close(t) with the bar the indicator was
computed on. If that close printed at the BID, the next return is mechanically
positive -- and "the premium just fell" is exactly the state that makes a bid
print likely, so a contrarian indicator earns a spurious edge out of nothing
but the spread. Option spreads are wide, so this is not a rounding concern.

Measured over 90 days, RSI decile spread (D0 minus D9) of forward premium
return:

    horizon   close-to-close   NEXT-OPEN to close   share that was bounce
    h=1           +3.34%             +1.28%                 62%
    h=4           +7.81%             +5.14%                 34%

So the effect is REAL but roughly a third smaller than the naive number, and
most of the one-bar version is an artifact. Both targets are reported on every
run so the honest figure is never the one that has to be remembered separately.
The falling bounce share with horizon is itself the tell: a spread artifact is
a fixed one-time amount, so it dilutes as real drift accumulates.

WHAT IS MEASURED
--------------------------------------------------------------------------
Spearman rank IC within each bar's cross-section (so a component is judged on
whether it ORDERS contracts correctly, which is what selection uses it for),
averaged per session, with the SE clustered by session because same-session
names share a market-wide shock. Same statistics as cross_section_ic.py, reused
rather than re-derived.

ROBUSTNESS (--robustness), and what it changed
--------------------------------------------------------------------------
A pooled IC with a good t-statistic is not yet a finding. Three checks:

  * SUBPERIOD -- an effect present in only half the window is a regime
    artifact. Split at the median session.
  * PER SIDE -- is it CE, PE, or both? The pooled number hides that.
  * INDEPENDENCE -- CE and PE of the SAME underlying sit in the same
    cross-section, so they are not independent draws and the clustered SE does
    not account for the pairing. Running one side at a time removes it.

Measured 2026-08-28 on rsi, bounce-free target:

    h=1   full -0.0351 (t -3.99) | 1st half -0.0184 (t -1.73, NOT sig)
                                 | 2nd half -0.0513 (t -3.80)
    h=4   full -0.0709 (t -4.44) | 1st half -0.0611 (t -2.82)
                                 | 2nd half -0.0810 (t -3.42)
    CE only  h=1 -0.0323 (-3.93)   h=4 -0.0572 (-3.68)
    PE only  h=1 -0.0339 (-3.66)   h=4 -0.0708 (-4.93)

CONCLUSION THIS CHANGED: the h=1 effect is NOT stable -- it lives in the
second half and is insignificant in the first, on top of being 62% bid-ask
bounce. Only the h=4 (two-hour) effect survives both halves. The per-side runs
hold up, so the CE/PE pairing is not what is driving it.

    python vanguard/research/option_momentum_ic.py
    python vanguard/research/option_momentum_ic.py --lookback-days 60
    python vanguard/research/option_momentum_ic.py --robustness
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
from research.cross_section_ic import (  # noqa: E402
    MIN_NAMES_PER_BAR, aggregate_session_ics, bar_ic,
)

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
HORIZONS = (1, 2, 4)          # 30-minute bars ahead
# "cc" shares close(t) with the indicator window; "oc" starts at the NEXT bar's
# open and so shares no price with it. See the bounce section above.
TARGETS = ("cc", "oc")
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
RSI_PERIOD = 14


def load_side_premium(connection, start: date, end: date) -> pd.DataFrame:
    """Front-expiry, OI-weighted premium per (underlying, side, 30m bar)."""
    query = """
        WITH front AS (
            SELECT underlying, date(time AT TIME ZONE 'Asia/Kolkata') AS dt,
                   MIN(expiry) AS expiry
            FROM option_premium_candles
            WHERE interval = '30minute' AND oi IS NOT NULL AND close IS NOT NULL
              AND time >= %(start)s AND time < %(end)s
              AND expiry >= date(time AT TIME ZONE 'Asia/Kolkata')
            GROUP BY 1, 2
        )
        SELECT o.underlying, o.option_type AS side, o.time AS ts,
               date(o.time AT TIME ZONE 'Asia/Kolkata') AS dt,
               f.expiry,
               SUM(o.close * o.oi) / NULLIF(SUM(o.oi), 0) AS premium,
               SUM(o.open  * o.oi) / NULLIF(SUM(o.oi), 0) AS open_px,
               SUM(o.oi) AS oi
        FROM option_premium_candles o
        JOIN front f
          ON f.underlying = o.underlying
         AND f.dt = date(o.time AT TIME ZONE 'Asia/Kolkata')
         AND f.expiry = o.expiry
        WHERE o.interval = '30minute' AND o.oi IS NOT NULL
          AND o.close IS NOT NULL AND o.open IS NOT NULL
          AND o.time >= %(start)s AND o.time < %(end)s
        GROUP BY 1, 2, 3, 4, 5
    """
    frame = pd.read_sql(query, connection, params={"start": start, "end": end})
    for col in ("premium", "open_px", "oi"):
        frame[col] = frame[col].astype(float)
    return frame


def macd_and_rsi(premium: pd.Series) -> pd.DataFrame:
    """Standard MACD(12,26,9) and Wilder RSI(14) on one premium series.

    RSI uses Wilder's smoothing (alpha = 1/period), not a simple mean -- the
    simple-mean variant is a different indicator that happens to share the name
    and would not be comparable with any published level.
    """
    fast = premium.ewm(span=MACD_FAST, adjust=False).mean()
    slow = premium.ewm(span=MACD_SLOW, adjust=False).mean()
    macd = fast - slow
    signal = macd.ewm(span=MACD_SIGNAL, adjust=False).mean()

    delta = premium.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / RSI_PERIOD, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / RSI_PERIOD, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    # All-gain windows give avg_loss == 0, which is RSI 100, not "unknown".
    rsi = rsi.where(avg_loss > 0, other=100.0).where(avg_gain > 0, other=rsi)

    return pd.DataFrame({
        # NORMALISED by the premium level. Raw MACD is in rupees, so a Rs 900
        # contract would outrank a Rs 9 one on nothing but its price -- and a
        # cross-sectional rank is exactly where that bites.
        "macd": macd / premium.replace(0.0, np.nan),
        "macd_hist": (macd - signal) / premium.replace(0.0, np.nan),
        "rsi": rsi,
    }, index=premium.index)


def build_features(frame: pd.DataFrame, horizons=HORIZONS) -> pd.DataFrame:
    """Indicators + forward PREMIUM returns per (underlying, side) series."""
    out = []
    for (underlying, side), group in frame.sort_values("ts").groupby(["underlying", "side"]):
        group = group.reset_index(drop=True)
        ind = macd_and_rsi(group["premium"])
        block = pd.concat([group, ind], axis=1)

        # Drop any row whose indicator window spans an expiry roll: the level
        # jumps, so the indicator describes the roll rather than the market.
        same = group["expiry"] == group["expiry"].shift(1)
        run = same.groupby((~same).cumsum()).cumcount() + 1
        block.loc[run < MACD_SLOW, ["macd", "macd_hist"]] = np.nan
        block.loc[run < RSI_PERIOD, "rsi"] = np.nan

        for h in horizons:
            # A forward return that crosses a roll is a contract change, not a
            # return, so it is not a target either.
            ok = group["expiry"].shift(-h) == group["expiry"]
            block[f"cc_{h}"] = (group["premium"].shift(-h)
                                / group["premium"] - 1.0).where(ok)
            block[f"oc_{h}"] = (group["premium"].shift(-h)
                                / group["open_px"].shift(-1) - 1.0).where(ok)
        out.append(block)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def run_study(features: pd.DataFrame, horizons=HORIZONS) -> list[dict]:
    results = []
    for component in ("macd", "macd_hist", "rsi"):
      for kind in TARGETS:
        for h in horizons:
            target = f"{kind}_{h}"
            agg = ic_for(features, component, target)
            usable = features[[component, target]].dropna()
            results.append({"component": component, "horizon": h, "target": kind,
                            "n_obs": len(usable), **agg})
    return results


def ic_for(features: pd.DataFrame, component: str, target: str) -> dict:
    """One (component, target) IC over whatever slice of features is passed."""
    per_session = []
    for _, session in features.groupby("dt"):
        bar_ics = [ic for _, bar in session.groupby("ts")
                   if (ic := bar_ic(bar[component], bar[target])) is not None]
        if bar_ics:
            per_session.append(float(np.mean(bar_ics)))
    return aggregate_session_ics(per_session)


def robustness(features: pd.DataFrame, component: str = "rsi") -> None:
    """Subperiod / per-side / independence checks -- see the module docstring."""
    def show(label, agg):
        if agg["mean_ic"] is None:
            print(f"  {label:<34} (no usable cross-sections)")
            return
        t = f"{agg['t_stat']:+.2f}" if agg["t_stat"] is not None else " n/a"
        print(f"  {label:<34} IC={agg['mean_ic']:+.4f}  t={t:>6}  "
              f"sessions={agg['n_sessions']}")

    sessions = sorted(features["dt"].unique())
    mid = sessions[len(sessions) // 2]
    print(f"\nrobustness: {len(sessions)} sessions, split at {mid}")
    for target in ("oc_1", "oc_4"):
        print(f"\n=== {component} vs {target} (bounce-free) ===")
        show("FULL window", ic_for(features, component, target))
        show("first half", ic_for(features[features["dt"] < mid], component, target))
        show("second half", ic_for(features[features["dt"] >= mid], component, target))
        for side in ("CE", "PE"):
            show(f"{side} only (removes CE/PE pairing)",
                 ic_for(features[features["side"] == side], component, target))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--robustness", action="store_true",
                        help="subperiod / per-side / independence checks")
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=args.lookback_days)
    connection = psycopg2.connect(args.dsn)
    try:
        raw = load_side_premium(connection, start, end)
    finally:
        connection.close()
    if raw.empty:
        print("no option premium rows in window")
        return 1

    features = build_features(raw)
    print(f"window {raw['dt'].min()} .. {raw['dt'].max()}   "
          f"series={raw.groupby(['underlying', 'side']).ngroups}  rows={len(raw):,}")
    print(f"\nTARGET = forward return of the OPTION'S OWN PREMIUM "
          f"(a long option pays when its premium rises, CE or PE alike)\n")
    print(f"{'component':<12}{'tgt':>4}{'h':>3}{'n_obs':>10}{'sess':>6}{'mean IC':>10}"
          f"{'SE(clust)':>11}{'t':>7}   95% CI")
    for row in run_study(features):
        if row["mean_ic"] is None:
            print(f"{row['component']:<12}{row['target']:>4}{row['horizon']:>3}"
                  f"{row['n_obs']:>10,}{row['n_sessions']:>6}      (no usable cross-sections)")
            continue
        ci = (f"[{row['ci_low']:+.4f}, {row['ci_high']:+.4f}]"
              if row["ci_low"] is not None else "")
        t = f"{row['t_stat']:+.2f}" if row["t_stat"] is not None else "  n/a"
        se = f"{row['se']:.4f}" if row["se"] is not None else "   n/a"
        print(f"{row['component']:<12}{row['target']:>4}{row['horizon']:>3}"
              f"{row['n_obs']:>10,}{row['n_sessions']:>6}{row['mean_ic']:>+10.4f}"
              f"{se:>11}{t:>7}   {ci}")
    print("\nn in every t-statistic is the number of SESSIONS, not observations —\n"
          "same-session contracts share a market-wide shock and are not independent.")
    if args.robustness:
        robustness(features)
    return 0


if __name__ == "__main__":
    sys.exit(main())
