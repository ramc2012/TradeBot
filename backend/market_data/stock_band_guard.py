"""Poison-proof magnitude guard for NSE **equity** spot ingestion (F1).

Why this exists
---------------
``index_band_guard`` covers the 12 index app-symbols. Stocks fell through it
with structural checks only — and structural checks cannot catch the documented
failure mode, because the contamination is a *complete, internally coherent
frame belonging to another instrument* delivered under the wrong symbol label
(Fyers WS ``topic_id``→symbol misresolution). Observed 2026-07-17 under
``NSE:BHEL-EQ`` (true level ~443):

    ltp 254.50  vol    3660   <- foreign instrument #1
    ltp 13847   vol   12887   <- foreign instrument #2
    ltp 949.30  vol  201489   <- foreign instrument #3

Every field of those frames is self-consistent, so only a *magnitude* test
against an anchor that does not come from the live tape can reject them.

The anchor
----------
Per equity underlying, the **most recent non-``live_tick`` close** from
``underlying_spot_candles`` (i.e. broker-history rows: ``fyers`` /
``upstox_spot`` / ``timescaledb_spot_1minute``). It is therefore:

* external to the tape being policed — no run of bad prints can move it, unlike
  the self-referential rolling medians elsewhere in this codebase which invert
  once contamination exceeds 50% of their window;
* never derived from the rows it judges.

A price outside ``±REL_TOL`` of that anchor is **dropped** — never clamped,
never interpolated, never replaced with a fabricated value. Failing toward NO
DATA is the whole point: a missing bar is recoverable by backfill, a wrong bar
is not.

Honest limits
-------------
A magnitude band cannot catch same-decade contamination (the BHEL/366.75 frame
above sits 17% from anchor and passes). That class is the source-level Fyers
subscription fix, deliberately deferred. This is the safety net beneath it.

Symbols with **no** anchor row in the window are NOT guarded (fail-open). We
cannot judge a price we have no external reference for, and rejecting every
tick for a newly listed name would silently blind a lane. Unanchored names are
logged so the gap is a visible fact.

The hot path (``is_guarded`` / ``passes`` / ``check_ohlc``) is pure and sync.
Anchor seeding is async, bounded (time + name + source), and best-effort — a DB
failure leaves the guard fail-open, never blocks ingest.
"""
from __future__ import annotations

import time as _time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from loguru import logger

# Reject an equity print deviating more than this fraction from its anchor.
# NSE per-scrip circuit limits top out at ±20%, and the anchor is at most a few
# sessions old, so ±30% cannot false-reject a real move while still rejecting
# every 2x+ cross-instrument frame.
REL_TOL = 0.30

# Anchor lookback (calendar days) and refresh cadence.
_ANCHOR_LOOKBACK_DAYS = 5
_REFRESH_TTL_SECONDS = 6 * 60 * 60
# When unseeded names are pending, refresh sooner than the TTL (the subscription
# set grows through the session as the JIT watchlist fills in).
_PENDING_MIN_INTERVAL_SECONDS = 30.0

# Broker-history sources: everything except the tape we are policing.
_ANCHOR_SOURCES = ("fyers", "upstox_spot", "upstox", "timescaledb_spot_1minute")

_PREFIX = "NSE:"
_SUFFIX = "-EQ"

# Names whose last anchor lookup found nothing, with the monotonic time of that
# attempt — so a name with no broker history is not re-queried every cycle.
_NO_ANCHOR_RETRY_SECONDS = 600.0

_ref_close: dict[str, float] = {}
_pending: set[str] = set()
_no_anchor_at: dict[str, float] = {}
_unanchored_logged: set[str] = set()
_last_refresh_at: float = 0.0


def is_equity_symbol(symbol: str) -> bool:
    return bool(symbol) and symbol.startswith(_PREFIX) and symbol.endswith(_SUFFIX)


def underlying_for_symbol(symbol: str) -> Optional[str]:
    if not is_equity_symbol(symbol):
        return None
    name = symbol[len(_PREFIX):-len(_SUFFIX)].strip().upper()
    return name or None


def app_symbol_for_underlying(underlying: str) -> Optional[str]:
    """``RELIANCE`` → ``NSE:RELIANCE-EQ`` (for callers keyed by the DB column)."""
    if not underlying:
        return None
    name = str(underlying).strip().upper()
    if not name:
        return None
    return f"{_PREFIX}{name}{_SUFFIX}"


def note_symbol(symbol: str) -> None:
    """Register an equity symbol seen on the tape so the next refresh anchors it."""
    name = underlying_for_symbol(symbol)
    if not name or name in _ref_close:
        return
    attempted_at = _no_anchor_at.get(name)
    if attempted_at is not None and _time.monotonic() - attempted_at < _NO_ANCHOR_RETRY_SECONDS:
        return
    _pending.add(name)


def set_reference_close(underlying: str, close: float) -> None:
    name = str(underlying or "").strip().upper()
    if not name:
        return
    try:
        value = float(close)
    except (TypeError, ValueError):
        return
    if value > 0:
        _ref_close[name] = value
        _pending.discard(name)


def clear_reference_closes() -> None:
    """Drop all anchors and pending state (test isolation)."""
    _ref_close.clear()
    _pending.clear()
    _no_anchor_at.clear()
    _unanchored_logged.clear()
    _reject_counts.clear()
    _reject_logged_at.clear()
    global _last_refresh_at
    _last_refresh_at = 0.0


def is_guarded(symbol: str) -> bool:
    """True only for an equity symbol that has an external anchor."""
    name = underlying_for_symbol(symbol)
    return bool(name) and name in _ref_close


def passes(symbol: str, price: Any) -> bool:
    """True if ``price`` is plausible for ``symbol``. Unanchored symbols pass."""
    name = underlying_for_symbol(symbol)
    if not name:
        return True
    ref = _ref_close.get(name)
    if not ref or ref <= 0:
        if name not in _unanchored_logged:
            _unanchored_logged.add(name)
            logger.info(
                "[stock_band_guard] no external anchor for {name} — equity magnitude "
                "guard is fail-open for this name until a broker-history close exists",
                name=name,
            )
        return True
    try:
        px = float(price)
    except (TypeError, ValueError):
        return False
    if px <= 0:
        return False
    return abs(px - ref) / ref <= REL_TOL


def check_ohlc(underlying: str, open_: Any, high: Any, low: Any, close: Any) -> bool:
    """True only if every non-null O/H/L/C of an equity bar passes the band.

    Keyed by the DB ``underlying`` column. One contaminating tick inside a
    minute corrupts that bar's high or low even when its close is legit, so the
    candle-level guard must test all four legs.
    """
    symbol = app_symbol_for_underlying(underlying)
    if not symbol or not is_guarded(symbol):
        return True
    for value in (open_, high, low, close):
        if value in (None, ""):
            continue
        try:
            px = float(value)
        except (TypeError, ValueError):
            return False
        if px <= 0:
            continue
        if not passes(symbol, px):
            return False
    return True


_reject_counts: dict[str, int] = {}
_reject_logged_at: dict[str, float] = {}
_REJECT_LOG_INTERVAL_SECONDS = 60.0


def note_reject(symbol: str) -> tuple[bool, int]:
    """Count a rejection; return ``(should_log_loudly, running_count)``.

    A contaminating burst is thousands of ticks — the rejection must be loud but
    must not become the log storm that takes the event loop down. One WARNING
    per symbol per minute, with the running count so the true volume is visible.
    """
    name = underlying_for_symbol(symbol) or symbol
    count = _reject_counts.get(name, 0) + 1
    _reject_counts[name] = count
    now = _time.monotonic()
    last = _reject_logged_at.get(name)
    if last is None or now - last >= _REJECT_LOG_INTERVAL_SECONDS:
        _reject_logged_at[name] = now
        return True, count
    return False, count


def reject_counts() -> dict[str, int]:
    """Per-name rejection totals (telemetry / tests)."""
    return dict(_reject_counts)


def _cutoff_literal(days: int) -> str:
    """UTC cutoff rendered as a SQL literal.

    Deliberately inlined rather than bound as a parameter: ``underlying_spot_candles``
    is a ~1,300-chunk hypertable, and TimescaleDB only performs *plan-time* chunk
    exclusion against a constant. Measured on this database, the same query costs
    **1.9 s planning** with ``NOW() - INTERVAL`` versus **6.8 ms** with a literal —
    a 280x difference paid on every call. The value is generated here from the
    system clock and never touches user input, so there is no injection surface;
    the format is fixed-width and contains no quotable characters.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
    return cutoff.strftime("%Y-%m-%d %H:%M:%S+00")


async def refresh_reference_closes(session_factory: Any = None) -> int:
    """Seed anchors for pending equity names from broker-history closes.

    Bounded three ways per the PG constraint: by **name** (only symbols actually
    seen on the tape), by **time** (5 days), and by **source** (never
    ``live_tick``). Returns the number of anchors seeded.
    """
    global _last_refresh_at
    _last_refresh_at = _time.monotonic()
    names = sorted(_pending)
    if not names:
        return 0
    if session_factory is None:
        from db.database import AsyncSessionLocal as session_factory  # type: ignore
    from sqlalchemy import text

    seeded = 0
    try:
        async with session_factory() as session:
            result = await session.execute(
                text(
                    f"""
                    SELECT DISTINCT ON (underlying) underlying, close
                    FROM underlying_spot_candles
                    WHERE underlying = ANY(:names)
                      AND interval IN ('1minute', '30minute')
                      AND source = ANY(:sources)
                      AND close > 0
                      AND time >= TIMESTAMPTZ '{_cutoff_literal(_ANCHOR_LOOKBACK_DAYS)}'
                    ORDER BY underlying, time DESC
                    """
                ),
                {
                    "names": names,
                    "sources": list(_ANCHOR_SOURCES),
                },
            )
            for row in result.fetchall():
                before = len(_ref_close)
                set_reference_close(row.underlying, row.close)
                if len(_ref_close) != before:
                    seeded += 1
    except Exception as exc:  # noqa: BLE001 — never block ingest on a DB blip
        logger.debug(f"[stock_band_guard] anchor refresh skipped: {exc}")
        return seeded
    now = _time.monotonic()
    for name in names:
        if name not in _ref_close:
            _no_anchor_at[name] = now
            _pending.discard(name)
    if seeded:
        logger.info(f"[stock_band_guard] seeded {seeded} equity anchor closes")
    return seeded


async def maybe_refresh_reference_closes(session_factory: Any = None) -> None:
    """TTL-gated refresh — safe to call from the flush loop every few seconds."""
    elapsed = _time.monotonic() - _last_refresh_at
    if _pending:
        if elapsed < _PENDING_MIN_INTERVAL_SECONDS:
            return
    elif elapsed < _REFRESH_TTL_SECONDS:
        return
    await refresh_reference_closes(session_factory)
