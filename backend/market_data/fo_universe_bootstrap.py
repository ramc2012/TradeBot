from __future__ import annotations

import asyncio
from datetime import date, timedelta
from time import monotonic
from typing import Any

from loguru import logger
from sqlalchemy import text

from analysis.backtest import MACDBacktester
from api.routers.auth import (
    ensure_upstox_session,
    get_broker_token,
    refresh_persistent_credentials_async,
)
from db.database import AsyncSessionLocal
from market_data.catalog_integrity import (
    assert_unique_spot_keys,
    filter_key_collisions,
)


MIN_UNDERLYING_ROWS = 50
BOOTSTRAP_TTL_SECONDS = 900.0
_bootstrap_lock = asyncio.Lock()
_last_bootstrap_checked_at = 0.0
_last_bootstrap_result: dict[str, Any] = {}


async def _catalog_counts() -> dict[str, int]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT
                    COUNT(*) AS total_rows,
                    COUNT(*) FILTER (
                        WHERE spot_instrument_key IS NOT NULL
                          AND underlying_key IS NOT NULL
                    ) AS keyed_rows,
                    COUNT(*) FILTER (
                        WHERE kind = 'STOCK'
                          AND spot_instrument_key IS NOT NULL
                          AND underlying_key IS NOT NULL
                    ) AS keyed_stocks
                FROM fo_underlying_catalog
                """
            )
        )
        row = result.fetchone()
    if row is None:
        return {"total_rows": 0, "keyed_rows": 0, "keyed_stocks": 0}
    return {
        "total_rows": int(row.total_rows or 0),
        "keyed_rows": int(row.keyed_rows or 0),
        "keyed_stocks": int(row.keyed_stocks or 0),
    }


def _catalog_is_ready(counts: dict[str, int], *, min_rows: int) -> bool:
    return (
        int(counts.get("keyed_rows") or 0) >= min_rows
        and int(counts.get("keyed_stocks") or 0) > 0
    )


async def _upsert_universe_rows(rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                INSERT INTO fo_underlying_catalog (symbol, kind, updated_at)
                VALUES (:symbol, :kind, NOW())
                ON CONFLICT (symbol) DO UPDATE
                SET kind = EXCLUDED.kind,
                    updated_at = NOW()
                """
            ),
            rows,
        )
        await session.commit()


async def _upsert_metadata_rows(rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    async with AsyncSessionLocal() as session:
        # Never let a re-bootstrap assign an instrument key that another symbol
        # already owns — that is the M&M/MARUTI ISIN collision, which silently
        # corrupts underlying_spot_candles for BOTH names (see catalog_integrity).
        rows = await filter_key_collisions(session, rows)
        if not rows:
            return
        await session.execute(
            text(
                """
                UPDATE fo_underlying_catalog
                SET spot_instrument_key = COALESCE(:spot_instrument_key, spot_instrument_key),
                    underlying_key = COALESCE(:underlying_key, underlying_key),
                    updated_at = NOW()
                WHERE symbol = :symbol
                """
            ),
            rows,
        )
        await session.commit()


async def ensure_fo_underlying_catalog(
    *,
    min_rows: int = MIN_UNDERLYING_ROWS,
    force: bool = False,
) -> dict[str, Any]:
    """Ensure the live NSE F&O underlying catalog is populated with broker keys.

    This is the minimum catalog Strategy 1 and the ATM watchlist need in order
    to scan the full stock universe on a fresh cloud deployment.
    """
    global _last_bootstrap_checked_at, _last_bootstrap_result

    import os
    if os.environ.get("SHARED_MP_REDIS_URL"):
        try:
            from market_data.fno_membership import sync_membership
            await sync_membership(force=force)
        except Exception as exc:
            logger.warning(f"Canonical F&O membership refresh deferred: {exc}")

    now = monotonic()
    if not force and _last_bootstrap_result and (now - _last_bootstrap_checked_at) < BOOTSTRAP_TTL_SECONDS:
        counts = dict(_last_bootstrap_result.get("counts_after") or {})
        if _catalog_is_ready(counts, min_rows=min_rows):
            return _last_bootstrap_result

    async with _bootstrap_lock:
        counts_before = await _catalog_counts()
        if not force and _catalog_is_ready(counts_before, min_rows=min_rows):
            _last_bootstrap_checked_at = monotonic()
            _last_bootstrap_result = {
                "status": "ready",
                "counts_before": counts_before,
                "counts_after": counts_before,
                "universe_rows": 0,
                "metadata_rows": 0,
            }
            return _last_bootstrap_result

        await refresh_persistent_credentials_async(force=True)
        await ensure_upstox_session(force_validate=True)
        token = str(get_broker_token("upstox") or "").strip()
        if not token:
            result = {
                "status": "skipped_no_upstox",
                "counts_before": counts_before,
                "counts_after": counts_before,
                "universe_rows": 0,
                "metadata_rows": 0,
            }
            _last_bootstrap_checked_at = monotonic()
            _last_bootstrap_result = result
            return result

        backtester = MACDBacktester(access_token=token)
        universe = await backtester.fetch_fo_universe()
        universe_rows = [
            {"symbol": symbol, "kind": "INDEX"}
            for symbol in sorted(universe.get("indices") or [])
        ] + [
            {"symbol": symbol, "kind": "STOCK"}
            for symbol in sorted(universe.get("stocks") or [])
        ]
        await _upsert_universe_rows(universe_rows)

        async def resolve(symbol: str) -> dict[str, str] | None:
            meta = await backtester._resolve_underlying_metadata(symbol)
            if not meta:
                return None
            spot_key = str(meta.get("spot_instrument_key") or "").strip()
            underlying_key = str(meta.get("underlying_key") or "").strip()
            if not spot_key or not underlying_key:
                return None
            return {
                "symbol": symbol,
                "spot_instrument_key": spot_key,
                "underlying_key": underlying_key,
            }

        symbols = [row["symbol"] for row in universe_rows]
        resolved = [row for row in await asyncio.gather(*(resolve(symbol) for symbol in symbols)) if row]
        await _upsert_metadata_rows(resolved)

        counts_after = await _catalog_counts()
        # Loud, explicit invariant check: no two underlyings may share a
        # spot_instrument_key. Non-raising so one bad row can never take the
        # backend dark, but it ERRORs per collision so it cannot pass silently.
        async with AsyncSessionLocal() as session:
            collisions = await assert_unique_spot_keys(session)
        result = {
            "status": "ready" if _catalog_is_ready(counts_after, min_rows=min_rows) else "partial",
            "counts_before": counts_before,
            "counts_after": counts_after,
            "universe_rows": len(universe_rows),
            "metadata_rows": len(resolved),
            "key_collisions": collisions,
        }
        _last_bootstrap_checked_at = monotonic()
        _last_bootstrap_result = result
        logger.info(
            "[FO universe] bootstrap complete: "
            f"{result['status']} | keyed_rows={counts_after['keyed_rows']} | "
            f"keyed_stocks={counts_after['keyed_stocks']}"
        )
        return result

