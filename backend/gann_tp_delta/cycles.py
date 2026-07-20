"""Gann TIME cycles — the recognised families, with sources.

Gann's time work is *calendar* work. Every construct below is measured in
CALENDAR DAYS from an anchor date (a significant prior high or low), or is an
absolute seasonal date. None of it is a bar count: a "90" in Gann is ninety
days / ninety degrees of the annual circle, not ninety bars of an arbitrary
intraday sampling grid.

That distinction is the reason this module exists. ``gann_tp_delta.geometry
.time_cycles`` counts ``config.geometry.bar_cycles`` in *bars*; on the legacy
15-minute frame "cycle 90" was 22.5 hours. The integers were Gann's, the
semantics were not. Everything here is day-count based and is consumed off a
DAILY bar series.

Families implemented
--------------------
``calendar``        30/45/60/90/120/135/144/180/270/360 day counts.
                    Source: W. D. Gann, *How to Make Profits in Commodities*
                    (1942), "Time Periods" / the Master Time Factor tables;
                    reiterated in *45 Years in Wall Street* (1949) ch. on
                    time cycles.  144 is the Master Number (square of 12).
``fractional_year`` The solar year (365.2425 d) divided by 8, 4, 3, 2, and the
                    2/3 and 3/4 divisions, plus integer multiples up to 3 yrs.
                    Source: Gann, *How to Make Profits in Commodities*, "The
                    Master Time Factor — divisions of the year"; also the
                    Master Calendar course material (1/8 = 45.6d, 1/4 = 91.3d,
                    1/3 = 121.7d, 1/2 = 182.6d, 2/3 = 243.5d, 3/4 = 273.9d).
``anniversary``     365 / 730 / 1095 / 1461 days — the 1st..4th anniversary of
                    a significant high or low.  Source: Gann, *45 Years in
                    Wall Street*, "Anniversary Dates"; *Truth of the Stock
                    Tape* on yearly repetition from extremes.
``week``            7 / 13 / 26 / 52 WEEKS → 49 / 91 / 182 / 364 days.
                    Source: Gann, *How to Make Profits in Commodities*,
                    weekly time-period tables (13 weeks = one quarter of the
                    circle, 26 = half, 52 = full).
``sq9_time``        Square-of-Nine TIME counts: degrees of TIME from the
                    anchor.  A full 360° revolution of the wheel advances the
                    square-root of the count by 2, so a count at θ degrees
                    from day 1 is ``(1 + θ/180)**2`` days — reproducing the
                    classic 1, 4, 9, 16, 25, 36 … squares of time with the
                    45° subdivisions between them.  Source: Gann's Square of
                    Nine (Master "Squares" course); the time application is
                    the same wheel read on the date ring rather than the
                    price ring.
``seasonal``        Equinoxes and solstices (≈ Mar 20, Jun 21, Sep 22, Dec 21)
                    — ABSOLUTE dates, not day-counts from an anchor.  Source:
                    Gann, *How to Make Profits in Commodities*, "Seasonal Time
                    Periods"; the cardinal points of the annual circle
                    (0°/90°/180°/270°).
``master_long``     10 / 20 / 30 / 60 year master cycles.  Source: Gann,
                    *Tunnel Thru the Air* (1927) and the *45 Years in Wall
                    Street* master-cycle chapter.  Generated so the library is
                    complete, and marked ``testable=False`` — see
                    :data:`MAX_TESTABLE_DAYS`.  This repo holds ~5.1 years of
                    index history; a 10-year cycle has ZERO observations in it
                    and must be reported UNTESTABLE, never "weak".

Anchor causality
----------------
Cycles are counted FROM AN ANCHOR, and a swing high/low is only identifiable
after the fact.  :func:`causal_anchors` therefore emits a pivot together with
the date on which it would first have been *confirmable in real time*
(``confirmed_date`` = the pivot date plus ``right`` further sessions).  A
projection is only admissible if its projected date is on or after the anchor's
confirmation date.  See :func:`project_cycle_dates`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Sequence

import pandas as pd

SOLAR_YEAR_DAYS = 365.2425

#: Cycles longer than this cannot accumulate enough non-overlapping
#: observations in the deepest history this repo holds (~5.1 years of index
#: daily bars ≈ 1,250 sessions).  They are still generated — so the library is
#: honest about what Gann specified — but are flagged ``testable=False`` and
#: are excluded from prominence scoring rather than being scored weakly.
MAX_TESTABLE_DAYS = 400


@dataclass(frozen=True)
class CycleDef:
    """One Gann time cycle: a calendar-day count with its provenance."""

    key: str
    family: str
    days: int
    label: str
    source: str
    testable: bool = True

    def __post_init__(self) -> None:  # pragma: no cover - dataclass guard
        if self.days <= 0:
            raise ValueError(f"cycle {self.key!r} must have a positive day count")


# ── Family generators ──────────────────────────────────────────────────────

_CALENDAR_DAYS = [30, 45, 60, 90, 120, 135, 144, 180, 270, 360]
_CALENDAR_SOURCE = (
    "Gann, How to Make Profits in Commodities (1942), Master Time Factor / "
    "Time Periods; 45 Years in Wall Street (1949), time-cycle tables"
)

_FRACTIONS: list[tuple[str, float]] = [
    ("1/8", 1.0 / 8.0),
    ("1/4", 1.0 / 4.0),
    ("1/3", 1.0 / 3.0),
    ("1/2", 1.0 / 2.0),
    ("2/3", 2.0 / 3.0),
    ("3/4", 3.0 / 4.0),
]
_FRACTION_SOURCE = (
    "Gann, How to Make Profits in Commodities (1942), 'divisions of the year' "
    "(1/8, 1/4, 1/3, 1/2, 2/3, 3/4 of 365.2425 days) and their multiples"
)

_ANNIVERSARY_SOURCE = (
    "Gann, 45 Years in Wall Street (1949), Anniversary Dates; Truth of the "
    "Stock Tape (1923), yearly repetition from significant highs and lows"
)

_WEEK_SOURCE = (
    "Gann, How to Make Profits in Commodities (1942), weekly time periods "
    "(13 weeks = one quarter of the circle, 26 = half, 52 = full)"
)

_SQ9_TIME_SOURCE = (
    "Gann Square of Nine read on the TIME ring: a 360 degree revolution "
    "advances the root of the day-count by 2, so days = (1 + theta/180)^2"
)

_SEASONAL_SOURCE = (
    "Gann, How to Make Profits in Commodities (1942), Seasonal Time Periods — "
    "the cardinal points of the annual circle (equinoxes and solstices)"
)

_MASTER_SOURCE = (
    "Gann, Tunnel Thru the Air (1927) and 45 Years in Wall Street (1949), "
    "master long cycles of 10, 20, 30 and 60 years"
)


def calendar_cycles() -> list[CycleDef]:
    return [
        CycleDef(f"cal_{d}", "calendar", d, f"{d}-day calendar count", _CALENDAR_SOURCE)
        for d in _CALENDAR_DAYS
    ]


def fractional_year_cycles(max_multiple: int = 3) -> list[CycleDef]:
    """1/8 … 3/4 of the solar year, and integer multiples of each."""
    out: list[CycleDef] = []
    seen: set[int] = set()
    for multiple in range(1, max(1, int(max_multiple)) + 1):
        for name, fraction in _FRACTIONS:
            days = int(round(SOLAR_YEAR_DAYS * fraction * multiple))
            if days in seen or days <= 0:
                continue
            seen.add(days)
            label = f"{name} year" if multiple == 1 else f"{multiple} x {name} year"
            out.append(
                CycleDef(
                    f"fy_{name.replace('/', '_')}_x{multiple}",
                    "fractional_year",
                    days,
                    f"{label} ({days}d)",
                    _FRACTION_SOURCE,
                    testable=days <= MAX_TESTABLE_DAYS,
                )
            )
    return sorted(out, key=lambda c: c.days)


def anniversary_cycles(max_years: int = 4) -> list[CycleDef]:
    out: list[CycleDef] = []
    for year in range(1, max(1, int(max_years)) + 1):
        days = int(round(SOLAR_YEAR_DAYS * year))
        out.append(
            CycleDef(
                f"anniv_{year}y",
                "anniversary",
                days,
                f"{year}-year anniversary ({days}d)",
                _ANNIVERSARY_SOURCE,
                testable=days <= MAX_TESTABLE_DAYS,
            )
        )
    return out


def week_cycles() -> list[CycleDef]:
    return [
        CycleDef(
            f"wk_{weeks}",
            "week",
            weeks * 7,
            f"{weeks}-week count ({weeks * 7}d)",
            _WEEK_SOURCE,
            testable=weeks * 7 <= MAX_TESTABLE_DAYS,
        )
        for weeks in (7, 13, 26, 52)
    ]


def sq9_time_cycles(max_days: int = MAX_TESTABLE_DAYS, step_degrees: int = 45) -> list[CycleDef]:
    """Square-of-Nine TIME counts: ``days = (1 + theta/180)**2``.

    theta = 180 -> 4d, 360 -> 9d, 540 -> 16d, 720 -> 25d … i.e. the classic
    squares of time, with the 45-degree subdivisions in between.
    """
    out: list[CycleDef] = []
    seen: set[int] = set()
    theta = int(step_degrees)
    while True:
        days = int(round((1.0 + theta / 180.0) ** 2))
        if days > int(max_days):
            break
        if days >= 20 and days not in seen:  # below ~20d the wheel is too dense to separate
            seen.add(days)
            out.append(
                CycleDef(
                    f"sq9t_{theta}",
                    "sq9_time",
                    days,
                    f"Square-of-Nine time {theta} degrees ({days}d)",
                    _SQ9_TIME_SOURCE,
                )
            )
        theta += int(step_degrees)
        if theta > 100_000:  # pragma: no cover - hard guard
            break
    return out


def master_long_cycles() -> list[CycleDef]:
    """10/20/30/60-year master cycles — generated, always ``testable=False``."""
    return [
        CycleDef(
            f"master_{years}y",
            "master_long",
            int(round(SOLAR_YEAR_DAYS * years)),
            f"{years}-year master cycle",
            _MASTER_SOURCE,
            testable=False,
        )
        for years in (10, 20, 30, 60)
    ]


def all_cycles() -> list[CycleDef]:
    """Every recognised family, deduplicated by (family, days)."""
    out: list[CycleDef] = []
    seen: set[tuple[str, int]] = set()
    for cycle in (
        calendar_cycles()
        + fractional_year_cycles()
        + anniversary_cycles()
        + week_cycles()
        + sq9_time_cycles()
        + master_long_cycles()
    ):
        token = (cycle.family, cycle.days)
        if token in seen:
            continue
        seen.add(token)
        out.append(cycle)
    return sorted(out, key=lambda c: (c.days, c.family))


def testable_cycles(history_days: int, min_observations: int = 20) -> list[CycleDef]:
    """Cycles that *could* accumulate ``min_observations`` non-overlapping
    repetitions inside ``history_days`` of calendar history.

    This is a necessary condition only — the actual observation count depends
    on how many causal anchors exist.  A cycle that fails here can never be
    significant and is reported UNTESTABLE rather than "weak".
    """
    span = max(int(history_days), 0)
    keep: list[CycleDef] = []
    for cycle in all_cycles():
        if not cycle.testable:
            continue
        if cycle.days * max(int(min_observations), 1) > span:
            continue
        keep.append(cycle)
    return keep


def untestable_cycles(history_days: int, min_observations: int = 20) -> list[tuple[CycleDef, str]]:
    """The complement of :func:`testable_cycles`, each with a stated reason."""
    span = max(int(history_days), 0)
    keep = {c.key for c in testable_cycles(span, min_observations)}
    out: list[tuple[CycleDef, str]] = []
    for cycle in all_cycles():
        if cycle.key in keep:
            continue
        if not cycle.testable:
            reason = (
                f"{cycle.days}d ({cycle.days / SOLAR_YEAR_DAYS:.1f}y) exceeds the "
                f"{MAX_TESTABLE_DAYS}d testability ceiling — zero repetitions exist "
                f"in any history this repo holds"
            )
        else:
            possible = span // cycle.days if cycle.days else 0
            reason = (
                f"{cycle.days}d cycle over {span}d of history admits at most "
                f"{possible} non-overlapping observations, below the "
                f"{min_observations} minimum"
            )
        out.append((cycle, reason))
    return out


# ── Seasonal absolute dates ────────────────────────────────────────────────

#: (month, day) of the equinoxes and solstices.  Fixed to the modern mean
#: dates; the true instants drift ±1 day, which the tolerance window absorbs.
SEASONAL_POINTS: list[tuple[str, int, int, int]] = [
    ("vernal_equinox", 3, 20, 0),
    ("summer_solstice", 6, 21, 90),
    ("autumnal_equinox", 9, 22, 180),
    ("winter_solstice", 12, 21, 270),
]

SEASONAL_SOURCE = _SEASONAL_SOURCE


def seasonal_dates(start: date, end: date) -> list[tuple[date, str]]:
    """Equinox/solstice dates in ``[start, end]`` — absolute, anchor-free."""
    out: list[tuple[date, str]] = []
    for year in range(start.year, end.year + 1):
        for name, month, day, _degree in SEASONAL_POINTS:
            point = date(year, month, day)
            if start <= point <= end:
                out.append((point, name))
    return sorted(out)


# ── Causal anchors on a daily series ───────────────────────────────────────


@dataclass(frozen=True)
class CausalAnchor:
    """A swing pivot together with the date it became knowable.

    ``confirmed_date`` is the date of the ``right``-th session AFTER the pivot
    session.  Nothing derived from this anchor may be used before that date.
    """

    kind: str          # swing_high | swing_low
    index: int         # position in the daily frame
    pivot_date: date
    price: float
    confirmed_index: int
    confirmed_date: date
    magnitude: float   # swing size as a fraction of price, for ranking


def causal_anchors(
    frame: pd.DataFrame,
    *,
    left: int = 5,
    right: int = 5,
    min_magnitude_pct: float = 0.0,
) -> list[CausalAnchor]:
    """Confirmed daily swing pivots, each carrying its confirmation date.

    A bar at index *i* is a swing high only if its high is the maximum of
    ``[i-left, i+right]`` — which is not knowable until bar ``i+right`` has
    printed.  ``confirmed_index`` records exactly that, so downstream code can
    enforce causality instead of assuming it.
    """
    if frame is None or frame.empty:
        return []
    needed = int(left) + int(right) + 1
    if len(frame.index) < needed:
        return []
    highs = frame["high"].astype(float).tolist()
    lows = frame["low"].astype(float).tolist()
    closes = frame["close"].astype(float).tolist()
    dates = [pd.Timestamp(value).date() for value in frame["time"]]
    last = len(frame.index) - 1
    out: list[CausalAnchor] = []
    for index in range(int(left), len(frame.index) - int(right)):
        window_lo = index - int(left)
        window_hi = index + int(right) + 1
        confirmed_index = min(index + int(right), last)
        local_high = max(highs[window_lo:window_hi])
        local_low = min(lows[window_lo:window_hi])
        span = max(local_high - local_low, 0.0)
        reference = abs(closes[index]) or 1.0
        magnitude = span / reference
        if magnitude < float(min_magnitude_pct):
            continue
        if highs[index] >= local_high:
            out.append(
                CausalAnchor(
                    "swing_high", index, dates[index], float(highs[index]),
                    confirmed_index, dates[confirmed_index], magnitude,
                )
            )
        if lows[index] <= local_low:
            out.append(
                CausalAnchor(
                    "swing_low", index, dates[index], float(lows[index]),
                    confirmed_index, dates[confirmed_index], magnitude,
                )
            )
    return out


@dataclass(frozen=True)
class CycleProjection:
    """A projected turn date from one anchor and one cycle."""

    cycle_key: str
    cycle_days: int
    anchor_kind: str
    anchor_date: date
    anchor_index: int
    projected_date: date
    projected_index: int     # nearest trading session index, -1 if beyond the frame
    window_lo: int
    window_hi: int


def project_cycle_dates(
    anchors: Sequence[CausalAnchor],
    cycles: Sequence[CycleDef],
    session_dates: Sequence[date],
    *,
    tolerance_sessions: int = 3,
) -> list[CycleProjection]:
    """Project every (anchor, cycle) pair onto the trading calendar.

    Causality: a projection is DROPPED unless its projected date is on or
    after ``anchor.confirmed_date`` — the first moment the anchor could have
    been recognised in real time.  With cycles of 30 days and up against a
    5-session confirmation lag this is never binding in practice, but it is
    enforced rather than assumed, and it becomes binding the moment anyone
    shortens the cycle set.
    """
    if not session_dates:
        return []
    calendar = list(session_dates)
    out: list[CycleProjection] = []
    tolerance = max(int(tolerance_sessions), 0)
    for anchor in anchors:
        for cycle in cycles:
            projected = anchor.pivot_date + timedelta(days=int(cycle.days))
            if projected < anchor.confirmed_date:
                continue
            index = _nearest_session_index(calendar, projected)
            out.append(
                CycleProjection(
                    cycle.key,
                    int(cycle.days),
                    anchor.kind,
                    anchor.pivot_date,
                    anchor.index,
                    projected,
                    index,
                    (index - tolerance) if index >= 0 else -1,
                    (index + tolerance) if index >= 0 else -1,
                )
            )
    return out


def _nearest_session_index(calendar: list[date], target: date) -> int:
    """Index of the trading session closest to ``target``; -1 if outside."""
    if not calendar or target < calendar[0] or target > calendar[-1]:
        return -1
    position = _bisect_left(calendar, target)
    if position >= len(calendar):
        return len(calendar) - 1
    if calendar[position] == target or position == 0:
        return position
    before = calendar[position - 1]
    after = calendar[position]
    return position - 1 if (target - before) <= (after - target) else position


def _bisect_left(calendar: list[date], target: date) -> int:
    lo, hi = 0, len(calendar)
    while lo < hi:
        mid = (lo + hi) // 2
        if calendar[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def next_projection(
    anchors: Sequence[CausalAnchor],
    cycles: Sequence[CycleDef],
    *,
    as_of: date,
    horizon_days: int = 180,
    max_repeats: int = 8,
) -> tuple[date, CycleDef, CausalAnchor, int] | None:
    """The soonest FORWARD projected turn date at or after ``as_of``.

    A Gann cycle REPEATS from its anchor — 30, 60, 90, 120 days from the same
    significant high are all counts of the 30-day cycle — so repetitions up to
    ``max_repeats`` are considered.  Without that, a cycle whose first
    repetition has already elapsed would report no forward date at all, which
    is wrong: the count simply rolls to the next repetition.

    Only anchors already confirmed as of ``as_of`` are eligible.  Returns
    ``(date, cycle, anchor, repeat)``.
    """
    best: tuple[date, CycleDef, CausalAnchor, int] | None = None
    limit = as_of + timedelta(days=max(int(horizon_days), 1))
    for anchor in anchors:
        if anchor.confirmed_date > as_of:
            continue
        for cycle in cycles:
            for repeat in range(1, max(int(max_repeats), 1) + 1):
                projected = anchor.pivot_date + timedelta(days=int(cycle.days) * repeat)
                if projected > limit:
                    break
                if projected < as_of or projected < anchor.confirmed_date:
                    continue
                if best is None or projected < best[0]:
                    best = (projected, cycle, anchor, repeat)
    return best


def resolve_price_unit(price: float) -> float:
    """Per-instrument Square-of-Nine chart scale, as a power of ten.

    ``geometry.square_of_nine`` steps by ``d(P)/P = 2*(theta/180)/sqrt(P/unit)``,
    so with ``unit`` pinned at 1.0 the angular resolution is a pure function of
    the price level: 0.19 % per 45 degrees for SENSEX at 77k, but 3.2 % for
    NATURALGAS at 275 and ~3 % for a Rs.300 stock.  That makes the "cardinal
    SQ9" gate a no-op for expensive instruments and unreachable for cheap ones
    — an artefact of magnitude, not of geometry.

    Gann's own remedy is the chart scale: a low-priced commodity is plotted at
    a different scale from an index.  So pick the power of ten that puts
    ``sqrt(price/unit)`` into [60, 600] — i.e. between 0.08 % and 0.83 % per
    45 degrees.  Powers of ten preserve the natural-number character of the
    wheel; only the scale moves.

    Of the seven symbols in the legacy universe only NATURALGAS changes
    (unit 1.0 -> 0.01); NIFTY/BANKNIFTY/SENSEX/CRUDEOIL/GOLD/SILVERM already
    land inside the band at unit 1.0.
    """
    value = abs(float(price))
    if not math.isfinite(value) or value <= 0:
        return 1.0
    exponent = 0
    while math.sqrt(value / (10.0 ** exponent)) > 600.0:
        exponent += 1
        if exponent > 12:  # pragma: no cover - hard guard
            break
    while math.sqrt(value / (10.0 ** exponent)) < 60.0:
        exponent -= 1
        if exponent < -12:  # pragma: no cover - hard guard
            break
    return float(10.0 ** exponent)


def cycle_catalog() -> list[dict[str, object]]:
    """Serialisable view of the whole library, for persistence and audit."""
    return [
        {
            "key": cycle.key,
            "family": cycle.family,
            "days": cycle.days,
            "label": cycle.label,
            "source": cycle.source,
            "testable_in_principle": cycle.testable,
        }
        for cycle in all_cycles()
    ]


__all__ = [
    "SOLAR_YEAR_DAYS",
    "MAX_TESTABLE_DAYS",
    "CycleDef",
    "CausalAnchor",
    "CycleProjection",
    "all_cycles",
    "calendar_cycles",
    "fractional_year_cycles",
    "anniversary_cycles",
    "week_cycles",
    "sq9_time_cycles",
    "master_long_cycles",
    "testable_cycles",
    "untestable_cycles",
    "seasonal_dates",
    "SEASONAL_POINTS",
    "SEASONAL_SOURCE",
    "causal_anchors",
    "project_cycle_dates",
    "next_projection",
    "resolve_price_unit",
    "cycle_catalog",
]
