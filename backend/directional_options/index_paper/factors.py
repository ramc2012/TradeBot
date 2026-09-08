"""The factor panel: every feature the lane can see, assembled per session.

The lane decides from the WHOLE feature set. That is not a stylistic
preference — it is forced by measurement. Bucketing 1-5 day outcomes by IV
level, by variance risk premium and by realised-vol trend puts every bucket at
17-25% with +/-5.5% standard errors: no single vol-state variable separates the
days when long premium pays from the days it does not.

Three commitments hold this module together:

  1. EVERY FACTOR IS JOURNALLED, whether it acted or not. A factor with weight
     zero still records its value at every decision, so its information
     coefficient can be measured months later against outcomes the lane
     actually experienced. That is the only path from "plausible" to
     "measured" that does not involve fitting on the same data twice.

  2. ROLE IS SEPARATE FROM DIRECTION. A factor may inform DIRECTION (which way),
     SIZE (how much conviction) or act as a GATE (whether to trade at all).
     Conflating them is how this stack previously credited a magnitude signal
     with directional skill. The measured evidence here says the positioning
     factors are magnitude factors: futures open-interest z-score carries
     IC +0.139 against |5-day return| (t = 1.84 clustered by date) and
     IC +0.025 against the SIGNED return (t = 0.33). It sizes; it must not point.

  3. CONFIDENCE SETS WEIGHT, AND MOST FACTORS START AT ZERO. Chain-derived
     features have days of history, participant data has ten sessions, and the
     stack has already falsified intraday momentum, cross-sectional flow, sector
     RS and timing. A factor whose sign is theory-determined but unmeasured
     enters as SHADOW: computed, journalled, weight zero, until its own IC says
     otherwise.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Sequence

from loguru import logger
from sqlalchemy import text

from db.database import AsyncSessionLocal
from directional_options.vol.realized import DailyBar, garman_klass_rv

Role = str        # "direction" | "size" | "gate"
Confidence = str  # "measured" | "plausible" | "speculative"

# Weight a factor's contribution by how well it is established.  A factor is
# only "measured" if a number computed against THIS data supports it.
#
# Unmeasured factors are given SMALL but non-zero weight rather than zero.  A
# lane whose direction factors all carry zero weight never trades, never
# generates outcomes, and therefore can never measure the very factors that
# would let them be promoted — the instrument has to run to calibrate itself.
# The weights are deliberately small so that a wrong theoretical sign costs
# little, and every contribution is journalled so per-factor information
# coefficients can be computed later against outcomes this lane actually had.
CONFIDENCE_WEIGHT: dict[Confidence, float] = {
    "measured": 1.00,
    "plausible": 0.35,
    "speculative": 0.15,
}


@dataclass
class Factor:
    name: str
    family: str
    role: Role
    value: float | None
    confidence: Confidence
    # CONTRACT: `score` is the RAW reading, squashed to [-1, +1] and NOT
    # pre-inverted. `sign` carries desirability: +1 means a higher score is
    # favourable (long, or bigger size), -1 means it is unfavourable. The
    # composite multiplies the two exactly once.
    #
    # Four factors originally baked the inversion into BOTH, so the composite
    # double-negated them — a variance risk premium three vol points rich was
    # INCREASING position size. Keep the raw reading in `score`.
    sign: int = 1
    score: float | None = None
    status: str = "ok"            # ok | unavailable | stale | insufficient_history
    detail: str = ""
    weight: float = 0.0

    @property
    def usable(self) -> bool:
        return self.status == "ok" and self.score is not None

    @property
    def acting(self) -> bool:
        return self.usable and self.weight > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "family": self.family,
            "role": self.role,
            "value": self.value,
            "score": self.score,
            "sign": self.sign,
            "confidence": self.confidence,
            "weight": self.weight,
            "status": self.status,
            "acting": self.acting,
            "detail": self.detail,
        }


def unavailable(name: str, family: str, role: Role, why: str, confidence: Confidence = "plausible") -> Factor:
    return Factor(
        name=name, family=family, role=role, value=None, confidence=confidence,
        status="unavailable", detail=why, weight=0.0,
    )


# Squash scales, MEASURED rather than assumed.
#
# Each scale is set so the 80th percentile of |raw| maps to |score| ~ 0.60
# (tanh(0.7) = 0.60), which keeps the bulk of readings on the informative part
# of the curve instead of pinned against its asymptote. A factor that returns
# +/-1 on most days is not a signal, it is a constant vote, and it contributes
# nothing to a weighted composite except its own sign.
#
# Measured 2026-09-08 over the full clean history available to each factor
# (~1,725 index-sessions for the spot-derived ones, 1,140 for futures OI, 146
# for the variance risk premium, 36 for the chain). Saturation before/after:
#
#   vrp_spread      0.020 -> 0.0963   |score|>0.90 on 97.9% of readings -> ~20%
#   trend_20d       0.030 -> 0.0650   21.0% -> ~20%
#   reversion_10d   2.000 -> 3.6800   14.1% -> ~20%
#   rv_compression  0.250 -> 0.3850    9.8% -> ~20%
#   futures_oi_z    1.500 -> 1.9900    7.8% -> ~20%
#
# Verified after the change: 0.0% / 2.5% / 1.7% / 2.3% / 3.3% respectively.
# chain_oi_asymmetry was a separate, STRUCTURAL saturation -- see its factor.
#
# vrp_spread was by far the worst: its median reading was already twice its
# scale, so it acted as a constant -1 on size regardless of how rich premium
# actually was. Re-derive with scripts measuring |raw| p80 whenever the vol
# regime shifts materially.
SCALE_VRP_SPREAD = 0.0963
SCALE_TREND_20D = 0.0650
SCALE_REVERSION_10D = 3.6800
SCALE_RV_COMPRESSION = 0.3850
SCALE_FUTURES_OI_Z = 1.9900
SCALE_BREAKEVEN_RATIO = 0.2500        # 0.15 (25 DTE) to 0.43 (4 DTE) is the live span
# PROVISIONAL: only 36 observations exist (12 sessions x 3 indices, and the
# three are ~90% correlated, so the effective sample is nearer 12). Re-derive
# once the chain history is months rather than weeks deep.
SCALE_CHAIN_OI_ASYMMETRY = 0.7330


def oi_asymmetry(ce_up: float, ce_dn: float, pe_up: float, pe_dn: float) -> tuple[float | None, float | None]:
    """(gross-normalised, legacy net-normalised) put-vs-call OI-change asymmetry.

    Returns both so the difference stays visible and testable. The legacy form
    divides by |ce_net| + |pe_net|, which equals the numerator exactly whenever
    the two sides move in opposite directions — so it pins to +/-1 on a
    condition that says nothing about magnitude. The gross form divides by
    total activity, which is strictly larger and degenerates only when one side
    sees no activity at all.
    """
    ce_net, pe_net = ce_up - ce_dn, pe_up - pe_dn
    numerator = pe_net - ce_net
    gross = ce_up + ce_dn + pe_up + pe_dn
    net = abs(ce_net) + abs(pe_net)
    return (
        (numerator / gross) if gross > 0 else None,
        (numerator / net) if net > 0 else None,
    )


def squash(value: float, scale: float) -> float:
    """Map an unbounded quantity into [-1, 1].

    tanh rather than a min/max clip, so a large reading still ranks above a
    merely sizeable one instead of saturating into a tie — which matters when
    the composite is a weighted average of a handful of terms. tanh does reach
    +/-1 in floating point beyond about 19 scale units, but every factor here
    picks a scale such that a real reading lands within a few units of it, so
    the saturating region is not one this lane visits.
    """
    if scale <= 0:
        return 0.0
    return math.tanh(value / scale)


# ── context ─────────────────────────────────────────────────────────────────


@dataclass
class FactorContext:
    """Everything the factor functions need, loaded once per evaluation."""

    underlying: str
    session_date: date
    as_of: datetime
    daily_bars: list[DailyBar] = field(default_factory=list)
    vol_ctx: dict[str, Any] = field(default_factory=dict)
    futures_oi: dict[str, Any] | None = None
    chain_oi_change: dict[str, Any] | None = None
    sessions_to_expiry: int | None = None
    breakeven_ratio: float | None = None

    @property
    def spot(self) -> float | None:
        return self.daily_bars[-1].close if self.daily_bars else None


# ── loaders ─────────────────────────────────────────────────────────────────

# Rollover and near-expiry rows are excluded here rather than in the factor,
# because the module that writes this table NULLs only the rollover row and not
# the pre-expiry open-interest collapse — leaving BANKNIFTY 2026-08-24 booked as
# a -40% "long unwind" when it is expiry mechanics.
_FUTURES_OI_SQL = text(
    """
    SELECT ts, expiry, close, d_price_pct, oi, d_oi, d_oi_pct, oi_z, oi_pctile,
           oi_state, activity_surge, is_rollover, (expiry - ts) AS dte
    FROM futures_oi_baselines
    WHERE symbol = :symbol
      AND ts <= :session_date
      AND ts >= :lower
      AND is_rollover = false
      AND (expiry - ts) > 3
    ORDER BY ts DESC
    LIMIT 1
    """
)


async def load_futures_oi(underlying: str, session_date: date, *, max_age_sessions: int = 4) -> dict[str, Any] | None:
    """Front-contract futures OI state as of the last completed session.

    `ts <= session_date` is the whole point: this must be known BEFORE the
    session the lane acts in. The freshness ceiling exists because the
    collector behind this table has run as a one-off backfill, not a daily job —
    a silently stale row would otherwise be read as live positioning.
    """
    lower = session_date - timedelta(days=max_age_sessions * 3 + 10)
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                _FUTURES_OI_SQL,
                {"symbol": underlying, "session_date": session_date, "lower": lower},
            )
        ).mappings().first()
    if not row:
        return None
    out = dict(row)
    out["age_days"] = (session_date - row["ts"]).days
    return out


# Daily OI change across the strike ladder, split call vs put.  `time` carries
# literal bounds on both sides and is never wrapped in a function.
_CHAIN_OI_SQL = text(
    """
    WITH deduped AS (
        SELECT DISTINCT ON (time, expiry, strike, option_type)
               time, expiry, strike, option_type, oi, underlying_price
        FROM option_premium_candles
        WHERE underlying = :underlying
          AND interval = '30minute'
          AND time >= :lower
          AND time <= :upper
          AND oi IS NOT NULL
        ORDER BY time, expiry, strike, option_type, synced_at DESC
    ), last_per_day AS (
        SELECT DISTINCT ON ((time AT TIME ZONE 'Asia/Kolkata')::date, expiry, strike, option_type)
               (time AT TIME ZONE 'Asia/Kolkata')::date AS day,
               expiry, strike, option_type, oi, underlying_price
        FROM deduped
        ORDER BY (time AT TIME ZONE 'Asia/Kolkata')::date, expiry, strike, option_type, time DESC
    )
    SELECT day, expiry, strike, option_type, oi, underlying_price
    FROM last_per_day
    ORDER BY day, expiry, strike, option_type
    """
)


async def load_chain_oi_change(
    underlying: str, session_date: date, *, lookback_days: int = 6
) -> dict[str, Any] | None:
    """Call-vs-put open-interest change over the ladder, session on session.

    The audit called this the highest value-to-effort item available: open
    interest is populated on essentially every index option row, while traded
    volume is zero on 97.5% of them. Where a build happens on the ladder is the
    only positioning signal this chain actually carries.
    """
    upper = datetime.combine(session_date, datetime.max.time(), tzinfo=timezone.utc)
    lower = upper - timedelta(days=lookback_days)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                _CHAIN_OI_SQL, {"underlying": underlying, "lower": lower, "upper": upper}
            )
        ).mappings().all()
    if not rows:
        return None

    by_day: dict[date, dict[tuple, int]] = {}
    spot_by_day: dict[date, float] = {}
    for r in rows:
        key = (r["expiry"], float(r["strike"]), r["option_type"])
        by_day.setdefault(r["day"], {})[key] = int(r["oi"] or 0)
        if r["underlying_price"] is not None:
            spot_by_day[r["day"]] = float(r["underlying_price"])

    days = sorted(by_day)
    if len(days) < 2:
        return None
    today, prev = days[-1], days[-2]
    spot = spot_by_day.get(today)

    ce_up = ce_dn = pe_up = pe_dn = 0
    common = set(by_day[today]) & set(by_day[prev])
    for key in common:
        delta = by_day[today][key] - by_day[prev][key]
        expiry, strike, opt = key
        if spot is not None and abs(math.log(max(strike, 1e-9) / spot)) > 0.06:
            continue  # only the strikes near enough to matter
        if opt == "CE":
            ce_up += max(delta, 0); ce_dn += max(-delta, 0)
        else:
            pe_up += max(delta, 0); pe_dn += max(-delta, 0)

    ce_net, pe_net = ce_up - ce_dn, pe_up - pe_dn

    # Normalised by GROSS open-interest activity, not by |ce_net| + |pe_net|.
    # The latter is what the first version used and it saturates structurally:
    # whenever the two sides move in OPPOSITE directions the numerator equals
    # the denominator exactly, so the ratio pins to +/-1 regardless of how
    # large or small the flows were. Measured over the available chain history,
    # its raw 95th percentile was exactly 1.0000 -- a fifth of readings sat on
    # the boundary carrying no magnitude information at all, and on 2026-09-07
    # all three indices reported -0.98 to -1.00 simultaneously.
    #
    # Gross activity is a strictly larger denominator that only degenerates
    # when one side sees no activity whatsoever.
    gross = ce_up + ce_dn + pe_up + pe_dn
    asym, legacy = oi_asymmetry(ce_up, ce_dn, pe_up, pe_dn)
    return {
        "day": today,
        "prev_day": prev,
        "session_gap_days": (today - prev).days,
        "n_common_contracts": len(common),
        "ce_net_oi_change": ce_net,
        "pe_net_oi_change": pe_net,
        "gross_oi_change": gross,
        "asymmetry": asym,
        "net_asymmetry_legacy": legacy,
        "spot": spot,
    }


# ── factor functions ────────────────────────────────────────────────────────


def factor_vrp(ctx: FactorContext) -> Factor:
    """Variance risk premium: implied minus trailing realised, in vol units.

    A long-premium lane PAYS this. Positive spread means the option is dear
    against what the index has been delivering, which is the carry headwind.
    Sign is -1: a wide premium argues for smaller size, never for a direction.
    """
    spread = ctx.vol_ctx.get("vrp_spread")
    if spread is None:
        return unavailable("vrp_spread", "vol", "size", "no variance risk premium at this bar")
    return Factor(
        name="vrp_spread", family="vol", role="size", value=float(spread),
        confidence="measured", sign=-1,
        score=squash(float(spread), SCALE_VRP_SPREAD),
        detail=f"implied is {float(spread) * 100:+.2f} vol points against trailing realised",
        weight=CONFIDENCE_WEIGHT["measured"],
    )


def factor_rv_percentile(ctx: FactorContext) -> Factor:
    """Where trailing realised vol sits in its own 2.7-year cone.

    Low realised is a double-edged reading for long premium: cheap in absolute
    vol terms, but it is precisely the regime in which the index fails to travel
    far enough to clear a breakeven. Treated as size, not direction.
    """
    pct = ctx.vol_ctx.get("rv_percentile")
    if pct is None:
        return unavailable("rv_percentile", "vol", "size", "cone percentile not available at this tenor")
    centred = (float(pct) - 50.0) / 50.0
    return Factor(
        name="rv_percentile", family="vol", role="size", value=float(pct),
        confidence="plausible", sign=1, score=max(min(centred, 1.0), -1.0),
        detail=f"trailing realised vol at the {float(pct):.0f}th percentile of its own cone",
        weight=CONFIDENCE_WEIGHT["plausible"],
    )


def factor_rv_compression(ctx: FactorContext) -> Factor:
    """Short realised vol against medium realised vol.

    Deliberately SHADOW. The compression-precedes-expansion hypothesis was
    tested directly on this stack's option data and returned zero lift, so it
    enters journalled and weightless rather than excluded — an excluded factor
    can never be re-measured.
    """
    bars = ctx.daily_bars
    rv5, rv20 = garman_klass_rv(bars, 5), garman_klass_rv(bars, 20)
    if not rv5 or not rv20 or rv20 <= 0:
        return unavailable("rv_compression", "vol", "size", "insufficient clean daily bars", "speculative")
    ratio = rv5 / rv20
    return Factor(
        name="rv_compression", family="vol", role="size", value=ratio,
        confidence="speculative", sign=-1, score=squash(ratio - 1.0, SCALE_RV_COMPRESSION),
        detail=f"5d/20d realised vol = {ratio:.2f} (compression-precedes-expansion is falsified here; shadow only)",
        weight=CONFIDENCE_WEIGHT["speculative"],
    )


def factor_futures_oi_z(ctx: FactorContext) -> Factor:
    """Front-contract futures open interest, z-scored against 60 sessions.

    The one positioning factor with real history and a real measurement behind
    it, and it is a MAGNITUDE factor. Re-measured independently on clean index
    spot with non-overlapping windows and date-clustered errors:
    IC(oi_z, |5d return|) = +0.139, t = 1.84; IC against the SIGNED return is
    +0.025, t = 0.33. High-tercile oi_z delivers a 1.90% mean 5-day absolute
    move against 1.51% in the low tercile.

    That shape is exactly what a long-premium lane needs — the drag is theta, so
    what has to be forecast is how far the index travels, not which way.
    """
    oi = ctx.futures_oi
    if not oi or oi.get("oi_z") is None:
        return unavailable("futures_oi_z", "positioning", "size", "no clean front-contract futures OI row", "measured")
    age = int(oi.get("age_days") or 0)
    if age > 6:
        return Factor(
            name="futures_oi_z", family="positioning", role="size", value=float(oi["oi_z"]),
            confidence="measured", sign=1, score=None, status="stale",
            detail=f"futures OI row is {age} days old; the collector is a one-off backfill, not a daily job",
            weight=0.0,
        )
    return Factor(
        name="futures_oi_z", family="positioning", role="size", value=float(oi["oi_z"]),
        confidence="measured", sign=1, score=squash(float(oi["oi_z"]), SCALE_FUTURES_OI_Z),
        detail=f"front-contract OI z={float(oi['oi_z']):+.2f} ({age}d old), dte={oi.get('dte')}",
        weight=CONFIDENCE_WEIGHT["measured"],
    )


def factor_futures_oi_state(ctx: FactorContext) -> Factor:
    """The classic price-vs-OI quadrant: long build, short build, unwind, covering.

    SHADOW. The signed information coefficient on the underlying z-score was
    +0.025 (t = 0.33), so there is no measured basis for reading direction out
    of this table; the quadrant label is theory, not evidence, until its own
    journalled IC says otherwise.
    """
    oi = ctx.futures_oi
    if not oi or not oi.get("oi_state"):
        return unavailable("futures_oi_state", "positioning", "direction", "no OI state label", "speculative")
    # Labels verified against the table's own vocabulary rather than assumed:
    # short_covering 459, short_buildup 377, long_unwind 369, long_buildup 233.
    # The first version guessed "long_build"/"short_build" and silently
    # unavailable-d itself on 7 of 10 decisions.
    mapping = {
        "long_buildup": 1.0,
        "short_covering": 0.5,
        "long_unwind": -0.5,
        "short_buildup": -1.0,
    }
    state = str(oi["oi_state"])
    score = mapping.get(state)
    if score is None:
        return unavailable("futures_oi_state", "positioning", "direction", f"unmapped state {state!r}", "speculative")
    return Factor(
        name="futures_oi_state", family="positioning", role="direction", value=score,
        confidence="speculative", sign=1, score=score,
        detail=f"price/OI quadrant = {state} (shadow: signed IC on this table measured +0.03, t=0.33)",
        weight=CONFIDENCE_WEIGHT["speculative"],
    )


def factor_chain_oi_asymmetry(ctx: FactorContext) -> Factor:
    """Where open interest built on the ladder — puts against calls, near the money.

    Open interest is the only positioning signal this chain carries: volume is
    zero on 97.5% of index option rows. Positive score means puts built faster
    than calls, read here as support being sold into, i.e. mildly bullish.
    SHADOW — the read is conventional, and conventional is not measured.
    """
    data = ctx.chain_oi_change
    if not data or data.get("asymmetry") is None:
        return unavailable("chain_oi_asymmetry", "structure", "direction", "fewer than two comparable chain sessions", "speculative")
    if int(data.get("n_common_contracts") or 0) < 20:
        return unavailable(
            "chain_oi_asymmetry", "structure", "direction",
            f"only {data.get('n_common_contracts')} contracts common to both sessions", "speculative",
        )
    asym = float(data["asymmetry"])
    return Factor(
        name="chain_oi_asymmetry", family="structure", role="direction", value=asym,
        # squash, not a hard clip. A clip on an already-bounded ratio cannot
        # distinguish a decisive reading from a marginal one once either
        # reaches the edge.
        confidence="speculative", sign=1, score=squash(asym, SCALE_CHAIN_OI_ASYMMETRY),
        detail=(
            f"near-money OI change: CE {data['ce_net_oi_change']:+,} vs PE {data['pe_net_oi_change']:+,} "
            f"on {data['gross_oi_change']:,} gross, over {data['n_common_contracts']} common contracts"
        ),
        weight=CONFIDENCE_WEIGHT["speculative"],
    )


def factor_trend(ctx: FactorContext, window: int = 20) -> Factor:
    """Trailing return over `window` sessions.

    SHADOW, and emphatically so. This stack's own walk-forward work found
    intraday momentum anti-predictive in every regime tested and trend alone
    anti-predictive at longer horizons too. It is journalled so that a sign can
    eventually be established from this lane's own outcomes rather than
    inherited from convention.
    """
    bars = ctx.daily_bars
    if len(bars) < window + 1:
        return unavailable("trend_20d", "trend", "direction", f"need {window + 1} clean daily bars", "speculative")
    window_bars = bars[-(window + 1):]

    # A trailing return is a comparison of two LEVELS, so a dropped session in
    # between does not invalidate it the way it invalidates a return series —
    # only the endpoints have to be trustworthy. Requiring full contiguity made
    # this factor unavailable on 10 of 10 decisions, because 14 sessions are
    # dropped from the cleaned series and a 20-session window nearly always
    # contains one.
    gaps = sum(1 for b in window_bars[1:] if not b.contiguous)
    if gaps > max(2, int(0.2 * window)):
        return unavailable(
            "trend_20d", "trend", "direction",
            f"{gaps} dropped sessions inside a {window}-session window", "speculative",
        )
    ret = math.log(window_bars[-1].close / window_bars[0].close)
    return Factor(
        name="trend_20d", family="trend", role="direction", value=ret,
        confidence="speculative", sign=1, score=squash(ret, SCALE_TREND_20D),
        detail=(
            f"{window}-session return {ret * 100:+.2f}% over {gaps} dropped session(s) "
            "(shadow: momentum measured anti-predictive here)"
        ),
        weight=CONFIDENCE_WEIGHT["speculative"],
    )


def factor_reversion(ctx: FactorContext, window: int = 10) -> Factor:
    """Distance from a short moving average, in units of realised vol.

    SHADOW. The fade was the entry edge in this stack's directional research,
    but only before fill lag, which flipped it negative. Sign is -1 (stretched
    above the mean argues for the downside), journalled for measurement.
    """
    bars = ctx.daily_bars
    if len(bars) < window + 1:
        return unavailable("reversion_10d", "trend", "direction", f"need {window + 1} clean daily bars", "speculative")
    closes = [b.close for b in bars[-window:]]
    ma = sum(closes) / len(closes)
    rv = garman_klass_rv(bars, 20)
    if not rv or rv <= 0 or ma <= 0:
        return unavailable("reversion_10d", "trend", "direction", "no realised vol to scale by", "speculative")
    daily_sigma = rv / math.sqrt(252.0)
    stretch = (bars[-1].close - ma) / (ma * daily_sigma)
    return Factor(
        name="reversion_10d", family="trend", role="direction", value=stretch,
        confidence="speculative", sign=-1, score=squash(stretch, SCALE_REVERSION_10D),
        detail=f"{stretch:+.2f} daily sigma from the {window}-session mean (shadow)",
        weight=CONFIDENCE_WEIGHT["speculative"],
    )


def factor_geometry(ctx: FactorContext) -> Factor:
    """How far the contract has to travel, relative to what it implies it can.

    Measured on 2.7 years of clean bars: a delta-0.40 call at 25 DTE clears its
    3-day breakeven 49.0% of the time, one at 4 DTE only 40.7%. The breakeven
    ratio is therefore a real, quantified size input and not a formality.
    """
    ratio = ctx.breakeven_ratio
    if ratio is None:
        return unavailable("breakeven_ratio", "structure", "size", "no tradeable contract geometry", "measured")
    return Factor(
        name="breakeven_ratio", family="structure", role="size", value=ratio,
        confidence="measured", sign=-1, score=squash(ratio - 0.35, SCALE_BREAKEVEN_RATIO),
        detail=f"needs {ratio:.2f} of a 1-sigma move to break even (0.15 at 25 DTE, 0.43 at 4 DTE)",
        weight=CONFIDENCE_WEIGHT["measured"],
    )


DIRECTION_FACTORS: tuple[Callable[[FactorContext], Factor], ...] = (
    factor_futures_oi_state,
    factor_chain_oi_asymmetry,
    factor_trend,
    factor_reversion,
)

SIZE_FACTORS: tuple[Callable[[FactorContext], Factor], ...] = (
    factor_vrp,
    factor_rv_percentile,
    factor_rv_compression,
    factor_futures_oi_z,
    factor_geometry,
)


@dataclass
class FactorPanel:
    underlying: str
    session_date: date
    as_of: datetime
    factors: dict[str, Factor] = field(default_factory=dict)

    def by_role(self, role: Role) -> list[Factor]:
        return [f for f in self.factors.values() if f.role == role]

    def acting(self, role: Role) -> list[Factor]:
        return [f for f in self.by_role(role) if f.acting]

    def direction_score(self) -> tuple[float | None, list[str]]:
        """Weighted mean of acting direction factors, in [-1, +1].

        Returns None when nothing is acting — a legitimate outcome that must be
        journalled as such rather than silently treated as neutral.
        """
        acting = self.acting("direction")
        if not acting:
            return None, [f"{f.name}: {f.status}/{f.confidence}" for f in self.by_role("direction")]
        total = sum(f.weight for f in acting)
        if total <= 0:
            return None, ["all direction factors carry zero weight"]
        score = sum(f.sign * f.score * f.weight for f in acting) / total
        return score, [f"{f.name}={f.sign * f.score:+.2f}x{f.weight:.2f}" for f in acting]

    def direction_agreement(self) -> tuple[int, int]:
        """(factors pointing the majority way, factors acting).

        A composite that is a small net of two factors pulling hard in opposite
        directions is not the same as two factors quietly agreeing, and a bare
        weighted mean cannot tell them apart.  The entry gate requires genuine
        agreement, not a surviving remainder.
        """
        acting = self.acting("direction")
        if not acting:
            return 0, 0
        longs = sum(1 for f in acting if f.sign * f.score > 0)
        shorts = sum(1 for f in acting if f.sign * f.score < 0)
        return max(longs, shorts), len(acting)

    def size_score(self) -> tuple[float, list[str]]:
        """Weighted mean of acting size factors; 0.0 when none act."""
        acting = self.acting("size")
        if not acting:
            return 0.0, ["no size factor is acting"]
        total = sum(f.weight for f in acting)
        if total <= 0:
            return 0.0, ["all size factors carry zero weight"]
        score = sum(f.sign * f.score * f.weight for f in acting) / total
        return score, [f"{f.name}={f.sign * f.score:+.2f}x{f.weight:.2f}" for f in acting]

    def as_dict(self) -> dict[str, Any]:
        d_score, d_terms = self.direction_score()
        s_score, s_terms = self.size_score()
        agree, acting_n = self.direction_agreement()
        return {
            "direction_agreement": agree,
            "direction_acting": acting_n,
            "underlying": self.underlying,
            "session_date": self.session_date.isoformat(),
            "as_of": self.as_of.isoformat(),
            "direction_score": d_score,
            "direction_terms": d_terms,
            "size_score": s_score,
            "size_terms": s_terms,
            "n_factors": len(self.factors),
            "n_acting": sum(1 for f in self.factors.values() if f.acting),
            "n_unavailable": sum(1 for f in self.factors.values() if f.status != "ok"),
            "factors": {k: v.as_dict() for k, v in self.factors.items()},
        }


async def build_panel(
    underlying: str,
    session_date: date,
    as_of: datetime,
    *,
    daily_bars: Sequence[DailyBar],
    vol_ctx: dict[str, Any],
    sessions_to_expiry: int | None = None,
    breakeven_ratio: float | None = None,
) -> FactorPanel:
    """Assemble every factor for one (underlying, session).

    A factor that raises is recorded as unavailable rather than allowed to kill
    the pass — a broken feature must degrade the panel, not the lane.
    """
    ctx = FactorContext(
        underlying=underlying,
        session_date=session_date,
        as_of=as_of,
        daily_bars=list(daily_bars),
        vol_ctx=vol_ctx or {},
        sessions_to_expiry=sessions_to_expiry,
        breakeven_ratio=breakeven_ratio,
    )
    try:
        ctx.futures_oi = await load_futures_oi(underlying, session_date)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"[factors] futures OI load failed for {underlying}: {exc}")
    try:
        ctx.chain_oi_change = await load_chain_oi_change(underlying, session_date)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"[factors] chain OI load failed for {underlying}: {exc}")

    panel = FactorPanel(underlying=underlying, session_date=session_date, as_of=as_of)
    for fn in (*DIRECTION_FACTORS, *SIZE_FACTORS):
        try:
            factor = fn(ctx)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"[factors] {fn.__name__} raised for {underlying}: {exc}")
            factor = unavailable(fn.__name__, "unknown", "size", f"raised: {exc}", "speculative")
        panel.factors[factor.name] = factor
    return panel
