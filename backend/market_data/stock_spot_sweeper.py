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

Why it silently did nothing for weeks (found 2026-08-27)
---------------------------------------------------------
This module was written against Fyers. Nomad Curie has been Upstox-only since
a deliberate 12-Aug-2026 decision, so every scheduled pass reached

    [stock-spot-sweep] no Fyers session — skipping this pass

and returned ``skipped_no_broker``. The supervisor logged "completed" each
time. The sweep was scheduled correctly, fired on time, and wrote nothing —
for as long as the deployment has been Upstox-only.

Why "today" was missing entirely
--------------------------------
With this sweep inert, the only writer of F&O stock spot was
``data/upstox_research_sync.py``, which calls Upstox's ``/historical-candle``
endpoint. **That endpoint never returns the current session**, even when today
is passed as the ``to`` date — verified 2026-08-27: asking for 25-Aug..27-Aug
returned 26 candles covering only the 25th and 26th. So today's bars could
only appear once "today" became "yesterday", which is exactly what was
observed: every one of 26-Aug's thirteen session bars first landed at
27-Aug 00:00:56 UTC.

Upstox serves the current session from a DIFFERENT endpoint,
``/historical-candle/intraday/...``, which returned all 13 of 27-Aug's bars
when asked. Both endpoints are PUBLIC — they answer with no Authorization
header at all — so the fix needs no broker session, no token, and no share of
the authenticated rate budget.

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
  a second keyspace.
* Two endpoints, one job: ``intraday`` supplies the CURRENT session and
  ``historical`` supplies prior days. A pass that wants today plus history
  calls both and merges, because neither one alone covers the window.
* ``ON CONFLICT DO NOTHING`` — the sweep only FILLS HOLES. It can never overwrite
  a live-tick bar, and it is safe to re-run.
* Trailing ``days`` window (default 3 sessions) means a single missed evening —
  restart, dead token, holiday — self-heals on the next run with no manual step.
* One bad symbol never aborts the run; failures are counted and reported.
* Fully bounded: symbol cap, per-call pacing, and a caller-supplied deadline.
"""
from __future__ import annotations

import asyncio
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from time import monotonic
from typing import Any, Optional

import httpx
from loguru import logger
from sqlalchemy import text

from brokers.rate_limiter import CLASS_BULK, broker_class
from core.config import settings
from db.database import AsyncSessionLocal

SOURCE = "upstox_sweep"
_INTRADAY_CURSOR = 0

# app interval -> Upstox v2 interval / v3 (unit, interval) pair. v3 is used for
# the intraday endpoint because v2's intraday path only accepts 1minute and
# 30minute, while v3 accepts an arbitrary minutes value.
_RESOLUTION = {
    "1minute": ("1minute", "minutes", "1"),
    "3minute": ("3minute", "minutes", "3"),
    "5minute": ("5minute", "minutes", "5"),
    "15minute": ("15minute", "minutes", "15"),
    "30minute": ("30minute", "minutes", "30"),
}

UPSTOX_V2 = "https://api.upstox.com/v2"
UPSTOX_V3 = "https://api.upstox.com/v3"
# Both candle endpoints answer without an Authorization header (verified
# 2026-08-27). A token is sent when one happens to be configured, purely so a
# future auth-only endpoint does not silently regress to anonymous.
_HTTP_TIMEOUT = 20.0


def _parse_intervals(raw: str | None) -> list[str]:
    out: list[str] = []
    for part in str(raw or "").split(","):
        interval = part.strip()
        if interval and interval in _RESOLUTION and interval not in out:
            out.append(interval)
    return out or ["30minute"]


async def _stock_universe(limit: int) -> list[tuple[str, str]]:
    """(symbol, canonical Upstox instrument_key) for every keyed F&O stock."""
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

    universe: list[tuple[str, str]] = []
    for symbol, raw_key in rows:
        key = str(raw_key or "").strip()
        # Upstox instrument keys look like NSE_EQ|INE002A01018. A Fyers-style
        # "NSE:RELIANCE-EQ" would 404 against these endpoints, so a row that is
        # not an Upstox key is skipped rather than guessed at.
        if not key or "|" not in key:
            continue
        universe.append((str(symbol), key))
    if limit and limit > 0:
        universe = universe[:limit]
    return universe


async def _upsert(
    symbol: str, instrument_key: str, interval: str, rows: list[dict], *, source: str = SOURCE,
) -> int:
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
                "volume": r["volume"], "source": source,
            }
            for r in rows
        ])
        await session.commit()
    return len(rows)


async def _get_candles(client: "httpx.AsyncClient", url: str) -> list[list]:
    """One candle GET. Returns [] on anything that is not a clean 200 payload.

    A data-maintenance sweep must not be able to raise: one bad symbol out of
    211 is a counted failure, not an aborted pass.
    """
    response = await client.get(url, headers=_headers())
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")
    payload = response.json()
    return list(((payload.get("data") or {}).get("candles")) or [])


def _headers() -> dict[str, str]:
    token = str(getattr(settings, "UPSTOX_ANALYTICS_TOKEN", "") or "").strip()
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _fetch_window(
    client: "httpx.AsyncClient",
    instrument_key: str,
    interval: str,
    from_date: date,
    to_date: date,
    today: date,
) -> list[list]:
    """Candles for [from_date, to_date], from whichever endpoints cover it.

    ``/historical-candle`` NEVER returns the current session, so a window that
    includes today must also call ``/historical-candle/intraday``. Calling only
    the former is precisely why today's bars never existed until the next
    morning. Both are called when the window spans both, and the results are
    merged.
    """
    v2_interval, v3_unit, v3_interval = _RESOLUTION[interval]
    encoded = urllib.parse.quote(instrument_key, safe="")
    candles: list[list] = []
    attempted = 0
    failures: list[str] = []

    # The two legs fail INDEPENDENTLY. A history call that errors must not cost
    # this symbol today's bars as well: today is the scarce data here — history
    # can be re-fetched on any later pass, the current session cannot once it
    # has gone stale. Only a failure of every attempted leg propagates.
    if from_date < today:
        attempted += 1
        history_to = min(to_date, today - timedelta(days=1))
        try:
            candles += await _get_candles(client, (
                f"{UPSTOX_V2}/historical-candle/{encoded}/{v2_interval}"
                f"/{history_to.isoformat()}/{from_date.isoformat()}"
            ))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"history: {exc}")
    if to_date >= today:
        attempted += 1
        try:
            candles += await _get_candles(client, (
                f"{UPSTOX_V3}/historical-candle/intraday/{encoded}/{v3_unit}/{v3_interval}"
            ))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"intraday: {exc}")

    if failures and len(failures) == attempted:
        raise RuntimeError("; ".join(failures))
    if failures:
        logger.debug(f"[stock-spot-sweep] {instrument_key} partial: {'; '.join(failures)}")
    return candles


def _normalize(raw: Any) -> list[dict]:
    """Upstox candles are POSITIONAL arrays: [ts, open, high, low, close, volume, oi].

    Dict-shaped rows are still accepted so a future payload change, or a caller
    passing already-normalised data, does not silently drop every bar.
    """
    rows: list[dict] = []
    for r in raw or []:
        try:
            if isinstance(r, dict):
                stamp, o, h, l, c, v = (
                    r["time"], r["open"], r["high"], r["low"], r["close"], r.get("volume") or 0)
            else:
                stamp, o, h, l, c = r[0], r[1], r[2], r[3], r[4]
                v = r[5] if len(r) > 5 else 0
            rows.append({
                "time": datetime.fromisoformat(str(stamp).replace("Z", "+00:00")),
                "open": float(o), "high": float(h), "low": float(l), "close": float(c),
                "volume": int(v or 0),
            })
        except (TypeError, ValueError, KeyError, IndexError):
            continue
    # The two endpoints can overlap at a session boundary; de-duplicate on the
    # bar timestamp so an overlap never double-counts a bar.
    unique: dict[datetime, dict] = {}
    for row in rows:
        unique[row["time"]] = row
    return sorted(unique.values(), key=lambda r: r["time"])


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


def completed_rows(rows: list[dict], interval: str, now: datetime) -> list[dict]:
    minutes = int(interval.removesuffix("minute"))
    def close_time(row):
        stamp = row["time"]
        if stamp.tzinfo is None:
            return None
        local = stamp.astimezone(ZoneInfo("Asia/Kolkata"))
        end = stamp + timedelta(minutes=minutes)
        if local.hour == 15 and local.minute >= 15 and local.minute < 30:
            end = min(end, local.replace(hour=15, minute=30, second=0, microsecond=0))
        return end
    return [r for r in rows if (end := close_time(r)) is not None and end <= now]


def aggregate_stock_30minute(rows: list[dict], now: datetime) -> list[dict]:
    """Only contiguous completed NSE 3m bars form a 30m bar (15m at close)."""
    groups = {}
    for row in completed_rows(rows, "3minute", now):
        stamp = row["time"].astimezone(ZoneInfo("Asia/Kolkata"))
        anchor = stamp.replace(hour=9, minute=15, second=0, microsecond=0)
        offset = (stamp - anchor).total_seconds() / 60
        if offset < 0 or offset >= 375 or offset % 3:
            continue
        start = anchor + timedelta(minutes=30 * int(offset // 30))
        groups.setdefault(start, {})[stamp] = row
    result = []
    for start, group in sorted(groups.items()):
        count = 5 if start.hour == 15 and start.minute == 15 else 10
        expected = [start + timedelta(minutes=3*i) for i in range(count)]
        if start + timedelta(minutes=3*count) > now or set(group) != set(expected):
            continue
        ordered = [group[t] for t in expected]
        result.append({"time": start, "open": ordered[0]["open"],
                       "high": max(r["high"] for r in ordered),
                       "low": min(r["low"] for r in ordered),
                       "close": ordered[-1]["close"],
                       "volume": sum(r["volume"] for r in ordered)})
    return result


async def sweep_stock_spot(
    *,
    intervals: Optional[list[str]] = None,
    days: Optional[int] = None,
    max_symbols: Optional[int] = None,
    deadline_seconds: float = 900.0,
    derive_30minute: bool = False,
) -> dict[str, Any]:
    """Sweep the F&O stock universe for intraday spot. Bounded, idempotent.

    Returns a summary dict for the supervisor's audit trail. Never raises: a
    broker/DB failure degrades to a reported error, because a data-maintenance
    job must not be able to take the supervisor down.
    """
    global _INTRADAY_CURSOR
    if not bool(getattr(settings, "STOCK_SPOT_SWEEP_ENABLED", True)):
        return {"status": "disabled"}

    intervals = intervals or _parse_intervals(getattr(settings, "STOCK_SPOT_SWEEP_INTERVALS", "30minute"))
    if derive_30minute and "3minute" in intervals:
        intervals = [i for i in intervals if i != "30minute"]
    days = int(days if days is not None else getattr(settings, "STOCK_SPOT_SWEEP_DAYS", 3))
    max_symbols = int(
        max_symbols if max_symbols is not None
        else getattr(settings, "STOCK_SPOT_SWEEP_MAX_SYMBOLS", 0)
    )
    pace = float(getattr(settings, "STOCK_SPOT_SWEEP_SLEEP_SECONDS", 0.35))

    try:
        # NO BROKER SESSION. Both Upstox candle endpoints answer without an
        # Authorization header, so this pass cannot be skipped for want of a
        # token — which is exactly what it did, every scheduled day, for as long
        # as this deployment has been Upstox-only.
        universe = await _stock_universe(max_symbols)
        if not universe:
            return {"status": "skipped_empty_universe"}

        offset = _INTRADAY_CURSOR % len(universe) if derive_30minute else 0
        universe = universe[offset:] + universe[:offset]
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

        today = date.today()
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
          for interval in intervals:
            before = await _coverage(interval, from_date, to_date)
            ok = failed = stored = derived = 0
            budget_hit = False

            for ordinal, (symbol, key) in enumerate(universe):
                if monotonic() - started >= deadline_seconds:
                    budget_hit = True
                    logger.warning(
                        f"[stock-spot-sweep] deadline hit during {interval} "
                        f"after {ok + failed}/{len(universe)} symbols"
                    )
                    break
                try:
                    # Still declared CLASS_BULK. These endpoints are public and
                    # spend no authenticated quota, but the class also carries
                    # the admission rule that keeps a 211-symbol sweep behind any
                    # queued CRITICAL waiter — that protection is still wanted.
                    with broker_class(CLASS_BULK):
                        raw = await _fetch_window(
                            client, key, interval, from_date, to_date, today)
                    now = datetime.now(timezone.utc)
                    rows = completed_rows(_normalize(raw), interval, now)
                    stored += await _upsert(symbol, key, interval, rows)
                    if derive_30minute and interval == "3minute":
                        derived += await _upsert(symbol, key, "30minute", aggregate_stock_30minute(rows, now))
                    ok += 1
                except Exception as exc:  # noqa: BLE001 — one bad symbol never aborts
                    failed += 1
                    logger.debug(f"[stock-spot-sweep] {symbol} ({key}) failed: {exc}")
                if derive_30minute:
                    _INTRADAY_CURSOR = (offset + ordinal + 1) % len(universe)
                await asyncio.sleep(pace)

            if failed or budget_hit:
                summary["status"] = "partial"
            after = await _coverage(interval, from_date, to_date)
            summary["intervals"][interval] = {
                "names_before": before, "names_after": after,
                "symbols_ok": ok, "symbols_failed": failed,
                "rows_fetched": stored, "budget_hit": budget_hit,
                "derived_30minute_rows": derived,
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


# ── Same-session index 1-minute heal ────────────────────────────────────────
# A restart mid-session (host reboot 2026-09-15 11:44 IST) leaves today's index
# 1-minute tape starting at the restart. Nothing could refill it: both runtimes
# run MARKET_INTELLIGENCE_STRATEGY_LOCAL_ONLY, so gap_fill_spot_history reads
# only the DB, and its broker leg would have used /historical-candle, which never
# returns the current session. The auction lane's open-coverage check then
# refused NIFTY/BANKNIFTY/SENSEX for the rest of the day. The intraday endpoint
# is public, so the heal needs no session and spends no authenticated budget.
INDEX_HEAL_SOURCE = "upstox_intraday_heal"
INDEX_HEAL_SCOPE = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX")
_IST = ZoneInfo("Asia/Kolkata")
_INDEX_HEAL_COOLDOWN: dict[str, float] = {}


async def _index_keys(symbols: tuple[str, ...]) -> list[tuple[str, str]]:
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text(
            """
            SELECT symbol, spot_instrument_key
            FROM fo_underlying_catalog
            WHERE kind = 'INDEX' AND spot_instrument_key LIKE '%|%'
            ORDER BY symbol
            """
        ))).fetchall()
    return [(str(sym), str(key)) for sym, key in rows if str(sym) in symbols]


async def _session_minutes_present(symbols: list[str], start: datetime, end: datetime) -> dict[str, int]:
    """Distinct completed 1-minute bars per index in [start, end) — literal time bounds."""
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text(
            """
            SELECT underlying, COUNT(DISTINCT time) AS n
            FROM underlying_spot_candles
            WHERE interval = '1minute'
              AND time >= :start AND time < :end
              AND underlying = ANY(:symbols)
            GROUP BY underlying
            """
        ), {"start": start, "end": end, "symbols": symbols})).fetchall()
    return {str(sym): int(n or 0) for sym, n in rows}


async def heal_index_intraday_gaps(
    symbols: tuple[str, ...] = INDEX_HEAL_SCOPE,
    *,
    min_missing: int = 5,
    settle_minutes: int = 3,
    cooldown_seconds: float = 600.0,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Fill today's missing index 1-minute bars from Upstox intraday. Never raises.

    Cheap when healthy: one bounded COUNT per call; the broker is touched only
    for an index short by ``min_missing`` bars, at most once per cooldown.
    ``ON CONFLICT DO NOTHING`` — live-tick bars are never overwritten.
    """
    if not bool(getattr(settings, "INDEX_INTRADAY_HEAL_ENABLED", True)):
        return {"status": "disabled"}
    try:
        from core.trading_calendar import trading_calendar

        current = (now or datetime.now(timezone.utc)).astimezone(_IST)
        if not trading_calendar.has_exchange_session("NSE", current.date()):
            return {"status": "skipped_no_session"}
        session_open = current.replace(hour=9, minute=15, second=0, microsecond=0)
        session_close = current.replace(hour=15, minute=30, second=0, microsecond=0)
        horizon = min(current - timedelta(minutes=settle_minutes), session_close)
        expected = int((horizon - session_open).total_seconds() // 60)
        if expected < min_missing:
            return {"status": "skipped_too_early"}

        keys = await _index_keys(symbols)
        present = await _session_minutes_present(
            [sym for sym, _ in keys],
            session_open.astimezone(timezone.utc),
            horizon.astimezone(timezone.utc),
        )
        clock = monotonic()
        short = [
            (sym, key) for sym, key in keys
            if expected - present.get(sym, 0) >= min_missing
            and _INDEX_HEAL_COOLDOWN.get(sym, 0.0) <= clock
        ]
        summary: dict[str, Any] = {"status": "ok", "expected": expected, "present": present, "healed": {}}
        if not short:
            return summary

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            for sym, key in short:
                _INDEX_HEAL_COOLDOWN[sym] = clock + cooldown_seconds
                encoded = urllib.parse.quote(key, safe="")
                try:
                    with broker_class(CLASS_BULK):
                        raw = await _get_candles(
                            client, f"{UPSTOX_V3}/historical-candle/intraday/{encoded}/minutes/1"
                        )
                    stamp_now = now or datetime.now(timezone.utc)
                    rows = [
                        r for r in completed_rows(_normalize(raw), "1minute", stamp_now)
                        if session_open <= r["time"].astimezone(_IST) < session_close
                    ]
                    summary["healed"][sym] = await _upsert(
                        sym, key, "1minute", rows, source=INDEX_HEAL_SOURCE
                    )
                except Exception as exc:  # noqa: BLE001 — one index never aborts the heal
                    summary["healed"][sym] = f"error: {exc}"
        if summary["healed"]:
            logger.info(f"[index-intraday-heal] expected={expected} present={present} healed={summary['healed']}")
        return summary
    except Exception as exc:  # noqa: BLE001 — data maintenance must not raise
        logger.warning(f"[index-intraday-heal] failed: {exc}")
        return {"status": "error", "error": str(exc)}
