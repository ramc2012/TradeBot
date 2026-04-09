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
from sqlalchemy import text

from analytics.technicals import latest_macd_rsi
from analysis.instruments import (
    INDEX_EXPIRY_WEEKDAY,
    get_monthly_expiry,
    get_index_monthly_expiry,
)
from api.routers.auth import ensure_fyers_session, ensure_upstox_session, get_active_adapter
from brokers.base import BrokerAdapter, OptionChain, OptionChainEntry
from db.database import AsyncSessionLocal
from db.redis_client import get_redis
from market_data.option_history import option_history_service


UTC = timezone.utc
DEFAULT_WATCHLIST_TTL = 120  # 2 min — covers full 211-symbol load time (~55s)
DEFAULT_EXPIRY_TTL = 300

# ── NSE expiry rules ──────────────────────────────────────────────────────────
# Index F&O: weekly expiry every Thursday (NIFTY on Thursdays, BANKNIFTY on
#   Wednesdays, etc.) — broker option-chain data always returns the correct
#   weekly series, so we honour whatever expiry the caller selects.
# Stock F&O: monthly expiry only (last Thursday of the expiry month).
#   Passing a weekly expiry date to the stock chain API returns empty results.
#   We therefore override the expiry to the nearest monthly expiry for stocks.

def _nearest_monthly_expiry() -> date:
    """Return the nearest upcoming (or today's) NSE stock monthly expiry (last Thursday)."""
    today = date.today()
    monthly = get_monthly_expiry(today.year, today.month)
    if today > monthly:
        nm = today.replace(day=28) + timedelta(days=4)
        monthly = get_monthly_expiry(nm.year, nm.month)
    return monthly


def _nearest_index_expiry(symbol: str) -> date:
    """
    Return the nearest upcoming (or today's) monthly expiry for a specific index.

    Each index has a fixed expiry weekday (NIFTY=Thu, BANKNIFTY=Wed, FINNIFTY=Tue,
    MIDCPNIFTY=Mon, SENSEX=Fri).  This function returns the last occurrence of that
    weekday in the current (or next) month, adjusted backward past market holidays.
    Used as a FALLBACK when broker data is unavailable.
    """
    today = date.today()
    monthly = get_index_monthly_expiry(symbol, today.year, today.month)
    if today > monthly:
        nm = today.replace(day=28) + timedelta(days=4)
        monthly = get_index_monthly_expiry(symbol, nm.year, nm.month)
    return monthly


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


class ATMWatchlistService:
    """Build an all-F&O ATM call/put watchlist using live chain data."""

    # Shared semaphore across all concurrent watchlist builds to cap total
    # Fyers/Upstox option-chain requests at 2 simultaneous (stays well under 10/s)
    _chain_semaphore: asyncio.Semaphore = asyncio.Semaphore(2)

    async def get_expiries(self) -> dict[str, Any]:
        redis = await get_redis()
        cache_key = "atm_watchlist:expiries:v3"
        cached = await redis.get(cache_key)
        if cached:
            return json.loads(cached)

        fyers_adapter = get_active_adapter("fyers")
        if fyers_adapter is None and await ensure_fyers_session():
            fyers_adapter = get_active_adapter("fyers")
        upstox_adapter = await self._get_upstox_adapter()
        if fyers_adapter is None and upstox_adapter is None:
            # Even without a broker, always return the computed monthly expiry so the
            # frontend dropdown is never empty and the watchlist query can still fire.
            _today = date.today()
            _monthly = get_monthly_expiry(_today.year, _today.month)
            if _today > _monthly:
                _nm = _today.replace(day=28) + timedelta(days=4)
                _monthly = get_monthly_expiry(_nm.year, _nm.month)
            _monthly_iso = _monthly.isoformat()
            payload = {
                "expiries": [_monthly_iso],
                "default_expiry": _monthly_iso,
                "monthly_expiry": _monthly_iso,
                "source": "none",
                "detail": "Connect Fyers or Upstox to resolve watchlist expiries. Showing computed monthly expiry as fallback.",
                "expiry_scope_note": f"Indices: selected expiry · Stocks: monthly ({_monthly_iso})",
            }
            await redis.set(cache_key, json.dumps(payload), ex=60)
            return payload

        underlyings = await self._load_underlyings()
        representative = [
            row for row in underlyings
            if row.symbol in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "TCS"}
        ]
        if not representative:
            representative = underlyings[:10]

        fyers_failed = False
        used_upstox_fallback = False

        async def fetch_expiries(meta: UnderlyingMeta) -> list[str]:
            nonlocal fyers_failed, used_upstox_fallback
            try:
                if fyers_adapter is not None:
                    contracts = await fyers_adapter.get_option_contracts(self._to_fyers_symbol(meta))
                    expiries = sorted({str(row.get("expiry")) for row in contracts if row.get("expiry")})
                    if expiries:
                        return expiries
            except Exception as exc:
                fyers_failed = True
                logger.debug(f"[ATM watchlist] Expiry discovery failed for {meta.symbol}: {exc}")

            if upstox_adapter is not None:
                try:
                    contracts = await upstox_adapter.get_option_contracts(meta.underlying_key)
                    expiries = sorted({str(row.get("expiry")) for row in contracts if row.get("expiry")})
                    if expiries:
                        used_upstox_fallback = True
                    return expiries
                except Exception as exc:
                    logger.debug(f"[ATM watchlist] Upstox expiry discovery failed for {meta.symbol}: {exc}")
            return []

        expiry_results = await asyncio.gather(*(fetch_expiries(meta) for meta in representative))
        # Map symbol → broker expiry list (for per-index monthly derivation)
        sym_to_expiries: dict[str, list[str]] = {
            meta.symbol: exp_list
            for meta, exp_list in zip(representative, expiry_results)
        }
        expiries = sorted({expiry for items in expiry_results for expiry in items if expiry})
        _today = date.today()
        today = _today.isoformat()

        # Per-index monthlies — derived from broker data (regulation-proof) with computed fallback
        _index_monthlies: dict[str, str] = {}
        for _sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"):
            broker_exp_list = sym_to_expiries.get(_sym, [])
            broker_m = _nearest_monthly_from_expiry_list(broker_exp_list)
            if broker_m is not None:
                _index_monthlies[_sym] = broker_m.isoformat()
            else:
                # Fallback: computed weekday rule (used when broker is unavailable)
                _index_monthlies[_sym] = _nearest_index_expiry(_sym).isoformat()

        # NIFTY monthly is the canonical default for the expiry dropdown.
        # Each index auto-corrects to its own monthly inside _build_row().
        monthly_expiry_iso = _index_monthlies.get("NIFTY") or get_monthly_expiry(
            _today.year, _today.month
        ).isoformat()

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
            # Each index auto-corrects to its own native expiry weekday in _build_row().
            # E.g. selecting NIFTY Apr-30 → FINNIFTY auto-uses Apr-28, BANKNIFTY Apr-29.
            "expiry_scope_note": (
                f"NIFTY {_index_monthlies.get('NIFTY', monthly_expiry_iso)} · "
                f"BNKN {_index_monthlies.get('BANKNIFTY', '?')} · "
                f"FINN {_index_monthlies.get('FINNIFTY', '?')} · "
                f"MIDCP {_index_monthlies.get('MIDCPNIFTY', '?')} · "
                f"SENSEX {_index_monthlies.get('SENSEX', '?')} · "
                f"Stocks {monthly_expiry_iso}"
            ),
            "index_monthlies": _index_monthlies,
        }
        await redis.set(cache_key, json.dumps(payload), ex=DEFAULT_EXPIRY_TTL)
        return payload

    async def get_watchlist(self, expiry: Optional[str] = None) -> dict[str, Any]:
        expiry_payload = await self.get_expiries()
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
        cache_key = f"atm_watchlist:v3:{selected_expiry}"
        cached = await redis.get(cache_key)
        if cached:
            return json.loads(cached)

        fyers_adapter = get_active_adapter("fyers")
        if fyers_adapter is None and await ensure_fyers_session():
            fyers_adapter = get_active_adapter("fyers")
        upstox_adapter = await self._get_upstox_adapter()
        if upstox_adapter is None and fyers_adapter is None:
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

        underlyings = await self._load_underlyings()

        # Load any partially-built rows from a prior partial-cache key
        partial_key = f"atm_watchlist:partial:{selected_expiry}"
        build_lock_key = f"atm_watchlist:building:{selected_expiry}"
        partial_cache = await redis.get(partial_key)
        prior_rows: dict[str, dict] = {}
        if partial_cache:
            for row in json.loads(partial_cache):
                prior_rows[row["underlying"]] = row

        # Only fetch symbols not already in cache
        pending = [m for m in underlyings if m.symbol not in prior_rows]
        logger.info(
            f"[ATM watchlist] {len(prior_rows)} cached, {len(pending)} to fetch for {selected_expiry}"
        )

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
        ) -> None:
            """Background task: finish building remaining rows and update caches."""
            tasks = [build(meta, delay=i * 0.5) for i, meta in enumerate(pending_metas)]
            new_rows = [row for row in await asyncio.gather(*tasks) if row]
            for row in new_rows:
                prior[row["underlying"]] = row
            rows = sorted(prior.values(), key=lambda r: (r["kind"] != "INDEX", r["underlying"]))
            await redis.set(partial_key, json.dumps(rows), ex=300)
            logger.info(
                f"[ATM watchlist] BG build done: {len(rows)}/{len(all_underlyings)} rows for {selected_expiry}"
            )
            if len(rows) >= len(all_underlyings):
                await redis.delete(partial_key)
                await redis.delete(build_lock_key)
            _payload = {
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
                "detail": None if fyers_adapter else "Fyers is not connected, using Upstox live chain data.",
                "timestamp": datetime.now(UTC).isoformat(),
            }
            await redis.set(cache_key, json.dumps(_payload), ex=DEFAULT_WATCHLIST_TTL)
            await self._archive_expired_contracts()

        # ── Fast-return strategy ──────────────────────────────────────────────
        # If we already have partial rows, return them immediately to the caller
        # and kick off the remaining build as a background task (avoids blocking
        # the HTTP request for 60–120 s while all 211 symbols are fetched).
        # A Redis lock prevents spawning multiple concurrent background builds.
        if prior_rows:
            rows = sorted(prior_rows.values(), key=lambda r: (r["kind"] != "INDEX", r["underlying"]))
            detail_msg = None if fyers_adapter else "Fyers is not connected, using Upstox live chain data."
            if pending:
                already_building = await redis.get(build_lock_key)
                if not already_building:
                    await redis.set(build_lock_key, "1", ex=180)
                    asyncio.ensure_future(_bg_build_and_cache(pending, dict(prior_rows), underlyings))
                    detail_msg = (
                        (detail_msg + " " if detail_msg else "")
                        + f"Building {len(pending)} remaining symbols in background — refresh in ~60s."
                    )
            partial_payload = {
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
                "detail": detail_msg,
                "timestamp": datetime.now(UTC).isoformat(),
            }
            return partial_payload

        # ── First-ever build: synchronous, wait for all rows ──────────────────
        # Stagger requests: 1 new req every 0.5s = 2 req/s, shared semaphore keeps
        # total concurrent calls to 2 across parallel watchlist builds (avoids 429).
        tasks = [build(meta, delay=i * 0.5) for i, meta in enumerate(pending)]
        new_rows = [row for row in await asyncio.gather(*tasks) if row]

        for row in new_rows:
            prior_rows[row["underlying"]] = row

        rows = sorted(prior_rows.values(), key=lambda r: (r["kind"] != "INDEX", r["underlying"]))

        await redis.set(partial_key, json.dumps(rows), ex=300)

        logger.info(
            f"[ATM watchlist] Built {len(rows)}/{len(underlyings)} rows "
            f"({len(new_rows)} new, {len(prior_rows)-len(new_rows)} from partial cache)"
        )

        if len(rows) >= len(underlyings):
            await redis.delete(partial_key)

        await self._archive_expired_contracts()
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
            "source": "fyers" if fyers_adapter else "upstox",
            "detail": None if fyers_adapter else "Fyers is not connected, so the watchlist is using Upstox live chain data.",
            "timestamp": datetime.now(UTC).isoformat(),
        }
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
        # Priority: broker-reported expiry list → computed weekday fallback
        #
        # STOCK underlyings: monthly expiry ONLY (last Thursday of month).
        #   Passing a weekly expiry returns empty results from the broker.
        #   Override to the nearest stock monthly expiry unconditionally.
        #
        # INDEX underlyings: each index has its own expiry schedule that can
        #   change due to regulatory updates.  We ALWAYS ask the broker for the
        #   actual available expiry list and pick the nearest monthly from that.
        #   "Monthly" = the last expiry in the calendar month (the furthest-out
        #   contract for that month, which is the monthly contract in every
        #   weekly+monthly series).
        #   We only fall back to the hardcoded weekday computation when the
        #   broker returns no data (disconnected / rate-limited).
        if meta.kind != "INDEX":
            # Stock: always use last-Thursday monthly (no weekly series for stocks)
            monthly = _nearest_monthly_expiry()
            expiry = monthly.isoformat()
            expiry_date = monthly
        else:
            # Index: get actual available expiries from the broker
            broker_expiries = await self._get_broker_expiries_for_symbol(
                meta, upstox_adapter, fyers_adapter
            )
            broker_monthly = _nearest_monthly_from_expiry_list(broker_expiries)
            if broker_monthly is not None:
                if broker_monthly.isoformat() != expiry:
                    logger.debug(
                        f"[ATM watchlist] {meta.symbol} expiry broker-resolved: "
                        f"{expiry} → {broker_monthly.isoformat()} "
                        f"(from {len(broker_expiries)} broker expiries)"
                    )
                expiry = broker_monthly.isoformat()
                expiry_date = broker_monthly
            else:
                # Broker unavailable — fall back to computed weekday rule
                native_weekday = INDEX_EXPIRY_WEEKDAY.get(meta.symbol, 3)
                if expiry_date.weekday() != native_weekday:
                    idx_monthly = _nearest_index_expiry(meta.symbol)
                    logger.debug(
                        f"[ATM watchlist] {meta.symbol} expiry weekday-corrected (broker offline): "
                        f"{expiry} → {idx_monthly.isoformat()} (native weekday {native_weekday})"
                    )
                    expiry = idx_monthly.isoformat()
                    expiry_date = idx_monthly

        contracts = await self._get_contracts_for_expiry(meta, expiry, upstox_adapter) if upstox_adapter else []

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
        cache_key = f"atm_watchlist:sym_expiries:v1:{meta.symbol}"
        cached = await redis.get(cache_key)
        if cached:
            return json.loads(cached)

        expiries: list[str] = []

        # Try Fyers first (faster for index chains)
        if fyers_adapter is not None and not expiries:
            try:
                fyers_sym = self._to_fyers_symbol(meta)
                contracts = await fyers_adapter.get_option_contracts(fyers_sym)
                expiries = sorted({str(row.get("expiry")) for row in contracts if row.get("expiry")})
            except Exception as exc:
                logger.debug(f"[ATM watchlist] Fyers expiry fetch failed for {meta.symbol}: {exc}")

        # Fallback to Upstox
        if upstox_adapter is not None and not expiries:
            try:
                contracts = await upstox_adapter.get_option_contracts(meta.underlying_key)
                expiries = sorted({str(row.get("expiry")) for row in contracts if row.get("expiry")})
            except Exception as exc:
                logger.debug(f"[ATM watchlist] Upstox expiry fetch failed for {meta.symbol}: {exc}")

        if expiries:
            await redis.set(cache_key, json.dumps(expiries), ex=300)
        return expiries

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

        try:
            contracts = await upstox_adapter.get_option_contracts(meta.underlying_key, expiry)
        except Exception as exc:
            logger.debug(f"[ATM watchlist] Contract discovery failed for {meta.symbol}: {exc}")
            return []

        normalized = [
            {
                "instrument_key": row.get("instrument_key"),
                "trading_symbol": row.get("trading_symbol"),
                "strike_price": float(row.get("strike_price", 0) or 0.0),
                "instrument_type": row.get("instrument_type"),
                "expiry": row.get("expiry"),
                "lot_size": row.get("lot_size"),   # NSE-mandated lot size from Upstox
            }
            for row in contracts
            if row.get("instrument_key") and row.get("instrument_type") in {"CE", "PE"}
        ]
        await redis.set(cache_key, json.dumps(normalized), ex=DEFAULT_EXPIRY_TTL)
        return normalized

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

    async def _get_upstox_adapter(self) -> Optional[BrokerAdapter]:
        await ensure_upstox_session(force_validate=False)
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
