"""S2 MP+OF — port of the commodity Market-Profile + Order-Flow engine to
the NSE index-options lane.

The evaluator itself (:func:`commodity_mp_signal.evaluate_commodity_mp_signal`)
is symbol-agnostic — it consumes 1-minute OHLCV bars plus a today/prior
profile and emits a BUY/SELL signal with `entry_style`, `confidence`,
`stop_hint`, and the standard `mp_*` fields. This module is a thin
adapter that adds the bits S2 needs:

* **Universe + expiry routing** — NIFTY and SENSEX trade BOTH weekly and
  monthly ATM options on a signal; BANKNIFTY / FINNIFTY / MIDCPNIFTY
  trade monthly only (NSE discontinued their weeklies in late 2024).
* **Direction → option side** — a BUY signal opens long ATM CE, a SELL
  opens long ATM PE (long-premium only; no shorting).
* **Index spot loader** — reads from ``underlying_spot_candles``, the
  same table the commodity desk uses for futures bars, so the data path
  is identical to the validated commodity setup.
* **Prior-session profile loader + persistence** — mirrors the commodity
  persistence under ``runtime/commodity_profiles/<ROOT>/<DATE>.json``,
  so the historical timeline (Yesterday / Week / Month) reuses the same
  store. The commodity desk and S2 are intentionally writing to the
  same store: an index profile and a futures profile coexist by root.

When the feature flag :data:`settings.NSE_S2_USE_MP_OF_ENGINE` is on,
:meth:`PaperStrategyAgent._build_strategy2_signal_context` calls
:func:`evaluate_strategy2_mp_of` before the legacy MACD path, and falls
through to MACD when the MP+OF result has no signal (e.g. when there
isn't enough 1-minute history yet or the prior session is missing).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from loguru import logger
from sqlalchemy import text

from core.db import AsyncSessionLocal


# ─── Universe / routing ───────────────────────────────────────────────────

# Each underlying maps to the set of expiry tracks we trade when a signal
# fires. NIFTY and SENSEX have active weekly contracts; the others do not
# (BANKNIFTY/FINNIFTY/MIDCPNIFTY weeklies were discontinued by NSE in late
# 2024, leaving monthly as the only listed expiry).
S2_EXPIRY_ROUTING: dict[str, tuple[str, ...]] = {
    "NIFTY":      ("weekly", "monthly"),
    "SENSEX":     ("weekly", "monthly"),
    "BANKNIFTY":  ("monthly",),
    "FINNIFTY":   ("monthly",),
    "MIDCPNIFTY": ("monthly",),
}

# Tick-size hint per index. Used by MarketProfileEngine when binning 1-min
# bars into TPO rows. Values match what NSE / BSE publish for the spot index
# tick. The MP engine clamps to its own minimum so a wrong value here just
# coarsens the profile slightly, never breaks it.
S2_TICK_SIZE: dict[str, float] = {
    "NIFTY":      0.05,
    "BANKNIFTY":  0.05,
    "FINNIFTY":   0.05,
    "MIDCPNIFTY": 0.05,
    "SENSEX":     0.10,
}

# Minimum periods (30-min auction windows) before the MP engine is allowed
# to emit a signal. Matches the commodity threshold so behaviour is the
# same across desks — wait for IB to print, then evaluate.
S2_MP_MIN_PERIODS = 2

# Look-back when loading 1-min spot. Two trading days is enough to cover
# the prior session (used for prior_profile) plus today's developing one.
S2_SPOT_LOOKBACK_DAYS = 5


# ─── Spot loader ──────────────────────────────────────────────────────────


async def load_index_1m_spot(
    underlying: str,
    *,
    lookback_days: int = S2_SPOT_LOOKBACK_DAYS,
) -> list[dict[str, Any]]:
    """Pull 1-minute spot candles for an NSE index.

    Reads from ``underlying_spot_candles`` — the same hypertable the
    commodity desk uses for its 1-min futures bars. Returns a list of
    plain dicts shaped like ``{"time": datetime, "open": float, "high":
    float, "low": float, "close": float, "volume": int}``, sorted
    ascending by time so downstream MP/CVD code can scan once.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT time, open, high, low, close, COALESCE(volume, 0) AS volume
                FROM underlying_spot_candles
                WHERE underlying = :underlying
                  AND interval = '1minute'
                  AND time >= NOW() - (:lookback_days * INTERVAL '1 day')
                ORDER BY time
                """
            ),
            {"underlying": underlying, "lookback_days": int(lookback_days)},
        )
        rows = result.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            out.append(
                {
                    "time": row.time,
                    "open": float(row.open),
                    "high": float(row.high),
                    "low": float(row.low),
                    "close": float(row.close),
                    "volume": int(row.volume or 0),
                }
            )
        except (TypeError, ValueError):
            continue
    return out


# ─── Evaluator (BUY/SELL → CE/PE + expiry tracks) ─────────────────────────


def map_signal_to_option_side(signal: Optional[str]) -> Optional[str]:
    """BUY → CE (long calls), SELL → PE (long puts); everything else → None.

    S2 is long-premium only; there's no short-CE / short-PE leg.
    """
    if signal == "BUY":
        return "CE"
    if signal == "SELL":
        return "PE"
    return None


def expiry_tracks_for(underlying: str) -> tuple[str, ...]:
    """Return the expiry types to trade for an underlying.

    Default fallback is ``("monthly",)`` so an unknown symbol still trades
    the safer (deeper) contract instead of silently dropping the signal.
    """
    return S2_EXPIRY_ROUTING.get(str(underlying).upper(), ("monthly",))


def shape_result_for_s2(
    result: dict[str, Any],
    *,
    underlying: str,
) -> dict[str, Any]:
    """Tag a commodity-MP result with S2-specific routing fields.

    Adds:
      * ``side``: "CE" / "PE" / None
      * ``expiry_tracks``: tuple of "weekly" / "monthly"
      * ``underlying``: copied through so the agent doesn't have to re-thread it
    """
    enriched = dict(result or {})
    enriched["side"] = map_signal_to_option_side(enriched.get("signal"))
    enriched["expiry_tracks"] = expiry_tracks_for(underlying)
    enriched["underlying"] = underlying
    return enriched


# ─── MP engine + prior profile (self-contained) ───────────────────────────


def build_index_market_profile(underlying: str, rows: list[dict[str, Any]]):
    """Build a MarketProfileSnapshot for an NSE index from 1-min bars.

    Mirrors :py:meth:`CommodityStrategyAgent._build_market_profile` but
    sized for an index instead of a futures contract — tick size comes
    from :data:`S2_TICK_SIZE`, IB length is 4 periods (= 1 hour at 15m).
    """
    if not rows or len(rows) < 2:
        return None
    # Late imports — these pull in heavyweight engines we don't want to
    # load until the flag is on.
    from analytics.market_profile import MarketProfileEngine, MarketBar
    from paper_engine.commodity_strategy_agent import _parse_iso_timestamp

    bars: list = []
    for row in rows:
        parsed = _parse_iso_timestamp(row.get("time"))
        close = row.get("close")
        if parsed is None or close is None:
            continue
        bars.append(
            MarketBar(
                timestamp=parsed,
                open=float(row.get("open", close) or close),
                high=float(row.get("high", close) or close),
                low=float(row.get("low", close) or close),
                close=float(close),
                volume=float(row.get("volume") or 0.0),
            )
        )
    if len(bars) < 2:
        return None
    engine = MarketProfileEngine(
        {
            "period_minutes": 15,
            "tick_size": S2_TICK_SIZE.get(underlying.upper(), 0.05),
            "initial_balance_periods": 4,
            "value_area_pct": 0.70,
            "min_tail_tpos": 2,
        }
    )
    try:
        return engine.build_profile(symbol=underlying, bars=bars)
    except Exception as exc:
        logger.debug(f"[s2_mp_of] MP build failed for {underlying}: {exc}")
        return None


# In-process cache for prior-session profiles: (underlying, today) -> snapshot.
# Cleared automatically when the calendar date rolls.
_S2_PRIOR_PROFILE_CACHE: dict[tuple[str, date], Any] = {}


async def load_strategy2_prior_session_profile(
    underlying: str,
    *,
    today: Optional[date] = None,
):
    """Build a snapshot of the prior trading session's MP for an index.

    Cached in-process for the rest of the calendar day. Used by the
    ``open_drive`` and ``va_migration`` triggers in the MP+OF evaluator;
    returns None when only one session of 1-min history is present.
    """
    from paper_engine.commodity_strategy_agent import (
        _filter_closed_interval_rows,
        _latest_session_rows,
        _parse_iso_timestamp,
        IST,
    )

    if today is None:
        from paper_engine.commodity_strategy_agent import _now_ist
        today = _now_ist().date()
    key = (underlying.upper(), today)
    if key in _S2_PRIOR_PROFILE_CACHE:
        return _S2_PRIOR_PROFILE_CACHE[key]

    prior_profile = None
    try:
        candles = await load_index_1m_spot(underlying, lookback_days=5)
        if candles:
            closed = _filter_closed_interval_rows(candles, interval="1minute")
            latest_session, latest_date = _latest_session_rows(closed)
            if latest_date is not None:
                prior_rows = [
                    c for c in closed
                    if (
                        (parsed := _parse_iso_timestamp(c.get("time"))) is not None
                        and parsed.astimezone(IST).date() < latest_date
                    )
                ]
                if prior_rows:
                    prior_session, _prior_date = _latest_session_rows(prior_rows)
                    if prior_session:
                        prior_profile = build_index_market_profile(underlying, prior_session)
    except Exception as exc:
        logger.debug(f"[s2_mp_of] prior MP load failed for {underlying}: {exc}")
        prior_profile = None

    _S2_PRIOR_PROFILE_CACHE[key] = prior_profile
    return prior_profile


# ─── Top-level evaluator ──────────────────────────────────────────────────


async def evaluate_strategy2_mp_of(
    *,
    underlying: str,
    started_at: Optional[datetime] = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Run the MP+OF evaluator on 1-min index spot and return an S2 signal.

    Loads everything internally — caller just provides the underlying.
    Returns the standard commodity-MP result dict with three S2-specific
    routing fields appended (``side``, ``expiry_tracks``, ``underlying``).

    When there isn't enough 1-minute history yet, returns ``{"signal":
    None, "reason": "insufficient_1m_spot", ...}`` so the caller can
    fall through to its legacy path.

    With ``persist=True`` (default) today's profile is saved to disk
    once IB has printed, so the historical timeline (Yesterday / Week /
    Month) populates automatically without a separate batch job.
    """
    # Late imports keep this module cheap when the flag is off.
    from paper_engine.commodity_mp_signal import evaluate_commodity_mp_signal
    from paper_engine.commodity_strategy_agent import (
        _infer_09ist_anchor,
        _compute_atr_series,
        _latest_session_rows,
        _filter_closed_interval_rows,
    )
    from paper_engine.commodity_profile_store import (
        build_daily_profile_from_snapshot,
        save_profile,
    )

    closed_1m_raw = await load_index_1m_spot(underlying)
    closed_1m = _filter_closed_interval_rows(closed_1m_raw, interval="1minute")
    if not closed_1m or len(closed_1m) < 30:
        return shape_result_for_s2(
            {
                "signal": None,
                "reason": "insufficient_1m_spot",
                "rows_seen": len(closed_1m or []),
            },
            underlying=underlying,
        )

    session_rows, session_date = _latest_session_rows(closed_1m)
    if not session_rows:
        return shape_result_for_s2(
            {"signal": None, "reason": "no_session_rows"},
            underlying=underlying,
        )

    today_profile = build_index_market_profile(underlying, session_rows)
    prior_profile = await load_strategy2_prior_session_profile(
        underlying, today=session_date,
    )
    cvd_anchor = _infer_09ist_anchor(closed_1m)
    atr_series = _compute_atr_series(closed_1m, period=14)
    atr_1m = atr_series[-1] if atr_series else None

    result = evaluate_commodity_mp_signal(
        closed_1m,
        symbol=underlying,
        today_profile=today_profile,
        prior_profile=prior_profile,
        cvd_anchor_index=cvd_anchor,
        atr_1m=atr_1m,
    )

    if (
        persist
        and today_profile is not None
        and int(getattr(today_profile, "period_count", 0) or 0) >= S2_MP_MIN_PERIODS
    ):
        try:
            snapshot = build_daily_profile_from_snapshot(underlying, today_profile)
            if snapshot is not None:
                save_profile(snapshot)
        except Exception as exc:
            logger.debug(f"[s2_mp_of] persist failed for {underlying}: {exc}")

    return shape_result_for_s2(result, underlying=underlying)
