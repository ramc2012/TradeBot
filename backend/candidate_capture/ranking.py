"""Global ranking with an explicit NO_TRADE, and the three capital policies.

RANKING IS BY COMPOUNDED UTILITY, NOT BY PROBABILITY
────────────────────────────────────────────────────
Sorting a decision set by P(win) picks the contract most likely to make money,
which is nearly always the cheapest, widest-spread, least liquid one — it has
the highest chance of a favourable tick and the worst economics. The plan's
score exists to stop that:

    score = expected_log_growth
          - slippage_penalty
          - downside_tail_penalty
          - uncertainty_penalty
          - liquidity_penalty
          - concentration_penalty

Every penalty is measured from the candidate's OWN row, never a constant.

NO_TRADE IS A CANDIDATE, NOT A THRESHOLD
────────────────────────────────────────
Abstention competes on the same scale as every contract, with a utility of
exactly zero. That is materially different from ranking contracts and then
applying a cut-off: a threshold is a number someone has to tune and re-tune per
class, whereas "beat doing nothing" is the true economic bar and needs no
calibration. If every contract in a set scores below zero, the set's answer is
NO_TRADE, and that is a decision the system made rather than a gap in its output.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

NO_TRADE = "NO_TRADE"

# Penalty weights. Structural a-priori values, deliberately NOT swept against
# realized returns — a weight tuned on the evaluation set is a parameter fitted
# to the thing the promotion gates are supposed to be testing.
W_SLIPPAGE = 1.0
W_TAIL = 0.5
W_UNCERTAINTY = 0.5
W_LIQUIDITY = 0.25
W_CONCENTRATION = 0.5
# Charged when a row has NO usable quoted spread. Deliberately punitive: a
# contract nobody is quoting is the one whose fill assumption is least
# trustworthy, and the previous 0.0 rewarded exactly that.
MISSING_SPREAD_PENALTY_PCT = 0.30
# Large enough to put an uneconomic contract decisively below the abstain floor.
UNECONOMIC_PENALTY = 1.0

# Fixed fraction used to turn a probability-weighted return into a log-growth
# quantity. Same value the promotion gates use, so the ranker optimises the
# quantity it is later judged on.
SIZING_FRACTION = 0.02

# ── the payoff geometry ────────────────────────────────────────────────────
# A long option position is exited at a TARGET or a STOP, both expressed as a
# fraction of the premium paid. Stating both explicitly is what makes the
# expected-growth term coherent.
#
# An earlier version paired a breakeven-scaled upside with a TOTAL-loss
# downside: win = log1p(f * 3 * breakeven), lose = log1p(-f). Because breakeven
# is a premium-move fraction of a few percent, that demanded p ~ 0.97 before any
# contract could beat abstaining — and it INVERTED the ranking, because a cheap
# illiquid wing has the LARGEST breakeven (the flat per-order brokerage dwarfs
# its premium), hence the largest imputed upside. The model preferred exactly the
# contracts its penalties existed to avoid.
#
# +40/-20 is not arbitrary: it is this project's own measured result, that
# payoff geometry beat every entry signal tested on this instrument class.
TARGET_RETURN_PCT = 0.40
STOP_RETURN_PCT = 0.20
# A contract whose round trip cannot be paid for by the target move is
# uneconomic by construction — no probability makes it worth taking.
UNECONOMIC_MARGIN = 1.0


def _finite(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


@dataclass
class RankedCandidate:
    option_type: str
    strike: Optional[float]
    expiry: Optional[Any]
    probability: Optional[float]
    utility: float
    components: dict[str, float] = field(default_factory=dict)
    is_no_trade: bool = False
    # Index into the caller's candidate list. Carried so the winner can be
    # resolved by position rather than re-matched on (type, strike, expiry) —
    # field equality would silently pick the wrong row whenever two contracts
    # in one set share a strike across different expiries.
    index: Optional[int] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "option_type": self.option_type,
            "strike": self.strike,
            "expiry": self.expiry,
            "probability": self.probability,
            "utility": round(self.utility, 8),
            "components": {k: round(v, 8) for k, v in self.components.items()},
            "is_no_trade": self.is_no_trade,
            "index": self.index,
        }


def candidate_utility(
    *,
    probability: float,
    snapshot: Mapping[str, Any],
    breakeven_move_pct: Optional[float] = None,
    expected_upside_pct: Optional[float] = None,
    exposure_share: float = 0.0,
    fraction: float = SIZING_FRACTION,
) -> tuple[float, dict[str, float]]:
    """Compounded-utility score for one contract, with its components exposed.

    The components are returned rather than folded away so a ranking decision can
    be explained after the fact — "it ranked third because its spread penalty was
    large", not "the model preferred it".
    """
    p = min(max(float(probability), 0.0), 1.0)

    # A win takes the target; a loss takes the stop. Both are fractions of the
    # premium, so the two branches are on the same scale — which is what the
    # earlier breakeven-vs-total-loss pairing got wrong.
    upside = _finite(expected_upside_pct)
    if upside is None or upside <= 0:
        upside = TARGET_RETURN_PCT
    downside = STOP_RETURN_PCT

    win_growth = math.log1p(fraction * upside)
    loss_growth = math.log1p(-fraction * downside)
    expected_log = p * win_growth + (1.0 - p) * loss_growth

    # UNECONOMIC CHECK. Breakeven is the premium move that merely pays the round
    # trip. If the target cannot clear it, the contract loses money even when the
    # trade works, and no probability rescues it — so it is driven below the
    # abstain floor rather than ranked. This is what stops the cheap wide wings,
    # whose breakeven can exceed 40% of premium, from ever being selected.
    breakeven = _finite(breakeven_move_pct)
    uneconomic = bool(breakeven is not None and breakeven >= upside * UNECONOMIC_MARGIN)

    # Slippage: the row's own measured half-spread.
    #
    # A MISSING spread is charged the WORST case, not zero. `assess_quote`
    # returns spread_pct=None for exactly the two worst quote states — no
    # two-sided quote at all, and a crossed book — so treating None as 0.0 gave
    # the most favourable slippage score to the contracts whose quotes were
    # broken, while a real 30% spread was charged in full.
    measured_spread = _finite(snapshot.get("spread_pct"))
    spread_pct = measured_spread if measured_spread is not None else MISSING_SPREAD_PENALTY_PCT
    slippage_penalty = W_SLIPPAGE * fraction * spread_pct

    # Downside tail: a contract that can lose its whole premium has a fatter
    # tail than the mean return admits. Scaled by how far it is from the money.
    steps = abs(_finite(snapshot.get("moneyness_steps")) or 0.0)
    tail_penalty = W_TAIL * fraction * (1.0 - p) * min(steps / 8.0, 1.0)

    # Uncertainty: greatest at p = 0.5, zero at either certainty. A coin-flip
    # ranked on its point estimate is the classic over-confident pick.
    uncertainty_penalty = W_UNCERTAINTY * fraction * (1.0 - abs(2.0 * p - 1.0))

    # Liquidity: rank within its own chain, so this never becomes an absolute
    # threshold needing per-underlying tuning.
    percentile = _finite(snapshot.get("liquidity_percentile"))
    liquidity_penalty = W_LIQUIDITY * fraction * (1.0 - (percentile if percentile is not None else 0.0))

    # Concentration: how much of the book is already in this underlying.
    concentration_penalty = W_CONCENTRATION * fraction * max(0.0, float(exposure_share))

    components = {
        "expected_log_growth": expected_log,
        # Applied as a component so a refusal is visible in the breakdown rather
        # than appearing as an unexplained low score.
        "uneconomic_penalty": -UNECONOMIC_PENALTY if uneconomic else 0.0,
        "slippage_penalty": -slippage_penalty,
        "downside_tail_penalty": -tail_penalty,
        "uncertainty_penalty": -uncertainty_penalty,
        "liquidity_penalty": -liquidity_penalty,
        "concentration_penalty": -concentration_penalty,
    }
    return float(sum(components.values())), components


def rank_decision_set(
    candidates: Sequence[Mapping[str, Any]],
    probabilities: Sequence[float],
    *,
    exposure_share: float = 0.0,
    fraction: float = SIZING_FRACTION,
) -> list[RankedCandidate]:
    """Rank one decision set, with NO_TRADE always present and always at zero.

    `candidates` are contract snapshots; the abstain option is appended here
    rather than expected in the input, so a caller cannot forget it and produce
    a ranking with no floor.
    """
    ranked: list[RankedCandidate] = []
    for position, (snapshot, prob) in enumerate(zip(candidates, probabilities)):
        if str(snapshot.get("option_type")) == NO_TRADE:
            continue  # the abstain row is added below, never scored as a contract
        utility, components = candidate_utility(
            probability=prob,
            snapshot=snapshot,
            breakeven_move_pct=snapshot.get("breakeven_move_pct"),
            expected_upside_pct=snapshot.get("expected_upside_pct"),
            exposure_share=exposure_share,
            fraction=fraction,
        )
        ranked.append(
            RankedCandidate(
                option_type=str(snapshot.get("option_type")),
                strike=_finite(snapshot.get("strike")),
                expiry=snapshot.get("expiry"),
                probability=float(prob),
                utility=utility,
                components=components,
                index=position,
            )
        )

    ranked.append(
        RankedCandidate(
            option_type=NO_TRADE,
            strike=None,
            expiry=None,
            probability=None,
            # Exactly zero: abstaining neither gains nor loses. This is the bar
            # every contract must clear, and it needs no tuning.
            utility=0.0,
            components={"expected_log_growth": 0.0},
            is_no_trade=True,
        )
    )

    ranked.sort(key=lambda c: (-c.utility, c.is_no_trade))
    return ranked


def select(ranked: Sequence[RankedCandidate]) -> RankedCandidate:
    """The chosen candidate — possibly, and legitimately, NO_TRADE."""
    return ranked[0]


# ══════════════════════════════════════════════════════════════════════════
# Three capital policies (plan section 9)
# ══════════════════════════════════════════════════════════════════════════
# The SAME decisions run through three different sizing rules. Keeping them
# separate is the point: the aggressive one must never influence labels, model
# selection, or the research record.

PORTFOLIO_RESEARCH = "research_1r"
PORTFOLIO_PRACTICAL = "practical_kelly"
PORTFOLIO_DOUBLING = "doubling_shadow"

# Practical policy: capped fractional Kelly. The cap matters more than the
# fraction — uncapped Kelly on a mis-estimated edge is a reliable way to go
# broke, and every probability here is an estimate.
KELLY_FRACTION = 0.25
KELLY_MAX_RISK = 0.02
# Aggressive shadow: the ten-step doubling sequence, run ONLY to answer whether
# it is statistically achievable. It is not an objective and cannot be one —
# training toward it would select for catastrophic risk.
DOUBLING_RISK_FRACTION = 0.5
DOUBLING_TARGET_STEPS = 10


@dataclass
class PortfolioResult:
    policy: str
    equity: float
    peak_equity: float
    max_drawdown: float
    trades: int
    wins: int
    ruined: bool
    doubling_steps_completed: int = 0
    episodes_restarted: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict_safe(self)


def asdict_safe(obj: PortfolioResult) -> dict[str, Any]:
    return {
        "policy": obj.policy,
        "equity": round(obj.equity, 6),
        "peak_equity": round(obj.peak_equity, 6),
        "max_drawdown": round(obj.max_drawdown, 6),
        "trades": obj.trades,
        "wins": obj.wins,
        "ruined": obj.ruined,
        "doubling_steps_completed": obj.doubling_steps_completed,
        "episodes_restarted": obj.episodes_restarted,
    }


def run_portfolio(
    returns: Sequence[float],
    *,
    policy: str,
    probabilities: Optional[Sequence[float]] = None,
    starting_equity: float = 1.0,
) -> PortfolioResult:
    """Run one sequence of realized net returns through one capital policy.

    `returns` are per-trade net-of-cost fractional returns, in chronological
    order — the model's own selections, not every candidate.
    """
    equity = float(starting_equity)
    peak = equity
    worst_dd = 0.0
    wins = 0
    trades = 0
    ruined = False
    steps_done = 0
    restarts = 0
    episode_start = equity

    for i, raw in enumerate(returns):
        r = _finite(raw)
        if r is None:
            continue
        trades += 1
        if r > 0:
            wins += 1

        if policy == PORTFOLIO_RESEARCH:
            # Fixed 1R per trade: unbiased for COMPARING models, because
            # position size never varies with conviction and cannot flatter one.
            fraction = KELLY_MAX_RISK
        elif policy == PORTFOLIO_PRACTICAL:
            p = 0.5
            if probabilities is not None and i < len(probabilities):
                p = min(max(float(probabilities[i]), 0.0), 1.0)
            edge = 2.0 * p - 1.0
            fraction = max(0.0, min(KELLY_FRACTION * edge, KELLY_MAX_RISK))
        elif policy == PORTFOLIO_DOUBLING:
            fraction = DOUBLING_RISK_FRACTION
        else:
            raise ValueError(f"unknown capital policy {policy!r}")

        equity *= 1.0 + fraction * r
        if equity <= 0:
            equity = 0.0
            ruined = True

        peak = max(peak, equity)
        if peak > 0:
            worst_dd = max(worst_dd, (peak - equity) / peak)

        if policy == PORTFOLIO_DOUBLING:
            # A failed aggressive episode restarts ONLY this shadow episode. It
            # never resets the system's cumulative drawdown and never touches
            # the other two books.
            if equity >= episode_start * 2.0:
                steps_done += 1
                episode_start = equity
            elif ruined or equity <= episode_start * 0.5:
                restarts += 1
                episode_start = equity if equity > 0 else float(starting_equity)
                equity = episode_start
                ruined = False

        if ruined:
            break

    return PortfolioResult(
        policy=policy,
        equity=equity,
        peak_equity=peak,
        max_drawdown=worst_dd,
        trades=trades,
        wins=wins,
        ruined=ruined,
        doubling_steps_completed=min(steps_done, DOUBLING_TARGET_STEPS),
        episodes_restarted=restarts,
    )


def run_all_portfolios(
    returns: Sequence[float], probabilities: Optional[Sequence[float]] = None
) -> dict[str, dict[str, Any]]:
    """All three books over the same decisions.

    The doubling book's result is reported and never fed back: it answers
    whether the ten-step sequence is achievable, and nothing else. It must not
    influence labels, gates, or model promotion.
    """
    return {
        policy: run_portfolio(returns, policy=policy, probabilities=probabilities).as_dict()
        for policy in (PORTFOLIO_RESEARCH, PORTFOLIO_PRACTICAL, PORTFOLIO_DOUBLING)
    }
