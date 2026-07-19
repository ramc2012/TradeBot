"""Post-close F&O stock spot sweep — the durable writer stock 30m never had.

Why this exists
---------------
Stock 30-minute spot had NO durable live writer. The only live producer is
``data/upstox_research_sync.py``, which calls ``_sync_spot_history(limit=25)``
(``upstox_research_sync.py:1535``) — 25 of ~211 F&O names per pass. Observed
coverage: 123 names on 07-16, **19** on 07-17. Every day that actually reached
~209 names got there via a MANUAL run of
``scripts/backfill_stock_intraday_spot.py``, so the hole re-opened the next
evening. Stock 3-minute is thinner still (22 names 07-15, ZERO 07-16, 67 on
07-17 — all ``live_tick``/aggregate, no durable writer at all).

This module closes that loop: one bounded sweep of the whole F&O stock universe,
scheduled ONCE after the NSE close by the paper supervisor.

Why post-close + BULK
---------------------
Two independent guarantees that it cannot compete with live decision traffic:

1. **Schedule** — the supervisor runs it with ``post_close_force_daily``, so the
   pass fires after 15:35 IST when no lane is making entry decisions. It is not
   an in-session job.
2. **Quota class** — every broker call is wrapped in ``CLASS_BULK``, which is
   hard-capped at 25% of the shared broker budget and is inadmissible while any
   ``CLASS_CRITICAL`` waiter is queued. So even if a manual/catch-up invocation
   ever overlaps the session, watchlist builds and held-position marks still win.

Design notes
------------
* Rows are written under the catalog's canonical ``spot_instrument_key`` so they
  merge with the existing ``upstox_spot``/``live_tick`` rows rather than forking
  a second Fyers-symbol keyspace.
* ``ON CONFLICT DO NOTHING`` — the sweep only FILLS HOLES. It can never overwrite
  a live-tick bar, and it is safe to re-run.
* Trailing ``days`` window (default 3 sessions) means a single missed evening —
  restart, dead token, holiday — self-heals on the next run with no manual step.
* One bad symbol never aborts the run; failures are counted and reported.
* Fully bounded: symbol cap, per-call pacing, and a caller-supplied deadline.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from time import monotonic
from typing import Any, Optional

from loguru import logger
from sqlalchemy import text

from brokers.rate_limiter import CLASS_BULK, broker_class
from core.config import settings
from db.database import AsyncSessionLocal

SOURCE = "fyers"

# app interval -> Fyers resolution
_RESOLUTION = {
    "1minute": "1",
    "3minute": "3",
    "5minute": "5",
    "15minute": "15",
    "30minute": "30",
}


def _parse_intervals(raw: str | None) -> list[str]:
    out: list[str] = []
    for part in str(raw or "").split(","):
        interval = part.strip()
        if interval and interval in _RESOLUTION and interval not in out:
            out.append(interval)
    return out or ["30minute"]


async def _stock_universe(limit: int) -> list[tuple[str, str, str]]:
    """(symbol, canonical instrument_key, fyers symbol) for every keyed F&O stock."""
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text(
            """
            SELECT symbol, spot_instrument_key
            FROM fo_underlying_catalog
            WHERE kind = 'STOCK'
              AND spot_instrument_key IS NOT NULL
              AND spot_instrument_key <> ''
            ORDER BY symbol
            """
        ))).fetchall()

    universe: list[tuple[str, str, str]] = []
    for symbol, raw_key in rows:
        key = str(raw_key or "").strip()
        if not key:
            continue
        fyers_symbol = key if key.startswith(("NSE:", "BSE:")) else f"NSE:{symbol}-EQ"
        universe.append((str(symbol), key, fyers_symbol))
    if limit and limit > 0:
        universe = universe[:limit]
    return universe


async def _upsert(symbol: str, instrument_key: str, interval: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    async with AsyncSessionLocal() as session:
        await session.execute(text(
            """
            INSERT INTO underlying_spot_candles
                (time, instrument_key, underlying, interval,
                 open, high, low, close, volume, oi, source)
            VALUES
                (:time, :instrument_key, :underlying, :interval,
                 :open, :high, :low, :close, :volume, 0, :source)
            ON CONFLICT (instrument_key, interval, "time") DO NOTHING
            """
        ), [
            {
                "time": r["time"],
                "instrument_key": instrument_key,
                "underlying": symbol,
                "interval": interval,
                "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
                "volume": r["volume"], "source": SOURCE,
            }
            for r in rows
        ])
        await session.commit()
    return len(rows)


def _normalize(raw: Any) -> list[dict]:
    rows: list[dict] = []
    for r in raw or []:
        try:
            rows.append({
                "time": datetime.fromisoformat(str(r["time"]).replace("Z", "+00:00")),
                "open": float(r["open"]), "high": float(r["high"]),
                "low": float(r["low"]), "close": float(r["close"]),
                "volume": int(r.get("volume") or 0),
            })
        except (TypeError, ValueError, KeyError):
            continue
    return rows


async def _coverage(interval: str, frm: date, to: date) -> int:
    """Distinct F&O stock names present at ``interval`` in the window."""
    async with AsyncSessionLocal() as session:
        row = (await session.execute(text(
            """
            SELECT COUNT(DISTINCT s.underlying) AS names
            FROM underlying_spot_candles s
            JOIN fo_underlying_catalog c
              ON c.symbol = s.underlying AND c.kind = 'STOCK'
            WHERE s.interval = :interval
              AND s.time >= :frm AND s.time < :to
            """
        ), {"interval": interval, "frm": frm, "to": to + timedelta(days=1)})).one()
    return int(row.names or 0)


async def sweep_stock_spot(
    *,
    intervals: Optional[list[str]] = None,
    days: Optional[int] = None,
    max_symbols: Optional[int] = None,
    deadline_seconds: float = 900.0,
) -> dict[str, Any]:
    """Sweep the F&O stock universe for intraday spot. Bounded, idempotent.

    Returns a summary dict for the supervisor's audit trail. Never raises: a
    broker/DB failure degrades to a reported error, because a data-maintenance
    job must not be able to take the supervisor down.
    """
    if not bool(getattr(settings, "STOCK_SPOT_SWEEP_ENABLED", True)):
        return {"status": "disabled"}

    intervals = intervals or _parse_intervals(getattr(settings, "STOCK_SPOT_SWEEP_INTERVALS", "30minute"))
    days = int(days if days is not None else getattr(settings, "STOCK_SPOT_SWEEP_DAYS", 3))
    max_symbols = int(
        max_symbols if max_symbols is not None
        else getattr(settings, "STOCK_SPOT_SWEEP_MAX_SYMBOLS", 0)
    )
    pace = float(getattr(settings, "STOCK_SPOT_SWEEP_SLEEP_SECONDS", 0.35))

    try:
        from api.routers.auth import ensure_fyers_session, get_active_adapter

        adapter = get_active_adapter("fyers")
        if adapter is None:
            await ensure_fyers_session(force_validate=False)
            adapter = get_active_adapter("fyers")
        if adapter is None:
            logger.warning("[stock-spot-sweep] no Fyers session — skipping this pass")
            return {"status": "skipped_no_broker"}

        universe = await _stock_universe(max_symbols)
        if not universe:
            return {"status": "skipped_empty_universe"}

        to_date = date.today()
        from_date = to_date - timedelta(days=max(days, 1) - 1)
        started = monotonic()

        summary: dict[str, Any] = {
            "status": "ok",
            "from": from_date.isoformat(),
            "to": to_date.isoformat(),
            "symbols": len(universe),
            "intervals": {},
        }

        for interval in intervals:
            resolution = _RESOLUTION[interval]
            before = await _coverage(interval, from_date, to_date)
            ok = failed = stored = 0
            budget_hit = False

            for symbol, key, fyers_symbol in universe:
                if monotonic() - started >= deadline_seconds:
                    budget_hit = True
                    logger.warning(
                        f"[stock-spot-sweep] deadline hit during {interval} "
                        f"after {ok + failed}/{len(universe)} symbols"
                    )
                    break
                try:
                    # BULK: hard-capped at 25% of the shared broker budget and
                    # inadmissible while any CRITICAL waiter queues, so this can
                    # never starve live decision traffic.
                    with broker_class(CLASS_BULK):
                        raw = await adapter.get_historical_candles(
                            symbol=fyers_symbol,
                            resolution=resolution,
                            range_from=from_date.isoformat(),
                            range_to=to_date.isoformat(),
                        )
                    stored += await _upsert(symbol, key, interval, _normalize(raw))
                    ok += 1
                except Exception as exc:  # noqa: BLE001 — one bad symbol never aborts
                    failed += 1
                    logger.debug(f"[stock-spot-sweep] {symbol} ({fyers_symbol}) failed: {exc}")
                await asyncio.sleep(pace)

            after = await _coverage(interval, from_date, to_date)
            summary["intervals"][interval] = {
                "names_before": before, "names_after": after,
                "symbols_ok": ok, "symbols_failed": failed,
                "rows_fetched": stored, "budget_hit": budget_hit,
            }
            logger.info(
                f"[stock-spot-sweep] {interval} {from_date}..{to_date}: "
                f"names {before} -> {after} | ok={ok} failed={failed} rows={stored}"
                + (" | DEADLINE" if budget_hit else "")
            )

        summary["elapsed_s"] = round(monotonic() - started, 1)
        return summary

    except Exception as exc:  # noqa: BLE001 — data maintenance never breaks the supervisor
        logger.warning(f"[stock-spot-sweep] pass failed: {type(exc).__name__}: {exc}")
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
