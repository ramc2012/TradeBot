"""Which indicator OR CONTEXT actually predicts a 2x on the ATM proxy?

WHY THIS EXISTS. two_x_study.py found a stable, cost-surviving edge but leaned
entirely on ONE indicator (option RSI) plus days-to-expiry. That is a thin basis
for a decision, and it ignores the two things the desk actually reasons with:

  * OTHER INDICATORS -- PCR and its change, open-interest flow per side, option
    activity, underlying momentum.
  * CONTEXT -- not "is this number high" but "what kind of market is this, and
    where in its own history does this option sit". An RSI reading means
    something different when premium is historically cheap than when it is
    expensive, and different again in a trending market than a quiet one.

Everything here is computed from `option_premium_candles` and
`underlying_spot_candles` DIRECTLY rather than from the derived feature tables,
because those are sparse where it matters: results_calendar holds 3 sessions,
fo_security_ban is empty, iv_surface and oi_positioning start 2026-05-29. The
raw tables run to 2024, and a context variable measured over 65 sessions cannot
be distinguished from a regime.

THE TARGET is unchanged and deliberately so: EV of "exit at 2x, else exit at the
horizon", not P(2x). two_x_study established that those rank OPPOSITELY --
0-7 DTE has the best hit rate (34.8%) and the worst expectancy (-11.0%) -- so
hit rate is a trap and EV is the thing to rank on.

    python vanguard/research/two_x_features.py
    python vanguard/research/two_x_features.py --lookback-days 400
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
from research.two_x_study import BAR_HORIZONS, load_touch  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"

# Front-expiry CE/PE open interest and option activity per session: PCR and OI
# flow, the indicators two_x_study left out.
CHAIN_SQL = """
WITH front AS (
    SELECT underlying, date(time AT TIME ZONE 'Asia/Kolkata') AS dt, MIN(expiry) AS expiry
    FROM option_premium_candles
    WHERE interval = '30minute' AND oi IS NOT NULL AND time >= %(start)s
      AND expiry >= date(time AT TIME ZONE 'Asia/Kolkata')
    GROUP BY 1, 2
), last_print AS (
    SELECT o.underlying, date(o.time AT TIME ZONE 'Asia/Kolkata') AS dt,
           o.option_type, o.strike,
           (array_agg(o.oi ORDER BY o.time DESC))[1] AS oi,
           SUM(o.volume) AS volume
    FROM option_premium_candles o
    JOIN front f ON f.underlying = o.underlying
                AND f.dt = date(o.time AT TIME ZONE 'Asia/Kolkata')
                AND f.expiry = o.expiry
    WHERE o.interval = '30minute' AND o.oi IS NOT NULL AND o.time >= %(start)s
    GROUP BY 1, 2, 3, 4
)
SELECT underlying, dt,
       SUM(oi) FILTER (WHERE option_type = 'CE') AS ce_oi,
       SUM(oi) FILTER (WHERE option_type = 'PE') AS pe_oi,
       SUM(volume) FILTER (WHERE option_type = 'CE') AS ce_vol,
       SUM(volume) FILTER (WHERE option_type = 'PE') AS pe_vol
FROM last_print GROUP BY 1, 2
"""

# The lane's OWN decision matrix, now that the backfill gives it real history
# (features_flow 48 -> 342 sessions, timing 69 -> 346, sector_rs 51 -> 337).
# Joined per (symbol, session) so each ATM entry carries what M6 would have
# seen. Prior-session flow, matching M6's own join.
MATRIX_SQL = """
SELECT f.ts::date AS dt, f.symbol AS underlying,
       f.flow_score, f.n_ingredients, f.pcr_z, f.ivs_z,
       r.regime, r.gex_percentile,
       t.timing_state, t.timing_score, t.rvol AS timing_rvol, t.va_position,
       s.rs_z20
FROM features_flow f
LEFT JOIN LATERAL (SELECT regime, gex_percentile FROM regime r2
                   WHERE r2.symbol = f.symbol AND r2.ts::date <= f.ts::date
                   ORDER BY r2.ts DESC LIMIT 1) r ON true
LEFT JOIN LATERAL (SELECT timing_state, timing_score, rvol, va_position FROM timing t2
                   WHERE t2.symbol = f.symbol AND t2.ts::date <= f.ts::date
                   ORDER BY t2.ts DESC LIMIT 1) t ON true
LEFT JOIN sector_taxonomy tax ON tax.symbol = f.symbol
LEFT JOIN LATERAL (SELECT rs_z20 FROM sector_rs s2
                   WHERE s2.sector20 = tax.sector20 AND s2.ts::date <= f.ts::date
                   ORDER BY s2.ts DESC LIMIT 1) s ON true
"""

# MAX PAIN: the strike at which option HOLDERS lose the most at expiry, i.e.
# where the most open interest expires worthless. Classic dealer-positioning
# context and not derivable from anything already loaded, so it is computed
# here from per-strike OI rather than skipped.
#
#   pain(K) = SUM_CE oi_i * max(0, K - strike_i) + SUM_PE oi_i * max(0, strike_i - K)
#   max_pain = argmin_K pain(K)
#
# The self-join is O(strikes^2) per chain-day, which is fine at ~30 strikes and
# is why it is done in Postgres rather than looped in pandas over 68k groups.
# Front expiry only -- max pain on a back month is not what pins a spot price.
MAX_PAIN_SQL = """
WITH front AS (
    SELECT underlying, date(time AT TIME ZONE 'Asia/Kolkata') AS dt, MIN(expiry) AS expiry
    FROM option_premium_candles
    WHERE interval = '30minute' AND oi IS NOT NULL AND time >= %(start)s
      AND expiry >= date(time AT TIME ZONE 'Asia/Kolkata')
    GROUP BY 1, 2
), last_print AS (
    -- One row per contract per session: aggregates cannot be nested, so the
    -- last-print pick has to happen before any SUM over strikes.
    SELECT o.underlying, date(o.time AT TIME ZONE 'Asia/Kolkata') AS dt,
           o.strike, o.option_type,
           (array_agg(o.oi ORDER BY o.time DESC))[1] AS oi
    FROM option_premium_candles o
    JOIN front f ON f.underlying = o.underlying
                AND f.dt = date(o.time AT TIME ZONE 'Asia/Kolkata')
                AND f.expiry = o.expiry
    WHERE o.interval = '30minute' AND o.oi IS NOT NULL AND o.time >= %(start)s
    GROUP BY 1, 2, 3, 4
), agg AS (
    SELECT underlying, dt, strike,
           COALESCE(SUM(oi) FILTER (WHERE option_type = 'CE'), 0) AS ce_oi,
           COALESCE(SUM(oi) FILTER (WHERE option_type = 'PE'), 0) AS pe_oi
    FROM last_print GROUP BY 1, 2, 3
), pain AS (
    SELECT a.underlying, a.dt, a.strike AS candidate,
           SUM(b.ce_oi * GREATEST(0, a.strike - b.strike)
             + b.pe_oi * GREATEST(0, b.strike - a.strike)) AS pain
    FROM agg a JOIN agg b ON b.underlying = a.underlying AND b.dt = a.dt
    GROUP BY 1, 2, 3
)
SELECT DISTINCT ON (underlying, dt) underlying, dt, candidate AS max_pain
FROM pain ORDER BY underlying, dt, pain ASC
"""

SPOT_SQL = """
SELECT underlying, date(time AT TIME ZONE 'Asia/Kolkata') AS dt,
       (array_agg(close ORDER BY time DESC))[1] AS close,
       SUM(volume) AS volume
FROM underlying_spot_candles
WHERE interval = '30minute' AND time >= %(start)s
GROUP BY 1, 2
"""


def build(entries: pd.DataFrame, chain: pd.DataFrame, spot: pd.DataFrame) -> pd.DataFrame:
    """Attach every indicator and context to each ATM entry."""
    # ── per-underlying chain features: PCR and OI flow ─────────────────────
    chain = chain.sort_values("dt").copy()
    for col in ("ce_oi", "pe_oi", "ce_vol", "pe_vol"):
        chain[col] = chain[col].astype(float)
    g = chain.groupby("underlying")
    chain["pcr_oi"] = chain["pe_oi"] / chain["ce_oi"].replace(0, np.nan)
    chain["d_pcr"] = g["pcr_oi"].diff()
    chain["total_oi"] = chain["ce_oi"] + chain["pe_oi"]
    chain["d_oi_pct"] = g["total_oi"].pct_change()
    chain["opt_vol"] = chain["ce_vol"] + chain["pe_vol"]
    # Option activity relative to this name's own normal -- an absolute volume
    # only ranks big names above small ones.
    chain["opt_rvol"] = chain["opt_vol"] / g["opt_vol"].transform(
        lambda s: s.rolling(20, min_periods=5).median())

    # ── underlying context ─────────────────────────────────────────────────
    spot = spot.sort_values("dt").copy()
    spot["close"] = spot["close"].astype(float)
    sg = spot.groupby("underlying")["close"]
    spot["ret_5d"] = sg.pct_change(5)
    spot["ret_20d"] = sg.pct_change(20)
    spot["rv_20d"] = sg.transform(lambda s: s.pct_change().rolling(20, min_periods=10).std())
    # Where in its own recent range -- 1.0 = at the 20d high, 0.0 = at the low.
    hi = sg.transform(lambda s: s.rolling(20, min_periods=10).max())
    lo = sg.transform(lambda s: s.rolling(20, min_periods=10).min())
    spot["range_pos"] = (spot["close"] - lo) / (hi - lo).replace(0, np.nan)

    # ── market-wide context: breadth and index vol ─────────────────────────
    daily_ret = spot.assign(r=sg.pct_change())
    market = daily_ret.groupby("dt").agg(
        breadth=("r", lambda s: float((s > 0).mean())),
        mkt_ret=("r", "median"))
    market["mkt_rv_20d"] = market["mkt_ret"].rolling(20, min_periods=10).std()
    market["mkt_ret_5d"] = market["mkt_ret"].rolling(5, min_periods=3).sum()

    out = (entries
           .merge(chain[["underlying", "dt", "pcr_oi", "d_pcr", "d_oi_pct", "opt_rvol"]],
                  on=["underlying", "dt"], how="left")
           .merge(spot[["underlying", "dt", "ret_5d", "ret_20d", "rv_20d", "range_pos"]],
                  on=["underlying", "dt"], how="left")
           .merge(market[["breadth", "mkt_rv_20d", "mkt_ret_5d"]], on="dt", how="left"))

    # ── option-level context: is this premium cheap or dear for THIS name? ──
    # premium/spot is a crude implied-vol stand-in that needs no greeks; its
    # PERCENTILE against the name's own trailing year is the context that
    # matters -- the same RSI means different things on a cheap option and an
    # expensive one.
    out = out.sort_values("dt")
    out["prem_norm"] = out["premium"] / out["spot"]
    out["prem_pctile"] = out.groupby(["underlying", "side"])["prem_norm"].transform(
        lambda s: s.rolling(60, min_periods=20).rank(pct=True))
    return out


def ev_table(frame: pd.DataFrame, feature: str, horizon: str = "3d",
             buckets: int = 5) -> pd.DataFrame:
    """EV and hit rate of the 2x-target rule, by quintile of `feature`."""
    cols = [feature, f"maxfwd_{horizon}", f"endfwd_{horizon}", "premium"]
    d = frame[cols].dropna()
    if len(d) < 1000 or d[feature].nunique() < buckets:
        return pd.DataFrame()
    d = d.copy()
    d["q"] = pd.qcut(d[feature].rank(method="first"), buckets, labels=False, duplicates="drop")
    rows = []
    for q, g in d.groupby("q"):
        hit = (g[f"maxfwd_{horizon}"] / g["premium"]) >= 2.0
        ret = np.where(hit, 1.0, g[f"endfwd_{horizon}"] / g["premium"] - 1.0)
        rows.append({"q": int(q), "n": len(g),
                     "ev": round(float(ret.mean()) * 100, 2),
                     "hit": round(float(hit.mean()) * 100, 2)})
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=400)
    parser.add_argument("--horizon", default="3d", choices=list(BAR_HORIZONS))
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    start = date.today() - timedelta(days=args.lookback_days)
    connection = psycopg2.connect(args.dsn)
    try:
        daily = load(connection, start)
        entries = pick_atm(clean(daily))
        touch = load_touch(connection, start)
        chain = pd.read_sql(CHAIN_SQL, connection, params={"start": start})
        spot = pd.read_sql(SPOT_SQL, connection, params={"start": start})
        matrix = pd.read_sql(MATRIX_SQL, connection)
        maxpain = pd.read_sql(MAX_PAIN_SQL, connection, params={"start": start})
    finally:
        connection.close()

    key = ["underlying", "expiry", "strike", "side"]
    for f in (entries, touch, chain, spot):
        f["dt"] = pd.to_datetime(f["dt"]).dt.date
    carry = [f"maxfwd_{k}" for k in BAR_HORIZONS] + [f"endfwd_{k}" for k in BAR_HORIZONS]
    entries = entries.merge(touch[key + ["dt"] + carry], on=key + ["dt"], how="left")

    blocks = []
    for _, g in entries.sort_values("dt").groupby(["underlying", "side"]):
        g = g.reset_index(drop=True)
        b = pd.concat([g, macd_and_rsi(g["premium"])], axis=1)
        b.loc[: RSI_PERIOD - 1, ["rsi", "macd", "macd_hist"]] = np.nan
        blocks.append(b)
    feat = build(pd.concat(blocks, ignore_index=True), chain, spot)
    matrix["dt"] = pd.to_datetime(matrix["dt"]).dt.date
    feat = feat.merge(matrix, on=["dt", "underlying"], how="left")
    maxpain["dt"] = pd.to_datetime(maxpain["dt"]).dt.date
    maxpain["max_pain"] = maxpain["max_pain"].astype(float)
    feat = feat.merge(maxpain, on=["dt", "underlying"], how="left")
    # SIGNED distance: above max pain is a different state from below it, and
    # the absolute version cannot tell them apart.
    feat["mp_dist"] = (feat["spot"] - feat["max_pain"]) / feat["spot"]
    feat["mp_dist_abs"] = feat["mp_dist"].abs()
    have = feat["flow_score"].notna().sum()
    print(f"decision-matrix join: {have:,} of {len(feat):,} entries carry a "
          f"features_flow row ({have / len(feat) * 100:.0f}%)")

    print(f"window {feat['dt'].min()} .. {feat['dt'].max()}  entries={len(feat):,}  "
          f"horizon={args.horizon}\n")
    print("EV of 'exit at 2x else exit at horizon', by quintile of each feature.")
    print("A feature is only useful if EV is MONOTONIC across q0..q4 — a single\n"
          "good bucket is what noise looks like.\n")
    print(f"{'feature':<14}{'q0':>16}{'q1':>16}{'q2':>16}{'q3':>16}{'q4':>16}   spread")
    print(f"{'':14}" + "".join(f"{'EV%   hit%':>16}" for _ in range(5)))

    features = ["rsi", "macd", "dte", "pcr_oi", "d_pcr", "d_oi_pct", "opt_rvol",
                "prem_pctile", "ret_5d", "ret_20d", "rv_20d", "range_pos",
                "breadth", "mkt_rv_20d", "mkt_ret_5d",
                # the lane's own matrix, on equal terms with everything else
                "flow_score", "pcr_z", "ivs_z", "gex_percentile",
                "timing_score", "timing_rvol", "va_position", "rs_z20",
                "mp_dist", "mp_dist_abs"]
    scored = []
    for f in features:
        t = ev_table(feat, f, args.horizon)
        if t.empty:
            print(f"{f:<14}  (insufficient data)")
            continue
        cells = "".join(f"{r.ev:>9.1f}{r.hit:>7.1f}" for r in t.itertuples())
        spread = t["ev"].iloc[0] - t["ev"].iloc[-1]
        print(f"{f:<14}{cells}{spread:>9.1f}")
        scored.append((abs(spread), f, spread))
    # ── CONTEXT AS A CONDITION, not a competitor ──────────────────────────
    # A univariate sweep asks "does this feature beat the others". The desk's
    # actual question is different: does the SAME indicator mean different
    # things in different contexts. RSI on a historically cheap option is not
    # the same reading as RSI on an expensive one, and averaging the two is how
    # a real interaction gets reported as a flat line.
    print("\n\nINTERACTION: option RSI conditioned on premium-cheapness and DTE")
    print("EV% of the 2x rule; 'cheap' / 'dear' = bottom / top third of prem_pctile\n")
    print(f"{'DTE':>9}{'premium':>9}{'n':>7}"
          + "".join(f"{'RSI q' + str(i):>10}" for i in range(5)))
    for lo, hi in ((0, 7), (8, 20), (21, 60)):
        for plabel, pmask in (("cheap", lambda d: d["prem_pctile"] <= 0.33),
                              ("mid", lambda d: d["prem_pctile"].between(0.33, 0.67)),
                              ("dear", lambda d: d["prem_pctile"] >= 0.67)):
            sub = feat[(feat["dte"] >= lo) & (feat["dte"] <= hi)].dropna(
                subset=["rsi", "prem_pctile", f"maxfwd_{args.horizon}",
                        f"endfwd_{args.horizon}"])
            sub = sub[pmask(sub)]
            if len(sub) < 1000:
                continue
            sub = sub.copy()
            sub["q"] = pd.qcut(sub["rsi"].rank(method="first"), 5,
                               labels=False, duplicates="drop")
            cells = ""
            for q in range(5):
                g = sub[sub["q"] == q]
                if len(g) < 50:
                    cells += f"{'-':>10}"
                    continue
                hit = (g[f"maxfwd_{args.horizon}"] / g["premium"]) >= 2.0
                ret = np.where(hit, 1.0, g[f"endfwd_{args.horizon}"] / g["premium"] - 1.0)
                cells += f"{ret.mean() * 100:>10.1f}"
            print(f"{f'{lo}-{hi}d':>9}{plabel:>9}{len(sub):>7,}{cells}")

    print("\nRanked by |EV spread q0-q4| (sign shows which end is better):")
    for mag, f, spread in sorted(scored, reverse=True):
        direction = "LOW is better" if spread > 0 else "HIGH is better"
        print(f"  {f:<14}{spread:>+8.1f}   {direction}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
