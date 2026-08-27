"""Shared exit-rule engine: given an entry premium and a chronological
stream of subsequent 30-minute option-premium bars, determines the exit
(price/ts/reason) using the SAME 15/20/10 stop/trail-activation/trail rule
described in fusion/m6_select.py's own module docstring -- one rule,
defined exactly once, so M9's live paper walk-forward and M8's historical
backtest replay can never silently drift apart.

RULE, restated precisely against the ticket's own stored stop/target1/
target2 (STOP_PCT/TARGET1_PCT/TARGET2_PCT from fusion/m6_select.py -- not
re-declared here, so a change to those constants can never leave this file
enforcing a stale level):
  initial stop  = entry * (1 - STOP_PCT)              -- 'stop' if this
                                                          fires before the
                                                          trail ever
                                                          activates
  trail-activate = entry * (1 + TARGET1_PCT)           -- once ANY bar's
                                                          high reaches this,
                                                          the trail turns on
  trail level   = running_peak_high * (1 - TRAIL_PCT)  -- ratchets up only,
                                                          never down; the
                                                          live stop becomes
                                                          max(initial stop,
                                                          trail level) once
                                                          activated. Exit
                                                          via the trail is
                                                          labelled 'target1'
                                                          (it did reach
                                                          trail-activation,
                                                          which is the
                                                          useful journal
                                                          fact -- it just
                                                          gave back before
                                                          reaching target2)
  target2       = entry * (1 + TARGET2_PCT)            -- a hard outer exit
                                                          if reached before
                                                          the trailing stop
                                                          catches it

A single bar's true intrabar path (did the low print before the high, or
after) is not observable from OHLC alone. Doctrine says default to the more
conservative reading rather than assume the best case, so when one bar's
range would satisfy BOTH the live stop and target2, the stop is assumed to
have printed first.

time_stop_eod (same trading session as fill, no stop/target1/target2 hit by
the session cutoff) is handled here since it only needs the bar stream
itself. time_stop_t3 (a multi-day defensive backstop for a position that
somehow survived past its own first EOD -- e.g. the flatten sweep did not
run that day) and daily_standdown_flatten (an account-level, cross-position
concern) both need state beyond one position's bar list and are handled by
the caller (paper/engine.py for M9, backtest/harness.py for M8), not here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from fusion.m6_select import STOP_PCT, TARGET1_PCT, TARGET2_PCT

TRAIL_PCT = 0.10
EOD_FLATTEN_IST = time(15, 10)   # mirrors MACD mini's own expiry-flatten sweep time
_IST = ZoneInfo("Asia/Kolkata")


def load_same_session_bars(cursor, symbol, strike, option_type, expiry, fill_ts, as_of_ts):
    """Every bar for one contract, strictly after fill_ts, on fill_ts's own
    IST trading day, up to as_of_ts -- the exact bar stream walk_exit()
    expects. Shared by M9 (paper engine, as_of_ts = now) and M8 (backtest
    replay, as_of_ts = the replay's own current instant) so both walk
    forward against identically-scoped data.

    DEDUP: DISTINCT ON (time) needs a deterministic tiebreak or Postgres
    returns whichever duplicate it happened to reach first. An earlier
    `(close IS NULL) ASC` tiebreak was dead code -- the WHERE clause already
    excludes NULL close, so every candidate row tied at 0 and the ordering
    was still arbitrary. `source` is the real discriminator (the same class
    of duplicate m2_flow.load_chain_eod dedups on). It is an ORDER BY
    preference rather than a `WHERE source = 'upstox'` filter deliberately:
    today upstox is the only source (verified live), so a hard filter would
    be a silent no-op that quietly drops every bar the day a second feed is
    added, whereas a preference keeps the data and still picks one row
    deterministically.

    option_premium_candles carries more than one bar granularity for the
    same contract (confirmed live: 3minute alongside 30minute for the same
    underlying/strike/option_type/expiry) -- every OTHER cadence-sensitive
    module in this codebase (M2/M3/M5, and M6's own timing join) standardizes
    on 30minute, so the walk must too. Without this filter, DISTINCT ON
    (time) would silently interleave both granularities into one sequence,
    corrupting peak-high tracking and the EOD-timing check with sub-bars
    that do not represent a real 30-minute window."""
    session_day = fill_ts.astimezone(_IST).date()
    cursor.execute(
        """SELECT DISTINCT ON (time) time, open, high, low, close
           FROM option_premium_candles
           WHERE underlying = %(symbol)s AND strike = %(strike)s
             AND option_type = %(option_type)s AND expiry = %(expiry)s
             AND interval = '30minute'
             AND time > %(fill_ts)s AND time <= %(as_of)s
             AND date(time AT TIME ZONE 'Asia/Kolkata') = %(session_day)s
             AND open IS NOT NULL AND high IS NOT NULL
             AND low IS NOT NULL AND close IS NOT NULL
           ORDER BY time ASC, (source = 'upstox') DESC, source ASC""",
        {"symbol": symbol, "strike": strike, "option_type": option_type, "expiry": expiry,
         "fill_ts": fill_ts, "as_of": as_of_ts, "session_day": session_day},
    )
    return [Bar(ts=ts, open=float(open_), high=float(high), low=float(low), close=float(close))
            for ts, open_, high, low, close in cursor.fetchall()]


@dataclass
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass
class ExitResult:
    exit_price: float
    exit_ts: datetime
    # stop | trail_stop | target2 | time_stop_eod.
    #
    # 'stop' and 'trail_stop' are DELIBERATELY distinct. Both are a downside
    # exit, but they are different events and the journal has to be able to
    # tell them apart: 'stop' means the trade never worked (the initial -15%
    # stop was hit with the trail never armed); 'trail_stop' means the trade
    # DID reach +20%, armed the trail, and then gave back to the ratchet. An
    # earlier version labelled the second case 'target1', which made per-reason
    # attribution unrecoverable -- a giveback and a target touch were the same
    # string. There is no plain 'target1' exit: +20% only ARMS the trail, it
    # never closes the position on its own.
    exit_reason: str
    holding_bars: int


def r_multiple(entry_premium: float, exit_price: float) -> float | None:
    """THE definition of R for this system. Defined here, once, because M8 and
    M9 each had their own and they disagreed by 6.67x.

    R = (exit - entry) / entry -- i.e. the unit of risk is the FULL premium
    paid, not the stop distance. That is the honest denominator for a long
    option: the position can go to zero, the stop is a courtesy the market may
    gap straight through (see _fillable), and M7 already sizes on full premium
    (sizing_risk_rupees = entry * lots * lot_size). M8 previously divided by
    `entry * STOP_PCT`, inflating every R by 1/0.15 = 6.67x relative to M9's
    full-premium R, which made vanguard_backtest_runs' avg_r and decile report
    silently non-comparable with attribution_runs' -- two tables of R values
    that looked like the same unit and were not.

    Returns None rather than raising on a zero entry, so a bad row is dropped
    from the statistics instead of fabricating an R.
    """
    if not entry_premium:
        return None
    return round((exit_price - entry_premium) / entry_premium, 4)


def _fillable(level: float, bar: Bar) -> float:
    """The price a stop at `level` could ACTUALLY have filled at in this bar.

    A stop is a trigger, not a guarantee. If the bar opened below the level
    (a gap through it), the order fills at the open, not at the level -- and
    returning `level` there books a fill at a price the instrument never
    traded, silently flattering every gap-down. Options gap hard and often, so
    this is not a rare edge case. The result is additionally clamped into
    [low, high] so no exit price can ever fall outside the bar's real range.
    """
    price = bar.open if bar.open < level else level
    return max(bar.low, min(bar.high, price))


def walk_exit(entry_premium: float, fill_ts: datetime, bars: list[Bar]) -> ExitResult | None:
    """bars must be chronological, same instrument, strictly after fill_ts,
    limited by the caller to the fill's own trading session (walk_exit has
    no calendar awareness beyond EOD_FLATTEN_IST on each bar's own day --
    it does not know which day is fill_ts's session close). Returns None if
    no bar resolves an exit (position is still open as of the last bar
    seen) -- the caller decides what "still open" means across sessions.

    ORDER WITHIN A BAR, and why it matters: the stop already in force when the
    bar OPENS is tested before this bar's own high is folded into the trail.
    Otherwise a single bar that both spikes to +20% (arming the trail) and
    craters through the initial stop gets booked as a trail exit at a level
    the trail only reached because of that same bar's high -- turning a losing
    trade into a winning one out of nothing but evaluation order.

    Only once the bar has survived the pre-existing stop does its high count
    toward the peak/trail. If the newly-raised trail is then breached by the
    same bar's low, the true intrabar sequence is unknowable and the
    conservative reading is taken.
    """
    initial_stop = entry_premium * (1 - STOP_PCT)
    activate_level = entry_premium * (1 + TARGET1_PCT)
    target2_level = entry_premium * (1 + TARGET2_PCT)

    activated = False
    peak_high = entry_premium
    live_stop = initial_stop

    for i, bar in enumerate(bars, start=1):
        # 1. The stop that was already in force before this bar printed.
        if float(bar.low) <= live_stop:
            return ExitResult(
                exit_price=round(_fillable(live_stop, bar), 4), exit_ts=bar.ts,
                exit_reason="trail_stop" if activated else "stop", holding_bars=i,
            )

        # 2. Survived it -- now this bar's own high may arm/raise the trail.
        peak_high = max(peak_high, float(bar.high))
        if not activated and peak_high >= activate_level:
            activated = True
        if activated:
            live_stop = max(live_stop, peak_high * (1 - TRAIL_PCT))

        hit_target2 = float(bar.high) >= target2_level
        hit_trail = float(bar.low) <= live_stop

        # 3. Both reachable inside one bar: order unknowable, so take the WORSE
        #    (lower) of the two fills. Note this is NOT unconditionally
        #    "stop first" -- once the trail has ratcheted above target2, the
        #    trail is the BETTER outcome and preferring it would be
        #    anti-conservative, flattering the trade instead of penalising it.
        if hit_target2 and hit_trail:
            if live_stop <= target2_level:
                return ExitResult(
                    exit_price=round(_fillable(live_stop, bar), 4), exit_ts=bar.ts,
                    exit_reason="trail_stop", holding_bars=i,
                )
            return ExitResult(
                exit_price=round(target2_level, 4), exit_ts=bar.ts,
                exit_reason="target2", holding_bars=i,
            )
        if hit_trail:
            return ExitResult(
                exit_price=round(_fillable(live_stop, bar), 4), exit_ts=bar.ts,
                exit_reason="trail_stop", holding_bars=i,
            )
        if hit_target2:
            return ExitResult(
                exit_price=round(target2_level, 4), exit_ts=bar.ts,
                exit_reason="target2", holding_bars=i,
            )
        if bar.ts.astimezone(_IST).time() >= EOD_FLATTEN_IST:
            return ExitResult(
                exit_price=float(bar.close), exit_ts=bar.ts,
                exit_reason="time_stop_eod", holding_bars=i,
            )
    return None
