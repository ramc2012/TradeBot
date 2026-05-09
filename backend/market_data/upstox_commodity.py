"""Upstox-assisted MCX contract resolution for commodity load sharing."""
from __future__ import annotations

import gzip
import json
from datetime import date, datetime, timezone
from time import monotonic
from typing import Any, Optional

import httpx
from loguru import logger

from api.routers.auth import ensure_upstox_session, get_active_adapter
from brokers.base import BrokerAdapter
from market_data.commodity_contract_specs import canonicalize_commodity_root


_MONTH_CODES = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_MONTH_TO_NUMBER = {code: index + 1 for index, code in enumerate(_MONTH_CODES)}
_RESOLUTION_TTL_SECONDS = 6 * 60 * 60
_RESOLUTION_CACHE: dict[str, tuple[float, Optional[dict[str, Any]]]] = {}
_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/MCX.json.gz"
_INSTRUMENTS_CACHE: tuple[float, list[dict[str, Any]]] = (0.0, [])


def _parse_mcx_future_symbol(symbol: str) -> Optional[tuple[str, int, int]]:
    raw_symbol = str(symbol or "").strip().upper()
    if not raw_symbol.startswith("MCX:") or not raw_symbol.endswith("FUT"):
        return None
    body = raw_symbol.split(":", 1)[-1][:-3]
    if len(body) < 5:
        return None
    year_text = body[-5:-3]
    month_text = body[-3:]
    if not year_text.isdigit():
        return None
    month = _MONTH_TO_NUMBER.get(month_text)
    if month is None:
        return None
    root = canonicalize_commodity_root(body[:-5])
    if not root:
        return None
    return root, 2000 + int(year_text), month


def _parse_expiry(value: Any) -> Optional[date]:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


async def get_upstox_adapter() -> Optional[BrokerAdapter]:
    adapter = get_active_adapter("upstox")
    if adapter:
        return adapter
    if await ensure_upstox_session(force_validate=True):
        return get_active_adapter("upstox")
    return None


async def _load_mcx_instruments() -> list[dict[str, Any]]:
    global _INSTRUMENTS_CACHE

    cached_at, cached_rows = _INSTRUMENTS_CACHE
    if cached_rows and monotonic() - cached_at < _RESOLUTION_TTL_SECONDS:
        return list(cached_rows)

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(_INSTRUMENTS_URL, headers={"Accept-Encoding": "gzip"})
        response.raise_for_status()
        raw_bytes = gzip.decompress(response.content) if response.content[:2] == b"\x1f\x8b" else response.content
        payload = json.loads(raw_bytes.decode("utf-8"))
        rows = [dict(item) for item in list(payload or []) if isinstance(item, dict)]
        _INSTRUMENTS_CACHE = (monotonic(), rows)
        return list(rows)
    except Exception as exc:
        logger.warning(f"[Commodity Upstox] MCX instruments download failed: {exc}")
        return []


def _candidate_rank(
    contract: dict[str, Any],
    *,
    root: str,
    year: int,
    month: int,
) -> tuple[int, int, int, str]:
    expiry = _parse_expiry(contract.get("expiry"))
    candidate_root = canonicalize_commodity_root(
        contract.get("underlying_symbol")
        or contract.get("name")
        or contract.get("short_name")
        or root
    )
    same_root = 0 if candidate_root == root else 1
    same_month = 0 if expiry and expiry.year == year and expiry.month == month else 1
    expiry_distance = abs((expiry.year - year) * 12 + (expiry.month - month)) if expiry else 999
    trading_symbol = str(contract.get("trading_symbol") or "")
    return same_root, same_month, expiry_distance, trading_symbol


async def resolve_upstox_mcx_future(symbol: str) -> Optional[dict[str, Any]]:
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        return None

    cached = _RESOLUTION_CACHE.get(normalized)
    now = monotonic()
    if cached and now - cached[0] < _RESOLUTION_TTL_SECONDS:
        return dict(cached[1]) if cached[1] else None

    parsed = _parse_mcx_future_symbol(normalized)
    if parsed is None:
        _RESOLUTION_CACHE[normalized] = (now, None)
        return None

    root, year, month = parsed
    contracts = await _load_mcx_instruments()

    candidates = [
        dict(contract)
        for contract in list(contracts or [])
        if str(contract.get("exchange") or "").upper() == "MCX"
        and str(contract.get("segment") or "").upper() == "MCX_FO"
        and str(contract.get("instrument_type") or "").upper() == "FUT"
        and str(contract.get("instrument_key") or "").strip()
    ]
    if not candidates:
        _RESOLUTION_CACHE[normalized] = (now, None)
        return None

    best = min(candidates, key=lambda item: _candidate_rank(item, root=root, year=year, month=month))
    resolved = {
        "symbol": normalized,
        "instrument_key": str(best.get("instrument_key") or ""),
        "trading_symbol": str(best.get("trading_symbol") or ""),
        "expiry": str(best.get("expiry") or ""),
        "exchange": str(best.get("exchange") or "MCX"),
        "segment": str(best.get("segment") or "MCX_FO"),
        "lot_size": int(best.get("lot_size") or 0),
    }
    if not resolved["instrument_key"]:
        _RESOLUTION_CACHE[normalized] = (now, None)
        return None

    _RESOLUTION_CACHE[normalized] = (now, resolved)
    return dict(resolved)


async def load_upstox_mcx_quotes(symbols: list[str]) -> dict[str, float]:
    normalized_symbols = [str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()]
    if not normalized_symbols:
        return {}

    adapter = await get_upstox_adapter()
    if adapter is None:
        return {}

    instrument_by_symbol: dict[str, str] = {}
    for symbol in normalized_symbols:
        resolved = await resolve_upstox_mcx_future(symbol)
        instrument_key = str((resolved or {}).get("instrument_key") or "")
        if instrument_key:
            instrument_by_symbol[symbol] = instrument_key

    if not instrument_by_symbol:
        return {}

    try:
        payload = await adapter.get_ltp(list(instrument_by_symbol.values()))
    except Exception as exc:
        logger.warning(f"[Commodity Upstox] LTP fetch failed: {exc}")
        return {}

    quotes: dict[str, float] = {}
    for symbol, instrument_key in instrument_by_symbol.items():
        try:
            value = float(payload.get(instrument_key, 0) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            quotes[symbol] = value
    return quotes
