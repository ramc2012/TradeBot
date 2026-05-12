"""Data Quality agent read endpoints.

`/api/data-quality/snapshot` — per-symbol tick freshness ledger.
`/api/data-quality/candle-gaps` — detects missing rows in
    `underlying_spot_candles` and `option_premium_candles` over a recent
    window. Run-time visibility into whether the persistence layer is
    keeping up with the live feed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Query
from sqlalchemy import text

from db.database import AsyncSessionLocal
from market_data.data_quality_agent import data_quality_agent


router = APIRouter(prefix="/api/data-quality", tags=["data-quality"])


@router.get("/snapshot")
async def data_quality_snapshot() -> dict[str, Any]:
    return data_quality_agent.snapshot()


_INTERVAL_MINUTES = {
    "1minute": 1,
    "3minute": 3,
    "5minute": 5,
    "15minute": 15,
    "30minute": 30,
    "60minute": 60,
}


@router.get("/candle-gaps")
async def candle_gaps(
    table: str = Query(default="underlying_spot_candles"),
    interval: str = Query(default="1minute"),
    lookback_minutes: int = Query(default=60, ge=5, le=1440),
    per_symbol_limit: int = Query(default=25, ge=1, le=200),
) -> dict[str, Any]:
    """Compute the expected vs actual minute-candle count per symbol over
    the lookback window. Surfaces persistence gaps without requiring a
    full DB inspection.

    `table` ∈ {underlying_spot_candles, option_premium_candles}
    `interval` matches the `interval` column on those tables.
    """
    safe_table = table.strip().lower()
    if safe_table not in {"underlying_spot_candles", "option_premium_candles"}:
        return {"error": "unsupported table"}
    step_minutes = _INTERVAL_MINUTES.get(interval.strip().lower())
    if not step_minutes:
        return {"error": "unsupported interval"}

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=lookback_minutes)
    # Bucket-bounded expected count.
    expected = max(1, lookback_minutes // step_minutes)

    if safe_table == "underlying_spot_candles":
        sql = text(
            """
            SELECT underlying AS symbol,
                   COUNT(*) AS actual_count,
                   MAX(time) AS last_time,
                   MIN(time) AS first_time
            FROM underlying_spot_candles
            WHERE interval = :interval
              AND time >= :window_start
            GROUP BY underlying
            ORDER BY last_time DESC NULLS LAST
            LIMIT :per_symbol_limit
            """
        )
    else:
        sql = text(
            """
            SELECT instrument_key AS symbol,
                   COUNT(*) AS actual_count,
                   MAX(time) AS last_time,
                   MIN(time) AS first_time
            FROM option_premium_candles
            WHERE interval = :interval
              AND time >= :window_start
            GROUP BY instrument_key
            ORDER BY last_time DESC NULLS LAST
            LIMIT :per_symbol_limit
            """
        )

    rows: list[dict[str, Any]] = []
    overall_actual = 0
    overall_symbols = 0
    overall_with_gaps = 0
    try:
        async with AsyncSessionLocal() as session:
            # Cap at 8 seconds so the request fails fast on db-f1-micro
            # rather than hanging an SSL connection for 90s and pinning a
            # pool slot. Returns "error" in the payload so the caller knows.
            await session.execute(text("SET LOCAL statement_timeout = '8s'"))
            result = await session.execute(
                sql,
                {
                    "interval": interval,
                    "window_start": window_start,
                    "per_symbol_limit": per_symbol_limit,
                },
            )
            for record in result.mappings():
                actual = int(record["actual_count"] or 0)
                last_time: Optional[datetime] = record["last_time"]
                gap = max(0, expected - actual)
                gap_pct = round(100.0 * gap / expected, 1) if expected else 0.0
                last_age = (
                    round((now - last_time.astimezone(timezone.utc)).total_seconds(), 1)
                    if last_time is not None
                    else None
                )
                if gap > 0:
                    overall_with_gaps += 1
                overall_actual += actual
                overall_symbols += 1
                rows.append(
                    {
                        "symbol": record["symbol"],
                        "actual_count": actual,
                        "expected_count": expected,
                        "gap_count": gap,
                        "gap_pct": gap_pct,
                        "last_time": last_time.isoformat() if last_time is not None else None,
                        "last_age_seconds": last_age,
                    }
                )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}

    rows.sort(key=lambda r: r["gap_count"], reverse=True)
    return {
        "table": safe_table,
        "interval": interval,
        "lookback_minutes": lookback_minutes,
        "expected_count_per_symbol": expected,
        "symbol_count": overall_symbols,
        "symbols_with_gaps": overall_with_gaps,
        "total_actual_rows": overall_actual,
        "window_start": window_start.isoformat(),
        "now": now.isoformat(),
        "rows": rows,
    }
