"""Internal MCX commodity quotes for the macro research board.

Stooq retired its delayed-quote endpoint (``/q/l/`` answers 404 as of
2026-08), which left every commodity card on offline seeds. The desk already
maintains live + backfilled 1-minute MCX futures candles in
``underlying_spot_candles`` (source=commodity_broker_history for the durable
history, live_tick intra-session), so the MCX-covered names read those rows
instead of an external USD proxy. Pricing is therefore INR (exchange units:
CRUDEOIL ₹/bbl, GOLD ₹/10g, COPPER ₹/kg).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger

UTC = timezone.utc
IST = ZoneInfo("Asia/Kolkata")

# Dense durable history first; live_tick covers the running session before the
# broker-history sweep lands.
_SOURCE_PREFERENCE = ("commodity_broker_history", "live_tick")
# Long weekend + one holiday still leaves two MCX sessions in range.
_LOOKBACK_DAYS = 6


def compute_quote_from_rows(
    rows: list[tuple[datetime, str, float]],
) -> dict[str, Any] | None:
    """Collapse (time, source, close) 1-minute rows into a board quote.

    ``change_pct`` is the last close of the latest IST session against the
    prior session's last close (day-over-day). With only one session in range
    it degrades to change against that session's first bar (intraday).
    """
    if not rows:
        return None

    by_minute: dict[datetime, tuple[int, float]] = {}
    for ts, source, close in rows:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        try:
            rank = _SOURCE_PREFERENCE.index(source)
        except ValueError:
            rank = len(_SOURCE_PREFERENCE)
        existing = by_minute.get(ts)
        if existing is None or rank < existing[0]:
            by_minute[ts] = (rank, float(close))

    sessions: dict[date, list[tuple[datetime, float]]] = {}
    for ts in sorted(by_minute):
        sessions.setdefault(ts.astimezone(IST).date(), []).append(
            (ts, by_minute[ts][1])
        )
    if not sessions:
        return None

    ordered_dates = sorted(sessions)
    latest_bars = sessions[ordered_dates[-1]]
    as_of_ts, price = latest_bars[-1]
    if len(ordered_dates) > 1:
        reference = sessions[ordered_dates[-2]][-1][1]
        basis = "prev_session_close"
    else:
        reference = latest_bars[0][1]
        basis = "session_open"
    change_pct = ((price / reference) - 1.0) * 100.0 if reference else 0.0
    return {
        "price": round(price, 3),
        "change_pct": round(change_pct, 3),
        "as_of": as_of_ts.astimezone(IST).isoformat(),
        "source": "mcx_internal_1m",
        "change_basis": basis,
    }


async def fetch_mcx_quote(root: str) -> dict[str, Any] | None:
    """Latest MCX quote for ``root`` from underlying_spot_candles, or None."""
    # Bound `time` directly with a UTC datetime so TimescaleDB chunk exclusion
    # applies (never wrap the partitioning column in a function).
    cutoff = datetime.now(UTC) - timedelta(days=_LOOKBACK_DAYS)
    try:
        from sqlalchemy import text

        from db.database import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    SELECT time, source, close
                    FROM underlying_spot_candles
                    WHERE underlying = :root
                      AND interval = '1minute'
                      AND source IN ('commodity_broker_history', 'live_tick')
                      AND time >= :cutoff
                    ORDER BY time ASC
                    """
                ),
                {"root": str(root).upper(), "cutoff": cutoff},
            )
            rows = [
                (row[0], str(row[1]), float(row[2]))
                for row in result.fetchall()
                if row[2] is not None
            ]
    except Exception as exc:
        logger.debug(f"[MacroResearch] MCX internal quote read failed for {root}: {exc}")
        return None
    return compute_quote_from_rows(rows)
