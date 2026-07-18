"""Shared market symbol aliases for frontend/app symbols and broker keys."""
from __future__ import annotations

import time
from typing import Optional

from analysis.instruments import INDEX_INSTRUMENT_KEYS


APP_TO_BROKER_SYMBOL: dict[str, str] = {
    "NSE:NIFTY50-INDEX": INDEX_INSTRUMENT_KEYS["NIFTY"],
    "NSE:BANKNIFTY-INDEX": INDEX_INSTRUMENT_KEYS["BANKNIFTY"],
    "NSE:FINNIFTY-INDEX": INDEX_INSTRUMENT_KEYS["FINNIFTY"],
    "NSE:MIDCPNIFTY-INDEX": INDEX_INSTRUMENT_KEYS["MIDCPNIFTY"],
    "BSE:SENSEX-INDEX": INDEX_INSTRUMENT_KEYS["SENSEX"],
}

APP_TO_FYERS_SYMBOL: dict[str, str] = {
    "NSE:NIFTY50-INDEX": "NSE:NIFTY50-INDEX",
    "NSE:BANKNIFTY-INDEX": "NSE:NIFTYBANK-INDEX",
    "NSE:FINNIFTY-INDEX": "NSE:FINNIFTY-INDEX",
    "NSE:MIDCPNIFTY-INDEX": "NSE:MIDCPNIFTY-INDEX",
    "BSE:SENSEX-INDEX": "BSE:SENSEX-INDEX",
}

BROKER_TO_APP_SYMBOL: dict[str, str] = {
    broker_symbol: app_symbol for app_symbol, broker_symbol in APP_TO_BROKER_SYMBOL.items()
}

FYERS_TO_APP_SYMBOL: dict[str, str] = {
    fyers_symbol: app_symbol for app_symbol, fyers_symbol in APP_TO_FYERS_SYMBOL.items()
}

DISPLAY_NAMES: dict[str, str] = {
    "NSE:NIFTY50-INDEX": "NIFTY",
    "NSE:BANKNIFTY-INDEX": "BANKNIFTY",
    "NSE:FINNIFTY-INDEX": "FINNIFTY",
    "NSE:MIDCPNIFTY-INDEX": "MIDCPNIFTY",
    "BSE:SENSEX-INDEX": "SENSEX",
}

DISPLAY_TO_APP_SYMBOL: dict[str, str] = {
    display_name.upper(): app_symbol for app_symbol, display_name in DISPLAY_NAMES.items()
}

LIVE_INDEX_APP_SYMBOLS: tuple[str, ...] = tuple(APP_TO_BROKER_SYMBOL.keys())

# Symbols that must be continuously captured to `market_ticks`.
# Keep this intentionally small: these are the MP-critical index streams.
TICK_CAPTURE_APP_SYMBOLS: tuple[str, ...] = (
    "NSE:NIFTY50-INDEX",
    "NSE:BANKNIFTY-INDEX",
    "BSE:SENSEX-INDEX",
)

# NSE sector indices streamed for the sector-network + live terminal. These are
# already broker-native Fyers keys (to_fyers_symbol passes them through), so the
# quote_bus tape carries them once subscribed. BANKNIFTY is intentionally omitted
# — it is already in LIVE_INDEX_APP_SYMBOLS (NSE:BANKNIFTY-INDEX → NSE:NIFTYBANK).
SECTOR_INDEX_APP_SYMBOLS: tuple[str, ...] = (
    "NSE:NIFTYIT-INDEX",
    "NSE:NIFTYAUTO-INDEX",
    "NSE:NIFTYPHARMA-INDEX",
    "NSE:NIFTYFMCG-INDEX",
    "NSE:NIFTYMETAL-INDEX",
    "NSE:NIFTYENERGY-INDEX",
    "NSE:NIFTYREALTY-INDEX",
)


def to_broker_symbol(symbol: str) -> str:
    """Translate an app symbol to the broker's instrument key when known."""
    normalized = str(symbol or "").strip()
    app_symbol = to_app_symbol(normalized)
    return APP_TO_BROKER_SYMBOL.get(app_symbol, normalized)


def to_fyers_symbol(symbol: str) -> str:
    """Translate an app symbol to the equivalent Fyers market symbol when known."""
    normalized = str(symbol or "").strip()
    app_symbol = to_app_symbol(normalized)
    return APP_TO_FYERS_SYMBOL.get(app_symbol, normalized)


# ---------------------------------------------------------------------------
# Broker-canonical OPTION-CHAIN instrument resolution (2026-07-18, defect 7c).
#
# to_broker_symbol() maps only the 5 index app-symbols to Upstox instrument
# keys; a stock passes through as its bare name ("INFY"), which Upstox's
# /option/chain rejects with a hard 400 "Invalid Instrument key" — the exact
# shape of the 2026-07-17 storm (8120 400s, ~83% of the Upstox 30-min budget).
# fo_underlying_catalog already carries the broker-canonical key per stock
# (underlying_key, e.g. "NSE_EQ|INE009A01021" for INFY); resolve from there
# with an in-process TTL cache. Cache shape mirrors
# live_candle_store._resolve_symbol_metadata: positives are long-lived
# (catalog keys are stable), negatives expire quickly so a name added to the
# catalog mid-session resolves without a restart.
# ---------------------------------------------------------------------------

UNDERLYING_KEY_CACHE_TTL_SECONDS = 3600.0
UNDERLYING_KEY_NEGATIVE_TTL_SECONDS = 300.0

# catalog name -> (underlying_key or None, cached_at monotonic)
_underlying_key_cache: dict[str, tuple[Optional[str], float]] = {}


def clear_underlying_key_cache() -> None:
    """Test hook — drop all cached catalog resolutions."""
    _underlying_key_cache.clear()


def _stock_catalog_name(app_symbol: str) -> str:
    """Normalize a stock app/display symbol to its fo_underlying_catalog name.

    "NSE:INFY-EQ" -> "INFY"; "INFY" -> "INFY"; "BAJAJ-AUTO" -> "BAJAJ-AUTO"
    (only a trailing "-EQ" is stripped — NSE names legitimately contain '-').
    """
    name = str(app_symbol or "").strip().upper()
    if name.startswith("NSE:"):
        name = name[len("NSE:"):]
    if name.endswith("-EQ"):
        name = name[: -len("-EQ")]
    return name


async def resolve_upstox_option_underlying_key(symbol: str) -> Optional[str]:
    """Resolve the Upstox instrument key an option-chain call must send.

    Indices keep the static APP_TO_BROKER_SYMBOL mapping (byte-identical to
    to_broker_symbol — no DB touch). Stocks resolve via
    fo_underlying_catalog.underlying_key. Returns None when no canonical key
    exists — callers MUST fail closed instead of sending the bare name to the
    broker (guaranteed 400).
    """
    normalized = str(symbol or "").strip()
    if not normalized:
        return None
    app_symbol = to_app_symbol(normalized)
    static_key = APP_TO_BROKER_SYMBOL.get(app_symbol)
    if static_key:
        return static_key

    name = _stock_catalog_name(app_symbol)
    if not name:
        return None

    now = time.monotonic()
    cached = _underlying_key_cache.get(name)
    if cached is not None:
        value, cached_at = cached
        ttl = (
            UNDERLYING_KEY_CACHE_TTL_SECONDS
            if value
            else UNDERLYING_KEY_NEGATIVE_TTL_SECONDS
        )
        if now - cached_at < ttl:
            return value
        _underlying_key_cache.pop(name, None)

    try:
        # Lazy imports keep this widely-imported module light and avoid any
        # import-order coupling with db.database at startup.
        from sqlalchemy import text

        from db.database import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    SELECT underlying_key
                    FROM fo_underlying_catalog
                    WHERE symbol = :symbol
                      AND underlying_key IS NOT NULL
                    LIMIT 1
                    """
                ),
                {"symbol": name},
            )
            row = result.first()
    except Exception as exc:  # noqa: BLE001
        # Transient DB failure: fail closed for THIS call but do NOT
        # negative-cache — the next attempt retries the catalog.
        from loguru import logger

        logger.warning(
            f"[symbols] underlying_key lookup failed for {name!r} "
            f"(fail-closed, not cached): {exc}"
        )
        return None

    key = str(getattr(row, "underlying_key", "") or "").strip() if row is not None else ""
    _underlying_key_cache[name] = (key or None, now)
    return key or None


def to_fyers_option_symbol(symbol: str) -> str:
    """Fyers option-chain symbol for an underlying.

    Indices go through APP_TO_FYERS_SYMBOL exactly as before. A bare stock
    name ("INFY") is formatted to the Fyers equity symbology
    ("NSE:INFY-EQ") — Fyers' /options-chain-v3 has no bare-name symbol, so
    the old passthrough was a guaranteed invalid-symbol call whenever the
    chain route fell over to Fyers for a stock. Symbols already carrying an
    exchange prefix ("NSE:INFY-EQ", "MCX:CRUDEOIL...") pass through untouched.
    """
    fyers_symbol = to_fyers_symbol(symbol)
    if not fyers_symbol or ":" in fyers_symbol:
        return fyers_symbol
    name = fyers_symbol.strip().upper()
    if name.endswith("-INDEX") or name.endswith("-EQ"):
        return name
    return f"NSE:{name}-EQ"


def to_app_symbol(symbol: str) -> str:
    """Translate display names and broker symbols back to the canonical app symbol."""
    normalized = str(symbol or "").strip()
    if not normalized:
        return normalized
    if normalized in APP_TO_BROKER_SYMBOL:
        return normalized
    if normalized in BROKER_TO_APP_SYMBOL:
        return BROKER_TO_APP_SYMBOL[normalized]
    if normalized in FYERS_TO_APP_SYMBOL:
        return FYERS_TO_APP_SYMBOL[normalized]
    return DISPLAY_TO_APP_SYMBOL.get(normalized.upper(), normalized)
