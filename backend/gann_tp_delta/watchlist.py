"""Persisted Gann watchlist — one row per instrument per session.

Closes GAP 1.  The owner asked for a watchlist carrying "spot prices, current
regime as per gann model, next turn date, price-time squaring date, gann next
angle resistance, support etc".  All of that was already being computed inside
the paper agent's scan and then discarded: there was no `gann*` table in the
database and no ledger participation, so a lane that had in fact opened 406
paper positions across 21 sessions looked dead from every readable surface.

Contract for every field
------------------------
COMPUTED OR NULL.  There is no fabricated default anywhere in this module.
When a field cannot be derived — no confirmed anchor yet, too few daily bars
for the regime engine, no forward cycle projection inside the horizon — the
column is NULL and the reason is recorded under ``null_reasons`` so the gap is
visible instead of being papered over with a plausible-looking number.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

import pandas as pd

from gann_tp_delta.anchors import select_anchor
from gann_tp_delta.cycles import (
    CycleDef,
    causal_anchors,
    next_projection,
    resolve_price_unit,
    testable_cycles,
)
from gann_tp_delta.cycle_prominence import MIN_OBSERVATIONS as MIN_CYCLE_OBSERVATIONS
from gann_tp_delta.geometry import gann_fan, price_time_square, square_of_nine
from gann_tp_delta.scaling import harmonic_speed
from gann_tp_delta.schemas import GannAngle, SquareNineLevel

#: Forward horizon for "next turn date".  Beyond ~6 months a projection is not
#: actionable for a lane holding days-to-weeks, and the longer cycles that
#: reach further are the ones this history cannot test anyway.
NEXT_TURN_HORIZON_DAYS = 180

#: How many of the largest confirmed swing pivots count as "significant" highs
#: and lows for cycle counting.  Gann counted from major extremes; counting
#: from every confirmed 5-bar pivot makes a projection land on every session.
SIGNIFICANT_ANCHOR_COUNT = 8

#: Plus the most recent confirmed pivots — a fresh swing is Gann-significant
#: even before its magnitude ranks, and short counts from it are the ones a
#: multi-day lane can actually act on.
RECENT_ANCHOR_COUNT = 4


@dataclass
class GannWatchlistRow:
    session_date: date
    underlying: str
    instrument_class: str
    timeframe: str = "1day"
    spot: float | None = None
    regime: str | None = None
    regime_strength: float | None = None
    anchor_kind: str | None = None
    anchor_time: datetime | None = None
    anchor_price: float | None = None
    anchor_confirmed_at: datetime | None = None
    price_unit: float | None = None
    next_turn_date: date | None = None
    next_turn_cycle_key: str | None = None
    next_turn_cycle_days: int | None = None
    next_turn_prominence: str | None = None
    price_time_square_date: date | None = None
    nearest_angle_support: float | None = None
    nearest_angle_resistance: float | None = None
    nearest_angle_support_name: str | None = None
    nearest_angle_resistance_name: str | None = None
    nearest_sq9_support: float | None = None
    nearest_sq9_resistance: float | None = None
    nearest_sq9_support_degree: int | None = None
    nearest_sq9_resistance_degree: int | None = None
    conviction: float | None = None
    setup_state: str | None = None
    archetype: str | None = None
    side: str | None = None
    blockers: list[str] = field(default_factory=list)
    active_cycles: list[dict[str, Any]] = field(default_factory=list)
    null_reasons: dict[str, str] = field(default_factory=dict)
    daily_bars: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _days_per_session(frame: pd.DataFrame, lookback: int = 120) -> float:
    """Calendar days per trading session, measured on this instrument's frame.

    ~1.45 for a five-day NSE week, ~1.2 for MCX. Measured rather than assumed
    so a commodity with Saturday sessions is not projected on an NSE calendar.
    Falls back to 1.0 when there is not enough frame to measure, which is the
    old behaviour and is never worse than it.
    """
    if frame is None or len(frame.index) < 2:
        return 1.0
    tail = frame.tail(max(int(lookback), 2))
    first = pd.Timestamp(tail["time"].iloc[0])
    last = pd.Timestamp(tail["time"].iloc[-1])
    sessions = len(tail.index) - 1
    span = (last - first).days
    if sessions <= 0 or span <= 0:
        return 1.0
    return max(span / float(sessions), 1.0)


def _split_levels(
    price: float, angles: Sequence[GannAngle]
) -> tuple[GannAngle | None, GannAngle | None]:
    below = [a for a in angles if a.current_price <= price]
    above = [a for a in angles if a.current_price > price]
    support = max(below, key=lambda a: a.current_price, default=None)
    resistance = min(above, key=lambda a: a.current_price, default=None)
    return support, resistance


def _split_sq9(
    price: float, levels: Sequence[SquareNineLevel]
) -> tuple[SquareNineLevel | None, SquareNineLevel | None]:
    below = [l for l in levels if l.price <= price]
    above = [l for l in levels if l.price > price]
    support = max(below, key=lambda l: l.price, default=None)
    resistance = min(above, key=lambda l: l.price, default=None)
    return support, resistance


def compute_watchlist_row(
    *,
    underlying: str,
    instrument_class: str,
    daily_frame: pd.DataFrame,
    config: dict[str, Any],
    signal: Any = None,
    prominent_cycle_keys: Sequence[str] | None = None,
    cycles: Sequence[CycleDef] | None = None,
    as_of: date | None = None,
) -> GannWatchlistRow:
    """Build one persisted row from a DAILY bar frame.

    ``signal`` is the live :class:`ConfluenceSignal` when the caller already
    has one (the paper agent does); omitted, the regime/conviction columns are
    NULL with a recorded reason rather than being recomputed inconsistently.
    """
    session = as_of or (
        pd.Timestamp(daily_frame["time"].iloc[-1]).date()
        if daily_frame is not None and not daily_frame.empty
        else datetime.now(timezone.utc).date()
    )
    row = GannWatchlistRow(
        session_date=session,
        underlying=str(underlying).upper(),
        instrument_class=str(instrument_class),
        timeframe=str((config.get("paper_agent") or {}).get("timeframe") or "1day"),
        daily_bars=0 if daily_frame is None else int(len(daily_frame.index)),
    )
    if daily_frame is None or daily_frame.empty:
        row.null_reasons = {
            key: "no daily bars available for this instrument"
            for key in (
                "spot", "regime", "anchor_kind", "next_turn_date",
                "price_time_square_date", "nearest_angle_support",
                "nearest_angle_resistance", "nearest_sq9_support",
                "nearest_sq9_resistance", "conviction",
            )
        }
        return row

    frame = daily_frame.reset_index(drop=True)
    current_index = len(frame.index) - 1
    spot = _finite(frame["close"].iloc[-1])
    row.spot = spot
    if spot is None:
        row.null_reasons["spot"] = "latest daily close is not finite"
        return row

    geometry_cfg = dict(config.get("geometry") or {})
    unit_setting = geometry_cfg.get("price_unit", 1.0)
    price_unit = resolve_price_unit(spot) if str(unit_setting).lower() == "auto" else float(unit_setting or 1.0)
    row.price_unit = price_unit

    anchor = select_anchor(frame, mode="auto_pivot", config=config.get("anchors") or {})
    if anchor is None or anchor.mode == "fallback":
        row.null_reasons["anchor_kind"] = (
            "no confirmed swing pivot yet — a pivot needs "
            f"{int((config.get('anchors') or {}).get('pivot_right', 5))} further sessions to confirm"
        )
    else:
        row.anchor_kind = anchor.kind
        row.anchor_price = _finite(anchor.price)
        try:
            row.anchor_time = pd.Timestamp(anchor.time).to_pydatetime()
        except (TypeError, ValueError):
            row.anchor_time = None
        lag = int((config.get("anchors") or {}).get("pivot_right", 5))
        confirm_index = min(anchor.bar_index + lag, current_index)
        try:
            row.anchor_confirmed_at = pd.Timestamp(frame["time"].iloc[confirm_index]).to_pydatetime()
        except (TypeError, ValueError, IndexError):
            row.anchor_confirmed_at = None

    if anchor is not None:
        h, _vectors = harmonic_speed(
            frame,
            mode=str((config.get("scaling") or {}).get("default_h_mode") or "median_tpd"),
            anchor_config=config.get("anchors") or {},
            scaling_config=config.get("scaling") or {},
        )
        angles = gann_fan(
            anchor=anchor,
            h=h.value,
            current_bar_index=current_index,
            current_price=spot,
            ratios=geometry_cfg.get("gann_ratios") or [],
            projection_bars=int(geometry_cfg.get("projection_bars") or 60),
        )
        support, resistance = _split_levels(spot, angles)
        if support is not None:
            row.nearest_angle_support = _finite(support.current_price)
            row.nearest_angle_support_name = support.name
        else:
            row.null_reasons["nearest_angle_support"] = "every fan angle sits above spot"
        if resistance is not None:
            row.nearest_angle_resistance = _finite(resistance.current_price)
            row.nearest_angle_resistance_name = resistance.name
        else:
            row.null_reasons["nearest_angle_resistance"] = "every fan angle sits below spot"

        sq9 = square_of_nine(
            anchor_price=anchor.price,
            current_price=spot,
            price_unit=price_unit,
            degrees=geometry_cfg.get("sq9_degrees") or [],
        )
        sq9_support, sq9_resistance = _split_sq9(spot, sq9)
        if sq9_support is not None:
            row.nearest_sq9_support = _finite(sq9_support.price)
            row.nearest_sq9_support_degree = int(sq9_support.degree)
        else:
            row.null_reasons["nearest_sq9_support"] = "no Square-of-Nine level below spot in the degree set"
        if sq9_resistance is not None:
            row.nearest_sq9_resistance = _finite(sq9_resistance.price)
            row.nearest_sq9_resistance_degree = int(sq9_resistance.degree)
        else:
            row.null_reasons["nearest_sq9_resistance"] = "no Square-of-Nine level above spot in the degree set"

        square = price_time_square(
            anchor=anchor,
            current_bar_index=current_index,
            current_price=spot,
            h=h.value,
            tolerance=float(geometry_cfg.get("squaring_tolerance") or 0.05),
        )
        square_index = anchor.bar_index + int(round(square.scaled_price_move))
        if square.scaled_price_move <= 0:
            row.null_reasons["price_time_square_date"] = "price has not moved from the anchor"
        elif square_index < current_index:
            row.null_reasons["price_time_square_date"] = (
                f"squaring for this anchor elapsed {current_index - square_index} sessions ago"
            )
        else:
            # `square_index` is a SESSION index, not a calendar offset. Adding
            # it as calendar days under-projects by the weekends and holidays
            # in between (a 28-session squaring lands ~40 calendar days out,
            # not 28). Convert with the frame's OWN observed calendar-days-per
            # -session so the number is derived from this instrument's real
            # trading calendar rather than assumed.
            last_time = pd.Timestamp(frame["time"].iloc[-1])
            ahead_sessions = int(square_index - current_index)
            row.price_time_square_date = (
                last_time + pd.Timedelta(days=int(round(ahead_sessions * _days_per_session(frame))))
            ).date()
    else:
        for key in (
            "nearest_angle_support", "nearest_angle_resistance",
            "nearest_sq9_support", "nearest_sq9_resistance", "price_time_square_date",
        ):
            row.null_reasons[key] = "no anchor available"

    # ── Next turn date, from the real (calendar-day) Gann cycle library ────
    span_days = (
        pd.Timestamp(frame["time"].iloc[-1]).date() - pd.Timestamp(frame["time"].iloc[0]).date()
    ).days
    if cycles is not None:
        library = list(cycles)
    else:
        # Only cycles this instrument's own history could even test. Projecting
        # a 361-day count off ~1.3 years of stock history is decoration: it can
        # never have been verified, and with hundreds of anchers it guarantees
        # SOME cycle lands on today, every day.
        library = testable_cycles(span_days, MIN_CYCLE_OBSERVATIONS)
    allowed = set(prominent_cycle_keys or [])
    if allowed:
        library = [c for c in library if c.key in allowed]
    anchor_cfg = config.get("anchors") or {}
    daily_anchors = causal_anchors(
        frame,
        left=int(anchor_cfg.get("pivot_left", 5)),
        right=int(anchor_cfg.get("pivot_right", 5)),
    )
    # Gann counts from SIGNIFICANT highs and lows, not from every five-bar
    # wiggle. Keep the largest-magnitude confirmed pivots only — otherwise the
    # "next turn date" is always today, because with ~200 anchors x ~10 cycles
    # something always lands on the current session.
    # Count from THE anchor this row's geometry is measured from — the same
    # swing the fan, the Square of Nine and the squaring all originate at.
    # Counting from a POOL of anchors saturates the calendar (12 anchors x 10
    # cycles x 8 repeats puts a projection on almost every session, which is
    # how a "next turn date" becomes information-free), and it also breaks the
    # row's own contract that every field is measured from one stated anchor.
    if anchor is not None and anchor.mode != "fallback":
        daily_anchors = [a for a in daily_anchors if a.index == anchor.bar_index] or [
            a for a in daily_anchors if a.pivot_date == pd.Timestamp(anchor.time).date()
        ]
    else:
        daily_anchors = sorted(daily_anchors, key=lambda a: -a.magnitude)[:SIGNIFICANT_ANCHOR_COUNT]
    if not library:
        row.null_reasons["next_turn_date"] = (
            "no cycle is demonstrably prominent for this instrument — trading only "
            "prominent cycles means there is nothing to project"
        )
    elif not daily_anchors:
        row.null_reasons["next_turn_date"] = "no causally confirmed daily swing pivot to count from"
    else:
        projection = next_projection(
            daily_anchors, library, as_of=session, horizon_days=NEXT_TURN_HORIZON_DAYS
        )
        if projection is None:
            row.null_reasons["next_turn_date"] = (
                f"no cycle from a confirmed anchor projects inside the next "
                f"{NEXT_TURN_HORIZON_DAYS} days"
            )
        else:
            turn_date, cycle, source_anchor, repeat = projection
            row.next_turn_date = turn_date
            row.next_turn_cycle_key = cycle.key
            row.next_turn_cycle_days = int(cycle.days)
            row.next_turn_prominence = "prominent" if allowed else "unranked"
            row.active_cycles = [
                {
                    "cycle_key": cycle.key,
                    "family": cycle.family,
                    "days": int(cycle.days),
                    "repeat": int(repeat),
                    "anchor_date": source_anchor.pivot_date.isoformat(),
                    "anchor_kind": source_anchor.kind,
                    "anchor_confirmed_date": source_anchor.confirmed_date.isoformat(),
                    "projected_date": turn_date.isoformat(),
                    "days_away": (turn_date - session).days,
                }
            ]

    # ── Regime / conviction, from the live signal when the caller has one ──
    if signal is None:
        for key in ("regime", "conviction", "setup_state"):
            row.null_reasons[key] = "no strategy signal supplied for this session"
    else:
        row.regime = getattr(signal, "regime", None) or (signal.get("regime") if isinstance(signal, dict) else None)
        row.regime_strength = _finite(
            getattr(signal, "regime_strength", None)
            if not isinstance(signal, dict)
            else signal.get("regime_strength")
        )
        row.conviction = _finite(
            getattr(signal, "conviction", None) if not isinstance(signal, dict) else signal.get("conviction")
        )
        row.setup_state = (
            getattr(signal, "setup_state", None) if not isinstance(signal, dict) else signal.get("setup_state")
        )
        row.archetype = (
            getattr(signal, "archetype", None) if not isinstance(signal, dict) else signal.get("archetype")
        )
        row.side = getattr(signal, "side", None) if not isinstance(signal, dict) else signal.get("side")
        blockers = (
            getattr(signal, "blockers", None) if not isinstance(signal, dict) else signal.get("blockers")
        )
        row.blockers = [str(item) for item in (blockers or [])]
    return row


UPSERT_SQL = """
INSERT INTO gann_watchlist_snapshots (
    session_date, underlying, instrument_class, timeframe, spot,
    regime, regime_strength, anchor_kind, anchor_time, anchor_price,
    anchor_confirmed_at, price_unit, next_turn_date, next_turn_cycle_key,
    next_turn_cycle_days, next_turn_prominence, price_time_square_date,
    nearest_angle_support, nearest_angle_resistance,
    nearest_angle_support_name, nearest_angle_resistance_name,
    nearest_sq9_support, nearest_sq9_resistance,
    nearest_sq9_support_degree, nearest_sq9_resistance_degree,
    conviction, setup_state, archetype, side, blockers, active_cycles,
    null_reasons, daily_bars, computed_at
) VALUES (
    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,
    $21,$22,$23,$24,$25,$26,$27,$28,$29,$30::jsonb,$31::jsonb,$32::jsonb,$33, now()
)
ON CONFLICT (session_date, underlying) DO UPDATE SET
    instrument_class = EXCLUDED.instrument_class,
    timeframe = EXCLUDED.timeframe,
    spot = EXCLUDED.spot,
    regime = EXCLUDED.regime,
    regime_strength = EXCLUDED.regime_strength,
    anchor_kind = EXCLUDED.anchor_kind,
    anchor_time = EXCLUDED.anchor_time,
    anchor_price = EXCLUDED.anchor_price,
    anchor_confirmed_at = EXCLUDED.anchor_confirmed_at,
    price_unit = EXCLUDED.price_unit,
    next_turn_date = EXCLUDED.next_turn_date,
    next_turn_cycle_key = EXCLUDED.next_turn_cycle_key,
    next_turn_cycle_days = EXCLUDED.next_turn_cycle_days,
    next_turn_prominence = EXCLUDED.next_turn_prominence,
    price_time_square_date = EXCLUDED.price_time_square_date,
    nearest_angle_support = EXCLUDED.nearest_angle_support,
    nearest_angle_resistance = EXCLUDED.nearest_angle_resistance,
    nearest_angle_support_name = EXCLUDED.nearest_angle_support_name,
    nearest_angle_resistance_name = EXCLUDED.nearest_angle_resistance_name,
    nearest_sq9_support = EXCLUDED.nearest_sq9_support,
    nearest_sq9_resistance = EXCLUDED.nearest_sq9_resistance,
    nearest_sq9_support_degree = EXCLUDED.nearest_sq9_support_degree,
    nearest_sq9_resistance_degree = EXCLUDED.nearest_sq9_resistance_degree,
    conviction = EXCLUDED.conviction,
    setup_state = EXCLUDED.setup_state,
    archetype = EXCLUDED.archetype,
    side = EXCLUDED.side,
    blockers = EXCLUDED.blockers,
    active_cycles = EXCLUDED.active_cycles,
    null_reasons = EXCLUDED.null_reasons,
    daily_bars = EXCLUDED.daily_bars,
    computed_at = now()
"""


def upsert_params(row: GannWatchlistRow) -> list[Any]:
    import json

    return [
        row.session_date, row.underlying, row.instrument_class, row.timeframe, row.spot,
        row.regime, row.regime_strength, row.anchor_kind, row.anchor_time, row.anchor_price,
        row.anchor_confirmed_at, row.price_unit, row.next_turn_date, row.next_turn_cycle_key,
        row.next_turn_cycle_days, row.next_turn_prominence, row.price_time_square_date,
        row.nearest_angle_support, row.nearest_angle_resistance,
        row.nearest_angle_support_name, row.nearest_angle_resistance_name,
        row.nearest_sq9_support, row.nearest_sq9_resistance,
        row.nearest_sq9_support_degree, row.nearest_sq9_resistance_degree,
        row.conviction, row.setup_state, row.archetype, row.side,
        json.dumps(row.blockers), json.dumps(row.active_cycles), json.dumps(row.null_reasons),
        row.daily_bars,
    ]


async def write_rows(connection: Any, rows: Sequence[GannWatchlistRow]) -> int:
    """Persist rows one statement at a time — small, bounded, DB-polite."""
    written = 0
    for row in rows:
        await connection.execute(UPSERT_SQL, *upsert_params(row))
        written += 1
    return written


__all__ = [
    "NEXT_TURN_HORIZON_DAYS",
    "SIGNIFICANT_ANCHOR_COUNT",
    "RECENT_ANCHOR_COUNT",
    "GannWatchlistRow",
    "compute_watchlist_row",
    "UPSERT_SQL",
    "upsert_params",
    "write_rows",
]
