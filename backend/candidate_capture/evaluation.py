"""Prequential evaluation and the promotion gates a model must clear.

PREQUENTIAL MEANS THE SPLIT IS TIME, NOT A SHUFFLE
──────────────────────────────────────────────────
Every split here is chronological: fit on earlier sessions, score later ones,
never the reverse and never interleaved. A random split leaks in this data even
without touching a label, because rows from the same decision set share a spot
path and a chain snapshot — a shuffled fold puts near-duplicates of a training
row into the test set and reports the memorisation as skill.

STANDARD ERRORS ARE CLUSTERED BY SESSION
────────────────────────────────────────
This is the single most repeated statistical lesson in this repo, and it is not
a refinement. Measured previously here: day-clustering inflated the standard
error 2.5x in one analysis, and nominal (unclustered) SEs ran 1.6-4.7x too small
in another. Rows inside one session are not independent draws — they share the
same underlying path, the same regime, the same feed. Treating a session's
several hundred rows as several hundred observations manufactures significance
out of one day's move, which is exactly how a lane acquires an "edge" that
evaporates. Every interval reported here is clustered on `session_date`.

THE GATES ARE ADVERSARIAL BY DESIGN
───────────────────────────────────
They exist to REFUSE promotion, so each one is written to fail closed: a metric
that cannot be computed is a failed gate, never a skipped one. The bar is the
plan's own list, and two gates in particular encode failures this system has
already suffered — dependence on one exceptional trade (a lane whose entire P&L
came from 24 mispriced fills) and robustness to worse-than-observed slippage
(several books whose "net" excluded the spread entirely).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Any, Mapping, Optional, Sequence

import numpy as np

# ── gate thresholds ────────────────────────────────────────────────────────
# A priori structural points, not swept against these returns. Sweeping a
# threshold against the evaluation set is the thing the gates exist to prevent.
MIN_EVAL_ROWS = 200
MIN_EVAL_SESSIONS = 10
# Minimum TRADES ACTUALLY TAKEN. Distinct from MIN_EVAL_ROWS, which counts
# candidates scored: a model that abstains almost always could otherwise clear
# every economic gate on ten lucky picks, because those gates are computed on
# selected_returns while the row-count gate was measuring a different, much
# larger population.
MIN_SELECTED_TRADES = 60
# A model whose picks usually cannot be honestly marked has not been evaluated.
MAX_UNRESOLVED_SELECTION_RATE = 0.20
# t against a session-clustered SE. 2.0 is the conventional two-sided ~5% bar;
# with few clusters it is deliberately hard to clear.
MIN_T_STAT = 2.0
MIN_CLUSTERS_FOR_INFERENCE = 8
# Fraction of capital risked per trade when converting returns to log growth.
# Fixed and modest: this measures whether an edge COMPOUNDS, not how large a
# bet could be made.
KELLY_TEST_FRACTION = 0.02
MAX_DRAWDOWN_LIMIT = 0.25
# The model must beat an always-abstain baseline AND the incumbent champion.
MIN_EDGE_OVER_BASELINE = 0.0
# Stress: recompute every net return with this much extra half-spread per side.
SLIPPAGE_STRESS_EXTRA_PCT = 0.005
# After removing the single best trade, the mean must still be positive.
TOP_TRADE_EXCLUSION_COUNT = 1


@dataclass
class Gate:
    name: str
    passed: bool
    threshold: Any
    measured: Any
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _finite(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


# ── clustered inference ────────────────────────────────────────────────────
def clustered_mean(
    values: Sequence[float], clusters: Sequence[Any]
) -> dict[str, Any]:
    """Mean with a cluster-robust standard error, clustered on `clusters`.

    Uses the standard one-way cluster-robust variance for a mean: the variance
    of the CLUSTER SUMS about the implied mean, not of the individual rows. With
    G clusters the finite-sample correction G/(G-1) is applied, and the t-stat is
    referred to G-1 degrees of freedom rather than n-1 — with 10 sessions that is
    a materially wider interval than the nominal one, which is the point.
    """
    v = np.asarray([x for x in values], dtype=float)
    c = list(clusters)
    if v.size == 0 or v.size != len(c):
        return {
            "n": int(v.size), "clusters": 0, "mean": None, "se": None,
            "t_stat": None, "usable": False,
            "reason": "no rows, or values and clusters disagree in length",
        }

    mean = float(v.mean())
    groups: dict[Any, list[float]] = {}
    for value, key in zip(v, c):
        groups.setdefault(key, []).append(float(value))
    g = len(groups)

    if g < 2:
        return {
            "n": int(v.size), "clusters": g, "mean": mean, "se": None,
            "t_stat": None, "usable": False,
            "reason": f"{g} cluster(s): a standard error needs at least 2 sessions",
        }

    n = v.size
    # Sum of within-cluster deviations from the mean, squared and aggregated.
    total = 0.0
    for rows in groups.values():
        total += float(sum(x - mean for x in rows)) ** 2
    correction = g / (g - 1.0)
    variance = correction * total / (n**2)
    se = math.sqrt(variance) if variance > 0 else 0.0

    return {
        "n": int(n),
        "clusters": g,
        "mean": mean,
        "se": se if se > 0 else None,
        "t_stat": (mean / se) if se > 0 else None,
        "df": g - 1,
        "usable": g >= MIN_CLUSTERS_FOR_INFERENCE,
        "reason": (
            None
            if g >= MIN_CLUSTERS_FOR_INFERENCE
            else f"{g} clusters < {MIN_CLUSTERS_FOR_INFERENCE}; the interval is not trustworthy"
        ),
    }


def expected_log_growth(
    returns: Sequence[float], fraction: float = KELLY_TEST_FRACTION
) -> Optional[float]:
    """Mean log growth of capital under fixed-fraction sizing.

    The plan ranks by compounded value rather than raw upside, and this is the
    quantity that distinguishes them: a strategy can have a positive mean return
    and still destroy capital when compounded, because losses compound too.
    """
    vals = [x for x in (_finite(r) for r in returns) if x is not None]
    if not vals:
        return None
    total = 0.0
    for r in vals:
        growth = 1.0 + fraction * r
        if growth <= 0:
            # A wipeout under this sizing: log growth is undefined and the
            # honest answer is that the strategy is ruinous, not a large number.
            return float("-inf")
        total += math.log(growth)
    return total / len(vals)


def max_drawdown(returns: Sequence[float], fraction: float = KELLY_TEST_FRACTION) -> Optional[float]:
    """Peak-to-trough drawdown of the compounded equity curve, as a fraction."""
    vals = [x for x in (_finite(r) for r in returns) if x is not None]
    if not vals:
        return None
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for r in vals:
        equity *= 1.0 + fraction * r
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst


def calibration_error(probs: Sequence[float], outcomes: Sequence[int]) -> dict[str, Any]:
    """Brier score and skill against the base rate.

    Reuses the repo's existing scoring in `directional_options/calibration.py`,
    loaded as a leaf. Brier SKILL is the one that matters: a Brier score looks
    excellent on any rare event simply by predicting it never happens, and skill
    against the base rate removes exactly that free win.
    """
    from candidate_capture._leaf_import import isotonic

    module = isotonic()
    p = [float(x) for x in probs]
    o = [int(x) for x in outcomes]
    if not p or len(p) != len(o):
        return {"brier": None, "brier_skill": None, "usable": False}
    try:
        return {
            "brier": module.brier_score(p, o),
            "brier_skill": module.brier_skill(p, o),
            "base_rate": sum(o) / len(o),
            "usable": True,
        }
    except Exception as exc:  # noqa: BLE001
        return {"brier": None, "brier_skill": None, "usable": False, "error": str(exc)}


def stress_returns(
    returns: Sequence[float], extra_half_spread_pct: float = SLIPPAGE_STRESS_EXTRA_PCT
) -> list[float]:
    """Returns recomputed with a worse spread on BOTH sides of the trade.

    The exit half-spread in this data is always assumed (nothing stores a
    forward option quote), so the headline net return rests on an assumption
    that liquidity did not deteriorate over the hold. This is the gate that asks
    what happens if it did.

    Takes the returns of the trades the model would actually have TAKEN, the
    same input every other gate uses. An earlier version stressed every
    evaluated candidate instead, which measured the average contract rather than
    the model's choices and failed a model whose selections were fine.
    """
    out: list[float] = []
    for value in returns:
        net = _finite(value)
        if net is None:
            continue
        out.append(net - 2.0 * extra_half_spread_pct)
    return out


# ── the gate set ───────────────────────────────────────────────────────────
def evaluate_gates(
    *,
    rows: Sequence[Mapping[str, Any]],
    probs: Sequence[float],
    outcomes: Sequence[int],
    selected_returns: Sequence[float],
    session_dates: Sequence[Any],
    champion_mean_return: Optional[float] = None,
    unresolved_selections: int = 0,
) -> tuple[list[Gate], dict[str, Any]]:
    """Run every promotion gate. Returns (gates, metrics).

    `selected_returns` are the net returns of the trades this model would ACTUALLY
    have taken — not of every candidate. A model is judged on its choices.
    """
    gates: list[Gate] = []
    stats = clustered_mean(selected_returns, session_dates)
    n_rows = len(rows)
    n_sessions = len(set(session_dates))

    def add(name: str, passed: bool, threshold: Any, measured: Any, detail: str) -> None:
        gates.append(Gate(name, bool(passed), threshold, measured, detail))

    # 1 — enough evidence to judge at all
    add(
        "sample_size_rows", n_rows >= MIN_EVAL_ROWS, MIN_EVAL_ROWS, n_rows,
        "Evaluation rows available in this specialist's slice.",
    )
    add(
        "sample_size_sessions", n_sessions >= MIN_EVAL_SESSIONS, MIN_EVAL_SESSIONS, n_sessions,
        "Distinct sessions in which a trade was TAKEN. Rows within a session are "
        "not independent, so sessions are the real sample size.",
    )
    n_trades = len([r for r in selected_returns if _finite(r) is not None])
    add(
        "sample_size_trades", n_trades >= MIN_SELECTED_TRADES, MIN_SELECTED_TRADES, n_trades,
        "Trades the model actually took. Every economic gate below is computed "
        "on these, not on the candidates scored, so this is the sample size that "
        "those gates really rest on.",
    )

    # The model's picks must be markable, or the evaluation is measuring a
    # decision process the data cannot confirm.
    decided = n_trades + int(unresolved_selections)
    unresolved_rate = (unresolved_selections / decided) if decided else 1.0
    add(
        "selections_are_resolvable",
        bool(decided > 0 and unresolved_rate <= MAX_UNRESOLVED_SELECTION_RATE),
        MAX_UNRESOLVED_SELECTION_RATE,
        round(unresolved_rate, 4),
        "Fraction of non-abstaining picks that could NOT be honestly marked "
        "(no forward mark, or no trade arrived). A model that mostly picks "
        "unmarkable contracts has not been evaluated, however good the rest look.",
    )

    # 2 — positive after realistic costs, judged on a CLUSTERED interval
    mean = stats.get("mean")
    t_stat = stats.get("t_stat")
    add(
        "positive_net_return",
        bool(mean is not None and mean > 0),
        "> 0",
        mean,
        "Mean net-of-cost return of the trades this model would have taken.",
    )
    add(
        "significance_session_clustered",
        bool(t_stat is not None and t_stat >= MIN_T_STAT and stats.get("usable")),
        MIN_T_STAT,
        t_stat,
        f"t-stat against a SESSION-CLUSTERED standard error over "
        f"{stats.get('clusters')} clusters. Nominal SEs have run 1.6-4.7x too "
        f"small on this data.",
    )

    # 3 — it must COMPOUND, not merely average positive
    elg = expected_log_growth(selected_returns)
    add(
        "positive_expected_log_growth",
        bool(elg is not None and elg > 0 and math.isfinite(elg)),
        "> 0",
        elg,
        f"Mean log growth at a fixed {KELLY_TEST_FRACTION:.0%} fraction. A "
        f"positive mean return can still compound to ruin.",
    )

    dd = max_drawdown(selected_returns)
    add(
        "max_drawdown",
        bool(dd is not None and dd <= MAX_DRAWDOWN_LIMIT),
        MAX_DRAWDOWN_LIMIT,
        dd,
        "Peak-to-trough drawdown of the compounded equity curve.",
    )

    # 4 — calibrated probabilities
    cal = calibration_error(probs, outcomes)
    skill = cal.get("brier_skill")
    add(
        "calibrated_probabilities",
        bool(cal.get("usable") and skill is not None and skill > 0),
        "> 0",
        skill,
        "Brier skill against the base rate. A raw Brier score flatters any "
        "model that simply predicts a rare event never happens.",
    )

    # 5 — not one lucky trade
    ranked = sorted((x for x in (_finite(r) for r in selected_returns) if x is not None), reverse=True)
    trimmed = ranked[TOP_TRADE_EXCLUSION_COUNT:]
    trimmed_mean = (sum(trimmed) / len(trimmed)) if trimmed else None
    add(
        "not_one_exceptional_trade",
        bool(trimmed_mean is not None and trimmed_mean > 0),
        "> 0",
        trimmed_mean,
        f"Mean net return after removing the best {TOP_TRADE_EXCLUSION_COUNT} "
        f"trade(s). A lane here once booked its entire P&L from a handful of "
        f"mispriced fills.",
    )

    # 6 — survives a worse spread than the one observed
    stressed = stress_returns(selected_returns)
    stressed_mean = (sum(stressed) / len(stressed)) if stressed else None
    add(
        "robust_to_worse_slippage",
        bool(stressed_mean is not None and stressed_mean > 0),
        "> 0",
        stressed_mean,
        f"Mean net return with an extra {SLIPPAGE_STRESS_EXTRA_PCT:.1%} "
        f"half-spread per side. The exit spread is ASSUMED in this data, never "
        f"measured, so this is the gate that prices that assumption being wrong.",
    )

    # 7 — beats abstaining, and beats the incumbent
    add(
        "beats_no_trade_baseline",
        bool(mean is not None and mean > MIN_EDGE_OVER_BASELINE),
        MIN_EDGE_OVER_BASELINE,
        mean,
        "Abstaining returns exactly 0. A model that cannot beat doing nothing "
        "should abstain, and the NO_TRADE candidate lets it.",
    )
    # ALWAYS added, never conditional. When there is no incumbent the threshold
    # is the abstain floor, so the gate set has a FIXED shape: a conditional gate
    # meant "first model in a slot faced nine gates, its successor faced ten",
    # and `all(passed)` cannot compare two models judged against different bars.
    incumbent = champion_mean_return if champion_mean_return is not None else 0.0
    add(
        "beats_champion",
        bool(mean is not None and mean > incumbent),
        incumbent,
        mean,
        (
            "Must beat the incumbent champion on the same evaluation slice."
            if champion_mean_return is not None
            else "No incumbent for this slot, so the bar is the abstain floor of 0."
        ),
    )

    metrics = {
        "clustered": stats,
        "expected_log_growth": elg,
        "max_drawdown": dd,
        "calibration": cal,
        "trimmed_mean": trimmed_mean,
        "stressed_mean": stressed_mean,
        "eval_rows": n_rows,
        "eval_sessions": n_sessions,
        "selected_trades": n_trades,
        "unresolved_selections": int(unresolved_selections),
        "unresolved_rate": round(unresolved_rate, 6),
    }
    return gates, metrics


def gates_passed(gates: Sequence[Gate]) -> bool:
    """Every gate must pass. There is no weighted score and no override."""
    return bool(gates) and all(g.passed for g in gates)


def refusal_summary(gates: Sequence[Gate]) -> str:
    failed = [g for g in gates if not g.passed]
    if not failed:
        return "all gates passed"
    return "; ".join(f"{g.name} (measured {g.measured!r} vs {g.threshold!r})" for g in failed)
