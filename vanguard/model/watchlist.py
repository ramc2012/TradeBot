"""Freeze each session's model list and mark it through the next session.

The watchlist is deliberately separate from M6 tickets.  A shadow model can
therefore be observed prospectively without gaining an execution path.  Once
the end-of-session ranking is captured, membership and contract identity never
change; only exact-contract marks and performance fields are updated.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from typing import Any, Iterable

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from model.watchlist_exits import POLICY, HARD_STOP_POLICY, analyse_path, policy_card

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
TOP_N = int(os.environ.get("VANGUARD_WATCHLIST_TOP_N", "10"))
SELECTION_RULE = (
    "final session horizon-specific model ranking; qualification is ranking_score >= versioned threshold; "
    "same completed-bar exact-contract mark required; best CE/PE per underlying; descending score; "
    "observation-only next-session tracking, NOT a ticket list or BTST"
)


def rank_candidates(rows: Iterable[dict[str, Any]], top_n: int = TOP_N) -> list[dict[str, Any]]:
    """Return a deterministic observation ranking, one contract per underlying.

    Qualification remains attached to every row but is not a membership gate.
    The watchlist exists to measure the shadow model prospectively; applying
    the ticket abstention threshold here made healthy negative-edge sessions
    disappear completely and left nothing to observe. Ticket emission remains
    independently threshold-gated in M6.
    """
    ranked = [dict(row) for row in rows]
    ranked.sort(key=lambda row: (
        -float(row.get("ranking_score", row["conservative_edge"])),
        str(row["symbol"]),
        str(row["option_type"]),
    ))
    result = []
    seen = set()
    for row in ranked:
        if row["symbol"] in seen:
            continue
        seen.add(row["symbol"])
        row["qualified"] = (
            float(row.get("ranking_score", row["conservative_edge"]))
            >= float(row["selection_threshold"])
        )
        row["rank"] = len(result) + 1
        result.append(row)
        if len(result) >= top_n:
            break
    return result


def performance(entry_mark: float, latest_mark: float, high: float,
                low: float) -> tuple[float, float, float]:
    """Current return, maximum favourable excursion and maximum adverse excursion."""
    if entry_mark <= 0:
        raise ValueError("entry mark must be positive")
    return (
        latest_mark / entry_mark - 1.0,
        high / entry_mark - 1.0,
        low / entry_mark - 1.0,
    )


def latest_prediction_session(connection) -> date | None:
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT max((ts AT TIME ZONE 'Asia/Kolkata')::date)
               FROM vanguard_model_predictions"""
        )
        return cursor.fetchone()[0]


def _snapshot(connection, source_session: date,
              top_n: int = TOP_N) -> tuple[str, Any, list[dict[str, Any]]] | None:
    """Read the newest model's final prediction bar for one NSE session."""
    with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """SELECT p.model_version, max(p.ts) AS prediction_ts
               FROM vanguard_model_predictions p
               JOIN vanguard_model_versions m ON m.version=p.model_version
               WHERE (p.ts AT TIME ZONE 'Asia/Kolkata')::date=%(session)s
               GROUP BY p.model_version, m.created_at, m.horizon_bars
               ORDER BY (m.horizon_bars=24) DESC, m.created_at DESC LIMIT 1""",
            {"session": source_session},
        )
        head = cursor.fetchone()
        if head is None:
            return None
        cursor.execute(
            """SELECT p.symbol, p.option_type, p.instrument, p.strike, p.expiry,
                      quote.time AS source_mark_ts, quote.close AS source_mark,
                      p.q10_return, p.q50_return, p.q90_return,
                      conservative_edge, selection_threshold,
                      COALESCE(p.ranking_score,p.conservative_edge) AS ranking_score
               FROM vanguard_model_predictions p
               JOIN LATERAL (
                   SELECT o.time, o.close
                   FROM option_premium_candles o
                   WHERE o.underlying=p.symbol AND o.option_type=p.option_type
                     AND o.strike=p.strike AND o.expiry=p.expiry
                     AND o.interval='30minute' AND o.time = p.ts
                     AND (o.time AT TIME ZONE 'Asia/Kolkata')::date =
                         (p.ts AT TIME ZONE 'Asia/Kolkata')::date
                   ORDER BY o.time DESC LIMIT 1
               ) quote ON true
               WHERE p.model_version=%(version)s AND p.ts=%(ts)s
                 AND p.timing_policy IN
                     ('completed_same_bar_v1','completed_eod_direction_1_2d_v1')
                 AND p.source_mark_ts=p.ts
                 AND p.instrument IS NOT NULL AND p.strike IS NOT NULL AND p.expiry IS NOT NULL
               ORDER BY COALESCE(p.ranking_score,p.conservative_edge) DESC,
                        symbol, option_type""",
            {"version": head["model_version"], "ts": head["prediction_ts"]},
        )
        rows = cursor.fetchall()
    return head["model_version"], head["prediction_ts"], rank_candidates(rows, top_n)


def freeze_session(connection, source_session: date, top_n: int = TOP_N) -> dict[str, Any]:
    """Capture a session once. Repeated calls preserve the original membership."""
    with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            "SELECT * FROM vanguard_watchlist_runs WHERE source_session=%s",
            (source_session,),
        )
        existing = cursor.fetchone()
    if existing is not None:
        return {"created": False, **dict(existing)}

    snapshot = _snapshot(connection, source_session, top_n)
    if snapshot is None:
        raise RuntimeError(f"no model predictions for {source_session}")
    version, prediction_ts, ranked = snapshot
    ranked = ranked[:top_n]
    with connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO vanguard_watchlist_runs
               (source_session, model_version, prediction_ts, item_count, top_n,
                selection_rule, status)
               VALUES (%s,%s,%s,%s,%s,%s,'awaiting_next_session')""",
            (source_session, version, prediction_ts, len(ranked), top_n, SELECTION_RULE),
        )
        psycopg2.extras.execute_values(
            cursor,
            """INSERT INTO vanguard_watchlist_items
               (source_session, rank, symbol, option_type, direction, instrument,
                strike, expiry, source_mark_ts, source_mark,
                q10_return, q50_return, q90_return,
                conservative_edge, selection_threshold, ranking_score, status) VALUES %s""",
            [(
                source_session, row["rank"], row["symbol"], row["option_type"],
                "bullish" if row["option_type"] == "CE" else "bearish",
                row["instrument"], row["strike"], row["expiry"],
                row["source_mark_ts"], row["source_mark"],
                row["q10_return"], row["q50_return"], row["q90_return"],
                row["conservative_edge"], row["selection_threshold"],
                row.get("ranking_score", row["conservative_edge"]),
                "awaiting_next_session",
            ) for row in ranked],
        )
    return {
        "created": True, "source_session": source_session,
        "model_version": version, "prediction_ts": prediction_ts,
        "item_count": len(ranked), "status": "awaiting_next_session",
    }


def _next_observed_session(connection, source_session: date) -> date | None:
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT min((time AT TIME ZONE 'Asia/Kolkata')::date)
               FROM option_premium_candles
               WHERE interval='30minute'
                 AND (time AT TIME ZONE 'Asia/Kolkata')::date > %s""",
            (source_session,),
        )
        return cursor.fetchone()[0]


def _register_exit_policy(connection):
    card = policy_card()
    with connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO vanguard_watchlist_exit_policies (version, policy)
               VALUES (%s,%s) ON CONFLICT (version) DO NOTHING""",
            (POLICY.version, psycopg2.extras.Json(card)))
        cursor.execute(
            "SELECT policy, registered_at FROM vanguard_watchlist_exit_policies WHERE version=%s",
            (POLICY.version,))
        saved, registered_at = cursor.fetchone()
    if saved != card:
        raise RuntimeError("exit parameters changed without a new policy version")
    return registered_at


def track_open_watchlists(connection, refresh_session: date | None = None) -> dict[str, int]:
    """Audit and mark exact contracts; exit replay never writes a trading book.

    Closed sessions are recomputed ONLY by the explicit repair argument. Their
    original performance is retained once in performance_audit. Membership,
    model versions, source marks and contract identities are never replaced.
    """
    registered_at = _register_exit_policy(connection)
    as_of = datetime.now(timezone.utc)
    with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """SELECT * FROM vanguard_watchlist_runs
               WHERE (status IN ('awaiting_next_session','tracking') AND %(repair)s IS NULL)
                  OR source_session=%(repair)s ORDER BY source_session""",
            {"repair": refresh_session})
        runs = cursor.fetchall()

    updated_items = closed_runs = 0
    for run in runs:
        track_session = run["track_session"] or _next_observed_session(connection, run["source_session"])
        if track_session is None:
            continue
        with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(
                "SELECT * FROM vanguard_watchlist_items WHERE source_session=%s ORDER BY rank",
                (run["source_session"],))
            items = cursor.fetchall()
            cursor.execute(
                """SELECT DISTINCT ON (i.id, o.time) i.id, o.time,
                          o.open, o.high, o.low, o.close, o.volume
                   FROM vanguard_watchlist_items i JOIN option_premium_candles o
                     ON o.underlying=i.symbol AND o.option_type=i.option_type
                    AND o.strike=i.strike AND o.expiry=i.expiry AND o.interval='30minute'
                   WHERE i.source_session=%(source)s
                     AND (o.time AT TIME ZONE 'Asia/Kolkata')::date=%(track)s
                   ORDER BY i.id, o.time, (o.source='upstox') DESC, o.source ASC""",
                {"source": run["source_session"], "track": track_session})
            quotes = cursor.fetchall()
        paths = {}
        for row in quotes:
            paths.setdefault(row["id"], []).append(dict(row))
        # The clock, not an unrelated symbol's candle, determines session end.
        session_ended = as_of >= datetime.combine(
            track_session, datetime.min.time(), tzinfo=timezone.utc).replace(hour=9, minute=45)
        # 09:45 UTC = the declared 15:15 IST watchlist exit, not exchange close.
        statuses = []
        for item in items:
            analysis = analyse_path(paths.get(item["id"], []), as_of)
            good = "entry_mark" in analysis
            if good:
                analysis["hard_stop_control"] = analyse_path(paths.get(item["id"], []), as_of,
                                                           policy=HARD_STOP_POLICY).get("runner")
                analysis["policy"] = policy_card()
                analysis["policy_registered_at"] = registered_at
                analysis["provenance"] = "neural_next_session_not_btst"
                analysis["validation_basis"] = (
                    "prospective_policy" if registered_at <= analysis["entry_available_at"]
                    and run["generated_at"] <= analysis["entry_available_at"]
                    else "retrospective_replay_not_validation")
                analysis["baseline_kind"] = "session_close" if analysis["status"] == "closed" else "latest_mark"
                analysis["source_mark_age_minutes"] = (
                    (run["prediction_ts"] - item["source_mark_ts"]).total_seconds() / 60
                    if item["source_mark_ts"] else None)
                state = analysis["status"]
                if session_ended and state != "closed":
                    state = "missing_contract"
                    analysis["status"] = "missing_final_candle"
            else:
                state = "expired" if item["expiry"] < track_session else "missing_contract"
            statuses.append(state)
            # JSON round-trip only normalises datetimes; no NaN is permitted.
            payload = psycopg2.extras.Json(json.loads(json.dumps(analysis, default=str, allow_nan=False)))
            with connection.cursor() as cursor:
                cursor.execute(
                    """UPDATE vanguard_watchlist_items SET
                       performance_audit=COALESCE(performance_audit,jsonb_build_object(
                           'captured_at',now(),'entry_mark',entry_mark,'latest_mark',latest_mark,
                           'return_pct',return_pct,'max_return_pct',max_return_pct,
                           'min_return_pct',min_return_pct,'close_return_pct',close_return_pct,
                           'basis','legacy calculation included entry-candle extremes')),
                       exit_analysis=%s, status=%s, updated_at=now()
                       WHERE id=%s""", (payload, state, item["id"]))
                if good:
                    final = state == "closed"
                    cursor.execute(
                        """UPDATE vanguard_watchlist_items SET
                           entry_ts=%s,entry_mark=%s,latest_ts=%s,latest_mark=%s,
                           return_pct=%s,max_return_pct=%s,min_return_pct=%s,
                           close_ts=%s,close_mark=%s,close_return_pct=%s
                           WHERE id=%s""",
                        (analysis["entry_ts"], analysis["entry_mark"], analysis["latest_ts"],
                         analysis["latest_mark"], analysis["return_pct"],
                         analysis["max_return_pct"], analysis["min_return_pct"],
                         analysis["latest_ts"] if final else None,
                         analysis["latest_mark"] if final else None,
                         analysis["return_pct"] if final else None, item["id"]))
            updated_items += 1
        done = session_ended and all(s in ("closed", "expired", "missing_contract") for s in statuses)
        with connection.cursor() as cursor:
            cursor.execute(
                """UPDATE vanguard_watchlist_runs SET track_session=%s,status=%s,
                   started_at=COALESCE(started_at,now()),
                   closed_at=CASE WHEN %s THEN COALESCE(closed_at,now()) ELSE closed_at END,
                   updated_at=now() WHERE source_session=%s""",
                (track_session, "closed" if done else "tracking", done, run["source_session"]))
        closed_runs += int(done)
    return {"runs_seen": len(runs), "items_updated": updated_items, "runs_closed": closed_runs}

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--freeze-latest", action="store_true")
    parser.add_argument("--freeze-session", type=date.fromisoformat)
    parser.add_argument("--track", action="store_true")
    parser.add_argument("--refresh-session", type=date.fromisoformat,
                        help="explicitly repair derived marks for one closed session, retaining an audit")
    parser.add_argument("--top-n", type=int, default=TOP_N)
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()
    if not (args.freeze_latest or args.freeze_session or args.track or args.refresh_session):
        parser.error("choose --freeze-latest, --freeze-session and/or --track")

    connection = psycopg2.connect(args.dsn)
    connection.autocommit = True
    try:
        source_session = args.freeze_session
        if args.freeze_latest:
            source_session = latest_prediction_session(connection)
            if source_session is None:
                print("no model predictions -- nothing to freeze")
        if source_session is not None:
            result = freeze_session(connection, source_session, args.top_n)
            print(f"watchlist freeze: {result}")
        if args.track or args.refresh_session:
            with connection:
                result = track_open_watchlists(connection, args.refresh_session)
            print(f"watchlist marks: {result}")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
