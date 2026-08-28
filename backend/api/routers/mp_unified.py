"""/api/mp/unified — one Market-Profile snapshot for every UI surface.

THE CONVERGENCE POINT. Auction-intelligence widgets, the convergence dashboard,
the fractal-MP page and the commodity monitor each computed their own profile
of the same session. This endpoint serves the canonical snapshot from mp_core
(single TPO engine + research intelligence + compute-once cache) so the
frontends converge on one request shape and the backend on one computation.

Sources, in order of preference per symbol:
    index_futures_candles   NIFTY / BANKNIFTY (real volume; the tradeable print)
    underlying_spot_candles everything else (stocks carry volume; indices with
                            zero volume still build valid TPO profiles)

Weekly/monthly value areas come from the vanguard features_mp table when
present (the vanguard cycle writes them nightly); absent that, the oversold
flag is null rather than approximated — a partial multi-timeframe check is a
different, unvalidated signal.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from db.database import AsyncSessionLocal
from mp_core import VERDICTS, cache_stats, unified_snapshot

router = APIRouter(prefix="/api/mp/unified", tags=["mp-unified"])

_FUTURES_SYMBOLS = {"NIFTY", "BANKNIFTY", "SENSEX"}

_BARS_SQL = """
SELECT (time AT TIME ZONE 'Asia/Kolkata') AS time,
       date(time AT TIME ZONE 'Asia/Kolkata') AS dt,
       open, high, low, close, volume
FROM {table}
WHERE underlying = :symbol AND interval = '30minute'
  AND (time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15' AND '15:15'
  AND date(time AT TIME ZONE 'Asia/Kolkata') IN (
      SELECT DISTINCT date(time AT TIME ZONE 'Asia/Kolkata')
      FROM {table}
      WHERE underlying = :symbol AND interval = '30minute'
      ORDER BY 1 DESC LIMIT 2)
ORDER BY time
"""

_MTF_SQL = """
SELECT w_loc, m_loc, dt FROM features_mp
WHERE underlying = :symbol ORDER BY dt DESC LIMIT 1
"""


async def _load_two_sessions(symbol: str) -> tuple[list[dict], list[dict], str]:
    table = ("index_futures_candles" if symbol in _FUTURES_SYMBOLS
             else "underlying_spot_candles")
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(_BARS_SQL.format(table=table)), {"symbol": symbol})
        rows = [dict(r) for r in result.mappings().all()]
    if not rows:
        # futures preference falls back to spot rather than 404ing an index
        if table == "index_futures_candles":
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text(_BARS_SQL.format(table="underlying_spot_candles")),
                    {"symbol": symbol})
                rows = [dict(r) for r in result.mappings().all()]
            table = "underlying_spot_candles"
    if not rows:
        raise HTTPException(status_code=404,
                            detail=f"no 30minute bars for {symbol}")
    days = sorted({r["dt"] for r in rows})
    current = [r for r in rows if r["dt"] == days[-1]]
    prior = [r for r in rows if len(days) > 1 and r["dt"] == days[-2]]
    return current, prior, table


@router.get("/snapshot")
async def snapshot(symbol: str = Query("BANKNIFTY")) -> dict[str, Any]:
    sym = symbol.upper().strip()
    current, prior, table = await _load_two_sessions(sym)
    tick = max(round(float(current[-1]["close"]) * 0.0004, 2), 0.05)
    payload = unified_snapshot(sym, current, tick_size=tick,
                               prior_bars=prior or None)
    payload["source_table"] = table
    return payload


@router.get("/verdicts")
async def verdicts() -> dict[str, Any]:
    """The research verdicts, for any surface that renders MP metrics."""
    return {"verdicts": VERDICTS, "measured": "2026-08-28",
            "cache": cache_stats()}
