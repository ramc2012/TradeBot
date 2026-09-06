"""Train and register Vanguard's nonlinear option-P&L selector.

The population is every journaled candidate snapshot, expanded to both its ATM
call and put.  Entry features are taken only from that timestamp; the label is
the same contract's next 30-minute close.  Splits are whole chronological NSE
sessions with a two-session embargo.  Historical bid/ask is unavailable, so
the model predicts mark-to-mark return and the decision score deducts an
explicit assumed round-trip option cost.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime
from types import SimpleNamespace

import numpy as np
import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from model.nonlinear_selector import (  # noqa: E402
    FAMILY, FEATURE_NAMES, artifact_sha256, feature_row, fit_quantile_mlp,
)

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
COST_PROVENANCE = (
    "assumed 1% round-trip premium cost; archive has closes/Greeks but no historical bid/ask"
)

DATA_SQL = """
WITH current_options AS MATERIALIZED (
    SELECT DISTINCT ON (o.time, o.underlying, o.option_type)
           o.time, o.underlying, o.option_type, o.instrument_key, o.expiry,
           o.strike, o.open, o.high, o.low, o.close, o.volume, o.oi,
           o.iv, o.delta, o.gamma, o.theta, o.vega, s.close AS spot
    FROM option_premium_candles o
    JOIN underlying_spot_candles s
      ON s.time = o.time AND s.underlying = o.underlying
     AND s.interval = '30minute'
    WHERE o.interval = '30minute'
      AND o.time >= %(lo)s AND o.time <= %(hi)s
      AND o.expiry > o.time::date
      AND o.close >= 5
      AND o.option_type IN ('CE', 'PE')
    ORDER BY o.time, o.underlying, o.option_type, o.expiry,
             abs(o.strike - s.close), abs(abs(o.delta) - 0.5)
)
SELECT ce.ts, ce.symbol, ce.sector20,
       ce.flow_score, ce.flow_age_sessions, ce.flow_n_ingredients,
       ce.rs_z20, ce.rs_age_sessions, ce.regime, ce.gex_percentile,
       ce.regime_age_bars, ce.timing_state, ce.timing_score, ce.rvol,
       ce.va_position, ce.best_lag, ce.leadlag_corr, ce.ce_state, ce.pe_state,
       op.option_type, op.expiry, op.strike, op.open, op.high, op.low,
       op.close AS premium, op.volume, op.oi, op.iv, op.delta, op.gamma,
       op.theta, op.vega, op.spot,
       rt.straddle_to_spot, rt.normalized_straddle,
       rt.strangle_straddle_ratio, rt.put_wing_iv_ratio,
       rt.call_wing_iv_ratio, rt.atm_put_call_premium_ratio,
       rt.atm_call_put_extrinsic_ratio, rt.premium_pcr,
       rt.call_itm_atm_extrinsic_ratio, rt.call_otm_atm_extrinsic_ratio,
       rt.put_itm_atm_extrinsic_ratio, rt.put_otm_atm_extrinsic_ratio,
       rt.n_strikes AS ratio_n_strikes,
       nx.close AS exit_premium
FROM candidate_evaluations ce
JOIN current_options op ON op.time = ce.ts AND op.underlying = ce.symbol
LEFT JOIN option_premium_ratios rt
  ON rt.ts=op.time AND rt.symbol=op.underlying AND rt.expiry=op.expiry
JOIN option_premium_candles nx
  ON nx.instrument_key = op.instrument_key
 AND nx.interval = '30minute'
 AND nx.time = ce.ts + interval '30 minutes'
 AND nx.time >= %(lo)s AND nx.time <= %(exit_hi)s
ORDER BY ce.ts, ce.symbol, op.option_type
"""

INPUT_COLUMNS = (
    "flow_score", "flow_age_sessions", "flow_n_ingredients", "rs_z20",
    "rs_age_sessions", "regime", "gex_percentile", "regime_age_bars",
    "timing_state", "timing_score", "rvol", "va_position", "best_lag",
    "leadlag_corr", "ce_state", "pe_state",
)
INSTRUMENT_COLUMNS = (
    "expiry", "strike", "open", "high", "low", "premium", "volume", "oi",
    "iv", "delta", "gamma", "theta", "vega", "spot",
    "straddle_to_spot", "normalized_straddle", "strangle_straddle_ratio",
    "put_wing_iv_ratio", "call_wing_iv_ratio", "atm_put_call_premium_ratio",
    "atm_call_put_extrinsic_ratio", "premium_pcr",
    "call_itm_atm_extrinsic_ratio", "call_otm_atm_extrinsic_ratio",
    "put_itm_atm_extrinsic_ratio", "put_otm_atm_extrinsic_ratio",
    "ratio_n_strikes",
)


def load_examples(connection) -> tuple[np.ndarray, np.ndarray, list, list[str]]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT min(ts), max(ts) FROM candidate_evaluations")
        lo, hi = cursor.fetchone()
    if lo is None or hi is None:
        raise RuntimeError("candidate_evaluations is empty")
    print(f"extracting both CE/PE labels from {lo.date()} through {hi.date()} ...", flush=True)
    with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(DATA_SQL, {"lo": lo, "hi": hi, "exit_hi": hi})
        rows = cursor.fetchall()
    # exit_hi needs one bar beyond the final candidate timestamp.  The latest
    # candidate is itself session-final and has no same-session next bar, so
    # using hi is both sufficient and prevents an overnight label.
    vectors, targets, sessions, identities = [], [], [], []
    for row in rows:
        entry = float(row["premium"])
        exit_price = float(row["exit_premium"])
        if entry <= 0 or not np.isfinite(exit_price):
            continue
        inputs = {name: row[name] for name in INPUT_COLUMNS}
        instrument = {name: row[name] for name in INSTRUMENT_COLUMNS}
        instrument["dte_days"] = (row["expiry"] - row["ts"].date()).days
        vectors.append(feature_row(inputs, instrument, row["option_type"], row["ts"]))
        # Winsorisation limits bad/corporate-action prints without redefining
        # ordinary option convexity.  The untouched return remains derivable
        # from source data; this bound is part of the versioned method card.
        targets.append(float(np.clip(exit_price / entry - 1.0, -0.75, 2.0)))
        sessions.append(row["ts"].date())
        identities.append(f"{row['ts'].isoformat()}|{row['symbol']}|{row['option_type']}")
    if not vectors:
        raise RuntimeError("no candidate snapshots have a same-contract next-bar option label")
    return np.vstack(vectors), np.asarray(targets), sessions, identities


def chronological_split(sessions: list, embargo_sessions: int = 2):
    unique = sorted(set(sessions))
    if len(unique) < 30:
        raise RuntimeError(f"only {len(unique)} labelled sessions; need at least 30")
    test_n = max(8, round(len(unique) * 0.20))
    validation_n = max(8, round(len(unique) * 0.20))
    test_start = len(unique) - test_n
    validation_end = test_start - embargo_sessions
    validation_start = validation_end - validation_n
    train_end = validation_start - embargo_sessions
    if train_end < 10:
        raise RuntimeError("not enough sessions after chronological embargoes")
    train_days = set(unique[:train_end])
    validation_days = set(unique[validation_start:validation_end])
    test_days = set(unique[test_start:])
    array = np.asarray(sessions)
    return (
        np.asarray([day in train_days for day in array]),
        np.asarray([day in validation_days for day in array]),
        np.asarray([day in test_days for day in array]),
        unique[:train_end], unique[validation_start:validation_end], unique[test_start:],
    )


def pinball(y: np.ndarray, q: np.ndarray) -> float:
    levels = np.asarray([0.1, 0.5, 0.9])
    error = y.reshape(-1, 1) - q
    return float(np.mean(np.maximum(levels * error, (levels - 1.0) * error)))


def online_selections(edges: np.ndarray, targets: np.ndarray, sessions: np.ndarray,
                      identities: np.ndarray, threshold: float, max_per_session: int = 3):
    selected = []
    counts: defaultdict = defaultdict(int)
    order = np.argsort(identities)
    for index in order:
        day = sessions[index]
        if edges[index] >= threshold and counts[day] < max_per_session:
            selected.append(index)
            counts[day] += 1
    return np.asarray(selected, dtype=int)


def evaluate(model, x, y, sessions, identities, threshold) -> dict:
    q = model.predict(x)
    edge = model.conservative_edge(q)
    selected = online_selections(edge, y, np.asarray(sessions), np.asarray(identities), threshold)
    net = y[selected] - model.cost_pct if len(selected) else np.asarray([])
    by_day: defaultdict = defaultdict(list)
    for index, value in zip(selected, net):
        by_day[sessions[index]].append(float(value))
    daily = np.asarray([np.mean(values) for values in by_day.values()])
    return {
        "n": len(y), "pinball": pinball(y, q),
        "q10_coverage": float(np.mean(y <= q[:, 0])),
        "q90_coverage": float(np.mean(y <= q[:, 2])),
        "selected": int(len(selected)), "selected_sessions": len(by_day),
        "selected_net_mean": float(np.mean(net)) if len(net) else None,
        "selected_net_median": float(np.median(net)) if len(net) else None,
        "positive_selected_rate": float(np.mean(net > 0)) if len(net) else None,
        "positive_session_rate": float(np.mean(daily > 0)) if len(daily) else None,
        "all_net_mean": float(np.mean(y - model.cost_pct)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    parser.add_argument("--cost-pct", type=float, default=0.01)
    parser.add_argument("--epochs", type=int, default=80)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    connection = psycopg2.connect(args.dsn)
    try:
        x, y, sessions, identities = load_examples(connection)
        train, validation, test, train_days, validation_days, test_days = chronological_split(sessions)
        print(f"examples={len(y):,}; sessions train={len(train_days)}, embargo=2, "
              f"validation={len(validation_days)}, embargo=2, test={len(test_days)}", flush=True)
        model, fit_metrics = fit_quantile_mlp(
            x[train], y[train], x[validation], y[validation],
            cost_pct=args.cost_pct, epochs=args.epochs,
        )
        validation_q = model.predict(x[validation])
        validation_edges = model.conservative_edge(validation_q)
        # Three opportunities per session, learned from validation only.  A
        # positive floor preserves explicit economic abstention after costs.
        target_rate = min(0.05, 3.0 * len(validation_days) / max(1, int(validation.sum())))
        model.selection_threshold = max(0.0, float(np.quantile(validation_edges, 1.0 - target_rate)))
        validation_metrics = evaluate(
            model, x[validation], y[validation], np.asarray(sessions)[validation],
            np.asarray(identities)[validation], model.selection_threshold,
        )
        test_metrics = evaluate(
            model, x[test], y[test], np.asarray(sessions)[test],
            np.asarray(identities)[test], model.selection_threshold,
        )
        # Promotion is only to the broker-free paper selector.  Sparse or
        # negative holdout evidence is retained as refused/shadow, not forced.
        promote = bool(
            test_metrics["selected"] >= 10
            and test_metrics["selected_sessions"] >= 5
            and (test_metrics["selected_net_mean"] or 0.0) > 0.0
            and (test_metrics["positive_session_rate"] or 0.0) >= 0.50
        )
        # A repeatedly inspected research holdout is not a new prospective
        # confirmation. Training must never automatically activate tickets.
        status = "shadow"
        version = f"{FAMILY}_{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
        model.version = version
        artifact = model.to_artifact()
        metrics = {
            **fit_metrics, "validation": validation_metrics, "test": test_metrics,
            "selection_threshold": model.selection_threshold,
            "target_winsorisation": [-0.75, 2.0], "embargo_sessions": 2,
            "promotion_rule": "test selected>=10, sessions>=5, net mean>0, positive sessions>=50%",
            "historical_gate_passed": promote,
            "activation_policy": "shadow only; prospective review and explicit promotion required",
        }
        print(json.dumps({"version": version, "status": status, "metrics": metrics}, indent=2))
        if args.write:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO vanguard_model_versions
                           (version, family, status, horizon_bars, cost_pct, cost_provenance,
                            training_start, training_end, validation_start, validation_end,
                            test_start, test_end, n_train, n_validation, n_test,
                            feature_names, metrics, artifact, artifact_sha256)
                           VALUES (%s,%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (version, FAMILY, status, args.cost_pct, COST_PROVENANCE,
                         train_days[0], train_days[-1], validation_days[0], validation_days[-1],
                         test_days[0], test_days[-1], int(train.sum()), int(validation.sum()), int(test.sum()),
                         psycopg2.extras.Json(list(FEATURE_NAMES)), psycopg2.extras.Json(metrics),
                         psycopg2.extras.Json(artifact), artifact_sha256(artifact)),
                    )
            print(f"registered {version} as {status}")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
