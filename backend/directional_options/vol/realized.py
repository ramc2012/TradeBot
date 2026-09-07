"""Realised volatility, vol cones, and the variance risk premium.

This is the half of the vol picture that has real history.  Index spot bars in
this database go back to 2024-01-01 on the 30-minute grid, so realised vol,
its cone and its percentiles are all measurable over ~2.7 years.  Implied vol
only became measurable when the chain sweep widened on 2026-08-31, so an
IV PERCENTILE is not available yet and will not be for months.

That asymmetry decides the lane's vol gate.  A percentile of IV needs IV
history and cannot be computed; the variance risk premium (today's implied
against trailing realised) needs only one implied number and a long realised
series, and is available right now.  VRP is therefore the gate, and the IV
cone is left to accumulate.

Three estimators are offered because they disagree in informative ways: a
close-to-close number that is high while Parkinson is low means the index is
gapping between sessions rather than moving within them, which is a different
trade from a trending tape.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

import numpy as np
from sqlalchemy import text

from db.database import AsyncSessionLocal

TRADING_DAYS_PER_YEAR = 252.0

DEFAULT_CONE_WINDOWS: tuple[int, ...] = (5, 10, 20, 60, 120)

Estimator = str  # "close_close" | "parkinson" | "garman_klass"


@dataclass(frozen=True)
class DailyBar:
    """One session's OHLC.

    `contiguous` says whether the immediately preceding OBSERVED session was
    also retained.  It exists because dropping a contaminated or partial day
    and then differencing consecutive survivors silently turns a multi-day move
    into a single-day return, which inflated close-to-close realised vol on
    NIFTY from 5.5% to 10.0% purely as an artifact of the cleaning step.
    """

    day: date
    open: float
    high: float
    low: float
    close: float
    contiguous: bool = True


# Daily OHLC folded out of the 30-minute grid in IST, because the session
# boundary that matters is the Indian one, not UTC midnight.  `time` is bounded
# with literal timestamps and never wrapped in a function.
#
# The instrument_key filter is not cosmetic.  `underlying_spot_candles` holds
# TWO key conventions per index — the Upstox one ("NSE_INDEX|Nifty 50", ~8,000
# bars back to 2024) and a Fyers one ("NSE:NIFTY50-INDEX", ~50 bars in July
# 2026) — and the primary key includes instrument_key, so both survive for the
# same bar.  The Fyers-keyed rows carry cross-symbol tick contamination: they
# put a 28,532 high on NIFTY and a 48,243 low on BANKNIFTY on 14/15-Jul-2026.
# Folding a day without picking one key first takes max(high) and min(low)
# ACROSS the two, which is what pushed 60-day realised vol on NIFTY to 38%
# against a true ~11%.  We take the dominant key by row count (self-healing if
# the convention ever changes) and then one row per bar by source preference.
_DAILY_SQL = text(
    """
    WITH dominant AS (
        SELECT instrument_key
        FROM underlying_spot_candles
        WHERE underlying = :underlying
          AND interval = :interval
          AND time >= :lower
          AND time <= :upper
        GROUP BY instrument_key
        ORDER BY count(*) DESC
        LIMIT 1
    ), picked AS (
        -- The time-of-day predicate is an ADDITIONAL filter; `c.time` still
        -- carries its literal upper/lower bounds above, so chunk exclusion is
        -- untouched.  It is needed because the `live_tick` source writes bars
        -- as late as 18:00 IST, well after the 15:30 close, and those post-close
        -- rows land in the daily fold as spurious highs, lows and closes.
        SELECT DISTINCT ON (c.time)
               c.time, c.open, c.high, c.low, c.close
        FROM underlying_spot_candles c
        JOIN dominant d ON d.instrument_key = c.instrument_key
        WHERE c.underlying = :underlying
          AND c.interval = :interval
          AND c.time >= :lower
          AND c.time <= :upper
          AND (c.time AT TIME ZONE 'Asia/Kolkata')::time
              BETWEEN TIME '09:15' AND TIME '15:30'
        ORDER BY c.time,
                 CASE c.source
                     WHEN 'upstox_spot' THEN 0
                     WHEN 'live_tick' THEN 1
                     ELSE 2
                 END,
                 c.synced_at DESC
    )
    SELECT (time AT TIME ZONE 'Asia/Kolkata')::date AS day,
           (array_agg(open  ORDER BY time ASC ))[1] AS open,
           max(high)                                AS high,
           min(low)                                 AS low,
           (array_agg(close ORDER BY time DESC))[1] AS close,
           count(*)                                 AS n_bars
    FROM picked
    GROUP BY 1
    ORDER BY 1
    """
)

# An index that travelled more than this much between its high and its low in
# one session is a circuit-breaker day or a contaminated bar.  Either way it is
# not something a 120-day realised-vol window should absorb silently, so the
# day is dropped and REPORTED rather than quietly averaged in.
MAX_DAILY_LOG_RANGE = 0.15

# The sharper discriminator.  Tick contamination shows up as a high or a low
# that sits far outside the open/close BODY while the body itself is ordinary —
# NIFTY on 08-Jul-2026 opened 24,398, closed 23,882 and printed a 27,094 high,
# an 11% excursion above a 2% body.  A genuine violent session moves the body
# too, so the excursion stays small even when the total range is large.  This
# catches 08-Jul, which the total-range ceiling alone does not.
MAX_BODY_EXCURSION = 0.06

# A 30-minute NSE/BSE session is 13 bars.  Far fewer means the day is partly
# missing, and a daily close taken from a partial day is not a close.
MIN_BARS_PER_DAY = 8


@dataclass
class DailySeries:
    """Clean daily bars plus an explicit account of what was thrown away."""

    underlying: str
    bars: list[DailyBar] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "n_bars": len(self.bars),
            "first_day": self.bars[0].day.isoformat() if self.bars else None,
            "last_day": self.bars[-1].day.isoformat() if self.bars else None,
            "n_rejected": len(self.rejected),
            "rejected": self.rejected[:20],
        }


async def load_daily_series(
    underlying: str,
    *,
    as_of: datetime | None = None,
    lookback_days: int = 900,
    interval: str = "30minute",
    max_log_range: float = MAX_DAILY_LOG_RANGE,
    max_body_excursion: float = MAX_BODY_EXCURSION,
    min_bars_per_day: int = MIN_BARS_PER_DAY,
) -> DailySeries:
    upper = as_of or datetime.now(timezone.utc)
    lower = upper - timedelta(days=lookback_days)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                _DAILY_SQL,
                {"underlying": underlying, "interval": interval, "lower": lower, "upper": upper},
            )
        ).mappings().all()

    out = DailySeries(underlying=underlying)
    previous_retained = False
    for row in rows:
        try:
            o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
        except (TypeError, ValueError):
            out.rejected.append({"day": str(row.get("day")), "reason": "non-numeric ohlc"})
            previous_retained = False
            continue
        if min(o, h, l, c) <= 0 or h < l:
            out.rejected.append({"day": str(row["day"]), "reason": "non-positive or inverted ohlc"})
            previous_retained = False
            continue
        n_bars = int(row["n_bars"]) if row.get("n_bars") is not None else None
        if n_bars is not None and n_bars < min_bars_per_day:
            out.rejected.append(
                {
                    "day": str(row["day"]),
                    "reason": "partial session",
                    "n_bars": n_bars,
                }
            )
            previous_retained = False
            continue

        log_range = math.log(h / l)
        if log_range > max_log_range:
            out.rejected.append(
                {
                    "day": str(row["day"]),
                    "reason": "intraday range beyond the contamination ceiling",
                    "log_range": round(log_range, 4),
                    "high": h,
                    "low": l,
                    "n_bars": n_bars,
                }
            )
            previous_retained = False
            continue

        body_hi, body_lo = max(o, c), min(o, c)
        up_excursion = (h - body_hi) / body_hi
        down_excursion = (body_lo - l) / body_lo
        excursion = max(up_excursion, down_excursion)
        if excursion > max_body_excursion:
            out.rejected.append(
                {
                    "day": str(row["day"]),
                    "reason": "high/low far outside the open-close body",
                    "excursion": round(excursion, 4),
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,
                    "n_bars": n_bars,
                }
            )
            previous_retained = False
            continue

        out.bars.append(
            DailyBar(day=row["day"], open=o, high=h, low=l, close=c, contiguous=previous_retained)
        )
        previous_retained = True
    return out


async def load_daily_bars(
    underlying: str,
    *,
    as_of: datetime | None = None,
    lookback_days: int = 900,
    interval: str = "30minute",
) -> list[DailyBar]:
    series = await load_daily_series(
        underlying, as_of=as_of, lookback_days=lookback_days, interval=interval
    )
    return series.bars


def close_to_close_rv(
    bars: Sequence[DailyBar],
    window: int,
    min_coverage: float = 0.8,
) -> float | None:
    """Annualised stdev of daily log returns over the last `window` sessions.

    Returns that span a dropped session are EXCLUDED, not stretched.  The
    window still covers `window` sessions of wall-clock, so the number stays
    comparable across dates; it is refused outright when fewer than
    `min_coverage` of the returns in that span survived, because at that point
    the estimate describes the surviving days rather than the period.
    """
    if len(bars) < window + 1:
        return None
    window_bars = bars[-(window + 1):]
    rets = [
        math.log(curr.close / prev.close)
        for prev, curr in zip(window_bars, window_bars[1:])
        if curr.contiguous and prev.close > 0 and curr.close > 0
    ]
    if len(rets) < 2 or len(rets) < min_coverage * window:
        return None
    return float(np.std(np.array(rets, dtype=float), ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))


def parkinson_rv(bars: Sequence[DailyBar], window: int) -> float | None:
    """High/low range estimator — about 5x more efficient than close-to-close.

    Blind to overnight gaps by construction, which is exactly what makes the
    close-to-close/Parkinson gap informative rather than redundant.
    """
    if len(bars) < window:
        return None
    sample = bars[-window:]
    total = sum(math.log(b.high / b.low) ** 2 for b in sample)
    var = total / (4.0 * math.log(2.0) * window)
    return float(math.sqrt(max(var, 0.0) * TRADING_DAYS_PER_YEAR))


def garman_klass_rv(bars: Sequence[DailyBar], window: int) -> float | None:
    """Range plus open/close estimator; the most efficient of the three."""
    if len(bars) < window:
        return None
    sample = bars[-window:]
    total = 0.0
    for b in sample:
        hl = math.log(b.high / b.low)
        co = math.log(b.close / b.open)
        total += 0.5 * hl * hl - (2.0 * math.log(2.0) - 1.0) * co * co
    var = total / window
    return float(math.sqrt(max(var, 0.0) * TRADING_DAYS_PER_YEAR))


_ESTIMATORS = {
    "close_close": close_to_close_rv,
    "parkinson": parkinson_rv,
    "garman_klass": garman_klass_rv,
}


def realized_vol(bars: Sequence[DailyBar], window: int, estimator: Estimator = "close_close") -> float | None:
    fn = _ESTIMATORS.get(estimator)
    if fn is None:
        raise ValueError(f"unknown estimator {estimator!r}; expected one of {sorted(_ESTIMATORS)}")
    return fn(bars, window)


def rolling_realized_vol(
    bars: Sequence[DailyBar],
    window: int,
    estimator: Estimator = "close_close",
) -> list[tuple[date, float]]:
    """The full rolling series, which is what the cone percentiles come from."""
    fn = _ESTIMATORS[estimator]
    out: list[tuple[date, float]] = []
    start = window + (1 if estimator == "close_close" else 0)
    for end in range(start, len(bars) + 1):
        value = fn(bars[:end], window)
        if value is not None and math.isfinite(value):
            out.append((bars[end - 1].day, value))
    return out


@dataclass
class ConeRung:
    window_days: int
    current: float | None
    p_min: float | None
    p25: float | None
    p50: float | None
    p75: float | None
    p_max: float | None
    percentile_of_current: float | None
    n_observations: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_days": self.window_days,
            "current": self.current,
            "min": self.p_min,
            "p25": self.p25,
            "median": self.p50,
            "p75": self.p75,
            "max": self.p_max,
            "percentile_of_current": self.percentile_of_current,
            "n_observations": self.n_observations,
        }


@dataclass
class VolCone:
    underlying: str
    as_of: date | None
    estimator: Estimator
    rungs: list[ConeRung] = field(default_factory=list)
    n_bars: int = 0
    status: str = "ok"
    reason: str = ""

    def rung(self, window_days: int) -> ConeRung | None:
        for r in self.rungs:
            if r.window_days == window_days:
                return r
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "estimator": self.estimator,
            "n_bars": self.n_bars,
            "status": self.status,
            "reason": self.reason,
            "rungs": [r.as_dict() for r in self.rungs],
        }


def build_cone(
    underlying: str,
    bars: Sequence[DailyBar],
    *,
    windows: Sequence[int] = DEFAULT_CONE_WINDOWS,
    estimator: Estimator = "close_close",
    min_observations: int = 60,
) -> VolCone:
    cone = VolCone(
        underlying=underlying,
        as_of=bars[-1].day if bars else None,
        estimator=estimator,
        n_bars=len(bars),
    )
    if len(bars) < max(windows) + 2:
        cone.status = "insufficient"
        cone.reason = f"{len(bars)} daily bars, need > {max(windows) + 2}"
        return cone

    for window in windows:
        series = rolling_realized_vol(bars, window, estimator)
        values = np.array([v for _, v in series], dtype=float)
        if values.size < min_observations:
            cone.rungs.append(
                ConeRung(window, None, None, None, None, None, None, int(values.size))
            )
            continue
        current = float(values[-1])
        pct = float((values <= current).mean() * 100.0)
        cone.rungs.append(
            ConeRung(
                window_days=window,
                current=current,
                p_min=float(np.min(values)),
                p25=float(np.percentile(values, 25)),
                p50=float(np.percentile(values, 50)),
                p75=float(np.percentile(values, 75)),
                p_max=float(np.max(values)),
                percentile_of_current=pct,
                n_observations=int(values.size),
            )
        )
    return cone


@dataclass
class VariancePremium:
    """Implied minus realised, in vol points and as a ratio.

    `horizon_days` is the maturity the implied number came from; the realised
    window is matched to it so the two numbers describe the same horizon.  A
    30-day implied compared against a 5-day realised is a common and
    meaningless comparison.
    """

    underlying: str
    as_of: datetime
    horizon_days: int
    implied_vol: float
    realized_vol: float | None
    estimator: Estimator
    spread: float | None            # implied - realised, in vol (0.01 = 1 point)
    ratio: float | None             # implied / realised
    realized_percentile: float | None
    status: str = "ok"
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "as_of": self.as_of.isoformat(),
            "horizon_days": self.horizon_days,
            "implied_vol": self.implied_vol,
            "realized_vol": self.realized_vol,
            "estimator": self.estimator,
            "spread": self.spread,
            "spread_vol_points": None if self.spread is None else self.spread * 100.0,
            "ratio": self.ratio,
            "realized_percentile": self.realized_percentile,
            "status": self.status,
            "reason": self.reason,
        }


def variance_risk_premium(
    underlying: str,
    as_of: datetime,
    implied_vol_value: float,
    horizon_days: int,
    bars: Sequence[DailyBar],
    *,
    estimator: Estimator = "garman_klass",
    cone: VolCone | None = None,
) -> VariancePremium:
    """IV at a horizon against realised vol over a matched trailing window.

    The realised window is the horizon converted from calendar days to trading
    days (x 252/365) and floored at 5, so a 7-day implied is compared against
    roughly a trading week of realised rather than a calendar week that
    contains a weekend of zero movement.
    """
    window = max(int(round(horizon_days * TRADING_DAYS_PER_YEAR / 365.0)), 5)
    rv = realized_vol(bars, window, estimator)

    vp = VariancePremium(
        underlying=underlying,
        as_of=as_of,
        horizon_days=horizon_days,
        implied_vol=float(implied_vol_value),
        realized_vol=rv,
        estimator=estimator,
        spread=None,
        ratio=None,
        realized_percentile=None,
    )
    if rv is None or rv <= 0:
        vp.status = "no_realized"
        vp.reason = f"cannot compute {estimator} realised vol over a {window}-day window"
        return vp

    vp.spread = float(implied_vol_value) - rv
    vp.ratio = float(implied_vol_value) / rv

    # The percentile is computed at the window ACTUALLY used, not looked up in
    # a cone that may not carry it.  A 30-day horizon converts to a 21-session
    # window, the cone is built at (5, 10, 20, 60, 120), and the lookup
    # therefore returned None at every tenor except the ones that floor to 5 —
    # silently, with no warning, so the feature read as "not applicable" rather
    # than "never implemented".
    if cone is not None:
        rung = cone.rung(window)
        if rung is not None and rung.percentile_of_current is not None:
            vp.realized_percentile = rung.percentile_of_current
    if vp.realized_percentile is None:
        history = [value for _, value in rolling_realized_vol(bars, window, estimator)]
        if len(history) >= 60:
            below = sum(1 for value in history if value <= rv)
            vp.realized_percentile = 100.0 * below / len(history)
        else:
            vp.reason = (
                f"realised percentile needs 60 rolling observations at a {window}-session "
                f"window; only {len(history)} available"
            )
    return vp


async def realized_context(
    underlying: str,
    *,
    as_of: datetime | None = None,
    lookback_days: int = 900,
    estimator: Estimator = "garman_klass",
    windows: Sequence[int] = DEFAULT_CONE_WINDOWS,
) -> tuple[DailySeries, VolCone]:
    series = await load_daily_series(underlying, as_of=as_of, lookback_days=lookback_days)
    cone = build_cone(underlying, series.bars, windows=windows, estimator=estimator)
    return series, cone
