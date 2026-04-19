"""Commodity ATM CE/PE watchlist built from saved MCX futures symbols."""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timezone
from typing import Any, Optional

from loguru import logger

from api.routers.auth import ensure_fyers_session, get_active_adapter
from brokers.base import BrokerAdapter, OptionChainEntry
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


def _select_default_expiry(expiries: list[str], *, as_of: Optional[date] = None) -> Optional[str]:
    if not expiries:
        return None
    current = as_of or date.today()
    current_iso = current.isoformat()
    return next((expiry for expiry in expiries if expiry >= current_iso), expiries[0])


def _resolve_active_expiry(
    expiries: list[str],
    *,
    selected_expiry: Optional[str] = None,
    override_expiry: Optional[str] = None,
) -> Optional[str]:
    if override_expiry and override_expiry in expiries:
        return override_expiry
    if selected_expiry and selected_expiry in expiries:
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

    async def get_contract_catalog(
        self,
        symbols: list[str],
        selected_option_expiries: Optional[dict[str, str]] = None,
        selected_option_lookup_symbols: Optional[dict[str, str]] = None,
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

        adapter = await self._get_fyers_adapter()
        if adapter is None:
            return {
                "contracts": [],
                "summary": {"total_symbols": len(normalized), "contracts_ready": 0, "active_selections": 0},
                "source": "none",
                "detail": "Fyers is not connected, so commodity option contracts are unavailable.",
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
        symbol_contracts: list[dict[str, Any] | Exception] = []
        for index, symbol in enumerate(normalized):
            if index:
                await asyncio.sleep(0.15)
            try:
                symbol_contracts.append(await self._load_symbol_contracts(adapter, symbol))
            except Exception as exc:
                symbol_contracts.append(exc)

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
        return {
            "contracts": contracts,
            "summary": {
                "total_symbols": len(normalized),
                "contracts_ready": sum(1 for item in contracts if item["has_options"]),
                "active_selections": sum(1 for item in contracts if item["active_expiry"]),
            },
            "source": "fyers",
            "detail": detail,
            "timestamp": datetime.now(UTC).isoformat(),
        }

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
        catalog = await self.get_contract_catalog(symbols, selected_option_expiries, selected_option_lookup_symbols)
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
            return {
                "expiry": expiry,
                "rows": [],
                "summary": {"total_rows": 0, "ce_ready": 0, "pe_ready": 0},
                "source": "none",
                "detail": "Fyers is not connected, so the commodity ATM watchlist cannot be built.",
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
        active_lookup_symbols = [
            str(item.get("active_lookup_symbol") or item.get("lookup_symbol") or item.get("symbol") or "")
            for item in selected_contracts
        ]
        live_quote_map = await self._get_live_spot_quotes(adapter, active_lookup_symbols)

        for index, item in enumerate(selected_contracts):
            if index:
                await asyncio.sleep(0.25)
            lookup_symbol = str(item.get("active_lookup_symbol") or item.get("lookup_symbol") or item.get("symbol") or "")
            quote_payload = live_quote_map.get(lookup_symbol) or {}
            try:
                result = await self._build_row(
                    adapter=adapter,
                    symbol=str(item["symbol"]),
                    underlying=str(item["underlying"]),
                    lookup_symbol=lookup_symbol,
                    expiry=str(item["active_expiry"]),
                    live_spot_price=quote_payload.get("price"),
                    live_quote_source=str(quote_payload.get("source") or ""),
                )
            except Exception as exc:
                symbol = str(item["symbol"])
                chain_failures.append(f"{symbol} ({item['active_expiry']})")
                logger.warning(f"[Commodity ATM] Failed to build {symbol} {item['active_expiry']}: {exc}")
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
            "detail": _build_detail_message(
                unsupported_symbols=unsupported_symbols,
                skipped_symbols=skipped_symbols,
                selected_expiry=expiry,
                chain_failures=chain_failures,
                row_count=len(rows),
            ),
            "timestamp": datetime.now(UTC).isoformat(),
        }
        return payload

    async def _get_fyers_adapter(self) -> Optional[BrokerAdapter]:
        adapter = get_active_adapter("fyers")
        if adapter:
            return adapter
        if await ensure_fyers_session(force_validate=True):
            return get_active_adapter("fyers")
        return None

    async def _get_symbol_expiries(self, adapter: BrokerAdapter, symbol: str) -> list[str]:
        contracts = await adapter.get_option_contracts(symbol)
        return [str(item.get("expiry")) for item in contracts if item.get("expiry")]

    async def _get_live_spot_quotes(self, adapter: BrokerAdapter, symbols: list[str]) -> dict[str, dict[str, Any]]:
        requested_symbols = [symbol for symbol in _normalize_commodity_symbols(symbols) if symbol]
        if not requested_symbols:
            return {}

        quotes: dict[str, dict[str, Any]] = {}
        upstox_quotes = await load_upstox_mcx_quotes(requested_symbols)
        for symbol, value in upstox_quotes.items():
            if value > 0:
                quotes[symbol] = {"price": value, "source": "upstox"}

        remaining_symbols = [symbol for symbol in requested_symbols if symbol not in quotes]
        if not remaining_symbols:
            return quotes

        try:
            payload = await adapter.get_ltp(remaining_symbols)
        except Exception as exc:
            logger.warning(f"[Commodity ATM] Live MCX quote fetch failed: {exc}")
            return quotes

        for symbol in remaining_symbols:
            try:
                value = float(payload.get(symbol, 0) or 0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                quotes[symbol] = {"price": value, "source": "fyers"}
        return quotes

    async def _load_symbol_contracts(self, adapter: BrokerAdapter, symbol: str) -> dict[str, Any]:
        failures: list[str] = []
        candidates = _build_contract_discovery_candidates(symbol)
        expiry_candidates: dict[str, list[dict[str, str]]] = {}
        for index, candidate in enumerate(candidates):
            if index:
                await asyncio.sleep(0.1)
            try:
                expiries = await self._get_symbol_expiries(adapter, candidate)
            except Exception as exc:
                failures.append(str(exc))
                continue
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
            }
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
        chain = await adapter.get_option_chain(lookup_symbol, expiry)
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
