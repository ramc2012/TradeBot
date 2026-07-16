"""Absolute per-index magnitude guard for spot ingestion (WS-0.1b).

Why this exists
---------------
The documented cross-symbol contamination (Fyers WS ``topic_id``→symbol
misresolution on reconnect: a complete BANKNIFTY frame at ~57.8k written under
``NSE:NIFTY50-INDEX``, MIDCPNIFTY 14.8k under NIFTY, REALTY 908 under every index)
poisons index spot. The previous defence was a *self-referential rolling median*
in ``LiveCandleStore._validate_tick`` — and a clustered burst of contaminating
prints could win a majority of the 30-tick window, flip the median, and INVERT
the guard (legit prints rejected, contaminants admitted). A median that a
>50%-contaminated burst can drag cannot police that same burst.

This module replaces that with a reference that **does not come from the live
tape**, so no run of bad prints can ever move it:

1. A static, generous **absolute band** per index (``_ABS_BANDS``) — a born-safe
   floor that is correct from tick one (no warm-up on garbage) and rejects any
   value ~2x+ away (BANKNIFTY↔NIFTY, SENSEX, REALTY 908, base-metal swaps) even
   before any prior-session reference is available. Bounds are ~0.5x..2.1x
   current levels: comfortably outside a circuit-halt move (±20%) so a real
   index can never be false-rejected, yet tight enough to catch a foreign
   instrument's level.
2. An optional tighter **prior-session-close band** (``±REL_TOL``, default 20%)
   applied when a reference close has been seeded from history. This catches the
   20%+ cross-index contamination that sits inside the wide absolute band
   (FMCG/ENERGY/MIDCPNIFTY levels landing on NIFTY). The reference is the *median*
   close over recent completed sessions, so it is itself robust to residual
   contamination.

Same-decade-magnitude contamination (e.g. NIFTY↔FINNIFTY, ~10% apart, or the
26k sector cluster) is deliberately NOT the band's job — it is undetectable by
magnitude and is handled at the source by the reconnect map-reset routing fix in
``brokers/fyers.py``. This guard is the poison-proof safety net beneath it.

The API is pure/sync on the hot path (``passes`` / ``check_ohlc``); reference
seeding is async and fully best-effort (a failure leaves the absolute band in
force, never blocks ingest).
"""
from __future__ import annotations

import time as _time
from statistics import median
from typing import Any, Iterable, Optional

from loguru import logger

# Reject a print that deviates more than this fraction from the seeded
# prior-session reference close. 20% is generous — a broad index intraday move
# beyond ±20% would already have tripped exchange circuit breakers — while still
# catching every documented cross-index magnitude swap of 30%+.
REL_TOL = 0.20

# Minimum seconds between prior-session reference refreshes (see
# ``maybe_refresh_reference_closes``). ~6h ⇒ at most a few refreshes/day, driven
# off the existing flush loop with no separate scheduler.
_REFRESH_TTL_SECONDS = 6 * 60 * 60

# Absolute sanity band per **app symbol**: (low, high). Keyed by the canonical
# app symbol a tick carries after ``to_app_symbol`` (index spot + NSE sector
# indices). Bounds bracket ~0.5x..2.1x the 2026-Q3 level of each index — wide
# enough that no real intraday (or even circuit-halt) move is rejected, tight
# enough that a co-subscribed instrument's level (2x+ away) is caught with no
# reference. Review if an index sustainably approaches a bound.
_ABS_BANDS: dict[str, tuple[float, float]] = {
    # Tradeable indices (also in symbols.DISPLAY_NAMES)
    "NSE:NIFTY50-INDEX": (12_000.0, 50_000.0),
    "NSE:BANKNIFTY-INDEX": (28_000.0, 120_000.0),
    "NSE:FINNIFTY-INDEX": (13_000.0, 55_000.0),
    "NSE:MIDCPNIFTY-INDEX": (7_000.0, 32_000.0),
    "BSE:SENSEX-INDEX": (40_000.0, 160_000.0),
    # NSE sector indices (symbols.SECTOR_INDEX_APP_SYMBOLS) — streamed to
    # market_ticks and previously UNGUARDED (the coverage gap: the old median
    # gate only ran for DISPLAY_NAMES). Same contamination signature.
    "NSE:NIFTYIT-INDEX": (14_000.0, 60_000.0),
    "NSE:NIFTYAUTO-INDEX": (13_000.0, 56_000.0),
    "NSE:NIFTYPHARMA-INDEX": (12_000.0, 54_000.0),
    "NSE:NIFTYFMCG-INDEX": (24_000.0, 100_000.0),
    "NSE:NIFTYMETAL-INDEX": (6_000.0, 28_000.0),
    "NSE:NIFTYENERGY-INDEX": (18_000.0, 82_000.0),
    "NSE:NIFTYREALTY-INDEX": (400.0, 2_200.0),
}

# Underlying-name → app-symbol, for callers that key by the DB ``underlying``
# column (e.g. the market-intelligence spot backfill) rather than the app symbol.
_UNDERLYING_TO_APP: dict[str, str] = {
    "NIFTY": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:BANKNIFTY-INDEX",
    "FINNIFTY": "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
    "SENSEX": "BSE:SENSEX-INDEX",
}

# Prior-session reference close per app symbol (external anchor; NEVER written
# from the live tape). Seeded by ``refresh_reference_closes``.
_ref_close: dict[str, float] = {}
_last_refresh_at: float = 0.0


def is_guarded(app_symbol: str) -> bool:
    """True when ``app_symbol`` is an index we apply the magnitude band to."""
    return app_symbol in _ABS_BANDS


def app_symbol_for_underlying(underlying: str) -> Optional[str]:
    """Map a DB ``underlying`` name (e.g. ``NIFTY``) to its guarded app symbol."""
    if not underlying:
        return None
    return _UNDERLYING_TO_APP.get(str(underlying).strip().upper())


def set_reference_close(app_symbol: str, close: float) -> None:
    """Seed/override the prior-session reference close for ``app_symbol``."""
    try:
        value = float(close)
    except (TypeError, ValueError):
        return
    if value > 0:
        _ref_close[app_symbol] = value


def clear_reference_closes() -> None:
    """Drop all seeded references (test isolation)."""
    _ref_close.clear()
    global _last_refresh_at
    _last_refresh_at = 0.0


def passes(app_symbol: str, price: float) -> bool:
    """Return True if ``price`` is a plausible level for ``app_symbol``.

    Non-guarded symbols always pass (options, stocks, commodities move on their
    own scale). Guarded symbols must fall inside the absolute band AND, when a
    prior-session reference has been seeded, inside ``±REL_TOL`` of it.
    """
    band = _ABS_BANDS.get(app_symbol)
    if band is None:
        return True
    try:
        px = float(price)
    except (TypeError, ValueError):
        return False
    lo, hi = band
    if not (lo <= px <= hi):
        return False
    ref = _ref_close.get(app_symbol)
    if ref and ref > 0 and abs(px - ref) / ref > REL_TOL:
        return False
    return True


def check_ohlc(
    app_symbol: str,
    open_: Any,
    high: Any,
    low: Any,
    close: Any,
) -> bool:
    """True only if every non-null O/H/L/C of a candle row passes the band.

    A single contaminating tick inside a minute corrupts that bar's high or low
    even when its close is legit, so a candle-level guard must test all four.
    """
    if app_symbol not in _ABS_BANDS:
        return True
    for value in (open_, high, low, close):
        if value in (None, ""):
            continue
        try:
            px = float(value)
        except (TypeError, ValueError):
            return False
        if px <= 0:
            # Zero/negative is a different (structural) concern; don't reject a
            # missing leg here.
            continue
        if not passes(app_symbol, px):
            return False
    return True


async def refresh_reference_closes(session_factory: Any = None) -> int:
    """Seed ``_ref_close`` from the median close of recent completed sessions.

    Uses the *median* over a multi-day window per underlying, which stays on the
    true index level even if some of those rows are themselves contaminated
    (contamination is a per-symbol minority). Best-effort: any error leaves the
    absolute band in force and is swallowed. Returns the number of refs seeded.
    """
    global _last_refresh_at
    if session_factory is None:
        from db.database import AsyncSessionLocal as session_factory  # type: ignore
    from sqlalchemy import text

    seeded = 0
    try:
        async with session_factory() as session:
            for underlying, app_symbol in _UNDERLYING_TO_APP.items():
                try:
                    result = await session.execute(
                        text(
                            """
                            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY close) AS med
                            FROM underlying_spot_candles
                            WHERE underlying = :underlying
                              AND interval = '1minute'
                              AND close > 0
                              AND time >= (NOW() - INTERVAL '7 days')
                            """
                        ),
                        {"underlying": underlying},
                    )
                    med = result.scalar()
                    if med is not None and float(med) > 0:
                        set_reference_close(app_symbol, float(med))
                        seeded += 1
                except Exception as exc:  # noqa: BLE001 — per-symbol, non-fatal
                    logger.debug(f"[index_band_guard] ref seed failed for {underlying}: {exc}")
    except Exception as exc:  # noqa: BLE001 — never block ingest on a DB blip
        logger.debug(f"[index_band_guard] reference refresh skipped: {exc}")
        return seeded
    _last_refresh_at = _time.monotonic()
    if seeded:
        logger.info(f"[index_band_guard] seeded {seeded} prior-session reference closes")
    return seeded


async def maybe_refresh_reference_closes(session_factory: Any = None) -> None:
    """TTL-gated ``refresh_reference_closes`` — safe to call from a hot loop."""
    if _time.monotonic() - _last_refresh_at < _REFRESH_TTL_SECONDS:
        return
    await refresh_reference_closes(session_factory)


def median_in_band(app_symbol: str, prices: Iterable[float]) -> Optional[float]:
    """Median of ``prices`` restricted to the absolute band (diagnostics/tests)."""
    kept = [float(p) for p in prices if passes(app_symbol, p)]
    if not kept:
        return None
    return float(median(kept))
