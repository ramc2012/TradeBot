"""Front-month index-futures → auction-intelligence order-flow book mapping.

The auction-intelligence order-flow path reads real bid/ask depth + tape from a
futures "book" contract because an index spot has no order book. Operators can
pin a static ``{index_app_symbol=futures_app_symbol}`` map via
``AUCTION_OF_BOOK_SYMBOLS``, but index futures roll every month, so a static
value goes stale. When ``AUCTION_OF_AUTO_FUTURES_BOOK`` is on, this module
resolves the front-month index future automatically (roll-aware, via the shared
expiry calendar) for the configured indices and merges it with any static map
(static wins on conflict).

Default OFF → the resolved map is identical to the static parser, so behaviour
is unchanged (index-spot capture only) until an operator opts in.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from core.config import auction_of_book_symbols, settings

_MONTHS = (
    "", "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)

# Index code → spot app_symbol (the market_ticks key the order-flow path keys
# the book redirect on). Mirrors auction_intelligence.live.SYMBOL_MAP, kept here
# to avoid importing the live module (which imports market_data → cycle).
INDEX_APP_SYMBOL: dict[str, str] = {
    "NIFTY": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:BANKNIFTY-INDEX",
    "FINNIFTY": "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
    "SENSEX": "BSE:SENSEX-INDEX",
}

# BSE-listed indices price their futures on BSE; everything else is NSE.
_BSE_INDICES = {"SENSEX", "BANKEX"}


def front_month_index_future(symbol_code: str, as_of: Optional[date] = None) -> Optional[str]:
    """Resolve the front-month index-futures app_symbol (e.g. ``NSE:NIFTY26JUNFUT``).

    Roll-aware: if ``as_of`` is past this month's monthly expiry, the front
    month is next month's contract. The contract is named by its EXPIRY month.
    Returns None for an unknown index.
    """
    symbol_code = str(symbol_code or "").upper()
    if symbol_code not in INDEX_APP_SYMBOL:
        return None
    as_of = as_of or date.today()
    year, month = as_of.year, as_of.month
    try:
        from analysis.instruments import get_index_monthly_expiry

        expiry = get_index_monthly_expiry(symbol_code, year, month)
    except Exception:
        expiry = None
    if expiry is not None and as_of > expiry:
        # Past this month's expiry → trade next month's contract.
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    exchange = "BSE" if symbol_code in _BSE_INDICES else "NSE"
    return f"{exchange}:{symbol_code}{year % 100:02d}{_MONTHS[month]}FUT"


def resolve_auction_book_symbols(as_of: Optional[date] = None) -> dict[str, str]:
    """Merge the static ``AUCTION_OF_BOOK_SYMBOLS`` map with the auto-resolved
    front-month futures (when ``AUCTION_OF_AUTO_FUTURES_BOOK`` is on).

    Static entries win on conflict, so an operator can always override a specific
    index's book contract by hand. Returns ``{index_app_symbol: futures_app_symbol}``.
    """
    mapping: dict[str, str] = dict(auction_of_book_symbols())
    if not settings.AUCTION_OF_AUTO_FUTURES_BOOK:
        return mapping
    indices = [
        token.strip().upper()
        for token in str(settings.AUCTION_OF_FUTURES_INDICES or "").split(",")
        if token.strip()
    ]
    for symbol_code in indices:
        index_app = INDEX_APP_SYMBOL.get(symbol_code)
        if not index_app:
            continue
        future = front_month_index_future(symbol_code, as_of)
        if future:
            mapping.setdefault(index_app, future)  # static map wins on conflict
    return mapping


__all__ = ["front_month_index_future", "resolve_auction_book_symbols", "INDEX_APP_SYMBOL"]
