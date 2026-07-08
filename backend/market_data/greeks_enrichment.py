"""Greeks enrichment — stamp real broker greeks onto greeks-null option candles.

Background
----------
Until 2026-06-23 a Fyers-native index-option backfill wrote 1-minute
`option_premium_candles` rows for the five indices with Black-Scholes-computed
greeks (`source='fyers'`). That writer lived only on an unmerged branch and was
operated as manual batch passes; after 06-23 it stopped and was never in `main`,
so every option candle written since (the broker-history backfill — `source in
('fyers','upstox')` at 3/5/15/30-minute) carries `iv/delta/gamma/theta/vega =
NULL`. Charts and iv-consuming analytics went blind on live contracts.

This module restores greeks WITHOUT re-fetching from a broker or recomputing
Black-Scholes: it reuses `option_chain_snapshots`, which the live option-chain
service already persists (per-strike broker greeks every ~120s for the tracked
index expiries) and which keeps flowing today. For each greeks-null index option
bar we find the nearest chain snapshot for the same contract and copy its greeks.

Scope & conventions (verified against the live DB)
--------------------------------------------------
- Indices only. `option_chain_snapshots` only covers the five index underlyings;
  their symbol is stored in Fyers index form (`NSE:BANKNIFTY-INDEX`) while
  `option_premium_candles.underlying` is the plain app symbol (`BANKNIFTY`), so
  we map between them explicitly.
- Only `source in ('fyers','upstox')` rows are touched. `upstox_expired` greeks
  are owned by the research-sync daemon (its own Black-Scholes pass) — leave them.
- IV UNIT FIX: `option_chain_snapshots.iv` is in PERCENT (e.g. 13.98) whereas
  `option_premium_candles.iv` is a FRACTION (e.g. 0.1398, the historical
  convention every consumer expects). We divide snapshot iv by 100. delta / gamma
  / theta / vega share the same unit across both tables and are copied as-is.
- Match window: a snapshot whose time falls within the bar `[t - 90s, t + bar +
  90s]`, ranked by proximity to the bar midpoint. Greeks vary slowly, so the
  ~120s snapshot cadence comfortably covers every bar size we enrich.
- underlying_price is deliberately NOT written here (the snapshot table carries
  no per-row spot); it stays whatever the OHLC writer left. The positional vol
  gate already derives ATM IV independently (`positioning_feed._implied_vol`).

Coverage is intentionally partial: strikes outside the chain-poller's tracked
band, or bars on days the poller was degraded, keep NULL greeks. That is the
accepted trade-off of a snapshot-sourced (zero extra broker load) enrichment.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import text

from db.database import AsyncSessionLocal

# opc.underlying (app symbol) -> option_chain_snapshots.symbol (Fyers index form).
# These are the only five underlyings the live option-chain service tracks; the
# mapping is stable. Verified live 2026-07-06 against DISTINCT symbol in the table.
INDEX_SYMBOL_MAP: dict[str, str] = {
    "NIFTY": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:BANKNIFTY-INDEX",
    "FINNIFTY": "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
    "SENSEX": "BSE:SENSEX-INDEX",
}

# Bar size in seconds per interval — used to centre the snapshot match on the bar
# midpoint and bound the search window. Only intervals the broker-history backfill
# actually writes for options.
INTERVAL_SECONDS: dict[str, int] = {
    "1minute": 60,
    "3minute": 180,
    "5minute": 300,
    "15minute": 900,
    "30minute": 1800,
}

DEFAULT_INTERVALS: tuple[str, ...] = ("30minute", "15minute", "5minute", "3minute", "1minute")

# Snapshot rows created only by the two broker-history writers whose option rows
# are greeks-null. `upstox_expired` (research-sync) is intentionally excluded.
_TARGET_SOURCES: tuple[str, ...] = ("fyers", "upstox")

# Slack around the bar so a snapshot landing just before the open / just after the
# close of a bar still matches.
_WINDOW_SLACK_SECONDS = 90

# Terminal hot-path guardrails. The enrichment job is helpful, but it must never
# block websocket lanes or paper supervisors while it catches up.
_WINDOW_HOURS = 2
_DAEMON_LOOKBACK_HOURS = 6
_DAEMON_BATCH = 10_000
_STATEMENT_TIMEOUT_MS = 20_000
_LOCK_TIMEOUT_MS = 3_000
_IST = ZoneInfo("Asia/Kolkata")
_POST_CLOSE_GRACE_END = time(16, 0)

# One index UPDATE, per interval, over a short bounded window. Fills only greeks-null
# target rows; ranks candidate snapshots by proximity to the bar midpoint and copies
# the nearest one's greeks (iv scaled percent -> fraction). `:batch` is a safety cap
# on a single window's rows, not a pagination cursor.
_ENRICH_SQL = text(
    """
    WITH symmap(underlying, ocs_symbol) AS (
        SELECT * FROM unnest(CAST(:underlyings AS text[]), CAST(:ocs_symbols AS text[]))
    ),
    tgt AS (
        SELECT opc.instrument_key, opc.interval, opc.time,
               opc.underlying, opc.expiry, opc.strike, opc.option_type
        FROM option_premium_candles opc
        JOIN symmap ON symmap.underlying = opc.underlying
        WHERE opc.interval = :interval
          AND opc.iv IS NULL
          AND opc.source = ANY(:sources)
          AND opc.time >= :since AND opc.time < :until
        ORDER BY opc.time
        LIMIT :batch
    ),
    matched AS (
        SELECT tgt.instrument_key, tgt.interval, tgt.time,
               s.iv, s.delta, s.gamma, s.theta, s.vega
        FROM tgt
        JOIN symmap m ON m.underlying = tgt.underlying
        JOIN LATERAL (
            SELECT ocs.iv, ocs.delta, ocs.gamma, ocs.theta, ocs.vega
            FROM option_chain_snapshots ocs
            WHERE ocs.symbol = m.ocs_symbol
              AND ocs.expiry = tgt.expiry::text
              AND ocs.strike = tgt.strike
              AND ocs.option_type = tgt.option_type
              AND ocs.iv IS NOT NULL
              AND ocs.time BETWEEN tgt.time - make_interval(secs => :slack)
                               AND tgt.time + make_interval(secs => :bar_plus_slack)
            ORDER BY abs(extract(epoch FROM (
                        ocs.time - (tgt.time + make_interval(secs => :half_bar)))))
            LIMIT 1
        ) s ON true
    )
    UPDATE option_premium_candles opc
    SET iv = matched.iv / 100.0,
        delta = matched.delta,
        gamma = matched.gamma,
        theta = matched.theta,
        vega = matched.vega,
        synced_at = NOW()
    FROM matched
    WHERE opc.instrument_key = matched.instrument_key
      AND opc.interval = matched.interval
      AND opc.time = matched.time
    """
)


def _time_windows(
    since: datetime,
    until: datetime,
    *,
    hours: int = _WINDOW_HOURS,
) -> list[tuple[datetime, datetime]]:
    """Split [since, until) into short UTC windows so every UPDATE remains bounded."""
    windows: list[tuple[datetime, datetime]] = []
    cursor = since
    step = timedelta(hours=max(1, hours))
    while cursor < until:
        win_end = min(cursor + step, until)
        windows.append((cursor, win_end))
        cursor = win_end
    return windows


def _daemon_should_run(now: datetime) -> bool:
    """Keep live enrichment out of the after-hours research/backfill lane."""
    try:
        from core.trading_calendar import trading_calendar

        if trading_calendar.is_exchange_open("NSE", now):
            return True
        local = now.astimezone(_IST)
        return (
            trading_calendar.has_exchange_session("NSE", local.date())
            and time(15, 30) <= local.time() <= _POST_CLOSE_GRACE_END
        )
    except Exception:  # noqa: BLE001
        return True


async def _enrich_interval(
    session: Any,
    *,
    interval: str,
    since: datetime,
    until: datetime,
    batch: int,
) -> int:
    """Enrich one interval across [since, until), one short UPDATE at a time.
    Returns the number of rows updated (committed per window to keep locks short)."""
    bar = INTERVAL_SECONDS[interval]
    total = 0
    for win_start, win_end in _time_windows(since, until):
        try:
            await session.execute(
                text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT_MS}ms'")
            )
            await session.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT_MS}ms'"))
            result = await session.execute(
                _ENRICH_SQL,
                {
                    "interval": interval,
                    "sources": list(_TARGET_SOURCES),
                    "underlyings": list(INDEX_SYMBOL_MAP.keys()),
                    "ocs_symbols": list(INDEX_SYMBOL_MAP.values()),
                    "since": win_start,
                    "until": win_end,
                    "batch": batch,
                    "slack": _WINDOW_SLACK_SECONDS,
                    "bar_plus_slack": bar + _WINDOW_SLACK_SECONDS,
                    "half_bar": bar / 2.0,
                },
            )
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            # Compressed or busy Timescale chunks can still reject an UPDATE.
            # Skip that narrow window and let the next cycle/manual backfill retry.
            await session.rollback()
            message = str(exc).splitlines()[0]
            logger.warning(
                f"[greeks_enrich] {interval} window "
                f"{win_start.isoformat()}..{win_end.isoformat()} skipped: {message}"
            )
            continue
        updated = result.rowcount or 0
        total += updated
        if updated >= batch:
            logger.warning(
                f"[greeks_enrich] {interval} {win_start.isoformat()} hit the {batch}-row "
                "safety cap; some bars may remain null — narrow the window or raise batch"
            )
    return total


async def enrich_option_greeks(
    *,
    since: datetime,
    until: datetime,
    intervals: tuple[str, ...] = DEFAULT_INTERVALS,
    batch: int = 50000,
    session: Optional[Any] = None,
) -> dict[str, int]:
    """Enrich greeks-null index option candles in [since, until) from chain
    snapshots. Returns per-interval update counts. Best-effort and idempotent:
    only fills NULL iv, so re-running never clobbers existing greeks."""
    counts: dict[str, int] = {}

    async def _run(sess: Any) -> None:
        for interval in intervals:
            if interval not in INTERVAL_SECONDS:
                logger.warning(f"[greeks_enrich] skipping unsupported interval {interval}")
                continue
            counts[interval] = await _enrich_interval(
                sess, interval=interval, since=since, until=until, batch=batch,
            )

    if session is not None:
        await _run(session)
    else:
        async with AsyncSessionLocal() as sess:
            await _run(sess)

    filled = sum(counts.values())
    if filled:
        logger.info(
            f"[greeks_enrich] filled {filled} greeks rows "
            f"({since.date()}..{until.date()}): "
            + ", ".join(f"{k}={v}" for k, v in counts.items() if v)
        )
    return counts


async def run_daemon(*, poll_minutes: int = 10, lookback_days: int = 3) -> None:
    """Periodically enrich recent greeks-null index option candles.

    The daemon is intentionally capped to the latest terminal session window.
    Older catch-up is a manual/backfill concern; the live worker's job is to keep
    charts current without starving websocket traffic.
    """
    logger.info(
        "[greeks_enrich] daemon starting "
        f"(poll={poll_minutes}m, lookback={lookback_days}d, "
        f"cap={_DAEMON_LOOKBACK_HOURS}h, batch={_DAEMON_BATCH})"
    )
    while True:
        try:
            now = datetime.now(timezone.utc)
            if not _daemon_should_run(now):
                await asyncio.sleep(max(60, poll_minutes * 60))
                continue
            requested_since = now - timedelta(days=lookback_days)
            capped_since = now - timedelta(hours=_DAEMON_LOOKBACK_HOURS)
            await enrich_option_greeks(
                since=max(requested_since, capped_since),
                until=now + timedelta(minutes=1),
                batch=_DAEMON_BATCH,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — enrichment is best-effort
            logger.exception(f"[greeks_enrich] cycle failed: {exc}")
        await asyncio.sleep(max(60, poll_minutes * 60))
