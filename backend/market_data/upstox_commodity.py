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
    # Verified against the live master (2026-07-12): Upstox stamps MCX expiry
    # epochs at 23:59:59 IST == 18:29:59 UTC of the SAME calendar day
    # (epoch % 86400 == 66599), so the UTC .date() is the correct expiry date.
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


def _fyers_future_symbol(root: str, expiry: date) -> str:
    return f"MCX:{canonicalize_commodity_root(root)}{expiry:%y%b}FUT".upper()


def select_active_mcx_future_contract(
    contracts: list[dict[str, Any]],
    symbol_or_root: str,
    *,
    session_date: date,
) -> Optional[dict[str, Any]]:
    """Select the first contract that remains valid beyond ``session_date``.

    Rolling on the first MCX session whose date reaches the current contract's
    expiry keeps the lane out of an expiring instrument for that session. The
    broker master is authoritative for the actual expiry date; the configured
    symbol contributes only the commodity root.
    """
    parsed = _parse_mcx_future_symbol(symbol_or_root)
    root = canonicalize_commodity_root(
        parsed[0] if parsed else str(symbol_or_root or "").split(":")[-1]
    )
    candidates: list[tuple[date, dict[str, Any]]] = []
    for raw in contracts:
        candidate_root = canonicalize_commodity_root(
            raw.get("underlying_symbol") or raw.get("name") or raw.get("short_name") or ""
        )
        expiry = _parse_expiry(raw.get("expiry"))
        if (
            candidate_root != root
            or expiry is None
            or str(raw.get("exchange") or "").upper() != "MCX"
            or str(raw.get("segment") or "").upper() != "MCX_FO"
            or str(raw.get("instrument_type") or "").upper() != "FUT"
            or not str(raw.get("instrument_key") or "").strip()
        ):
            continue
        candidates.append((expiry, dict(raw)))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], str(item[1].get("trading_symbol") or "")))
    selected = next((item for item in candidates if item[0] > session_date), None)
    if selected is None:
        selected = next((item for item in candidates if item[0] >= session_date), candidates[-1])
    expiry, contract = selected
    return {
        "symbol": _fyers_future_symbol(root, expiry),
        "root": root,
        "instrument_key": str(contract.get("instrument_key") or ""),
        "trading_symbol": str(contract.get("trading_symbol") or ""),
        "expiry": expiry.isoformat(),
        "lot_size": int(contract.get("lot_size") or 0),
    }


async def resolve_active_upstox_mcx_future(
    symbol_or_root: str,
    *,
    session_date: date,
) -> Optional[dict[str, Any]]:
    return select_active_mcx_future_contract(
        await _load_mcx_instruments(),
        symbol_or_root,
        session_date=session_date,
    )


async def get_upstox_adapter() -> Optional[BrokerAdapter]:
    adapter = get_active_adapter("upstox")
    if adapter:
        return adapter
    if await ensure_upstox_session(force_validate=True):
        return get_active_adapter("upstox")
    return None


async def snapshot_mcx_active_contracts() -> int:
    """Archive every currently-active MCX FUTURES contract's metadata.

    Upstox's MCX instruments master is ACTIVE-ONLY (expired contracts are dropped),
    and Upstox does not serve expired MCX history. Snapshotting daily is the only
    Upstox-native way to capture instrument keys + expiries before contracts roll
    off — enabling forward-looking rolling-contract backfill. Forward-only: it
    cannot recover history from before the first snapshot. Idempotent upsert."""
    from db.database import AsyncSessionLocal
    from sqlalchemy import text

    rows = await _load_mcx_instruments()
    payload: list[dict[str, Any]] = []
    for r in rows:
        if (str(r.get("exchange") or "").upper() != "MCX"
                or str(r.get("segment") or "").upper() != "MCX_FO"
                or str(r.get("instrument_type") or "").upper() != "FUT"):
            continue
        key = str(r.get("instrument_key") or "").strip()
        if not key:
            continue
        root = canonicalize_commodity_root(
            r.get("underlying_symbol") or r.get("name") or r.get("short_name") or "")
        expiry = _parse_expiry(r.get("expiry"))
        payload.append({
            "instrument_key": key, "root": root or None,
            "trading_symbol": str(r.get("trading_symbol") or ""),
            "expiry": expiry, "lot_size": int(r.get("lot_size") or 0),
        })
    if not payload:
        return 0
    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS mcx_contract_snapshots (
                instrument_key TEXT PRIMARY KEY,
                root TEXT,
                trading_symbol TEXT,
                expiry DATE,
                lot_size INTEGER,
                first_seen DATE NOT NULL DEFAULT CURRENT_DATE,
                last_seen DATE NOT NULL DEFAULT CURRENT_DATE
            )
        """))
        await session.execute(text("""
            INSERT INTO mcx_contract_snapshots
                (instrument_key, root, trading_symbol, expiry, lot_size, first_seen, last_seen)
            VALUES (:instrument_key, :root, :trading_symbol, :expiry, :lot_size,
                    CURRENT_DATE, CURRENT_DATE)
            ON CONFLICT (instrument_key) DO UPDATE
            SET root = COALESCE(EXCLUDED.root, mcx_contract_snapshots.root),
                trading_symbol = EXCLUDED.trading_symbol,
                expiry = COALESCE(EXCLUDED.expiry, mcx_contract_snapshots.expiry),
                lot_size = EXCLUDED.lot_size,
                last_seen = CURRENT_DATE
        """), payload)
        await session.commit()
    logger.info(f"[Commodity] snapshotted {len(payload)} active MCX futures contracts")
    return len(payload)


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
        # Serve the stale cache rather than []: an empty result makes the
        # resolver fall back to the CONFIGURED (older) contract, which can roll
        # an already-rolled position BACKWARD — a full phantom round trip per
        # CDN flap. Contract lists change ~monthly; stale beats empty.
        if cached_rows:
            age_minutes = (monotonic() - cached_at) / 60.0
        else:
            age_minutes = None
        logger.warning(
            f"[Commodity Upstox] MCX instruments download failed: {exc}"
            + (
                f" — serving stale cache ({len(cached_rows)} rows, {age_minutes:.0f}m old)"
                if cached_rows
                else " — no cache available"
            )
        )
        return list(cached_rows) if cached_rows else []


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
    snapshots = await load_upstox_mcx_quote_snapshots(symbols)
    if snapshots:
        return {
            symbol: float(snapshot.get("price") or 0.0)
            for symbol, snapshot in snapshots.items()
            if float(snapshot.get("price") or 0.0) > 0
        }

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


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


async def load_upstox_mcx_quote_snapshots(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Load MCX futures LTP plus exchange day-change fields from Upstox quotes."""
    normalized_symbols = [str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()]
    if not normalized_symbols:
        return {}

    adapter = await get_upstox_adapter()
    if adapter is None or not hasattr(adapter, "_headers") or not hasattr(adapter, "BASE_URL"):
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
        # Route through the adapter's rate-limited chokepoint (shared
        # UPSTOX_DATA_LIMITER + 429/5xx retry + circuit recording). This poll
        # runs every ~12s while MCX is open — raw httpx here was an unmetered
        # bypass of the shared 8/s · 1800/30min budget.
        if hasattr(adapter, "_get_data_json"):
            payload_json = await adapter._get_data_json(  # type: ignore[attr-defined]
                "/market-quote/quotes",
                params={"instrument_key": ",".join(instrument_by_symbol.values())},
                timeout=10.0,
            )
            data = (payload_json or {}).get("data", {})
        else:  # pragma: no cover — non-Upstox adapter shape
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"{getattr(adapter, 'BASE_URL')}/market-quote/quotes",
                    params={"instrument_key": ",".join(instrument_by_symbol.values())},
                    headers=adapter._headers(),  # type: ignore[attr-defined]
                )
            response.raise_for_status()
            data = response.json().get("data", {})
    except Exception as exc:
        logger.debug(f"[Commodity Upstox] Full quote fetch failed: {exc}")
        return {}
    if not isinstance(data, dict):
        return {}

    snapshots: dict[str, dict[str, Any]] = {}
    values = [dict(item) for item in data.values() if isinstance(item, dict)]
    for symbol, instrument_key in instrument_by_symbol.items():
        payload = next(
            (
                item
                for key, item in data.items()
                if key == instrument_key
                or str(item.get("instrument_token") or item.get("instrument_key") or "") == instrument_key
            ),
            None,
        )
        if payload is None:
            payload = next(
                (
                    item
                    for item in values
                    if str(item.get("symbol") or item.get("trading_symbol") or "").upper()
                    in {symbol.split(":", 1)[-1], symbol}
                ),
                None,
            )
        if not isinstance(payload, dict):
            continue
        price = _to_float(payload.get("last_price") or payload.get("ltp"))
        net_change = _to_float(payload.get("net_change"))
        previous_close = (
            price - net_change
            if price is not None and net_change is not None
            else _to_float((payload.get("ohlc") or {}).get("close"))
        )
        change_pct = (
            (net_change / previous_close) * 100.0
            if net_change is not None and previous_close not in (None, 0)
            else None
        )
        if price is None or price <= 0:
            continue
        snapshots[symbol] = {
            "price": price,
            "previous_close": previous_close,
            "change": net_change,
            "change_pct": change_pct,
            "source": "upstox_full_quote",
        }
    return snapshots
