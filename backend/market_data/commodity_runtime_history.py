"""Small bridge for strategy modules that need live MCX futures candles."""
from __future__ import annotations

from typing import Any

from market_data.commodity_contract_specs import extract_commodity_root


DEFAULT_COMMODITY_FUTURES: dict[str, str] = {
    "CRUDEOIL": "MCX:CRUDEOIL26MAYFUT",
}


async def load_commodity_history_rows(
    root: str,
    *,
    interval: str = "1minute",
    lookback_days: int = 10,
) -> tuple[list[dict[str, Any]], str]:
    """Load recent MCX futures candles using the commodity strategy's resolver.

    The commodity agent already knows how to prefer Upstox-resolved MCX
    contracts and fall back to Fyers symbols, so this keeps MP/FMP/directional
    testing aligned with the live commodity desk.
    """
    normalized_root = str(root or "").strip().upper()
    from paper_engine.commodity_strategy_agent import CommodityStrategyAgent

    agent = CommodityStrategyAgent()
    configured_symbols = agent.get_symbols()
    selected_symbol = next(
        (
            symbol
            for symbol in configured_symbols
            if extract_commodity_root(symbol) == normalized_root
        ),
        DEFAULT_COMMODITY_FUTURES.get(normalized_root, ""),
    )
    if not selected_symbol:
        return [], normalized_root
    rows = await agent._load_history(  # noqa: SLF001 - shared runtime bridge for local strategy history.
        selected_symbol,
        interval=interval,
        lookback_days=lookback_days,
    )
    return rows, selected_symbol
