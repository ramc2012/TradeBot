"""Per-instrument Gann time-cycle prominence mapping.

The owner's instruction is "map which is prominent each instrument … trade
only the strong cycles as per instrument behaviour".  That is only worth
building if it is falsifiable, and the naive version is not: ~25 cycles x ~220
instruments is ~5,500 hypotheses, so at p<0.05 roughly 275 "prominent" cycles
appear from PURE NOISE.  A confident, entirely fictitious map is the default
outcome of a naive scan.

Every guard below exists to prevent that, and each is reported rather than
folded into a single number:

1. **Minimum non-overlapping repetitions** (:data:`MIN_OBSERVATIONS`, 20).
   Projections from nearby anchors land on nearly the same date and are not
   independent observations, so they are thinned to disjoint tolerance
   windows before counting.  A cycle with fewer than 20 surviving
   observations is reported ``UNTESTABLE`` — never "weak".  20 is the point
   below which the one-sided binomial test cannot reach p<0.05 for a
   *plausible* effect: against a typical null hit-rate of ~0.30, detecting a
   lift to 0.55 with ~80 % power needs n≈20-25; at n=10 even a hit-rate of
   0.70 fails to clear 0.05 after correction.
2. **A stated null**.  The null is not 0.5.  It is the empirical probability
   that a *uniformly random* date lands inside a turn window, computed as the
   union of +/-tolerance windows around every genuine turn divided by the
   number of sessions in the region.  This is what makes a 60 % hit rate
   unimpressive when turns already cover 55 % of the calendar.
3. **Benjamini-Hochberg FDR** across the full instrument x cycle grid, with
   raw and corrected p-values both reported.
4. **Era stability** — the in-sample region is halved and a cycle counts only
   if it beats the null in BOTH halves.  Single-era survivors are exactly the
   failure that killed two prior candidates in this repo.
5. **Out-of-sample holdout** — prominence is fitted on the first 70 % of
   history and confirmed on the held-out last 30 %, scored once and reported
   separately.  There is no combined in-sample ranking.
6. **A placebo control** — randomly chosen cycle lengths run through the
   IDENTICAL pipeline.  If Gann's cycles do not clearly beat random lengths,
   that is the finding, and it is reported as such.

Lookahead
---------
Anchors are :func:`gann_tp_delta.cycles.causal_anchors` — a pivot is emitted
with the date it would first have been confirmable (``right`` sessions later)
and a projection is dropped if it would precede that date.  Turn
identification (the DEPENDENT variable) does use both sides of the bar, which
is legitimate: we are measuring whether a turn happened, not trading it.  The
asymmetry is deliberate and is the whole difference between measurement and
lookahead.
"""
from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Iterable, Sequence

import pandas as pd

from gann_tp_delta.cycles import (
    CausalAnchor,
    CycleDef,
    CycleProjection,
    causal_anchors,
    project_cycle_dates,
    testable_cycles,
    untestable_cycles,
)

#: Minimum non-overlapping observations before a cycle is scored at all.
MIN_OBSERVATIONS = 20
#: Minimum observations in the held-out block for the OOS verdict to count.
MIN_OOS_OBSERVATIONS = 8
#: Minimum observations per era half for the stability verdict to count.
MIN_ERA_OBSERVATIONS = 8
#: +/- trading sessions around a projected date that counts as "on time".
#: Fixed across cycles so windows are comparable; a proportional tolerance
#: would silently hand long cycles a wider net and a higher base rate.
TOLERANCE_SESSIONS = 3
#: Swing-confirmation half-width for turn identification, in daily sessions.
TURN_PIVOT_BARS = 5
#: In-sample fraction; the remainder is the untouched holdout.
IS_FRACTION = 0.70
#: Benjamini-Hochberg false-discovery rate.
FDR_Q = 0.10


# ── Turns: the dependent variable ──────────────────────────────────────────


@dataclass(frozen=True)
class Turn:
    index: int
    turn_date: date
    kind: str
    magnitude: float   # swing range around the pivot, as a fraction of price


def identify_turns(
    frame: pd.DataFrame,
    *,
    pivot_bars: int = TURN_PIVOT_BARS,
    min_magnitude_pct: float | None = None,
) -> list[Turn]:
    """Genuine daily turns: confirmed swing pivots of material size.

    ``min_magnitude_pct`` defaults to the MEDIAN swing magnitude of the
    instrument's own confirmed pivots, so "material" is instrument-relative
    and roughly half of all pivots qualify.  A fixed percentage would make
    NATURALGAS all-turns and NIFTY no-turns.
    """
    if frame is None or frame.empty:
        return []
    pivots = causal_anchors(frame, left=pivot_bars, right=pivot_bars)
    if not pivots:
        return []
    magnitudes = sorted(p.magnitude for p in pivots)
    if min_magnitude_pct is None:
        mid = len(magnitudes) // 2
        floor = magnitudes[mid] if len(magnitudes) % 2 else (magnitudes[mid - 1] + magnitudes[mid]) / 2.0
    else:
        floor = float(min_magnitude_pct)
    turns = [
        Turn(p.index, p.pivot_date, p.kind, p.magnitude)
        for p in pivots
        if p.magnitude >= floor
    ]
    return sorted(turns, key=lambda t: t.index)


def turn_window_coverage(
    turns: Sequence[Turn],
    *,
    lo: int,
    hi: int,
    tolerance: int = TOLERANCE_SESSIONS,
) -> float:
    """P(a uniformly random session in [lo, hi) falls inside a turn window).

    This is the null hit-rate.  Computed as the union of +/-tolerance windows
    so overlapping turns are not double-counted.
    """
    span = max(int(hi) - int(lo), 0)
    if span <= 0:
        return 0.0
    covered: set[int] = set()
    for turn in turns:
        for index in range(turn.index - tolerance, turn.index + tolerance + 1):
            if lo <= index < hi:
                covered.add(index)
    return len(covered) / float(span)


# ── Observation thinning ───────────────────────────────────────────────────


def thin_projections(
    projections: Sequence[CycleProjection],
    *,
    tolerance: int = TOLERANCE_SESSIONS,
) -> list[CycleProjection]:
    """Greedy selection of projections with DISJOINT tolerance windows.

    Two anchors three days apart produce two projections three days apart:
    the same turn satisfies both, so counting both inflates n and understates
    the p-value.  Requiring the windows not to touch makes each surviving
    observation a genuinely separate test of the cycle.
    """
    ordered = sorted(
        (p for p in projections if p.projected_index >= 0),
        key=lambda p: (p.projected_index, p.anchor_index),
    )
    kept: list[CycleProjection] = []
    last_hi = -10**9
    for projection in ordered:
        if projection.window_lo > last_hi:
            kept.append(projection)
            last_hi = projection.window_hi
    return kept


def _hits(projections: Sequence[CycleProjection], turn_indices: set[int]) -> list[CycleProjection]:
    out: list[CycleProjection] = []
    for projection in projections:
        for index in range(projection.window_lo, projection.window_hi + 1):
            if index in turn_indices:
                out.append(projection)
                break
    return out


# ── Binomial tail, without a scipy hard dependency ─────────────────────────


def binom_sf(hits: int, trials: int, probability: float) -> float:
    """P(X >= hits) for X ~ Binomial(trials, probability). One-sided."""
    if trials <= 0:
        return 1.0
    p = min(max(float(probability), 1e-12), 1.0 - 1e-12)
    k = max(int(hits), 0)
    if k <= 0:
        return 1.0
    if k > trials:
        return 0.0
    total = 0.0
    log_p, log_q = math.log(p), math.log(1.0 - p)
    for i in range(k, trials + 1):
        log_term = (
            math.lgamma(trials + 1)
            - math.lgamma(i + 1)
            - math.lgamma(trials - i + 1)
            + i * log_p
            + (trials - i) * log_q
        )
        total += math.exp(log_term)
    return min(max(total, 0.0), 1.0)


def benjamini_hochberg(p_values: Sequence[float], q: float = FDR_Q) -> list[bool]:
    """BH step-up. Returns a per-hypothesis reject flag, input order."""
    n = len(p_values)
    if n == 0:
        return []
    ordered = sorted(range(n), key=lambda i: p_values[i])
    threshold_rank = -1
    for rank, position in enumerate(ordered, start=1):
        if p_values[position] <= q * rank / n:
            threshold_rank = rank
    flags = [False] * n
    if threshold_rank > 0:
        for rank, position in enumerate(ordered, start=1):
            if rank <= threshold_rank:
                flags[position] = True
    return flags


def bh_adjusted(p_values: Sequence[float]) -> list[float]:
    """BH-adjusted p-values (q-values), input order."""
    n = len(p_values)
    if n == 0:
        return []
    ordered = sorted(range(n), key=lambda i: p_values[i])
    adjusted = [1.0] * n
    running = 1.0
    for rank in range(n, 0, -1):
        position = ordered[rank - 1]
        value = min(1.0, p_values[position] * n / rank)
        running = min(running, value)
        adjusted[position] = running
    return adjusted


# ── Scoring ────────────────────────────────────────────────────────────────


@dataclass
class CycleScore:
    underlying: str
    cycle_key: str
    family: str
    cycle_days: int
    status: str = "UNTESTABLE"          # PROMINENT | TESTED_NOT_PROMINENT | UNTESTABLE
    untestable_reason: str | None = None
    is_observations: int = 0
    is_hits: int = 0
    is_hit_rate: float | None = None
    null_rate: float | None = None
    lift: float | None = None
    p_value: float | None = None
    p_value_fdr: float | None = None
    fdr_significant: bool = False
    era1_observations: int = 0
    era1_hit_rate: float | None = None
    era2_observations: int = 0
    era2_hit_rate: float | None = None
    era_stable: bool = False
    oos_observations: int = 0
    oos_hits: int = 0
    oos_hit_rate: float | None = None
    oos_null_rate: float | None = None
    oos_p_value: float | None = None
    oos_confirms: bool = False
    median_turn_magnitude_pct: float | None = None
    is_placebo: bool = False

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


def _score_region(
    projections: Sequence[CycleProjection],
    turns: Sequence[Turn],
    *,
    lo: int,
    hi: int,
    tolerance: int,
) -> tuple[int, int, float, float, float | None]:
    """(n_obs, hits, hit_rate, null_rate, median_hit_magnitude)."""
    region = [p for p in projections if lo <= p.projected_index < hi]
    thinned = thin_projections(region, tolerance=tolerance)
    region_turns = [t for t in turns if lo <= t.index < hi]
    turn_index = {t.index: t for t in region_turns}
    hit_set = set(turn_index)
    hits = _hits(thinned, hit_set)
    null_rate = turn_window_coverage(region_turns, lo=lo, hi=hi, tolerance=tolerance)
    magnitudes: list[float] = []
    for projection in hits:
        best: Turn | None = None
        for index in range(projection.window_lo, projection.window_hi + 1):
            candidate = turn_index.get(index)
            if candidate is not None and (best is None or candidate.magnitude > best.magnitude):
                best = candidate
        if best is not None:
            magnitudes.append(best.magnitude)
    magnitudes.sort()
    median_magnitude = None
    if magnitudes:
        mid = len(magnitudes) // 2
        median_magnitude = (
            magnitudes[mid] if len(magnitudes) % 2 else (magnitudes[mid - 1] + magnitudes[mid]) / 2.0
        )
    n_obs = len(thinned)
    hit_rate = len(hits) / n_obs if n_obs else 0.0
    return n_obs, len(hits), hit_rate, null_rate, median_magnitude


def score_instrument(
    underlying: str,
    frame: pd.DataFrame,
    cycles: Sequence[CycleDef],
    *,
    tolerance: int = TOLERANCE_SESSIONS,
    pivot_bars: int = TURN_PIVOT_BARS,
    anchor_bars: int = TURN_PIVOT_BARS,
    is_fraction: float = IS_FRACTION,
    min_observations: int = MIN_OBSERVATIONS,
    placebo: bool = False,
) -> list[CycleScore]:
    """Score every cycle for one instrument.  No FDR here — that is global."""
    scores: list[CycleScore] = []
    if frame is None or frame.empty:
        return scores
    n_sessions = len(frame.index)
    session_dates = [pd.Timestamp(value).date() for value in frame["time"]]
    anchors = causal_anchors(frame, left=anchor_bars, right=anchor_bars)
    turns = identify_turns(frame, pivot_bars=pivot_bars)
    if not anchors or not turns:
        return scores

    split = int(n_sessions * float(is_fraction))
    era_mid = split // 2

    for cycle in cycles:
        projections = project_cycle_dates(anchors, [cycle], session_dates, tolerance_sessions=tolerance)
        score = CycleScore(
            underlying=underlying,
            cycle_key=cycle.key,
            family=cycle.family,
            cycle_days=int(cycle.days),
            is_placebo=bool(placebo),
        )
        n_obs, hits, hit_rate, null_rate, magnitude = _score_region(
            projections, turns, lo=0, hi=split, tolerance=tolerance
        )
        score.is_observations = n_obs
        score.is_hits = hits
        score.null_rate = null_rate
        score.median_turn_magnitude_pct = magnitude
        if n_obs < int(min_observations):
            score.status = "UNTESTABLE"
            score.untestable_reason = (
                f"only {n_obs} non-overlapping observations in the in-sample region "
                f"({split} sessions), below the {min_observations} minimum"
            )
            scores.append(score)
            continue

        score.is_hit_rate = hit_rate
        score.lift = hit_rate - null_rate
        score.p_value = binom_sf(hits, n_obs, null_rate)

        e1_obs, _e1_hits, e1_rate, e1_null, _ = _score_region(
            projections, turns, lo=0, hi=era_mid, tolerance=tolerance
        )
        e2_obs, _e2_hits, e2_rate, e2_null, _ = _score_region(
            projections, turns, lo=era_mid, hi=split, tolerance=tolerance
        )
        score.era1_observations, score.era1_hit_rate = e1_obs, (e1_rate if e1_obs else None)
        score.era2_observations, score.era2_hit_rate = e2_obs, (e2_rate if e2_obs else None)
        score.era_stable = bool(
            e1_obs >= MIN_ERA_OBSERVATIONS
            and e2_obs >= MIN_ERA_OBSERVATIONS
            and e1_rate > e1_null
            and e2_rate > e2_null
        )

        o_obs, o_hits, o_rate, o_null, _ = _score_region(
            projections, turns, lo=split, hi=n_sessions, tolerance=tolerance
        )
        score.oos_observations, score.oos_hits = o_obs, o_hits
        score.oos_null_rate = o_null
        if o_obs >= MIN_OOS_OBSERVATIONS:
            score.oos_hit_rate = o_rate
            score.oos_p_value = binom_sf(o_hits, o_obs, o_null)
            score.oos_confirms = bool(o_rate > o_null and score.oos_p_value <= 0.10)
        score.status = "TESTED_NOT_PROMINENT"
        scores.append(score)
    return scores


def placebo_cycles(count: int, *, seed: int, low: int = 30, high: int = 400) -> list[CycleDef]:
    """Random cycle lengths for the control arm, same count as the real set."""
    rng = random.Random(seed)
    out: list[CycleDef] = []
    seen: set[int] = set()
    while len(out) < max(int(count), 0):
        days = rng.randint(int(low), int(high))
        if days in seen:
            continue
        seen.add(days)
        out.append(
            CycleDef(f"placebo_{days}", "placebo", days, f"random {days}-day control", "placebo control")
        )
    return sorted(out, key=lambda c: c.days)


@dataclass
class MappingResult:
    scores: list[CycleScore] = field(default_factory=list)
    placebo_scores: list[CycleScore] = field(default_factory=list)
    untestable: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


def finalise(
    scores: list[CycleScore],
    placebo_scores: list[CycleScore],
    *,
    q: float = FDR_Q,
) -> None:
    """Apply BH-FDR across the whole grid, then resolve PROMINENT in place.

    The genuine and placebo arms are corrected SEPARATELY over their own
    grids, so the placebo comparison is like-for-like: the question is whether
    genuine Gann cycles survive at a higher rate than random lengths do under
    an identical correction.
    """
    for arm in (scores, placebo_scores):
        tested = [s for s in arm if s.status != "UNTESTABLE" and s.p_value is not None]
        if not tested:
            continue
        p_values = [float(s.p_value) for s in tested]
        flags = benjamini_hochberg(p_values, q=q)
        adjusted = bh_adjusted(p_values)
        for score, flag, value in zip(tested, flags, adjusted):
            score.fdr_significant = bool(flag)
            score.p_value_fdr = float(value)
            if flag and score.era_stable and score.oos_confirms:
                score.status = "PROMINENT"


def prominence_summary(scores: Sequence[CycleScore], placebo_scores: Sequence[CycleScore]) -> dict[str, Any]:
    def _counts(arm: Sequence[CycleScore]) -> dict[str, int]:
        return {
            "cells": len(arm),
            "tested": sum(1 for s in arm if s.status != "UNTESTABLE"),
            "untestable": sum(1 for s in arm if s.status == "UNTESTABLE"),
            "raw_p05": sum(1 for s in arm if (s.p_value or 1.0) <= 0.05),
            "fdr_significant": sum(1 for s in arm if s.fdr_significant),
            "era_stable": sum(1 for s in arm if s.era_stable),
            "oos_confirms": sum(1 for s in arm if s.oos_confirms),
            "prominent": sum(1 for s in arm if s.status == "PROMINENT"),
        }

    genuine = _counts(scores)
    placebo = _counts(placebo_scores)
    genuine_rate = genuine["prominent"] / genuine["tested"] if genuine["tested"] else 0.0
    placebo_rate = placebo["prominent"] / placebo["tested"] if placebo["tested"] else 0.0
    return {
        "genuine": genuine,
        "placebo": placebo,
        "genuine_prominent_rate": genuine_rate,
        "placebo_prominent_rate": placebo_rate,
        "beats_placebo": genuine_rate > placebo_rate,
        "verdict": (
            "Gann cycles clear the placebo"
            if genuine_rate > placebo_rate
            else "Gann cycles do NOT beat randomly chosen cycle lengths on this history"
        ),
    }


def ranking(scores: Sequence[CycleScore], underlying: str) -> list[CycleScore]:
    """Per-instrument ranking: PROMINENT first, then by OOS lift, then by lift."""
    subset = [s for s in scores if s.underlying == underlying and s.status != "UNTESTABLE"]
    return sorted(
        subset,
        key=lambda s: (
            0 if s.status == "PROMINENT" else 1,
            -((s.oos_hit_rate or 0.0) - (s.oos_null_rate or 0.0)),
            -(s.lift or 0.0),
        ),
    )


__all__ = [
    "MIN_OBSERVATIONS",
    "MIN_OOS_OBSERVATIONS",
    "MIN_ERA_OBSERVATIONS",
    "TOLERANCE_SESSIONS",
    "TURN_PIVOT_BARS",
    "IS_FRACTION",
    "FDR_Q",
    "Turn",
    "CycleScore",
    "MappingResult",
    "identify_turns",
    "turn_window_coverage",
    "thin_projections",
    "binom_sf",
    "benjamini_hochberg",
    "bh_adjusted",
    "score_instrument",
    "placebo_cycles",
    "finalise",
    "prominence_summary",
    "ranking",
]
