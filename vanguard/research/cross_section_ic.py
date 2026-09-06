"""Cross-sectional information coefficients over the FULL universe.

WHY THIS EXISTS, AND WHY M10's PER-COMPONENT IC DOES NOT REPLACE IT
--------------------------------------------------------------------------
journal/attribution.py correlates each component score against the eventual
r_multiple of CLOSED TICKETS. Every one of those rows has already passed
M6's filter: |flow| >= 60, timing_score >= 70, timing_state = IGNITION,
regime in one of three buckets. Correlating a predictor inside its own
acceptance region truncates its range to almost nothing, and a truncated
predictor cannot show a correlation whether or not it has one. Add a sample
of a few dozen trades and the coefficient is noise with a decimal point.

This module measures the same components the other way round: score EVERY
symbol at EVERY bar (which `candidate_evaluations` now stores, one row per
symbol per bar, filtered or not), rank them cross-sectionally within each
bar, and correlate that rank against the symbol's own forward return. A
single healthy session yields ~200 observations x 13 bars; the 44-session
window the lane already holds yields six figures of them. That is a test M2
can actually fail.

THE THREE THINGS THIS GETS RIGHT THAT THE TICKET-LEVEL VERSION CANNOT
--------------------------------------------------------------------------
1. RANK, not level. Spearman across the bar's cross-section, so a component
   is judged on whether it ORDERS names correctly -- which is what a
   selection system uses it for -- rather than on the shape of its
   distribution.

2. SIGNED readings, not direction-aligned magnitudes. M6's component_scores
   are aligned to the direction the signal itself chose; correlating an
   aligned magnitude with a signed forward return measures the alignment,
   not the edge. `candidate_evaluations.signed_*` carries the raw signed
   values for exactly this.

3. SESSION-CLUSTERED standard errors. Every symbol in one session shares
   that session's market-wide shock, so observations are nowhere near
   independent and the naive per-observation SE runs far too small -- this
   lane's own directional research measured 1.6x to 4.7x too small on
   precisely this mistake, and a 2026-08-06 walk-forward found day-clustering
   inflated the SE 2.5x. The IC is therefore computed PER SESSION and the
   standard error is taken across those session means. n in the t-statistic
   is the number of SESSIONS, never the number of observations.

FORWARD RETURNS are intra-session only: LEAD(close, h) partitioned by
(symbol, session), so a horizon that runs past 15:15 yields NULL rather than
silently jumping the overnight gap. Vanguard's thesis is an intraday one
with an EOD time stop (see fusion/m7_risk.py's scope note); measuring it
across a gap would be measuring a different strategy.

    python vanguard/research/cross_section_ic.py --lookback-days 120 --write
    python vanguard/research/cross_section_ic.py --horizons 1,2,4
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras
from scipy import stats

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"

# The signed columns candidate_evaluations stores, plus conviction. Conviction
# is included as an UNSIGNED strength reading and is scored against the
# ABSOLUTE forward move, not the signed one -- it claims "this is a bigger
# opportunity", not "this goes up".
SIGNED_COMPONENTS = ("signed_flow", "signed_rs", "signed_timing", "signed_regime")
UNSIGNED_COMPONENTS = ("conviction",)
DEFAULT_HORIZONS = (1, 2, 4)
# Below this many names in a bar, a cross-sectional rank correlation is not a
# cross-section. The bar is skipped, and the skip is reported.
MIN_NAMES_PER_BAR = 25
# Below this many sessions, the clustered SE has fewer degrees of freedom than
# it needs to mean anything. The estimate is still stored, flagged.
MIN_SESSIONS = 20


# ── pure statistics (no DB -- unit-testable offline) ───────────────────────
def bar_ic(values: pd.Series, forward: pd.Series) -> float | None:
    """Spearman rank IC across one bar's cross-section.

    None (not 0.0) when the bar has too few names or no variation in either
    series -- a constant column has no rank order to correlate.
    """
    frame = pd.DataFrame({"x": values, "y": forward}).dropna()
    if len(frame) < MIN_NAMES_PER_BAR:
        return None
    if frame["x"].nunique() < 2 or frame["y"].nunique() < 2:
        return None
    rho, _ = stats.spearmanr(frame["x"], frame["y"])
    return None if not np.isfinite(rho) else float(rho)


def aggregate_session_ics(session_ics: list[float]) -> dict:
    """Mean IC and its SESSION-CLUSTERED standard error.

    The clustering is structural, not a correction applied afterwards: each
    input is already one session's own IC, so the spread across them IS the
    between-session variance. n is the session count.
    """
    values = [v for v in session_ics if v is not None and np.isfinite(v)]
    n = len(values)
    if n == 0:
        return {"mean_ic": None, "se": None, "t_stat": None, "ci_low": None,
                "ci_high": None, "n_sessions": 0}
    mean = float(np.mean(values))
    if n < 2:
        return {"mean_ic": mean, "se": None, "t_stat": None, "ci_low": None,
                "ci_high": None, "n_sessions": n}
    se = float(np.std(values, ddof=1) / np.sqrt(n))
    t_stat = mean / se if se else None
    # Student-t, not normal: with 20-40 sessions the tails matter.
    crit = float(stats.t.ppf(0.975, df=n - 1))
    return {
        "mean_ic": mean, "se": se,
        "t_stat": float(t_stat) if t_stat is not None else None,
        "ci_low": mean - crit * se, "ci_high": mean + crit * se,
        "n_sessions": n,
    }


def decile_profile(values: pd.Series, forward: pd.Series, n_buckets: int = 10) -> list[dict]:
    """Mean forward return by predictor decile, pooled over the window.

    Reported alongside the IC because a monotone decile profile and a
    significant IC answer different questions: the IC asks whether the
    ordering is right on average, the profile asks whether the extremes are
    where the payoff lives. A component can have a small IC and a usable top
    decile, or a decent IC driven entirely by the middle.
    """
    frame = pd.DataFrame({"x": values, "y": forward}).dropna()
    if len(frame) < n_buckets * 10:
        return []
    try:
        frame["bucket"] = pd.qcut(frame["x"].rank(method="first"), n_buckets, labels=False)
    except ValueError:
        return []
    out = []
    for bucket, group in frame.groupby("bucket"):
        out.append({
            "bucket": int(bucket),
            "n": int(len(group)),
            "x_low": float(group["x"].min()),
            "x_high": float(group["x"].max()),
            "mean_fwd": float(group["y"].mean()),
            "hit_rate": float((group["y"] > 0).mean()),
        })
    return out


# ── loaders ────────────────────────────────────────────────────────────────
def load_evaluations(connection, start: date, end: date) -> pd.DataFrame:
    query = """
        SELECT ts, symbol, sector20, conviction, direction,
               signed_flow, signed_rs, signed_timing, signed_regime,
               flow_age_sessions, first_failed_leg, survived_filter
        FROM candidate_evaluations
        WHERE ts >= %(start)s AND ts < %(end)s
    """
    frame = pd.read_sql(query, connection, params={
        "start": start, "end": end + timedelta(days=1)})
    if not frame.empty:
        frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    return frame


def load_forward_returns(connection, start: date, end: date, horizons: tuple[int, ...]) -> pd.DataFrame:
    """Intra-session forward return at each horizon, per (symbol, bar).

    LEAD is partitioned by (symbol, session) so no horizon can cross the
    overnight gap; a bar too close to the close simply has NULL at that
    horizon. The same :15/:45 session-grid filter M5 applies is applied here
    -- measuring a signal computed on the exchange grid against returns taken
    off a second, 15-minute-offset grid would misalign every pair.
    """
    leads = ",\n".join(
        f"LEAD(close, {h}) OVER w AS fwd_close_{h}" for h in horizons
    )
    query = f"""
        WITH bars AS (
            SELECT DISTINCT ON (underlying, time)
                   underlying AS symbol, time, close
            FROM underlying_spot_candles
            WHERE interval = '30minute'
              AND time >= %(start)s AND time < %(end)s
              AND underlying IN (SELECT symbol FROM sector_taxonomy WHERE instrument_type = 'Equity')
              AND EXTRACT(minute FROM time AT TIME ZONE 'Asia/Kolkata') IN (15, 45)
              AND (time AT TIME ZONE 'Asia/Kolkata')::time
                  BETWEEN TIME '09:15' AND TIME '15:15'
            ORDER BY underlying, time, volume DESC NULLS LAST
        )
        SELECT symbol, time AS ts, close, {leads}
        FROM bars
        WINDOW w AS (
            PARTITION BY symbol, date(time AT TIME ZONE 'Asia/Kolkata')
            ORDER BY time
        )
    """  # noqa: S608 - `leads` is built from int horizons only
    frame = pd.read_sql(query, connection, params={
        "start": start, "end": end + timedelta(days=1)})
    if frame.empty:
        return frame
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    frame["close"] = frame["close"].astype(float)
    for h in horizons:
        col = f"fwd_close_{h}"
        frame[col] = frame[col].astype(float)
        frame[f"fwd_ret_{h}"] = (frame[col] - frame["close"]) / frame["close"]
    return frame


# ── the study ──────────────────────────────────────────────────────────────
def run_study(evaluations: pd.DataFrame, forwards: pd.DataFrame,
              horizons: tuple[int, ...]) -> list[dict]:
    """One result row per (component, horizon). Pure -- takes frames, not a
    connection, so the whole statistical path is testable offline."""
    if evaluations.empty or forwards.empty:
        return []
    merged = evaluations.merge(forwards, on=["symbol", "ts"], how="inner")
    if merged.empty:
        return []
    merged["session"] = merged["ts"].dt.tz_convert("Asia/Kolkata").dt.date

    results = []
    for horizon in horizons:
        ret_col = f"fwd_ret_{horizon}"
        if ret_col not in merged.columns:
            continue
        for component in SIGNED_COMPONENTS + UNSIGNED_COMPONENTS:
            if component not in merged.columns:
                continue
            unsigned = component in UNSIGNED_COMPONENTS
            target = merged[ret_col].abs() if unsigned else merged[ret_col]
            work = pd.DataFrame({
                "session": merged["session"], "ts": merged["ts"],
                "x": merged[component], "y": target,
            }).dropna()
            if work.empty:
                results.append(_empty_result(component, horizon, unsigned))
                continue

            bar_ics, bars_skipped = [], 0
            for _, group in work.groupby("ts"):
                value = bar_ic(group["x"], group["y"])
                if value is None:
                    bars_skipped += 1
                else:
                    bar_ics.append((group["session"].iloc[0], value))
            session_means = (
                pd.DataFrame(bar_ics, columns=["session", "ic"])
                .groupby("session")["ic"].mean().tolist()
                if bar_ics else []
            )
            stats_out = aggregate_session_ics(session_means)
            results.append({
                "component": component,
                "horizon_bars": horizon,
                "unsigned_vs_abs_return": unsigned,
                "n_obs": int(len(work)),
                "n_bars_scored": len(bar_ics),
                "n_bars_skipped": bars_skipped,
                **stats_out,
                "sample_adequate": stats_out["n_sessions"] >= MIN_SESSIONS,
                "deciles": decile_profile(work["x"], work["y"]),
            })
    return results


def _empty_result(component: str, horizon: int, unsigned: bool) -> dict:
    return {"component": component, "horizon_bars": horizon,
            "unsigned_vs_abs_return": unsigned, "n_obs": 0, "n_bars_scored": 0,
            "n_bars_skipped": 0, "mean_ic": None, "se": None, "t_stat": None,
            "ci_low": None, "ci_high": None, "n_sessions": 0,
            "sample_adequate": False, "deciles": []}


def persist(connection, as_of: date, window: tuple[date, date], results: list[dict]) -> int:
    rows = [
        (as_of, window[0], window[1], r["component"], r["horizon_bars"],
         r["n_obs"], r["n_sessions"], r["mean_ic"], r["se"], r["t_stat"],
         r["ci_low"], r["ci_high"], psycopg2.extras.Json(r))
        for r in results
    ]
    if not rows:
        return 0
    with connection.cursor() as cursor:
        psycopg2.extras.execute_values(
            cursor,
            """INSERT INTO cross_section_ic
               (as_of_date, window_start, window_end, component, horizon_bars,
                n_obs, n_sessions, mean_ic, ic_se_clustered, t_stat,
                ci_low, ci_high, report)
               VALUES %s""",
            rows,
        )
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=120)
    parser.add_argument("--horizons", default=",".join(str(h) for h in DEFAULT_HORIZONS),
                        help="forward horizons in 30-minute bars, comma separated")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    horizons = tuple(int(h) for h in args.horizons.split(",") if h.strip())
    connection = psycopg2.connect(args.dsn)
    connection.autocommit = True
    try:
        as_of = args.as_of
        if as_of is None:
            with connection.cursor() as cursor:
                cursor.execute("SELECT max(ts) FROM candidate_evaluations")
                (latest,) = cursor.fetchone()
            if latest is None:
                print("candidate_evaluations is empty -- run M6 for at least one bar first.")
                print("Nothing is inferred from an empty journal; this is not a zero result.")
                return 0
            as_of = latest.date()
        start = as_of - timedelta(days=args.lookback_days)

        evaluations = load_evaluations(connection, start, as_of)
        forwards = load_forward_returns(connection, start, as_of, horizons)
        print(f"window {start} .. {as_of}   evaluations={len(evaluations):,}  "
              f"forward-return bars={len(forwards):,}")
        if evaluations.empty:
            print("no evaluations in the window -- nothing to correlate.")
            return 0

        results = run_study(evaluations, forwards, horizons)
        if not results:
            print("no (component, horizon) pair had a usable overlap.")
            return 0

        print(f"\n{'component':<16}{'h':>3}{'n_obs':>9}{'sess':>6}{'mean IC':>10}"
              f"{'SE(clust)':>11}{'t':>7}   95% CI")
        for r in sorted(results, key=lambda r: (r["horizon_bars"], r["component"])):
            ic = "—" if r["mean_ic"] is None else f"{r['mean_ic']:+.4f}"
            se = "—" if r["se"] is None else f"{r['se']:.4f}"
            t = "—" if r["t_stat"] is None else f"{r['t_stat']:+.2f}"
            ci = ("—" if r["ci_low"] is None
                  else f"[{r['ci_low']:+.4f}, {r['ci_high']:+.4f}]")
            flag = "" if r["sample_adequate"] else f"  (only {r['n_sessions']} sessions — under-powered)"
            print(f"{r['component']:<16}{r['horizon_bars']:>3}{r['n_obs']:>9,}"
                  f"{r['n_sessions']:>6}{ic:>10}{se:>11}{t:>7}   {ci}{flag}")

        print("\nn in every t-statistic above is the number of SESSIONS, not observations —")
        print("same-session names share a market-wide shock and are not independent.")

        if args.write:
            written = persist(connection, as_of, (start, as_of), results)
            print(f"\nwrote {written} rows to cross_section_ic")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
