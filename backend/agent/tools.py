"""Tool definitions for the Claude trading agent."""
from __future__ import annotations
import json
from typing import Any

from market_data import data_router, option_chain_service, market_profile_builder
from analytics.sector import sector_tracker
from analytics.greeks import aggregate_portfolio_greeks


AGENT_TOOLS = [
    {
        "name": "get_market_profile",
        "description": "Get Market Profile data for a symbol including POC, VAH, VAL, IB levels, and TPO distribution.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "NSE symbol e.g. NSE:NIFTY50-INDEX"},
                "timeframe": {"type": "string", "enum": ["daily", "hourly"], "description": "Profile timeframe"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_option_chain",
        "description": "Get current option chain for a symbol with OI, IV, PCR, and max pain.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "expiry": {"type": "string", "description": "Expiry date YYYY-MM-DD"},
            },
            "required": ["symbol", "expiry"],
        },
    },
    {
        "name": "get_greeks",
        "description": "Calculate Black-Scholes Greeks for a specific option.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "strike": {"type": "number"},
                "expiry": {"type": "string"},
                "option_type": {"type": "string", "enum": ["CE", "PE"]},
                "spot": {"type": "number"},
                "iv": {"type": "number", "description": "Implied volatility (0-1 range)"},
            },
            "required": ["symbol", "strike", "expiry", "option_type", "spot"],
        },
    },
    {
        "name": "get_pcr",
        "description": "Get Put-Call Ratio (volume and OI) for a symbol.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "expiry": {"type": "string"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_iv_rank",
        "description": "Get IV Rank and IV Percentile (52-week) for a symbol.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_sector_rotation",
        "description": "Get relative strength of NSE sectoral indices vs Nifty 50.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_open_positions",
        "description": "Get current open positions in the portfolio.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "place_order",
        "description": "Place a trade order. In paper mode executes immediately; in live mode adds to approval queue.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "action": {"type": "string", "enum": ["BUY", "SELL"]},
                "order_type": {"type": "string", "enum": ["MARKET", "LIMIT", "SL", "SL_M"]},
                "qty": {"type": "integer"},
                "price": {"type": "number"},
                "sl": {"type": "number"},
                "target": {"type": "number"},
                "instrument_type": {"type": "string", "enum": ["CE", "PE", "FUT", "EQ"]},
                "expiry": {"type": "string"},
                "strike": {"type": "number"},
            },
            "required": ["symbol", "action", "order_type", "qty"],
        },
    },
    {
        "name": "get_news_sentiment",
        "description": "Get recent news headlines and sentiment for a symbol or sector.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Symbol or sector to search"},
            },
            "required": ["query"],
        },
    },
]


async def execute_tool(tool_name: str, tool_input: dict, portfolio=None) -> Any:
    """Execute an agent tool call and return the result."""
    if tool_name == "get_market_profile":
        symbol = tool_input.get("symbol", "")
        timeframe = tool_input.get("timeframe", "daily")
        result = await market_profile_builder.get_cached_profile(symbol, timeframe)
        return result or {"error": "No market profile available"}

    elif tool_name == "get_option_chain":
        symbol = tool_input.get("symbol", "")
        expiry = tool_input.get("expiry", "")
        result = await option_chain_service.get_cached(symbol, expiry)
        return result or {"error": "No option chain data available"}

    elif tool_name == "get_greeks":
        from analytics.greeks import bs_greeks
        from datetime import datetime
        symbol = tool_input.get("symbol", "")
        strike = tool_input.get("strike", 0)
        expiry_str = tool_input.get("expiry", "")
        option_type = tool_input.get("option_type", "CE")
        spot = tool_input.get("spot", 0)
        iv = tool_input.get("iv", 0.20)
        try:
            expiry_dt = datetime.strptime(expiry_str, "%Y-%m-%d")
            T = max(0, (expiry_dt - datetime.utcnow()).days) / 365
        except Exception:
            T = 0.1
        greeks = bs_greeks(S=spot, K=strike, T=T, r=0.065, sigma=iv, option_type=option_type)
        return {
            "symbol": symbol,
            "strike": strike,
            "option_type": option_type,
            "delta": greeks.delta,
            "gamma": greeks.gamma,
            "theta": greeks.theta,
            "vega": greeks.vega,
            "rho": greeks.rho,
        }

    elif tool_name == "get_pcr":
        symbol = tool_input.get("symbol", "")
        expiry = tool_input.get("expiry", "")
        chain = await option_chain_service.get_cached(symbol, expiry)
        if chain:
            return {"pcr_oi": chain.get("pcr_oi", 1.0), "pcr_volume": chain.get("pcr_volume", 1.0)}
        return {"pcr_oi": 1.0, "pcr_volume": 1.0}

    elif tool_name == "get_iv_rank":
        symbol = tool_input.get("symbol", "")
        return await sector_tracker.get_iv_rank(symbol)

    elif tool_name == "get_sector_rotation":
        return await sector_tracker.get_sector_rotation()

    elif tool_name == "get_open_positions":
        if portfolio:
            return {"positions": portfolio.get_positions_list()}
        return {"positions": []}

    elif tool_name == "place_order":
        # This is handled specially by the agent — returned as proposal
        return {"status": "proposal_created", "order": tool_input}

    elif tool_name == "get_news_sentiment":
        # Placeholder — integrate with news API
        return {"headlines": [], "sentiment": "neutral"}

    return {"error": f"Unknown tool: {tool_name}"}
