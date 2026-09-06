"""M9 -- Paper/Shadow Execution Engine.

Consumes tickets M6 already wrote (emitted=true) and turns them into a
complete, broker-free PAPER trade lifecycle: decision -> fill -> walk-
forward -> outcome -> capital rollup. Every fill/outcome price comes from
REAL market data already sitting in option_premium_candles (never a live
tick stream, never a broker call) -- see db/migrations/002_tickets_journal.sql's
own header comment for why decisions here are always 'AUTO_PAPER_TAKEN',
never 'TAKEN' (that word is reserved for an actual human click on a future
UI this module does not implement).

ORDER OF OPERATIONS within one run_cycle(), and why: stand-down flatten
runs FIRST, before filling new tickets or walking existing ones -- a
position opened earlier in the day, before a LATER loss tripped the daily
stand-down, must still be forced flat immediately; M7's risk_check already
stops NEW tickets during stand-down, so this module's own job is only to
close what M7 didn't get a chance to prevent. fill_pending_tickets runs
next (turns today's freshly emitted tickets into open positions), then
walk_open_positions (advances every open position, including ones just
filled this same cycle, against same-session bars), then
force_close_stale_positions (the T+3 defensive backstop for a position that
survived past its own fill-day EOD -- normally impossible if this engine
runs at least once after 15:10 IST daily, but a real safety net for an
operational gap), then update_paper_capital (rolls realized P&L from
today's closes into today's equity row).
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backtest.exit_simulator import (  # noqa: E402
    load_same_session_bars,
    r_multiple,
    walk_exit,
)
from fusion.m7_risk import load_risk_state  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
INITIAL_CAPITAL = 1_000_000.0
T3_MAX_SESSIONS = 3
IST = ZoneInfo("Asia/Kolkata")


def current_capital(connection, as_of_date: date, initial_capital: float = INITIAL_CAPITAL) -> float:
    """The evolving simulated equity M6/M7 should size against: yesterday's
    close, or initial_capital on day 1. Never today's own not-yet-final
    ending_equity (no lookahead into a day still in progress)."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT ending_equity FROM paper_capital_daily WHERE dt < %(d)s AND ending_equity IS NOT NULL "
            "ORDER BY dt DESC LIMIT 1",
            {"d": as_of_date},
        )
        row = cursor.fetchone()
    return float(row[0]) if row else initial_capital


def apply_standdown_flatten(connection, as_of_ts: datetime, capital: float) -> list[int]:
    state = load_risk_state(connection, as_of_ts, capital)
    if not state.stand_down:
        return []
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT t.id, t.symbol, t.strike, t.option_type, t.expiry,
                      f.fill_price, f.fill_ts, t.sizing_lots, t.lot_size, t.sizing_risk_rupees
               FROM tickets t
               JOIN fills f ON f.ticket_id = t.id
               JOIN outcomes o ON o.ticket_id = t.id
               WHERE o.closed = false AND t.ts <= %(as_of)s""",
            {"as_of": as_of_ts},
        )
        open_rows = cursor.fetchall()

        closed_ids = []
        for (ticket_id, symbol, strike, option_type, expiry, fill_price, fill_ts,
             lots, lot_size, risk_rupees) in open_rows:
            cursor.execute(
                """SELECT close, time FROM option_premium_candles
                   WHERE underlying = %(symbol)s AND strike = %(strike)s
                     AND option_type = %(option_type)s AND expiry = %(expiry)s
                     AND interval = '30minute'
                     AND time <= %(as_of)s AND close IS NOT NULL
                   ORDER BY time DESC LIMIT 1""",
                {"symbol": symbol, "strike": strike, "option_type": option_type,
                 "expiry": expiry, "as_of": as_of_ts},
            )
            price_row = cursor.fetchone()
            exit_price = float(price_row[0]) if price_row else float(fill_price)
            exit_ts = price_row[1] if price_row else as_of_ts
            _close_outcome(cursor, ticket_id, fill_price=float(fill_price), exit_price=exit_price,
                           exit_ts=exit_ts, exit_reason="daily_standdown_flatten",
                           lots=lots, lot_size=lot_size, risk_rupees=risk_rupees,
                           holding_bars=None)
            closed_ids.append(ticket_id)
    return closed_ids


def fill_pending_tickets(connection, as_of_ts: datetime) -> list[int]:
    """fill_ts is the ticket's OWN ts (when M6 actually generated it), never
    as_of_ts (when this cycle happens to run). If M9 lags M6 -- a catch-up
    run after a gap, or simply running less often than every timing bar --
    stamping the fill at as_of_ts would backdate it past every bar that
    should be walked, since load_same_session_bars only looks strictly
    AFTER fill_ts. The paper fill must reflect when the trade was actually
    decided, not when this process happened to notice it."""
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT t.id, t.ts, t.entry_zone_low, t.entry_zone_high
               FROM tickets t
               LEFT JOIN decisions d ON d.ticket_id = t.id
               WHERE t.emitted = true AND t.ts <= %(as_of)s AND d.ticket_id IS NULL""",
            {"as_of": as_of_ts},
        )
        pending = cursor.fetchall()

        filled_ids = []
        for ticket_id, ticket_ts, entry_zone_low, entry_zone_high in pending:
            entry_premium = round((float(entry_zone_low) + float(entry_zone_high)) / 2.0, 4)
            cursor.execute(
                "INSERT INTO decisions (ticket_id, decision, notes) VALUES (%s, 'AUTO_PAPER_TAKEN', '')",
                (ticket_id,),
            )
            cursor.execute(
                """INSERT INTO fills (ticket_id, fill_price, fill_ts, fill_method)
                   VALUES (%s, %s, %s, 'simulated_at_ticket_premium')""",
                (ticket_id, entry_premium, ticket_ts),
            )
            cursor.execute(
                "INSERT INTO outcomes (ticket_id, closed) VALUES (%s, false)",
                (ticket_id,),
            )
            filled_ids.append(ticket_id)
    return filled_ids


def _close_outcome(cursor, ticket_id, *, fill_price, exit_price, exit_ts, exit_reason,
                    lots, lot_size, risk_rupees, holding_bars):
    pnl_rupees = None
    if lots is not None and lot_size is not None:
        pnl_rupees = round((exit_price - fill_price) * lots * lot_size, 2)
    # R comes from the SHARED definition, not from pnl/risk_rupees. Since
    # migration 006 those two are deliberately different denominators
    # (risk_rupees is risk-at-stop; R is a return on premium), and deriving R
    # here from whatever happens to be in sizing_risk_rupees made M9's R
    # silently depend on a sizing field, so any future change to how M7
    # records risk would have redefined R for the journal without touching a
    # line of M10. See exit_simulator.r_multiple.
    r_value = r_multiple(float(fill_price), float(exit_price))
    cursor.execute(
        """UPDATE outcomes SET exit_price=%(exit_price)s, exit_ts=%(exit_ts)s,
               exit_reason=%(exit_reason)s, pnl_rupees=%(pnl_rupees)s, r_multiple=%(r_multiple)s,
               holding_bars=%(holding_bars)s, closed=true, updated_at=now()
           WHERE ticket_id=%(ticket_id)s""",
        {"exit_price": exit_price, "exit_ts": exit_ts, "exit_reason": exit_reason,
         "pnl_rupees": pnl_rupees, "r_multiple": r_value, "holding_bars": holding_bars,
         "ticket_id": ticket_id},
    )


def walk_open_positions(connection, as_of_ts: datetime) -> list[int]:
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT t.id, t.symbol, t.strike, t.option_type, t.expiry,
                      f.fill_price, f.fill_ts, t.sizing_lots, t.lot_size, t.sizing_risk_rupees
               FROM tickets t
               JOIN fills f ON f.ticket_id = t.id
               JOIN outcomes o ON o.ticket_id = t.id
               WHERE o.closed = false AND f.fill_ts <= %(as_of)s""",
            {"as_of": as_of_ts},
        )
        open_rows = cursor.fetchall()

        closed_ids = []
        for (ticket_id, symbol, strike, option_type, expiry, fill_price, fill_ts,
             lots, lot_size, risk_rupees) in open_rows:
            bars = load_same_session_bars(cursor, symbol, strike, option_type, expiry, fill_ts, as_of_ts)
            result = walk_exit(float(fill_price), fill_ts, bars)
            if result is None:
                continue
            _close_outcome(cursor, ticket_id, fill_price=float(fill_price), exit_price=result.exit_price,
                           exit_ts=result.exit_ts, exit_reason=result.exit_reason,
                           lots=lots, lot_size=lot_size, risk_rupees=risk_rupees,
                           holding_bars=result.holding_bars)
            closed_ids.append(ticket_id)
    return closed_ids


def force_close_stale_positions(connection, as_of_ts: datetime) -> list[int]:
    """Defensive T+3 backstop: a position should always resolve via
    walk_open_positions' own time_stop_eod on its fill day. If one is still
    open T3_MAX_SESSIONS calendar days later, this engine was not run for a
    stretch (an operational gap, not a market outcome) -- force it flat at
    the last available print rather than let it sit open indefinitely."""
    cutoff = as_of_ts - timedelta(days=T3_MAX_SESSIONS)
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT t.id, t.symbol, t.strike, t.option_type, t.expiry,
                      f.fill_price, t.sizing_lots, t.lot_size, t.sizing_risk_rupees
               FROM tickets t
               JOIN fills f ON f.ticket_id = t.id
               JOIN outcomes o ON o.ticket_id = t.id
               WHERE o.closed = false AND f.fill_ts <= %(cutoff)s""",
            {"cutoff": cutoff},
        )
        stale_rows = cursor.fetchall()

        closed_ids = []
        for (ticket_id, symbol, strike, option_type, expiry, fill_price,
             lots, lot_size, risk_rupees) in stale_rows:
            cursor.execute(
                """SELECT close, time FROM option_premium_candles
                   WHERE underlying = %(symbol)s AND strike = %(strike)s
                     AND option_type = %(option_type)s AND expiry = %(expiry)s
                     AND interval = '30minute'
                     AND time <= %(as_of)s AND close IS NOT NULL
                   ORDER BY time DESC LIMIT 1""",
                {"symbol": symbol, "strike": strike, "option_type": option_type,
                 "expiry": expiry, "as_of": as_of_ts},
            )
            price_row = cursor.fetchone()
            exit_price = float(price_row[0]) if price_row else float(fill_price)
            exit_ts = price_row[1] if price_row else as_of_ts
            _close_outcome(cursor, ticket_id, fill_price=float(fill_price), exit_price=exit_price,
                           exit_ts=exit_ts, exit_reason="time_stop_t3",
                           lots=lots, lot_size=lot_size, risk_rupees=risk_rupees,
                           holding_bars=None)
            closed_ids.append(ticket_id)
    return closed_ids


def update_paper_capital(connection, as_of_date: date, initial_capital: float = INITIAL_CAPITAL) -> None:
    starting_equity = current_capital(connection, as_of_date, initial_capital)
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT COALESCE(SUM(o.pnl_rupees), 0) FROM outcomes o
               WHERE o.closed AND o.exit_ts AT TIME ZONE 'Asia/Kolkata' >= %(day)s
                 AND o.exit_ts AT TIME ZONE 'Asia/Kolkata' < %(next_day)s""",
            {"day": as_of_date, "next_day": as_of_date + timedelta(days=1)},
        )
        (realized_pnl,) = cursor.fetchone()
        ending_equity = starting_equity + float(realized_pnl)
        cursor.execute(
            """INSERT INTO paper_capital_daily (dt, starting_equity, ending_equity, realized_pnl, updated_at)
               VALUES (%(dt)s, %(starting_equity)s, %(ending_equity)s, %(realized_pnl)s, now())
               ON CONFLICT (dt) DO UPDATE SET
                   -- starting_equity must refresh too: recomputing a day after
                   -- an EARLIER day's P&L changed would otherwise leave a row
                   -- where ending_equity != starting_equity + realized_pnl.
                   starting_equity = EXCLUDED.starting_equity,
                   ending_equity = EXCLUDED.ending_equity,
                   realized_pnl = EXCLUDED.realized_pnl,
                   updated_at = now()""",
            {"dt": as_of_date, "starting_equity": starting_equity,
             "ending_equity": ending_equity, "realized_pnl": float(realized_pnl)},
        )


def _affected_capital_dates(connection, ticket_ids: list[int], as_of_date: date) -> list[date]:
    """Every IST session whose realized P&L this cycle actually changed.

    A cycle does not only close positions dated today. walk_open_positions
    resolves a position against the bars of ITS OWN fill session, and the T+3
    sweep closes positions that are days old -- so a single run can write
    exit_ts values on several different dates. Rolling up only `as_of_date`
    left that P&L permanently invisible to the equity curve, because
    update_paper_capital is the only thing that ever writes
    paper_capital_daily and it was never asked about those days again
    (confirmed by adversarial review, 2026-08-27).
    """
    days = {as_of_date}
    if ticket_ids:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT DISTINCT date(exit_ts AT TIME ZONE 'Asia/Kolkata')
                   FROM outcomes WHERE ticket_id = ANY(%(ids)s) AND exit_ts IS NOT NULL""",
                {"ids": ticket_ids},
            )
            days.update(row[0] for row in cursor.fetchall())
    return sorted(days)


def run_cycle(connection, as_of_ts: datetime, initial_capital: float = INITIAL_CAPITAL) -> dict:
    capital = current_capital(connection, as_of_ts.date(), initial_capital)
    flattened = apply_standdown_flatten(connection, as_of_ts, capital)
    filled = fill_pending_tickets(connection, as_of_ts)
    walked_closed = walk_open_positions(connection, as_of_ts)
    stale_closed = force_close_stale_positions(connection, as_of_ts)

    # Oldest first: each day's starting_equity chains off the prior day's
    # ending_equity (current_capital reads dt < the day being written), so a
    # later day must not be recomputed before the earlier one it depends on.
    closed_now = list(flattened) + list(walked_closed) + list(stale_closed)
    rolled = _affected_capital_dates(connection, closed_now, as_of_ts.date())
    for day in rolled:
        update_paper_capital(connection, day, initial_capital)

    return {
        "capital_used": capital,
        "standdown_flattened": flattened,
        "newly_filled": filled,
        "closed_by_walk": walked_closed,
        "closed_stale_t3": stale_closed,
        "capital_dates_rolled": rolled,
    }


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--ts", type=datetime.fromisoformat, default=None,
                        help="ISO timestamp to evaluate; default = now (IST)")
    parser.add_argument("--capital", type=float, default=INITIAL_CAPITAL)
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    connection = psycopg2.connect(args.dsn)
    connection.autocommit = True
    try:
        as_of_ts = args.ts or datetime.now(tz=IST)
        result = run_cycle(connection, as_of_ts, args.capital)
        print(f"M9 run_cycle @ {as_of_ts.isoformat()}")
        for k, v in result.items():
            print(f"  {k}: {v}")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
