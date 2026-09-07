"""Trading-session arithmetic and expiry geometry for a 1-5 day swing hold.

This module exists because the first cut of the lane measured its holding
period in wall-clock 30-minute intervals. An overnight gap from 15:15 to 09:15
IST is 18 hours — 36 "bars" — so every position that survived one night tripped
its max-hold on the next session's first evaluation. The book proved it: 34
closed trades, average hold 0.262 days, 27 of them same-session. A lane cannot
hold for one to five trading days while counting time in bars.

Two distinct clocks are needed and they are NOT interchangeable:

  * TRADING SESSIONS drive the holding period, the exit ladder and the horizon
    a signal is expressed over. Five sessions is five opportunities to be right.
  * CALENDAR DAYS drive theta. An option decays over the weekend; five trading
    sessions is seven calendar days of decay, and charging five understates the
    cost of carry by about 40% on exactly the term that makes long premium hard.

The second half of the module picks the expiry. Measured on 2.7 years of clean
daily bars, a delta-0.40 call needs the index to move only 0.15 sigma in its
favour to break even at 25 DTE, but 0.43 sigma at 4 DTE — so at a 3-day hold
BANKNIFTY's monthly clears its breakeven 49.0% of the time while NIFTY's front
weekly manages 40.7%. Choosing the NEAREST expiry, which the first cut did,
systematically selects the worst geometry available.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

from core.trading_calendar import trading_calendar
from directional_options.index_paper.costs import CostModel, estimate_fill
from directional_options.vol.blackscholes import black76_price
from directional_options.vol.surface import SurfaceSlice

IST = timezone(timedelta(hours=5, minutes=30))

# SENSEX is a BSE product but the two exchanges keep the same holiday calendar;
# the configured NSE calendar is used for all three indices.
CALENDAR_EXCHANGE = "NSE"

TRADING_SESSIONS_PER_YEAR = 252.0
CALENDAR_DAYS_PER_YEAR = 365.0

# Cap on the forward search so a broken calendar cannot spin.
_MAX_SEARCH_DAYS = 400


def ist_session_date(ts: datetime) -> date:
    return ts.astimezone(IST).date()


def is_session(day: date) -> bool:
    return trading_calendar.has_exchange_session(CALENDAR_EXCHANGE, day)


def sessions_between(start: date, end: date) -> int:
    """Trading sessions strictly after `start` up to and including `end`.

    Entering on Friday and exiting on the following Monday is ONE session held,
    not three days.
    """
    if end <= start:
        return 0
    count, cursor = 0, start
    for _ in range(_MAX_SEARCH_DAYS):
        cursor += timedelta(days=1)
        if cursor > end:
            break
        if is_session(cursor):
            count += 1
    return count


def add_sessions(start: date, sessions: int) -> date:
    """The date `sessions` trading sessions after `start`."""
    if sessions <= 0:
        return start
    remaining, cursor = sessions, start
    for _ in range(_MAX_SEARCH_DAYS):
        cursor += timedelta(days=1)
        if is_session(cursor):
            remaining -= 1
            if remaining == 0:
                return cursor
    return cursor


def calendar_days_for_sessions(start: date, sessions: int) -> float:
    """Calendar days spanned by N trading sessions from `start` — for theta.

    Uses the real calendar rather than a 7/5 fudge, so a long weekend or a
    holiday cluster is charged at its true decay cost.
    """
    if sessions <= 0:
        return 0.0
    return float((add_sessions(start, sessions) - start).days)


def sessions_to_expiry(from_day: date, expiry: date) -> int:
    return sessions_between(from_day, expiry)


@dataclass
class HoldPlan:
    """How long the lane intends to hold, reconciled against the expiry."""

    requested_sessions: int
    planned_sessions: int
    calendar_days: float
    exit_by_date: date
    sessions_to_expiry: int
    truncated_by_expiry: bool
    reason: str = ""

    @property
    def feasible(self) -> bool:
        return self.planned_sessions >= 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_sessions": self.requested_sessions,
            "planned_sessions": self.planned_sessions,
            "calendar_days": self.calendar_days,
            "exit_by_date": self.exit_by_date.isoformat(),
            "sessions_to_expiry": self.sessions_to_expiry,
            "truncated_by_expiry": self.truncated_by_expiry,
            "feasible": self.feasible,
            "reason": self.reason,
        }


def plan_hold(
    entry_day: date,
    expiry: date,
    requested_sessions: int,
    *,
    expiry_buffer_sessions: int = 1,
) -> HoldPlan:
    """Reconcile the intended hold against how much life the contract has.

    A position is never planned to run into expiry: the buffer keeps at least
    one session between the planned exit and the expiry, because the final
    session of an option's life is where gamma and theta stop being modellable
    and start being a coin toss.
    """
    to_expiry = sessions_to_expiry(entry_day, expiry)
    usable = to_expiry - expiry_buffer_sessions
    planned = max(min(requested_sessions, usable), 0)
    truncated = planned < requested_sessions

    reason = ""
    if planned < 1:
        reason = (
            f"expiry {expiry} is {to_expiry} session(s) away; with a "
            f"{expiry_buffer_sessions}-session buffer there is no room to hold"
        )
    elif truncated:
        reason = (
            f"hold truncated from {requested_sessions} to {planned} session(s) "
            f"by an expiry {to_expiry} session(s) out"
        )

    exit_by = add_sessions(entry_day, max(planned, 0))
    return HoldPlan(
        requested_sessions=requested_sessions,
        planned_sessions=planned,
        calendar_days=calendar_days_for_sessions(entry_day, max(planned, 0)),
        exit_by_date=exit_by,
        sessions_to_expiry=to_expiry,
        truncated_by_expiry=truncated,
        reason=reason,
    )


# ── expiry geometry ─────────────────────────────────────────────────────────


@dataclass
class ExpiryGeometry:
    """How hard a given contract has to work to repay its own cost."""

    expiry: date
    days_to_expiry: float
    sessions_to_expiry: int
    strike: float
    option_type: str
    premium: float
    implied_vol: float
    delta: float
    lot_size: int
    round_trip_cost_per_unit: float
    hold_sessions: int
    hold_calendar_days: float
    breakeven_move: float | None       # fraction of spot, in the favourable direction
    implied_move: float | None         # 1 sigma over the hold
    breakeven_ratio: float | None      # breakeven / implied — LOWER IS BETTER
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "expiry": self.expiry.isoformat(),
            "days_to_expiry": self.days_to_expiry,
            "sessions_to_expiry": self.sessions_to_expiry,
            "strike": self.strike,
            "option_type": self.option_type,
            "premium": self.premium,
            "implied_vol": self.implied_vol,
            "delta": self.delta,
            "lot_size": self.lot_size,
            "round_trip_cost_per_unit": self.round_trip_cost_per_unit,
            "hold_sessions": self.hold_sessions,
            "hold_calendar_days": self.hold_calendar_days,
            "breakeven_move": self.breakeven_move,
            "breakeven_move_pct": None if self.breakeven_move is None else self.breakeven_move * 100.0,
            "implied_move": self.implied_move,
            "breakeven_ratio": self.breakeven_ratio,
            "reason": self.reason,
        }


def breakeven_move(
    *,
    forward: float,
    strike: float,
    tenor_years: float,
    implied_vol: float,
    option_type: str,
    premium: float,
    round_trip_cost_per_unit: float,
    hold_calendar_days: float,
    r: float = 0.0,
    max_move: float = 0.25,
) -> float | None:
    """Favourable underlying move that repays cost and decay over the hold.

    Solved on the actual Black-76 price at the reduced maturity rather than
    approximated through delta, because over a multi-day hold on a short-dated
    option the delta at entry is not the delta that earns the money.

    Implied vol is held FIXED across the hold. That is a deliberate and
    conservative choice for a long-premium lane: assuming vol expands in your
    favour is the most common way a breakeven calculation flatters itself.
    """
    if premium <= 0 or forward <= 0 or strike <= 0 or tenor_years <= 0:
        return None
    t_exit = tenor_years - hold_calendar_days / CALENDAR_DAYS_PER_YEAR
    if t_exit <= 0:
        return None

    sign = 1.0 if option_type == "CE" else -1.0
    target = premium + round_trip_cost_per_unit

    lo, hi = 0.0, max_move
    if black76_price(forward * (1.0 + sign * hi), strike, t_exit, implied_vol, option_type, r) < target:
        return None
    for _ in range(64):
        mid = 0.5 * (lo + hi)
        px = black76_price(forward * (1.0 + sign * mid), strike, t_exit, implied_vol, option_type, r)
        if px >= target:
            hi = mid
        else:
            lo = mid
    return hi


def evaluate_geometry(
    sl: SurfaceSlice,
    *,
    strike: float,
    option_type: str,
    premium: float,
    implied_vol: float,
    delta: float,
    oi: float | None,
    log_moneyness: float | None,
    lot_size: int,
    entry_day: date,
    hold_sessions: int,
    cost_model: CostModel,
    r: float = 0.0,
) -> ExpiryGeometry:
    entry = estimate_fill(
        premium, lot_size, "buy", "entry",
        model=cost_model, oi=oi, log_moneyness=log_moneyness, sigma=implied_vol,
    )
    exit_ = estimate_fill(
        premium, lot_size, "sell", "exit",
        model=cost_model, oi=oi, log_moneyness=log_moneyness, sigma=implied_vol,
    )
    rt_per_unit = (entry.total_cost + exit_.total_cost) / max(lot_size, 1)
    hold_days = calendar_days_for_sessions(entry_day, hold_sessions)

    be = breakeven_move(
        forward=sl.forward or 0.0,
        strike=strike,
        tenor_years=sl.tenor_years,
        implied_vol=implied_vol,
        option_type=option_type,
        premium=premium,
        round_trip_cost_per_unit=rt_per_unit,
        hold_calendar_days=hold_days,
        r=r,
    )
    implied = implied_vol * math.sqrt(max(hold_sessions, 0) / TRADING_SESSIONS_PER_YEAR)
    ratio = (be / implied) if (be is not None and implied > 0) else None

    return ExpiryGeometry(
        expiry=sl.expiry,
        days_to_expiry=sl.days_to_expiry,
        sessions_to_expiry=sessions_to_expiry(entry_day, sl.expiry),
        strike=strike,
        option_type=option_type,
        premium=premium,
        implied_vol=implied_vol,
        delta=delta,
        lot_size=lot_size,
        round_trip_cost_per_unit=rt_per_unit,
        hold_sessions=hold_sessions,
        hold_calendar_days=hold_days,
        breakeven_move=be,
        implied_move=implied if implied > 0 else None,
        breakeven_ratio=ratio,
        reason="" if be is not None else "no move up to the search ceiling repays cost and decay",
    )
