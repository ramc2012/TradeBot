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
from api.routers.auth import ensure_fyers_session, ensure_upstox_session, get_active_adapter, get_broker_token
from brokers.base import BrokerAdapter, OptionChain, OptionChainEntry
from brokers.upstox import UpstoxAdapter
from db.database import AsyncSessionLocal
from db.redis_client import get_redis
from market_data.fo_universe_bootstrap import ensure_fo_underlying_catalog
from market_data.option_history import option_history_service


UTC = timezone.utc
DEFAULT_WATCHLIST_TTL = 900
DEFAULT_EXPIRY_TTL = 300
DEFAULT_PARTIAL_TTL = 900
DEFAULT_BUILD_LOCK_TTL = 120
WATCHLIST_CACHE_VERSION = "v12"
SYMBOL_EXPIRY_CACHE_VERSION = "v2"
PERSISTED_WATCHLIST_FRESH_SECONDS = 36 * 60 * 60

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


def _select_liquid_atm_strikes(
    *,
    strikes: list[float],
    spot_price: float,
    chain_entries,
    neighbours: int = 2,
    liquidity_lift: float = 1.5,
) -> dict[str, float]:
    """Pick asymmetric CE and PE strikes near spot, preferring liquidity.

    We trade directionally, NOT straddles — so CE and PE need not be
    on the same strike. The convention:

      CE: pick the most-liquid strike *at or above* spot, allowing
          ±*neighbours* slop. e.g. spot 23577 → 23600 (or 23650 if
          much more liquid).
      PE: pick the most-liquid strike *at or below* spot, allowing
          ±*neighbours* slop. e.g. spot 23577 → 23500 (or 23550 if
          much more liquid).

    The CE-side hunt biases upward (out-of-the-money for CE) and
    PE-side hunt biases downward (out-of-the-money for PE), so both
    sides land on the side with natural directional convexity.

    A neighbour is preferred over the side-anchored strike only when
    its single-side volume (CE volume for CE pick, PE volume for PE
    pick) is at least *liquidity_lift* × the anchored strike's volume.

    Returns {"CE": ce_strike, "PE": pe_strike}. They MAY coincide if
    the most-liquid strike on both sides happens to be the same one.
    """
    if not strikes or spot_price <= 0:
        s0 = strikes[0] if strikes else 0.0
        return {"CE": s0, "PE": s0}
    sorted_strikes = sorted(strikes)
    # Single-side liquidity maps.
    ce_liq: dict[float, float] = {}
    pe_liq: dict[float, float] = {}
    for entry in chain_entries or []:
        try:
            strike = float(entry.strike)
        except (TypeError, ValueError):
            continue
        opt = str(getattr(entry, "option_type", "")).upper()
        vol = float(getattr(entry, "volume", 0) or 0)
        if vol <= 0:
            vol = float(getattr(entry, "oi", 0) or 0) / 100.0  # OI proxy
        if opt == "CE":
            ce_liq[strike] = ce_liq.get(strike, 0.0) + max(vol, 0.0)
        elif opt == "PE":
            pe_liq[strike] = pe_liq.get(strike, 0.0) + max(vol, 0.0)

    def _pick(*, side: str) -> float:
        side_liq = ce_liq if side == "CE" else pe_liq
        # Anchor: nearest strike at or *above* spot for CE; at or
        # *below* spot for PE. Fall back to literal nearest if the
        # side-anchored search is empty.
        if side == "CE":
            anchored = [s for s in sorted_strikes if s >= spot_price]
            if not anchored:
                anchored = sorted_strikes
            anchor = anchored[0]
        else:
            anchored = [s for s in sorted_strikes if s <= spot_price]
            if not anchored:
                anchored = sorted_strikes
            anchor = anchored[-1]
        try:
            idx = sorted_strikes.index(anchor)
        except ValueError:
            return anchor
        lo = max(0, idx - neighbours)
        hi = min(len(sorted_strikes), idx + neighbours + 1)
        candidates = sorted_strikes[lo:hi]
        if all(side_liq.get(s, 0.0) <= 0 for s in candidates):
            return anchor  # no signal — keep side-anchored strike
        anchor_liq = side_liq.get(anchor, 0.0)
        if anchor_liq <= 0:
            return max(candidates, key=lambda s: side_liq.get(s, 0.0))
        best = anchor
        best_liq = anchor_liq
        for strike in candidates:
            if strike == anchor:
                continue
            cand_liq = side_liq.get(strike, 0.0)
            if cand_liq >= anchor_liq * liquidity_lift and cand_liq > best_liq:
                best = strike
                best_liq = cand_liq
        return best

    return {"CE": _pick(side="CE"), "PE": _pick(side="PE")}


def _select_liquid_atm_strike(
    *,
    strikes: list[float],
    spot_price: float,
    chain_entries,
    neighbours: int = 2,
    liquidity_lift: float = 1.5,
) -> float:
    """Backward-compat shim: returns the CE strike from the asymmetric
    selector. New callers should use _select_liquid_atm_strikes."""
    picks = _select_liquid_atm_strikes(
        strikes=strikes,
        spot_price=spot_price,
        chain_entries=chain_entries,
        neighbours=neighbours,
        liquidity_lift=liquidity_lift,
    )
    return picks["CE"]


def _trading_days_until(target: date, *, today: Optional[date] = None) -> int:
    """Count Mon–Fri weekdays from today (exclusive) to target (exclusive)."""
    today = today or date.today()
    if target <= today:
        return 0
    count = 0
    cur = today
    while cur < target:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            count += 1
    return count


def _next_weekly_expiry(symbol: str, today: Optional[date] = None) -> Optional[date]:
    """LEGACY weekday-based weekly resolver — retained as a last-resort
    fallback only. The authoritative source for available expiries is
    the broker's option chain (master) plus fo_expiry_catalog (monthly
    master). See _resolve_expiry_from_master() on the service for the
    proper instrument-master-driven path.
    """
    from analysis.instruments import INDEX_EXPIRY_WEEKDAY
    today = today or date.today()
    weekday = INDEX_EXPIRY_WEEKDAY.get(symbol.upper())
    if weekday is None:
        return None
    delta = (weekday - today.weekday()) % 7
    if delta == 0 and today.weekday() == weekday:
        return today
    return today + timedelta(days=delta if delta > 0 else 7)


def _stock_monthly_for_selected_expiry(
    selected_expiry: date,
    *,
    today: Optional[date] = None,
    rollover_td: Optional[int] = None,
) -> date:
    """Resolve stocks to the monthly expiry for the selected month, but
    *roll forward* when the active monthly has ≤ rollover_td trading
    days left. rollover_td defaults to MIN_TTE_DAYS_STOCK (3) so the
    S1 path is unchanged when callers don't pass a profile override.
    """
    today = today or date.today()
    active = get_monthly_expiry(selected_expiry.year, selected_expiry.month)
    if rollover_td is None:
        try:
            from agent.strategy_config import MIN_TTE_DAYS_STOCK as _stock_min_tte
        except Exception:
            _stock_min_tte = 3
        rollover_td = int(_stock_min_tte)
    if rollover_td > 0 and _trading_days_until(active, today=today) <= rollover_td:
        next_anchor = (active.replace(day=28) + timedelta(days=4))
        return get_monthly_expiry(next_anchor.year, next_anchor.month)
    return active


# NOTE: a pure weekday-math sync resolver previously lived here. It was
# superseded by ATMWatchlistService._resolve_expiry_from_master, which
# sources expiries from the broker chain (master) plus fo_expiry_catalog
# (monthly master) and falls back to weekday math only when both are
# unavailable. We deleted the sync version to remove the dead path —
# the broker- and catalog-driven resolution catches holiday-shifted
# expiries that pure weekday math cannot (e.g. SENSEX 2026-05-27 in
# the catalog vs the weekday-Thursday 2026-05-28).


def _parse_payload_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _latest_watchlist_row_time(rows: list[dict[str, Any]]) -> Optional[datetime]:
    latest: Optional[datetime] = None
    for row in rows:
        candidates = [
            _parse_payload_datetime(row.get("as_of")),
            _parse_payload_datetime((row.get("ce") or {}).get("as_of")),
            _parse_payload_datetime((row.get("pe") or {}).get("as_of")),
        ]
        row_latest = max((candidate for candidate in candidates if candidate is not None), default=None)
        if row_latest is not None and (latest is None or row_latest > latest):
            latest = row_latest
    return latest


def _watchlist_rows_are_fresh(rows: list[dict[str, Any]]) -> bool:
    latest = _latest_watchlist_row_time(rows)
    if latest is None:
        return False
    return (datetime.now(UTC) - latest).total_seconds() <= PERSISTED_WATCHLIST_FRESH_SECONDS


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


def _normalize_symbol_scope(symbols: Optional[list[str]]) -> tuple[str, ...]:
    if not symbols:
        return ()
    normalized = {
        str(symbol or "").strip().upper()
        for symbol in symbols
        if str(symbol or "").strip()
    }
    return tuple(sorted(normalized))


def _scope_cache_key(symbols: tuple[str, ...]) -> str:
    return "all" if not symbols else "scope:" + ",".join(symbols)


def _cache_mode_key(*, live_refresh: bool) -> str:
    return "live" if live_refresh else "local"

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


# Tracks which (set of missing index defaults) we have already self-healed
# in this process, so the patch log fires exactly once per missing set
# instead of every scan cycle.
_DEFAULT_INDEX_WARNING_SEEN: set[frozenset[str]] = set()


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
                  AND underlying = ANY(:underlyings)
                ORDER BY underlying, time DESC
            )
            SELECT
                latest.underlying,
                latest.kind,
                latest.expiry,
                latest.strike,
                latest.source_broker,
                latest.underlying_price,
                latest.time AS as_of,
                {lot_size_select}
                ce.time AS ce_as_of,
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
                pe.time AS pe_as_of,
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
                SELECT time, instrument_key, trading_symbol, ltp, prev_close, change, change_pct,
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
                SELECT time, instrument_key, trading_symbol, ltp, prev_close, change, change_pct,
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

    async def get_expiries(
        self,
        selected_expiry: Optional[str] = None,
        *,
        live_refresh: bool = False,
    ) -> dict[str, Any]:
        redis = await get_redis()
        mode_key = _cache_mode_key(live_refresh=live_refresh)
        cache_key = f"atm_watchlist:expiries:{WATCHLIST_CACHE_VERSION}:{mode_key}:{selected_expiry or 'default'}"
        cached = await redis.get(cache_key)
        if cached:
            cached_payload = json.loads(cached)
            cached_rows = list(cached_payload.get("rows") or [])
            if live_refresh and not _watchlist_rows_are_fresh(cached_rows):
                await redis.delete(cache_key)
            else:
                return cached_payload
        if not live_refresh:
            shared_live_cache = await redis.get(
                f"atm_watchlist:expiries:{WATCHLIST_CACHE_VERSION}:live:{selected_expiry or 'default'}"
            )
            if shared_live_cache:
                return json.loads(shared_live_cache)

        underlyings = await self._load_underlyings()
        representative = [
            row for row in underlyings
            if row.symbol in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "TCS"}
        ]
        if not representative:
            representative = underlyings[:10]

        fyers_adapter = None
        upstox_adapter = None
        if live_refresh:
            fyers_adapter = get_active_adapter("fyers")
            if fyers_adapter is None and await ensure_fyers_session(force_validate=True):
                fyers_adapter = get_active_adapter("fyers")
            upstox_adapter = await self._get_upstox_adapter()

        fyers_failed = False
        used_upstox_fallback = False
        used_catalog_fallback = False

        async def fetch_expiries(meta: UnderlyingMeta) -> list[str]:
            nonlocal fyers_failed, used_upstox_fallback, used_catalog_fallback
            expiries, live_source = await self._get_broker_expiry_snapshot_for_symbol(
                meta,
                upstox_adapter,
                fyers_adapter,
            )
            if live_source == "upstox":
                used_upstox_fallback = True
            elif live_source == "catalog":
                used_catalog_fallback = True
            elif live_source == "none" and fyers_adapter is not None:
                fyers_failed = True
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
            "stock_monthly_expiry": stock_monthly_expiry_iso,
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

    async def get_watchlist(
        self,
        expiry: Optional[str] = None,
        symbols: Optional[list[str]] = None,
        *,
        live_refresh: bool = False,
    ) -> dict[str, Any]:
        expiry_payload = await self.get_expiries(expiry, live_refresh=live_refresh)
        selected_expiry = expiry or expiry_payload.get("default_expiry")
        selected_expiry_date = self._parse_expiry(selected_expiry)
        scope_symbols = _normalize_symbol_scope(symbols)
        scope_set = set(scope_symbols)
        scope_key = _scope_cache_key(scope_symbols)
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
        mode_key = _cache_mode_key(live_refresh=live_refresh)
        cache_key = f"atm_watchlist:{WATCHLIST_CACHE_VERSION}:{mode_key}:{selected_expiry}:{scope_key}"
        partial_key = f"atm_watchlist:partial:{WATCHLIST_CACHE_VERSION}:{mode_key}:{selected_expiry}:{scope_key}"
        build_lock_key = f"atm_watchlist:building:{WATCHLIST_CACHE_VERSION}:{mode_key}:{selected_expiry}:{scope_key}"
        shared_live_cache_key = f"atm_watchlist:{WATCHLIST_CACHE_VERSION}:live:{selected_expiry}:{scope_key}"
        shared_live_full_cache_key = f"atm_watchlist:{WATCHLIST_CACHE_VERSION}:live:{selected_expiry}:all"

        def _sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return sorted(rows, key=lambda row: (row["kind"] != "INDEX", row["underlying"]))

        def _summarize_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
            return {
                "total_rows": len(rows),
                "ce_ready": sum(1 for row in rows if row.get("ce")),
                "pe_ready": sum(1 for row in rows if row.get("pe")),
                "fyers_rows": sum(1 for row in rows if row.get("live_source") == "fyers"),
                "upstox_rows": sum(1 for row in rows if row.get("live_source") == "upstox"),
            }

        def _filter_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            if not scope_set:
                return rows
            return [row for row in rows if str(row.get("underlying") or "").upper() in scope_set]

        if not live_refresh:
            shared_cached = await redis.get(shared_live_cache_key)
            if shared_cached:
                return json.loads(shared_cached)

        cached = await redis.get(cache_key)
        if cached:
            cached_payload = json.loads(cached)
            cached_rows = list(cached_payload.get("rows") or [])
            if live_refresh and not _watchlist_rows_are_fresh(cached_rows):
                await redis.delete(cache_key)
            else:
                return cached_payload

        if scope_symbols and not live_refresh:
            full_cache_keys = [shared_live_full_cache_key]
            if cache_key != shared_live_cache_key:
                full_cache_keys.append(f"atm_watchlist:{WATCHLIST_CACHE_VERSION}:{mode_key}:{selected_expiry}:all")
            for full_cache_key in full_cache_keys:
                full_cached = await redis.get(full_cache_key)
                if not full_cached:
                    continue
                filtered_payload = json.loads(full_cached)
                filtered_rows = _sort_rows(_filter_rows(list(filtered_payload.get("rows") or [])))
                filtered_payload["rows"] = filtered_rows
                filtered_payload["summary"] = _summarize_rows(filtered_rows)
                await redis.set(cache_key, json.dumps(filtered_payload), ex=DEFAULT_WATCHLIST_TTL)
                return filtered_payload

        fyers_adapter = None
        upstox_adapter = None
        if live_refresh:
            fyers_adapter = get_active_adapter("fyers")
            if fyers_adapter is None and await ensure_fyers_session(force_validate=True):
                fyers_adapter = get_active_adapter("fyers")
            upstox_adapter = await self._get_upstox_adapter()

        underlyings = await self._load_underlyings()
        if scope_set:
            underlyings = [meta for meta in underlyings if meta.symbol.upper() in scope_set]
        if not underlyings:
            payload = {
                "expiry": selected_expiry,
                "rows": [],
                "summary": {"total_rows": 0, "ce_ready": 0, "pe_ready": 0},
                "source": "none",
                "detail": "No matching underlyings are configured for the ATM watchlist scope.",
                "build_status": "ready",
                "timestamp": datetime.now(UTC).isoformat(),
            }
            await redis.set(cache_key, json.dumps(payload), ex=30)
            return payload

        partial_cache = await redis.get(partial_key)
        prior_rows: dict[str, dict] = {}
        loaded_from_persisted = False
        if partial_cache:
            partial_rows = list(json.loads(partial_cache))
            if live_refresh and not _watchlist_rows_are_fresh(partial_rows):
                await redis.delete(partial_key)
                partial_cache = None
            else:
                for row in partial_rows:
                    prior_rows[row["underlying"]] = row
        if not partial_cache:
            for row in await self._load_persisted_watchlist_rows(selected_expiry, underlyings):
                prior_rows[row["underlying"]] = row
            loaded_from_persisted = bool(prior_rows)
            allow_table_fallback = len(underlyings) >= 50
            if allow_table_fallback and len(prior_rows) < min(len(underlyings), 50):
                try:
                    for row in await self._load_premium_candle_watchlist_rows(selected_expiry, underlyings):
                        prior_rows.setdefault(row["underlying"], row)
                except Exception as exc:
                    logger.debug(f"[ATM watchlist] premium-candle fallback unavailable: {exc}")
                loaded_from_persisted = bool(prior_rows)
            if allow_table_fallback and len(prior_rows) < min(len(underlyings), 50):
                try:
                    for row in await self._load_catalog_watchlist_rows(selected_expiry, underlyings):
                        prior_rows.setdefault(row["underlying"], row)
                except Exception as exc:
                    logger.debug(f"[ATM watchlist] catalog fallback unavailable: {exc}")
                loaded_from_persisted = bool(prior_rows)

        if upstox_adapter is None and fyers_adapter is None:
            if prior_rows:
                rows = _sort_rows(list(prior_rows.values()))
                stale_live_refresh = live_refresh and not _watchlist_rows_are_fresh(rows)
                payload = {
                    "expiry": selected_expiry,
                    "rows": rows,
                    "summary": _summarize_rows(rows),
                    "source": "snapshot",
                    "detail": (
                        "Live brokers are offline and the saved ATM watchlist board is stale. "
                        "Reconnect Fyers or Upstox to refresh it."
                        if stale_live_refresh
                        else "Live brokers are offline. Showing the last saved ATM watchlist board."
                    ),
                    "build_status": "stale" if stale_live_refresh else "ready",
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

        # On a live_refresh cycle, treat any prior_row whose latest_time is
        # older than today's session-open as STALE → include it in pending so
        # the BG build actually re-fetches fresh option premium data. Without
        # this, Friday's snapshots (213 stocks) live forever in prior_rows
        # and the stock universe never refreshes on Monday morning.
        stale_force_refresh_count = 0
        if live_refresh and prior_rows:
            from datetime import datetime as _dt
            today_session_open = _dt.combine(_now_ist().date(), time(9, 15), tzinfo=IST).astimezone(UTC)
            stale_symbols: list[str] = []
            for symbol, row in list(prior_rows.items()):
                latest = _latest_watchlist_row_time([row])
                if latest is None or latest < today_session_open:
                    stale_symbols.append(symbol)
            if stale_symbols:
                stale_force_refresh_count = len(stale_symbols)
                # Drop them from prior_rows so they re-enter pending and the
                # BG build path picks them up below.
                for sym in stale_symbols:
                    prior_rows.pop(sym, None)
        pending = [m for m in underlyings if m.symbol not in prior_rows]
        logger.info(
            f"[ATM watchlist] {len(prior_rows)} cached, {len(pending)} to fetch for {selected_expiry} ({scope_key}) "
            f"(stale_force_refresh={stale_force_refresh_count})"
        )
        coverage_target = (
            len(underlyings)
            if len(underlyings) < 50
            else max(50, int(len(underlyings) * 0.9))
        )
        sufficient_prior_coverage = len(prior_rows) >= coverage_target

        def _build_payload(rows: list[dict[str, Any]], detail: Optional[str], build_status: str) -> dict[str, Any]:
            sorted_rows = _sort_rows(rows)
            return {
                "expiry": selected_expiry,
                "rows": sorted_rows,
                "summary": _summarize_rows(sorted_rows),
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
            refreshed = 0
            for meta in pending_metas:
                row = await build(meta)
                completed += 1
                if row:
                    refreshed += 1
                    prior[row["underlying"]] = row
                rows = _sort_rows(list(prior.values()))
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

            rows = _sort_rows(list(prior.values()))
            logger.info(
                f"[ATM watchlist] BG build done: {len(rows)}/{len(all_underlyings)} rows for {selected_expiry} ({scope_key})"
            )
            build_complete = len(rows) >= len(all_underlyings) and refreshed >= len(pending_metas)
            if build_complete:
                await redis.delete(partial_key)
                await redis.delete(build_lock_key)
            detail_msg = detail_prefix
            if not build_complete:
                detail_msg = (
                    (detail_msg + " " if detail_msg else "")
                    + (
                        "Live refresh could not update the saved ATM watchlist yet."
                        if len(rows) >= len(all_underlyings)
                        else f"Building {len(all_underlyings) - len(rows)} remaining symbols in background."
                    )
                )
            if build_complete:
                _payload = _build_payload(rows, detail_msg, "ready")
                await redis.set(cache_key, json.dumps(_payload), ex=DEFAULT_WATCHLIST_TTL)
            else:
                _payload = _build_payload(
                    rows,
                    detail_msg,
                    "stale" if len(rows) >= len(all_underlyings) else "building",
                )
                await redis.set(cache_key, json.dumps(_payload), ex=60)
            await self._archive_expired_contracts()

        if prior_rows:
            rows = _sort_rows(list(prior_rows.values()))
            detail_msg = None if fyers_adapter else "Fyers is not connected, using Upstox live chain data."
            payload_status = "ready"
            background_targets = pending
            background_detail_prefix = None if fyers_adapter else "Fyers is not connected, using Upstox live chain data."
            if loaded_from_persisted and not partial_cache:
                if scope_symbols:
                    background_targets = underlyings
                    detail_msg = (
                        (detail_msg + " " if detail_msg else "")
                        + "Showing the last saved ATM watchlist while live refresh updates in background."
                    )
                    background_detail_prefix = detail_msg
                elif sufficient_prior_coverage and _watchlist_rows_are_fresh(rows):
                    background_targets = []
                    detail_msg = (
                        (detail_msg + " " if detail_msg else "")
                        + "Showing the last saved full-universe ATM watchlist. "
                        + "Background live refresh is deferred to avoid a cold-start broker stampede."
                    )
                elif sufficient_prior_coverage:
                    payload_status = "building"
                    background_targets = underlyings
                    latest_row_time = _latest_watchlist_row_time(rows)
                    stale_detail = (
                        "Saved full-universe ATM watchlist is stale; "
                        "live refresh is rebuilding the stock universe in background."
                    )
                    if latest_row_time is not None:
                        stale_detail = (
                            f"Saved full-universe ATM watchlist is stale "
                            f"(latest {latest_row_time.astimezone(timezone(timedelta(hours=5, minutes=30))).strftime('%Y-%m-%d %H:%M IST')}); "
                            "live refresh is rebuilding the stock universe in background."
                        )
                    detail_msg = (detail_msg + " " if detail_msg else "") + stale_detail
                    background_detail_prefix = detail_msg
                else:
                    payload_status = "building"
                    seed_metas = pending[: min(8, len(pending))]
                    if seed_metas:
                        seed_rows = [
                            row
                            for row in await asyncio.gather(
                                *(build(meta, delay=i * 0.05) for i, meta in enumerate(seed_metas))
                            )
                            if row
                        ]
                        for row in seed_rows:
                            prior_rows[row["underlying"]] = row
                        pending = [m for m in underlyings if m.symbol not in prior_rows]
                        rows = _sort_rows(list(prior_rows.values()))
                    background_targets = pending
                    detail_msg = (
                        (detail_msg + " " if detail_msg else "")
                        + "Saved ATM watchlist coverage is incomplete; live refresh is building the missing stock universe."
                    )
                    background_detail_prefix = detail_msg
            elif pending:
                payload_status = "building"
                detail_msg = (
                    (detail_msg + " " if detail_msg else "")
                    + f"Building {len(pending)} remaining symbols in background."
                )
                background_detail_prefix = detail_msg
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
                            background_detail_prefix,
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
        rows = _sort_rows(list(prior_rows.values()))
        remaining = [meta for meta in pending if meta.symbol not in prior_rows]
        await redis.set(partial_key, json.dumps(rows), ex=DEFAULT_PARTIAL_TTL)

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
        live_source = "none"
        fyers_symbol = self._to_fyers_symbol(meta)
        prefer_fyers = meta.kind == "INDEX"

        async def _load_fyers_chain() -> bool:
            nonlocal chain, live_source
            if fyers_adapter is None:
                return False
            try:
                candidate = await fyers_adapter.get_option_chain(fyers_symbol, expiry)
            except Exception as exc:
                logger.debug(f"[ATM watchlist] Fyers chain failed for {meta.symbol}: {exc}")
                return False
            if not candidate.entries:
                return False
            chain = candidate
            live_source = "fyers"
            return True

        async def _load_upstox_chain() -> bool:
            nonlocal chain, live_source
            if upstox_adapter is None:
                return False
            try:
                candidate = await upstox_adapter.get_option_chain(meta.underlying_key, expiry)
            except Exception as exc:
                logger.debug(f"[ATM watchlist] Upstox chain failed for {meta.symbol}: {exc}")
                return False
            if not candidate.entries:
                return False
            chain = candidate
            live_source = "upstox"
            return True

        if prefer_fyers:
            if not await _load_fyers_chain() and not await _load_upstox_chain():
                return None
        else:
            if not await _load_upstox_chain() and not await _load_fyers_chain():
                return None

        if not chain.entries:
            return None

        spot_price = float(chain.spot_price or 0.0)
        strikes = sorted({float(entry.strike) for entry in chain.entries})
        if not strikes:
            return None
        # Asymmetric liquid-strike selection. We trade directionally,
        # not straddles — CE and PE pick their own strike:
        #   CE → most-liquid strike at-or-above spot (±2 slop)
        #   PE → most-liquid strike at-or-below spot (±2 slop)
        # A neighbour is preferred over the side-anchored strike only
        # when its single-side volume is ≥1.5× the anchor's.
        atm_picks = _select_liquid_atm_strikes(
            strikes=strikes,
            spot_price=spot_price,
            chain_entries=chain.entries,
        )
        atm_ce_strike = atm_picks["CE"]
        atm_pe_strike = atm_picks["PE"]
        # Downstream payload still carries a single "atm_strike" for
        # legacy callers — set it to the CE pick (the more common
        # default and the one used by long-CE-only paths).
        atm_strike = atm_ce_strike
        ce_entry = next((entry for entry in chain.entries if entry.option_type == "CE" and float(entry.strike) == atm_ce_strike), None)
        pe_entry = next((entry for entry in chain.entries if entry.option_type == "PE" and float(entry.strike) == atm_pe_strike), None)
        if not ce_entry and not pe_entry:
            return None

        contract_map = {
            (float(contract["strike_price"]), str(contract["instrument_type"])): contract
            for contract in contracts
        }
        ce_contract = contract_map.get((atm_ce_strike, "CE"))
        pe_contract = contract_map.get((atm_pe_strike, "PE"))

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
                        # Same asymmetric liquid-strike pick on the
                        # Upstox fallback path. CE biases above spot,
                        # PE biases below.
                        atm_picks = _select_liquid_atm_strikes(
                            strikes=strikes,
                            spot_price=spot_price,
                            chain_entries=chain.entries,
                        )
                        atm_ce_strike = atm_picks["CE"]
                        atm_pe_strike = atm_picks["PE"]
                        atm_strike = atm_ce_strike
                        ce_entry = next(
                            (item for item in chain.entries if item.option_type == "CE" and float(item.strike) == atm_ce_strike),
                            None,
                        )
                        pe_entry = next(
                            (item for item in chain.entries if item.option_type == "PE" and float(item.strike) == atm_pe_strike),
                            None,
                        )
                        ce_contract = contract_map.get((atm_ce_strike, "CE"))
                        pe_contract = contract_map.get((atm_pe_strike, "PE"))
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

        # CE and PE may land on different strikes when the asymmetric
        # liquid-strike picker biases each side toward its OTM (CE
        # ≥ spot, PE ≤ spot). Pass each side's own strike so the
        # payload strike field matches the actual contract priced.
        ce_payload = await self._build_option_payload(
            meta,
            expiry,
            expiry_date,
            spot_price,
            atm_ce_strike,
            ce_entry,
            ce_contract,
            live_source,
        )
        pe_payload = await self._build_option_payload(
            meta,
            expiry,
            expiry_date,
            spot_price,
            atm_pe_strike,
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
            "as_of": datetime.now(UTC).isoformat(),
            "expiry": expiry,
            # Legacy single-strike field (kept for back-compat). CE/PE
            # may now be on different strikes — see ce_atm_strike /
            # pe_atm_strike below for the authoritative per-side values.
            "atm_strike": atm_strike,
            "ce_atm_strike": atm_ce_strike,
            "pe_atm_strike": atm_pe_strike,
            "atm_strikes_asymmetric": atm_ce_strike != atm_pe_strike,
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
            "as_of": datetime.now(UTC).isoformat(),
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
        expiries, _source = await self._get_broker_expiry_snapshot_for_symbol(
            meta,
            upstox_adapter,
            fyers_adapter,
        )
        return expiries

    async def _get_broker_expiry_snapshot_for_symbol(
        self,
        meta: "UnderlyingMeta",
        upstox_adapter: Optional[BrokerAdapter],
        fyers_adapter: Optional[BrokerAdapter] = None,
    ) -> tuple[list[str], str]:
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
            payload = json.loads(cached)
            if isinstance(payload, dict):
                return list(payload.get("expiries") or []), str(payload.get("source") or "cache")
            return list(payload or []), "cache"

        persisted_expiries = await self._load_persisted_expiries_for_symbol(meta.symbol)
        expiries: list[str] = []
        source = "none"

        # Upstox contract metadata is the canonical expiry ladder when available.
        if upstox_adapter is not None and not expiries:
            try:
                contracts = await upstox_adapter.get_option_contracts(meta.underlying_key)
                expiries = sorted({str(row.get("expiry")) for row in contracts if row.get("expiry")})
                if _is_nse_derivatives_symbol(meta.symbol):
                    expiries = _normalize_nse_expiry_ladder(expiries)
                if expiries:
                    source = "upstox"
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
                    source = "fyers"
                    await self._persist_expiries_for_symbol(meta.symbol, expiries)
            except Exception as exc:
                logger.debug(f"[ATM watchlist] Fyers expiry fetch failed for {meta.symbol}: {exc}")

        if expiries:
            await redis.set(cache_key, json.dumps({"expiries": expiries, "source": source}), ex=300)
            return expiries, source

        if persisted_expiries:
            if _is_nse_derivatives_symbol(meta.symbol):
                persisted_expiries = _normalize_nse_expiry_ladder(persisted_expiries)
            await redis.set(
                cache_key,
                json.dumps({"expiries": persisted_expiries, "source": "catalog"}),
                ex=DEFAULT_EXPIRY_TTL,
            )
            return persisted_expiries, "catalog"
        return [], source

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
        if not underlyings:
            return []
        expiry_date = self._parse_expiry(expiry)
        if expiry_date is None:
            return []
        underlying_symbols = [meta.symbol for meta in underlyings]

        async with AsyncSessionLocal() as session:
            try:
                result = await session.execute(
                    text(self._persisted_watchlist_query(include_underlying_lot_size=True)),
                    {"expiry": expiry_date, "underlyings": underlying_symbols},
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
                    {"expiry": expiry_date, "underlyings": underlying_symbols},
                )
            rows = result.fetchall()

        meta_by_symbol = {meta.symbol: meta for meta in underlyings}
        payload_rows: list[dict[str, Any]] = []
        for row in rows:
            meta = meta_by_symbol.get(str(row.underlying))
            if meta is None:
                continue
            fyers_symbol = self._to_fyers_symbol(meta) if meta else None

            def _option_payload(prefix: str, option_type: str) -> Optional[dict[str, Any]]:
                ltp = getattr(row, f"{prefix}_ltp")
                instrument_key = getattr(row, f"{prefix}_instrument_key")
                trading_symbol = getattr(row, f"{prefix}_trading_symbol")
                if all(value is None for value in (ltp, instrument_key, trading_symbol)):
                    return None
                as_of = getattr(row, f"{prefix}_as_of", None) or getattr(row, "as_of", None)
                return {
                    "strike": float(row.strike),
                    "option_type": option_type,
                    "as_of": as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of or ""),
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

            row_as_of = getattr(row, "as_of", None)
            payload_rows.append(
                {
                    "underlying": str(row.underlying),
                    "kind": str(row.kind),
                    "spot_price": round(float(row.underlying_price or 0.0), 2),
                    "as_of": row_as_of.isoformat() if hasattr(row_as_of, "isoformat") else str(row_as_of or ""),
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

    async def _load_premium_candle_watchlist_rows(
        self,
        expiry: str,
        underlyings: list[UnderlyingMeta],
    ) -> list[dict[str, Any]]:
        if not underlyings:
            return []
        expiry_date = self._parse_expiry(expiry)
        if expiry_date is None:
            return []
        underlying_symbols = [meta.symbol for meta in underlyings]
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    WITH latest_contracts AS (
                        SELECT DISTINCT ON (underlying, strike, option_type)
                            time,
                            underlying,
                            expiry,
                            strike,
                            option_type,
                            instrument_key,
                            trading_symbol,
                            time,
                            close AS ltp,
                            volume,
                            oi,
                            iv,
                            underlying_price
                        FROM option_premium_candles
                        WHERE expiry = :expiry
                          AND underlying = ANY(:underlyings)
                          AND option_type IN ('CE', 'PE')
                          AND close IS NOT NULL
                        ORDER BY underlying, strike, option_type, time DESC
                    ),
                    latest_spot AS (
                        SELECT DISTINCT ON (underlying)
                            underlying,
                            underlying_price,
                            time
                        FROM latest_contracts
                        WHERE underlying_price IS NOT NULL
                          AND underlying_price > 0
                        ORDER BY underlying, time DESC
                    ),
                    atm_strikes AS (
                        SELECT DISTINCT ON (contracts.underlying)
                            contracts.underlying,
                            contracts.strike,
                            spot.underlying_price,
                            spot.time AS spot_time
                        FROM latest_contracts contracts
                        JOIN latest_spot spot
                          ON spot.underlying = contracts.underlying
                        ORDER BY contracts.underlying,
                                 ABS(contracts.strike::float8 - spot.underlying_price::float8),
                                 contracts.strike
                    )
                    SELECT
                        atm.underlying,
                        COALESCE(underlying_catalog.kind, 'STOCK') AS kind,
                        atm.strike,
                        atm.underlying_price,
                        COALESCE(catalog_ce.lot_size, catalog_pe.lot_size, underlying_catalog.lot_size) AS lot_size,
                        ce.instrument_key AS ce_instrument_key,
                        ce.trading_symbol AS ce_trading_symbol,
                        ce.time AS ce_as_of,
                        ce.ltp AS ce_ltp,
                        ce.volume AS ce_volume,
                        ce.oi AS ce_oi,
                        ce.iv AS ce_iv,
                        pe.instrument_key AS pe_instrument_key,
                        pe.trading_symbol AS pe_trading_symbol,
                        pe.time AS pe_as_of,
                        pe.ltp AS pe_ltp,
                        pe.volume AS pe_volume,
                        pe.oi AS pe_oi,
                        pe.iv AS pe_iv
                    FROM atm_strikes atm
                    LEFT JOIN latest_contracts ce
                      ON ce.underlying = atm.underlying
                     AND ce.strike = atm.strike
                     AND ce.option_type = 'CE'
                    LEFT JOIN latest_contracts pe
                      ON pe.underlying = atm.underlying
                     AND pe.strike = atm.strike
                     AND pe.option_type = 'PE'
                    LEFT JOIN fo_contract_catalog catalog_ce
                      ON catalog_ce.instrument_key = ce.instrument_key
                    LEFT JOIN fo_contract_catalog catalog_pe
                      ON catalog_pe.instrument_key = pe.instrument_key
                    LEFT JOIN fo_underlying_catalog underlying_catalog
                      ON underlying_catalog.symbol = atm.underlying
                    WHERE ce.ltp IS NOT NULL
                       OR pe.ltp IS NOT NULL
                    ORDER BY CASE WHEN COALESCE(underlying_catalog.kind, 'STOCK') = 'INDEX' THEN 0 ELSE 1 END,
                             atm.underlying
                    """
                ),
                {"expiry": expiry_date, "underlyings": underlying_symbols},
            )
            rows = result.fetchall()

        meta_by_symbol = {meta.symbol: meta for meta in underlyings}
        payload_rows: list[dict[str, Any]] = []
        for row in rows:
            meta = meta_by_symbol.get(str(row.underlying))
            if meta is None:
                continue

            def _option_payload(prefix: str, option_type: str) -> Optional[dict[str, Any]]:
                ltp = getattr(row, f"{prefix}_ltp")
                instrument_key = getattr(row, f"{prefix}_instrument_key")
                trading_symbol = getattr(row, f"{prefix}_trading_symbol")
                if all(value is None for value in (ltp, instrument_key, trading_symbol)):
                    return None
                as_of = getattr(row, f"{prefix}_as_of", None)
                return {
                    "strike": float(row.strike),
                    "option_type": option_type,
                    "as_of": as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of or ""),
                    "instrument_key": instrument_key,
                    "trading_symbol": trading_symbol,
                    "ltp": round(float(ltp or 0.0), 2),
                    "prev_close": None,
                    "change": None,
                    "change_pct": None,
                    "oi": int(getattr(row, f"{prefix}_oi") or 0),
                    "prev_oi": None,
                    "oi_change": None,
                    "oi_change_pct": None,
                    "volume": int(getattr(row, f"{prefix}_volume") or 0),
                    "iv": round(float(getattr(row, f"{prefix}_iv") or 0.0), 4)
                    if getattr(row, f"{prefix}_iv") is not None else None,
                    "macd": None,
                    "macd_signal": None,
                    "macd_histogram": None,
                    "rsi": None,
                }

            payload_rows.append(
                {
                    "underlying": str(row.underlying),
                    "kind": str(row.kind),
                    "spot_price": round(float(row.underlying_price or 0.0), 2),
                    "as_of": row.spot_time.isoformat() if hasattr(row.spot_time, "isoformat") else str(row.spot_time or ""),
                    "expiry": expiry,
                    "atm_strike": float(row.strike),
                    "live_source": "upstox_history",
                    "fyers_symbol": self._to_fyers_symbol(meta),
                    "lot_size": int(row.lot_size) if row.lot_size is not None else None,
                    "ce": _option_payload("ce", "CE"),
                    "pe": _option_payload("pe", "PE"),
                }
            )
        return payload_rows

    async def _load_catalog_watchlist_rows(
        self,
        expiry: str,
        underlyings: list[UnderlyingMeta],
    ) -> list[dict[str, Any]]:
        if not underlyings:
            return []
        expiry_date = self._parse_expiry(expiry)
        if expiry_date is None:
            return []
        underlying_symbols = [meta.symbol for meta in underlyings]
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    WITH latest_spot AS (
                        SELECT DISTINCT ON (underlying)
                            underlying,
                            close AS spot_price,
                            time
                        FROM underlying_spot_candles
                        WHERE interval = '1minute'
                          AND underlying = ANY(:underlyings)
                          AND close IS NOT NULL
                        ORDER BY underlying, time DESC
                    ),
                    latest_premium_spot AS (
                        SELECT DISTINCT ON (underlying)
                            underlying,
                            underlying_price AS spot_price,
                            time
                        FROM option_premium_candles
                        WHERE expiry = :expiry
                          AND underlying = ANY(:underlyings)
                          AND underlying_price IS NOT NULL
                          AND underlying_price > 0
                        ORDER BY underlying, time DESC
                    ),
                    median_strikes AS (
                        SELECT
                            underlying,
                            percentile_cont(0.5) WITHIN GROUP (ORDER BY strike::float8) AS median_strike
                        FROM fo_contract_catalog
                        WHERE expiry = :expiry
                          AND underlying = ANY(:underlyings)
                          AND option_type IN ('CE', 'PE')
                        GROUP BY underlying
                    ),
                    price_refs AS (
                        SELECT
                            med.underlying,
                            COALESCE(spot.spot_price, premium.spot_price, med.median_strike) AS reference_price
                        FROM median_strikes med
                        LEFT JOIN latest_spot spot
                          ON spot.underlying = med.underlying
                        LEFT JOIN latest_premium_spot premium
                          ON premium.underlying = med.underlying
                    ),
                    atm_strikes AS (
                        SELECT DISTINCT ON (catalog.underlying)
                            catalog.underlying,
                            catalog.strike,
                            refs.reference_price AS spot_price
                        FROM fo_contract_catalog catalog
                        JOIN price_refs refs
                          ON refs.underlying = catalog.underlying
                        WHERE catalog.expiry = :expiry
                          AND catalog.underlying = ANY(:underlyings)
                          AND catalog.option_type IN ('CE', 'PE')
                        ORDER BY catalog.underlying,
                                 ABS(catalog.strike::float8 - refs.reference_price::float8),
                                 catalog.strike
                    )
                    SELECT
                        atm.underlying,
                        COALESCE(underlying_catalog.kind, 'STOCK') AS kind,
                        atm.strike,
                        atm.spot_price,
                        COALESCE(ce.lot_size, pe.lot_size, underlying_catalog.lot_size) AS lot_size,
                        ce.instrument_key AS ce_instrument_key,
                        ce.trading_symbol AS ce_trading_symbol,
                        pe.instrument_key AS pe_instrument_key,
                        pe.trading_symbol AS pe_trading_symbol
                    FROM atm_strikes atm
                    LEFT JOIN fo_contract_catalog ce
                      ON ce.underlying = atm.underlying
                     AND ce.expiry = :expiry
                     AND ce.strike = atm.strike
                     AND ce.option_type = 'CE'
                    LEFT JOIN fo_contract_catalog pe
                      ON pe.underlying = atm.underlying
                     AND pe.expiry = :expiry
                     AND pe.strike = atm.strike
                     AND pe.option_type = 'PE'
                    LEFT JOIN fo_underlying_catalog underlying_catalog
                      ON underlying_catalog.symbol = atm.underlying
                    WHERE ce.instrument_key IS NOT NULL
                       OR pe.instrument_key IS NOT NULL
                    ORDER BY CASE WHEN COALESCE(underlying_catalog.kind, 'STOCK') = 'INDEX' THEN 0 ELSE 1 END,
                             atm.underlying
                    """
                ),
                {"expiry": expiry_date, "underlyings": underlying_symbols},
            )
            rows = result.fetchall()

        meta_by_symbol = {meta.symbol: meta for meta in underlyings}
        payload_rows: list[dict[str, Any]] = []
        for row in rows:
            meta = meta_by_symbol.get(str(row.underlying))
            if meta is None:
                continue

            def _option_payload(prefix: str, option_type: str) -> Optional[dict[str, Any]]:
                instrument_key = getattr(row, f"{prefix}_instrument_key")
                trading_symbol = getattr(row, f"{prefix}_trading_symbol")
                if not instrument_key and not trading_symbol:
                    return None
                return {
                    "strike": float(row.strike),
                    "option_type": option_type,
                    "instrument_key": instrument_key,
                    "trading_symbol": trading_symbol,
                    "ltp": 0.0,
                    "prev_close": None,
                    "change": None,
                    "change_pct": None,
                    "oi": 0,
                    "prev_oi": None,
                    "oi_change": None,
                    "oi_change_pct": None,
                    "volume": 0,
                    "iv": None,
                    "macd": None,
                    "macd_signal": None,
                    "macd_histogram": None,
                    "rsi": None,
                }

            payload_rows.append(
                {
                    "underlying": str(row.underlying),
                    "kind": str(row.kind),
                    "spot_price": round(float(row.spot_price or 0.0), 2),
                    "expiry": expiry,
                    "atm_strike": float(row.strike),
                    "live_source": "catalog",
                    "fyers_symbol": self._to_fyers_symbol(meta),
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

    async def _upsert_default_index_rows(self, metas: list[UnderlyingMeta]) -> None:
        """Insert missing index defaults into fo_underlying_catalog so
        the per-cycle 'missing default index rows' warning stops firing.
        Idempotent — ON CONFLICT DO NOTHING."""
        if not metas:
            return
        try:
            payload = [
                {
                    "symbol": meta.symbol.upper(),
                    "kind": meta.kind or "INDEX",
                    "spot_instrument_key": meta.spot_instrument_key,
                    "underlying_key": meta.underlying_key,
                }
                for meta in metas
            ]
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text(
                        """
                        INSERT INTO fo_underlying_catalog (
                            symbol, kind, spot_instrument_key, underlying_key
                        ) VALUES (
                            :symbol, :kind, :spot_instrument_key, :underlying_key
                        )
                        ON CONFLICT (symbol) DO UPDATE
                        SET spot_instrument_key = COALESCE(
                                EXCLUDED.spot_instrument_key,
                                fo_underlying_catalog.spot_instrument_key
                            ),
                            underlying_key = COALESCE(
                                EXCLUDED.underlying_key,
                                fo_underlying_catalog.underlying_key
                            ),
                            kind = COALESCE(
                                NULLIF(EXCLUDED.kind, ''),
                                fo_underlying_catalog.kind
                            )
                        """
                    ),
                    payload,
                )
                await session.commit()
        except Exception as exc:
            # Self-heal is best-effort. If the table schema differs in
            # this deployment, fall back silently — the caller still
            # uses the in-memory default list for the current cycle.
            logger.debug(f"[ATM watchlist] default-index self-heal skipped: {exc}")

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
            by_symbol = {row.symbol.upper(): row for row in rows}
            missing_defaults = [
                meta for meta in DEFAULT_INDEX_UNDERLYINGS
                if meta.symbol.upper() not in by_symbol
            ]
            if missing_defaults:
                # Self-heal: upsert the missing default index rows so the
                # next caller does not hit this branch. Was previously a
                # per-cycle warning that flooded logs every 30-60s.
                await self._upsert_default_index_rows(missing_defaults)
                # Log once per process so the operator sees the patch
                # without scan-cycle spam.
                missing_key = frozenset(meta.symbol.upper() for meta in missing_defaults)
                if missing_key not in _DEFAULT_INDEX_WARNING_SEEN:
                    _DEFAULT_INDEX_WARNING_SEEN.add(missing_key)
                    logger.info(
                        "[ATM watchlist] Self-healed missing default index rows in "
                        f"fo_underlying_catalog: {', '.join(meta.symbol for meta in missing_defaults)}."
                    )
                default_symbols = {meta.symbol.upper() for meta in DEFAULT_INDEX_UNDERLYINGS}
                rows = [
                    by_symbol.get(meta.symbol.upper(), meta)
                    for meta in DEFAULT_INDEX_UNDERLYINGS
                ] + [row for row in rows if row.symbol.upper() not in default_symbols]
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
        analytics_token = str(get_broker_token("upstox") or "").strip()
        if analytics_token:
            try:
                fallback = UpstoxAdapter()
                await fallback.authenticate({"access_token": analytics_token})
                logger.info("[ATM watchlist] Using saved Upstox analytics token for read-only chain refresh")
                return fallback
            except Exception as exc:
                logger.debug(f"[ATM watchlist] Upstox analytics-token fallback failed: {exc}")
        return None

    @staticmethod
    def _to_fyers_symbol(meta: UnderlyingMeta) -> str:
        if meta.kind == "INDEX":
            # BSE indices use BSE: prefix; NSE indices use NSE: prefix
            # Explicit mapping takes precedence over the fallback
            return INDEX_FYERS_SYMBOLS.get(meta.symbol, f"NSE:{meta.symbol}-INDEX")
        return f"NSE:{meta.symbol}-EQ"

    async def _list_known_monthly_expiries(self, symbol: str) -> set[date]:
        """Monthly-expiry master: query fo_expiry_catalog for the rows
        the F&O catalog has marked as a monthly for this symbol."""
        try:
            async with AsyncSessionLocal() as session:
                rows = await session.execute(
                    text(
                        """
                        SELECT expiry
                        FROM fo_expiry_catalog
                        WHERE underlying = :symbol
                          AND expiry >= CURRENT_DATE
                        ORDER BY expiry
                        """
                    ),
                    {"symbol": symbol.upper()},
                )
                return {row.expiry for row in rows.fetchall()}
        except Exception as exc:
            logger.debug(f"[ATM watchlist] monthly-master lookup failed for {symbol}: {exc}")
            return set()

    async def _resolve_expiry_from_master(
        self,
        *,
        meta: "UnderlyingMeta",
        profile,  # StrategyContractProfile
        today: Optional[date] = None,
        upstox_adapter: Optional[BrokerAdapter] = None,
        fyers_adapter: Optional[BrokerAdapter] = None,
    ) -> Optional[date]:
        """Pick the expiry the strategy wants from the actual available
        list returned by the brokers (instrument master), filtered
        against fo_expiry_catalog to distinguish weekly from monthly.

        Order of truth:
          1. Broker option-chain reports all available expiries for the
             symbol (weekly + monthly, holiday-shifted, expired-skipped).
          2. fo_expiry_catalog records which of those are MONTHLY
             contracts. Everything in (1) that is NOT in (2) is a
             weekly.
          3. The legacy weekday helpers (_next_weekly_expiry,
             get_monthly_expiry) are only used as a last-resort
             fallback when the broker chain is completely unavailable
             (e.g. weekend with no cached chain).
        """
        today = today or date.today()
        # 1. Broker chain — the authoritative list of tradable expiries.
        broker_isos, _source = await self._get_broker_expiry_snapshot_for_symbol(
            meta, upstox_adapter, fyers_adapter
        )
        broker_dates = sorted({
            date.fromisoformat(s) for s in broker_isos if s
        })
        # 2. Monthly master — which of those are monthlies.
        monthly_set = await self._list_known_monthly_expiries(meta.symbol)

        kind = (meta.kind or "").upper()
        allow_t0 = profile.index_allow_t0 if kind == "INDEX" else True

        def _filter_future(dates: list[date]) -> list[date]:
            return [d for d in dates if (d > today if not allow_t0 else d >= today)]

        if not broker_dates:
            # Last-resort fallback: synthesise expiries from the
            # weekday helpers so the system doesn't go dark when broker
            # chain is unavailable. This is the only path that still
            # uses date arithmetic.
            logger.debug(
                f"[ATM watchlist] broker chain empty for {meta.symbol}; "
                "falling back to weekday-based expiry math."
            )
            if kind == "INDEX" and profile.index_expiry == "weekly":
                w = _next_weekly_expiry(meta.symbol, today=today)
                if w is None:
                    return None
                if w == today and not allow_t0:
                    w = w + timedelta(days=7)
                return w
            # monthly fallback
            anchor = today
            try:
                cand = get_index_monthly_expiry(meta.symbol, anchor.year, anchor.month)
            except Exception:
                cand = get_monthly_expiry(anchor.year, anchor.month)
            if cand < today or (cand == today and not allow_t0):
                nxt = (cand + timedelta(days=4)).replace(day=28)
                try:
                    cand = get_index_monthly_expiry(meta.symbol, nxt.year, nxt.month)
                except Exception:
                    cand = get_monthly_expiry(nxt.year, nxt.month)
            if kind == "STOCK" and profile.stock_rollover_td > 0:
                if _trading_days_until(cand, today=today) <= profile.stock_rollover_td:
                    nxt = (cand + timedelta(days=4)).replace(day=28)
                    cand = get_monthly_expiry(nxt.year, nxt.month)
            return cand

        candidates = _filter_future(broker_dates)
        if not candidates:
            return None

        # Weekly preference (indices only). Pick the nearest expiry
        # that is NOT a known monthly. If everything in the chain is a
        # monthly (some symbols only trade monthly), fall back to the
        # nearest available.
        if kind == "INDEX" and profile.index_expiry == "weekly":
            weeklies = [d for d in candidates if d not in monthly_set]
            return weeklies[0] if weeklies else candidates[0]

        # Monthly preference (default for stocks, configurable for indices).
        monthlies = [d for d in candidates if d in monthly_set]
        if not monthlies:
            # Broker chain returned only weeklies (or fo_expiry_catalog
            # missing rows for this symbol). Take the nearest available
            # contract — better than returning None.
            return candidates[0]

        nearest_monthly = monthlies[0]
        if kind == "STOCK" and profile.stock_rollover_td > 0:
            if _trading_days_until(nearest_monthly, today=today) <= profile.stock_rollover_td:
                # Roll to the next monthly if available.
                for d in monthlies[1:]:
                    return d
        return nearest_monthly

    async def get_watchlist_for_strategy(
        self,
        profile,  # StrategyContractProfile
        *,
        symbols: list[str],
        live_refresh: bool = False,
    ) -> dict[str, Any]:
        """Build a watchlist tailored to a strategy's contract profile.

        This is the strategy-aware entry point. The profile tells MI:
          * which expiry to resolve per symbol (weekly vs monthly)
          * whether T-0 (expiry day) is allowed for indices
          * how aggressively to roll stocks on near-expiry
          * how tight to make the strike-neighbour search

        Today's S1 callers can keep using get_watchlist() — that path
        is unchanged. Strategies with different needs (S2, future
        directional intraday) call this method with their profile.

        The implementation groups symbols by their resolved expiry and
        issues parallel get_watchlist() requests scoped to each
        expiry+symbol-set so the broker chain queries stay efficient.
        """
        if not symbols:
            return {"rows": [], "source": "no_symbols", "profile": profile.name}
        today = date.today()
        # Resolve each symbol's preferred expiry from the instruments
        # master (broker chain + fo_expiry_catalog), not weekday math.
        # If brokers are unavailable the resolver falls back to date
        # arithmetic as a last resort.
        underlyings = await self._load_underlyings()
        meta_by_symbol = {u.symbol.upper(): u for u in underlyings}
        upstox_adapter = await self._get_upstox_adapter()
        fyers_adapter = get_active_adapter("fyers")
        resolutions: dict[str, Optional[date]] = {}
        for sym in symbols:
            su = sym.upper()
            meta = meta_by_symbol.get(su)
            if meta is None:
                # Synthesise a minimal meta when the symbol isn't in
                # fo_underlying_catalog yet — resolver only needs
                # `.symbol` and `.kind` to query the broker.
                synthesised_kind = (
                    "INDEX"
                    if su in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
                              "SENSEX", "BANKEX", "NIFTYNXT50"}
                    else "STOCK"
                )
                meta = UnderlyingMeta(
                    symbol=su,
                    kind=synthesised_kind,
                    spot_instrument_key="",
                    underlying_key="",
                )
            resolutions[su] = await self._resolve_expiry_from_master(
                meta=meta,
                profile=profile,
                today=today,
                upstox_adapter=upstox_adapter,
                fyers_adapter=fyers_adapter,
            )
        # Bucket by expiry so we hit the broker per-expiry, not per-symbol.
        by_expiry: dict[str, list[str]] = {}
        unresolved: list[str] = []
        for sym, exp in resolutions.items():
            if exp is None:
                unresolved.append(sym)
                continue
            by_expiry.setdefault(exp.isoformat(), []).append(sym)

        if not by_expiry:
            return {
                "rows": [],
                "source": "no_resolved_expiries",
                "profile": profile.name,
                "unresolved": unresolved,
            }

        # Issue parallel watchlist requests, one per resolved expiry.
        import asyncio as _asyncio
        payloads = await _asyncio.gather(
            *(
                self.get_watchlist(
                    expiry=exp_iso,
                    symbols=sym_list,
                    live_refresh=live_refresh,
                )
                for exp_iso, sym_list in by_expiry.items()
            ),
            return_exceptions=True,
        )

        # Merge rows, tagging each with the profile + resolved expiry.
        merged_rows: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for (exp_iso, sym_list), payload in zip(by_expiry.items(), payloads):
            if isinstance(payload, Exception):
                errors.append({"expiry": exp_iso, "symbols": sym_list, "error": str(payload)})
                continue
            for row in list((payload or {}).get("rows") or []):
                if str(row.get("underlying") or "").upper() in {s.upper() for s in sym_list}:
                    row = dict(row)
                    row["strategy_profile"] = profile.name
                    row["profile_resolved_expiry"] = exp_iso
                    merged_rows.append(row)

        return {
            "rows": merged_rows,
            "source": "strategy_profile",
            "profile": profile.name,
            "expiries_requested": list(by_expiry.keys()),
            "unresolved_symbols": unresolved,
            "errors": errors,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    async def get_next_expiry_row(
        self,
        symbol: str,
        *,
        live_refresh: bool = False,
    ) -> Optional[dict[str, Any]]:
        """Feedback API the strategy calls when the active-expiry row
        for *symbol* is unusable (data stale, insufficient bars, etc.)
        and it wants to evaluate the NEXT expiry instead.

        MI owns instrument selection — the strategy should never roll
        expiries by itself. This method returns the watchlist row for
        the next monthly expiry of *symbol*, building it if needed.
        Returns None when no next expiry exists (e.g. broker quote
        chain doesn't have the further-out contract yet).
        """
        symbol = str(symbol or "").upper().strip()
        if not symbol:
            return None
        today = date.today()
        # Resolve the active monthly first, then jump one month forward
        # so we always land on the *next* monthly regardless of where
        # we currently are in the cycle.
        active_monthly = get_monthly_expiry(today.year, today.month)
        if active_monthly <= today:
            anchor = (active_monthly + timedelta(days=4)).replace(day=28)
            active_monthly = get_monthly_expiry(anchor.year, anchor.month)
        next_anchor = (active_monthly + timedelta(days=4)).replace(day=28)
        next_monthly = get_monthly_expiry(next_anchor.year, next_anchor.month)
        try:
            payload = await self.get_watchlist(
                expiry=next_monthly.isoformat(),
                symbols=[symbol],
                live_refresh=live_refresh,
            )
        except Exception as exc:
            logger.debug(
                f"[ATM watchlist] get_next_expiry_row({symbol}) failed: {exc}"
            )
            return None
        rows = list((payload or {}).get("rows") or [])
        for row in rows:
            if str(row.get("underlying") or "").upper() == symbol:
                return row
        return None


atm_watchlist_service = ATMWatchlistService()
