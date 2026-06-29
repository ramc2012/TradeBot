"""MACD diffusion — hourly CE/PE breadth to sense market sentiment.

Every hour we record, across the tracked ATM F&O universe, how many CE legs and
how many PE legs have MACD > 0. The reading is a diffusion/breadth index:

    many CE above zero + few PE above zero  →  bullish tape
    few  CE above zero + many PE above zero →  bearish tape

`net_diffusion = ce_pct - pe_pct` collapses that into a single signed sentiment
number in [-1, 1].

Two write paths:
  • compute_and_store()    — the live hourly snapshot, reads the freshest per-leg
                             MACD from atm_option_watchlist_snapshots (which only
                             retains ~1 day) and upserts the current hour bucket.
  • backfill_from_candles()— seeds history by recomputing each currently-tracked
                             leg's 30m premium MACD from option_premium_candles
                             and bucketing the signs by hour. Fills empty buckets
                             only (never clobbers a live row).
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import text

from analysis.macd_engine import compute_macd
from db.database import AsyncSessionLocal


def _pcts(ce_total: int, ce_above: int, pe_total: int, pe_above: int) -> tuple[float | None, float | None, float | None]:
    ce_pct = (ce_above / ce_total) if ce_total else None
    pe_pct = (pe_above / pe_total) if pe_total else None
    net = (ce_pct - pe_pct) if (ce_pct is not None and pe_pct is not None) else None
    return ce_pct, pe_pct, net


_UPSERT_LIVE = """
    INSERT INTO macd_diffusion_snapshots
        (bucket_time, market, ce_total, ce_above_zero, pe_total, pe_above_zero, ce_pct, pe_pct, net_diffusion, source)
    VALUES (:bucket, :market, :ce_total, :ce_above, :pe_total, :pe_above, :ce_pct, :pe_pct, :net, 'live')
    ON CONFLICT (market, bucket_time) DO UPDATE SET
        ce_total = EXCLUDED.ce_total,
        ce_above_zero = EXCLUDED.ce_above_zero,
        pe_total = EXCLUDED.pe_total,
        pe_above_zero = EXCLUDED.pe_above_zero,
        ce_pct = EXCLUDED.ce_pct,
        pe_pct = EXCLUDED.pe_pct,
        net_diffusion = EXCLUDED.net_diffusion,
        source = 'live',
        created_at = now()
"""

# Backfill only fills empty buckets — a live snapshot always wins.
_UPSERT_BACKFILL = """
    INSERT INTO macd_diffusion_snapshots
        (bucket_time, market, ce_total, ce_above_zero, pe_total, pe_above_zero, ce_pct, pe_pct, net_diffusion, source)
    VALUES (:bucket, :market, :ce_total, :ce_above, :pe_total, :pe_above, :ce_pct, :pe_pct, :net, 'backfill')
    ON CONFLICT (market, bucket_time) DO NOTHING
"""


async def compute_and_store(*, market: str = "NSE", bucket: datetime | None = None) -> dict[str, Any] | None:
    """Snapshot the current CE/PE-above-zero breadth into the current hour bucket.

    Reads the freshest MACD per logical contract from the live watchlist
    snapshot table. Returns the stored counts, or None when there's nothing to
    count yet (e.g. before the first watchlist build of the day).
    """
    now = (bucket or datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0)
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text(
                    """
                    WITH latest AS (
                        SELECT DISTINCT ON (underlying, expiry, strike, option_type)
                               option_type, macd
                        FROM atm_option_watchlist_snapshots
                        WHERE macd IS NOT NULL
                        ORDER BY underlying, expiry, strike, option_type, time DESC
                    )
                    SELECT
                        COUNT(*) FILTER (WHERE option_type = 'CE')                AS ce_total,
                        COUNT(*) FILTER (WHERE option_type = 'CE' AND macd > 0)   AS ce_above,
                        COUNT(*) FILTER (WHERE option_type = 'PE')                AS pe_total,
                        COUNT(*) FILTER (WHERE option_type = 'PE' AND macd > 0)   AS pe_above
                    FROM latest
                    """
                )
            )
        ).first()
        ce_total = int(row.ce_total or 0)
        ce_above = int(row.ce_above or 0)
        pe_total = int(row.pe_total or 0)
        pe_above = int(row.pe_above or 0)
        if ce_total == 0 and pe_total == 0:
            return None
        ce_pct, pe_pct, net = _pcts(ce_total, ce_above, pe_total, pe_above)
        await session.execute(
            text(_UPSERT_LIVE),
            {
                "bucket": now, "market": market,
                "ce_total": ce_total, "ce_above": ce_above,
                "pe_total": pe_total, "pe_above": pe_above,
                "ce_pct": ce_pct, "pe_pct": pe_pct, "net": net,
            },
        )
        await session.commit()
    result = {
        "bucket_time": now.replace(minute=0, second=0, microsecond=0).isoformat(),
        "ce_total": ce_total, "ce_above_zero": ce_above,
        "pe_total": pe_total, "pe_above_zero": pe_above,
        "net_diffusion": net,
    }
    logger.info(f"[MACDDiffusion] live snapshot CE {ce_above}/{ce_total} · PE {pe_above}/{pe_total} · net {net}")
    return result


async def backfill_from_candles(*, market: str = "NSE", days: int = 21) -> int:
    """Seed historical hourly diffusion from option_premium_candles.

    For each currently-tracked ATM leg, recompute the 30m premium MACD and, per
    hour, take the latest bar's MACD sign; aggregate the CE/PE counts across legs
    into hourly buckets. Pure DB reads (no broker calls). Returns buckets filled.
    """
    async with AsyncSessionLocal() as session:
        legs = (
            await session.execute(
                text(
                    """
                    SELECT DISTINCT underlying, expiry, strike, option_type
                    FROM atm_option_watchlist_snapshots
                    WHERE expiry >= CURRENT_DATE
                    """
                )
            )
        ).all()

        agg: dict[datetime, dict[str, int]] = defaultdict(
            lambda: {"ce_total": 0, "ce_above": 0, "pe_total": 0, "pe_above": 0}
        )

        for leg in legs:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT time, close FROM (
                            SELECT DISTINCT ON (time) time, close
                            FROM option_premium_candles
                            WHERE underlying = :u AND expiry = :e AND strike = :s
                              AND option_type = :ot AND interval = '30minute'
                              AND time >= now() - make_interval(days => :days)
                              AND close IS NOT NULL
                            ORDER BY time, synced_at DESC NULLS LAST
                        ) d ORDER BY time ASC
                        """
                    ),
                    {"u": leg.underlying, "e": leg.expiry, "s": leg.strike, "ot": leg.option_type, "days": days},
                )
            ).all()
            if len(rows) < 35:  # not enough to warm up MACD (slow EMA 26 + signal 9)
                continue
            closes = [float(r.close) for r in rows]
            macd_line, _, _ = compute_macd(closes)
            # latest MACD per hour for this leg
            per_hour: dict[datetime, float] = {}
            for r, m in zip(rows, macd_line):
                if m is None:
                    continue
                t = r.time if r.time.tzinfo else r.time.replace(tzinfo=timezone.utc)
                per_hour[t.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)] = m
            is_ce = leg.option_type == "CE"
            for hour, m in per_hour.items():
                b = agg[hour]
                if is_ce:
                    b["ce_total"] += 1
                    if m > 0:
                        b["ce_above"] += 1
                else:
                    b["pe_total"] += 1
                    if m > 0:
                        b["pe_above"] += 1

        filled = 0
        for hour, b in agg.items():
            ce_pct, pe_pct, net = _pcts(b["ce_total"], b["ce_above"], b["pe_total"], b["pe_above"])
            await session.execute(
                text(_UPSERT_BACKFILL),
                {
                    "bucket": hour, "market": market,
                    "ce_total": b["ce_total"], "ce_above": b["ce_above"],
                    "pe_total": b["pe_total"], "pe_above": b["pe_above"],
                    "ce_pct": ce_pct, "pe_pct": pe_pct, "net": net,
                },
            )
            filled += 1
        await session.commit()
    logger.info(f"[MACDDiffusion] backfilled {filled} hourly buckets from {len(legs)} legs ({days}d)")
    return filled


async def run_daemon(*, poll_minutes: int = 60, backfill_days: int = 21) -> None:
    """Backfill once at startup, then snapshot the live breadth every hour."""
    logger.info(f"[MACDDiffusion] daemon starting (poll={poll_minutes}m, backfill={backfill_days}d)")
    try:
        await backfill_from_candles(days=backfill_days)
    except Exception as exc:  # noqa: BLE001 — backfill is best-effort
        logger.warning(f"[MACDDiffusion] startup backfill skipped: {exc}")
    while True:
        try:
            await compute_and_store()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[MACDDiffusion] hourly snapshot failed: {exc}")
        await asyncio.sleep(max(60, poll_minutes * 60))
