"""Auction coverage from the app's canonical F&O catalog; never fetch a master."""
from time import monotonic
from sqlalchemy import text
from db.database import AsyncSessionLocal

INDEX_SYMBOLS = ("NIFTY", "BANKNIFTY", "SENSEX")
_STOCKS: dict[str, dict] = {}
_REFRESH_AT = 0.0


async def refresh_universe() -> dict:
    global _STOCKS, _REFRESH_AT
    if monotonic() < _REFRESH_AT:
        return _STOCKS
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("""
            SELECT symbol, lot_size, spot_instrument_key, fno_active
            FROM fo_underlying_catalog WHERE kind = 'STOCK' ORDER BY symbol
        """))
        stocks = {r.symbol: {"app_symbol": f"NSE:{r.symbol}-EQ", "display": r.symbol,
                             "instrument_proxy": "cash_equity", "lot_size": int(r.lot_size or 0),
                             "tick_size": 0.05, "spot_instrument_key": r.spot_instrument_key, "fno_active": r.fno_active}
                  for r in result}
    _STOCKS = {s: d for s, d in stocks.items() if d["fno_active"] is not False}
    _REFRESH_AT = monotonic() + 300
    # Compatibility for existing callers; keep retired names addressable for held books.
    from auction_intelligence.live import SYMBOL_MAP
    from auction_intelligence.options.mapper import _UNDERLYING_TO_APP_SYMBOL
    SYMBOL_MAP.update(stocks)
    from market_data.symbols import register_broker_symbol
    for spec in stocks.values():
        if spec.get("spot_instrument_key"):
            register_broker_symbol(spec["app_symbol"], spec["spot_instrument_key"])
    _UNDERLYING_TO_APP_SYMBOL.update({s: d["app_symbol"] for s, d in stocks.items()})
    return _STOCKS


async def universe_payload() -> dict:
    stocks = await refresh_universe()
    return {"symbols": [*INDEX_SYMBOLS, *sorted(stocks)], "indices": list(INDEX_SYMBOLS),
            "stock_count": len(stocks), "source": "fo_underlying_catalog",
            "execution_mode": "paper", "allow_live_orders": False,
            "coverage_note": "Eligibility is separate from candle, quote and contract readiness."}
