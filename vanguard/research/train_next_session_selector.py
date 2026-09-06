"""Train the shadow model that ranks directional moves over 1-2 sessions.

The feature snapshot is the final journaled Vanguard bar of source session D.
The front ATM option and chain ratios are sampled at that timestamp as input
features, but the model target is the underlying auction, not that contract's
life or a 30-minute premium fluctuation. It averages
the side-adjusted underlying return at the next-session and second-session
14:45 closes, measured from the next-session 09:15 close. Thus CE is rewarded
for a rising underlying, PE for a falling underlying, and a one-day spike that
fully reverses on day two is not mislabeled as persistent direction.

The model is deliberately separate from the one-bar M6 model. It is always
registered as shadow and cannot create a ticket or broker order.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime

import numpy as np
import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from model.nonlinear_selector import (  # noqa: E402
    FEATURE_NAMES,
    artifact_sha256,
    feature_row,
    fit_quantile_mlp,
)
from research.train_nonlinear_selector import (  # noqa: E402
    INPUT_COLUMNS,
    INSTRUMENT_COLUMNS,
    chronological_split,
    evaluate,
)

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
FAMILY = "mlp_quantile_v3_direction_1_2d"
HORIZON_BARS = 24
COST_PROVENANCE = (
    "underlying directional-return target; option execution cost is evaluated "
    "separately by the exact-contract watchlist"
)
TARGET_POLICY = {
    "source": "final completed Vanguard candidate snapshot in session D",
    "contract": "front non-expiring-session ATM call and put used as source features",
    "entry": "underlying next-session 09:15 IST close; available 09:45",
    "day_1": "side-adjusted underlying return at next-session 14:45 close",
    "day_2": "side-adjusted underlying return at second-session 14:45 close",
    "label": "mean(day_1, day_2), positive for the correctly directed option side",
    "overnight_gap": "excluded",
    "execution": "shadow observation only",
}

DATA_SQL = """
WITH session_calendar AS MATERIALIZED (
    SELECT session_date,
           lead(session_date, 1) OVER (ORDER BY session_date) AS track_session,
           lead(session_date, 2) OVER (ORDER BY session_date) AS second_session
    FROM (
        SELECT DISTINCT (time AT TIME ZONE 'Asia/Kolkata')::date AS session_date
        FROM underlying_spot_candles WHERE interval='30minute'
    ) sessions
), option_cohorts AS MATERIALIZED (
    SELECT DISTINCT time,underlying
    FROM option_premium_candles
    WHERE interval='30minute' AND option_type IN ('CE','PE')
      AND expiry>(time AT TIME ZONE 'Asia/Kolkata')::date AND close>=5
), spot_cohorts AS MATERIALIZED (
    SELECT DISTINCT time,underlying
    FROM underlying_spot_candles WHERE interval='30minute'
), candidate_eod AS MATERIALIZED (
    SELECT ce.*,
           (ce.ts AT TIME ZONE 'Asia/Kolkata')::date AS source_session,
           row_number() OVER (
               PARTITION BY ce.symbol,(ce.ts AT TIME ZONE 'Asia/Kolkata')::date
               ORDER BY ce.ts DESC
           ) AS session_rank
    FROM candidate_evaluations ce
    JOIN option_cohorts o ON o.time=ce.ts AND o.underlying=ce.symbol
    JOIN spot_cohorts s ON s.time=ce.ts AND s.underlying=ce.symbol
), source_rows AS MATERIALIZED (
    SELECT ce.*, cal.track_session, cal.second_session
    FROM candidate_eod ce
    JOIN session_calendar cal ON cal.session_date=ce.source_session
    WHERE ce.session_rank=1 AND cal.track_session IS NOT NULL
      AND cal.second_session IS NOT NULL
), spot_at_source AS MATERIALIZED (
    SELECT DISTINCT ON (s.time,s.underlying) s.time,s.underlying,s.close
    FROM underlying_spot_candles s
    JOIN source_rows ce ON ce.ts=s.time AND ce.symbol=s.underlying
    WHERE s.interval='30minute'
    ORDER BY s.time,s.underlying,
             CASE s.source WHEN 'upstox_spot' THEN 0 WHEN 'upstox_sweep' THEN 1
                           WHEN 'upstox' THEN 2 WHEN 'fyers_spot' THEN 3
                           WHEN 'fyers' THEN 4 ELSE 9 END,
             s.synced_at DESC
), future_spot AS MATERIALIZED (
    SELECT DISTINCT ON (s.time,s.underlying) s.time,s.underlying,s.close
    FROM underlying_spot_candles s
    JOIN source_rows ce ON ce.symbol=s.underlying
      AND s.time IN (
          ((ce.track_session+time '09:15') AT TIME ZONE 'Asia/Kolkata'),
          ((ce.track_session+time '14:45') AT TIME ZONE 'Asia/Kolkata'),
          ((ce.second_session+time '14:45') AT TIME ZONE 'Asia/Kolkata')
      )
    WHERE s.interval='30minute'
    ORDER BY s.time,s.underlying,
             CASE s.source WHEN 'upstox_spot' THEN 0 WHEN 'upstox_sweep' THEN 1
                           WHEN 'upstox' THEN 2 WHEN 'fyers_spot' THEN 3
                           WHEN 'fyers' THEN 4 ELSE 9 END,
             s.synced_at DESC
), current_options AS MATERIALIZED (
    SELECT DISTINCT ON (ce.ts,ce.symbol,o.option_type)
           ce.ts, ce.source_session, ce.track_session, ce.second_session,
           ce.symbol, ce.sector20,
           ce.flow_score, ce.flow_age_sessions, ce.flow_n_ingredients,
           ce.rs_z20, ce.rs_age_sessions, ce.regime, ce.gex_percentile,
           ce.regime_age_bars, ce.timing_state, ce.timing_score, ce.rvol,
           ce.va_position, ce.best_lag, ce.leadlag_corr, ce.ce_state, ce.pe_state,
           o.option_type, o.instrument_key, o.expiry, o.strike, o.open, o.high,
           o.low, o.close AS premium, o.volume, o.oi, o.iv, o.delta, o.gamma,
           o.theta, o.vega, spot.close AS spot,
           rt.straddle_to_spot, rt.normalized_straddle,
           rt.strangle_straddle_ratio, rt.put_wing_iv_ratio,
           rt.call_wing_iv_ratio, rt.atm_put_call_premium_ratio,
           rt.atm_call_put_extrinsic_ratio, rt.premium_pcr,
           rt.call_itm_atm_extrinsic_ratio, rt.call_otm_atm_extrinsic_ratio,
           rt.put_itm_atm_extrinsic_ratio, rt.put_otm_atm_extrinsic_ratio,
           rt.n_strikes AS ratio_n_strikes
    FROM source_rows ce
    JOIN spot_at_source spot
      ON spot.time=ce.ts AND spot.underlying=ce.symbol
    JOIN option_premium_candles o
      ON o.time=ce.ts AND o.underlying=ce.symbol AND o.interval='30minute'
     AND o.option_type IN ('CE','PE') AND o.expiry>ce.source_session AND o.close>=5
    LEFT JOIN option_premium_ratios rt
      ON rt.ts=o.time AND rt.symbol=o.underlying AND rt.expiry=o.expiry
    ORDER BY ce.ts,ce.symbol,o.option_type,o.expiry,
             abs(o.strike-spot.close),abs(abs(o.delta)-0.5),
             CASE o.source WHEN 'upstox' THEN 0 ELSE 1 END,o.source,o.instrument_key
)
SELECT op.*,
       entry.close AS underlying_entry,
       exit_1.close AS underlying_exit_1,
       exit_2.close AS underlying_exit_2
FROM current_options op
JOIN future_spot entry
  ON entry.underlying=op.symbol
 AND entry.time=((op.track_session+time '09:15') AT TIME ZONE 'Asia/Kolkata')
JOIN future_spot exit_1
  ON exit_1.underlying=op.symbol
 AND exit_1.time=((op.track_session+time '14:45') AT TIME ZONE 'Asia/Kolkata')
JOIN future_spot exit_2
  ON exit_2.underlying=op.symbol
 AND exit_2.time=((op.second_session+time '14:45') AT TIME ZONE 'Asia/Kolkata')
ORDER BY op.ts,op.symbol,op.option_type
"""


def load_examples(connection):
    print("extracting causal 1-2 session directional labels ...", flush=True)
    with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(DATA_SQL)
        rows = cursor.fetchall()
    vectors, targets, sessions, identities = [], [], [], []
    for row in rows:
        entry = float(row["underlying_entry"])
        exit_1 = float(row["underlying_exit_1"])
        exit_2 = float(row["underlying_exit_2"])
        if entry <= 0 or not np.isfinite(exit_1) or not np.isfinite(exit_2):
            continue
        inputs = {name: row[name] for name in INPUT_COLUMNS}
        instrument = {name: row[name] for name in INSTRUMENT_COLUMNS}
        instrument["dte_days"] = (row["expiry"] - row["source_session"]).days
        vectors.append(feature_row(inputs, instrument, row["option_type"], row["ts"]))
        side_sign = 1.0 if row["option_type"] == "CE" else -1.0
        directional_return = side_sign * 0.5 * (
            (exit_1 / entry - 1.0) + (exit_2 / entry - 1.0)
        )
        targets.append(float(np.clip(directional_return, -0.15, 0.15)))
        sessions.append(row["source_session"])
        identities.append(f"{row['ts'].isoformat()}|{row['symbol']}|{row['option_type']}")
    if not vectors:
        raise RuntimeError("no causal 1-2 session directional labels are available")
    return np.vstack(vectors), np.asarray(targets), sessions, identities


def ranking_metrics(model, x, y, sessions, identities, top_n: int = 10,
                    score: str = "median") -> dict:
    """Evaluate the best CE/PE side margin, then top N names per session.

    Directional return is a signed underlying target, so the conditional
    median is the natural ranking statistic.  The intraday option selector's
    lower-tail/width penalty answers a different question (premium P&L after
    costs) and is retained only as a diagnostic comparison.
    """
    quantiles = model.predict(x)
    scores = (quantiles[:, 1] if score == "median"
              else model.conservative_edge(quantiles))
    by_day_symbol: defaultdict = defaultdict(lambda: defaultdict(list))
    for index, (day, identity) in enumerate(zip(sessions, identities)):
        _, symbol, side = identity.rsplit("|", 2)
        by_day_symbol[day][symbol].append((float(scores[index]), side, index))
    selected = []
    daily = []
    for day in sorted(by_day_symbol):
        choices = []
        for symbol, sides in by_day_symbol[day].items():
            ordered = sorted(sides, reverse=True)
            best_score, best_side, best_index = ordered[0]
            other_score = ordered[1][0] if len(ordered) > 1 else best_score
            choices.append((best_score - other_score, symbol, best_side, best_index))
        day_indices = [choice[3] for choice in sorted(choices, reverse=True)[:top_n]]
        selected.extend(day_indices)
        if day_indices:
            daily.append(float(np.mean(y[day_indices] - model.cost_pct)))
    values = y[selected] - model.cost_pct if selected else np.asarray([])
    return {
        "top_n": top_n, "ranking_score": score,
        "selected": len(selected),
        "sessions": len(daily),
        "net_mean": float(np.mean(values)) if len(values) else None,
        "net_median": float(np.median(values)) if len(values) else None,
        "positive_rate": float(np.mean(values > 0)) if len(values) else None,
        "positive_session_rate": float(np.mean(np.asarray(daily) > 0)) if daily else None,
        "worst_session_mean": float(min(daily)) if daily else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    parser.add_argument("--cost-pct", type=float, default=0.0)
    parser.add_argument("--epochs", type=int, default=80)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    connection = psycopg2.connect(args.dsn)
    try:
        x, y, sessions, identities = load_examples(connection)
        dataset_sha256 = hashlib.sha256(
            np.ascontiguousarray(x).tobytes()
            + np.ascontiguousarray(y).tobytes()
            + "\n".join(identities).encode()
        ).hexdigest()
        train, validation, test, train_days, validation_days, test_days = chronological_split(sessions)
        print(
            f"examples={len(y):,}; sessions train={len(train_days)}, embargo=2, "
            f"validation={len(validation_days)}, embargo=2, test={len(test_days)}",
            flush=True,
        )
        model, fit_metrics = fit_quantile_mlp(
            x[train], y[train], x[validation], y[validation],
            cost_pct=args.cost_pct, epochs=args.epochs, seed=20260902,
        )
        model.standardized_clip = 2.0
        model.prediction_clip = (-0.15, 0.15)
        validation_edges = model.conservative_edge(model.predict(x[validation]))
        target_rate = min(0.05, 3.0 * len(validation_days) / max(1, int(validation.sum())))
        model.selection_threshold = max(
            0.0, float(np.quantile(validation_edges, 1.0 - target_rate))
        )
        validation_metrics = evaluate(
            model, x[validation], y[validation], np.asarray(sessions)[validation],
            np.asarray(identities)[validation], model.selection_threshold,
        )
        test_metrics = evaluate(
            model, x[test], y[test], np.asarray(sessions)[test],
            np.asarray(identities)[test], model.selection_threshold,
        )
        validation_ranking = ranking_metrics(
            model, x[validation], y[validation], np.asarray(sessions)[validation],
            np.asarray(identities)[validation],
        )
        test_ranking = ranking_metrics(
            model, x[test], y[test], np.asarray(sessions)[test],
            np.asarray(identities)[test],
        )
        validation_conservative_ranking = ranking_metrics(
            model, x[validation], y[validation], np.asarray(sessions)[validation],
            np.asarray(identities)[validation], score="conservative_edge",
        )
        test_conservative_ranking = ranking_metrics(
            model, x[test], y[test], np.asarray(sessions)[test],
            np.asarray(identities)[test], score="conservative_edge",
        )
        historical_gate_passed = bool(
            (validation_ranking["net_mean"] or 0.0) > 0.0
            and (test_ranking["net_mean"] or 0.0) > 0.0
            and (validation_ranking["positive_session_rate"] or 0.0) >= 0.50
            and (test_ranking["positive_session_rate"] or 0.0) >= 0.50
        )
        # Daily direction is ranked by conditional median and observed even
        # when it is negative. Zero is only a descriptive bullish/bearish
        # confidence boundary; it cannot activate a ticket.
        model.selection_threshold = 0.0
        status = "shadow"
        version = f"{FAMILY}_{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
        model.version = version
        artifact = model.to_artifact()
        artifact["objective"] = TARGET_POLICY
        artifact["horizon_bars"] = HORIZON_BARS
        metrics = {
            **fit_metrics,
            "validation": validation_metrics,
            "test": test_metrics,
            "validation_watchlist_top10": validation_ranking,
            "test_watchlist_top10": test_ranking,
            "validation_conservative_top10": validation_conservative_ranking,
            "test_conservative_top10": test_conservative_ranking,
            "selection_threshold": model.selection_threshold,
            "target_policy": TARGET_POLICY,
            "target_winsorisation": [-0.15, 0.15],
            "standardized_input_clip": 2.0,
            "prediction_clip": [-0.15, 0.15],
            "dataset_sha256": dataset_sha256,
            "embargo_sessions": 2,
            "ranking_score": "within-symbol CE-versus-PE conditional-median margin",
            "historical_gate_passed": historical_gate_passed,
            "historical_gate": (
                "validation and test top-10 mean > 0 and positive sessions >= 50%; "
                "failure still permits prospective shadow observation"
            ),
            "activation_policy": "shadow watchlist only; no ticket or broker path",
        }
        card = {"version": version, "family": FAMILY, "status": status, "metrics": metrics}
        print(json.dumps(card, indent=2))
        if args.write:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO vanguard_model_versions
                           (version,family,status,horizon_bars,cost_pct,cost_provenance,
                            training_start,training_end,validation_start,validation_end,
                            test_start,test_end,n_train,n_validation,n_test,feature_names,
                            metrics,artifact,artifact_sha256)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            version, FAMILY, status, HORIZON_BARS, args.cost_pct,
                            COST_PROVENANCE, train_days[0], train_days[-1],
                            validation_days[0], validation_days[-1], test_days[0],
                            test_days[-1], int(train.sum()), int(validation.sum()),
                            int(test.sum()), psycopg2.extras.Json(list(FEATURE_NAMES)),
                            psycopg2.extras.Json(metrics), psycopg2.extras.Json(artifact),
                            artifact_sha256(artifact),
                        ),
                    )
            print(f"registered {version} as {status}")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
