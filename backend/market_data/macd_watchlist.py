"""MACD session watchlist — the frozen, sticky, warmed ATM ladder.

Owner spec (2026-07-20), verbatim intent:

    "Let it have its own watchlist with strike it monitors with spot and
     indicator values. ... For MACD, MACD refined - only these two lanes ...
     Their watchlist is same. First pre-market fix the expiries to be traded ...
     Once a position is entered on a strike that strike persists till the
     closure and new strike for that instrument fetched after closure based on
     spot price at that time."

    "new ATM CE/PE does not mean exact atm strike. it is next or just below
     spot liquid contract."   → free pick by LIQUIDITY, no ITM/OTM bias.

    "when picking a new strike for watchlist its history also be fetched for
     computing the indicator values."

This module owns SESSION STATE for the two surviving MACD lanes
(``s1_atm_30m_macd`` and ``macd_refined`` — they share ONE watchlist):

  * the pre-open build (anchor price → expiry → liquid strike → freeze),
  * the sticky-strike pins (durable, read from Postgres, never memory),
  * the history warm-up bookkeeping (so "no signal" is always distinguishable
    from "not enough history"),
  * the prior-session volume history that the liquidity selector ranks on.

It writes ONE row per (session_date, underlying, option_type) into the sidecar
table ``macd_session_watchlist`` (migration 028).  It deliberately does NOT
change ``atm_option_watchlist_snapshots`` semantics — that hypertable is read by
17 modules and stores a time-series SAMPLE of a contract; everything here is
session state.  The only thing existing consumers observe is that the ``strike``
VALUE stops drifting intraday, which is the moving-ATM defect (documented cause
of the option-premium chart gaps) we are deliberately removing.

NOTHING in this module changes strategy math.  No entry rule, exit rule, gate,
threshold or sizing formula is touched: this is instrument selection and
scheduling only.

Every path is flag-gated OFF by default (see core/config.py):
``MACD_PREOPEN_WATCHLIST_ENABLED``, ``MACD_STICKY_STRIKES_ENABLED``,
``MACD_LIQUID_STRIKE_SELECTION_ENABLED``, ``MACD_WARMUP_ENABLED``,
``EXPIRY_POLICY_ENABLED``.
"""
from __future__ import annotations

import asyncio
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any, Callable, Iterable, Optional, Sequence

from loguru import logger
from sqlalchemy import text

from db.database import AsyncSessionLocal

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

SESSION_TABLE = "macd_session_watchlist"

# ── Price anchors, in preference order. NEVER silently mixed: the label is
# written on every row so a session where prev_close dominated for stocks is a
# REPORTED FACT, not a hidden degradation.
ANCHOR_PREOPEN_WS_TICK = "preopen_ws_tick"
ANCHOR_PREOPEN_EQUILIBRIUM = "preopen_equilibrium_ltp"
ANCHOR_PREV_CLOSE = "prev_close"
ANCHOR_NONE = "unavailable"

# Strike statuses (terminal exclusions are `no_liquid_strike` and `not_ready`).
STATUS_OK = "ok"
STATUS_NO_LIQUID = "no_liquid_strike"
STATUS_NOT_READY = "not_ready"

WARMUP_READY = "ready"
WARMUP_NOT_READY = "not_ready"

# Prior-session lookback for the liquidity history.  FIVE sessions = one
# trading week.  Long enough that a single quiet day or an exchange holiday
# cannot zero a genuinely liquid strike; short enough that it still reflects
# where interest actually sits after spot has moved.  Holiday-aware (the
# session list is walked through core.expiry_policy, not bare Mon–Fri).
LIQUIDITY_LOOKBACK_SESSIONS = 5

# NSE continuous session 09:15–15:30 IST = 6h15m ⇒ 13 x 30-minute bars/session.
BARS_PER_SESSION_30M = 13


def _now_ist() -> datetime:
    return datetime.now(IST)


def _session_bounds_utc(day: date) -> tuple[datetime, datetime]:
    """(start, end) UTC covering one IST calendar day.

    Used to bound `time` DIRECTLY with literals so TimescaleDB can still do
    plan-time chunk exclusion (wrapping `time` in a function is what OOM-killed
    Postgres on 2026-07-20 — see the standing PG query rule).
    """
    start_ist = datetime.combine(day, dt_time(0, 0), tzinfo=IST)
    return start_ist.astimezone(UTC), (start_ist + timedelta(days=1)).astimezone(UTC)


def prior_trading_sessions(count: int = LIQUIDITY_LOOKBACK_SESSIONS, *, today: Optional[date] = None) -> list[date]:
    """The `count` NSE trading days strictly BEFORE `today`, ascending.

    Holiday-aware via core.expiry_policy (which reads core.trading_calendar —
    the same table the ops surface edits), so a holiday week does not silently
    shorten the lookback to four real sessions.
    """
    from core.expiry_policy import expiry_policy

    today = today or _now_ist().date()
    out: list[date] = []
    cursor = today
    guard = 0
    while len(out) < max(count, 0) and guard < 60:
        cursor -= timedelta(days=1)
        guard += 1
        if expiry_policy._is_trading_day(cursor):
            out.append(cursor)
    return sorted(out)


# ══════════════════════════════════════════════════════════════════════════
# (4) History warm-up requirement — DERIVED from the indicator params
# ══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class WarmupRequirement:
    interval: str
    macd_fast: int
    macd_slow: int
    macd_signal: int
    min_bars: int        # bars below which MACD MUST refuse to produce a value
    target_bars: int     # bars we actually fetch, for a STABLE (not merely valid) EMA
    bars_per_session: int
    min_sessions: float
    target_sessions: float
    source: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "interval": self.interval,
            "macd": [self.macd_fast, self.macd_slow, self.macd_signal],
            "min_bars": self.min_bars,
            "target_bars": self.target_bars,
            "bars_per_session": self.bars_per_session,
            "min_sessions": round(self.min_sessions, 2),
            "target_sessions": round(self.target_sessions, 2),
            "source": self.source,
        }


def warmup_requirement() -> WarmupRequirement:
    """How many bars a NEW strike needs before MACD may be computed on it.

    DERIVED, never guessed.  MACD(fast, slow, signal) cannot produce a valid
    signal line before ``slow + signal`` closes; ``analytics.technicals``
    already enforces exactly that (``MACD_MIN_BARS = 26 + 9``) and returns
    None below it, so the compute layer never fabricates.  What this function
    adds is LEGIBILITY: the watchlist row records the resulting bar count so a
    silent "no signal" can always be told apart from "not enough history".

    Both surviving MACD lanes carry the SAME params on the SAME timeframe
    (agent/strategy_config.py MACD_FAST/SLOW/SIGNAL/INTERVAL and
    macd_refined/config.py signal.macd_*, timeframe) — they share one
    watchlist, so a divergence is a real configuration error and is logged
    LOUDLY rather than silently resolved.
    """
    fast, slow, signal, interval = 12, 26, 9, "30minute"
    source = "fallback_literals"
    try:
        from agent import strategy_config as _sc

        fast = int(_sc.MACD_FAST)
        slow = int(_sc.MACD_SLOW)
        signal = int(_sc.MACD_SIGNAL)
        interval = str(_sc.MACD_INTERVAL)
        source = "agent.strategy_config"
    except Exception as exc:  # noqa: BLE001
        logger.warning("[MACD watchlist] could not read agent.strategy_config MACD params ({!r})", exc)

    try:
        from macd_refined.config import clone_default_config as _load_refined

        refined = _load_refined()
        rsig = dict((refined or {}).get("signal") or {})
        r_fast = int(rsig.get("macd_fast", fast))
        r_slow = int(rsig.get("macd_slow", slow))
        r_signal = int(rsig.get("macd_signal", signal))
        r_interval = str((refined or {}).get("timeframe", interval))
        if (r_fast, r_slow, r_signal, r_interval) != (fast, slow, signal, interval):
            logger.error(
                "[MACD watchlist] MACD lanes DISAGREE on indicator params — s1={} refined={}. "
                "They share ONE watchlist, so the warm-up depth is sized on the STRICTER "
                "(larger) requirement. Reconcile agent/strategy_config.py with "
                "macd_refined/config.py.",
                (fast, slow, signal, interval),
                (r_fast, r_slow, r_signal, r_interval),
            )
            slow = max(slow, r_slow)
            signal = max(signal, r_signal)
            source += "+macd_refined(divergent)"
        else:
            source += "+macd_refined(agree)"
    except Exception as exc:  # noqa: BLE001
        logger.warning("[MACD watchlist] could not read macd_refined config MACD params ({!r})", exc)

    min_bars = slow + signal
    try:
        from analytics.technicals import MACD_MIN_BARS as _compute_min

        if int(_compute_min) != min_bars:
            logger.error(
                "[MACD watchlist] derived MACD min bars ({}) != analytics.technicals."
                "MACD_MIN_BARS ({}). Using the STRICTER value so we can never feed a short "
                "series to the indicator.",
                min_bars,
                int(_compute_min),
            )
            min_bars = max(min_bars, int(_compute_min))
    except Exception:  # noqa: BLE001
        pass

    # option_history.load_candles' own default (80) is ~6.2 sessions at 30m and
    # is what the existing premium-refresh path already fetches — reuse it so
    # warm-up does not invent a second, different depth.
    target_bars = max(min_bars, 80)
    bars_per_session = BARS_PER_SESSION_30M if interval == "30minute" else max(1, BARS_PER_SESSION_30M)
    return WarmupRequirement(
        interval=interval,
        macd_fast=fast,
        macd_slow=slow,
        macd_signal=signal,
        min_bars=min_bars,
        target_bars=target_bars,
        bars_per_session=bars_per_session,
        min_sessions=min_bars / bars_per_session,
        target_sessions=target_bars / bars_per_session,
        source=source,
    )


def _warmup_band_strikes(strikes: Sequence[float], spot_price: float, band: int) -> list[float]:
    """The strikes to pre-warm: the spot-spanning selection window, widened by
    ``band - 1`` on each side.

    band=1 is exactly the window ``_spot_spanning_window`` ranks over, so every
    strike the selector could ever pick is warm and a mid-session re-pick inside
    the window costs no broker call at all.  band>=2 warms strikes the selector
    can never choose — see the arithmetic in core/config.py.
    """
    from market_data.atm_watchlist import _spot_spanning_window

    ordered = sorted({float(s) for s in strikes or []})
    window = _spot_spanning_window(ordered, float(spot_price))
    if not window:
        return []
    lo = ordered.index(window[0]) - max(int(band) - 1, 0)
    hi = ordered.index(window[-1]) + max(int(band) - 1, 0)
    return [ordered[i] for i in range(max(lo, 0), min(hi, len(ordered) - 1) + 1)]


@dataclass
class WarmupResult:
    underlying: str
    option_type: str
    strike: float
    expiry: date
    bars: int
    status: str          # ready | not_ready
    path: str            # db_only | db_plus_broker | broker_only | insufficient | error
    detail: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "option_type": self.option_type,
            "strike": self.strike,
            "expiry": self.expiry.isoformat(),
            "bars": self.bars,
            "status": self.status,
            "path": self.path,
            "detail": self.detail,
        }


async def warm_up_strike(
    *,
    underlying: str,
    expiry: date,
    strike: float,
    option_type: str,
    instrument_key: Optional[str] = None,
    requirement: Optional[WarmupRequirement] = None,
    bulk: bool = True,
) -> WarmupResult:
    """Fetch enough premium history for MACD on ONE strike.

    Uses the EXISTING gap-aware machinery (``market_data.option_history``'s
    ``load_candles``) rather than a new fetch path, so ``_broker_lookback_days``
    and the broker's own HTTP bounds still apply.  Known constraint stands:
    ``option_premium_candles`` is ~100% REST-fed, so this is real broker load.

    Budget: mid-session warm-ups run under the BULK quota class so they can
    NEVER starve live decision traffic or held-position marks.  Pre-open the
    caller may pass ``bulk=False`` (STANDARD) because nothing competes.

    NEVER fabricates.  A short series is reported as ``not_ready`` with its
    actual bar count and the strike is EXCLUDED from decisions; it is never
    padded, forward-filled, or handed to the indicator.
    """
    from brokers.rate_limiter import (
        CLASS_BULK,
        CLASS_STANDARD,
        PRIORITY_BULK,
        broker_class,
        broker_priority,
    )
    from market_data.option_history import option_history_service

    req = requirement or warmup_requirement()

    def _result(bars: int, path: str, detail: Optional[str] = None) -> WarmupResult:
        status = WARMUP_READY if bars >= req.min_bars else WARMUP_NOT_READY
        if status == WARMUP_NOT_READY:
            logger.warning(
                "[MACD warm-up] {} {} {} {} NOT READY: {} bars < {} required "
                "({} on {}). Strike EXCLUDED from decisions — a short series is never "
                "padded or fed to MACD.",
                underlying,
                expiry.isoformat(),
                f"{strike:g}",
                option_type,
                bars,
                req.min_bars,
                f"MACD({req.macd_fast},{req.macd_slow},{req.macd_signal})",
                req.interval,
            )
        return WarmupResult(
            underlying=underlying,
            option_type=option_type,
            strike=float(strike),
            expiry=expiry,
            bars=bars,
            status=status,
            path=path if status == WARMUP_READY else (path if path == "error" else "insufficient"),
            detail=detail,
        )

    # Bar count already in the DB, before any broker call — this is what makes
    # `db_only` vs `db_plus_broker` reportable.
    try:
        existing = await option_history_service.load_candles(
            underlying=underlying,
            expiry=expiry,
            strike=float(strike),
            option_type=option_type,
            instrument_key=instrument_key,
            interval=req.interval,
            limit=req.target_bars,
            allow_broker_refresh=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[MACD warm-up] DB read failed for {} {} {} {}: {!r}",
            underlying, expiry.isoformat(), f"{strike:g}", option_type, exc,
        )
        return _result(0, "error", repr(exc))

    if len(existing) >= req.min_bars:
        return _result(len(existing), "db_only")

    quota = CLASS_BULK if bulk else CLASS_STANDARD
    try:
        with broker_class(quota), broker_priority(PRIORITY_BULK):
            merged = await option_history_service.load_candles(
                underlying=underlying,
                expiry=expiry,
                strike=float(strike),
                option_type=option_type,
                instrument_key=instrument_key,
                interval=req.interval,
                limit=req.target_bars,
                allow_broker_refresh=True,
            )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[MACD warm-up] broker fetch failed for {} {} {} {}: {!r} — reporting the "
            "DB-only bar count ({}), never a fabricated one.",
            underlying, expiry.isoformat(), f"{strike:g}", option_type, exc, len(existing),
        )
        return _result(len(existing), "error", repr(exc))

    path = "broker_only" if not existing else "db_plus_broker"
    return _result(len(merged), path)


async def warm_up_strikes(
    targets: Sequence[dict[str, Any]],
    *,
    requirement: Optional[WarmupRequirement] = None,
    bulk: bool = True,
    pacing_seconds: Optional[float] = None,
) -> list[WarmupResult]:
    """Sequential, PACED warm-up of many strikes (one broker call each).

    Sequential on purpose: the whole point of the BULK class is that this
    cannot burst.  `pacing_seconds` defaults to MACD_WARMUP_PACING_SECONDS.
    """
    from core.config import settings

    req = requirement or warmup_requirement()
    pace = float(pacing_seconds if pacing_seconds is not None else settings.MACD_WARMUP_PACING_SECONDS)
    out: list[WarmupResult] = []
    for index, target in enumerate(targets):
        if index and pace > 0:
            await asyncio.sleep(pace)
        out.append(
            await warm_up_strike(
                underlying=str(target["underlying"]),
                expiry=target["expiry"],
                strike=float(target["strike"]),
                option_type=str(target["option_type"]),
                instrument_key=target.get("instrument_key"),
                requirement=req,
                bulk=bulk,
            )
        )
    ready = sum(1 for item in out if item.status == WARMUP_READY)
    logger.info(
        "[MACD warm-up] {} strikes attempted, {} ready, {} not ready (min_bars={}, "
        "target={}, class={})",
        len(out), ready, len(out) - ready, req.min_bars, req.target_bars,
        "bulk" if bulk else "standard",
    )
    return out


# ══════════════════════════════════════════════════════════════════════════
# (3b) Liquidity history — HISTORICAL volume, per prior session, MEDIAN
# ══════════════════════════════════════════════════════════════════════════
async def load_prior_volume(
    *,
    underlying: str,
    kind: str,
    expiry: date,
    sessions: int = LIQUIDITY_LOOKBACK_SESSIONS,
    today: Optional[date] = None,
) -> dict[str, dict[float, float]]:
    """Median per-session traded volume per (side, strike) over prior sessions.

    Owner-specified: liquidity is measured from HISTORICAL volume and open
    interest, NOT the live/instantaneous snapshot.  That is structural, not a
    preference — the ladder is built at 09:04, when today's traded volume for
    every contract is ~0, so a live-volume ranker would score every candidate
    at zero and silently collapse back to the arithmetic anchor, i.e. the
    liquidity logic would be inert exactly when it is needed.

    Sources, resolved per symbol class from what is genuinely populated:

      * INDEX  → ``option_chain_snapshots`` (full ladder present; measured
        2026-07-17: five index symbols, 435k rows/day, oi>0 84%, volume>0 59%).
        The column is cumulative-for-day, so per session we take MAX.
      * STOCK  → ``option_premium_candles`` at the 30-minute interval, SUM per
        session.  HONEST LIMITATION: this table only holds the strikes the old
        moving-ATM tracker previously visited (4–5 per underlying per week), so
        one or two of the three window candidates routinely have NO row and
        come back `unmeasurable` — which is an exclusion, not a zero score.

    MEDIAN across sessions, not a sum: a sum lets a single expiry-day spike win
    a strike that is dead on the other four days.

    Returns ``{"CE": {strike: median_volume}, "PE": {...}}``.  A strike absent
    from the map has NO measurable history.
    """
    days = prior_trading_sessions(sessions, today=today)
    if not days:
        return {"CE": {}, "PE": {}}
    start_utc, _ = _session_bounds_utc(days[0])
    _, end_utc = _session_bounds_utc(days[-1])

    is_index = str(kind or "").upper() == "INDEX"
    if is_index:
        # Bound `time` DIRECTLY with literals (chunk exclusion) + symbol +
        # expiry. date_trunc appears only in GROUP BY, never in WHERE.
        query = """
            SELECT option_type,
                   strike,
                   date_trunc('day', time AT TIME ZONE 'Asia/Kolkata') AS session_key,
                   MAX(volume) AS session_volume
              FROM option_chain_snapshots
             WHERE time >= :start_utc
               AND time <  :end_utc
               AND symbol = :symbol
               AND expiry = :expiry
             GROUP BY option_type, strike, session_key
        """
        params = {
            "start_utc": start_utc,
            "end_utc": end_utc,
            "symbol": underlying,
            "expiry": expiry.isoformat(),
        }
    else:
        query = """
            SELECT option_type,
                   strike,
                   date_trunc('day', time AT TIME ZONE 'Asia/Kolkata') AS session_key,
                   SUM(volume) AS session_volume
              FROM option_premium_candles
             WHERE time >= :start_utc
               AND time <  :end_utc
               AND underlying = :symbol
               AND expiry = :expiry
               AND interval = '30minute'
             GROUP BY option_type, strike, session_key
        """
        params = {
            "start_utc": start_utc,
            "end_utc": end_utc,
            "symbol": underlying,
            "expiry": expiry,
        }

    buckets: dict[str, dict[float, list[float]]] = {"CE": {}, "PE": {}}
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(text(query), params)
            rows = result.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[MACD watchlist] prior-volume history query FAILED for {} {} ({} sessions): "
            "{!r}. No fabricated fallback — callers will see every candidate as "
            "unmeasurable and exclude the instrument LOUDLY.",
            underlying, expiry.isoformat(), len(days), exc,
        )
        return {"CE": {}, "PE": {}}

    for row in rows:
        side = str(row.option_type or "").upper()
        if side not in buckets:
            continue
        try:
            strike = float(row.strike)
        except (TypeError, ValueError):
            continue
        volume = float(row.session_volume or 0.0)
        buckets[side].setdefault(strike, []).append(volume)

    out: dict[str, dict[float, float]] = {"CE": {}, "PE": {}}
    for side, per_strike in buckets.items():
        for strike, values in per_strike.items():
            out[side][strike] = float(statistics.median(values)) if values else 0.0
    logger.debug(
        "[MACD watchlist] prior volume {} {} src={} sessions={} CE={} PE={}",
        underlying,
        expiry.isoformat(),
        "option_chain_snapshots" if is_index else "option_premium_candles",
        len(days),
        len(out["CE"]),
        len(out["PE"]),
    )
    return out


# ══════════════════════════════════════════════════════════════════════════
# (2) Pre-open anchor price — measured sources, LABELLED, never mixed silently
# ══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class PriceAnchorResult:
    underlying: str
    price: Optional[float]
    anchor: str
    at: Optional[datetime]

    @property
    def ok(self) -> bool:
        return bool(self.price and self.price > 0)


async def resolve_price_anchor(
    *,
    underlying: str,
    session_date: Optional[date] = None,
    preopen_ltp: Optional[float] = None,
) -> PriceAnchorResult:
    """Anchor price for the pre-open ladder, with the source LABELLED.

    Measured coverage on 2026-07-20 in the 09:00–09:15 IST window:
      indices  12 symbols / 1,476 ticks   → equilibrium is reliable
      equities 67 symbols /   289 ticks   → ~4 ticks per name, 67 of ~211 F&O
                                            names (~32% coverage)
    So the honest statement is: the pre-open equilibrium price exists for the
    indices and roughly a THIRD of the stock universe; the previous close is
    the anchor for the majority of stocks.  That is reported per row via
    ``price_anchor`` — it is a fact, not a hidden degradation.

    Order: pre-open WS tick → broker pre-open LTP (if the caller has one) →
    previous close (``underlying_spot_candles`` 30m; 224 underlyings covered
    today, the guaranteed floor).
    """
    session_date = session_date or _now_ist().date()
    # NSE call auction 09:00–09:08 IST; we read the whole 09:00–09:15 band.
    window_start = datetime.combine(session_date, dt_time(9, 0), tzinfo=IST).astimezone(UTC)
    window_end = datetime.combine(session_date, dt_time(9, 15), tzinfo=IST).astimezone(UTC)

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    SELECT time, ltp
                      FROM market_ticks
                     WHERE time >= :start_utc
                       AND time <  :end_utc
                       AND symbol = :symbol
                       AND ltp IS NOT NULL
                     ORDER BY time DESC
                     LIMIT 1
                    """
                ),
                {"start_utc": window_start, "end_utc": window_end, "symbol": underlying},
            )
            row = result.fetchone()
        if row is not None and row.ltp:
            return PriceAnchorResult(underlying, float(row.ltp), ANCHOR_PREOPEN_WS_TICK, row.time)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[MACD watchlist] pre-open tick lookup failed for {}: {!r}", underlying, exc)

    if preopen_ltp and float(preopen_ltp) > 0:
        return PriceAnchorResult(underlying, float(preopen_ltp), ANCHOR_PREOPEN_EQUILIBRIUM, _now_ist())

    prior = prior_trading_sessions(1, today=session_date)
    if prior:
        start_utc, end_utc = _session_bounds_utc(prior[0])
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text(
                        """
                        SELECT time, close
                          FROM underlying_spot_candles
                         WHERE time >= :start_utc
                           AND time <  :end_utc
                           AND underlying = :symbol
                           AND interval = '30minute'
                           AND close IS NOT NULL
                         ORDER BY time DESC
                         LIMIT 1
                        """
                    ),
                    {"start_utc": start_utc, "end_utc": end_utc, "symbol": underlying},
                )
                row = result.fetchone()
            if row is not None and row.close:
                return PriceAnchorResult(underlying, float(row.close), ANCHOR_PREV_CLOSE, row.time)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MACD watchlist] prev-close lookup failed for {}: {!r}", underlying, exc)

    logger.error(
        "[MACD watchlist] NO usable anchor price for {} on {} (no pre-open tick, no broker "
        "equilibrium LTP, no prior-session 30m close). Instrument EXCLUDED from the frozen "
        "ladder — we do NOT anchor a strike ladder on a guessed price.",
        underlying,
        session_date.isoformat(),
    )
    return PriceAnchorResult(underlying, None, ANCHOR_NONE, None)


# ══════════════════════════════════════════════════════════════════════════
# (3) Sticky strikes — pins read from POSTGRES (+ the refined paper JSON)
# ══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class PositionPin:
    underlying: str
    option_type: str
    strike: float
    expiry: Optional[date]
    position_id: str
    source: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "option_type": self.option_type,
            "strike": self.strike,
            "expiry": self.expiry.isoformat() if self.expiry else None,
            "position_id": self.position_id,
            "source": self.source,
        }


MACD_STRATEGY_KEYS = ("macd_strategy", "strategy1")


async def load_open_position_pins() -> dict[tuple[str, str], PositionPin]:
    """(underlying, option_type) → the strike an OPEN MACD position sits on.

    Read from DURABLE state only, never from memory, so the pins reconstruct
    identically after a mid-session restart:
      * Postgres ``agent_positions`` (status='open') for the S1 book;
      * ``backend/runtime/macd_refined/paper/paper_positions.json`` for the
        refined lane's PAPER book (owner rule: live_engine/risk_manager are
        untouched — paper only).
    """
    pins: dict[tuple[str, str], PositionPin] = {}
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    SELECT id::text AS id, underlying, option_type, strike, expiry
                      FROM agent_positions
                     WHERE status = 'open'
                       AND strategy_key = ANY(:keys)
                       AND option_type IS NOT NULL
                       AND strike IS NOT NULL
                    """
                ),
                {"keys": list(MACD_STRATEGY_KEYS)},
            )
            rows = result.fetchall()
        for row in rows:
            side = str(row.option_type or "").upper()
            underlying = str(row.underlying or "").upper()
            if side not in {"CE", "PE"} or not underlying:
                continue
            pins[(underlying, side)] = PositionPin(
                underlying=underlying,
                option_type=side,
                strike=float(row.strike),
                expiry=row.expiry,
                position_id=str(row.id),
                source="agent_positions",
            )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[MACD watchlist] could not read open positions from agent_positions: {!r}. "
            "Sticky pins are UNKNOWN this cycle — the frozen ladder is left untouched "
            "rather than re-picked, so an open position can never be drifted away from "
            "by a failed read.",
            exc,
        )
        raise

    for pin in _load_refined_paper_pins():
        pins.setdefault((pin.underlying, pin.option_type), pin)
    return pins


class RefinedPaperBookUnreadable(RuntimeError):
    """The macd_refined paper book exists but could not be parsed.

    Raised rather than swallowed: an unreadable book is NOT an empty book, and
    treating it as empty would silently drop every sticky pin the refined lane
    owns and let the ladder drift away from live positions.  FAIL CLOSED.
    """


def _load_refined_paper_pins() -> list[PositionPin]:
    import json
    from pathlib import Path

    try:
        from macd_refined.config import RUNTIME_ROOT

        path = Path(RUNTIME_ROOT) / "paper" / "paper_positions.json"
    except Exception as exc:  # noqa: BLE001
        raise RefinedPaperBookUnreadable(
            f"cannot locate the macd_refined paper book: {exc!r}"
        ) from exc
    if not path.exists():
        # A book that has never been written is genuinely empty — that is a
        # different fact from a book we failed to read, and only this one is safe.
        return []
    try:
        payload = json.loads(path.read_text() or "{}")
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[MACD watchlist] refined paper book at {} is UNREADABLE ({!r}). Sticky pins "
            "are UNKNOWN — failing closed rather than treating the book as empty and "
            "drifting the ladder off live positions.",
            path, exc,
        )
        raise RefinedPaperBookUnreadable(str(path)) from exc

    raw: Iterable[Any]
    if isinstance(payload, dict):
        # "open_positions" is the key macd_refined.paper actually writes; the
        # other two are tolerated shapes. Reading only "positions"/"open" made
        # this function silently return [] for the real book — every refined pin
        # was lost.
        raw = payload.get("open_positions") or payload.get("positions") or payload.get("open") or []
        if isinstance(raw, dict):
            raw = list(raw.values())
    elif isinstance(payload, list):
        raw = payload
    else:
        raw = []

    out: list[PositionPin] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "open").lower()
        if status not in {"open", "active"}:
            continue
        underlying = str(item.get("underlying") or item.get("symbol") or "").upper()
        side = str(item.get("option_type") or item.get("side") or "").upper()
        strike = item.get("strike")
        if not underlying or side not in {"CE", "PE"} or strike in (None, ""):
            continue
        expiry = item.get("expiry")
        parsed_expiry: Optional[date] = None
        if expiry:
            try:
                parsed_expiry = date.fromisoformat(str(expiry)[:10])
            except ValueError:
                parsed_expiry = None
        try:
            out.append(
                PositionPin(
                    underlying=underlying,
                    option_type=side,
                    strike=float(strike),
                    expiry=parsed_expiry,
                    position_id=str(item.get("id") or item.get("position_id") or f"{underlying}:{side}"),
                    source="macd_refined_paper",
                )
            )
        except (TypeError, ValueError):
            continue
    return out


# ══════════════════════════════════════════════════════════════════════════
# Sidecar persistence
# ══════════════════════════════════════════════════════════════════════════
_UPSERT_SQL = f"""
INSERT INTO {SESSION_TABLE} (
    session_date, underlying, option_type, kind, expiry, strike, instrument_key,
    trading_symbol, price_anchor, anchor_price, anchor_at, strike_status,
    liquidity_oi, liquidity_prior_volume, spread_rel, warmup_bars, warmup_path,
    warmup_status, pinned_position_id, frozen_at, repicked_at, repick_seq,
    expiry_anchor, expiry_rolled, notes, updated_at
) VALUES (
    :session_date, :underlying, :option_type, :kind, :expiry, :strike, :instrument_key,
    :trading_symbol, :price_anchor, :anchor_price, :anchor_at, :strike_status,
    :liquidity_oi, :liquidity_prior_volume, :spread_rel, :warmup_bars, :warmup_path,
    :warmup_status, :pinned_position_id, :frozen_at, :repicked_at, :repick_seq,
    :expiry_anchor, :expiry_rolled, :notes, now()
)
ON CONFLICT (session_date, underlying, option_type) DO UPDATE SET
    kind = EXCLUDED.kind,
    expiry = EXCLUDED.expiry,
    strike = EXCLUDED.strike,
    instrument_key = EXCLUDED.instrument_key,
    trading_symbol = EXCLUDED.trading_symbol,
    price_anchor = EXCLUDED.price_anchor,
    anchor_price = EXCLUDED.anchor_price,
    anchor_at = EXCLUDED.anchor_at,
    strike_status = EXCLUDED.strike_status,
    liquidity_oi = EXCLUDED.liquidity_oi,
    liquidity_prior_volume = EXCLUDED.liquidity_prior_volume,
    spread_rel = EXCLUDED.spread_rel,
    warmup_bars = EXCLUDED.warmup_bars,
    warmup_path = EXCLUDED.warmup_path,
    warmup_status = EXCLUDED.warmup_status,
    pinned_position_id = EXCLUDED.pinned_position_id,
    frozen_at = COALESCE({SESSION_TABLE}.frozen_at, EXCLUDED.frozen_at),
    repicked_at = EXCLUDED.repicked_at,
    repick_seq = EXCLUDED.repick_seq,
    expiry_anchor = EXCLUDED.expiry_anchor,
    expiry_rolled = EXCLUDED.expiry_rolled,
    notes = EXCLUDED.notes,
    updated_at = now()
"""


def _row_defaults(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "session_date": None,
        "underlying": None,
        "option_type": None,
        "kind": "STOCK",
        "expiry": None,
        "strike": None,
        "instrument_key": None,
        "trading_symbol": None,
        "price_anchor": None,
        "anchor_price": None,
        "anchor_at": None,
        "strike_status": STATUS_OK,
        "liquidity_oi": None,
        "liquidity_prior_volume": None,
        "spread_rel": None,
        "warmup_bars": 0,
        "warmup_path": None,
        "warmup_status": WARMUP_NOT_READY,
        "pinned_position_id": None,
        "frozen_at": None,
        "repicked_at": None,
        "repick_seq": 0,
        "expiry_anchor": None,
        "expiry_rolled": False,
        "notes": None,
    }
    base.update(overrides)
    return base


async def persist_rows(rows: Sequence[dict[str, Any]]) -> int:
    if not rows:
        return 0
    try:
        async with AsyncSessionLocal() as session:
            for row in rows:
                await session.execute(text(_UPSERT_SQL), row)
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("[MACD watchlist] persist FAILED for {} rows: {!r}", len(rows), exc)
        raise
    return len(rows)


async def load_session_watchlist(session_date: Optional[date] = None) -> dict[tuple[str, str], dict[str, Any]]:
    """The frozen ladder for a session, keyed by (underlying, option_type).

    This is the restart-safety path: after a mid-session restart the ladder is
    read back from Postgres (durable) and the pins are re-derived from
    ``agent_positions`` (also durable), so nothing depends on in-process state.
    """
    session_date = session_date or _now_ist().date()
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(f"SELECT * FROM {SESSION_TABLE} WHERE session_date = :session_date"),
                {"session_date": session_date},
            )
            rows = result.mappings().all()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[MACD watchlist] could not load the frozen ladder for {}: {!r}",
            session_date.isoformat(), exc,
        )
        return {}
    return {
        (str(row["underlying"]).upper(), str(row["option_type"]).upper()): dict(row)
        for row in rows
    }


# ══════════════════════════════════════════════════════════════════════════
# (2) The pre-open build
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class PreopenBuildReport:
    session_date: date
    built: int = 0
    excluded_no_liquid: int = 0
    excluded_no_anchor: int = 0
    pinned: int = 0
    not_ready: int = 0
    anchors: dict[str, int] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_date": self.session_date.isoformat(),
            "built": self.built,
            "excluded_no_liquid": self.excluded_no_liquid,
            "excluded_no_anchor": self.excluded_no_anchor,
            "pinned": self.pinned,
            "not_ready": self.not_ready,
            "anchors": dict(self.anchors),
        }


def preopen_window_now(now: Optional[datetime] = None) -> bool:
    """Whether we are inside the configured pre-open sample window (IST).

    Default 09:04–09:14: NSE's call auction runs 09:00–09:08, so a 09:00 sample
    mostly returns a carried previous close.
    """
    from core.config import settings

    current = (now or _now_ist()).astimezone(IST).time()

    def _parse(raw: str, fallback: dt_time) -> dt_time:
        try:
            hour, minute = str(raw).split(":")
            return dt_time(int(hour), int(minute))
        except Exception:  # noqa: BLE001
            logger.warning("[MACD watchlist] bad pre-open window value {!r}; using {}", raw, fallback)
            return fallback

    start = _parse(settings.MACD_PREOPEN_WINDOW_START, dt_time(9, 4))
    end = _parse(settings.MACD_PREOPEN_WINDOW_END, dt_time(9, 14))
    return start <= current <= end


async def build_preopen_watchlist(
    *,
    universe: Sequence[tuple[str, str]],
    chain_loader: Callable[..., Any],
    session_date: Optional[date] = None,
    preopen_ltp: Optional[dict[str, float]] = None,
    warm_up: Optional[bool] = None,
) -> PreopenBuildReport:
    """Build the session ladder ONCE and FREEZE it.

    `universe` is a sequence of (symbol, kind) pairs.
    `chain_loader` is an awaitable ``loader(symbol, kind, expiry) -> OptionChain``
    supplied by the caller (``ATMWatchlistService``) so this module stays free of
    the broker import graph and is trivially testable.

    Sequence per instrument:
      1. expiry   ← core.expiry_policy (calendar; validated once pre-market)
      2. anchor   ← pre-open tick → broker equilibrium LTP → prev close (LABELLED)
      3. sticky   ← an open position PINS its strike/expiry; selection is SKIPPED
      4. strike   ← unbiased liquidity pick over the spot-spanning window,
                    ranked on MEDIAN prior-session volume + carried OI
      5. warm-up  ← history fetched so MACD can actually be computed; a short
                    series is `not_ready` and EXCLUDED, never padded
    """
    from core.config import settings
    from core.expiry_policy import expiry_policy, forced_close_check
    from market_data.atm_watchlist import resolve_row_strikes

    session_date = session_date or _now_ist().date()
    report = PreopenBuildReport(session_date=session_date)
    requirement = warmup_requirement()
    do_warm = settings.MACD_WARMUP_ENABLED if warm_up is None else bool(warm_up)
    pins = await load_open_position_pins() if settings.MACD_STICKY_STRIKES_ENABLED else {}
    now = _now_ist()

    logger.info(
        "[MACD watchlist] PRE-OPEN BUILD {} — {} instruments, warm_up={}, sticky_pins={}, "
        "warmup_requirement={}",
        session_date.isoformat(), len(universe), do_warm, len(pins), requirement.as_dict(),
    )

    rows: list[dict[str, Any]] = []
    for symbol, kind in universe:
        symbol = str(symbol or "").upper().strip()
        kind = "INDEX" if str(kind or "").upper() == "INDEX" else "STOCK"
        if not symbol:
            continue

        decision = expiry_policy.resolve(symbol, kind, today=session_date)
        anchor = await resolve_price_anchor(
            underlying=symbol,
            session_date=session_date,
            preopen_ltp=(preopen_ltp or {}).get(symbol),
        )
        report.anchors[anchor.anchor] = report.anchors.get(anchor.anchor, 0) + 1
        if not anchor.ok:
            report.excluded_no_anchor += 2
            continue

        chain = None
        try:
            chain = await chain_loader(symbol, kind, decision.current_expiry)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[MACD watchlist] chain load FAILED for {} {} at pre-open: {!r} — instrument "
                "EXCLUDED (no stale ladder is reused).",
                symbol, decision.current_expiry.isoformat(), exc,
            )
        entries = list(getattr(chain, "entries", None) or [])
        if not entries:
            report.excluded_no_liquid += 2
            continue

        strikes = sorted({float(entry.strike) for entry in entries})
        prior_volume = await load_prior_volume(
            underlying=symbol,
            kind=kind,
            expiry=decision.current_expiry,
            today=session_date,
        )
        picks, meta = resolve_row_strikes(
            symbol=symbol,
            kind=kind,
            strikes=strikes,
            spot_price=float(anchor.price or 0.0),
            chain_entries=entries,
            prior_volume=prior_volume,
        )
        candidates = {
            side: {item["strike"]: item for item in (meta.get("candidates") or {}).get(side, [])}
            for side in ("CE", "PE")
        }

        # ── BAND pre-warm (MACD_WARMUP_BAND_STRIKES) ──────────────────────────
        # Warm every strike the selector could ever choose, not just the one it
        # did, so a mid-session re-pick after a position closes costs ZERO
        # broker calls and ZERO latency for any drift inside the window.
        # band=1 → exactly the 3-strike spanning window (3 x 2 x ~216 = 1,296
        # fetches, ~7.6 min at 0.35s pacing — fits the pre-09:00 dead zone).
        # band=2 → 5 strikes, 864 of which the selector can never pick.
        warm_by_key: dict[tuple[str, float], WarmupResult] = {}
        if do_warm:
            band_strikes = _warmup_band_strikes(
                strikes, float(anchor.price or 0.0), int(settings.MACD_WARMUP_BAND_STRIKES)
            )
            by_contract = {
                (str(getattr(item, "option_type", "")).upper(), float(item.strike)): item
                for item in entries
            }
            targets = [
                {
                    "underlying": symbol,
                    "expiry": decision.current_expiry,
                    "strike": band_strike,
                    "option_type": band_side,
                    "instrument_key": getattr(
                        by_contract.get((band_side, band_strike)), "instrument_key", None
                    ),
                }
                for band_side in ("CE", "PE")
                for band_strike in band_strikes
                if (band_side, band_strike) in by_contract
            ]
            for result in await warm_up_strikes(
                targets, requirement=requirement, bulk=False  # pre-open: nothing competes
            ):
                warm_by_key[(result.option_type, float(result.strike))] = result

        for side in ("CE", "PE"):
            pin = pins.get((symbol, side))
            if pin is not None:
                # STICKY DOMINATES, and it now SURVIVES THE ROLL.
                #
                # The window is not even computed: no liquidity move can drift a
                # strike that carries an open position. The pin also holds the
                # EXPIRY — and past the 5TD stock roll that expiry is no longer
                # the one the rest of the universe points at, which is exactly
                # the owner's split ("except for held positions other
                # instruments to rollover to next expiry"). Resolving the row
                # through `held_expiry` makes that explicit instead of implicit:
                # the row records rolled=False with the HELD reason, so a pinned
                # row on the old month can never be mistaken for a stale row the
                # roll forgot. The un-held side of the SAME underlying still
                # rolls (it uses `decision` below), which is the rule read
                # literally: held-ness is per contract, not per symbol.
                held_decision = expiry_policy.resolve(
                    symbol, kind, today=session_date, held_expiry=pin.expiry
                )
                pin_expiry = pin.expiry or decision.current_expiry
                # Through the SHARED gate, not must_force_close directly: the
                # note below claims "the exit cascade force-closes it today",
                # and that is only true when the exit cascade's own flags are
                # up. Calling the raw computation here annotated rows with a
                # closure that would never happen while
                # EXPIRY_POLICY_FORCED_CLOSE_ENABLED was down.
                hold = forced_close_check(
                    symbol, pin_expiry, kind=kind, today=session_date
                )
                note = f"sticky:{pin.source}"
                if held_decision.roll_reason:
                    note = f"{note};{held_decision.roll_reason}"
                if hold is not None and hold.must_close:
                    # The pin is NOT released here — releasing it while the
                    # position is still open is precisely how a row gets
                    # orphaned. It is released by `repick_after_close` once the
                    # exit cascade has actually closed the position.
                    note = f"{note};{hold.reason}_due"
                    logger.warning(
                        "[MACD watchlist] {} {} is PINNED to {} which is {} trading day(s) "
                        "from expiry (boundary {}) — the exit cascade force-closes it today; "
                        "the pin holds until it actually closes.",
                        symbol, side, pin_expiry.isoformat(),
                        hold.trading_days_to_expiry, hold.boundary_trading_days,
                    )
                rows.append(
                    _row_defaults(
                        session_date=session_date,
                        underlying=symbol,
                        option_type=side,
                        kind=kind,
                        expiry=pin_expiry,
                        strike=pin.strike,
                        price_anchor=anchor.anchor,
                        anchor_price=anchor.price,
                        anchor_at=anchor.at,
                        strike_status=STATUS_OK,
                        pinned_position_id=pin.position_id,
                        frozen_at=now,
                        expiry_anchor=held_decision.anchor.value,
                        expiry_rolled=held_decision.rolled,
                        warmup_status=WARMUP_READY,
                        warmup_path="pinned_open_position",
                        warmup_bars=requirement.min_bars,
                        notes=note,
                    )
                )
                report.pinned += 1
                report.built += 1
                continue

            strike = picks.get(side)
            if strike is None:
                rows.append(
                    _row_defaults(
                        session_date=session_date,
                        underlying=symbol,
                        option_type=side,
                        kind=kind,
                        expiry=decision.current_expiry,
                        price_anchor=anchor.anchor,
                        anchor_price=anchor.price,
                        anchor_at=anchor.at,
                        strike_status=STATUS_NO_LIQUID,
                        frozen_at=now,
                        expiry_anchor=decision.anchor.value,
                        expiry_rolled=decision.rolled,
                        warmup_status=WARMUP_NOT_READY,
                        notes="no liquid contract in the spot-spanning window",
                    )
                )
                report.excluded_no_liquid += 1
                continue

            diag = candidates.get(side, {}).get(strike, {})
            entry = next(
                (
                    item
                    for item in entries
                    if str(getattr(item, "option_type", "")).upper() == side
                    and float(item.strike) == float(strike)
                ),
                None,
            )
            warm_bars, warm_status, warm_path = 0, WARMUP_NOT_READY, None
            if do_warm:
                result = warm_by_key.get((side, float(strike)))
                if result is None:
                    # Outside the pre-warmed band (only possible if the band was
                    # narrowed below the selection window) — warm it directly.
                    result = await warm_up_strike(
                        underlying=symbol,
                        expiry=decision.current_expiry,
                        strike=float(strike),
                        option_type=side,
                        instrument_key=getattr(entry, "instrument_key", None),
                        requirement=requirement,
                        bulk=False,  # pre-open: nothing competes for the budget
                    )
                warm_bars, warm_status, warm_path = result.bars, result.status, result.path
            if warm_status != WARMUP_READY:
                report.not_ready += 1
            rows.append(
                _row_defaults(
                    session_date=session_date,
                    underlying=symbol,
                    option_type=side,
                    kind=kind,
                    expiry=decision.current_expiry,
                    strike=float(strike),
                    instrument_key=getattr(entry, "instrument_key", None),
                    trading_symbol=getattr(entry, "trading_symbol", None),
                    price_anchor=anchor.anchor,
                    anchor_price=anchor.price,
                    anchor_at=anchor.at,
                    strike_status=STATUS_OK if warm_status == WARMUP_READY or not do_warm else STATUS_NOT_READY,
                    liquidity_oi=diag.get("oi"),
                    liquidity_prior_volume=diag.get("flow"),
                    spread_rel=diag.get("spread_rel"),
                    warmup_bars=warm_bars,
                    warmup_path=warm_path,
                    warmup_status=warm_status,
                    frozen_at=now,
                    expiry_anchor=decision.anchor.value,
                    expiry_rolled=decision.rolled,
                    notes=meta.get("mode"),
                )
            )
            report.built += 1

    await persist_rows(rows)
    report.rows = rows
    logger.info("[MACD watchlist] PRE-OPEN BUILD complete: {}", report.as_dict())
    return report


# ══════════════════════════════════════════════════════════════════════════
# (3) Mid-session re-pick after a position closes
# ══════════════════════════════════════════════════════════════════════════
async def repick_after_close(
    *,
    underlying: str,
    option_type: str,
    kind: str,
    spot_price: float,
    chain_entries,
    expiry: Optional[date] = None,
    session_date: Optional[date] = None,
    warm_up: Optional[bool] = None,
) -> Optional[dict[str, Any]]:
    """Choose the next strike for an instrument whose position just CLOSED.

    Owner: "new strike for that instrument fetched after closure based on spot
    price at that time" — so the anchor here is the LIVE spot at the moment of
    closure, not the pre-open anchor.

    THE PIN RELEASE. While the position was open the row was pinned to the
    position's own (possibly pre-roll) expiry. Now that it has closed — whether
    by a normal exit or by the compulsory `forced_expiry_roll_2td` closure — the
    instrument re-joins the normal universe, so `expiry=None` resolves the
    CURRENT policy expiry, which past the 5TD roll is the NEXT month. The written
    row carries `pinned_position_id=None`, which is the release itself: the row
    is overwritten in place (same PK), never orphaned or duplicated.

    The replacement is warmed under the BULK quota class so it can never starve
    live decision traffic; if the pre-open band warm-up already covered the
    window, this costs ZERO broker calls.  A replacement that is not warm is
    written `not_ready` and excluded until it is — it does NOT trade on a short
    series.
    """
    from core.config import settings
    from core.expiry_policy import expiry_policy
    from market_data.atm_watchlist import resolve_row_strikes

    session_date = session_date or _now_ist().date()
    side = str(option_type or "").upper()
    underlying = str(underlying or "").upper()
    if expiry is None:
        expiry = expiry_policy.resolve(underlying, kind, today=session_date).current_expiry
        logger.info(
            "[MACD watchlist] pin RELEASED for {} {} — re-picking on the policy expiry {}",
            underlying, side, expiry.isoformat(),
        )
    entries = list(chain_entries or [])
    if side not in {"CE", "PE"} or not entries or spot_price <= 0:
        logger.error(
            "[MACD watchlist] cannot re-pick {} {}: side/chain/spot unusable "
            "(entries={}, spot={}).", underlying, side, len(entries), spot_price,
        )
        return None

    existing = (await load_session_watchlist(session_date)).get((underlying, side)) or {}
    seq = int(existing.get("repick_seq") or 0) + 1

    strikes = sorted({float(entry.strike) for entry in entries})
    prior_volume = await load_prior_volume(
        underlying=underlying, kind=kind, expiry=expiry, today=session_date
    )
    picks, meta = resolve_row_strikes(
        symbol=underlying,
        kind=kind,
        strikes=strikes,
        spot_price=float(spot_price),
        chain_entries=entries,
        prior_volume=prior_volume,
    )
    strike = picks.get(side)
    now = _now_ist()
    if strike is None:
        row = _row_defaults(
            session_date=session_date,
            underlying=underlying,
            option_type=side,
            kind=kind,
            expiry=expiry,
            price_anchor="live_spot_at_close",
            anchor_price=float(spot_price),
            anchor_at=now,
            strike_status=STATUS_NO_LIQUID,
            repicked_at=now,
            repick_seq=seq,
            frozen_at=existing.get("frozen_at") or now,
            notes="re-pick after close found no liquid contract",
        )
        await persist_rows([row])
        return row

    diag = {
        item["strike"]: item
        for item in (meta.get("candidates") or {}).get(side, [])
    }.get(strike, {})
    entry = next(
        (
            item
            for item in entries
            if str(getattr(item, "option_type", "")).upper() == side
            and float(item.strike) == float(strike)
        ),
        None,
    )

    do_warm = settings.MACD_WARMUP_ENABLED if warm_up is None else bool(warm_up)
    warm_bars, warm_status, warm_path = 0, WARMUP_NOT_READY, None
    if do_warm:
        result = await warm_up_strike(
            underlying=underlying,
            expiry=expiry,
            strike=float(strike),
            option_type=side,
            instrument_key=getattr(entry, "instrument_key", None),
            bulk=True,  # mid-session: BULK so live decisions/marks are never starved
        )
        warm_bars, warm_status, warm_path = result.bars, result.status, result.path

    row = _row_defaults(
        session_date=session_date,
        underlying=underlying,
        option_type=side,
        kind=kind,
        expiry=expiry,
        strike=float(strike),
        instrument_key=getattr(entry, "instrument_key", None),
        trading_symbol=getattr(entry, "trading_symbol", None),
        price_anchor="live_spot_at_close",
        anchor_price=float(spot_price),
        anchor_at=now,
        strike_status=STATUS_OK if (warm_status == WARMUP_READY or not do_warm) else STATUS_NOT_READY,
        liquidity_oi=diag.get("oi"),
        liquidity_prior_volume=diag.get("flow"),
        spread_rel=diag.get("spread_rel"),
        warmup_bars=warm_bars,
        warmup_path=warm_path,
        warmup_status=warm_status,
        pinned_position_id=None,
        frozen_at=existing.get("frozen_at") or now,
        repicked_at=now,
        repick_seq=seq,
        notes=meta.get("mode"),
    )
    await persist_rows([row])
    logger.info(
        "[MACD watchlist] RE-PICK {} {} after close: strike={} (spot={:.2f}, seq={}, "
        "warmup={} bars={})",
        underlying, side, f"{strike:g}", spot_price, seq, warm_status, warm_bars,
    )
    return row


# ══════════════════════════════════════════════════════════════════════════
# (1) Expiry validation wiring — the broker becomes a VALIDATOR, not hot path
# ══════════════════════════════════════════════════════════════════════════
def broker_expiry_probe(service: Any) -> Callable[[str, str], Any]:
    """Adapt ``ATMWatchlistService`` into the awaitable probe expiry_policy wants.

    Keeps core/expiry_policy.py free of the broker import graph.
    """

    async def _probe(symbol: str, kind: str) -> Optional[list[date]]:
        snapshot = await service._get_broker_expiry_snapshot_for_symbol(symbol, kind)
        out: list[date] = []
        for raw in (snapshot or {}).get("expiries", []) if isinstance(snapshot, dict) else (snapshot or []):
            try:
                out.append(date.fromisoformat(str(raw)[:10]))
            except (TypeError, ValueError):
                continue
        return out or None

    return _probe


async def validate_expiries_for_session(
    *,
    service: Any,
    universe: Sequence[tuple[str, str]],
    today: Optional[date] = None,
) -> dict[str, Any]:
    """Run the ONCE-per-session calendar-vs-exchange check.

    A mismatch is LOUD (ERROR naming both values + a durable runtime marker) and
    the exchange wins.  A broker outage is a WARNING and we proceed on the
    calendar — that is the entire point of inverting the dependency, and it is
    what removes the per-cycle 9-symbol probe that produced 405 swallowed
    TimeoutErrors on 2026-07-20.
    """
    from core.config import settings
    from core.expiry_policy import expiry_policy

    if not settings.EXPIRY_POLICY_ENABLED:
        logger.info("[MACD watchlist] EXPIRY_POLICY_ENABLED is off — skipping validation.")
        return {"skipped": "EXPIRY_POLICY_ENABLED=False"}
    if not settings.EXPIRY_POLICY_VALIDATE_ON_OPEN:
        logger.info("[MACD watchlist] EXPIRY_POLICY_VALIDATE_ON_OPEN is off — skipping validation.")
        return {"skipped": "EXPIRY_POLICY_VALIDATE_ON_OPEN=False"}

    report = await expiry_policy.validate_against_exchange(
        probe=broker_expiry_probe(service), symbols=universe, today=today
    )
    return report.as_dict()


# ══════════════════════════════════════════════════════════════════════════
# Scheduler entrypoint — TWO phases, deliberately separated
# ══════════════════════════════════════════════════════════════════════════
async def run_preopen_phase(now: Optional[datetime] = None) -> dict[str, Any]:
    """The supervisor-facing entrypoint. Idempotent; a no-op outside its windows.

    PHASE 1 (from the pre-open prep band up to 09:00) — expiry validation.
      Nothing competes for the broker budget in this dead zone, so the
      once-per-session exchange check runs here rather than in the hot path.

    PHASE 2 (MACD_PREOPEN_WINDOW_START..END, default 09:04-09:14) — anchor
      sample, strike pick, freeze.  09:04 and not 09:00 because NSE's call
      auction runs 09:00-09:08; a 09:00 sample mostly returns a carried
      previous close.

    Warm-up is driven from phase 1 (see the band note in core/config.py): the
    ~1.3k candle fetches fit the 08:00-09:00 dead zone but NOT the ten-minute
    pre-open window, which is why the two phases are separate at all.
    """
    from core.config import settings
    from core.trading_calendar import trading_calendar
    from market_data.atm_watchlist import atm_watchlist_service

    now = (now or _now_ist()).astimezone(IST)
    session_date = now.date()
    if not trading_calendar.has_exchange_session("NSE", session_date):
        return {"skipped": "not an NSE session day"}
    if not settings.MACD_PREOPEN_WATCHLIST_ENABLED:
        return {"skipped": "MACD_PREOPEN_WATCHLIST_ENABLED=False"}

    metas = await atm_watchlist_service._load_underlyings()
    universe = [(str(meta.symbol).upper(), str(meta.kind).upper()) for meta in metas]
    by_symbol = {str(meta.symbol).upper(): meta for meta in metas}
    out: dict[str, Any] = {"session_date": session_date.isoformat(), "universe": len(universe)}

    if not preopen_window_now(now):
        out["phase"] = "validate"
        out["expiry_validation"] = await validate_expiries_for_session(
            service=atm_watchlist_service, universe=universe, today=session_date
        )
        return out

    existing = await load_session_watchlist(session_date)
    if existing:
        # FROZEN means frozen. A second pass in the window must not re-race the
        # ladder — that is the whole defect class we are removing.
        out["phase"] = "already_frozen"
        out["rows"] = len(existing)
        return out

    async def _chain_loader(symbol: str, kind: str, expiry: date):
        meta = by_symbol.get(symbol)
        if meta is None:
            return None
        from api.routers.auth import ensure_fyers_session, get_active_adapter

        fyers = get_active_adapter("fyers")
        if fyers is None and await ensure_fyers_session(force_validate=True):
            fyers = get_active_adapter("fyers")
        upstox = await atm_watchlist_service._get_upstox_adapter()
        # Same preference the row builder uses: Fyers for indices, Upstox for
        # single stocks (see _build_row's prefer_fyers).
        adapter = fyers if (kind == "INDEX" and fyers is not None) else (upstox or fyers)
        if adapter is None:
            return None
        key = (
            atm_watchlist_service._to_fyers_symbol(meta)
            if adapter is fyers
            else meta.underlying_key
        )
        return await adapter.get_option_chain(key, expiry.isoformat())

    out["phase"] = "build"
    report = await build_preopen_watchlist(
        universe=universe, chain_loader=_chain_loader, session_date=session_date
    )
    out["build"] = report.as_dict()
    return out
