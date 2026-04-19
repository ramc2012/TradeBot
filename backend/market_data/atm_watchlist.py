"""ATM CE/PE watchlist builder with live metrics and lightweight persistence."""
from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from loguru import logger
from sqlalchemy.exc import ProgrammingError
from sqlalchemy import text

from analytics.technicals import latest_macd_rsi
from analysis.instruments import (
    INDEX_INSTRUMENT_KEYS,
    get_fo_market,
    get_monthly_expiry,
    get_index_monthly_expiry,
)
from api.routers.auth import ensure_fyers_session, ensure_upstox_session, get_active_adapter
from brokers.base import BrokerAdapter, OptionChain, OptionChainEntry
from db.database import AsyncSessionLocal
from db.redis_client import get_redis
from market_data.fo_universe_bootstrap import ensure_fo_underlying_catalog
from market_data.option_history import option_history_service


UTC = timezone.utc
DEFAULT_WATCHLIST_TTL = 900
DEFAULT_EXPIRY_TTL = 300
DEFAULT_PARTIAL_TTL = 900
DEFAULT_BUILD_LOCK_TTL = 900
WATCHLIST_CACHE_VERSION = "v5"
SYMBOL_EXPIRY_CACHE_VERSION = "v2"

# ── NSE expiry rules ──────────────────────────────────────────────────────────
# NSE index contracts currently share the same Tuesday expiry ladder. Stocks
# remain monthly-only, using the monthly expiry for the selected contract month.
# BSE indices keep their own native weekday ladders.

def _is_nse_derivatives_symbol(symbol: str) -> bool:
    return symbol in INDEX_INSTRUMENT_KEYS and symbol not in {"SENSEX", "BANKEX"}


def _normalize_nse_expiry_ladder(expiries: list[str]) -> list[str]:
    normalized: set[str] = set()
    for raw_expiry in expiries:
        try:
            parsed = date.fromisoformat(str(raw_expiry))
        except (TypeError, ValueError):
            continue
        monthly = get_monthly_expiry(parsed.year, parsed.month)
        if parsed > monthly:
            parsed = monthly
        normalized.add(parsed.isoformat())
    return sorted(normalized)

def _nearest_monthly_expiry() -> date:
    """Return the nearest upcoming NSE monthly expiry."""
    today = date.today()
    monthly = get_monthly_expiry(today.year, today.month)
    if today > monthly:
        nm = today.replace(day=28) + timedelta(days=4)
        monthly = get_monthly_expiry(nm.year, nm.month)
    return monthly


def _nearest_index_expiry(symbol: str) -> date:
    """
    Return the nearest upcoming (or today's) monthly expiry for a specific index.

    NSE indices now share Tuesday monthly expiry. BSE indices keep their native
    weekday. This function returns the last occurrence of that expiry weekday in
    the current (or next) month, adjusted backward past market holidays.
    Used as a FALLBACK when broker data is unavailable.
    """
    today = date.today()
    monthly = get_index_monthly_expiry(symbol, today.year, today.month)
    if today > monthly:
        nm = today.replace(day=28) + timedelta(days=4)
        monthly = get_index_monthly_expiry(symbol, nm.year, nm.month)
    return monthly


def _stock_monthly_for_selected_expiry(selected_expiry: date) -> date:
    """Resolve stocks to the monthly expiry for the selected expiry month."""
    return get_monthly_expiry(selected_expiry.year, selected_expiry.month)


def _index_monthly_for_selected_expiry(symbol: str, selected_expiry: date) -> date:
    """Resolve an index to its monthly expiry for the selected expiry month."""
    return get_index_monthly_expiry(symbol, selected_expiry.year, selected_expiry.month)


def _nearest_monthly_from_expiry_list(expiries: list[str]) -> Optional[date]:
    """
    Given a list of ISO-format expiry dates from the broker, return the nearest
    upcoming monthly expiry.

    Monthly = the LAST expiry in each calendar month (weekly series + monthly series
    always have the monthly contract as the final entry for that month).

    Returns the first such date that is >= today, or None if the list is empty.
    """
    if not expiries:
        return None
    today = date.today()
    # Parse all dates, keep future/today ones
    parsed: list[date] = []
    for e in expiries:
        try:
            d = date.fromisoformat(e)
            if d >= today:
                parsed.append(d)
        except ValueError:
            continue
    if not parsed:
        return None
    # Group by (year, month) — monthly = max date per group
    from itertools import groupby
    from operator import attrgetter
    grouped: dict[tuple[int, int], date] = {}
    for d in sorted(parsed):
        key = (d.year, d.month)
        grouped[key] = d  # last one (max) because we iterate sorted
    # Return the earliest monthly that is >= today
    monthlies = sorted(grouped.values())
    return monthlies[0] if monthlies else None


def _monthly_expiries_from_list(expiries: list[str]) -> list[date]:
    parsed: list[date] = []
    for expiry in expiries:
        try:
            parsed.append(date.fromisoformat(str(expiry)))
        except ValueError:
            continue
    if not parsed:
        return []
    grouped: dict[tuple[int, int], date] = {}
    for item in sorted(parsed):
        grouped[(item.year, item.month)] = item
    return sorted(grouped.values())

INDEX_FYERS_SYMBOLS = {
    # NSE indices
    "NIFTY":      "NSE:NIFTY50-INDEX",
    "BANKNIFTY":  "NSE:NIFTYBANK-INDEX",
    "FINNIFTY":   "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
    "NIFTYNXT50": "NSE:NIFTYNXT50-INDEX",
    # BSE indices
    "SENSEX":     "BSE:SENSEX-INDEX",
    "BANKEX":     "BSE:BANKEX-INDEX",
}

_FYERS_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


@dataclass(frozen=True)
class UnderlyingMeta:
    symbol: str
    kind: str
    spot_instrument_key: str
    underlying_key: str


DEFAULT_INDEX_UNDERLYINGS: tuple[UnderlyingMeta, ...] = (
    UnderlyingMeta(
        symbol="NIFTY",
        kind="INDEX",
        spot_instrument_key=INDEX_INSTRUMENT_KEYS["NIFTY"],
        underlying_key=INDEX_INSTRUMENT_KEYS["NIFTY"],
    ),
    UnderlyingMeta(
        symbol="BANKNIFTY",
        kind="INDEX",
        spot_instrument_key=INDEX_INSTRUMENT_KEYS["BANKNIFTY"],
        underlying_key=INDEX_INSTRUMENT_KEYS["BANKNIFTY"],
    ),
    UnderlyingMeta(
        symbol="FINNIFTY",
        kind="INDEX",
        spot_instrument_key=INDEX_INSTRUMENT_KEYS["FINNIFTY"],
        underlying_key=INDEX_INSTRUMENT_KEYS["FINNIFTY"],
    ),
    UnderlyingMeta(
        symbol="MIDCPNIFTY",
        kind="INDEX",
        spot_instrument_key=INDEX_INSTRUMENT_KEYS["MIDCPNIFTY"],
        underlying_key=INDEX_INSTRUMENT_KEYS["MIDCPNIFTY"],
    ),
    UnderlyingMeta(
        symbol="SENSEX",
        kind="INDEX",
        spot_instrument_key=INDEX_INSTRUMENT_KEYS["SENSEX"],
        underlying_key=INDEX_INSTRUMENT_KEYS["SENSEX"],
    ),
)


class ATMWatchlistService:
    """Build an all-F&O ATM call/put watchlist using live chain data."""

    # Shared semaphore across all concurrent watchlist builds to cap total
    # Fyers/Upstox option-chain requests at 2 simultaneous (stays well under 10/s)
    _chain_semaphore: asyncio.Semaphore = asyncio.Semaphore(2)

    @staticmethod
    def _persisted_watchlist_query(include_underlying_lot_size: bool) -> str:
        lot_size_select = (
            "catalog.lot_size,"
            if include_underlying_lot_size
            else "NULL::INTEGER AS lot_size,"
        )
        return f"""
            WITH latest_underlying AS (
                SELECT DISTINCT ON (underlying)
                    underlying,
                    kind,
                    expiry,
                    strike,
                    source_broker,
                    underlying_price,
                    time
                FROM atm_option_watchlist_snapshots
                WHERE expiry = :expiry
                ORDER BY underlying, time DESC
            )
            SELECT
                latest.underlying,
                latest.kind,
                latest.expiry,
                latest.strike,
                latest.source_broker,
                latest.underlying_price,
                {lot_size_select}
                ce.instrument_key AS ce_instrument_key,
                ce.trading_symbol AS ce_trading_symbol,
                ce.ltp AS ce_ltp,
                ce.prev_close AS ce_prev_close,
                ce.change AS ce_change,
                ce.change_pct AS ce_change_pct,
                ce.oi AS ce_oi,
                ce.prev_oi AS ce_prev_oi,
                ce.oi_change AS ce_oi_change,
                ce.oi_change_pct AS ce_oi_change_pct,
                ce.volume AS ce_volume,
                ce.iv AS ce_iv,
                ce.macd AS ce_macd,
                ce.macd_signal AS ce_macd_signal,
                ce.macd_histogram AS ce_macd_histogram,
                ce.rsi AS ce_rsi,
                pe.instrument_key AS pe_instrument_key,
                pe.trading_symbol AS pe_trading_symbol,
                pe.ltp AS pe_ltp,
                pe.prev_close AS pe_prev_close,
                pe.change AS pe_change,
                pe.change_pct AS pe_change_pct,
                pe.oi AS pe_oi,
                pe.prev_oi AS pe_prev_oi,
                pe.oi_change AS pe_oi_change,
                pe.oi_change_pct AS pe_oi_change_pct,
                pe.volume AS pe_volume,
                pe.iv AS pe_iv,
                pe.macd AS pe_macd,
                pe.macd_signal AS pe_macd_signal,
                pe.macd_histogram AS pe_macd_histogram,
                pe.rsi AS pe_rsi
            FROM latest_underlying latest
            LEFT JOIN fo_underlying_catalog catalog
              ON catalog.symbol = latest.underlying
            LEFT JOIN LATERAL (
                SELECT instrument_key, trading_symbol, ltp, prev_close, change, change_pct,
                       oi, prev_oi, oi_change, oi_change_pct, volume, iv,
                       macd, macd_signal, macd_histogram, rsi
                FROM atm_option_watchlist_snapshots
                WHERE underlying = latest.underlying
                  AND expiry = latest.expiry
                  AND strike = latest.strike
                  AND option_type = 'CE'
                ORDER BY time DESC
                LIMIT 1
            ) ce ON TRUE
            LEFT JOIN LATERAL (
                SELECT instrument_key, trading_symbol, ltp, prev_close, change, change_pct,
                       oi, prev_oi, oi_change, oi_change_pct, volume, iv,
                       macd, macd_signal, macd_histogram, rsi
                FROM atm_option_watchlist_snapshots
                WHERE underlying = latest.underlying
                  AND expiry = latest.expiry
                  AND strike = latest.strike
                  AND option_type = 'PE'
                ORDER BY time DESC
                LIMIT 1
            ) pe ON TRUE
            ORDER BY CASE WHEN latest.kind = 'INDEX' THEN 0 ELSE 1 END, latest.underlying
        """

    async def get_expiries(self, selected_expiry: Optional[str] = None) -> dict[str, Any]:
        redis = await get_redis()
        cache_key = f"atm_watchlist:expiries:{WATCHLIST_CACHE_VERSION}:{selected_expiry or 'default'}"
        cached = await redis.get(cache_key)
        if cached:
            return json.loads(cached)

        underlyings = await self._load_underlyings()
        representative = [
            row for row in underlyings
            if row.symbol in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "TCS"}
        ]
        if not representative:
            representative = underlyings[:10]

        fyers_adapter = get_active_adapter("fyers")
        if fyers_adapter is None and await ensure_fyers_session(force_validate=True):
            fyers_adapter = get_active_adapter("fyers")
        upstox_adapter = await self._get_upstox_adapter()

        fyers_failed = False
        used_upstox_fallback = False
        used_catalog_fallback = False

        async def fetch_expiries(meta: UnderlyingMeta) -> list[str]:
            nonlocal fyers_failed, used_upstox_fallback, used_catalog_fallback
            persisted_expiries = await self._load_persisted_expiries_for_symbol(meta.symbol)
            live_source = "none"
            expiries = await self._get_broker_expiries_for_symbol(
                meta,
                upstox_adapter,
                fyers_adapter,
            )
            if expiries:
                if upstox_adapter is not None:
                    try:
                        contracts = await upstox_adapter.get_option_contracts(meta.underlying_key)
                        upstox_expiries = sorted({str(row.get("expiry")) for row in contracts if row.get("expiry")})
                        if upstox_expiries:
                            live_source = "upstox"
                            expiries = upstox_expiries
                    except Exception as exc:
                        logger.debug(f"[ATM watchlist] Upstox expiry discovery failed for {meta.symbol}: {exc}")
                if live_source == "none" and fyers_adapter is not None:
                    try:
                        contracts = await fyers_adapter.get_option_contracts(self._to_fyers_symbol(meta))
                        fyers_expiries = sorted({str(row.get("expiry")) for row in contracts if row.get("expiry")})
                        if fyers_expiries:
                            live_source = "fyers"
                            expiries = fyers_expiries
                    except Exception as exc:
                        fyers_failed = True
                        logger.debug(f"[ATM watchlist] Expiry discovery failed for {meta.symbol}: {exc}")

                if live_source == "upstox":
                    used_upstox_fallback = True
                elif live_source == "none":
                    used_catalog_fallback = True
            return expiries

        expiry_results = await asyncio.gather(*(fetch_expiries(meta) for meta in representative))
        # Map symbol → broker expiry list.
        sym_to_expiries: dict[str, list[str]] = {
            meta.symbol: exp_list
            for meta, exp_list in zip(representative, expiry_results)
        }
        nifty_expiries = sym_to_expiries.get("NIFTY", [])
        if nifty_expiries:
            nifty_expiries = _normalize_nse_expiry_ladder(nifty_expiries)
        expiries = list(nifty_expiries) if nifty_expiries else sorted({expiry for items in expiry_results for expiry in items if expiry})
        _today = date.today()
        today = _today.isoformat()

        # NIFTY's live ladder is the canonical dropdown scope for the NSE board.
        # Normalize any stale broker monthly dates back onto the official NSE
        # Tuesday monthly schedule before exposing them to the UI.
        if nifty_expiries:
            expiries = _normalize_nse_expiry_ladder(expiries)
        live_nifty_month = _nearest_monthly_from_expiry_list(nifty_expiries)
        if live_nifty_month is None:
            live_nifty_month = _nearest_index_expiry("NIFTY")
        selected_scope_date = self._parse_expiry(selected_expiry) or live_nifty_month
        monthly_expiry_iso = get_monthly_expiry(selected_scope_date.year, selected_scope_date.month).isoformat()

        _index_monthlies: dict[str, str] = {}
        for _sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"):
            if _is_nse_derivatives_symbol(_sym):
                _index_monthlies[_sym] = monthly_expiry_iso
            else:
                _index_monthlies[_sym] = _index_monthly_for_selected_expiry(_sym, selected_scope_date).isoformat()
        stock_monthly_expiry_iso = _stock_monthly_for_selected_expiry(selected_scope_date).isoformat()

        # Always ensure NIFTY monthly is in the list — prevents empty dropdown when
        # brokers are rate-limited (watchlistExpiry stays "" → enabled:false otherwise).
        if monthly_expiry_iso not in expiries:
            expiries = sorted(set(expiries) | {monthly_expiry_iso})

        default_expiry = (
            monthly_expiry_iso
            if monthly_expiry_iso in expiries
            else next((expiry for expiry in expiries if expiry >= today), expiries[0] if expiries else None)
        )
        detail: Optional[str] = None
        source = "fyers"
        if used_upstox_fallback:
            source = "upstox"
            detail = "Fyers is rate-limited for expiry discovery right now, so watchlist expiries are coming from Upstox."
        elif used_catalog_fallback and fyers_adapter is None and upstox_adapter is None:
            source = "catalog"
            detail = "Live brokers are offline, so the watchlist is using the saved expiry catalog."
        elif used_catalog_fallback:
            source = "catalog"
            detail = "Live expiry refresh was unavailable for part of the universe, so the watchlist reused saved expiry metadata."
        elif fyers_adapter is None and upstox_adapter is not None:
            source = "upstox"
            detail = "Fyers is not connected, so expiries are resolved through Upstox."
        elif fyers_failed and not expiries:
            detail = "Expiry discovery is temporarily rate-limited on Fyers."
        if not default_expiry and monthly_expiry_iso:
            default_expiry = monthly_expiry_iso
            detail = (detail + " " if detail else "") + f"Using inferred monthly expiry {monthly_expiry_iso} until live discovery recovers."
        payload = {
            "expiries": expiries,
            "default_expiry": default_expiry,
            "monthly_expiry": monthly_expiry_iso,
            "source": source,
            "detail": detail,
            # NSE indices/stocks share the same monthly expiry. BSE indices keep
            # their native expiry ladder.
            "expiry_scope_note": (
                f"NIFTY {_index_monthlies.get('NIFTY', monthly_expiry_iso)} · "
                f"BNKN {_index_monthlies.get('BANKNIFTY', '?')} · "
                f"FINN {_index_monthlies.get('FINNIFTY', '?')} · "
                f"MIDCP {_index_monthlies.get('MIDCPNIFTY', '?')} · "
                f"SENSEX {_index_monthlies.get('SENSEX', '?')} · "
                f"Stocks {stock_monthly_expiry_iso}"
            ),
            "index_monthlies": _index_monthlies,
        }
        await redis.set(cache_key, json.dumps(payload), ex=DEFAULT_EXPIRY_TTL)
        return payload

    async def get_watchlist(self, expiry: Optional[str] = None) -> dict[str, Any]:
        expiry_payload = await self.get_expiries(expiry)
        selected_expiry = expiry or expiry_payload.get("default_expiry")
        selected_expiry_date = self._parse_expiry(selected_expiry)
        if not selected_expiry or selected_expiry_date is None:
            return {
                "expiry": None,
                "rows": [],
                "summary": {"total_rows": 0, "ce_ready": 0, "pe_ready": 0},
                "source": "none",
                "detail": expiry_payload.get("detail") or "No expiry is available for the ATM watchlist.",
                "timestamp": datetime.now(UTC).isoformat(),
            }

        redis = await get_redis()
        cache_key = f"atm_watchlist:{WATCHLIST_CACHE_VERSION}:{selected_expiry}"
        partial_key = f"atm_watchlist:partial:{WATCHLIST_CACHE_VERSION}:{selected_expiry}"
        build_lock_key = f"atm_watchlist:building:{WATCHLIST_CACHE_VERSION}:{selected_expiry}"
        cached = await redis.get(cache_key)
        if cached:
            cached_payload = json.loads(cached)
            return cached_payload

        fyers_adapter = get_active_adapter("fyers")
        if fyers_adapter is None and await ensure_fyers_session(force_validate=True):
            fyers_adapter = get_active_adapter("fyers")
        upstox_adapter = await self._get_upstox_adapter()

        underlyings = await self._load_underlyings()

        partial_cache = await redis.get(partial_key)
        prior_rows: dict[str, dict] = {}
        loaded_from_persisted = False
        if partial_cache:
            for row in json.loads(partial_cache):
                prior_rows[row["underlying"]] = row
        else:
            for row in await self._load_persisted_watchlist_rows(selected_expiry, underlyings):
                prior_rows[row["underlying"]] = row
            loaded_from_persisted = bool(prior_rows)

        if upstox_adapter is None and fyers_adapter is None:
            if prior_rows:
                rows = sorted(prior_rows.values(), key=lambda r: (r["kind"] != "INDEX", r["underlying"]))
                payload = {
                    "expiry": selected_expiry,
                    "rows": rows,
                    "summary": {
                        "total_rows": len(rows),
                        "ce_ready": sum(1 for row in rows if row.get("ce")),
                        "pe_ready": sum(1 for row in rows if row.get("pe")),
                        "fyers_rows": sum(1 for row in rows if row.get("live_source") == "fyers"),
                        "upstox_rows": sum(1 for row in rows if row.get("live_source") == "upstox"),
                    },
                    "source": "snapshot",
                    "detail": "Live brokers are offline. Showing the last saved ATM watchlist board.",
                    "build_status": "ready",
                    "timestamp": datetime.now(UTC).isoformat(),
                }
                await redis.set(cache_key, json.dumps(payload), ex=DEFAULT_WATCHLIST_TTL)
                return payload
            payload = {
                "expiry": selected_expiry,
                "rows": [],
                "summary": {"total_rows": 0, "ce_ready": 0, "pe_ready": 0},
                "source": "none",
                "detail": "Connect Fyers or Upstox to build the ATM watchlist.",
                "timestamp": datetime.now(UTC).isoformat(),
            }
            await redis.set(cache_key, json.dumps(payload), ex=30)
            return payload

        pending = [m for m in underlyings if m.symbol not in prior_rows]
        logger.info(
            f"[ATM watchlist] {len(prior_rows)} cached, {len(pending)} to fetch for {selected_expiry}"
        )

        def _build_payload(rows: list[dict[str, Any]], detail: Optional[str], build_status: str) -> dict[str, Any]:
            return {
                "expiry": selected_expiry,
                "rows": rows,
                "summary": {
                    "total_rows": len(rows),
                    "ce_ready": sum(1 for row in rows if row.get("ce")),
                    "pe_ready": sum(1 for row in rows if row.get("pe")),
                    "fyers_rows": sum(1 for row in rows if row.get("live_source") == "fyers"),
                    "upstox_rows": sum(1 for row in rows if row.get("live_source") == "upstox"),
                },
                "source": "fyers" if fyers_adapter else "upstox",
                "detail": detail,
                "build_status": build_status,
                "timestamp": datetime.now(UTC).isoformat(),
            }

        async def build(meta: UnderlyingMeta, delay: float = 0.0) -> Optional[dict[str, Any]]:
            if delay:
                await asyncio.sleep(delay)
            async with ATMWatchlistService._chain_semaphore:
                try:
                    return await self._build_row(
                        meta,
                        selected_expiry,
                        selected_expiry_date,
                        upstox_adapter,
                        fyers_adapter,
                    )
                except Exception as exc:
                    logger.warning(f"[ATM watchlist] Failed to build {meta.symbol}: {exc}")
                    return None

        async def _bg_build_and_cache(
            pending_metas: list,
            prior: dict[str, dict],
            all_underlyings: list,
            interim_status: str,
            detail_prefix: Optional[str],
        ) -> None:
            completed = 0
            for meta in pending_metas:
                row = await build(meta)
                completed += 1
                if row:
                    prior[row["underlying"]] = row
                rows = sorted(prior.values(), key=lambda r: (r["kind"] != "INDEX", r["underlying"]))
                await redis.set(partial_key, json.dumps(rows), ex=DEFAULT_PARTIAL_TTL)
                remaining_count = max(len(all_underlyings) - len(rows), 0)
                detail_msg = detail_prefix
                if interim_status == "building" and remaining_count > 0:
                    detail_msg = (
                        (detail_msg + " " if detail_msg else "")
                        + f"Building {remaining_count} remaining symbols in background."
                    )
                interim_payload = _build_payload(rows, detail_msg, interim_status if interim_status == "building" else "ready")
                await redis.set(cache_key, json.dumps(interim_payload), ex=DEFAULT_WATCHLIST_TTL)
                if completed % 10 == 0 or row:
                    logger.info(
                        f"[ATM watchlist] BG progress: {len(rows)}/{len(all_underlyings)} rows for {selected_expiry}"
                    )
                if completed < len(pending_metas):
                    await asyncio.sleep(0.5)

            rows = sorted(prior.values(), key=lambda r: (r["kind"] != "INDEX", r["underlying"]))
            logger.info(
                f"[ATM watchlist] BG build done: {len(rows)}/{len(all_underlyings)} rows for {selected_expiry}"
            )
            build_complete = len(rows) >= len(all_underlyings)
            if build_complete:
                await redis.delete(partial_key)
                await redis.delete(build_lock_key)
            detail_msg = detail_prefix
            if not build_complete:
                detail_msg = (
                    (detail_msg + " " if detail_msg else "")
                    + f"Building {len(all_underlyings) - len(rows)} remaining symbols in background."
                )
            if build_complete:
                _payload = _build_payload(rows, detail_msg, "ready")
                await redis.set(cache_key, json.dumps(_payload), ex=DEFAULT_WATCHLIST_TTL)
            await self._archive_expired_contracts()

        if prior_rows:
            rows = sorted(prior_rows.values(), key=lambda r: (r["kind"] != "INDEX", r["underlying"]))
            detail_msg = None if fyers_adapter else "Fyers is not connected, using Upstox live chain data."
            payload_status = "ready"
            background_targets = pending
            if loaded_from_persisted and not partial_cache:
                background_targets = underlyings
                detail_msg = (
                    (detail_msg + " " if detail_msg else "")
                    + "Showing the last saved ATM watchlist while live refresh updates in background."
                )
            elif pending:
                payload_status = "building"
                detail_msg = (
                    (detail_msg + " " if detail_msg else "")
                    + f"Building {len(pending)} remaining symbols in background."
                )
            if background_targets:
                already_building = await redis.get(build_lock_key)
                if not already_building:
                    await redis.set(build_lock_key, "1", ex=DEFAULT_BUILD_LOCK_TTL)
                    asyncio.ensure_future(
                        _bg_build_and_cache(
                            background_targets,
                            dict(prior_rows),
                            underlyings,
                            "building" if payload_status == "building" else "ready",
                            None if fyers_adapter else "Fyers is not connected, using Upstox live chain data.",
                        )
                    )
            partial_payload = _build_payload(rows, detail_msg, payload_status)
            await redis.set(cache_key, json.dumps(partial_payload), ex=DEFAULT_WATCHLIST_TTL)
            return partial_payload

        priority_metas = [meta for meta in pending if meta.kind == "INDEX"]
        seed_metas = priority_metas or pending[: min(8, len(pending))]
        seed_tasks = [build(meta, delay=i * 0.05) for i, meta in enumerate(seed_metas)]
        seed_rows = [row for row in await asyncio.gather(*seed_tasks) if row]
        for row in seed_rows:
            prior_rows[row["underlying"]] = row
        rows = sorted(prior_rows.values(), key=lambda r: (r["kind"] != "INDEX", r["underlying"]))
        remaining = [meta for meta in pending if meta.symbol not in prior_rows]
        await redis.set(partial_key, json.dumps(rows), ex=300)

        detail_msg = None if fyers_adapter else "Fyers is not connected, using Upstox live chain data."
        if remaining:
            already_building = await redis.get(build_lock_key)
            if not already_building:
                await redis.set(build_lock_key, "1", ex=DEFAULT_BUILD_LOCK_TTL)
                asyncio.ensure_future(
                    _bg_build_and_cache(
                        remaining,
                        dict(prior_rows),
                        underlyings,
                        "building",
                        None if fyers_adapter else "Fyers is not connected, using Upstox live chain data.",
                    )
                )
            detail_msg = (
                (detail_msg + " " if detail_msg else "")
                + f"Building {len(remaining)} remaining symbols in background."
            )
        else:
            await redis.delete(partial_key)
            await self._archive_expired_contracts()

        payload = _build_payload(rows, detail_msg, "building" if remaining else "ready")
        await redis.set(cache_key, json.dumps(payload), ex=DEFAULT_WATCHLIST_TTL)
        return payload

    async def _build_row(
        self,
        meta: UnderlyingMeta,
        expiry: str,
        expiry_date: date,
        upstox_adapter: Optional[BrokerAdapter],
        fyers_adapter: Optional[BrokerAdapter],
    ) -> Optional[dict[str, Any]]:
        # ── Expiry resolution ──────────────────────────────────────────────────
        # Stocks are monthly-only, so they always resolve to the monthly expiry
        # of the selected contract month. NSE indices honour the selected
        # expiry directly. BSE indices continue to use their native monthly.
        if meta.kind != "INDEX":
            monthly = _stock_monthly_for_selected_expiry(expiry_date)
            expiry = monthly.isoformat()
            expiry_date = monthly
        elif not _is_nse_derivatives_symbol(meta.symbol):
            native_monthly = _index_monthly_for_selected_expiry(meta.symbol, expiry_date)
            if native_monthly.isoformat() != expiry:
                logger.debug(
                    f"[ATM watchlist] {meta.symbol} expiry native-monthly resolved: "
                    f"{expiry} → {native_monthly.isoformat()}"
                )
            expiry = native_monthly.isoformat()
            expiry_date = native_monthly

        # Contract metadata comes from Upstox when live, but must continue to
        # resolve from the persisted catalog when Upstox is offline.
        contracts = await self._get_contracts_for_expiry(meta, expiry, upstox_adapter)

        chain: Optional[OptionChain] = None
        live_source = "upstox"
        fyers_symbol = self._to_fyers_symbol(meta)
        if fyers_adapter:
            try:
                chain = await fyers_adapter.get_option_chain(fyers_symbol, expiry)
                if chain.entries:
                    live_source = "fyers"
            except Exception as exc:
                logger.debug(f"[ATM watchlist] Fyers chain failed for {meta.symbol}: {exc}")

        if chain is None or not chain.entries:
            if upstox_adapter is None:
                return None
            try:
                chain = await upstox_adapter.get_option_chain(meta.underlying_key, expiry)
                live_source = "upstox"
            except Exception as exc:
                logger.debug(f"[ATM watchlist] Upstox chain failed for {meta.symbol}: {exc}")
                return None

        if not chain.entries:
            return None

        spot_price = float(chain.spot_price or 0.0)
        strikes = sorted({float(entry.strike) for entry in chain.entries})
        if not strikes:
            return None
        atm_strike = min(strikes, key=lambda strike: abs(strike - spot_price))
        ce_entry = next((entry for entry in chain.entries if entry.option_type == "CE" and float(entry.strike) == atm_strike), None)
        pe_entry = next((entry for entry in chain.entries if entry.option_type == "PE" and float(entry.strike) == atm_strike), None)
        if not ce_entry and not pe_entry:
            return None

        contract_map = {
            (float(contract["strike_price"]), str(contract["instrument_type"])): contract
            for contract in contracts
        }
        ce_contract = contract_map.get((atm_strike, "CE"))
        pe_contract = contract_map.get((atm_strike, "PE"))

        if (
            live_source == "fyers"
            and not self._entries_match_expiry((ce_entry, pe_entry), expiry_date)
            and upstox_adapter is not None
        ):
            logger.debug(
                f"[ATM watchlist] Fyers returned mismatched expiry contracts for {meta.symbol} {expiry}; "
                "falling back to Upstox for the selected expiry."
            )
            _upstox_succeeded = False
            try:
                chain = await upstox_adapter.get_option_chain(meta.underlying_key, expiry)
                live_source = "upstox"
                if chain.entries:
                    spot_price = float(chain.spot_price or 0.0)
                    strikes = sorted({float(item.strike) for item in chain.entries})
                    if strikes:
                        atm_strike = min(strikes, key=lambda item: abs(item - spot_price))
                        ce_entry = next(
                            (item for item in chain.entries if item.option_type == "CE" and float(item.strike) == atm_strike),
                            None,
                        )
                        pe_entry = next(
                            (item for item in chain.entries if item.option_type == "PE" and float(item.strike) == atm_strike),
                            None,
                        )
                        ce_contract = contract_map.get((atm_strike, "CE"))
                        pe_contract = contract_map.get((atm_strike, "PE"))
                        _upstox_succeeded = True
            except Exception as exc:
                logger.debug(f"[ATM watchlist] Upstox expiry fallback failed for {meta.symbol}: {exc}")

            if not _upstox_succeeded:
                # Upstox returned nothing or failed — use Fyers' nearest available expiry.
                # The CE/PE entries and atm_strike from Fyers are still valid for live pricing.
                logger.debug(
                    f"[ATM watchlist] Using Fyers nearest-expiry data for {meta.symbol} "
                    f"(requested {expiry}, Upstox unavailable)."
                )
                live_source = "fyers"

        ce_payload = await self._build_option_payload(
            meta,
            expiry,
            expiry_date,
            spot_price,
            atm_strike,
            ce_entry,
            ce_contract,
            live_source,
        )
        pe_payload = await self._build_option_payload(
            meta,
            expiry,
            expiry_date,
            spot_price,
            atm_strike,
            pe_entry,
            pe_contract,
            live_source,
        )

        # Extract lot_size from Upstox contract data (most reliable source).
        # Prefer CE contract; fall back to PE; fall back to None.
        lot_size: Optional[int] = None
        for contract in (ce_contract, pe_contract):
            if contract and contract.get("lot_size"):
                try:
                    lot_size = int(contract["lot_size"])
                    break
                except (TypeError, ValueError):
                    pass

        # Persist to fo_underlying_catalog so resolve_lot_size() can use it later.
        if lot_size:
            await self._persist_lot_size(meta.symbol, lot_size)

        return {
            "underlying": meta.symbol,
            "kind": meta.kind,
            "spot_price": round(spot_price, 2),
            "expiry": expiry,
            "atm_strike": atm_strike,
            "live_source": live_source,
            "fyers_symbol": fyers_symbol,
            "lot_size": lot_size,   # NSE-mandated lot size for this underlying
            "ce": ce_payload,
            "pe": pe_payload,
        }

    async def _build_option_payload(
        self,
        meta: UnderlyingMeta,
        expiry: str,
        expiry_date: date,
        spot_price: float,
        strike: float,
        entry: Optional[OptionChainEntry],
        contract: Optional[dict[str, Any]],
        source_broker: str,
    ) -> Optional[dict[str, Any]]:
        if entry is None:
            return None

        catalog_instrument_key = str((contract or {}).get("instrument_key") or "").strip() or None
        live_instrument_key = str(entry.instrument_key or "").strip() or None
        instrument_key = catalog_instrument_key or live_instrument_key
        trading_symbol = str((contract or {}).get("trading_symbol") or "").strip() or None
        technicals = await self._load_technicals(
            underlying=meta.symbol,
            expiry=expiry_date,
            strike=strike,
            option_type=entry.option_type,
            instrument_key=instrument_key,
            fallback_close=float(entry.ltp or 0.0),
        )
        payload = {
            "strike": strike,
            "option_type": entry.option_type,
            "instrument_key": instrument_key,
            "trading_symbol": trading_symbol,
            "ltp": round(float(entry.ltp or 0.0), 2),
            "prev_close": round(float(entry.prev_close or 0.0), 2) if entry.prev_close is not None else None,
            "change": round(float(entry.ltp or 0.0) - float(entry.prev_close or 0.0), 2)
            if entry.prev_close is not None
            else None,
            "change_pct": round(
                ((float(entry.ltp or 0.0) - float(entry.prev_close or 0.0)) / float(entry.prev_close or 1.0)) * 100.0,
                2,
            ) if entry.prev_close not in (None, 0) else None,
            "oi": int(entry.oi or 0),
            "prev_oi": int(entry.prev_oi or 0) if entry.prev_oi is not None else None,
            "oi_change": int((entry.oi or 0) - int(entry.prev_oi or 0)) if entry.prev_oi is not None else None,
            "oi_change_pct": round(
                (((entry.oi or 0) - int(entry.prev_oi or 0)) / float(entry.prev_oi or 1.0)) * 100.0,
                2,
            ) if entry.prev_oi not in (None, 0) else None,
            "volume": int(entry.volume or 0),
            "iv": round(float(entry.iv or 0.0), 4) if entry.iv is not None else None,
            "delta": round(float(entry.delta), 4) if entry.delta is not None else None,
            "gamma": round(float(entry.gamma), 6) if entry.gamma is not None else None,
            "theta": round(float(entry.theta), 4) if entry.theta is not None else None,
            "vega": round(float(entry.vega), 4) if entry.vega is not None else None,
            **technicals,
        }
        await self._persist_snapshot(
            meta=meta,
            expiry=expiry_date,
            strike=strike,
            spot_price=spot_price,
            option=payload,
            source_broker=source_broker,
        )
        return payload

    async def _persist_lot_size(self, symbol: str, lot_size: int) -> None:
        """Save broker-provided lot_size to fo_underlying_catalog for future lookups."""
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text(
                        """
                        UPDATE fo_underlying_catalog
                        SET lot_size = :lot_size
                        WHERE symbol = :symbol
                          AND (lot_size IS NULL OR lot_size != :lot_size)
                        """
                    ),
                    {"symbol": symbol, "lot_size": lot_size},
                )
                await session.commit()
        except Exception as exc:
            logger.debug(f"[ATM watchlist] lot_size persist failed for {symbol}: {exc}")

    async def _get_broker_expiries_for_symbol(
        self,
        meta: "UnderlyingMeta",
        upstox_adapter: Optional[BrokerAdapter],
        fyers_adapter: Optional[BrokerAdapter] = None,
    ) -> list[str]:
        """
        Fetch all available expiry dates for a symbol directly from the broker.

        Returns a sorted list of ISO date strings (e.g. ["2026-04-28", "2026-05-26", ...]).
        Cached in Redis for 5 minutes per symbol.
        Falls back to empty list if both brokers are unavailable.
        """
        redis = await get_redis()
        cache_key = f"atm_watchlist:sym_expiries:{SYMBOL_EXPIRY_CACHE_VERSION}:{meta.symbol}"
        cached = await redis.get(cache_key)
        if cached:
            return json.loads(cached)

        persisted_expiries = await self._load_persisted_expiries_for_symbol(meta.symbol)
        expiries: list[str] = []

        # Upstox contract metadata is the canonical expiry ladder when available.
        if upstox_adapter is not None and not expiries:
            try:
                contracts = await upstox_adapter.get_option_contracts(meta.underlying_key)
                expiries = sorted({str(row.get("expiry")) for row in contracts if row.get("expiry")})
                if _is_nse_derivatives_symbol(meta.symbol):
                    expiries = _normalize_nse_expiry_ladder(expiries)
                if expiries:
                    await self._persist_expiries_for_symbol(meta.symbol, expiries)
            except Exception as exc:
                logger.debug(f"[ATM watchlist] Upstox expiry fetch failed for {meta.symbol}: {exc}")

        # Fyers is the live fallback when Upstox is unavailable. Persisted
        # ladders are only used when both live sources fail.
        if fyers_adapter is not None and not expiries:
            try:
                fyers_sym = self._to_fyers_symbol(meta)
                contracts = await fyers_adapter.get_option_contracts(fyers_sym)
                expiries = sorted({str(row.get("expiry")) for row in contracts if row.get("expiry")})
                if _is_nse_derivatives_symbol(meta.symbol):
                    expiries = _normalize_nse_expiry_ladder(expiries)
                if expiries:
                    await self._persist_expiries_for_symbol(meta.symbol, expiries)
            except Exception as exc:
                logger.debug(f"[ATM watchlist] Fyers expiry fetch failed for {meta.symbol}: {exc}")

        if expiries:
            await redis.set(cache_key, json.dumps(expiries), ex=300)
            return expiries

        if persisted_expiries:
            if _is_nse_derivatives_symbol(meta.symbol):
                persisted_expiries = _normalize_nse_expiry_ladder(persisted_expiries)
            await redis.set(cache_key, json.dumps(persisted_expiries), ex=DEFAULT_EXPIRY_TTL)
            return persisted_expiries
        return []

    async def _get_contracts_for_expiry(
        self,
        meta: UnderlyingMeta,
        expiry: str,
        upstox_adapter: Optional[BrokerAdapter],
    ) -> list[dict[str, Any]]:
        if upstox_adapter is None:
            return []
        redis = await get_redis()
        cache_key = f"atm_watchlist:contracts:{meta.symbol}:{expiry}"
        cached = await redis.get(cache_key)
        if cached:
            return json.loads(cached)

        normalized: list[dict[str, Any]] = []
        if upstox_adapter is not None:
            try:
                contracts = await upstox_adapter.get_option_contracts(meta.underlying_key, expiry)
                normalized = [
                    {
                        "instrument_key": row.get("instrument_key"),
                        "trading_symbol": row.get("trading_symbol"),
                        "strike_price": float(row.get("strike_price", 0) or 0.0),
                        "instrument_type": row.get("instrument_type"),
                        "expiry": row.get("expiry"),
                        "lot_size": row.get("lot_size"),
                    }
                    for row in contracts
                    if row.get("instrument_key") and row.get("instrument_type") in {"CE", "PE"}
                ]
                if normalized:
                    await self._persist_contracts_for_expiry(meta.symbol, normalized)
            except Exception as exc:
                logger.debug(f"[ATM watchlist] Contract discovery failed for {meta.symbol}: {exc}")

        if not normalized:
            normalized = await self._load_persisted_contracts_for_expiry(meta.symbol, expiry)

        await redis.set(cache_key, json.dumps(normalized), ex=DEFAULT_EXPIRY_TTL)
        return normalized

    async def _load_persisted_expiries_for_symbol(self, symbol: str) -> list[str]:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    SELECT expiry
                    FROM fo_expiry_catalog
                    WHERE underlying = :underlying
                      AND expiry >= CURRENT_DATE
                    ORDER BY expiry ASC
                    """
                ),
                {"underlying": symbol},
            )
            rows = [row.expiry.isoformat() for row in result.fetchall() if row.expiry is not None]
            if rows:
                return rows

            result = await session.execute(
                text(
                    """
                    SELECT DISTINCT expiry
                    FROM fo_contract_catalog
                    WHERE underlying = :underlying
                      AND expiry >= CURRENT_DATE
                    ORDER BY expiry ASC
                    """
                ),
                {"underlying": symbol},
            )
            return [row.expiry.isoformat() for row in result.fetchall() if row.expiry is not None]

    async def _persist_expiries_for_symbol(self, symbol: str, expiries: list[str]) -> None:
        monthly_expiries = _monthly_expiries_from_list(expiries)
        if not monthly_expiries:
            return
        rows = []
        for index, expiry in enumerate(monthly_expiries):
            previous = monthly_expiries[index - 1] if index > 0 else None
            rows.append(
                {
                    "underlying": symbol,
                    "expiry": expiry,
                    "previous_monthly_expiry": previous,
                }
            )
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text(
                        """
                        DELETE FROM fo_expiry_catalog
                        WHERE underlying = :underlying
                          AND expiry >= CURRENT_DATE
                        """
                    ),
                    {"underlying": symbol},
                )
                await session.execute(
                    text(
                        """
                        INSERT INTO fo_expiry_catalog (
                            underlying, expiry, previous_monthly_expiry,
                            created_at, updated_at
                        )
                        VALUES (
                            :underlying, :expiry, :previous_monthly_expiry,
                            NOW(), NOW()
                        )
                        ON CONFLICT (underlying, expiry) DO UPDATE
                        SET previous_monthly_expiry = EXCLUDED.previous_monthly_expiry,
                            updated_at = NOW()
                        """
                    ),
                    rows,
                )
                await session.execute(
                    text(
                        """
                        UPDATE fo_underlying_catalog
                        SET expiries_synced_at = NOW(),
                            updated_at = NOW()
                        WHERE symbol = :underlying
                        """
                    ),
                    {"underlying": symbol},
                )
                await session.commit()
        except Exception as exc:
            logger.debug(f"[ATM watchlist] expiry persist failed for {symbol}: {exc}")

    async def _load_persisted_contracts_for_expiry(self, symbol: str, expiry: str) -> list[dict[str, Any]]:
        expiry_date = self._parse_expiry(expiry)
        if expiry_date is None:
            return []
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    SELECT instrument_key,
                           trading_symbol,
                           strike::float8 AS strike_price,
                           option_type AS instrument_type,
                           expiry,
                           lot_size
                    FROM fo_contract_catalog
                    WHERE underlying = :underlying
                      AND expiry = :expiry
                      AND option_type IN ('CE', 'PE')
                    ORDER BY strike ASC, option_type ASC
                    """
                ),
                {"underlying": symbol, "expiry": expiry_date},
            )
            return [
                {
                    "instrument_key": row.instrument_key,
                    "trading_symbol": row.trading_symbol,
                    "strike_price": float(row.strike_price or 0.0),
                    "instrument_type": row.instrument_type,
                    "expiry": row.expiry.isoformat() if row.expiry is not None else expiry,
                    "lot_size": row.lot_size,
                }
                for row in result.fetchall()
                if row.instrument_key and row.instrument_type in {"CE", "PE"}
            ]

    async def _persist_contracts_for_expiry(self, symbol: str, contracts: list[dict[str, Any]]) -> None:
        rows = []
        for contract in contracts:
            expiry_value = self._parse_expiry(contract.get("expiry"))
            if expiry_value is None:
                continue
            instrument_key = str(contract.get("instrument_key") or "").strip()
            option_type = str(contract.get("instrument_type") or "").strip()
            if not instrument_key or option_type not in {"CE", "PE"}:
                continue
            rows.append(
                {
                    "instrument_key": instrument_key,
                    "trading_symbol": contract.get("trading_symbol"),
                    "underlying": symbol,
                    "market": get_fo_market(symbol),
                    "expiry": expiry_value,
                    "strike": float(contract.get("strike_price") or 0.0),
                    "option_type": option_type,
                    "lot_size": contract.get("lot_size"),
                }
            )
        if not rows:
            return
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text(
                        """
                        INSERT INTO fo_contract_catalog (
                            instrument_key, trading_symbol, underlying, expiry,
                            strike, option_type, lot_size, market, updated_at
                        )
                        VALUES (
                            :instrument_key, :trading_symbol, :underlying, :expiry,
                            :strike, :option_type, :lot_size, :market, NOW()
                        )
                        ON CONFLICT (instrument_key) DO UPDATE
                        SET trading_symbol = COALESCE(EXCLUDED.trading_symbol, fo_contract_catalog.trading_symbol),
                            underlying = EXCLUDED.underlying,
                            expiry = EXCLUDED.expiry,
                            strike = EXCLUDED.strike,
                            option_type = EXCLUDED.option_type,
                            lot_size = COALESCE(EXCLUDED.lot_size, fo_contract_catalog.lot_size),
                            market = COALESCE(EXCLUDED.market, fo_contract_catalog.market),
                            updated_at = NOW()
                        """
                    ),
                    rows,
                )
                await session.commit()
        except Exception as exc:
            logger.debug(f"[ATM watchlist] contract persist failed for {symbol}: {exc}")

    async def _load_technicals(
        self,
        *,
        underlying: str,
        expiry: date,
        strike: float,
        option_type: str,
        instrument_key: Optional[str],
        fallback_close: float,
    ) -> dict[str, Any]:
        closes = await self._load_history_closes(
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            instrument_key=instrument_key,
        )
        if not closes and fallback_close > 0:
            closes = [fallback_close]
        return latest_macd_rsi(closes)

    async def _load_persisted_watchlist_rows(
        self,
        expiry: str,
        underlyings: list[UnderlyingMeta],
    ) -> list[dict[str, Any]]:
        expiry_date = self._parse_expiry(expiry)
        if expiry_date is None:
            return []

        async with AsyncSessionLocal() as session:
            try:
                result = await session.execute(
                    text(self._persisted_watchlist_query(include_underlying_lot_size=True)),
                    {"expiry": expiry_date},
                )
            except ProgrammingError as exc:
                message = str(exc).lower()
                if "catalog.lot_size" not in message and "column lot_size does not exist" not in message:
                    raise
                logger.warning(
                    "[ATM watchlist] fo_underlying_catalog.lot_size is missing; "
                    "reloading persisted rows without underlying lot size."
                )
                await session.rollback()
                result = await session.execute(
                    text(self._persisted_watchlist_query(include_underlying_lot_size=False)),
                    {"expiry": expiry_date},
                )
            rows = result.fetchall()

        meta_by_symbol = {meta.symbol: meta for meta in underlyings}
        payload_rows: list[dict[str, Any]] = []
        for row in rows:
            meta = meta_by_symbol.get(str(row.underlying))
            fyers_symbol = self._to_fyers_symbol(meta) if meta else None

            def _option_payload(prefix: str, option_type: str) -> Optional[dict[str, Any]]:
                ltp = getattr(row, f"{prefix}_ltp")
                instrument_key = getattr(row, f"{prefix}_instrument_key")
                trading_symbol = getattr(row, f"{prefix}_trading_symbol")
                if all(value is None for value in (ltp, instrument_key, trading_symbol)):
                    return None
                return {
                    "strike": float(row.strike),
                    "option_type": option_type,
                    "instrument_key": instrument_key,
                    "trading_symbol": trading_symbol,
                    "ltp": round(float(ltp or 0.0), 2),
                    "prev_close": round(float(getattr(row, f"{prefix}_prev_close") or 0.0), 2)
                    if getattr(row, f"{prefix}_prev_close") is not None else None,
                    "change": round(float(getattr(row, f"{prefix}_change") or 0.0), 2)
                    if getattr(row, f"{prefix}_change") is not None else None,
                    "change_pct": round(float(getattr(row, f"{prefix}_change_pct") or 0.0), 2)
                    if getattr(row, f"{prefix}_change_pct") is not None else None,
                    "oi": int(getattr(row, f"{prefix}_oi") or 0),
                    "prev_oi": int(getattr(row, f"{prefix}_prev_oi") or 0)
                    if getattr(row, f"{prefix}_prev_oi") is not None else None,
                    "oi_change": int(getattr(row, f"{prefix}_oi_change") or 0)
                    if getattr(row, f"{prefix}_oi_change") is not None else None,
                    "oi_change_pct": round(float(getattr(row, f"{prefix}_oi_change_pct") or 0.0), 2)
                    if getattr(row, f"{prefix}_oi_change_pct") is not None else None,
                    "volume": int(getattr(row, f"{prefix}_volume") or 0),
                    "iv": round(float(getattr(row, f"{prefix}_iv") or 0.0), 4)
                    if getattr(row, f"{prefix}_iv") is not None else None,
                    "macd": float(getattr(row, f"{prefix}_macd"))
                    if getattr(row, f"{prefix}_macd") is not None else None,
                    "macd_signal": float(getattr(row, f"{prefix}_macd_signal"))
                    if getattr(row, f"{prefix}_macd_signal") is not None else None,
                    "macd_histogram": float(getattr(row, f"{prefix}_macd_histogram"))
                    if getattr(row, f"{prefix}_macd_histogram") is not None else None,
                    "rsi": float(getattr(row, f"{prefix}_rsi"))
                    if getattr(row, f"{prefix}_rsi") is not None else None,
                }

            payload_rows.append(
                {
                    "underlying": str(row.underlying),
                    "kind": str(row.kind),
                    "spot_price": round(float(row.underlying_price or 0.0), 2),
                    "expiry": expiry,
                    "atm_strike": float(row.strike),
                    "live_source": str(row.source_broker or "snapshot"),
                    "fyers_symbol": fyers_symbol,
                    "lot_size": int(row.lot_size) if row.lot_size is not None else None,
                    "ce": _option_payload("ce", "CE"),
                    "pe": _option_payload("pe", "PE"),
                }
            )
        return payload_rows

    async def _load_history_closes(
        self,
        *,
        underlying: str,
        expiry: date,
        strike: float,
        option_type: str,
        instrument_key: Optional[str],
    ) -> list[float]:
        premium_closes = await option_history_service.load_closes(
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            instrument_key=instrument_key,
            interval="30minute",
            limit=80,
        )
        if premium_closes:
            return premium_closes

        async with AsyncSessionLocal() as session:
            snapshot_rows = await session.execute(
                text("""
                    SELECT ltp
                    FROM atm_option_watchlist_snapshots
                    WHERE underlying = :underlying
                      AND expiry = :expiry
                      AND strike = :strike
                      AND option_type = :option_type
                    ORDER BY time DESC
                    LIMIT 60
                """),
                {
                    "underlying": underlying,
                    "expiry": expiry,
                    "strike": strike,
                    "option_type": option_type,
                },
            )
            return [float(row.ltp) for row in reversed(snapshot_rows.fetchall()) if row.ltp is not None][-60:]

    async def _persist_snapshot(
        self,
        *,
        meta: UnderlyingMeta,
        expiry: date,
        strike: float,
        spot_price: float,
        option: dict[str, Any],
        source_broker: str,
    ) -> None:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO atm_option_watchlist_snapshots (
                        time, underlying, kind, expiry, strike, option_type, source_broker,
                        instrument_key, trading_symbol, underlying_price, ltp, prev_close,
                        change, change_pct, oi, prev_oi, oi_change, oi_change_pct,
                        volume, iv, macd, macd_signal, macd_histogram, rsi
                    )
                    VALUES (
                        NOW(), :underlying, :kind, :expiry, :strike, :option_type, :source_broker,
                        :instrument_key, :trading_symbol, :underlying_price, :ltp, :prev_close,
                        :change, :change_pct, :oi, :prev_oi, :oi_change, :oi_change_pct,
                        :volume, :iv, :macd, :macd_signal, :macd_histogram, :rsi
                    )
                """),
                {
                    "underlying": meta.symbol,
                    "kind": meta.kind,
                    "expiry": expiry,
                    "strike": strike,
                    "option_type": option["option_type"],
                    "source_broker": source_broker,
                    "instrument_key": option.get("instrument_key"),
                    "trading_symbol": option.get("trading_symbol"),
                    "underlying_price": spot_price,
                    "ltp": option.get("ltp"),
                    "prev_close": option.get("prev_close"),
                    "change": option.get("change"),
                    "change_pct": option.get("change_pct"),
                    "oi": option.get("oi"),
                    "prev_oi": option.get("prev_oi"),
                    "oi_change": option.get("oi_change"),
                    "oi_change_pct": option.get("oi_change_pct"),
                    "volume": option.get("volume"),
                    "iv": option.get("iv"),
                    "macd": option.get("macd"),
                    "macd_signal": option.get("macd_signal"),
                    "macd_histogram": option.get("macd_histogram"),
                    "rsi": option.get("rsi"),
                },
            )
            await session.commit()

    async def _archive_expired_contracts(self) -> None:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    WITH expired_snapshots AS (
                        SELECT
                            COALESCE(NULLIF(instrument_key, ''), CONCAT(underlying, ':', expiry::text, ':', strike::text, ':', option_type)) AS archive_key,
                            *
                        FROM atm_option_watchlist_snapshots
                        WHERE expiry < CURRENT_DATE
                    ),
                    latest AS (
                        SELECT DISTINCT ON (archive_key)
                            archive_key,
                            underlying,
                            kind,
                            expiry,
                            strike,
                            option_type,
                            source_broker,
                            trading_symbol,
                            time AS last_seen_at,
                            underlying_price AS last_underlying_price,
                            ltp AS last_ltp,
                            change_pct AS last_change_pct,
                            oi AS last_oi,
                            oi_change AS last_oi_change,
                            volume AS last_volume,
                            iv AS last_iv,
                            macd AS last_macd,
                            macd_signal AS last_macd_signal,
                            macd_histogram AS last_macd_histogram,
                            rsi AS last_rsi
                        FROM expired_snapshots
                        ORDER BY archive_key, time DESC
                    ),
                    summary AS (
                        SELECT
                            archive_key,
                            MIN(time) AS first_seen_at,
                            MAX(time) AS last_seen_at,
                            COUNT(*)::INT AS snapshot_count
                        FROM expired_snapshots
                        GROUP BY archive_key
                    )
                    INSERT INTO expired_option_contract_archive (
                        instrument_key,
                        underlying,
                        kind,
                        expiry,
                        strike,
                        option_type,
                        source_broker,
                        trading_symbol,
                        first_seen_at,
                        last_seen_at,
                        last_underlying_price,
                        last_ltp,
                        last_change_pct,
                        last_oi,
                        last_oi_change,
                        last_volume,
                        last_iv,
                        last_macd,
                        last_macd_signal,
                        last_macd_histogram,
                        last_rsi,
                        snapshot_count,
                        archived_at
                    )
                    SELECT
                        latest.archive_key,
                        latest.underlying,
                        latest.kind,
                        latest.expiry,
                        latest.strike,
                        latest.option_type,
                        latest.source_broker,
                        latest.trading_symbol,
                        summary.first_seen_at,
                        summary.last_seen_at,
                        latest.last_underlying_price,
                        latest.last_ltp,
                        latest.last_change_pct,
                        latest.last_oi,
                        latest.last_oi_change,
                        latest.last_volume,
                        latest.last_iv,
                        latest.last_macd,
                        latest.last_macd_signal,
                        latest.last_macd_histogram,
                        latest.last_rsi,
                        summary.snapshot_count,
                        NOW()
                    FROM latest
                    JOIN summary
                      ON summary.archive_key = latest.archive_key
                    ON CONFLICT (instrument_key) DO UPDATE
                    SET
                        last_seen_at = EXCLUDED.last_seen_at,
                        last_underlying_price = EXCLUDED.last_underlying_price,
                        last_ltp = EXCLUDED.last_ltp,
                        last_change_pct = EXCLUDED.last_change_pct,
                        last_oi = EXCLUDED.last_oi,
                        last_oi_change = EXCLUDED.last_oi_change,
                        last_volume = EXCLUDED.last_volume,
                        last_iv = EXCLUDED.last_iv,
                        last_macd = EXCLUDED.last_macd,
                        last_macd_signal = EXCLUDED.last_macd_signal,
                        last_macd_histogram = EXCLUDED.last_macd_histogram,
                        last_rsi = EXCLUDED.last_rsi,
                        snapshot_count = EXCLUDED.snapshot_count,
                        archived_at = NOW()
                """)
            )
            await session.commit()

    @staticmethod
    def _parse_expiry(expiry: Optional[str]) -> Optional[date]:
        if not expiry:
            return None
        try:
            return date.fromisoformat(str(expiry))
        except ValueError:
            return None

    @staticmethod
    def _parse_fyers_contract_expiry(symbol: Optional[str], reference_year: int) -> Optional[date]:
        raw = str(symbol or "").strip()
        if not raw:
            return None
        raw = raw.split(":")[-1]
        match = re.search(r"(\d{2})([A-Z]{3})\d+(?:\.\d+)?(?:CE|PE)$", raw)
        if not match:
            return None
        day = int(match.group(1))
        month = _FYERS_MONTHS.get(match.group(2))
        if not month:
            return None
        try:
            return date(reference_year, month, day)
        except ValueError:
            return None

    def _entry_matches_expiry(self, entry: Optional[OptionChainEntry], expiry_date: date) -> bool:
        if entry is None:
            return True
        parsed = self._parse_fyers_contract_expiry(entry.instrument_key, expiry_date.year)
        if parsed is None:
            return True
        return parsed == expiry_date

    def _entries_match_expiry(
        self,
        entries: tuple[Optional[OptionChainEntry], Optional[OptionChainEntry]],
        expiry_date: date,
    ) -> bool:
        return all(self._entry_matches_expiry(entry, expiry_date) for entry in entries if entry is not None)

    async def _load_underlyings(self) -> list[UnderlyingMeta]:
        async def _query_rows() -> list[UnderlyingMeta]:
            statement = text("""
                SELECT symbol, kind, spot_instrument_key, underlying_key
                FROM fo_underlying_catalog
                WHERE spot_instrument_key IS NOT NULL
                  AND underlying_key IS NOT NULL
                ORDER BY CASE WHEN kind = 'INDEX' THEN 0 ELSE 1 END, symbol
            """)
            async with AsyncSessionLocal() as session:
                result = await session.execute(statement)
                return [
                    UnderlyingMeta(
                        symbol=str(row.symbol),
                        kind=str(row.kind),
                        spot_instrument_key=str(row.spot_instrument_key),
                        underlying_key=str(row.underlying_key),
                    )
                    for row in result.fetchall()
                ]

        rows = await _query_rows()
        stock_count = sum(1 for row in rows if row.kind == "STOCK")
        if len(rows) <= len(DEFAULT_INDEX_UNDERLYINGS) or stock_count == 0:
            await ensure_fo_underlying_catalog()
            rows = await _query_rows()
        if rows:
            return rows
        logger.warning(
            "[ATM watchlist] fo_underlying_catalog is empty; "
            "falling back to the default index watchlist universe."
        )
        return list(DEFAULT_INDEX_UNDERLYINGS)

    async def _get_upstox_adapter(self) -> Optional[BrokerAdapter]:
        await ensure_upstox_session(force_validate=True)
        adapter = get_active_adapter("upstox")
        if adapter:
            return adapter
        return None

    @staticmethod
    def _to_fyers_symbol(meta: UnderlyingMeta) -> str:
        if meta.kind == "INDEX":
            # BSE indices use BSE: prefix; NSE indices use NSE: prefix
            # Explicit mapping takes precedence over the fallback
            return INDEX_FYERS_SYMBOLS.get(meta.symbol, f"NSE:{meta.symbol}-INDEX")
        return f"NSE:{meta.symbol}-EQ"


atm_watchlist_service = ATMWatchlistService()
