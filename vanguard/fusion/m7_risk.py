"""M7 -- Risk & Sizing. Veto authority over every ticket M6 wants to emit.

Defaults are the spec's own (Section 3, M7), implemented literally:
  - risk per trade 0.75% of capital at stop; hard cap 1.0%
  - long-premium: premium-at-risk counts FULLY (an option can gap/decay to
    zero regardless of where its stop sits -- a stop-distance-only risk
    figure understates what is actually at risk); max premium/trade 1.5%
  - sizing: 0.25x fractional Kelly from a rolling 60-trade edge estimate;
    fixed-fractional (risk_pct straight to capital, no Kelly adjustment)
    until 60 closed trades exist to estimate an edge from
  - max concurrent 3; max per sector20 = 2; portfolio heat <= 2.5%
  - daily loss stop -2.0% -> STAND-DOWN, no new tickets until next session
  - weekly loss stop -4.0% -> stand-down + mandatory review flag
  - event guard: no fresh tickets 1 session before a symbol's results date

Scope decision (stated once, honestly): every position sized here is an
INTRADAY thesis with an EOD time stop. The spec's "positional, T+3 unless
target-1 hit" variant is a real, separate instrument class the spec
describes but this build does not yet distinguish -- M6 does not yet emit a
positional-vs-intraday flag, so there is nothing for this module to key a
T+3 rule on. Documented here rather than silently defaulted.

This module NEVER places an order and NEVER calls a broker API -- it only
computes a position size and a yes/no gate from data already in Postgres
(open `fills`/`outcomes` rows, `paper_capital_daily`, `sector_taxonomy`,
`results_calendar`). It is deliberately pure/query-only so M6 can call
`risk_check()` synchronously while building a ticket.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import psycopg2
import psycopg2.extras

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"

RISK_PER_TRADE_PCT = 0.75          # of capital, at stop
RISK_PER_TRADE_HARD_CAP_PCT = 1.00
MAX_PREMIUM_PER_TRADE_PCT = 1.50   # long-premium: the ENTIRE premium, not stop-distance
KELLY_FRACTION = 0.25
KELLY_MIN_TRADES = 60
MAX_CONCURRENT_POSITIONS = 3
MAX_POSITIONS_PER_SECTOR20 = 2
MAX_PORTFOLIO_HEAT_PCT = 2.5
DAILY_LOSS_STOP_PCT = -2.0
WEEKLY_LOSS_STOP_PCT = -4.0
DEFAULT_CAPITAL = 1_000_000.0       # configurable; see main()'s --capital


@dataclass
class RiskState:
    """Everything risk_check() needs, fetched once per M6 run (not once per
    candidate) so a 20-candidate session issues a handful of queries, not
    hundreds."""
    capital: float
    daily_pnl_pct: float
    weekly_pnl_pct: float
    open_positions: list[dict]           # [{ticket_id, symbol, sector20, risk_rupees}, ...]
    kelly_edge: dict | None              # {p, b, n} from the last 60 CLOSED trades, or None if < 60
    stand_down: bool = field(init=False)
    weekly_review_flag: bool = field(init=False)
    portfolio_heat_pct: float = field(init=False)

    def __post_init__(self) -> None:
        self.stand_down = self.daily_pnl_pct <= DAILY_LOSS_STOP_PCT
        self.weekly_review_flag = self.weekly_pnl_pct <= WEEKLY_LOSS_STOP_PCT
        heat_rupees = sum(p["risk_rupees"] for p in self.open_positions)
        self.portfolio_heat_pct = 100.0 * heat_rupees / self.capital if self.capital else 0.0


def load_risk_state(connection, as_of: datetime, capital: float) -> RiskState:
    """Risk state AS OF `as_of` -- every query is bounded by it.

    The bound is not cosmetic. M8's backtest replays historical bars through
    this exact function, so an unbounded query answers "what is the book doing
    NOW" for a decision being made in July. An adversarial review confirmed
    this on 2026-08-27: the open-positions and Kelly-sample queries had no time
    predicate whatsoever, so every historical bar in a replay was sized against
    the present-day book and the present-day edge estimate -- lookahead that
    silently invalidates the backtest it feeds. The daily/weekly P&L queries
    were bounded to the right DAY but not to the right INSTANT, so a bar at
    10:00 could see a loss that only closed at 14:30 and stand itself down for
    a drawdown that had not happened yet.
    """
    today = as_of.date()
    week_start = today - timedelta(days=today.weekday())

    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT COALESCE(SUM(o.pnl_rupees), 0)
               FROM outcomes o JOIN tickets t ON t.id = o.ticket_id
               WHERE o.closed AND o.exit_ts::date = %(today)s
                 AND o.exit_ts <= %(as_of)s""",
            {"today": today, "as_of": as_of},
        )
        (daily_pnl,) = cursor.fetchone()
        cursor.execute(
            """SELECT COALESCE(SUM(o.pnl_rupees), 0)
               FROM outcomes o
               WHERE o.closed AND o.exit_ts::date >= %(week_start)s
                 AND o.exit_ts::date <= %(today)s
                 AND o.exit_ts <= %(as_of)s""",
            {"week_start": week_start, "today": today, "as_of": as_of},
        )
        (weekly_pnl,) = cursor.fetchone()

        # Open AS OF as_of: filled at or before it, and either never closed or
        # closed strictly after it. A position closed at 11:00 must still count
        # as open to a decision being made at 10:00.
        cursor.execute(
            """SELECT t.id, t.symbol, st.sector20,
                      COALESCE(t.sizing_risk_rupees, 0) AS risk_rupees
               FROM fills f
               JOIN tickets t ON t.id = f.ticket_id
               LEFT JOIN outcomes o ON o.ticket_id = t.id
               LEFT JOIN sector_taxonomy st ON st.symbol = t.symbol
               WHERE f.fill_ts <= %(as_of)s
                 AND (o.ticket_id IS NULL OR NOT o.closed
                      OR o.exit_ts IS NULL OR o.exit_ts > %(as_of)s)""",
            {"as_of": as_of},
        )
        open_positions = [
            {"ticket_id": r[0], "symbol": r[1], "sector20": r[2], "risk_rupees": float(r[3])}
            for r in cursor.fetchall()
        ]

        cursor.execute(
            """SELECT o.r_multiple FROM outcomes o
               WHERE o.closed AND o.r_multiple IS NOT NULL
                 AND o.exit_ts <= %(as_of)s
               ORDER BY o.exit_ts DESC LIMIT %(n)s""",
            {"n": KELLY_MIN_TRADES, "as_of": as_of},
        )
        r_multiples = [float(row[0]) for row in cursor.fetchall()]

    kelly_edge = None
    if len(r_multiples) >= KELLY_MIN_TRADES:
        wins = [r for r in r_multiples if r > 0]
        losses = [-r for r in r_multiples if r <= 0]
        if wins and losses:
            p = len(wins) / len(r_multiples)
            avg_win = sum(wins) / len(wins)
            avg_loss = sum(losses) / len(losses)
            b = avg_win / avg_loss if avg_loss else None
            if b:
                kelly_edge = {"p": p, "b": b, "n": len(r_multiples)}

    return RiskState(
        capital=capital,
        daily_pnl_pct=100.0 * float(daily_pnl) / capital if capital else 0.0,
        weekly_pnl_pct=100.0 * float(weekly_pnl) / capital if capital else 0.0,
        open_positions=open_positions,
        kelly_edge=kelly_edge,
    )


def kelly_risk_pct(edge: dict | None) -> float:
    """0.25x Kelly fraction of capital to risk, or the fixed-fractional
    default (RISK_PER_TRADE_PCT) when there isn't yet a 60-trade edge
    estimate. Kelly f* = p - (1-p)/b; negative f* (a measured negative
    edge) clamps to 0 -- Kelly sizing must never SIZE UP a losing system."""
    if edge is None:
        return RISK_PER_TRADE_PCT
    p, b = edge["p"], edge["b"]
    f_star = p - (1 - p) / b
    scaled = max(0.0, f_star) * KELLY_FRACTION * 100.0
    return min(scaled, RISK_PER_TRADE_HARD_CAP_PCT)


def event_guard_blocks(connection, symbol: str, as_of: date) -> str | None:
    """None if clear; else the reason string. 'One session before' means
    the day immediately preceding a known results_date -- results_calendar
    is populated (partially -- see M1's corporate_announcements module) from
    NSE's own already-published board-meeting schedule, never predicted.

    The query is `>=`, not `>`: found live, not in an offline test -- with
    `>`, `as_of == results_date` (a fresh ticket requested ON the results
    day itself) was silently excluded from the result set, so the guard
    never fired on the results day, only the day before it. If new
    positions must not be opened the session before results, they must not
    be opened on results day either -- the number may already be out and
    the read highly unstable either way.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT results_date FROM results_calendar
               WHERE symbol = %(symbol)s AND results_date >= %(as_of)s
               ORDER BY results_date ASC LIMIT 1""",
            {"symbol": symbol, "as_of": as_of},
        )
        row = cursor.fetchone()
    if row is None:
        return None
    results_date = row[0]
    # "1 session before" -- the guard fires the trading day immediately
    # preceding results_date, and on results_date itself. A precise
    # trading-calendar lookup is out of scope here; this uses the calendar
    # day as a conservative proxy (it can over-block across a weekend,
    # never under-block).
    if (results_date - as_of).days <= 1:
        return f"event guard: {symbol} reports on {results_date.isoformat()}"
    return None


@dataclass
class SizingResult:
    allowed: bool
    reason: str | None = None
    lots: int = 0
    notional: float = 0.0
    risk_rupees: float = 0.0
    method: str | None = None


def risk_check(state: RiskState, connection, *, symbol: str, sector20: str | None,
               entry_premium: float, stop_premium: float, lot_size: int, as_of: date) -> SizingResult:
    """The single veto/sizing entry point M6 calls per candidate, in gate
    order (cheapest/most decisive checks first)."""
    if state.stand_down:
        return SizingResult(False, f"STAND-DOWN: daily P&L {state.daily_pnl_pct:.2f}% <= {DAILY_LOSS_STOP_PCT}%")
    # The weekly stop was computed into RiskState from the start but never
    # consulted here, so a book could bleed past -4% for the week and keep
    # taking new risk as long as no single DAY breached -2% (confirmed by
    # adversarial review, 2026-08-27). Enforcing it is the whole point of
    # having computed it; a limit that is measured and then ignored is worse
    # than no limit, because the number sitting in the journal implies it was
    # being respected.
    if state.weekly_review_flag:
        return SizingResult(
            False,
            f"WEEKLY-STOP: weekly P&L {state.weekly_pnl_pct:.2f}% <= {WEEKLY_LOSS_STOP_PCT}% "
            "— no new risk until the week is reviewed",
        )
    if len(state.open_positions) >= MAX_CONCURRENT_POSITIONS:
        return SizingResult(False, f"max concurrent positions reached ({MAX_CONCURRENT_POSITIONS})")
    if sector20:
        same_sector = sum(1 for p in state.open_positions if p["sector20"] == sector20)
        if same_sector >= MAX_POSITIONS_PER_SECTOR20:
            return SizingResult(False, f"max positions in sector20={sector20} reached ({MAX_POSITIONS_PER_SECTOR20})")
    if state.portfolio_heat_pct >= MAX_PORTFOLIO_HEAT_PCT:
        return SizingResult(False, f"portfolio heat {state.portfolio_heat_pct:.2f}% >= {MAX_PORTFOLIO_HEAT_PCT}%")
    blocked = event_guard_blocks(connection, symbol, as_of)
    if blocked:
        return SizingResult(False, blocked)
    if entry_premium <= 0 or lot_size <= 0:
        return SizingResult(False, "invalid entry_premium or lot_size")
    if stop_premium >= entry_premium:
        return SizingResult(False, "stop_premium must be below entry_premium for a long option")

    risk_pct = kelly_risk_pct(state.kelly_edge)
    risk_budget_rupees = state.capital * risk_pct / 100.0
    # Doctrine: "premium-at-risk counts fully" -- the binding risk figure
    # for sizing is the FULL premium paid, not the (smaller) distance to the
    # stop, because an option can gap/decay to zero past any stop.
    per_lot_premium_risk = entry_premium * lot_size
    lots_by_risk_budget = int(risk_budget_rupees // per_lot_premium_risk) if per_lot_premium_risk > 0 else 0
    max_premium_rupees = state.capital * MAX_PREMIUM_PER_TRADE_PCT / 100.0
    lots_by_premium_cap = int(max_premium_rupees // per_lot_premium_risk) if per_lot_premium_risk > 0 else 0
    lots = max(0, min(lots_by_risk_budget, lots_by_premium_cap))
    if lots < 1:
        return SizingResult(False, "position would round to 0 lots under the risk/premium caps")

    notional = lots * lot_size * entry_premium
    risk_rupees = lots * per_lot_premium_risk    # full premium at risk, per doctrine
    remaining_heat_pct = MAX_PORTFOLIO_HEAT_PCT - state.portfolio_heat_pct
    if 100.0 * risk_rupees / state.capital > remaining_heat_pct:
        capped_lots = int((remaining_heat_pct / 100.0 * state.capital) // per_lot_premium_risk)
        if capped_lots < 1:
            return SizingResult(False, f"portfolio heat headroom ({remaining_heat_pct:.2f}%) too small for 1 lot")
        lots = capped_lots
        notional = lots * lot_size * entry_premium
        risk_rupees = lots * per_lot_premium_risk

    method = f"kelly_{KELLY_FRACTION}x" if state.kelly_edge else "fixed_fractional"
    return SizingResult(True, None, lots, notional, risk_rupees, method)


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    connection = psycopg2.connect(args.dsn)
    try:
        state = load_risk_state(connection, datetime.now(timezone.utc), args.capital)
        print(f"capital={args.capital:,.0f}  daily_pnl={state.daily_pnl_pct:+.3f}%  "
              f"weekly_pnl={state.weekly_pnl_pct:+.3f}%  stand_down={state.stand_down}  "
              f"weekly_review={state.weekly_review_flag}")
        print(f"open positions: {len(state.open_positions)}  portfolio_heat={state.portfolio_heat_pct:.3f}%")
        print(f"kelly edge: {state.kelly_edge}")
        risk_pct = kelly_risk_pct(state.kelly_edge)
        print(f"risk_pct this session: {risk_pct:.4f}%  "
              f"({'kelly-derived' if state.kelly_edge else 'fixed-fractional fallback (< 60 closed trades)'})")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
