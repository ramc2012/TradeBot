"""Commodity ATM CE/PE watchlist built from saved MCX futures symbols."""
from __future__ import annotations

import asyncio
import time
from copy import deepcopy
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from loguru import logger

from api.routers.auth import ensure_fyers_session, get_active_adapter
from brokers.base import BrokerAdapter, OptionChainEntry
from core.config import settings
from market_data.commodity_contract_specs import (
    canonicalize_commodity_root,
    extract_commodity_root,
    get_commodity_contract_spec,
)
from market_data.upstox_commodity import load_upstox_mcx_quotes


UTC = timezone.utc
_MCX_FUTURE_PARTS_RE = re.compile(
    r"^(?P<exchange>MCX):(?P<root>[A-Z0-9]+?)(?P<year>\d{2})(?P<month>[A-Z]{3})FUT$"
)
_MONTH_CODES = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_MONTH_TO_NUMBER = {code: index + 1 for index, code in enumerate(_MONTH_CODES)}
_MCX_OPTION_ROOT_ALIASES: dict[str, tuple[str, ...]] = {
    "SILVERMIC": ("SILVERM",),
}
# MCX option expiries can extend well beyond the saved future's contract month.
# Scan a wider futures ladder so far-month option expiries do not collapse onto
# the last nearby future we happened to discover.
_MCX_DISCOVERY_MONTH_OFFSETS = tuple(range(-1, 10))

# ── Deadlines (2026-08-06) ───────────────────────────────────────────────────
# This module shipped with ZERO bounds on its broker awaits — the defect class
# fixed for the NSE watchlist on 2026-07-08 was never applied here. Measured
# 2026-08-06 07:38 IST: get_watchlist() was still running when a caller's 45s
# asyncio.wait_for fired.
#
# The arithmetic behind the hang: get_contract_catalog() probes a wide futures
# ladder per saved symbol (11 month offsets x root aliases). For the live set of
# 8 MCX symbols that is 99 SERIAL Fyers /options-chain-v3 calls, and each one
# enters FyersAdapter._get_data_json, which retries 5 times with a 15s HTTP
# timeout and exponential back-off — ~90s worst case for a SINGLE call, so ~2.5
# hours worst case for one sweep. Even the happy path carries a 13.3s floor of
# hard-coded anti-429 spacing sleeps before a single byte moves.
#
# Two bounds, because one is not enough:
#   * per-await   — no single broker read can wedge the build (the 07-08 fix),
#   * per-build   — the SERIAL ladder as a whole cannot outrun its caller, and
#                   returns a partial payload instead of being cancelled.
# Returning beats being cancelled: a cancelled build discards every probe it
# completed, so the next poll starts from zero and the endpoint never converges.
BROKER_EXPIRY_CALL_TIMEOUT_SECONDS = 12.0
BROKER_CHAIN_CALL_TIMEOUT_SECONDS = 25.0
BROKER_QUOTE_CALL_TIMEOUT_SECONDS = 12.0
BROKER_SESSION_TIMEOUT_SECONDS = 15.0
# Both budgets sit under the 45s prewarm bound in core.paper_bootstrap.
CATALOG_BUILD_BUDGET_SECONDS = 40.0
WATCHLIST_BUILD_BUDGET_SECONDS = 40.0
# Don't start a broker call we cannot finish inside the remaining budget.
_MIN_CALL_HEADROOM_SECONDS = 1.0

# Per-candidate expiry-probe memo. Keyed by the *candidate* future symbol (not
# the saved symbol) so it also dedupes the overlap between two saved contracts
# sharing a root. This is what makes a deadline-truncated sweep resumable: the
# memo is mutated synchronously as each probe lands, so even a caller that
# cancels us at 2s (see fno_analytics._load_mcx_snapshot) leaves the probes it
# did pay for behind, and the next poll continues from there.
EXPIRY_PROBE_MEMO_TTL_SECONDS = 900.0
EXPIRY_PROBE_NEGATIVE_MEMO_TTL_SECONDS = 120.0

_MEMO_MISS = object()


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def _bounded(awaitable: Any, *, timeout: float, label: str) -> Any:
    """Run a single broker await under a hard ceiling.

    Re-raised as a plain ``TimeoutError`` carrying the call label, because the
    thing that made this class of hang expensive to diagnose was never the
    absence of a stack trace — it was a bare timeout with nothing naming the
    call that burned it. ``CancelledError`` still propagates untouched so an
    outer deadline can cancel us.
    """
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"{label} exceeded {timeout:.0f}s") from exc


def _remaining(deadline: Optional[float]) -> float:
    """Seconds left before ``deadline`` (a ``time.monotonic()`` stamp)."""
    if deadline is None:
        return float("inf")
    return deadline - time.monotonic()


def _is_rate_limit_error(exc: Exception | str) -> bool:
    text = str(exc).lower()
    return "429" in text or "limit reached" in text or "too many requests" in text


def _normalize_commodity_symbols(symbols: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = str(raw or "").strip().upper()
        if not symbol.startswith("MCX:"):
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        cleaned.append(symbol)
    return cleaned


def _extract_commodity_root(symbol: str) -> str:
    return extract_commodity_root(symbol)


def _expand_option_lookup_candidates(symbol: str) -> list[str]:
    raw_symbol = str(symbol or "").strip().upper()
    match = _MCX_FUTURE_PARTS_RE.match(raw_symbol)
    if not match:
        return [raw_symbol] if raw_symbol else []

    root = str(match.group("root"))
    year = str(match.group("year"))
    month = str(match.group("month"))
    candidates = [raw_symbol]
    alias_candidates = _MCX_OPTION_ROOT_ALIASES.get(root, ())
    if not alias_candidates:
        alias_candidates = _MCX_OPTION_ROOT_ALIASES.get(canonicalize_commodity_root(root), ())
    for alias_root in alias_candidates:
        alias_symbol = f"MCX:{alias_root}{year}{month}FUT"
        if alias_symbol not in candidates:
            candidates.append(alias_symbol)
    return candidates


def _parse_iso_date(value: str) -> Optional[date]:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


def _filter_upcoming_expiries(expiries: list[str], *, as_of: Optional[date] = None) -> list[str]:
    current = as_of or date.today()
    filtered = []
    for expiry in expiries:
        parsed = _parse_iso_date(expiry)
        if parsed is None or parsed < current:
            continue
        filtered.append(parsed.isoformat())
    return sorted(set(filtered))


def _parse_future_contract_month(symbol: str) -> Optional[tuple[str, str, int, int]]:
    raw_symbol = str(symbol or "").strip().upper()
    match = _MCX_FUTURE_PARTS_RE.match(raw_symbol)
    if not match:
        return None
    exchange = str(match.group("exchange"))
    root = str(match.group("root"))
    year = 2000 + int(match.group("year"))
    month = _MONTH_TO_NUMBER.get(str(match.group("month")))
    if month is None:
        return None
    return exchange, root, year, month


def _add_months(year: int, month: int, offset: int) -> tuple[int, int]:
    total = (year * 12) + (month - 1) + offset
    return total // 12, (total % 12) + 1


def _format_future_contract_symbol(exchange: str, root: str, year: int, month: int) -> str:
    return f"{exchange}:{root}{year % 100:02d}{_MONTH_CODES[month - 1]}FUT"


def _build_contract_discovery_candidates(symbol: str) -> list[str]:
    parsed = _parse_future_contract_month(symbol)
    if parsed is None:
        return _expand_option_lookup_candidates(symbol)

    exchange, root, year, month = parsed
    candidate_roots = [root]
    alias_roots = _MCX_OPTION_ROOT_ALIASES.get(root, ())
    if not alias_roots:
        alias_roots = _MCX_OPTION_ROOT_ALIASES.get(canonicalize_commodity_root(root), ())
    for alias_root in alias_roots:
        if alias_root not in candidate_roots:
            candidate_roots.append(alias_root)

    candidates: list[str] = []
    seen: set[str] = set()
    for candidate_root in candidate_roots:
        for offset in _MCX_DISCOVERY_MONTH_OFFSETS:
            candidate_year, candidate_month = _add_months(year, month, offset)
            candidate = _format_future_contract_symbol(exchange, candidate_root, candidate_year, candidate_month)
            if candidate in seen:
                continue
            seen.add(candidate)
            candidates.append(candidate)
    return candidates


def _expiry_lookup_priority(lookup_symbol: str, expiry: str, *, preferred_symbol: str) -> tuple[int, int, int, str]:
    parsed_expiry = _parse_iso_date(expiry)
    parsed_lookup = _parse_future_contract_month(lookup_symbol)
    if parsed_expiry is None or parsed_lookup is None:
        return (9_999, 9_999, 1 if lookup_symbol != preferred_symbol else 0, lookup_symbol)

    _, _, contract_year, contract_month = parsed_lookup
    expiry_serial = parsed_expiry.year * 12 + parsed_expiry.month
    contract_serial = contract_year * 12 + contract_month
    if contract_serial < expiry_serial:
        month_gap = expiry_serial - contract_serial
        return (1, month_gap, 1 if lookup_symbol != preferred_symbol else 0, lookup_symbol)

    month_gap = contract_serial - expiry_serial
    return (0, month_gap, 1 if lookup_symbol != preferred_symbol else 0, lookup_symbol)


def _resolve_expiry_lookup_symbol(
    expiry_mappings: list[dict[str, Any]],
    expiry: Optional[str],
    *,
    default_symbol: Optional[str] = None,
) -> Optional[str]:
    if expiry:
        for item in expiry_mappings:
            if str(item.get("expiry")) == expiry:
                return str(item.get("lookup_symbol") or "").strip() or default_symbol
    return default_symbol


_MIN_TTE_DAYS_FOR_AUTO_SELECT = 5
_DURABLE_CACHE_KEY = "commodity_atm_watchlist_cache_v1"


def _freeze_cache_key(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_freeze_cache_key(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_cache_key(item) for item in value)
    return value


def _select_default_expiry(expiries: list[str], *, as_of: Optional[date] = None) -> Optional[str]:
    """Return the nearest expiry that gives at least
    ``_MIN_TTE_DAYS_FOR_AUTO_SELECT`` days of time to expiry. This prevents
    auto-selecting a 0-3 day TTE contract (which the strategy's TTE filter
    would reject anyway), wasting a watchlist slot on something that cannot
    trade. Falls back to the first non-expired expiry if no viable one is
    found, and to the first listed expiry as a last resort.
    """
    if not expiries:
        return None
    current = as_of or date.today()
    current_iso = current.isoformat()
    min_iso = (current + timedelta(days=_MIN_TTE_DAYS_FOR_AUTO_SELECT)).isoformat()
    viable = next((expiry for expiry in expiries if expiry >= min_iso), None)
    if viable:
        return viable
    not_expired = next((expiry for expiry in expiries if expiry >= current_iso), None)
    return not_expired or expiries[0]


def _resolve_active_expiry(
    expiries: list[str],
    *,
    selected_expiry: Optional[str] = None,
    override_expiry: Optional[str] = None,
    auto_rotate_below_min_tte: bool = True,
) -> Optional[str]:
    """Resolve the expiry the watchlist should load.

    Honours an explicit override or a user-selected expiry, but if that
    selection has TTE < ``_MIN_TTE_DAYS_FOR_AUTO_SELECT`` and a viable
    further-out expiry exists, auto-rotate to it. Prevents the watchlist
    from carrying a "selected" 1-2 day expiry that the strategy will refuse
    to trade.
    """
    if override_expiry and override_expiry in expiries:
        return override_expiry

    current = date.today()
    min_iso = (current + timedelta(days=_MIN_TTE_DAYS_FOR_AUTO_SELECT)).isoformat()

    if selected_expiry and selected_expiry in expiries:
        if not auto_rotate_below_min_tte or selected_expiry >= min_iso:
            return selected_expiry
        # Selected expiry is below TTE minimum — try to find a viable one.
        viable = next((expiry for expiry in expiries if expiry >= min_iso), None)
        if viable:
            return viable
        # No viable expiry exists; honour the user's selection rather than
        # silently swapping it for one that already expired.
        return selected_expiry
    return _select_default_expiry(expiries)


def _selection_is_still_active(expiry: Optional[str], *, as_of: Optional[date] = None) -> bool:
    parsed = _parse_iso_date(str(expiry or "").strip())
    if parsed is None:
        return False
    current = as_of or date.today()
    return parsed >= current


def _serialize_option(entry: Optional[OptionChainEntry]) -> Optional[dict[str, Any]]:
    if entry is None:
        return None

    ltp = float(entry.ltp or 0.0)
    prev_close = float(entry.prev_close) if entry.prev_close is not None else None
    change = round(ltp - prev_close, 2) if prev_close is not None else None
    change_pct = round((change / prev_close) * 100.0, 2) if prev_close not in (None, 0) and change is not None else None
    prev_oi = float(entry.prev_oi) if entry.prev_oi is not None else None
    oi = float(entry.oi or 0.0)
    oi_change = round(oi - prev_oi, 2) if prev_oi is not None else None
    oi_change_pct = round((oi_change / prev_oi) * 100.0, 2) if prev_oi not in (None, 0) and oi_change is not None else None

    return {
        "strike": float(entry.strike),
        "option_type": str(entry.option_type),
        "instrument_key": entry.instrument_key,
        "trading_symbol": entry.instrument_key,
        "ltp": ltp,
        "prev_close": prev_close,
        "change": change,
        "change_pct": change_pct,
        "oi": int(oi),
        "prev_oi": prev_oi,
        "oi_change": oi_change,
        "oi_change_pct": oi_change_pct,
        "volume": int(entry.volume or 0),
        "iv": float(entry.iv) if entry.iv is not None else None,
        "delta": float(entry.delta) if entry.delta is not None else None,
        "gamma": float(entry.gamma) if entry.gamma is not None else None,
        "theta": float(entry.theta) if entry.theta is not None else None,
        "vega": float(entry.vega) if entry.vega is not None else None,
        "macd": None,
        "macd_signal": None,
        "macd_histogram": None,
        "rsi": None,
    }


def _spread_ratio(entry: OptionChainEntry) -> float:
    bid = float(entry.bid or 0.0)
    ask = float(entry.ask or 0.0)
    ltp = float(entry.ltp or 0.0)
    anchor = ltp or max(bid, ask, 1.0)
    if anchor <= 0:
        return 1.0
    spread = max(ask - bid, 0.0)
    return spread / anchor


def _liquidity_score(entry: OptionChainEntry) -> float:
    volume = float(entry.volume or 0.0)
    oi = float(entry.oi or 0.0)
    bid = float(entry.bid or 0.0)
    ask = float(entry.ask or 0.0)
    score = (volume * 1.0) + (oi * 0.35)
    if bid > 0 and ask > 0:
        score += 50.0
    score -= _spread_ratio(entry) * 200.0
    return score


def _is_liquid_entry(entry: OptionChainEntry) -> bool:
    volume = int(entry.volume or 0)
    oi = int(entry.oi or 0)
    bid = float(entry.bid or 0.0)
    ask = float(entry.ask or 0.0)
    spread_ok = bid > 0 and ask > 0 and _spread_ratio(entry) <= 0.08
    depth_ok = volume >= 20 or oi >= 100
    return (
        spread_ok
        and depth_ok
    )


def _strike_step(strikes: list[float]) -> float:
    positive_steps = sorted(
        {
            round(abs(right - left), 6)
            for left, right in zip(strikes, strikes[1:])
            if abs(right - left) > 0
        }
    )
    return positive_steps[0] if positive_steps else 1.0


def _select_nearest_liquid_entry(
    entries: list[OptionChainEntry],
    *,
    spot_price: float,
    reference_strike: float,
    strike_step: float,
) -> tuple[Optional[OptionChainEntry], dict[str, Any]]:
    if not entries:
        return None, {
            "selection_mode": "missing",
            "liquidity_score": None,
            "distance_steps": None,
            "distance_from_atm": None,
            "is_liquid": False,
        }

    ranked = sorted(
        entries,
        key=lambda entry: (
            abs(float(entry.strike) - spot_price),
            -_liquidity_score(entry),
        ),
    )
    liquid_ranked = [entry for entry in ranked if _is_liquid_entry(entry)]
    chosen = liquid_ranked[0] if liquid_ranked else ranked[0]
    distance_from_atm = abs(float(chosen.strike) - reference_strike)
    distance_steps = distance_from_atm / max(strike_step, 1e-9)
    mode = "nearest_liquid" if chosen.strike != reference_strike else "atm"
    if not liquid_ranked:
        mode = "atm_fallback" if chosen.strike == reference_strike else "nearest_available"
    return chosen, {
        "selection_mode": mode,
        "liquidity_score": round(_liquidity_score(chosen), 2),
        "distance_steps": round(distance_steps, 2),
        "distance_from_atm": round(distance_from_atm, 2),
        "is_liquid": _is_liquid_entry(chosen),
    }


def _build_detail_message(
    *,
    unsupported_symbols: list[str],
    skipped_symbols: list[str],
    selected_expiry: Optional[str],
    chain_failures: list[str],
    row_count: int,
) -> Optional[str]:
    parts: list[str] = []
    if unsupported_symbols:
        parts.append(
            "No Fyers option expiries were returned for: " + ", ".join(unsupported_symbols) + "."
        )
    if selected_expiry and skipped_symbols:
        parts.append(
            f"{len(skipped_symbols)} saved MCX symbols do not have {selected_expiry} option contracts."
        )
    if chain_failures:
        parts.append("Chain load failed for: " + ", ".join(chain_failures) + ".")
    if row_count == 0 and not parts:
        parts.append("No saved MCX symbols match the selected option expiry.")
    return " ".join(parts) if parts else None


class CommodityATMWatchlistService:
    """Build an MCX ATM watchlist from the commodity page's saved symbols."""

    def __init__(self) -> None:
        self._contract_catalog_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._watchlist_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._fyers_backoff_until: datetime | None = None
        self._last_rate_limit_error: str | None = None
        self._durable_cache_restored = False
        # candidate symbol -> (monotonic stamp, expiries | None on failure)
        self._expiry_probe_memo: dict[str, tuple[float, Optional[list[str]]]] = {}

    def _memoized_expiry_probe(self, candidate: str) -> Any:
        entry = self._expiry_probe_memo.get(candidate)
        if entry is None:
            return _MEMO_MISS
        stamp, expiries = entry
        ttl = (
            EXPIRY_PROBE_MEMO_TTL_SECONDS
            if expiries is not None
            else EXPIRY_PROBE_NEGATIVE_MEMO_TTL_SECONDS
        )
        if (time.monotonic() - stamp) > ttl:
            self._expiry_probe_memo.pop(candidate, None)
            return _MEMO_MISS
        return list(expiries) if expiries is not None else None

    def _store_expiry_probe(self, candidate: str, expiries: Optional[list[str]]) -> None:
        self._expiry_probe_memo[candidate] = (
            time.monotonic(),
            list(expiries) if expiries is not None else None,
        )

    def _restore_durable_cache(self) -> None:
        if self._durable_cache_restored:
            return
        self._durable_cache_restored = True
        try:
            from core.runtime_state import load_runtime_state

            payload, _ = load_runtime_state(_DURABLE_CACHE_KEY)
        except Exception as exc:
            logger.debug(f"[Commodity ATM] Durable cache restore skipped: {exc}")
            return
        if not isinstance(payload, dict):
            return
        for item in list(payload.get("contract_catalog") or []):
            if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
                continue
            key = _freeze_cache_key(item.get("key"))
            if isinstance(key, tuple):
                self._contract_catalog_cache[key] = dict(item["payload"])
        for item in list(payload.get("watchlist") or []):
            if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
                continue
            key = _freeze_cache_key(item.get("key"))
            if isinstance(key, tuple):
                self._watchlist_cache[key] = dict(item["payload"])

    def _persist_durable_cache(self) -> None:
        try:
            from core.runtime_state import save_runtime_state

            payload = {
                "contract_catalog": [
                    {"key": list(key), "payload": value}
                    for key, value in list(self._contract_catalog_cache.items())[-20:]
                ],
                "watchlist": [
                    {"key": list(key), "payload": value}
                    for key, value in list(self._watchlist_cache.items())[-20:]
                ],
                "updated_at": _utc_now().isoformat(),
            }
            save_runtime_state(_DURABLE_CACHE_KEY, payload)
        except Exception as exc:
            logger.debug(f"[Commodity ATM] Durable cache persist skipped: {exc}")

    def _catalog_cache_key(
        self,
        symbols: list[str],
        selected_option_expiries: dict[str, str],
        selected_option_lookup_symbols: dict[str, str],
    ) -> tuple[Any, ...]:
        return (
            tuple(symbols),
            tuple(sorted(selected_option_expiries.items())),
            tuple(sorted(selected_option_lookup_symbols.items())),
        )

    def _watchlist_cache_key(
        self,
        symbols: list[str],
        selected_option_expiries: dict[str, str],
        selected_option_lookup_symbols: dict[str, str],
        expiry: str | None,
    ) -> tuple[Any, ...]:
        return (
            *self._catalog_cache_key(symbols, selected_option_expiries, selected_option_lookup_symbols),
            str(expiry or ""),
        )

    def _in_backoff(self) -> bool:
        return self._fyers_backoff_until is not None and _utc_now() < self._fyers_backoff_until

    def _mark_rate_limit(self, error: Exception | str) -> None:
        self._last_rate_limit_error = str(error)
        self._fyers_backoff_until = _utc_now() + timedelta(
            seconds=max(int(settings.COMMODITY_FYERS_RATE_LIMIT_BACKOFF_SECONDS), 15)
        )

    def _clear_rate_limit(self) -> None:
        self._fyers_backoff_until = None
        self._last_rate_limit_error = None

    def _cached_payload(
        self,
        cache: dict[tuple[Any, ...], dict[str, Any]],
        key: tuple[Any, ...],
        *,
        label: str,
    ) -> dict[str, Any] | None:
        self._restore_durable_cache()
        cached = cache.get(key)
        if cached is None:
            return None
        payload = deepcopy(cached)
        detail_parts = [str(payload.get("detail") or "").strip()]
        if self._last_rate_limit_error:
            detail_parts.append(
                f"Reusing the last good {label} while Fyers rate limits cool down. ({self._last_rate_limit_error})"
            )
        else:
            detail_parts.append(f"Reusing the last good {label} while Fyers rate limits cool down.")
        payload["detail"] = " ".join(part for part in detail_parts if part).strip()
        payload["cache_reused"] = True
        payload["timestamp"] = _utc_now().isoformat()
        if self._fyers_backoff_until is not None:
            payload["backoff_until"] = self._fyers_backoff_until.isoformat()
        return payload

    def _store_cache(
        self,
        cache: dict[tuple[Any, ...], dict[str, Any]],
        key: tuple[Any, ...],
        payload: dict[str, Any],
    ) -> None:
        cache[key] = deepcopy(payload)
        self._persist_durable_cache()

    def _static_contract_catalog(
        self,
        symbols: list[str],
        selected_option_expiries: dict[str, str],
        selected_option_lookup_symbols: dict[str, str],
        *,
        detail: str,
        source: str,
    ) -> dict[str, Any]:
        contracts: list[dict[str, Any]] = []
        for symbol in symbols:
            spec = get_commodity_contract_spec(symbol)
            underlying = _extract_commodity_root(symbol)
            selected_expiry = selected_option_expiries.get(symbol)
            selected_lookup_symbol = selected_option_lookup_symbols.get(symbol) or symbol
            expiry_mappings = (
                [{"expiry": selected_expiry, "lookup_symbol": selected_lookup_symbol}]
                if selected_expiry
                else []
            )
            contracts.append(
                {
                    "symbol": symbol,
                    "underlying": underlying or spec.root,
                    "lookup_symbol": symbol,
                    "expiries": [selected_expiry] if selected_expiry else [],
                    "selected_expiry": selected_expiry,
                    "suggested_expiry": selected_expiry,
                    "active_expiry": selected_expiry,
                    "has_options": bool(selected_expiry),
                    "active_lookup_symbol": selected_lookup_symbol,
                    "default_lookup_symbol": symbol,
                    "expiry_mappings": expiry_mappings,
                    "selected_lookup_symbol": selected_lookup_symbol if selected_expiry else None,
                    "selection_policy": "saved_static_fallback" if selected_expiry else "static_metadata_only",
                    "selection_locked": bool(selected_expiry),
                    "lot_size": spec.futures_lot_size,
                    "contract_unit_label": spec.contract_unit_label,
                    "quote_unit_label": spec.quote_unit_label,
                    "strategy_title": spec.options_label,
                    "detail": "Live MCX expiry discovery is unavailable; showing saved/static contract metadata.",
                }
            )
        payload: dict[str, Any] = {
            "contracts": contracts,
            "summary": {
                "total_symbols": len(contracts),
                "contracts_ready": sum(1 for item in contracts if item.get("has_options")),
                "active_selections": sum(1 for item in contracts if item.get("active_expiry")),
            },
            "source": source,
            "detail": detail,
            "timestamp": _utc_now().isoformat(),
        }
        if self._fyers_backoff_until is not None:
            payload["backoff_until"] = self._fyers_backoff_until.isoformat()
        return payload

    def get_cached_contract_catalog(
        self,
        symbols: list[str],
        selected_option_expiries: Optional[dict[str, str]] = None,
        selected_option_lookup_symbols: Optional[dict[str, str]] = None,
    ) -> dict[str, Any] | None:
        normalized = _normalize_commodity_symbols(symbols)
        if not normalized:
            return None
        self._restore_durable_cache()
        cache_key = self._catalog_cache_key(
            normalized,
            {
                str(symbol).strip().upper(): str(expiry).strip()
                for symbol, expiry in dict(selected_option_expiries or {}).items()
                if str(symbol).strip() and str(expiry).strip()
            },
            {
                str(symbol).strip().upper(): str(lookup_symbol).strip().upper()
                for symbol, lookup_symbol in dict(selected_option_lookup_symbols or {}).items()
                if str(symbol).strip() and str(lookup_symbol).strip()
            },
        )
        cached = self._contract_catalog_cache.get(cache_key)
        if cached is None:
            return None
        payload = deepcopy(cached)
        payload["cache_reused"] = True
        payload["timestamp"] = _utc_now().isoformat()
        return payload

    def get_cached_watchlist(
        self,
        symbols: list[str],
        selected_option_expiries: Optional[dict[str, str]] = None,
        selected_option_lookup_symbols: Optional[dict[str, str]] = None,
        expiry: Optional[str] = None,
    ) -> dict[str, Any] | None:
        normalized = _normalize_commodity_symbols(symbols)
        if not normalized:
            return None
        self._restore_durable_cache()
        cache_key = self._watchlist_cache_key(
            normalized,
            {
                str(symbol).strip().upper(): str(selected_expiry).strip()
                for symbol, selected_expiry in dict(selected_option_expiries or {}).items()
                if str(symbol).strip() and str(selected_expiry).strip()
            },
            {
                str(symbol).strip().upper(): str(lookup_symbol).strip().upper()
                for symbol, lookup_symbol in dict(selected_option_lookup_symbols or {}).items()
                if str(symbol).strip() and str(lookup_symbol).strip()
            },
            expiry,
        )
        cached = self._watchlist_cache.get(cache_key)
        if cached is None:
            return None
        payload = deepcopy(cached)
        payload["cache_reused"] = True
        payload["timestamp"] = _utc_now().isoformat()
        return payload

    async def get_contract_catalog(
        self,
        symbols: list[str],
        selected_option_expiries: Optional[dict[str, str]] = None,
        selected_option_lookup_symbols: Optional[dict[str, str]] = None,
        *,
        deadline: Optional[float] = None,
    ) -> dict[str, Any]:
        normalized = _normalize_commodity_symbols(symbols)
        if not normalized:
            return {
                "contracts": [],
                "summary": {"total_symbols": 0, "contracts_ready": 0, "active_selections": 0},
                "source": "none",
                "detail": "Save MCX symbols to list commodity option contracts.",
                "timestamp": datetime.now(UTC).isoformat(),
            }

        selected_option_expiries = {
            str(symbol).strip().upper(): str(expiry).strip()
            for symbol, expiry in dict(selected_option_expiries or {}).items()
            if str(symbol).strip() and str(expiry).strip()
        }
        selected_option_lookup_symbols = {
            str(symbol).strip().upper(): str(lookup_symbol).strip().upper()
            for symbol, lookup_symbol in dict(selected_option_lookup_symbols or {}).items()
            if str(symbol).strip() and str(lookup_symbol).strip()
        }
        cache_key = self._catalog_cache_key(normalized, selected_option_expiries, selected_option_lookup_symbols)
        self._restore_durable_cache()
        adapter = await self._get_fyers_adapter()
        if adapter is None:
            cached_payload = self._cached_payload(
                self._contract_catalog_cache,
                cache_key,
                label="commodity contract catalog",
            )
            if cached_payload is not None:
                cached_payload["source"] = str(cached_payload.get("source") or "durable_cache")
                return cached_payload
            return self._static_contract_catalog(
                normalized,
                selected_option_expiries,
                selected_option_lookup_symbols,
                detail=(
                    "Fyers is not connected. Showing saved MCX futures with "
                    "static contract metadata until live expiry discovery returns."
                ),
                source="static_without_broker",
            )
        if self._in_backoff():
            cached_payload = self._cached_payload(
                self._contract_catalog_cache,
                cache_key,
                label="commodity contract catalog",
            )
            if cached_payload is not None:
                return cached_payload
            return self._static_contract_catalog(
                normalized,
                selected_option_expiries,
                selected_option_lookup_symbols,
                detail=(
                    "Fyers contract discovery is cooling down after rate limits. "
                    "Showing saved MCX futures with static contract metadata."
                ),
                source="fyers_rate_limit_static",
            )
        if deadline is None:
            deadline = time.monotonic() + CATALOG_BUILD_BUDGET_SECONDS
        symbol_contracts: list[dict[str, Any] | Exception] = []
        rate_limit_errors: list[str] = []
        truncated = False
        broker_probes: list[str] = []
        for index, symbol in enumerate(normalized):
            if _remaining(deadline) <= _MIN_CALL_HEADROOM_SECONDS:
                truncated = True
                symbol_contracts.extend(
                    TimeoutError("Expiry discovery budget exhausted before this symbol.")
                    for _ in normalized[index:]
                )
                break
            if broker_probes:
                # Inter-call spacing for expiry discovery. Previously 0.15s
                # — too aggressive; the 4 MCX symbols hit Fyers data API
                # within ~0.6s and all four 429'd. 0.6s spreads to ~2.4s,
                # well under Fyers burst budget. Gated on probes actually sent,
                # so a memo-warm symbol pays no spacing for traffic it never
                # generated — that is what keeps a warm sweep near-instant.
                await asyncio.sleep(0.6)
            try:
                result = await self._load_symbol_contracts(
                    adapter, symbol, deadline=deadline, probed=broker_probes
                )
                truncated = truncated or bool(result.get("truncated"))
                symbol_contracts.append(result)
            except Exception as exc:
                symbol_contracts.append(exc)
                if isinstance(exc, TimeoutError):
                    truncated = True
                if _is_rate_limit_error(exc):
                    rate_limit_errors.append(str(exc))
                    # Skip remaining symbols this cycle — we're hard-limited
                    # right now and further calls will also 429.
                    symbol_contracts.extend(
                        RuntimeError(str(exc)) for _ in normalized[index + 1 :]
                    )
                    break

        contracts: list[dict[str, Any]] = []
        unsupported_symbols: list[str] = []

        for symbol, contract_result in zip(normalized, symbol_contracts):
            underlying = _extract_commodity_root(symbol)
            spec = get_commodity_contract_spec(symbol)
            lookup_symbol = symbol
            alias_note: Optional[str] = None
            expiry_mappings: list[dict[str, Any]] = []
            if isinstance(contract_result, Exception):
                logger.debug(f"[Commodity ATM] Expiry discovery failed for {symbol}: {contract_result}")
                row_expiries: list[str] = []
            else:
                lookup_symbol = str(contract_result.get("lookup_symbol") or symbol)
                alias_note = contract_result.get("alias_note")
                row_expiries = sorted({str(item) for item in contract_result.get("expiries", []) if item})
                expiry_mappings = [
                    {
                        "expiry": str(item.get("expiry")),
                        "lookup_symbol": str(item.get("lookup_symbol") or lookup_symbol),
                    }
                    for item in list(contract_result.get("expiry_mappings") or [])
                    if item.get("expiry")
                ]

            selected_expiry = selected_option_expiries.get(symbol)
            selected_lookup_symbol = selected_option_lookup_symbols.get(symbol)
            pinned_selection = bool(selected_expiry and _selection_is_still_active(selected_expiry))
            contract_expiries = list(row_expiries)
            if pinned_selection and selected_expiry not in contract_expiries:
                contract_expiries.append(selected_expiry)
                contract_expiries = sorted(set(contract_expiries))
            suggested_expiry = _select_default_expiry(row_expiries)
            active_expiry = (
                selected_expiry
                if pinned_selection
                else _resolve_active_expiry(
                    contract_expiries,
                    selected_expiry=selected_expiry,
                )
            )
            resolved_lookup_symbol = _resolve_expiry_lookup_symbol(
                expiry_mappings,
                active_expiry,
                default_symbol=lookup_symbol,
            ) or symbol
            active_lookup_symbol = (
                selected_lookup_symbol
                if pinned_selection and selected_lookup_symbol
                else resolved_lookup_symbol
            )
            if not row_expiries:
                unsupported_symbols.append(symbol)

            contracts.append(
                {
                    "symbol": symbol,
                    "underlying": underlying,
                    "lookup_symbol": lookup_symbol,
                    "expiries": contract_expiries,
                    "selected_expiry": selected_expiry if pinned_selection else (selected_expiry if selected_expiry in row_expiries else None),
                    "suggested_expiry": suggested_expiry,
                    "active_expiry": active_expiry,
                    "has_options": bool(row_expiries),
                    "active_lookup_symbol": active_lookup_symbol,
                    "default_lookup_symbol": lookup_symbol,
                    "expiry_mappings": expiry_mappings,
                    "selected_lookup_symbol": active_lookup_symbol if pinned_selection else None,
                    "selection_policy": "pinned_until_expiry" if pinned_selection else "exchange_ladder",
                    "selection_locked": pinned_selection,
                    "lot_size": spec.futures_lot_size,
                    "contract_unit_label": spec.contract_unit_label,
                    "quote_unit_label": spec.quote_unit_label,
                    "strategy_title": spec.options_label,
                    "detail": alias_note or (None if row_expiries else "No option expiries returned by Fyers for this contract."),
                }
            )

        detail = _build_detail_message(
            unsupported_symbols=unsupported_symbols,
            skipped_symbols=[],
            selected_expiry=None,
            chain_failures=[],
            row_count=sum(1 for item in contracts if item["has_options"]),
        )
        if truncated:
            detail = " ".join(
                part
                for part in [
                    detail,
                    "Expiry discovery hit its time budget; this catalog is partial "
                    "and will fill in on the next call.",
                ]
                if part
            )
        payload = {
            "contracts": contracts,
            "summary": {
                "total_symbols": len(normalized),
                "contracts_ready": sum(1 for item in contracts if item["has_options"]),
                "active_selections": sum(1 for item in contracts if item["active_expiry"]),
            },
            "source": "fyers",
            "detail": detail,
            "partial": truncated,
            "timestamp": _utc_now().isoformat(),
        }
        # A truncated sweep is returned but never cached: get_cached_contract_catalog
        # has no TTL, so persisting a partial ladder would pin the symbols we never
        # reached out of the catalog indefinitely. Per-candidate probes are already
        # memoized, so the next call resumes cheaply and caches the complete result.
        if payload["summary"]["contracts_ready"] > 0 and not truncated:
            self._store_cache(self._contract_catalog_cache, cache_key, payload)
            if rate_limit_errors:
                self._mark_rate_limit(rate_limit_errors[-1])
            elif broker_probes:
                # Only a real, successful broker round-trip is evidence the rate
                # limit has lifted. A sweep served entirely from the probe memo
                # touches Fyers zero times, so clearing the backoff on it would
                # let the memo silently cancel an active cooldown.
                self._clear_rate_limit()
        elif rate_limit_errors:
            self._mark_rate_limit(rate_limit_errors[-1])
            cached_payload = self._cached_payload(
                self._contract_catalog_cache,
                cache_key,
                label="commodity contract catalog",
            )
            if cached_payload is not None:
                return cached_payload
            return self._static_contract_catalog(
                normalized,
                selected_option_expiries,
                selected_option_lookup_symbols,
                detail=(
                    "Fyers contract discovery hit rate limits and no cache is "
                    "available; using saved MCX expiry selections as fallback."
                ),
                source="fyers_rate_limit_static",
            )
        return payload

    async def get_expiries(self, symbols: list[str]) -> dict[str, Any]:
        catalog = await self.get_contract_catalog(symbols)
        contracts = list(catalog.get("contracts") or [])
        expiries = sorted({expiry for item in contracts for expiry in item.get("expiries", [])})
        return {
            "expiries": expiries,
            "default_expiry": _select_default_expiry(expiries),
            "source": catalog.get("source", "none"),
            "detail": catalog.get("detail"),
            "symbols": [
                {
                    "symbol": item["symbol"],
                    "underlying": item["underlying"],
                    "expiries": item["expiries"],
                    "selected_expiry": item.get("selected_expiry"),
                    "suggested_expiry": item.get("suggested_expiry"),
                    "active_expiry": item.get("active_expiry"),
                    "lookup_symbol": item.get("lookup_symbol"),
                    "active_lookup_symbol": item.get("active_lookup_symbol"),
                    "expiry_mappings": item.get("expiry_mappings"),
                    "selected_lookup_symbol": item.get("selected_lookup_symbol"),
                    "selection_policy": item.get("selection_policy"),
                    "selection_locked": item.get("selection_locked"),
                }
                for item in contracts
            ],
            "timestamp": catalog.get("timestamp"),
        }

    async def get_watchlist(
        self,
        symbols: list[str],
        selected_option_expiries: Optional[dict[str, str]] = None,
        selected_option_lookup_symbols: Optional[dict[str, str]] = None,
        expiry: Optional[str] = None,
    ) -> dict[str, Any]:
        if isinstance(selected_option_expiries, str) and expiry is None:
            expiry = selected_option_expiries
            selected_option_expiries = None
        selected_option_expiries = {
            str(symbol).strip().upper(): str(selected_expiry).strip()
            for symbol, selected_expiry in dict(selected_option_expiries or {}).items()
            if str(symbol).strip() and str(selected_expiry).strip()
        }
        selected_option_lookup_symbols = {
            str(symbol).strip().upper(): str(lookup_symbol).strip().upper()
            for symbol, lookup_symbol in dict(selected_option_lookup_symbols or {}).items()
            if str(symbol).strip() and str(lookup_symbol).strip()
        }
        # ONE budget spanning both phases (expiry discovery + chain rows). The
        # phases run back-to-back and each was independently unbounded, so a
        # per-phase budget would still let the pair overrun every caller.
        deadline = time.monotonic() + WATCHLIST_BUILD_BUDGET_SECONDS
        normalized_symbols = _normalize_commodity_symbols(symbols)
        cache_key = self._watchlist_cache_key(
            normalized_symbols,
            selected_option_expiries,
            selected_option_lookup_symbols,
            expiry,
        )
        if self._in_backoff():
            cached_payload = self._cached_payload(
                self._watchlist_cache,
                cache_key,
                label="commodity ATM watchlist",
            )
            if cached_payload is not None:
                return cached_payload
        catalog = self.get_cached_contract_catalog(
            normalized_symbols,
            selected_option_expiries,
            selected_option_lookup_symbols,
        ) or await self.get_contract_catalog(
            normalized_symbols,
            selected_option_expiries,
            selected_option_lookup_symbols,
            deadline=deadline,
        )
        contracts = list(catalog.get("contracts") or [])
        if not contracts:
            return {
                "expiry": expiry,
                "rows": [],
                "summary": {"total_rows": 0, "ce_ready": 0, "pe_ready": 0},
                "source": catalog.get("source", "none"),
                "detail": catalog.get("detail") or "No MCX option expiry is available for the saved symbols.",
                "timestamp": catalog.get("timestamp") or datetime.now(UTC).isoformat(),
            }

        adapter = await self._get_fyers_adapter()
        if adapter is None:
            cached_payload = self._cached_payload(
                self._watchlist_cache,
                cache_key,
                label="commodity ATM watchlist",
            )
            if cached_payload is not None:
                cached_payload["source"] = str(cached_payload.get("source") or "durable_cache")
                return cached_payload
            return {
                "expiry": expiry,
                "rows": [],
                "summary": {"total_rows": 0, "ce_ready": 0, "pe_ready": 0},
                "source": "none",
                "detail": "Fyers is not connected and no saved commodity ATM watchlist cache is available.",
                "timestamp": datetime.now(UTC).isoformat(),
            }

        selected_contracts: list[dict[str, Any]] = []
        skipped_symbols: list[str] = []
        unsupported_symbols = [str(item.get("symbol")) for item in contracts if not item.get("has_options")]
        for item in contracts:
            row_expiries = list(item.get("expiries") or [])
            if expiry and expiry not in row_expiries:
                if row_expiries:
                    skipped_symbols.append(str(item.get("symbol")))
                continue
            active_expiry = _resolve_active_expiry(
                row_expiries,
                selected_expiry=item.get("selected_expiry"),
                override_expiry=expiry,
            )
            if not expiry and item.get("selection_locked") and item.get("selected_expiry"):
                active_expiry = str(item.get("selected_expiry"))
            if not active_expiry:
                if row_expiries:
                    skipped_symbols.append(str(item.get("symbol")))
                continue
            active_lookup_symbol = (
                str(item.get("selected_lookup_symbol") or "").strip()
                if not expiry and item.get("selection_locked") and item.get("selected_lookup_symbol")
                else ""
            )
            if not active_lookup_symbol:
                active_lookup_symbol = _resolve_expiry_lookup_symbol(
                    list(item.get("expiry_mappings") or []),
                    active_expiry,
                    default_symbol=str(item.get("default_lookup_symbol") or item.get("lookup_symbol") or item.get("symbol") or ""),
                ) or str(item.get("symbol") or "")
            selected_contracts.append(
                {
                    **item,
                    "active_expiry": active_expiry,
                    "active_lookup_symbol": active_lookup_symbol,
                }
            )

        if not selected_contracts:
            return {
                "expiry": expiry,
                "rows": [],
                "summary": {"total_rows": 0, "ce_ready": 0, "pe_ready": 0},
                "source": "fyers",
                "detail": _build_detail_message(
                    unsupported_symbols=unsupported_symbols,
                    skipped_symbols=skipped_symbols,
                    selected_expiry=expiry,
                    chain_failures=[],
                    row_count=0,
                ),
                "timestamp": datetime.now(UTC).isoformat(),
            }

        skipped_symbols = [
            *skipped_symbols,
            *[
                str(item.get("symbol"))
                for item in contracts
                if item.get("has_options")
                and str(item.get("symbol")) not in {str(selected["symbol"]) for selected in selected_contracts}
            ],
        ]

        rows: list[dict[str, Any]] = []
        chain_failures: list[str] = []
        rate_limit_errors: list[str] = []
        active_lookup_symbols = [
            str(item.get("active_lookup_symbol") or item.get("lookup_symbol") or item.get("symbol") or "")
            for item in selected_contracts
        ]
        live_quote_map = await self._get_live_spot_quotes(adapter, active_lookup_symbols)

        rows_truncated = False
        for index, item in enumerate(selected_contracts):
            if _remaining(deadline) <= _MIN_CALL_HEADROOM_SECONDS:
                # Out of budget: return the rows we did build. Partial beats
                # being cancelled — a cancelled build banks nothing at all.
                rows_truncated = True
                logger.warning(
                    f"[Commodity ATM] Watchlist budget "
                    f"({WATCHLIST_BUILD_BUDGET_SECONDS:.0f}s) exhausted after "
                    f"{index}/{len(selected_contracts)} contracts; returning partial rows"
                )
                break
            if index:
                # Spread chain calls across ~2.4s for 4 commodities to keep
                # well under the Fyers data-API burst limit (~10 req/sec but
                # burst-sensitive). At the previous 0.25s spacing all four
                # calls landed in ~1s and routinely triggered 429s, leaving
                # the watchlist incomplete.
                await asyncio.sleep(0.6)
            lookup_symbol = str(item.get("active_lookup_symbol") or item.get("lookup_symbol") or item.get("symbol") or "")
            quote_payload = live_quote_map.get(lookup_symbol) or {}
            result = None
            symbol = str(item["symbol"])
            chain_attempts = 3
            for attempt in range(chain_attempts):
                try:
                    result = await self._build_row(
                        adapter=adapter,
                        symbol=symbol,
                        underlying=str(item["underlying"]),
                        lookup_symbol=lookup_symbol,
                        expiry=str(item["active_expiry"]),
                        live_spot_price=quote_payload.get("price"),
                        live_quote_source=str(quote_payload.get("source") or ""),
                    )
                    break
                except Exception as exc:
                    backoff = 0.75 * (2 ** attempt)
                    # Only retry if the back-off AND the retry itself still fit
                    # in the budget; otherwise the retry ladder is just a slower
                    # way to blow past the caller's timeout.
                    can_retry = (
                        _is_rate_limit_error(exc)
                        and attempt < chain_attempts - 1
                        and _remaining(deadline) > (backoff + _MIN_CALL_HEADROOM_SECONDS)
                    )
                    if can_retry:
                        logger.warning(
                            f"[Commodity ATM] 429 on {symbol} chain (attempt {attempt + 1}/{chain_attempts}); "
                            f"retrying in {backoff:.2f}s"
                        )
                        await asyncio.sleep(backoff)
                        continue
                    chain_failures.append(f"{symbol} ({item['active_expiry']})")
                    if _is_rate_limit_error(exc):
                        rate_limit_errors.append(str(exc))
                        self._mark_rate_limit(exc)
                        cached_payload = self._cached_payload(
                            self._watchlist_cache,
                            cache_key,
                            label="commodity ATM watchlist",
                        )
                        if cached_payload is not None:
                            return cached_payload
                    logger.warning(f"[Commodity ATM] Failed to build {symbol} {item['active_expiry']}: {exc}")
                    result = None
                    break
            if result is None:
                continue
            if result:
                rows.append(
                    {
                        **result,
                        "selected_expiry": item.get("selected_expiry"),
                        "suggested_expiry": item.get("suggested_expiry"),
                        "active_expiry": item.get("active_expiry"),
                        "available_expiries": list(item.get("expiries") or []),
                        "lookup_symbol": item.get("active_lookup_symbol") or item.get("lookup_symbol"),
                        "expiry_mappings": list(item.get("expiry_mappings") or []),
                        "selected_lookup_symbol": item.get("selected_lookup_symbol"),
                        "selection_policy": item.get("selection_policy"),
                        "selection_locked": item.get("selection_locked"),
                    }
                )

        rows.sort(key=lambda item: str(item["underlying"]))
        partial = rows_truncated or bool(catalog.get("partial"))
        detail = _build_detail_message(
            unsupported_symbols=unsupported_symbols,
            skipped_symbols=skipped_symbols,
            selected_expiry=expiry,
            chain_failures=chain_failures,
            row_count=len(rows),
        )
        if partial:
            detail = " ".join(
                part
                for part in [
                    detail,
                    f"Build hit its {WATCHLIST_BUILD_BUDGET_SECONDS:.0f}s budget; "
                    "these rows are partial and will fill in on the next call.",
                ]
                if part
            )
        payload = {
            "expiry": expiry,
            "rows": rows,
            "summary": {
                "total_rows": len(rows),
                "ce_ready": sum(1 for row in rows if row.get("ce")),
                "pe_ready": sum(1 for row in rows if row.get("pe")),
                "tracked_symbols": len(_normalize_commodity_symbols(symbols)),
                "configured_contracts": len(selected_contracts),
            },
            "source": "fyers",
            "detail": detail,
            "partial": partial,
            "timestamp": _utc_now().isoformat(),
        }
        if rate_limit_errors and payload["summary"]["total_rows"] == 0:
            self._mark_rate_limit(rate_limit_errors[-1])
            cached_payload = self._cached_payload(
                self._watchlist_cache,
                cache_key,
                label="commodity ATM watchlist",
            )
            if cached_payload is not None:
                return cached_payload
        # Same rule as the catalog: never persist a budget-truncated row set as
        # the cached watchlist — get_cached_watchlist has no TTL and would serve
        # the short book indefinitely.
        if payload["summary"]["total_rows"] > 0 and not partial:
            self._store_cache(self._watchlist_cache, cache_key, payload)
            if rate_limit_errors:
                self._mark_rate_limit(rate_limit_errors[-1])
            else:
                self._clear_rate_limit()
        elif rate_limit_errors:
            self._mark_rate_limit(rate_limit_errors[-1])
        return payload

    async def _get_fyers_adapter(self) -> Optional[BrokerAdapter]:
        adapter = get_active_adapter("fyers")
        if adapter:
            return adapter
        try:
            validated = await _bounded(
                ensure_fyers_session(force_validate=True),
                timeout=BROKER_SESSION_TIMEOUT_SECONDS,
                label="ensure_fyers_session",
            )
        except Exception as exc:
            logger.warning(f"[Commodity ATM] Fyers session validation failed: {exc}")
            return None
        if validated:
            return get_active_adapter("fyers")
        return None

    async def _get_symbol_expiries(self, adapter: BrokerAdapter, symbol: str) -> list[str]:
        contracts = await _bounded(
            adapter.get_option_contracts(symbol),
            timeout=BROKER_EXPIRY_CALL_TIMEOUT_SECONDS,
            label=f"get_option_contracts({symbol})",
        )
        return [str(item.get("expiry")) for item in contracts if item.get("expiry")]

    async def _get_live_spot_quotes(self, adapter: BrokerAdapter, symbols: list[str]) -> dict[str, dict[str, Any]]:
        requested_symbols = [symbol for symbol in _normalize_commodity_symbols(symbols) if symbol]
        if not requested_symbols:
            return {}

        quotes: dict[str, dict[str, Any]] = {}
        try:
            upstox_quotes = await _bounded(
                load_upstox_mcx_quotes(requested_symbols),
                timeout=BROKER_QUOTE_CALL_TIMEOUT_SECONDS,
                label="load_upstox_mcx_quotes",
            )
        except Exception as exc:
            logger.warning(f"[Commodity ATM] Upstox MCX quote fetch failed: {exc}")
            upstox_quotes = {}
        for symbol, value in upstox_quotes.items():
            if value > 0:
                quotes[symbol] = {"price": value, "source": "upstox"}

        remaining_symbols = [symbol for symbol in requested_symbols if symbol not in quotes]
        if not remaining_symbols:
            return quotes

        try:
            payload = await _bounded(
                adapter.get_ltp(remaining_symbols),
                timeout=BROKER_QUOTE_CALL_TIMEOUT_SECONDS,
                label="get_ltp(MCX spots)",
            )
        except Exception as exc:
            logger.warning(f"[Commodity ATM] Live MCX quote fetch failed: {exc}")
            if _is_rate_limit_error(exc):
                self._mark_rate_limit(exc)
            return quotes

        for symbol in remaining_symbols:
            try:
                value = float(payload.get(symbol, 0) or 0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                quotes[symbol] = {"price": value, "source": "fyers"}
        if quotes:
            self._clear_rate_limit()
        return quotes

    async def _load_symbol_contracts(
        self,
        adapter: BrokerAdapter,
        symbol: str,
        *,
        deadline: Optional[float] = None,
        probed: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Resolve a saved MCX future's option-expiry ladder.

        ``probed`` (optional) accumulates the candidate symbols we actually sent
        to the broker, so the caller can tell a real round-trip apart from a
        sweep served entirely out of the probe memo — the two must not be
        treated alike when deciding whether a rate-limit backoff has lifted.
        """
        failures: list[str] = []
        candidates = _build_contract_discovery_candidates(symbol)
        expiry_candidates: dict[str, list[dict[str, str]]] = {}
        issued_calls = 0
        truncated = False
        for candidate in candidates:
            memoized = self._memoized_expiry_probe(candidate)
            if memoized is not _MEMO_MISS:
                # Warm probe: no broker call, and no anti-429 spacing to pay.
                if memoized is None:
                    continue
                expiries = memoized
            else:
                if _remaining(deadline) <= _MIN_CALL_HEADROOM_SECONDS:
                    # Out of budget. Return what we resolved so far rather than
                    # pushing the caller past its own timeout; the probes we did
                    # land stay memoized, so the next call resumes here.
                    truncated = True
                    break
                if issued_calls:
                    await asyncio.sleep(0.1)
                issued_calls += 1
                if probed is not None:
                    probed.append(candidate)
                try:
                    expiries = await self._get_symbol_expiries(adapter, candidate)
                except Exception as exc:
                    if _is_rate_limit_error(exc):
                        # Transient by definition — never memoize a 429.
                        raise
                    failures.append(str(exc))
                    self._store_expiry_probe(candidate, None)
                    continue
                self._store_expiry_probe(candidate, expiries)
            upcoming_expiries = _filter_upcoming_expiries(expiries)
            for expiry in upcoming_expiries:
                expiry_candidates.setdefault(expiry, []).append(
                    {
                        "lookup_symbol": candidate,
                    }
                )
        if expiry_candidates:
            expiry_mappings: list[dict[str, str]] = []
            mapped_notes: list[str] = []
            for expiry in sorted(expiry_candidates):
                chosen = min(
                    expiry_candidates[expiry],
                    key=lambda item: _expiry_lookup_priority(
                        str(item["lookup_symbol"]),
                        expiry,
                        preferred_symbol=symbol,
                    ),
                )
                lookup_symbol = str(chosen["lookup_symbol"])
                expiry_mappings.append({"expiry": expiry, "lookup_symbol": lookup_symbol})
                if lookup_symbol != symbol:
                    mapped_notes.append(f"{expiry} -> {lookup_symbol}")
            alias_note = None
            if mapped_notes:
                alias_note = "Resolved MCX expiry map via underlying futures: " + "; ".join(mapped_notes[:4])
                if len(mapped_notes) > 4:
                    alias_note += "..."
            return {
                "lookup_symbol": expiry_mappings[0]["lookup_symbol"],
                "expiries": [item["expiry"] for item in expiry_mappings],
                "expiry_mappings": expiry_mappings,
                "alias_note": alias_note,
                "truncated": truncated,
            }
        if truncated:
            raise TimeoutError(
                f"Expiry discovery for {symbol} ran out of budget before any "
                "candidate resolved."
            )
        if failures:
            raise ValueError(failures[-1])
        raise ValueError("There are no upcoming expiry contracts.")

    async def _build_row(
        self,
        *,
        adapter: BrokerAdapter,
        symbol: str,
        underlying: str,
        lookup_symbol: str,
        expiry: str,
        live_spot_price: Optional[float] = None,
        live_quote_source: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        chain = await _bounded(
            adapter.get_option_chain(lookup_symbol, expiry),
            timeout=BROKER_CHAIN_CALL_TIMEOUT_SECONDS,
            label=f"get_option_chain({lookup_symbol}, {expiry})",
        )
        spec = get_commodity_contract_spec(symbol)
        ce_entries = [
            entry for entry in chain.entries if str(entry.option_type).upper() == "CE"
        ]
        pe_entries = [
            entry for entry in chain.entries if str(entry.option_type).upper() == "PE"
        ]
        strikes = sorted({float(entry.strike) for entry in [*ce_entries, *pe_entries]})
        if not strikes:
            return None

        try:
            spot_price = float(live_spot_price or 0.0)
        except (TypeError, ValueError):
            spot_price = 0.0
        if spot_price <= 0:
            spot_price = float(chain.spot_price or 0.0)
        if spot_price <= 0:
            return None

        atm_strike = min(strikes, key=lambda strike: abs(strike - spot_price))
        strike_step = _strike_step(strikes)
        ce_entry, ce_selection = _select_nearest_liquid_entry(
            ce_entries,
            spot_price=spot_price,
            reference_strike=atm_strike,
            strike_step=strike_step,
        )
        pe_entry, pe_selection = _select_nearest_liquid_entry(
            pe_entries,
            spot_price=spot_price,
            reference_strike=atm_strike,
            strike_step=strike_step,
        )
        if ce_entry is None and pe_entry is None:
            return None

        ce_payload = _serialize_option(ce_entry)
        pe_payload = _serialize_option(pe_entry)
        if ce_payload is not None:
            ce_payload.update(ce_selection)
        if pe_payload is not None:
            pe_payload.update(pe_selection)

        return {
            "underlying": underlying,
            "symbol": symbol,
            "kind": "MCX",
            "spot_price": round(spot_price, 2),
            "expiry": str(chain.expiry or expiry),
            "atm_strike": atm_strike,
            "live_source": str(live_quote_source or "fyers"),
            "fyers_symbol": lookup_symbol,
            "lot_size": spec.futures_lot_size,
            "contract_unit_label": spec.contract_unit_label,
            "quote_unit_label": spec.quote_unit_label,
            "strategy_title": spec.options_label,
            "contract_notes": spec.notes,
            "selection_policy": "nearest_liquid_contract",
            "ce": ce_payload,
            "pe": pe_payload,
        }


commodity_atm_watchlist_service = CommodityATMWatchlistService()
