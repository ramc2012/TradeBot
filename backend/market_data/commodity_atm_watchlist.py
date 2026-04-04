"""Commodity ATM CE/PE watchlist built from saved MCX futures symbols."""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timezone
from typing import Any, Optional

from loguru import logger

from api.routers.auth import ensure_fyers_session, get_active_adapter
from brokers.base import BrokerAdapter, OptionChainEntry


UTC = timezone.utc
_MCX_FUTURE_SYMBOL_RE = re.compile(r"^(?P<exchange>MCX):(?P<root>[A-Z0-9]+)\d{2}[A-Z]{3}FUT$")
_MCX_FUTURE_PARTS_RE = re.compile(r"^(?P<exchange>MCX):(?P<root>[A-Z0-9]+)(?P<year>\d{2})(?P<month>[A-Z]{3})FUT$")
_MCX_OPTION_ROOT_ALIASES: dict[str, tuple[str, ...]] = {
    "SILVERMIC": ("SILVERM",),
}


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
    match = _MCX_FUTURE_SYMBOL_RE.match(str(symbol or "").strip().upper())
    if match:
        return str(match.group("root"))
    token = str(symbol or "").strip().upper().split(":")[-1]
    return token or str(symbol or "").strip().upper()


def _expand_option_lookup_candidates(symbol: str) -> list[str]:
    raw_symbol = str(symbol or "").strip().upper()
    match = _MCX_FUTURE_PARTS_RE.match(raw_symbol)
    if not match:
        return [raw_symbol] if raw_symbol else []

    root = str(match.group("root"))
    year = str(match.group("year"))
    month = str(match.group("month"))
    candidates = [raw_symbol]
    for alias_root in _MCX_OPTION_ROOT_ALIASES.get(root, ()):
        alias_symbol = f"MCX:{alias_root}{year}{month}FUT"
        if alias_symbol not in candidates:
            candidates.append(alias_symbol)
    return candidates


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
            lookup_symbol = symbol
            alias_note: Optional[str] = None
            if isinstance(contract_result, Exception):
                logger.debug(f"[Commodity ATM] Expiry discovery failed for {symbol}: {contract_result}")
                row_expiries: list[str] = []
            else:
                lookup_symbol = str(contract_result.get("lookup_symbol") or symbol)
                alias_note = contract_result.get("alias_note")
                row_expiries = sorted({str(item) for item in contract_result.get("expiries", []) if item})

            selected_expiry = selected_option_expiries.get(symbol)
            suggested_expiry = _select_default_expiry(row_expiries)
            active_expiry = _resolve_active_expiry(
                row_expiries,
                selected_expiry=selected_expiry,
            )
            if not row_expiries:
                unsupported_symbols.append(symbol)

            contracts.append(
                {
                    "symbol": symbol,
                    "underlying": underlying,
                    "lookup_symbol": lookup_symbol,
                    "expiries": row_expiries,
                    "selected_expiry": selected_expiry if selected_expiry in row_expiries else None,
                    "suggested_expiry": suggested_expiry,
                    "active_expiry": active_expiry,
                    "has_options": bool(row_expiries),
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
                    }
                for item in contracts
            ],
            "timestamp": catalog.get("timestamp"),
        }

    async def get_watchlist(
        self,
        symbols: list[str],
        selected_option_expiries: Optional[dict[str, str]] = None,
        expiry: Optional[str] = None,
    ) -> dict[str, Any]:
        if isinstance(selected_option_expiries, str) and expiry is None:
            expiry = selected_option_expiries
            selected_option_expiries = None
        catalog = await self.get_contract_catalog(symbols, selected_option_expiries)
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
            if not active_expiry:
                if row_expiries:
                    skipped_symbols.append(str(item.get("symbol")))
                continue
            selected_contracts.append({**item, "active_expiry": active_expiry})

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

        for index, item in enumerate(selected_contracts):
            if index:
                await asyncio.sleep(0.25)
            try:
                result = await self._build_row(
                    adapter=adapter,
                    symbol=str(item["symbol"]),
                    underlying=str(item["underlying"]),
                    lookup_symbol=str(item.get("lookup_symbol") or item["symbol"]),
                    expiry=str(item["active_expiry"]),
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
                        "lookup_symbol": item.get("lookup_symbol"),
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
        if await ensure_fyers_session():
            return get_active_adapter("fyers")
        return None

    async def _get_symbol_expiries(self, adapter: BrokerAdapter, symbol: str) -> list[str]:
        contracts = await adapter.get_option_contracts(symbol)
        return [str(item.get("expiry")) for item in contracts if item.get("expiry")]

    async def _load_symbol_contracts(self, adapter: BrokerAdapter, symbol: str) -> dict[str, Any]:
        failures: list[str] = []
        candidates = _expand_option_lookup_candidates(symbol)
        for index, candidate in enumerate(candidates):
            if index:
                await asyncio.sleep(0.1)
            try:
                expiries = await self._get_symbol_expiries(adapter, candidate)
            except Exception as exc:
                failures.append(str(exc))
                continue
            if expiries:
                alias_note = None
                if candidate != symbol:
                    alias_note = f"Using {candidate} option chain for {symbol}."
                return {
                    "lookup_symbol": candidate,
                    "expiries": expiries,
                    "alias_note": alias_note,
                }
        if failures:
            raise ValueError(failures[-1])
        raise ValueError("There are no expiry contracts.")

    async def _build_row(
        self,
        *,
        adapter: BrokerAdapter,
        symbol: str,
        underlying: str,
        lookup_symbol: str,
        expiry: str,
    ) -> Optional[dict[str, Any]]:
        chain = await adapter.get_option_chain(lookup_symbol, expiry)
        strikes = sorted(
            {
                float(entry.strike)
                for entry in chain.entries
                if str(entry.option_type).upper() in {"CE", "PE"}
            }
        )
        if not strikes:
            return None

        spot_price = float(chain.spot_price or 0.0)
        if spot_price <= 0:
            return None

        atm_strike = min(strikes, key=lambda strike: abs(strike - spot_price))
        ce_entry = next(
            (
                entry for entry in chain.entries
                if str(entry.option_type).upper() == "CE" and float(entry.strike) == atm_strike
            ),
            None,
        )
        pe_entry = next(
            (
                entry for entry in chain.entries
                if str(entry.option_type).upper() == "PE" and float(entry.strike) == atm_strike
            ),
            None,
        )
        if ce_entry is None and pe_entry is None:
            return None

        return {
            "underlying": underlying,
            "symbol": symbol,
            "kind": "MCX",
            "spot_price": round(spot_price, 2),
            "expiry": str(chain.expiry or expiry),
            "atm_strike": atm_strike,
            "live_source": "fyers",
            "fyers_symbol": lookup_symbol,
            "ce": _serialize_option(ce_entry),
            "pe": _serialize_option(pe_entry),
        }


commodity_atm_watchlist_service = CommodityATMWatchlistService()
