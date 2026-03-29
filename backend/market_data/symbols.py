"""Shared market symbol aliases for frontend/app symbols and broker keys."""
from __future__ import annotations

from analysis.instruments import INDEX_INSTRUMENT_KEYS


APP_TO_BROKER_SYMBOL: dict[str, str] = {
    "NSE:NIFTY50-INDEX": INDEX_INSTRUMENT_KEYS["NIFTY"],
    "NSE:BANKNIFTY-INDEX": INDEX_INSTRUMENT_KEYS["BANKNIFTY"],
    "NSE:FINNIFTY-INDEX": INDEX_INSTRUMENT_KEYS["FINNIFTY"],
    "NSE:MIDCPNIFTY-INDEX": INDEX_INSTRUMENT_KEYS["MIDCPNIFTY"],
}

BROKER_TO_APP_SYMBOL: dict[str, str] = {
    broker_symbol: app_symbol for app_symbol, broker_symbol in APP_TO_BROKER_SYMBOL.items()
}

DISPLAY_NAMES: dict[str, str] = {
    "NSE:NIFTY50-INDEX": "NIFTY",
    "NSE:BANKNIFTY-INDEX": "BANKNIFTY",
    "NSE:FINNIFTY-INDEX": "FINNIFTY",
    "NSE:MIDCPNIFTY-INDEX": "MIDCPNIFTY",
}

LIVE_INDEX_APP_SYMBOLS: tuple[str, ...] = tuple(APP_TO_BROKER_SYMBOL.keys())


def to_broker_symbol(symbol: str) -> str:
    """Translate an app symbol to the broker's instrument key when known."""
    return APP_TO_BROKER_SYMBOL.get(symbol, symbol)


def to_app_symbol(symbol: str) -> str:
    """Translate a broker instrument key back to the app symbol when known."""
    return BROKER_TO_APP_SYMBOL.get(symbol, symbol)

