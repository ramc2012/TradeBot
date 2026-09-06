"""Assemble an arbitrage-checked index volatility surface from stored candles.

Source of truth is `option_premium_candles` on the 30-MINUTE grid.  That
interval is not an arbitrary choice: the finer 1m/3m/5m grids in this stack are
fed by the ATM tracker and carry only 6-31 strikes, which cannot identify a
smile, while the 30m grid carries 20-130 strikes per (expiry, bar) for the
three indices.

Two data defects are handled inside this module rather than upstream, because
both of them poison a fit silently:

  * DUPLICATE BARS — roughly 28.8% of 30-minute option candles have been
    observed duplicated in this database.  A duplicated strike is weighted
    twice by the fit for no reason, so every read here is `DISTINCT ON` the
    contract key.

  * STALE SNAPSHOTS — the option-chain sweep lags the bar it belongs to by
    45-60 minutes, so a naive "latest row" read mixes a fresh front expiry
    with an hour-old far expiry.  Every slice carries its own `synced_at`
    age and the snapshot refuses to publish past `max_age_minutes`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Sequence

import numpy as np
from loguru import logger
from sqlalchemy import text

from db.database import AsyncSessionLocal
from directional_options.vol.blackscholes import (
    DEFAULT_TICK_SIZE,
    IVResult,
    black76_greeks,
    forward_from_parity,
    implied_vol,
)
from directional_options.vol.svi import SVIFit, calendar_arbitrage, fit_svi_slice

# The three index underlyings this substrate is scoped to.  SENSEX rows are
# stored with market='NSE' in this database despite being a BSE product; the
# label is wrong but the data is real, so the filter is on `underlying`.
INDEX_UNIVERSE: tuple[str, ...] = ("NIFTY", "BANKNIFTY", "SENSEX")

SOURCE_INTERVAL = "30minute"

# Indian index options expire at 15:30 IST = 10:00 UTC.
_EXPIRY_UTC_TIME = time(hour=10, minute=0)
_IST = timezone(timedelta(hours=5, minutes=30))

DEFAULT_RISK_FREE = 0.065

# Beyond this the quote is a lottery ticket whose vega cannot identify sigma;
# including it drags the wing fit around for no informational gain.
MAX_ABS_LOG_MONEYNESS = 0.35


@dataclass(frozen=True)
class SliceQuote:
    """One usable chain point, already inverted to implied vol."""

    strike: float
    option_type: str
    price: float
    log_moneyness: float
    implied_vol: float
    vega: float
    volume: int | None = None
    oi: int | None = None


@dataclass
class SurfaceSlice:
    underlying: str
    ts: datetime
    expiry: date
    tenor_years: float
    forward: float | None
    spot: float | None
    atm_strike: float | None
    quotes: list[SliceQuote] = field(default_factory=list)
    fit: SVIFit | None = None
    rejects: list[dict[str, Any]] = field(default_factory=list)
    stale_seconds: float | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.fit is not None and self.fit.ok

    @property
    def days_to_expiry(self) -> float:
        return self.tenor_years * 365.0

    def atm_total_variance(self) -> float | None:
        if not self.ok:
            return None
        return float(self.fit.params.total_variance(0.0))

    def atm_iv(self) -> float | None:
        w = self.atm_total_variance()
        if w is None or self.tenor_years <= 0:
            return None
        return math.sqrt(max(w, 0.0) / self.tenor_years)

    def as_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "ts": self.ts.isoformat(),
            "expiry": self.expiry.isoformat(),
            "tenor_years": self.tenor_years,
            "days_to_expiry": self.days_to_expiry,
            "forward": self.forward,
            "spot": self.spot,
            "atm_strike": self.atm_strike,
            "atm_iv": self.atm_iv(),
            "atm_total_variance": self.atm_total_variance(),
            "n_quotes": len(self.quotes),
            "n_rejects": len(self.rejects),
            "stale_seconds": self.stale_seconds,
            "fit": self.fit.as_dict() if self.fit else None,
            "reason": self.reason,
        }


@dataclass
class SurfaceSnapshot:
    underlying: str
    ts: datetime
    slices: list[SurfaceSlice] = field(default_factory=list)
    calendar: dict[str, Any] = field(default_factory=dict)
    rejects: list[dict[str, Any]] = field(default_factory=list)
    status: str = "ok"
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok" and any(s.ok for s in self.slices)

    def usable_slices(self) -> list[SurfaceSlice]:
        return [s for s in self.slices if s.ok]

    def as_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "ts": self.ts.isoformat(),
            "status": self.status,
            "reason": self.reason,
            "n_slices": len(self.slices),
            "n_usable": len(self.usable_slices()),
            "calendar": self.calendar,
            "slices": [s.as_dict() for s in self.slices],
            "n_rejects": len(self.rejects),
        }


def expiry_datetime_utc(expiry: date) -> datetime:
    return datetime.combine(expiry, _EXPIRY_UTC_TIME, tzinfo=timezone.utc)


def tenor_years(bar_ts: datetime, expiry: date) -> float:
    """Calendar-day year fraction to the 15:30 IST expiry stamp."""
    delta = expiry_datetime_utc(expiry) - bar_ts
    return max(delta.total_seconds() / (365.0 * 86400.0), 0.0)


_SNAPSHOT_SQL = text(
    """
    WITH deduped AS (
        SELECT DISTINCT ON (expiry, strike, option_type)
               expiry, strike, option_type, close, volume, oi,
               underlying_price, synced_at, time
        FROM option_premium_candles
        WHERE underlying = :underlying
          AND interval = :interval
          AND time = :bar_ts
        ORDER BY expiry, strike, option_type, synced_at DESC
    )
    SELECT * FROM deduped
    """
)

# The bar to build from.  `time` is bounded with literal timestamps on both
# sides and is never wrapped in a function — wrapping the partitioning column
# has previously made the planner scan every chunk and killed a live session.
#
# "Latest" is NOT good enough on its own.  The final one or two bars of every
# session carry only the 1-2 ATM-tracker strikes because the chain sweep lands
# 45-60 minutes behind the bar it belongs to, so a naive max(time) hands back a
# bar that cannot identify a smile.  The breadth floor is part of the query.
_LATEST_BAR_SQL = text(
    """
    SELECT time AS ts, count(DISTINCT strike) AS strikes
    FROM option_premium_candles
    WHERE underlying = :underlying
      AND interval = :interval
      AND time >= :lower
      AND time <= :upper
    GROUP BY time
    HAVING count(DISTINCT strike) >= :min_strikes
    ORDER BY time DESC
    LIMIT 1
    """
)

# Below this many distinct strikes in a bar the chain is the ATM tracker, not
# a chain, and no slice in it will clear MIN_POINTS_FOR_FIT.
MIN_BAR_STRIKES = 12


async def latest_bar_ts(
    underlying: str,
    *,
    as_of: datetime | None = None,
    lookback_hours: int = 96,
    interval: str = SOURCE_INTERVAL,
    min_strikes: int = MIN_BAR_STRIKES,
) -> datetime | None:
    """Most recent bar that actually carries a chain, not just the ATM tracker."""
    upper = as_of or datetime.now(timezone.utc)
    lower = upper - timedelta(hours=lookback_hours)
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                _LATEST_BAR_SQL,
                {
                    "underlying": underlying,
                    "interval": interval,
                    "lower": lower,
                    "upper": upper,
                    "min_strikes": int(min_strikes),
                },
            )
        ).first()
    return row.ts if row and row.ts else None


async def load_chain_bar(
    underlying: str,
    bar_ts: datetime,
    *,
    interval: str = SOURCE_INTERVAL,
) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                _SNAPSHOT_SQL,
                {"underlying": underlying, "interval": interval, "bar_ts": bar_ts},
            )
        ).mappings().all()
    return [dict(r) for r in rows]


def _atm_pair_forward(
    rows: Sequence[dict[str, Any]],
    spot: float | None,
    T: float,
    r: float,
) -> tuple[float | None, float | None, str]:
    """Forward from put-call parity at the strike closest to spot.

    Falls back through progressively further strikes because the nearest one
    is sometimes missing a side.  If no complete pair exists the slice is
    unusable — we do NOT fall back to `forward = spot`, which would silently
    bake a zero-carry assumption into the whole smile.
    """
    by_strike: dict[float, dict[str, float]] = {}
    for row in rows:
        strike = row.get("strike")
        opt = (row.get("option_type") or "").upper()
        close = row.get("close")
        if strike is None or close is None or opt not in {"CE", "PE"}:
            continue
        by_strike.setdefault(float(strike), {})[opt] = float(close)

    pairs = [(k, v) for k, v in by_strike.items() if "CE" in v and "PE" in v]
    if not pairs:
        return None, None, "no complete call/put pair for parity"

    anchor = spot if spot and spot > 0 else float(np.median([k for k, _ in pairs]))
    pairs.sort(key=lambda item: abs(item[0] - anchor))

    for strike, legs in pairs[:3]:
        forward = forward_from_parity(legs["CE"], legs["PE"], strike, T, r)
        if forward and forward > 0:
            return forward, strike, ""
    return None, None, "parity produced a non-positive forward"


def build_slice_from_rows(
    underlying: str,
    ts: datetime,
    expiry: date,
    rows: Sequence[dict[str, Any]],
    *,
    r: float = DEFAULT_RISK_FREE,
    tick_size: float = DEFAULT_TICK_SIZE,
    min_vega_ticks: float = 1.0,
    max_abs_k: float = MAX_ABS_LOG_MONEYNESS,
) -> SurfaceSlice:
    """Invert one expiry's quotes to IV and fit a slice.

    Only OTM quotes are inverted.  An ITM index option's quote is mostly
    intrinsic, its time value is a small difference of two large numbers, and
    its bid/ask in vol terms is enormous — using it makes the fit worse, not
    better, and put-call parity already carries whatever information it holds.
    """
    T = tenor_years(ts, expiry)
    spot_values = [float(r_["underlying_price"]) for r_ in rows if r_.get("underlying_price") is not None]
    spot = float(np.median(spot_values)) if spot_values else None

    synced = [r_["synced_at"] for r_ in rows if r_.get("synced_at") is not None]
    stale_seconds = (max(synced) - ts).total_seconds() if synced else None

    sl = SurfaceSlice(
        underlying=underlying,
        ts=ts,
        expiry=expiry,
        tenor_years=T,
        forward=None,
        spot=spot,
        atm_strike=None,
        stale_seconds=stale_seconds,
    )

    if T <= 0:
        sl.reason = "expired at or before this bar"
        return sl

    forward, atm_strike, why = _atm_pair_forward(rows, spot, T, r)
    sl.forward, sl.atm_strike = forward, atm_strike
    if forward is None:
        sl.reason = why
        return sl

    if spot is None:
        # `underlying_price` is null on roughly a fifth of slices.  The forward
        # discounted back is a principled stand-in — over these maturities the
        # two differ by well under a tenth of a percent — and it keeps gamma,
        # minimum-variance delta and the P&L attribution defined instead of
        # silently zeroing their spot terms.
        spot = forward * math.exp(-r * T)
        sl.spot = spot
        sl.reason = "spot inferred from the forward (underlying_price was null)"

    quotes: list[SliceQuote] = []
    for row in rows:
        strike = row.get("strike")
        opt = (row.get("option_type") or "").upper()
        close = row.get("close")
        if strike is None or close is None or opt not in {"CE", "PE"}:
            continue
        strike = float(strike)
        price = float(close)
        k = math.log(strike / forward)

        if abs(k) > max_abs_k:
            continue
        # OTM only: calls above the forward, puts below it.
        if (opt == "CE" and k < 0) or (opt == "PE" and k > 0):
            continue

        res: IVResult = implied_vol(
            price, forward, strike, T, opt, r, tick_size=tick_size, min_vega_ticks=min_vega_ticks
        )
        if not res.ok:
            sl.rejects.append(
                {
                    "strike": strike,
                    "option_type": opt,
                    "price": price,
                    "log_moneyness": k,
                    "status": res.status,
                    "reason": res.reason,
                }
            )
            continue

        greeks = black76_greeks(forward, strike, T, res.sigma, opt, r, spot=spot)
        quotes.append(
            SliceQuote(
                strike=strike,
                option_type=opt,
                price=price,
                log_moneyness=k,
                implied_vol=float(res.sigma),
                vega=float(greeks.vega),
                volume=_as_int(row.get("volume")),
                oi=_as_int(row.get("oi")),
            )
        )

    sl.quotes = sorted(quotes, key=lambda q: q.log_moneyness)
    if not sl.quotes:
        sl.reason = "no quote survived the identification test"
        return sl

    sl.fit = fit_svi_slice(
        [q.log_moneyness for q in sl.quotes],
        [q.implied_vol for q in sl.quotes],
        T,
        weights=[q.vega for q in sl.quotes],
        strikes=[q.strike for q in sl.quotes],
    )
    if not sl.fit.ok:
        sl.reason = sl.fit.reason or sl.fit.status
    return sl


def build_snapshot_from_rows(
    underlying: str,
    ts: datetime,
    rows: Sequence[dict[str, Any]],
    *,
    r: float = DEFAULT_RISK_FREE,
    max_age_minutes: float | None = None,
    min_expiries: int = 1,
) -> SurfaceSnapshot:
    snap = SurfaceSnapshot(underlying=underlying, ts=ts)
    if not rows:
        snap.status = "no_data"
        snap.reason = f"no {SOURCE_INTERVAL} chain rows at {ts.isoformat()}"
        return snap

    by_expiry: dict[date, list[dict[str, Any]]] = {}
    for row in rows:
        expiry = row.get("expiry")
        if expiry is None:
            continue
        by_expiry.setdefault(expiry, []).append(row)

    for expiry in sorted(by_expiry):
        sl = build_slice_from_rows(underlying, ts, expiry, by_expiry[expiry], r=r)
        if max_age_minutes is not None and sl.stale_seconds is not None:
            if sl.stale_seconds > max_age_minutes * 60.0:
                sl.fit = None
                sl.reason = (
                    f"slice synced {sl.stale_seconds / 60.0:.1f}m after its bar, "
                    f"past the {max_age_minutes:.0f}m ceiling"
                )
        snap.slices.append(sl)
        snap.rejects.extend(
            {**rej, "expiry": expiry.isoformat()} for rej in sl.rejects
        )

    usable = snap.usable_slices()
    if len(usable) < min_expiries:
        snap.status = "unusable"
        snap.reason = f"{len(usable)} usable slice(s), need {min_expiries}"
        return snap

    k_lo = min((s.fit.k_min for s in usable if s.fit and s.fit.k_min is not None), default=-0.2)
    k_hi = max((s.fit.k_max for s in usable if s.fit and s.fit.k_max is not None), default=0.2)
    snap.calendar = calendar_arbitrage(
        [(s.tenor_years, s.fit.params) for s in usable],
        k_grid=np.linspace(k_lo, k_hi, 81),
    )
    return snap


async def build_surface_snapshot(
    underlying: str,
    *,
    bar_ts: datetime | None = None,
    as_of: datetime | None = None,
    r: float = DEFAULT_RISK_FREE,
    max_age_minutes: float | None = 240.0,
    interval: str = SOURCE_INTERVAL,
) -> SurfaceSnapshot:
    """Build the surface for one index at one bar.

    `max_age_minutes` defaults to 4 hours rather than something tight because
    the chain sweep for this stack legitimately lands 45-60 minutes behind its
    bar, and the last two bars of a session are still filling after the close.
    """
    symbol = underlying.strip().upper()
    if symbol not in INDEX_UNIVERSE:
        return SurfaceSnapshot(
            underlying=symbol,
            ts=bar_ts or as_of or datetime.now(timezone.utc),
            status="out_of_scope",
            reason=f"{symbol} is not one of {INDEX_UNIVERSE}; index-only substrate",
        )

    ts = bar_ts or await latest_bar_ts(symbol, as_of=as_of, interval=interval)
    if ts is None:
        return SurfaceSnapshot(
            underlying=symbol,
            ts=as_of or datetime.now(timezone.utc),
            status="no_data",
            reason="no chain bar found in the lookback window",
        )

    rows = await load_chain_bar(symbol, ts, interval=interval)
    return build_snapshot_from_rows(symbol, ts, rows, r=r, max_age_minutes=max_age_minutes)


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
