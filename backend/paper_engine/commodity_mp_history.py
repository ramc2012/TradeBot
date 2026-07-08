"""Write-once durable commodity MP history.

The commodity MP+OF lane builds today's + the prior session's market profile
from a shallow live broker fetch (lookback 2/5 days) and writes one JSON per
session to ``runtime/commodity_profiles/<ROOT>/<date>.json`` via
``commodity_profile_store``. That leaves the live HTF gate reading only ~5-7
sessions of history.

This module backfills that history WRITE-ONCE from the durable 1-minute MCX
candle store (``underlying_spot_candles``, 500+ sessions per root). The
expensive/external fetch already happened once into that store; deriving a
per-session profile from it is local, so re-running never hits a broker and
never rewrites a session already on disk. Mirrors the auction-MP template
(``api/routers/auction_intelligence.backfill_durable_mp_history``) with three
commodity changes:

1. Group bars by IST session date and clip to the MCX window (09:00–23:59 IST),
   not the NSE 09:15–15:30 cash window. One IST date == one MCX session, so a
   per-session profile never spans a (non-back-adjusted) front-month roll.
2. Build with the per-instrument COARSE ``mp_profile_tick`` (same as the live
   build site) so backfilled and live profiles share identical geometry.
3. Persist via ``commodity_profile_store.save_profile`` skipping dates already
   present (write-once), and skip today until the MCX close.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, datetime, time

from loguru import logger

from auction_intelligence.market_profile.engine import MarketProfileEngine
from auction_intelligence.schemas import MarketBar
from market_data.commodity_contract_specs import (
    extract_commodity_root,
    get_commodity_contract_spec,
)
from paper_engine import commodity_profile_store as profile_store
from paper_engine.base_strategy_agent import _now_ist
from paper_engine.commodity_profile_store import (
    IST,
    build_daily_profile_from_snapshot,
    save_profile,
)

# MCX session window (IST). Full day session + evening session; the durable
# spot store carries bars up to ~23:55 IST. Clipping drops any spurious
# overnight/pre-open rows so the profile reflects the real session only.
_MCX_SESSION_START = time(9, 0)
_MCX_SESSION_END = time(23, 59)
_MCX_CLOSE = time(23, 30)

# Minimum 15-min periods (== FUTURES_MP_MIN_PERIODS) before a session profile is
# considered complete enough to persist — mirrors the live persist gate.
_MIN_PERIODS = 4
# Minimum 1-min bars in a session before we bother building.
_MIN_BARS = 20


async def _load_session_bars(root: str, *, limit: int) -> dict[date, list[MarketBar]]:
    """Per-IST-session 1-minute bars for ``root`` from underlying_spot_candles.

    Returns the ``limit`` most-recent sessions (>=20 candles), each clipped to
    the MCX window, keyed by IST session date.
    """
    normalized = root.upper()
    try:
        from sqlalchemy import text
        from db.database import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    -- One root can contain overlapping continuous and
                    -- contract-specific histories.  Building a profile from
                    -- every row mixes duplicate timestamps (and, around a
                    -- roll, conflicting prices).  Rank one coherent
                    -- source/instrument per session.  Prefer the continuous
                    -- series when its coverage is within 90% of the densest
                    -- candidate; otherwise use the densest contract.
                    WITH candidates AS (
                        SELECT timezone('Asia/Kolkata', time)::date AS session_date,
                               source,
                               instrument_key,
                               COUNT(DISTINCT time) AS bar_count
                        FROM underlying_spot_candles
                        WHERE underlying = :root
                          AND interval = '1minute'
                        GROUP BY 1, 2, 3
                        HAVING COUNT(DISTINCT time) >= :min_bars
                    ), scored AS (
                        SELECT *, MAX(bar_count) OVER (PARTITION BY session_date) AS max_bar_count
                        FROM candidates
                    ), ranked AS (
                        SELECT *, ROW_NUMBER() OVER (
                            PARTITION BY session_date
                            ORDER BY
                                CASE WHEN bar_count >= max_bar_count * 0.90 THEN 0 ELSE 1 END,
                                CASE WHEN source = 'fyers_mcx_cont' THEN 0 ELSE 1 END,
                                bar_count DESC,
                                instrument_key
                        ) AS source_rank
                        FROM scored
                    ), recent_sessions AS (
                        SELECT session_date, source, instrument_key
                        FROM ranked
                        WHERE source_rank = 1
                        ORDER BY session_date DESC
                        LIMIT :limit
                    )
                    SELECT r.session_date,
                           c.time, c.open, c.high, c.low, c.close, c.volume
                    FROM recent_sessions r
                    JOIN underlying_spot_candles c
                      ON c.underlying = :root
                     AND c.interval = '1minute'
                     AND c.source = r.source
                     AND c.instrument_key = r.instrument_key
                     AND timezone('Asia/Kolkata', c.time)::date = r.session_date
                    ORDER BY r.session_date ASC, c.time ASC
                    """
                ),
                {"root": normalized, "limit": limit, "min_bars": _MIN_BARS},
            )
            db_rows = result.mappings().all()
    except Exception as exc:
        logger.debug(f"[commodity_mp_history] candle load failed for {normalized}: {exc}")
        return {}

    bars_by_session: dict[date, list[MarketBar]] = defaultdict(list)
    for row in db_rows:
        session_date = row.get("session_date")
        if not isinstance(session_date, date):
            continue
        timestamp = row.get("time")
        if not isinstance(timestamp, datetime):
            continue
        ist_ts = timestamp.astimezone(IST)
        if not (_MCX_SESSION_START <= ist_ts.time() <= _MCX_SESSION_END):
            continue
        close = row.get("close")
        if close is None:
            continue
        try:
            bars_by_session[session_date].append(
                MarketBar(
                    timestamp=ist_ts,
                    open=float(row.get("open") or close),
                    high=float(row.get("high") or close),
                    low=float(row.get("low") or close),
                    close=float(close),
                    volume=float(row.get("volume") or 0.0),
                )
            )
        except (TypeError, ValueError):
            continue
    return bars_by_session


def _build_session_profile(root: str, spec, bars: list[MarketBar]):
    """Build one session's MarketProfileSnapshot at the coarse value tick."""
    engine = MarketProfileEngine(
        {
            "period_minutes": 15,
            "tick_size": spec.mp_profile_tick(),
            "initial_balance_periods": 4,
            "value_area_pct": 0.70,
            "min_tail_tpos": 2,
        }
    )
    return engine.build_profile(symbol=root, bars=bars)


async def backfill_commodity_mp_history(
    root: str,
    *,
    lookback_sessions: int = 90,
    force: bool = False,
    reason: str = "scheduled_repair",
) -> dict[str, object]:
    """Write-once durable commodity MP history for one root.

    Builds per-session profiles from underlying_spot_candles and persists ONLY
    session dates not already on disk (unless ``force``). Today is included only
    after the MCX close. Idempotent — re-runs fill genuine gaps only.
    """
    normalized = extract_commodity_root(root) if ":" in root else root.upper()
    spec = get_commodity_contract_spec(normalized)
    now_ist = _now_ist()
    today = now_ist.date()
    after_close = now_ist.time() > _MCX_CLOSE

    if force:
        profile_store.invalidate_cache(normalized)
        existing: set[date] = set()
    else:
        existing = {
            p.session_date
            for p in profile_store.load_recent(normalized, days=lookback_sessions + 10)
        }

    sessions = await _load_session_bars(normalized, limit=lookback_sessions)

    persisted = 0
    filled: list[str] = []
    skipped_existing = 0
    skipped_today_open = 0
    skipped_build = 0
    for session_date in sorted(sessions):
        # Scheduling valve: this backfill loops up to ~90 sessions of pure-CPU
        # TPO building; without a yield the whole loop runs as one event-loop
        # block (it blew its 420s watchdog inside the 2026-07-08 wedge).
        await asyncio.sleep(0)
        if not force and session_date in existing:
            skipped_existing += 1
            continue
        if session_date >= today and not after_close:
            skipped_today_open += 1
            continue
        bars = sessions[session_date]
        if len(bars) < _MIN_BARS:
            skipped_build += 1
            continue
        try:
            # Pure per-session TPO build (own MarketProfileEngine, reads only
            # the local bars list) — run off-loop so a 90-session backfill
            # cannot wedge the tape/API.
            snapshot = await asyncio.to_thread(_build_session_profile, normalized, spec, bars)
        except Exception as exc:
            logger.debug(f"[commodity_mp_history] build failed {normalized} {session_date}: {exc}")
            skipped_build += 1
            continue
        if snapshot is None or int(getattr(snapshot, "period_count", 0) or 0) < _MIN_PERIODS:
            skipped_build += 1
            continue
        daily = build_daily_profile_from_snapshot(normalized, snapshot)
        if daily is None:
            skipped_build += 1
            continue
        if save_profile(daily):
            persisted += 1
            filled.append(session_date.isoformat())

    if persisted:
        # save_profile refreshes the cache per write, but force-overwrites may
        # have left stale segment reads; drop the root cache so the next gate
        # read re-segments against the rewritten files.
        profile_store.invalidate_cache(normalized)
        logger.info(
            f"[commodity_mp_history] {normalized}: persisted {persisted} session(s) "
            f"(existing={len(existing)}, reason={reason}); filled={filled}"
        )

    return {
        "root": normalized,
        "reason": reason,
        "tick": spec.mp_profile_tick(),
        "existing_dates": len(existing),
        "candidate_sessions": len(sessions),
        "missing_persisted": persisted,
        "filled_dates": filled,
        "skipped_existing": skipped_existing,
        "skipped_today_open": skipped_today_open,
        "skipped_build": skipped_build,
        "after_close": after_close,
    }
