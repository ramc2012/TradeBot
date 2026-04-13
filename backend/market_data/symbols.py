"""Shared market symbol aliases for frontend/app symbols and broker keys."""
from __future__ import annotations

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
