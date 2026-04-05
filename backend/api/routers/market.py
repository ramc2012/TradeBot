"""Market data routes."""
from __future__ import annotations
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import List, Optional
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from market_data import (
    atm_watchlist_service,
    data_router,
    market_profile_builder,
    option_chain_service,
)
from market_data.symbols import to_app_symbol, to_broker_symbol, to_fyers_symbol
from analytics.greeks import bs_greeks, implied_volatility
from analytics.sector import sector_tracker
from api.routers.auth import (
    ensure_fyers_session,
    ensure_upstox_session,
    get_active_adapter,
    get_broker_token,
)

router = APIRouter(prefix="/api/market", tags=["market"])

_PROFILE_TIMEFRAMES = {"day", "week", "month", "daily", "hourly"}


def _market_symbol_for_adapter(adapter, app_symbol: str, broker_symbol: str) -> str:
    if getattr(adapter, "broker_name", "") == "fyers":
        return to_fyers_symbol(app_symbol)
    return broker_symbol


async def _get_market_adapter():
    fyers = get_active_adapter("fyers")
    if fyers:
        return fyers, "fyers"
    if await ensure_fyers_session():
        fyers = get_active_adapter("fyers")
        if fyers:
            return fyers, "fyers"

    upstox = get_active_adapter("upstox")
    if upstox:
        return upstox, "upstox"
    if await ensure_upstox_session():
        upstox = get_active_adapter("upstox")
        if upstox:
            return upstox, "upstox"

    return None, "none"


async def _resolve_option_expiry(adapter, broker_symbol: str, requested_expiry: Optional[str]) -> Optional[str]:
    get_contracts = getattr(adapter, "get_option_contracts", None)
    if not callable(get_contracts):
        return requested_expiry

    try:
        contracts = await get_contracts(broker_symbol)
    except Exception:
        return requested_expiry

    expiries = sorted({row.get("expiry") for row in contracts if row.get("expiry")})
    if not expiries:
        return requested_expiry
    if requested_expiry and requested_expiry in expiries:
        return requested_expiry

    today = date.today().isoformat()
    for expiry in expiries:
        if expiry >= today:
            return expiry
    return expiries[0]


def _normalize_profile_timeframe(timeframe: str) -> str:
    normalized = (timeframe or "day").lower()
    if normalized not in _PROFILE_TIMEFRAMES:
        raise HTTPException(
            status_code=400,
            detail="Unsupported timeframe. Use one of: day, week, month, daily, hourly.",
        )
    return "day" if normalized == "daily" else normalized


async def _fetch_fyers_historical_rows(
    symbol: str,
    resolution: str,
    from_date: date,
    to_date: date,
) -> list[dict]:
    adapter = get_active_adapter("fyers")
    if adapter is None:
        if not await ensure_fyers_session():
            return []
        adapter = get_active_adapter("fyers")
    get_history = getattr(adapter, "get_historical_candles", None) if adapter else None
    if not callable(get_history):
        return []
    try:
        return await get_history(
            symbol,
            resolution,
            from_date.isoformat(),
            to_date.isoformat(),
        )
    except Exception:
        return []


async def _fetch_upstox_historical_rows(
    instrument_key: str,
    interval: str,
    from_date: date,
    to_date: date,
) -> list[dict]:
    token = get_broker_token("upstox")
    if not token:
        return []

    encoded_key = quote(instrument_key, safe="")
    url = (
        f"https://api.upstox.com/v2/historical-candle/"
        f"{encoded_key}/{interval}/{to_date.isoformat()}/{from_date.isoformat()}"
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
    if resp.status_code != 200:
        return []

    rows = []
    for candle in reversed(resp.json().get("data", {}).get("candles", [])):
        rows.append(
            {
                "time": str(candle[0]),
                "open": float(candle[1]),
                "high": float(candle[2]),
                "low": float(candle[3]),
                "close": float(candle[4]),
                "volume": int(candle[5] or 0),
            }
        )
    return rows


async def _fetch_market_historical_rows(
    *,
    app_symbol: str,
    broker_symbol: str,
    fyers_resolution: str,
    upstox_interval: str,
    from_date: date,
    to_date: date,
) -> tuple[list[dict], str]:
    fyers_rows = await _fetch_fyers_historical_rows(
        app_symbol,
        fyers_resolution,
        from_date,
        to_date,
    )
    if fyers_rows:
        return fyers_rows, "fyers"

    upstox_rows = await _fetch_upstox_historical_rows(
        broker_symbol,
        upstox_interval,
        from_date,
        to_date,
    )
    if upstox_rows:
        return upstox_rows, "upstox"

    return [], "none"


def _merge_rows(primary: list[dict], secondary: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for row in primary + secondary:
        key = str(row.get("time") or row.get("timestamp"))
        if key:
            merged[key] = row
    return [merged[key] for key in sorted(merged)]


@router.get("/option-chain/{symbol}")
async def get_option_chain(symbol: str, expiry: Optional[str] = Query(None)):
    """Get full option chain with PCR, max pain, greeks."""
    app_symbol = to_app_symbol(symbol)
    broker_symbol = to_broker_symbol(symbol)
    adapter, source = await _get_market_adapter()
    adapter_symbol = _market_symbol_for_adapter(adapter, app_symbol, broker_symbol) if adapter else broker_symbol
    expiry = await _resolve_option_expiry(adapter, adapter_symbol, expiry) if adapter else expiry
    if not expiry:
        from agent.trading_agent import TradingAgent
        expiry = TradingAgent._next_expiry()

    cached = await option_chain_service.get_cached(app_symbol, expiry)
    if cached and cached.get("source") == source:
        return cached

    if adapter:
        option_chain_service.set_broker(adapter)
        option_chain_service.track(app_symbol, expiry)
        await option_chain_service._refresh(app_symbol, expiry)
        cached = await option_chain_service.get_cached(app_symbol, expiry)
        if cached:
            return cached
    return {
        "symbol": app_symbol,
        "expiry": expiry,
        "entries": [],
        "source": source,
        "error": "No data available",
    }


@router.get("/atm-watchlist/expiries")
async def get_atm_watchlist_expiries():
    return await atm_watchlist_service.get_expiries()


@router.get("/atm-watchlist")
async def get_atm_watchlist(expiry: Optional[str] = Query(None)):
    return await atm_watchlist_service.get_watchlist(expiry)


@router.get("/market-profile/{symbol}")
async def get_market_profile(
    symbol: str,
    timeframe: str = Query("day"),
):
    """Get Market Profile (POC, VAH, VAL, IB, TPO data)."""
    app_symbol = to_app_symbol(symbol)
    broker_symbol = to_broker_symbol(symbol)
    normalized_timeframe = _normalize_profile_timeframe(timeframe)
    profile = await market_profile_builder.get_cached_profile(app_symbol, normalized_timeframe)
    if not profile:
        built = None
        source = "live"
        today = date.today()
        if normalized_timeframe == "hourly":
            built = market_profile_builder.build_hourly_profile(app_symbol)
        elif normalized_timeframe == "day":
            live_rows = market_profile_builder.get_tick_rows(app_symbol)
            rows = live_rows
            source_interval = "tick"
            if len(rows) < 50:
                rows, source = await _fetch_market_historical_rows(
                    app_symbol=app_symbol,
                    broker_symbol=broker_symbol,
                    fyers_resolution="1",
                    upstox_interval="1minute",
                    from_date=today - timedelta(days=5),
                    to_date=today,
                )
                source_interval = "1minute" if source == "upstox" else "1"
            built = market_profile_builder.build_profile_from_rows(
                app_symbol,
                rows,
                normalized_timeframe,
                source_interval,
            )
        elif normalized_timeframe == "week":
            historical_rows, source = await _fetch_market_historical_rows(
                app_symbol=app_symbol,
                broker_symbol=broker_symbol,
                fyers_resolution="1",
                upstox_interval="1minute",
                from_date=today - timedelta(days=9),
                to_date=today,
            )
            rows = market_profile_builder.aggregate_rows(historical_rows, 3)
            rows = _merge_rows(rows, market_profile_builder.get_three_minute_rows(app_symbol))
            built = market_profile_builder.build_profile_from_rows(
                app_symbol,
                rows,
                normalized_timeframe,
                "3minute" if source == "upstox" else "3minute",
            )
        elif normalized_timeframe == "month":
            historical_rows, source = await _fetch_market_historical_rows(
                app_symbol=app_symbol,
                broker_symbol=broker_symbol,
                fyers_resolution="30",
                upstox_interval="30minute",
                from_date=today - timedelta(days=40),
                to_date=today,
            )
            live_month_rows = market_profile_builder.aggregate_rows(
                market_profile_builder.get_three_minute_rows(app_symbol),
                30,
            )
            rows = _merge_rows(historical_rows, live_month_rows)
            built = market_profile_builder.build_profile_from_rows(
                app_symbol,
                rows,
                normalized_timeframe,
                "30minute",
            )
        if built:
            payload = asdict(built)
            payload["tpo_data"] = {str(k): v for k, v in built.tpo_data.items()}
            if "source" not in payload:
                payload["source"] = source
            await market_profile_builder.store_profile(built)
            return payload
        return {
            "symbol": app_symbol,
            "timeframe": normalized_timeframe,
            "error": "No market profile data. Waiting for live tick feed.",
        }
    return profile


@router.get("/iv-rank/{symbol}")
async def get_iv_rank(symbol: str):
    return await sector_tracker.get_iv_rank(symbol)


@router.get("/pcr/{symbol}")
async def get_pcr(symbol: str, expiry: Optional[str] = Query(None)):
    if not expiry:
        from agent.trading_agent import TradingAgent
        expiry = TradingAgent._next_expiry()
    chain = await option_chain_service.get_cached(symbol, expiry)
    if chain:
        return {
            "symbol": symbol,
            "pcr_oi": chain.get("pcr_oi", 1.0),
            "pcr_volume": chain.get("pcr_volume", 1.0),
            "total_ce_oi": chain.get("total_ce_oi", 0),
            "total_pe_oi": chain.get("total_pe_oi", 0),
        }
    return {"symbol": symbol, "pcr_oi": 1.0, "pcr_volume": 1.0}


class LTPRequest(BaseModel):
    symbols: List[str]


@router.post("/ltp")
async def get_ltp(req: LTPRequest):
    app_symbols = [to_app_symbol(symbol) for symbol in req.symbols]
    adapter, source = await _get_market_adapter()
    if not adapter:
        # Return cached from data_router
        return {symbol: data_router.get_ltp(symbol) for symbol in app_symbols}
    try:
        mapped = {
            symbol: _market_symbol_for_adapter(adapter, symbol, to_broker_symbol(symbol))
            for symbol in app_symbols
        }
        live = await adapter.get_ltp(list(mapped.values()))
        return {
            symbol: float(live.get(broker_symbol, 0.0) or data_router.get_ltp(symbol))
            for symbol, broker_symbol in mapped.items()
        }
    except Exception:
        return {symbol: data_router.get_ltp(symbol) for symbol in app_symbols}


@router.get("/greeks/{symbol}/{strike}/{expiry}/{option_type}")
async def get_greeks(
    symbol: str,
    strike: float,
    expiry: str,
    option_type: str,
    spot: float = Query(...),
    iv: float = Query(0.20),
    r: float = Query(0.065),
):
    """Calculate Black-Scholes Greeks for a specific option."""
    from datetime import datetime
    try:
        expiry_dt = datetime.strptime(expiry, "%Y-%m-%d")
        T = max(0, (expiry_dt - datetime.utcnow()).days) / 365
    except ValueError:
        raise HTTPException(400, f"Invalid expiry format: {expiry}, use YYYY-MM-DD")

    greeks = bs_greeks(S=spot, K=strike, T=T, r=r, sigma=iv, option_type=option_type)
    iv_calc = implied_volatility(
        market_price=0,  # would pass LTP if known
        S=spot, K=strike, T=T, r=r, option_type=option_type
    )
    return {
        "symbol": symbol,
        "strike": strike,
        "expiry": expiry,
        "option_type": option_type,
        "spot": spot,
        "delta": greeks.delta,
        "gamma": greeks.gamma,
        "theta": greeks.theta,
        "vega": greeks.vega,
        "rho": greeks.rho,
        "iv": greeks.iv,
        "T_years": round(T, 4),
    }


@router.get("/expiries/{symbol}")
async def get_expiries(symbol: str):
    """Get available expiry dates for a symbol."""
    app_symbol = to_app_symbol(symbol)
    broker_symbol = to_broker_symbol(symbol)
    adapter, source = await _get_market_adapter()
    if not adapter:
        return {"symbol": app_symbol, "expiries": [], "default_expiry": None, "source": "none"}
    try:
        get_contracts = getattr(adapter, "get_option_contracts", None)
        if not callable(get_contracts):
            return {"symbol": app_symbol, "expiries": [], "default_expiry": None, "source": source}

        contracts = await get_contracts(_market_symbol_for_adapter(adapter, app_symbol, broker_symbol))
        expiries = sorted({row.get("expiry") for row in contracts if row.get("expiry")})
        default_expiry = (
            await _resolve_option_expiry(
                adapter,
                _market_symbol_for_adapter(adapter, app_symbol, broker_symbol),
                None,
            )
            if expiries
            else None
        )
        return {
            "symbol": app_symbol,
            "expiries": expiries,
            "default_expiry": default_expiry,
            "count": len(expiries),
            "source": source,
        }
    except Exception:
        return {"symbol": app_symbol, "expiries": [], "default_expiry": None, "source": source}
