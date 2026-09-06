"""Paper-trade the two validated MP edges. Idempotent; runs live and at EOD.

THE TWO STRATEGIES, exactly as validated on 2026-08-28 and nothing more:

  gap_overnight   sig_strong_close (close above the value area, close_pos in
                  [0.70, 0.90]) -> LONG at that session's close, exit at the
                  NEXT session's 09:15 open. The edge is the gap; it is spent
                  by 09:15, so nothing is held past the open. Validated:
                  BANKNIFTY 5y +0.175%/night, 71% win, t+3.92, every year
                  positive; futures transfer consistent (+0.174%, n=34).
  oversold_mtf    sig_oversold_mtf (close below the day's AND prior week's AND
                  prior month's value) -> LONG at the close, exit at the close
                  of the 4th session after. NO STOP: the research measured that
                  a tight stop costs about half this edge, because the path
                  dips before it bounces. Replicated on NIFTY / BANKNIFTY /
                  bank stocks (lifts 1.24 / 1.70 / 1.44).

UNIVERSE: the researched names only -- NIFTY, BANKNIFTY, and the 16 bank
stocks. features_mp flags exist for ~225 names; trading the rest would be
extrapolation, and the point of a paper phase is to test the finding, not to
dilute it.

MECHANICS. One idempotent pass (--run):
  1. settle gap trades whose next-session 09:15 open now exists
  2. settle oversold trades whose 4th-session close now exists
  3. open trades for the newest features_mp session not yet traded
Safe to call from every live bar and from EOD: entries key on
(strategy, underlying, signal_dt) UNIQUE, exits only fill once. Prices come
from index_futures_candles for the indices (the tradeable print; roll sessions
excluded from entry) and underlying_spot_candles for stocks.

    python vanguard/paper/mp_edges.py --run
    python vanguard/paper/mp_edges.py --status
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"

BANKS = ("HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK", "INDUSINDBK",
         "BANKBARODA", "PNB", "CANBK", "UNIONBANK", "FEDERALBNK", "IDFCFIRSTB",
         "AUBANK", "RBLBANK", "YESBANK", "BANKINDIA")
INDICES = ("NIFTY", "BANKNIFTY")
UNIVERSE = INDICES + BANKS

NOTIONAL = 500_000.0
COST_BP = {"futures": 4.0, "spot_proxy": 5.0}   # stocks assume single-stock futures
MAX_OVERSOLD_PER_NIGHT = 10
HOLD_SESSIONS = 4


def _session_price(cur, underlying: str, dt, *, which: str):
    """(price, source): close/open of a session; futures preferred for indices.

    which='close' -> last 30m bar's close; which='open' -> the 09:15 bar's open."""
    order = "DESC" if which == "close" else "ASC"
    col = "close" if which == "close" else "open"
    tables = (["index_futures_candles", "underlying_spot_candles"]
              if underlying in INDICES else ["underlying_spot_candles"])
    for table in tables:
        cur.execute(
            f"""SELECT {col} FROM {table}
                WHERE underlying = %s AND interval = '30minute'
                  AND date(time AT TIME ZONE 'Asia/Kolkata') = %s
                  AND (time AT TIME ZONE 'Asia/Kolkata')::time
                      BETWEEN '09:15' AND '15:15'
                ORDER BY time {order} LIMIT 1""",
            (underlying, dt))
        row = cur.fetchone()
        if row and row[0] is not None:
            return float(row[0]), ("futures" if table == "index_futures_candles"
                                   else "spot_proxy")
    return None, None


def _sessions_after(cur, underlying: str, dt, n: int) -> list:
    """The next n session dates with data for this name, strictly after dt."""
    table = ("index_futures_candles" if underlying in INDICES
             else "underlying_spot_candles")
    cur.execute(
        f"""SELECT DISTINCT date(time AT TIME ZONE 'Asia/Kolkata') AS d
            FROM {table}
            WHERE underlying = %s AND interval = '30minute'
              AND date(time AT TIME ZONE 'Asia/Kolkata') > %s
            ORDER BY 1 LIMIT %s""",
        (underlying, dt, n))
    return [r[0] for r in cur.fetchall()]


def settle(connection) -> int:
    """Close whatever can be closed with the data that now exists."""
    closed = 0
    with connection.cursor() as cur:
        cur.execute("SELECT id, strategy, underlying, signal_dt, entry_px, "
                    "cost_bp FROM mp_paper_trades WHERE status = 'open'")
        for tid, strat, name, sig_dt, entry_px, cost_bp in cur.fetchall():
            entry_px, cost_bp = float(entry_px), float(cost_bp)
            if strat == "gap_overnight":
                nxt = _sessions_after(cur, name, sig_dt, 1)
                if not nxt:
                    continue
                px, _ = _session_price(cur, name, nxt[0], which="open")
                reason, exit_dt = "next_open", nxt[0]
            else:
                later = _sessions_after(cur, name, sig_dt, HOLD_SESSIONS)
                if len(later) < HOLD_SESSIONS:
                    continue
                px, _ = _session_price(cur, name, later[-1], which="close")
                reason, exit_dt = "h4_close", later[-1]
            if px is None:
                continue
            gross = (px / entry_px - 1.0) * 100.0
            net = gross - cost_bp / 100.0
            cur.execute(
                """UPDATE mp_paper_trades
                   SET exit_ts = %s, exit_px = %s, exit_reason = %s,
                       gross_ret_pct = %s, net_ret_pct = %s, status = 'closed'
                   WHERE id = %s AND status = 'open'""",
                (datetime.combine(exit_dt, datetime.min.time()), px, reason,
                 round(gross, 4), round(net, 4), tid))
            closed += cur.rowcount
    connection.commit()
    return closed


def enter(connection) -> int:
    """Open trades for the newest flagged features_mp session in the universe."""
    opened = 0
    with connection.cursor() as cur:
        cur.execute(
            """SELECT MAX(dt) FROM features_mp
               WHERE underlying = ANY(%s)
                 AND (sig_strong_close OR sig_oversold_mtf)""",
            (list(UNIVERSE),))
        latest = cur.fetchone()[0]
        if latest is None:
            return 0
        cur.execute(
            """SELECT underlying, sig_strong_close, sig_oversold_mtf,
                      exp_range_pct
               FROM features_mp
               WHERE dt = %s AND underlying = ANY(%s)
                 AND (sig_strong_close OR sig_oversold_mtf)""",
            (latest, list(UNIVERSE)))
        rows = cur.fetchall()
        oversold = sorted(
            [r for r in rows if r[2]],
            key=lambda r: -(float(r[3]) if r[3] is not None else 0.0),
        )[:MAX_OVERSOLD_PER_NIGHT]
        plans = ([(r[0], "gap_overnight") for r in rows if r[1]]
                 + [(r[0], "oversold_mtf") for r in oversold])
        for name, strat in plans:
            px, src = _session_price(cur, name, latest, which="close")
            if px is None:
                continue
            cur.execute(
                """INSERT INTO mp_paper_trades
                   (strategy, underlying, signal_dt, entry_px, entry_src,
                    notional, cost_bp)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (strategy, underlying, signal_dt) DO NOTHING""",
                (strat, name, latest, px, src, NOTIONAL, COST_BP[src]))
            opened += cur.rowcount
    connection.commit()
    return opened


def status(connection) -> None:
    q = """SELECT strategy, status, COUNT(*) n,
                  ROUND(AVG(net_ret_pct), 3) AS avg_net,
                  ROUND(SUM(net_ret_pct * notional / 100.0), 0) AS pnl_rs
           FROM mp_paper_trades GROUP BY 1, 2 ORDER BY 1, 2"""
    print(pd.read_sql(q, connection).to_string(index=False))
    open_q = """SELECT strategy, underlying, signal_dt, entry_px, entry_src
                FROM mp_paper_trades WHERE status = 'open'
                ORDER BY strategy, underlying"""
    frame = pd.read_sql(open_q, connection)
    if len(frame):
        print("\nOPEN:")
        print(frame.to_string(index=False))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true",
                        help="settle everything settleable, then enter the "
                             "newest flagged session (idempotent)")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL",
                                                        DEFAULT_DSN))
    args = parser.parse_args()
    connection = psycopg2.connect(args.dsn)
    try:
        if args.run:
            closed = settle(connection)
            opened = enter(connection)
            print(f"mp_edges: settled {closed}, opened {opened}")
        if args.status or not args.run:
            status(connection)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
