"""Emit a daily, broker-free 1-2-session directional shadow ranking.

This is deliberately not called by M6 and never writes `tickets` or `orders`.
It scores the final completed Vanguard feature snapshot of a session, stores
both CE and PE forecasts, and lets the immutable watchlist freeze the best side
per underlying.  The option contract is the observation vehicle; q10/q50/q90
are signed UNDERLYING returns over the next one and two sessions.
"""
from __future__ import annotations

import argparse
import os
import sys
from types import SimpleNamespace

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fusion.m6_select import resolve_instruments_at  # noqa: E402
from model.nonlinear_selector import load_swing_model, prediction_rows  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
TIMING_POLICY = "completed_eod_direction_1_2d_v1"


def load_final_evaluations(connection):
    with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """SELECT max(ce.ts) AS ts FROM candidate_evaluations ce
               WHERE EXISTS (
                   SELECT 1 FROM option_premium_candles o
                   WHERE o.time=ce.ts AND o.underlying=ce.symbol
                     AND o.interval='30minute' AND o.option_type IN ('CE','PE')
                     AND o.expiry>(ce.ts AT TIME ZONE 'Asia/Kolkata')::date
                     AND o.close>=5
               ) AND EXISTS (
                   SELECT 1 FROM underlying_spot_candles s
                   WHERE s.time=ce.ts AND s.underlying=ce.symbol
                     AND s.interval='30minute'
               )"""
        )
        head = cursor.fetchone()
        ts = head["ts"] if head else None
        if ts is None:
            return None, []
        cursor.execute(
            """SELECT * FROM candidate_evaluations
               WHERE ts=%s ORDER BY symbol""",
            (ts,),
        )
        rows = cursor.fetchall()
    evaluations = [SimpleNamespace(
        ts=row["ts"], symbol=row["symbol"], sector20=row["sector20"],
        inputs=dict(row),
    ) for row in rows]
    return ts, evaluations


def resolve_directional_outcomes(connection) -> int:
    """Resolve only when both later session closes exist in the archive."""
    with connection.cursor() as cursor:
        cursor.execute(
            """WITH sessions AS MATERIALIZED (
                   SELECT session_date,
                          lead(session_date,1) OVER (ORDER BY session_date) AS day_1,
                          lead(session_date,2) OVER (ORDER BY session_date) AS day_2
                   FROM (
                       SELECT DISTINCT (time AT TIME ZONE 'Asia/Kolkata')::date AS session_date
                       FROM underlying_spot_candles WHERE interval='30minute'
                   ) d
               ), resolved AS (
                   SELECT p.ts,p.symbol,p.option_type,p.model_version,
                          CASE WHEN p.option_type='CE' THEN 1.0 ELSE -1.0 END * 0.5 *
                          ((x1.close/e.close-1.0)+(x2.close/e.close-1.0)) AS target
                   FROM vanguard_model_predictions p
                   JOIN vanguard_model_versions m ON m.version=p.model_version
                   JOIN sessions s
                     ON s.session_date=(p.ts AT TIME ZONE 'Asia/Kolkata')::date
                   JOIN LATERAL (
                       SELECT close FROM underlying_spot_candles u
                       WHERE u.underlying=p.symbol AND u.interval='30minute'
                         AND u.time=((s.day_1+time '09:15') AT TIME ZONE 'Asia/Kolkata')
                       ORDER BY CASE u.source WHEN 'upstox_spot' THEN 0
                                              WHEN 'upstox_sweep' THEN 1
                                              WHEN 'upstox' THEN 2
                                              WHEN 'fyers_spot' THEN 3
                                              WHEN 'fyers' THEN 4 ELSE 9 END,
                                u.synced_at DESC LIMIT 1
                   ) e ON true
                   JOIN LATERAL (
                       SELECT close FROM underlying_spot_candles u
                       WHERE u.underlying=p.symbol AND u.interval='30minute'
                         AND u.time=((s.day_1+time '14:45') AT TIME ZONE 'Asia/Kolkata')
                       ORDER BY CASE u.source WHEN 'upstox_spot' THEN 0
                                              WHEN 'upstox_sweep' THEN 1
                                              WHEN 'upstox' THEN 2
                                              WHEN 'fyers_spot' THEN 3
                                              WHEN 'fyers' THEN 4 ELSE 9 END,
                                u.synced_at DESC LIMIT 1
                   ) x1 ON true
                   JOIN LATERAL (
                       SELECT close FROM underlying_spot_candles u
                       WHERE u.underlying=p.symbol AND u.interval='30minute'
                         AND u.time=((s.day_2+time '14:45') AT TIME ZONE 'Asia/Kolkata')
                       ORDER BY CASE u.source WHEN 'upstox_spot' THEN 0
                                              WHEN 'upstox_sweep' THEN 1
                                              WHEN 'upstox' THEN 2
                                              WHEN 'fyers_spot' THEN 3
                                              WHEN 'fyers' THEN 4 ELSE 9 END,
                                u.synced_at DESC LIMIT 1
                   ) x2 ON true
                   WHERE m.horizon_bars=24 AND p.realized_return IS NULL
                     AND p.timing_policy=%s AND e.close>0
               )
               UPDATE vanguard_model_predictions p
               SET realized_return=r.target, realized_net_return=r.target,
                   resolved_at=now()
               FROM resolved r
               WHERE p.ts=r.ts AND p.symbol=r.symbol
                 AND p.option_type=r.option_type AND p.model_version=r.model_version""",
            (TIMING_POLICY,),
        )
        return cursor.rowcount


def score_latest(connection) -> dict:
    model = load_swing_model(connection)
    if model is None:
        return {"scored": 0, "reason": "no compatible horizon-24 shadow model"}
    ts, evaluations = load_final_evaluations(connection)
    if ts is None or not evaluations:
        return {"scored": 0, "reason": "candidate evaluation journal is empty"}
    # The current front option supplies causal chain features and is marked as
    # a one-session execution proxy. The two-session outcome is measured on
    # the underlying and does not pretend the source contract survives it.
    instruments = resolve_instruments_at(
        connection, [evaluation.symbol for evaluation in evaluations], ts)
    triples = [
        (evaluation, instruments[(evaluation.symbol, side)], side)
        for evaluation in evaluations for side in ("CE", "PE")
        if (evaluation.symbol, side) in instruments
    ]
    forecasts = prediction_rows(model, triples)
    by_symbol = {}
    for forecast in forecasts:
        by_symbol.setdefault(forecast["evaluation"].symbol, {})[
            forecast["option_type"]
        ] = forecast
    rows = []
    for forecast in forecasts:
        evaluation = forecast["evaluation"]
        instrument = forecast["instrument_data"]
        # Compare CE and PE for the SAME underlying so a common forecast bias
        # cannot masquerade as directional confidence.
        other = by_symbol[evaluation.symbol].get(
            "PE" if forecast["option_type"] == "CE" else "CE"
        )
        rank_score = forecast["q50"] - other["q50"] if other is not None else 0.0
        rows.append((
            ts, evaluation.symbol, forecast["option_type"], model.version,
            forecast["q10"], forecast["q50"], forecast["q90"],
            forecast["edge"], 0.0, False,
            "experimental 1-2-session directional shadow; no ticket path",
            instrument["instrument"], instrument["strike"], instrument["expiry"],
            instrument["premium"], instrument["source_mark_ts"],
            TIMING_POLICY, rank_score,
        ))
    if rows:
        with connection.cursor() as cursor:
            psycopg2.extras.execute_values(
                cursor,
                """INSERT INTO vanguard_model_predictions
                   (ts,symbol,option_type,model_version,q10_return,q50_return,q90_return,
                    conservative_edge,selection_threshold,selected,reason,instrument,
                    strike,expiry,entry_mark,source_mark_ts,timing_policy,ranking_score)
                   VALUES %s ON CONFLICT (ts,symbol,option_type,model_version) DO NOTHING""",
                rows,
                page_size=500,
            )
    return {
        "model_version": model.version,
        "source_ts": ts.isoformat(),
        "evaluations": len(evaluations),
        "scored": len(rows),
        "ranking": "within-symbol CE-versus-PE conditional-median margin",
        "paper_only": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    connection = psycopg2.connect(args.dsn)
    try:
        if not args.write:
            model = load_swing_model(connection)
            ts, evaluations = load_final_evaluations(connection)
            print({"model": getattr(model, "version", None), "source_ts": ts,
                   "evaluations": len(evaluations), "write": False})
            return 0
        with connection:
            resolved = resolve_directional_outcomes(connection)
            result = score_latest(connection)
        print({**result, "resolved": resolved})
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
