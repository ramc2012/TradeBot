"""NSE + MCX F&O contract and analytics aggregation.

This module turns the app's existing research cache, ATM watchlist snapshots
and MCX commodity contract catalog into a contract-first analytics payload.
It deliberately reports data quality and risk context beside every analytics
summary so UI and agents do not treat incomplete data as tradeable.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable

from loguru import logger
from sqlalchemy import text

from db.database import AsyncSessionLocal
from market_data.commodity_atm_watchlist import commodity_atm_watchlist_service
from market_data.commodity_contract_specs import get_commodity_contract_spec
from paper_engine.commodity_strategy_agent import commodity_strategy_agent


INDEX_UNDERLYINGS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX"}
COMMODITY_INDEX_ROOTS = {"MCXBULLDEX", "MCXMETLDEX", "BULLDEX", "METLDEX"}
NEAR_EXPIRY_WARNING_DAYS = 5
COMMODITY_STATE_PATH = Path(__file__).resolve().parents[1] / "commodity_strategy.json"


@dataclass(frozen=True)
class CanonicalContract:
    contract_id: str
    exchange: str
    segment: str
    instrument_type: str
    underlying: str
    expiry_date: str | None = None
    strike_price: float | None = None
    option_type: str | None = None
    lot_size: int | None = None
    tick_size: float | None = None
    price_unit: str | None = None
    exercise_style: str | None = None
    settlement_type: str | None = None
    is_physical_settlement: bool = False
    is_devolvement_applicable: bool = False
    status: str = "active"
    source: str = "unknown"
    trading_symbol: str | None = None
    instrument_key: str | None = None
    freeze_quantity: int | None = None
    sync_status: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _iso_date(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def _days_to_expiry(value: Any) -> int | None:
    parsed = _parse_date(value)
    if parsed is None:
        return None
    return (parsed - date.today()).days


def _round(value: float | int | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _ratio(numerator: float | int | None, denominator: float | int | None, digits: int = 3) -> float | None:
    num = _safe_float(numerator)
    den = _safe_float(denominator)
    if num is None or den is None or den <= 0:
        return None
    return round(num / den, digits)


def _nse_instrument_type(underlying: str, option_type: str | None) -> str:
    is_index = underlying.upper() in INDEX_UNDERLYINGS
    if option_type in {"CE", "PE"}:
        return "OPTIDX" if is_index else "OPTSTK"
    return "FUTIDX" if is_index else "FUTSTK"


def _mcx_instrument_type(root: str, option_type: str | None) -> str:
    is_index = root.upper() in COMMODITY_INDEX_ROOTS
    if option_type in {"CE", "PE"}:
        return "OPTIDX" if is_index else "OPTFUT"
    return "FUTIDX" if is_index else "FUTCOM"


def _contract_id(
    exchange: str,
    segment: str,
    instrument_type: str,
    underlying: str,
    expiry: str | None,
    strike: float | None = None,
    option_type: str | None = None,
) -> str:
    parts = [exchange.upper(), segment.upper(), instrument_type.upper(), underlying.upper()]
    if expiry:
        parts.append(expiry)
    if option_type in {"CE", "PE"}:
        parts.extend([str(int(strike)) if strike is not None and float(strike).is_integer() else str(strike), option_type])
    return ":".join(parts)


def _contract_summary(contracts: Iterable[CanonicalContract]) -> dict[str, Any]:
    rows = list(contracts)
    option_rows = [row for row in rows if row.option_type in {"CE", "PE"}]
    future_rows = [row for row in rows if row.option_type not in {"CE", "PE"}]
    ids = [row.contract_id for row in rows]
    duplicate_ids = sorted({contract_id for contract_id in ids if ids.count(contract_id) > 1})
    missing_lot = [row.contract_id for row in rows if not row.lot_size]
    option_missing_strike = [row.contract_id for row in option_rows if row.strike_price is None]
    future_with_strike = [row.contract_id for row in future_rows if row.strike_price is not None]
    expiries = sorted({row.expiry_date for row in rows if row.expiry_date})
    underlyings = sorted({row.underlying for row in rows if row.underlying})
    return {
        "total_contracts": len(rows),
        "underlyings": len(underlyings),
        "expiries": len(expiries),
        "option_contracts": len(option_rows),
        "future_contracts": len(future_rows),
        "ce_contracts": sum(1 for row in option_rows if row.option_type == "CE"),
        "pe_contracts": sum(1 for row in option_rows if row.option_type == "PE"),
        "duplicate_contract_ids": duplicate_ids[:20],
        "missing_lot_size_count": len(missing_lot),
        "option_missing_strike_count": len(option_missing_strike),
        "future_with_strike_count": len(future_with_strike),
        "quality_status": "ok" if not duplicate_ids and not missing_lot and not option_missing_strike and not future_with_strike else "attention",
    }


async def _load_nse_contracts(limit: int = 2000) -> tuple[list[CanonicalContract], dict[str, Any]]:
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    SELECT market, instrument_key, trading_symbol, underlying, expiry, strike,
                           option_type, lot_size, tick_size, freeze_quantity, sync_status,
                           updated_at
                    FROM fo_contract_catalog
                    WHERE COALESCE(market, 'NSE') IN ('NSE', 'BSE')
                      AND expiry >= CURRENT_DATE
                    ORDER BY market, underlying, expiry, strike, option_type
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            )
            rows = [dict(row) for row in result.mappings().all()]
    except Exception as exc:
        logger.warning(f"[FNOAnalytics] NSE contract catalog unavailable: {exc}")
        return [], {"status": "unavailable", "detail": str(exc)}

    contracts: list[CanonicalContract] = []
    for row in rows:
        exchange = str(row.get("market") or "NSE").upper()
        underlying = str(row.get("underlying") or "").upper()
        option_type = str(row.get("option_type") or "").upper() or None
        strike = _safe_float(row.get("strike"))
        expiry = _iso_date(row.get("expiry"))
        instrument_type = _nse_instrument_type(underlying, option_type)
        contracts.append(
            CanonicalContract(
                contract_id=_contract_id(exchange, "FO", instrument_type, underlying, expiry, strike, option_type),
                exchange=exchange,
                segment="FO",
                instrument_type=instrument_type,
                underlying=underlying,
                expiry_date=expiry,
                strike_price=strike,
                option_type=option_type,
                lot_size=_safe_int(row.get("lot_size")),
                tick_size=_safe_float(row.get("tick_size")),
                exercise_style="European" if option_type in {"CE", "PE"} else None,
                settlement_type="physical" if instrument_type == "OPTSTK" else "cash_or_index_settlement",
                is_physical_settlement=instrument_type == "OPTSTK",
                is_devolvement_applicable=False,
                status="active",
                source="fo_contract_catalog",
                trading_symbol=row.get("trading_symbol"),
                instrument_key=row.get("instrument_key"),
                freeze_quantity=_safe_int(row.get("freeze_quantity")),
                sync_status=row.get("sync_status"),
            )
        )

    return contracts, {"status": "ready" if contracts else "missing", "source": "fo_contract_catalog"}


def _commodity_contract_from_catalog(item: dict[str, Any]) -> list[CanonicalContract]:
    symbol = str(item.get("symbol") or "").upper()
    root = str(item.get("underlying") or "").upper()
    if not root and symbol:
        root = symbol.split(":")[-1]
    spec = get_commodity_contract_spec(symbol or root)
    active_expiry = _iso_date(item.get("active_expiry") or item.get("selected_expiry") or item.get("suggested_expiry"))
    lot_size = _safe_int(item.get("lot_size")) or spec.futures_lot_size
    return [
        CanonicalContract(
            contract_id=_contract_id("MCX", "COM", _mcx_instrument_type(root, None), root, active_expiry),
            exchange="MCX",
            segment="COM",
            instrument_type=_mcx_instrument_type(root, None),
            underlying=root,
            expiry_date=active_expiry,
            lot_size=lot_size,
            tick_size=spec.mp_tick_size,
            price_unit=spec.quote_unit_label,
            settlement_type="delivery_or_cash_by_contract",
            is_physical_settlement=True,
            is_devolvement_applicable=False,
            status="active" if item.get("has_options") else "watch",
            source="commodity_contract_catalog",
            trading_symbol=symbol,
        )
    ]


def _commodity_option_contract_from_watch_row(row: dict[str, Any], option_type: str) -> CanonicalContract | None:
    side_key = option_type.lower()
    leg = row.get(side_key) or {}
    root = str(row.get("underlying") or row.get("symbol") or "").upper()
    expiry = _iso_date(row.get("active_expiry") or row.get("expiry"))
    strike = _safe_float(
        leg.get("strike")
        or row.get(f"{side_key}_strike")
        or row.get("trade_strike")
        or row.get("atm_strike")
    )
    if not root or not expiry or strike is None:
        return None
    spec = get_commodity_contract_spec(root)
    instrument_type = _mcx_instrument_type(root, option_type)
    return CanonicalContract(
        contract_id=_contract_id("MCX", "COM", instrument_type, root, expiry, strike, option_type),
        exchange="MCX",
        segment="COM",
        instrument_type=instrument_type,
        underlying=root,
        expiry_date=expiry,
        strike_price=strike,
        option_type=option_type,
        lot_size=_safe_int(row.get("lot_size")) or spec.futures_lot_size,
        tick_size=spec.mp_tick_size,
        price_unit=spec.quote_unit_label,
        exercise_style="European",
        settlement_type="devolves_to_underlying_future",
        is_physical_settlement=True,
        is_devolvement_applicable=True,
        status="active" if leg.get("ltp") is not None else "watch",
        source="commodity_atm_watchlist",
        trading_symbol=str(leg.get("trading_symbol") or row.get("trade_symbol") or "").strip() or None,
        instrument_key=str(leg.get("instrument_key") or "").strip() or None,
    )


async def _load_mcx_snapshot(timeout_seconds: float = 8.0) -> tuple[dict[str, Any], dict[str, Any]]:
    symbols = commodity_strategy_agent.get_symbols()
    selected_expiries = commodity_strategy_agent.get_selected_option_expiries()
    selected_lookup_symbols = commodity_strategy_agent.get_selected_option_lookup_symbols()
    contract_catalog = commodity_atm_watchlist_service.get_cached_contract_catalog(
        symbols,
        selected_expiries,
        selected_lookup_symbols,
    )
    atm_watchlist = commodity_atm_watchlist_service.get_cached_watchlist(
        symbols,
        selected_expiries,
        selected_lookup_symbols,
    )
    if contract_catalog is None or atm_watchlist is None:
        saved_catalog, saved_watchlist = _load_mcx_saved_state_snapshot()
        if contract_catalog is None and saved_catalog.get("contracts"):
            contract_catalog = saved_catalog
        if atm_watchlist is None and saved_watchlist.get("rows"):
            atm_watchlist = saved_watchlist
    if contract_catalog is None:
        try:
            contract_catalog = await asyncio.wait_for(
                commodity_atm_watchlist_service.get_contract_catalog(
                    symbols,
                    selected_expiries,
                    selected_lookup_symbols,
                ),
                timeout=timeout_seconds,
            )
        except Exception as exc:
            logger.warning(f"[FNOAnalytics] MCX contract catalog unavailable: {exc}")
            contract_catalog = None
    if atm_watchlist is None:
        try:
            atm_watchlist = await asyncio.wait_for(
                commodity_atm_watchlist_service.get_watchlist(
                    symbols,
                    selected_expiries,
                    selected_lookup_symbols,
                ),
                timeout=timeout_seconds,
            )
        except Exception as exc:
            logger.warning(f"[FNOAnalytics] MCX ATM watchlist unavailable: {exc}")
            atm_watchlist = None
    if contract_catalog is None or atm_watchlist is None:
        saved_catalog, saved_watchlist = _load_mcx_saved_state_snapshot()
        contract_catalog = contract_catalog or saved_catalog
        atm_watchlist = atm_watchlist or saved_watchlist
    return contract_catalog, atm_watchlist


def _load_mcx_saved_state_snapshot() -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        payload = json.loads(COMMODITY_STATE_PATH.read_text())
    except Exception as exc:
        return (
            {
                "contracts": [],
                "summary": {"total_symbols": 0, "contracts_ready": 0, "active_selections": 0},
                "source": "unavailable",
                "detail": f"Commodity saved state is unavailable: {exc}",
                "timestamp": _utc_now().isoformat(),
            },
            {
                "expiry": None,
                "rows": [],
                "summary": {"total_rows": 0, "ce_ready": 0, "pe_ready": 0},
                "source": "unavailable",
                "detail": f"Commodity saved state is unavailable: {exc}",
                "timestamp": _utc_now().isoformat(),
            },
        )

    config = payload.get("config") or {}
    runtime = payload.get("runtime") or {}
    option_rows = list(runtime.get("option_watchlist") or [])
    futures_rows = list(runtime.get("futures_watchlist") or runtime.get("watchlist") or [])
    selected_expiries = dict(config.get("selected_option_expiries") or {})
    selected_lookup_symbols = dict(config.get("selected_option_lookup_symbols") or {})
    symbols = list(config.get("symbols") or [])
    option_by_symbol = {str(row.get("symbol") or "").upper(): row for row in option_rows}
    contracts = []
    for symbol in symbols:
        symbol_key = str(symbol or "").upper()
        option_row = option_by_symbol.get(symbol_key, {})
        spec = get_commodity_contract_spec(symbol_key)
        contracts.append(
            {
                "symbol": symbol_key,
                "underlying": option_row.get("underlying") or spec.root,
                "lookup_symbol": option_row.get("lookup_symbol") or selected_lookup_symbols.get(symbol_key) or symbol_key,
                "active_lookup_symbol": option_row.get("lookup_symbol") or selected_lookup_symbols.get(symbol_key) or symbol_key,
                "selected_expiry": selected_expiries.get(symbol_key) or option_row.get("expiry"),
                "active_expiry": option_row.get("active_expiry") or option_row.get("expiry") or selected_expiries.get(symbol_key),
                "has_options": bool(option_row.get("ce") or option_row.get("pe")),
                "lot_size": option_row.get("lot_size") or spec.futures_lot_size,
                "contract_unit_label": option_row.get("contract_unit_label") or spec.contract_unit_label,
                "quote_unit_label": option_row.get("quote_unit_label") or spec.quote_unit_label,
                "source": "commodity_strategy_state",
            }
        )
    expiry = next((row.get("active_expiry") or row.get("expiry") for row in option_rows if row.get("active_expiry") or row.get("expiry")), None)
    timestamp = _utc_now().isoformat()
    detail = "Using saved commodity strategy runtime because live MCX catalog/watchlist refresh is unavailable."
    return (
        {
            "contracts": contracts,
            "summary": {
                "total_symbols": len(symbols),
                "contracts_ready": sum(1 for item in contracts if item.get("has_options")),
                "active_selections": sum(1 for item in contracts if item.get("active_expiry")),
            },
            "source": "commodity_strategy_state",
            "detail": detail,
            "timestamp": timestamp,
            "futures_rows": futures_rows,
        },
        {
            "expiry": expiry,
            "rows": option_rows,
            "summary": {
                "total_rows": len(option_rows),
                "ce_ready": sum(1 for row in option_rows if (row.get("ce") or {}).get("ltp") is not None),
                "pe_ready": sum(1 for row in option_rows if (row.get("pe") or {}).get("ltp") is not None),
            },
            "source": "commodity_strategy_state",
            "detail": detail,
            "timestamp": timestamp,
        },
    )


def _analyze_mcx(contract_catalog: dict[str, Any], atm_watchlist: dict[str, Any]) -> dict[str, Any]:
    contracts: list[CanonicalContract] = []
    for item in list(contract_catalog.get("contracts") or []):
        contracts.extend(_commodity_contract_from_catalog(item))

    rows = list(atm_watchlist.get("rows") or [])
    for row in rows:
        for option_type in ("CE", "PE"):
            option_contract = _commodity_option_contract_from_watch_row(row, option_type)
            if option_contract is not None:
                contracts.append(option_contract)

    ce_ready = sum(1 for row in rows if (row.get("ce") or {}).get("ltp") is not None)
    pe_ready = sum(1 for row in rows if (row.get("pe") or {}).get("ltp") is not None)
    devolvement_watch: list[dict[str, Any]] = []
    spread_watch: list[dict[str, Any]] = []
    for row in rows:
        expiry = row.get("active_expiry") or row.get("expiry") or atm_watchlist.get("expiry")
        tte = _days_to_expiry(expiry)
        for side in ("ce", "pe"):
            leg = row.get(side) or {}
            bid = _safe_float(leg.get("bid"))
            ask = _safe_float(leg.get("ask"))
            ltp = _safe_float(leg.get("ltp"))
            spread_pct = ((ask - bid) / ltp * 100.0) if bid is not None and ask is not None and ltp and ask >= bid else None
            if spread_pct is not None:
                spread_watch.append(
                    {
                        "underlying": row.get("underlying") or row.get("symbol"),
                        "option_type": side.upper(),
                        "expiry": expiry,
                        "strike": row.get(f"{side}_strike") or row.get("trade_strike") or row.get("atm_strike"),
                        "spread_pct": _round(spread_pct, 2),
                        "ltp": _round(ltp, 2),
                    }
                )
        if tte is not None and tte <= NEAR_EXPIRY_WARNING_DAYS:
            devolvement_watch.append(
                {
                    "underlying": row.get("underlying") or row.get("symbol"),
                    "expiry": expiry,
                    "days_to_expiry": tte,
                    "signal_side": row.get("signal_side"),
                    "trade_symbol": row.get("trade_symbol"),
                    "risk": "near_expiry_devolvement_review",
                }
            )

    return {
        "status": "ready" if contracts or rows else "missing",
        "source": {
            "contract_catalog": contract_catalog.get("source"),
            "atm_watchlist": atm_watchlist.get("source"),
            "detail": contract_catalog.get("detail") or atm_watchlist.get("detail"),
        },
        "contract_master": {
            "summary": _contract_summary(contracts),
            "sample": [asdict(row) for row in contracts[:30]],
        },
        "option_chain": {
            "expiry": atm_watchlist.get("expiry"),
            "rows": len(rows),
            "ce_ready": ce_ready,
            "pe_ready": pe_ready,
            "pcr_ready": _ratio(pe_ready, ce_ready),
            "timestamp": atm_watchlist.get("timestamp"),
        },
        "risk": {
            "devolvement_watch": devolvement_watch[:20],
            "spread_watch": sorted(spread_watch, key=lambda row: row.get("spread_pct") or -1, reverse=True)[:20],
            "notes": [
                "MCX ITM commodity options can devolve into underlying futures.",
                "Tender, delivery and post-devolvement margin must be visible before expiry.",
            ],
        },
    }


def _build_quality_checks(nse: dict[str, Any], mcx: dict[str, Any], fno_360: dict[str, Any] | None) -> list[dict[str, Any]]:
    nse_summary = (nse.get("contract_master") or {}).get("summary") or {}
    mcx_summary = (mcx.get("contract_master") or {}).get("summary") or {}
    fno_status = (fno_360 or {}).get("status") or "unknown"
    return [
        {
            "key": "nse_contract_master",
            "label": "NSE contract master",
            "status": "ok" if nse_summary.get("total_contracts", 0) > 0 and nse_summary.get("quality_status") == "ok" else "attention",
            "detail": f"{nse_summary.get('total_contracts', 0)} active contracts, {nse_summary.get('underlyings', 0)} underlyings",
        },
        {
            "key": "mcx_contract_master",
            "label": "MCX contract master",
            "status": "ok" if mcx_summary.get("total_contracts", 0) > 0 and mcx_summary.get("quality_status") == "ok" else "attention",
            "detail": f"{mcx_summary.get('total_contracts', 0)} normalized contracts, devolvement flags enabled",
        },
        {
            "key": "nse_snapshot_freshness",
            "label": "NSE option snapshot freshness",
            "status": "ok" if fno_status == "ready" else "attention",
            "detail": f"fno_360={fno_status}; latest={(fno_360 or {}).get('latest_time') or '--'}",
        },
        {
            "key": "mcx_devolvement_mapping",
            "label": "MCX option devolvement mapping",
            "status": "ok" if mcx_summary.get("option_contracts", 0) > 0 else "attention",
            "detail": f"{mcx_summary.get('option_contracts', 0)} option contracts mapped to futures-risk rules",
        },
    ]


def _stage_status(nse: dict[str, Any], mcx: dict[str, Any], fno_360: dict[str, Any] | None) -> list[dict[str, Any]]:
    nse_contracts = ((nse.get("contract_master") or {}).get("summary") or {}).get("total_contracts", 0)
    mcx_contracts = ((mcx.get("contract_master") or {}).get("summary") or {}).get("total_contracts", 0)
    nse_snapshot_ready = (fno_360 or {}).get("status") == "ready"
    mcx_rows = ((mcx.get("option_chain") or {}).get("rows") or 0) > 0
    return [
        {"stage": 1, "name": "Contract Master", "status": "ready" if nse_contracts or mcx_contracts else "missing"},
        {"stage": 2, "name": "EOD Pipeline", "status": "partial", "detail": "Existing research cache tables are present; MCX bhavcopy EOD ingestion is not yet a first-class table."},
        {"stage": 3, "name": "Option Chain", "status": "ready" if nse_snapshot_ready or mcx_rows else "missing"},
        {"stage": 4, "name": "Greeks & Vol", "status": "partial", "detail": "NSE option-chain IV/Greeks are surfaced where broker snapshots provide them; IV rank/percentile pipeline still needs history."},
        {"stage": 5, "name": "Futures Curve", "status": "partial", "detail": "Contract master supports expiry ordering; near/mid/far curve analytics need futures EOD/live rows."},
        {"stage": 6, "name": "Risk & Margin", "status": "partial", "detail": "Physical settlement and devolvement flags are live; margin file ingestion remains pending."},
        {"stage": 7, "name": "Live Alerts", "status": "partial", "detail": "Data freshness checks exist; alert review and notification rules need promotion."},
        {"stage": 8, "name": "Strategy Lab", "status": "existing", "detail": "Backtest/replay modules exist but must consume the normalized contract/risk model."},
        {"stage": 9, "name": "Research Assistant", "status": "partial", "detail": "Research tab is now data-backed; source-cited natural language layer is pending."},
    ]


async def build_fno_analytics(*, fno_360: dict[str, Any] | None = None, limit: int = 20) -> dict[str, Any]:
    # Load the active contract master broadly; the UI limit only applies to
    # watchlists/signal rows, not to data-quality and universe counts.
    nse_contracts, nse_source = await _load_nse_contracts(limit=max(limit * 100, 10_000))
    contract_catalog, atm_watchlist = await _load_mcx_snapshot()
    mcx = _analyze_mcx(contract_catalog, atm_watchlist)
    nse = {
        "status": "ready" if nse_contracts or (fno_360 or {}).get("status") == "ready" else "missing",
        "source": nse_source,
        "contract_master": {
            "summary": _contract_summary(nse_contracts),
            "sample": [asdict(row) for row in nse_contracts[:30]],
        },
        "option_chain": {
            "summary": (fno_360 or {}).get("market", {}),
            "breadth": (fno_360 or {}).get("breadth", {}),
            "buildup_counts": (fno_360 or {}).get("buildup_counts", {}),
            "top_volume": (fno_360 or {}).get("top_volume", [])[:limit],
            "top_oi": (fno_360 or {}).get("top_oi", [])[:limit],
            "analytics": (fno_360 or {}).get("analytics", {}),
            "status": (fno_360 or {}).get("status", "unknown"),
            "latest_time": (fno_360 or {}).get("latest_time"),
        },
        "risk": {
            "stock_physical_settlement_contracts": sum(1 for row in nse_contracts if row.instrument_type == "OPTSTK"),
            "freeze_quantity_missing": sum(1 for row in nse_contracts if row.freeze_quantity is None),
            "notes": [
                "Stock F&O options are flagged as physical-settlement risk.",
                "MWPL/ban, SPAN and deep-OTM short-option margin need exchange margin/circular feeds.",
            ],
        },
    }
    quality_checks = _build_quality_checks(nse, mcx, fno_360)
    return {
        "status": "ready" if any(check["status"] == "ok" for check in quality_checks) else "attention",
        "as_of": _utc_now().isoformat(),
        "module": "fno_contract_analytics",
        "scope": ["NSE_FO", "BSE_FO", "MCX_COM"],
        "nse": nse,
        "mcx": mcx,
        "quality_checks": quality_checks,
        "stage_status": _stage_status(nse, mcx, fno_360),
        "signals": {
            "nse": {
                "top_volume": (fno_360 or {}).get("top_volume", [])[:limit],
                "oi_change_contracts": ((fno_360 or {}).get("analytics") or {}).get("oi_change_contracts", [])[:limit],
                "volatility_watch": ((fno_360 or {}).get("analytics") or {}).get("volatility_watch", [])[:limit],
            },
            "mcx": {
                "devolvement_watch": (mcx.get("risk") or {}).get("devolvement_watch", [])[:limit],
                "spread_watch": (mcx.get("risk") or {}).get("spread_watch", [])[:limit],
            },
        },
    }
