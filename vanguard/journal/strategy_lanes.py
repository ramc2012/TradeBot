"""Maintain independent journals for Vanguard's three paper/shadow strategies."""
from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from model.swing_marks import summarize_path

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
IST = ZoneInfo("Asia/Kolkata")


def sync_mp_journals(connection) -> int:
    """Mirror each durable MP trade into its strategy-specific journal view.

    UNITS: ``vanguard_strategy_journal.realized_return_pct`` is a FRACTION
    (0.0207 = +2.07%), matching what ``track_swing`` writes and what the desk
    renders (it multiplies by 100).  ``mp_paper_trades.net_ret_pct`` is stored
    in PERCENT, so it is divided here.  Mirroring it raw made the Overnight
    journal claim +207.19% on a 388.80 -> 397.05 trade.  The percent-unit
    ``gross_ret_pct``/``net_ret_pct`` inside ``payload`` stay verbatim: that
    blob is a faithful copy of the source row, not a rendered field.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO vanguard_strategy_journal
               (strategy,event_key,event_type,source_session,event_ts,symbol,status,
                entry_mark,latest_mark,realized_return_pct,payload)
               SELECT strategy,'mp:'||id,'paper_trade',signal_dt,
                      COALESCE(exit_ts,created_at),underlying,status,entry_px,
                      COALESCE(exit_px,entry_px),net_ret_pct/100.0,
                      jsonb_build_object('trade_id',id,'side',side,'entry_src',entry_src,
                        'notional',notional,'cost_bp',cost_bp,'exit_due_dt',exit_due_dt,
                        'exit_ts',exit_ts,'exit_reason',exit_reason,'gross_ret_pct',gross_ret_pct)
               FROM mp_paper_trades
               WHERE strategy IN ('gap_overnight','oversold_mtf')
               ON CONFLICT (strategy,event_key) DO UPDATE SET
                 event_ts=EXCLUDED.event_ts,status=EXCLUDED.status,
                 latest_mark=EXCLUDED.latest_mark,
                 realized_return_pct=EXCLUDED.realized_return_pct,
                 payload=EXCLUDED.payload,updated_at=now()"""
        )
        return cursor.rowcount


def mark_open_mp_journals(connection) -> int:
    """Mark the MP lanes' OPEN journal rows to the latest 30m spot close.

    Without this the desk showed every open overnight/oversold position frozen
    at its entry with a blank return, because the mirror can only copy what the
    settled book holds.  ``mp_paper_trades`` is NEVER written here -- that book
    settles on its own schedule (next open / 4th close); this is the journal's
    running mark, stamped in the payload so it cannot be read as a settled exit.

    Cost matches paper/mp_edges.py (round-trip ``cost_bp``); the result is a
    FRACTION, the unit the whole journal column uses.

    Both ``time`` bounds are passed as explicit timestamps.  ``now()-interval
    '3 days'`` reads naturally but is only STABLE, so the planner cannot exclude
    chunks with it: that form planned for 6.8s against this 1300-chunk
    hypertable to execute in 0.26s, once a minute.  Bounded this way it is 2ms.
    """
    now = datetime.now(UTC)
    with connection.cursor() as cursor:
        cursor.execute(
            """WITH open_rows AS (
                   SELECT id,symbol,entry_mark,payload
                   FROM vanguard_strategy_journal
                   WHERE strategy IN ('gap_overnight','oversold_mtf')
                     AND status='open' AND entry_mark>0
               ), marks AS (
                   SELECT DISTINCT ON (o.symbol) o.symbol,s.time,s.close
                   FROM open_rows o
                   JOIN underlying_spot_candles s ON s.underlying=o.symbol
                    AND s.interval='30minute'
                    AND s.time>=%(lo)s AND s.time<%(hi)s
                   ORDER BY o.symbol,s.time DESC,
                            CASE s.source WHEN 'upstox_spot' THEN 0
                                          WHEN 'upstox_sweep' THEN 1
                                          WHEN 'upstox' THEN 2
                                          WHEN 'fyers_spot' THEN 3 ELSE 9 END,
                            s.synced_at DESC
               )
               UPDATE vanguard_strategy_journal j SET
                 latest_mark=m.close,
                 realized_return_pct=(m.close/j.entry_mark-1.0)
                     -(COALESCE((j.payload->>'cost_bp')::numeric,0)/10000.0),
                 payload=j.payload||jsonb_build_object(
                     'mark_basis','running_30m_spot_close','mark_ts',m.time),
                 updated_at=now()
               FROM marks m, open_rows o
               WHERE o.id=j.id AND m.symbol=j.symbol""",
            {"lo": now - timedelta(days=3), "hi": now + timedelta(days=1)},
        )
        return cursor.rowcount


def _future_sessions(connection, source_session):
    """Use NSE sessions, even if the archive is missing an entire day."""
    from model.market_calendar import is_session
    sessions = []
    day = source_session
    while len(sessions) < 2:
        day += timedelta(days=1)
        if is_session(day):
            sessions.append(day)
    return sessions


def track_swing(connection) -> dict[str, int]:
    with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """SELECT i.*,r.decision_at,r.generated_at,r.is_replay
               FROM vanguard_swing_watchlist_items i
               JOIN vanguard_swing_watchlist_runs r USING(source_session)
               WHERE i.status IN ('awaiting_entry','tracking')
               ORDER BY i.source_session,i.rank"""
        )
        items = [dict(row) for row in cursor.fetchall()]
    updated = closed = 0
    now = datetime.now(IST)
    for item in items:
        sessions = _future_sessions(connection, item["source_session"])
        horizon = int(item["horizon_sessions"])
        final_session = sessions[horizon-1] if len(sessions) >= horizon else None
        with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(
                """SELECT * FROM (
                       SELECT DISTINCT ON (o.interval,o.time)
                              o.interval,o.time,o.open,o.high,o.low,o.close
                       FROM option_premium_candles o
                       WHERE o.underlying=%(symbol)s AND o.option_type=%(option_type)s
                         AND o.expiry=%(expiry)s AND o.strike=%(strike)s
                         AND o.interval IN ('30minute','3minute')
                         AND o.time>=((%(source_session)s::date+time '14:45') AT TIME ZONE 'Asia/Kolkata')
                         AND (o.interval='30minute' OR
                              o.time>=((%(source_session)s::date+time '15:15') AT TIME ZONE 'Asia/Kolkata'))
                         AND (%(final_session)s::date IS NULL OR
                              o.time<=((%(final_session)s::date+time '14:45') AT TIME ZONE 'Asia/Kolkata'))
                       ORDER BY o.interval,o.time,(o.source='upstox') DESC,o.source,o.synced_at DESC
                   ) marks ORDER BY time,CASE interval WHEN '30minute' THEN 0 ELSE 1 END""",
                {**item, "final_session": final_session},
            )
            path = [dict(row) for row in cursor.fetchall()]
        if not path:
            continue
        result = summarize_path(item, path, sessions, now)
        if result is None:
            continue
        entry_mark = result["entry_mark"]
        latest_mark = result["latest_mark"]
        current_return = result["return_pct"]
        day_marks = result["day_marks"]
        status = result["status"]
        is_closed = status == "closed"
        with connection.cursor() as cursor:
            cursor.execute(
                """UPDATE vanguard_swing_watchlist_items SET
                   entry_ts=%s,entry_mark=%s,latest_ts=%s,latest_mark=%s,return_pct=%s,
                   max_return_pct=%s,min_return_pct=%s,
                   day_1_ts=%s,day_1_mark=%s,day_1_return_pct=%s,
                   day_2_ts=%s,day_2_mark=%s,day_2_return_pct=%s,status=%s,updated_at=now()
                   ,net_return_pct=%s WHERE id=%s""",
                (result["entry_ts"], entry_mark, result["latest_ts"], latest_mark, current_return,
                 result["max_return_pct"], result["min_return_pct"],
                 *(day_marks.get(1, (None, None, None))),
                 *(day_marks.get(2, (None, None, None))), status, result["net_return_pct"], item["id"]),
            )
            cursor.execute(
                """UPDATE vanguard_strategy_journal SET event_ts=%s,status=%s,
                   entry_mark=%s,latest_mark=%s,realized_return_pct=%s,
                   payload=payload||%s::jsonb,updated_at=now()
                   WHERE strategy='swing_1_2d' AND event_key=%s""",
                (result["latest_ts"], status, entry_mark, latest_mark, result["net_return_pct"],
                 json.dumps({"max_return_pct": result["max_return_pct"],
                             "min_return_pct": result["min_return_pct"],
                             "gross_return_pct": current_return,
                             "cost_pct": float(item["cost_pct"]),
                             "return_basis": "net_of_assumed_premium_cost",
                             "day_1_return_pct": day_marks.get(1, (None, None, None))[2],
                             "day_2_return_pct": day_marks.get(2, (None, None, None))[2]}),
                 f"{item['source_session']}:{item['rank']}"),
            )
        updated += 1
        closed += int(is_closed)
    with connection.cursor() as cursor:
        cursor.execute(
            """UPDATE vanguard_swing_watchlist_runs r SET
               status=CASE
                 WHEN NOT EXISTS (SELECT 1 FROM vanguard_swing_watchlist_items i
                                  WHERE i.source_session=r.source_session AND i.status<>'closed')
                   THEN 'closed'
                 WHEN EXISTS (SELECT 1 FROM vanguard_swing_watchlist_items i
                              WHERE i.source_session=r.source_session AND i.status='tracking')
                   THEN 'tracking' ELSE r.status END,
               entry_session=COALESCE(entry_session,(
                 SELECT min((entry_ts AT TIME ZONE 'Asia/Kolkata')::date)
                 FROM vanguard_swing_watchlist_items i WHERE i.source_session=r.source_session)),
               updated_at=now()
               WHERE r.status IN ('awaiting_entry','tracking')"""
        )
    return {"items_seen": len(items), "items_updated": updated, "items_closed": closed}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    parser.add_argument("--sync", action="store_true")
    parser.add_argument("--track-swing", action="store_true")
    args = parser.parse_args()
    if not (args.sync or args.track_swing):
        parser.error("choose --sync and/or --track-swing")
    connection = psycopg2.connect(args.dsn)
    try:
        with connection:
            result = {}
            if args.sync:
                result["journal_rows"] = sync_mp_journals(connection)
                result["open_marks"] = mark_open_mp_journals(connection)
            if args.track_swing:
                result["swing"] = track_swing(connection)
        print(result)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
