"""Practice-faithful re-test of Gann time cycles.

The shipped mapper (``gann_tp_delta.cycle_prominence``) scored each cycle
STANDALONE, from every confirmed 5-bar pivot, with a fixed +/-3 SESSION
tolerance, one repetition per anchor, and a turn defined as a confirmed 5-bar
pivot of at least median magnitude.  External research on how the method is
actually taught says five of those six choices diverge from practice.  This
module re-runs the measurement under the practice-implied choices, one arm at
a time, keeping every guard that made the first result trustworthy.

Guards preserved verbatim in spirit and re-implemented here so each arm gets
its OWN family:
  * minimum non-overlapping observations (disjoint tolerance windows)
  * empirical null = union coverage of turn windows in the same region
  * global Benjamini-Hochberg FDR across the whole grid OF THAT ARM
  * era stability (in-sample halves)
  * out-of-sample holdout (last 30 %, scored once)
  * a matched random placebo run through the IDENTICAL pipeline
  * NEW: a coverage guard — a cell whose null already exceeds 0.50 is
    reported UNTESTABLE_BY_COVERAGE rather than scored, because no hit rate
    can distinguish the hypothesis from chance there.

Nothing here is wired to a lane.  Research only.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from typing import Any, Callable, Sequence

import numpy as np
import pandas as pd

from gann_tp_delta.cycle_prominence import benjamini_hochberg, bh_adjusted, binom_sf
from gann_tp_delta.cycles import CycleDef, all_cycles, causal_anchors, resolve_price_unit

MIN_OBSERVATIONS = 20
MIN_ERA_OBSERVATIONS = 8
MIN_OOS_OBSERVATIONS = 8
IS_FRACTION = 0.70
FDR_Q = 0.10
MAX_NULL_COVERAGE = 0.50


# ── tolerance conventions ──────────────────────────────────────────────────


def tol_fixed_sessions(days: int) -> tuple[str, int]:
    """Shipped convention: +/-3 trading SESSIONS, flat across cycle lengths."""
    return ("sessions", 3)


def tol_scaled_calendar(days: int) -> tuple[str, int]:
    """The only explicit published convention found (C-grade practitioner):
    +/-1 day short-term, +/-3 intermediate, +/-7 long-term, CALENDAR days."""
    if days < 60:
        return ("calendar", 1)
    if days <= 180:
        return ("calendar", 3)
    return ("calendar", 7)


def tol_flat_calendar3(days: int) -> tuple[str, int]:
    """Calendar-day reading of the shipped tolerance — isolates the unit."""
    return ("calendar", 3)


# ── turn definitions ───────────────────────────────────────────────────────


def turns_pivot5_median(frame: pd.DataFrame) -> list[int]:
    """Shipped: confirmed 5-bar swing pivots of at least median magnitude."""
    pivots = causal_anchors(frame, left=5, right=5)
    if not pivots:
        return []
    magnitudes = sorted(p.magnitude for p in pivots)
    mid = len(magnitudes) // 2
    floor = magnitudes[mid] if len(magnitudes) % 2 else (magnitudes[mid - 1] + magnitudes[mid]) / 2.0
    return sorted({p.index for p in pivots if p.magnitude >= floor})


def turns_pivot5_all(frame: pd.DataFrame) -> list[int]:
    pivots = causal_anchors(frame, left=5, right=5)
    return sorted({p.index for p in pivots})


def turns_bar_reversal(frame: pd.DataFrame) -> list[int]:
    """Practitioner definition (B-grade, Cycles Research Institute summary):
    "reversal is higher top and higher bottom compared to the previous day or
    vice versa. Bar reversals at cycle ends are extremely important points for
    reversal in the trend."

    Made mechanical: an up-reversal at bar i requires high[i]>high[i-1] and
    low[i]>low[i-1] AND bar i-1 to have printed the lowest low of the prior 4
    sessions (so it reverses an actual down-leg rather than marking mid-trend
    drift).  Down-reversal is the mirror.
    """
    if frame is None or len(frame.index) < 6:
        return []
    high = frame["high"].astype(float).to_numpy()
    low = frame["low"].astype(float).to_numpy()
    out: list[int] = []
    for i in range(5, len(high)):
        prior_lo = low[i - 5 : i]
        prior_hi = high[i - 5 : i]
        up = high[i] > high[i - 1] and low[i] > low[i - 1] and low[i - 1] <= prior_lo.min()
        dn = high[i] < high[i - 1] and low[i] < low[i - 1] and high[i - 1] >= prior_hi.max()
        if up or dn:
            out.append(i)
    return out


# ── anchor rules ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Anchor:
    kind: str
    index: int
    pivot_date: date
    price: float
    confirmed_date: date
    magnitude: float


def _to_anchor(a) -> Anchor:
    return Anchor(a.kind, a.index, a.pivot_date, a.price, a.confirmed_date, a.magnitude)


def anchors_all_pivots(frame: pd.DataFrame) -> list[Anchor]:
    """Shipped: every confirmed 5-bar swing pivot."""
    return [_to_anchor(a) for a in causal_anchors(frame, left=5, right=5)]


def anchors_major(frame: pd.DataFrame) -> list[Anchor]:
    """Practice: only "the most recent, obvious and significant" extremes.

    No source supplies an operational filter, so this is OUR parameterisation:
    a 41-session confirmed pivot (left=right=20) whose swing magnitude is in
    the top 40 % of that instrument's own such pivots.  The placebo arm carries
    the multiple-comparison burden of the choice.
    """
    raw = [_to_anchor(a) for a in causal_anchors(frame, left=20, right=20)]
    if not raw:
        return []
    cut = float(np.quantile([a.magnitude for a in raw], 0.60))
    return [a for a in raw if a.magnitude >= cut]


def anchors_extremes(frame: pd.DataFrame) -> list[Anchor]:
    """The narrowest reading: running all-time high / all-time low of the
    frame, re-anchored each time a new extreme prints (Gann's yearly/ATH
    anchors).  Confirmed the session it prints (an extreme is knowable live).
    """
    if frame is None or frame.empty:
        return []
    high = frame["high"].astype(float).to_numpy()
    low = frame["low"].astype(float).to_numpy()
    dates = [pd.Timestamp(v).date() for v in frame["time"]]
    out: list[Anchor] = []
    best_hi, best_lo = -math.inf, math.inf
    for i in range(len(high)):
        if high[i] > best_hi:
            best_hi = high[i]
            out.append(Anchor("swing_high", i, dates[i], float(high[i]), dates[i], 0.0))
        if low[i] < best_lo:
            best_lo = low[i]
            out.append(Anchor("swing_low", i, dates[i], float(low[i]), dates[i], 0.0))
    return out


# ── projections ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Proj:
    anchor_index: int
    anchor_price: float
    repeat: int
    centre: int          # session index nearest the projected date
    lo: int              # inclusive session-index window
    hi: int


def _window(
    session_dates: list[date],
    session_ordinals: np.ndarray,
    target: date,
    unit: str,
    k: int,
) -> tuple[int, int, int] | None:
    """Session-index window around ``target`` under the tolerance convention."""
    n = len(session_dates)
    if n == 0:
        return None
    if target < session_dates[0] or target > session_dates[-1]:
        return None
    t = target.toordinal()
    if unit == "calendar":
        lo = int(np.searchsorted(session_ordinals, t - k, side="left"))
        hi = int(np.searchsorted(session_ordinals, t + k, side="right")) - 1
        if hi < lo:
            return None
        centre = int(np.argmin(np.abs(session_ordinals - t)))
        return centre, lo, hi
    centre = int(np.argmin(np.abs(session_ordinals - t)))
    return centre, max(centre - k, 0), min(centre + k, n - 1)


def project(
    anchors: Sequence[Anchor],
    cycle_days: int,
    session_dates: list[date],
    session_ordinals: np.ndarray,
    *,
    tolerance: Callable[[int], tuple[str, int]],
    repeats: int,
) -> list[Proj]:
    unit, k = tolerance(int(cycle_days))
    out: list[Proj] = []
    for a in anchors:
        for r in range(1, int(repeats) + 1):
            target = a.pivot_date + timedelta(days=int(cycle_days) * r)
            if target < a.confirmed_date:
                continue
            w = _window(session_dates, session_ordinals, target, unit, k)
            if w is None:
                continue
            centre, lo, hi = w
            out.append(Proj(a.index, a.price, r, centre, lo, hi))
    return out


def thin(projections: Sequence[Proj]) -> list[Proj]:
    """Greedy disjoint-window selection — the non-overlap guard."""
    kept: list[Proj] = []
    last = -10**9
    for p in sorted(projections, key=lambda p: (p.lo, p.anchor_index, p.repeat)):
        if p.lo > last:
            kept.append(p)
            last = p.hi
    return kept


def exact_null(
    session_ordinals: np.ndarray,
    turn_set: set[int],
    lo: int,
    hi: int,
    unit: str,
    k: int,
) -> float:
    """P(a UNIFORMLY RANDOM admissible date's window contains a turn).

    The shipped mapper approximated this with the union coverage of +/-k
    SESSION windows around each turn, which is exact only when the projection
    window is itself exactly +/-k sessions.  Under a CALENDAR-day tolerance the
    projection window is narrower than that (+/-1 calendar day usually admits a
    single session), so the shipped-style null over-states the chance rate and
    the test becomes conservative — which is precisely the "fewer significant
    cells than chance predicts" signature seen in the first run.

    This computes the null by CONSTRUCTING the window the identical way for
    every candidate centre session in the region.  Exact by construction, for
    every tolerance convention.
    """
    span = max(hi - lo, 0)
    if span <= 0:
        return 0.0
    n = len(session_ordinals)
    hit = 0
    for centre in range(lo, hi):
        if unit == "calendar":
            t = int(session_ordinals[centre])
            a = int(np.searchsorted(session_ordinals, t - k, side="left"))
            b = int(np.searchsorted(session_ordinals, t + k, side="right")) - 1
        else:
            a, b = max(centre - k, 0), min(centre + k, n - 1)
        if any(i in turn_set for i in range(a, b + 1)):
            hit += 1
    return hit / float(span)


def null_coverage(turn_indices: Sequence[int], lo: int, hi: int, half_width: int) -> float:
    span = max(hi - lo, 0)
    if span <= 0:
        return 0.0
    covered: set[int] = set()
    for t in turn_indices:
        for i in range(t - half_width, t + half_width + 1):
            if lo <= i < hi:
                covered.add(i)
    return len(covered) / float(span)


# ── price-level confluence ─────────────────────────────────────────────────

_SQ9_DEGREES = (45, 90, 135, 180, 225, 270, 315, 360)


def sq9_levels(anchor_price: float, jitter: random.Random | None = None) -> list[float]:
    """Square-of-Nine price levels either side of the anchor price, using the
    shipped per-instrument chart scale.  ``jitter`` (placebo) displaces each
    level by a uniform draw inside its own gap, preserving level DENSITY while
    destroying the geometry.
    """
    unit = resolve_price_unit(anchor_price)
    root = math.sqrt(max(anchor_price / unit, 1e-6))
    out: list[float] = []
    for sign in (1.0, -1.0):
        for deg in _SQ9_DEGREES:
            d = float(deg)
            if jitter is not None:
                d = d + jitter.uniform(-22.5, 22.5)
            price = ((root + (d / 180.0) * sign) ** 2) * unit
            if price > 0:
                out.append(price)
    return sorted(out)


def price_confluence(
    frame_high: np.ndarray,
    frame_low: np.ndarray,
    proj: Proj,
    atr: np.ndarray,
    jitter: random.Random | None = None,
) -> bool:
    """True if, anywhere inside the projected window, price traded within one
    ATR of a Square-of-Nine level projected from the SAME anchor's price.
    This is the documented joint construct: cycle date AND price level.
    """
    levels = sq9_levels(proj.anchor_price, jitter=jitter)
    if not levels:
        return False
    for i in range(proj.lo, proj.hi + 1):
        band = atr[i] if i < len(atr) else atr[-1]
        if not math.isfinite(band) or band <= 0:
            continue
        for level in levels:
            if frame_low[i] - band <= level <= frame_high[i] + band:
                return True
    return False


def true_range_atr(frame: pd.DataFrame, window: int = 20) -> np.ndarray:
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    close = frame["close"].astype(float)
    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    atr = tr.rolling(window, min_periods=5).mean().bfill().to_numpy()
    return atr


# ── one scored cell ────────────────────────────────────────────────────────


@dataclass
class Cell:
    arm: str
    underlying: str
    instrument_class: str
    cycle_key: str
    family: str
    cycle_days: int
    is_placebo: bool
    status: str = "UNTESTABLE"
    reason: str | None = None
    agree_threshold: int = 0
    #: fraction of in-sample sessions covered by AT LEAST ONE projection window
    #: of this cell — the skeptics' base-rate/coverage diagnostic.  A cell whose
    #: projections tile the calendar cannot be informative whatever it scores.
    proj_coverage: float | None = None
    tol_half_width: int = 0
    n_obs: int = 0
    hits: int = 0
    hit_rate: float | None = None
    null_rate: float | None = None
    lift: float | None = None
    p_value: float | None = None
    p_fdr: float | None = None
    fdr_significant: bool = False
    era_stable: bool = False
    era1_n: int = 0
    era2_n: int = 0
    oos_n: int = 0
    oos_hits: int = 0
    oos_hit_rate: float | None = None
    oos_null_rate: float | None = None
    oos_p: float | None = None
    oos_confirms: bool = False

    def row(self) -> dict[str, Any]:
        return asdict(self)


def _score_region(
    projections: Sequence[Proj],
    turn_set: set[int],
    turn_list: Sequence[int],
    lo: int,
    hi: int,
    half_width: int,
    confluence: Callable[[Proj], bool] | None,
    *,
    session_ordinals: np.ndarray | None = None,
    unit: str = "sessions",
    k: int = 3,
) -> tuple[int, int, float, float]:
    region = [p for p in projections if lo <= p.centre < hi]
    if confluence is not None:
        region = [p for p in region if confluence(p)]
    kept = thin(region)
    hits = 0
    for p in kept:
        if any(i in turn_set for i in range(p.lo, p.hi + 1)):
            hits += 1
    if session_ordinals is not None:
        null = exact_null(session_ordinals, turn_set, lo, hi, unit, k)
    else:
        region_turns = [t for t in turn_list if lo <= t < hi]
        null = null_coverage(region_turns, lo, hi, half_width)
    n = len(kept)
    return n, hits, (hits / n if n else 0.0), null


@dataclass
class ArmSpec:
    name: str
    anchors: Callable[[pd.DataFrame], list[Anchor]]
    turns: Callable[[pd.DataFrame], list[int]]
    tolerance: Callable[[int], tuple[str, int]]
    repeats: int
    confluence: bool = False
    note: str = ""


def score_instrument(
    spec: ArmSpec,
    underlying: str,
    instrument_class: str,
    frame: pd.DataFrame,
    cycles: Sequence[CycleDef],
    *,
    placebo: bool,
    seed: int,
) -> list[Cell]:
    out: list[Cell] = []
    n_sessions = len(frame.index)
    if n_sessions < 150:
        return out
    session_dates = [pd.Timestamp(v).date() for v in frame["time"]]
    session_ordinals = np.array([d.toordinal() for d in session_dates])
    anchors = spec.anchors(frame)
    turn_list = spec.turns(frame)
    if not anchors or not turn_list:
        return out
    turn_set = set(turn_list)
    split = int(n_sessions * IS_FRACTION)
    era_mid = split // 2
    high = frame["high"].astype(float).to_numpy()
    low = frame["low"].astype(float).to_numpy()
    atr = true_range_atr(frame)
    jitter = random.Random(seed ^ 0x5EED) if (placebo and spec.confluence) else None

    for cycle in cycles:
        unit, k = spec.tolerance(int(cycle.days))
        half = k if unit == "sessions" else max(int(round(k * 5.0 / 7.0)), 0)
        conf = None
        if spec.confluence:
            conf = lambda p: price_confluence(high, low, p, atr, jitter=jitter)
        projections = project(
            anchors, int(cycle.days), session_dates, session_ordinals,
            tolerance=spec.tolerance, repeats=spec.repeats,
        )
        cell = Cell(
            arm=spec.name, underlying=underlying, instrument_class=instrument_class,
            cycle_key=cycle.key, family=cycle.family, cycle_days=int(cycle.days),
            is_placebo=placebo,
        )
        covered: set[int] = set()
        for p in projections:
            if 0 <= p.centre < split:
                covered.update(range(max(p.lo, 0), min(p.hi, split - 1) + 1))
        cell.proj_coverage = len(covered) / float(split) if split else None
        cell.tol_half_width = half
        nullargs = dict(session_ordinals=session_ordinals, unit=unit, k=k)
        n, hits, rate, null = _score_region(projections, turn_set, turn_list, 0, split, half, conf, **nullargs)
        cell.n_obs, cell.hits, cell.null_rate = n, hits, null
        if n < MIN_OBSERVATIONS:
            cell.status = "UNTESTABLE"
            cell.reason = f"{n} non-overlapping in-sample observations < {MIN_OBSERVATIONS}"
            out.append(cell)
            continue
        if null > MAX_NULL_COVERAGE:
            cell.status = "UNTESTABLE_BY_COVERAGE"
            cell.reason = f"null coverage {null:.2f} > {MAX_NULL_COVERAGE}; no hit rate is informative"
            out.append(cell)
            continue
        cell.hit_rate, cell.lift = rate, rate - null
        cell.p_value = binom_sf(hits, n, null)

        e1n, _e1h, e1r, e1null = _score_region(projections, turn_set, turn_list, 0, era_mid, half, conf, **nullargs)
        e2n, _e2h, e2r, e2null = _score_region(projections, turn_set, turn_list, era_mid, split, half, conf, **nullargs)
        cell.era1_n, cell.era2_n = e1n, e2n
        cell.era_stable = bool(
            e1n >= MIN_ERA_OBSERVATIONS and e2n >= MIN_ERA_OBSERVATIONS
            and e1r > e1null and e2r > e2null
        )
        on, oh, orate, onull = _score_region(projections, turn_set, turn_list, split, n_sessions, half, conf, **nullargs)
        cell.oos_n, cell.oos_hits, cell.oos_null_rate = on, oh, onull
        if on >= MIN_OOS_OBSERVATIONS:
            cell.oos_hit_rate = orate
            cell.oos_p = binom_sf(oh, on, onull)
            cell.oos_confirms = bool(orate > onull and cell.oos_p <= 0.10)
        cell.status = "TESTED_NOT_PROMINENT"
        out.append(cell)
    return out


def finalise(cells: list[Cell], q: float = FDR_Q) -> dict[str, Any]:
    """BH-FDR across the whole grid of ONE arm+placebo-flag family."""
    tested = [c for c in cells if c.p_value is not None]
    if not tested:
        return {"tested": 0, "bh_threshold_rank1": None, "bh_critical_p": None, "rejected": 0}
    p = [float(c.p_value) for c in tested]
    flags = benjamini_hochberg(p, q=q)
    adj = bh_adjusted(p)
    for c, f, a in zip(tested, flags, adj):
        c.fdr_significant = bool(f)
        c.p_fdr = float(a)
        if f and c.era_stable and c.oos_confirms:
            c.status = "PROMINENT"
    n = len(p)
    ordered = sorted(p)
    critical = None
    for rank, value in enumerate(ordered, start=1):
        if value <= q * rank / n:
            critical = q * rank / n
    return {
        "tested": n,
        "min_p": min(p),
        "bh_threshold_rank1": q / n,
        "bh_critical_p": critical,
        "rejected": sum(flags),
        "expected_raw_p05_by_chance": 0.05 * n,
        "observed_raw_p05": sum(1 for v in p if v <= 0.05),
    }


__all__ = [
    "ArmSpec", "Cell", "Anchor", "Proj",
    "anchors_all_pivots", "anchors_major", "anchors_extremes",
    "turns_pivot5_median", "turns_pivot5_all", "turns_bar_reversal",
    "tol_fixed_sessions", "tol_scaled_calendar", "tol_flat_calendar3",
    "score_instrument", "finalise", "sq9_levels", "true_range_atr",
]
