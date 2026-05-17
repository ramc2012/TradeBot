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

from analysis.option_greeks import GreeksMode, compute_greeks
from analysis.signal_library import classify_oi_price, classify_strike_positioning, max_pain
from db.database import AsyncSessionLocal
from market_data.commodity_atm_watchlist import commodity_atm_watchlist_service
from market_data.commodity_contract_specs import get_commodity_contract_spec
from market_data.futures_curve import build_curve
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


async def _load_chain_max_pain(limit_underlyings: int = 30) -> list[dict[str, Any]]:
    """Compute max-pain + chain PCR-OI from option_premium_candles.

    Picks the latest CE+PE OI per (underlying, expiry, strike) for active
    expiries, then runs the canonical max-pain formula (writer payout
    minimised at strike K). Returns one row per (underlying, expiry)
    with: max_pain_strike, chain pcr_oi, total_call_oi, total_put_oi,
    strikes_count.

    Uses DISTINCT ON (strike, option_type) to dedupe the broker
    duplicates that exist for some symbols.
    """
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    WITH ranked AS (
                        SELECT DISTINCT ON (underlying, expiry, strike, option_type)
                            underlying, expiry, strike, option_type,
                            oi, time
                        FROM option_premium_candles
                        WHERE expiry >= CURRENT_DATE
                          AND interval = '30minute'
                          AND oi IS NOT NULL
                          AND option_type IN ('CE', 'PE')
                        ORDER BY underlying, expiry, strike, option_type, time DESC
                    )
                    SELECT underlying, expiry, strike, option_type, oi
                    FROM ranked
                    """
                )
            )
            rows = list(result.mappings().all())
    except Exception as exc:
        logger.warning(f"[FNOAnalytics] chain max-pain load failed: {exc}")
        return []

    # Group by (underlying, expiry) → strike → {ce_oi, pe_oi}
    grouped: dict[tuple[str, str], dict[float, dict[str, float]]] = {}
    for row in rows:
        underlying = str(row.get("underlying") or "").upper()
        expiry = _iso_date(row.get("expiry"))
        strike = _safe_float(row.get("strike"))
        side = str(row.get("option_type") or "").upper()
        oi = _safe_float(row.get("oi"))
        if not underlying or not expiry or strike is None or oi is None:
            continue
        bucket = grouped.setdefault((underlying, expiry), {})
        node = bucket.setdefault(strike, {"call_oi": 0.0, "put_oi": 0.0})
        if side == "CE":
            node["call_oi"] = float(oi)
        elif side == "PE":
            node["put_oi"] = float(oi)

    from analysis.signal_library import max_pain

    out: list[dict[str, Any]] = []
    for (underlying, expiry), strike_map in grouped.items():
        chain = [
            {"strike": strike, "call_oi": data["call_oi"], "put_oi": data["put_oi"]}
            for strike, data in sorted(strike_map.items())
        ]
        if len(chain) < 2:
            # Need at least two strikes for the writer-payout curve to be meaningful.
            continue
        mp = max_pain(chain)
        total_call_oi = sum(c["call_oi"] for c in chain)
        total_put_oi = sum(c["put_oi"] for c in chain)
        # Find the strike with the biggest CE OI build-up (resistance band)
        max_call_strike = max(chain, key=lambda c: c["call_oi"]) if chain else None
        # Biggest PE OI (support band)
        max_put_strike = max(chain, key=lambda c: c["put_oi"]) if chain else None
        out.append(
            {
                "underlying": underlying,
                "expiry": expiry,
                "strikes_count": len(chain),
                "max_pain_strike": _round(mp, 2),
                "max_call_oi_strike": _round((max_call_strike or {}).get("strike"), 2),
                "max_put_oi_strike": _round((max_put_strike or {}).get("strike"), 2),
                "total_call_oi": _round(total_call_oi, 0),
                "total_put_oi": _round(total_put_oi, 0),
                "chain_pcr_oi": _ratio(total_put_oi, total_call_oi),
            }
        )
    out.sort(key=lambda r: (r.get("underlying") or "", r.get("expiry") or ""))
    return out[:limit_underlyings]


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


def _greeks_for_leg(
    *,
    option_type: str,
    spot: float | None,
    strike: float | None,
    expiry: str | None,
    ltp: float | None,
) -> dict[str, Any] | None:
    """Return a compact Greeks payload for one option leg.

    Uses ``GreeksMode.EXCHANGE`` (10% risk-free rate) so values are
    comparable with NSE/MCX exchange-published numbers. Returns None when
    inputs are insufficient (no spot/strike/expiry/premium).
    """
    if spot is None or strike is None or expiry is None or ltp is None or ltp <= 0:
        return None
    tte = _days_to_expiry(expiry)
    if tte is None or tte < 0:
        return None
    tte_years = max(tte, 1) / 365.0
    try:
        result = compute_greeks(
            option_type=option_type,
            spot=float(spot),
            strike=float(strike),
            tte_years=tte_years,
            market_premium=float(ltp),
            mode=GreeksMode.EXCHANGE,
        )
    except Exception as exc:
        logger.debug(f"[FNOAnalytics] Greeks failed ({option_type} {strike} @ {ltp}): {exc}")
        return None
    return {
        "iv": _round(result.iv, 4),
        "delta": _round(result.delta, 4),
        "gamma": _round(result.gamma, 6),
        "theta": _round(result.theta, 4),
        "vega": _round(result.vega, 4),
        "intrinsic_value": _round(result.intrinsic_value, 2),
        "time_value": _round(result.time_value, 2),
        "probability_itm": _round(result.probability_itm, 4),
        "break_even": _round(result.break_even, 2),
        "days_to_expiry": result.days_to_expiry,
    }


def _build_mcx_curves(contract_catalog: dict[str, Any]) -> list[dict[str, Any]]:
    """Group MCX futures by underlying and run the curve analytics on each."""
    by_root: dict[str, list[dict[str, Any]]] = {}
    futures_rows = list(contract_catalog.get("futures_rows") or [])
    for row in futures_rows:
        if not isinstance(row, dict):
            continue
        root = str(row.get("underlying") or "").upper()
        symbol = str(row.get("symbol") or "").upper()
        expiry = row.get("active_expiry") or row.get("expiry")
        price = row.get("price") or row.get("ltp") or row.get("close")
        if not root or expiry is None or price is None:
            continue
        by_root.setdefault(root, []).append(
            {
                "contract_id": symbol or f"{root}:{expiry}",
                "expiry": expiry,
                "price": price,
                "open_interest": row.get("open_interest") or row.get("oi"),
                "volume": row.get("volume"),
            }
        )

    curves: list[dict[str, Any]] = []
    for root, contracts in by_root.items():
        if not contracts:
            continue
        spot = None
        for c in contracts:
            if c.get("spot_price"):
                spot = c.get("spot_price")
                break
        analysis = build_curve(underlying=root, contracts=contracts, spot_price=spot)
        curves.append(analysis.as_dict())
    return curves


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
    bid_ask_watch: list[dict[str, Any]] = []
    greeks_rows: list[dict[str, Any]] = []
    strike_positioning: list[dict[str, Any]] = []
    # Per-underlying summary: ATM strike, ATM straddle premium, implied
    # expected move (1-σ proxy ≈ straddle premium), PCR-OI etc. These are
    # the core trader-facing read-outs that the option-chain card in the
    # design doc surfaces alongside Greeks.
    straddle_summary: list[dict[str, Any]] = []
    for row in rows:
        expiry = row.get("active_expiry") or row.get("expiry") or atm_watchlist.get("expiry")
        tte = _days_to_expiry(expiry)
        underlying = str(row.get("underlying") or row.get("symbol") or "").upper()
        spot = _safe_float(row.get("spot_price") or row.get("underlying_price"))
        ce_leg = row.get("ce") or {}
        pe_leg = row.get("pe") or {}
        ce_strike = _safe_float(ce_leg.get("strike") or row.get("ce_strike") or row.get("trade_strike") or row.get("atm_strike"))
        pe_strike = _safe_float(pe_leg.get("strike") or row.get("pe_strike") or row.get("trade_strike") or row.get("atm_strike"))
        for side, strike in (("ce", ce_strike), ("pe", pe_strike)):
            leg = row.get(side) or {}
            bid = _safe_float(leg.get("bid"))
            ask = _safe_float(leg.get("ask"))
            ltp = _safe_float(leg.get("ltp"))
            spread_pct = ((ask - bid) / ltp * 100.0) if bid is not None and ask is not None and ltp and ask >= bid else None
            if spread_pct is not None:
                bid_ask_watch.append(
                    {
                        "underlying": underlying or row.get("symbol"),
                        "option_type": side.upper(),
                        "expiry": expiry,
                        "strike": strike,
                        "bid_ask_spread_pct": _round(spread_pct, 2),
                        "ltp": _round(ltp, 2),
                    }
                )
            greeks_payload = _greeks_for_leg(
                option_type=side.upper(),
                spot=spot,
                strike=strike,
                expiry=expiry,
                ltp=ltp,
            )
            if greeks_payload is not None:
                greeks_rows.append(
                    {
                        "underlying": underlying,
                        "option_type": side.upper(),
                        "expiry": expiry,
                        "strike": strike,
                        "ltp": _round(ltp, 2),
                        **greeks_payload,
                    }
                )
        if (
            ce_strike is not None
            and pe_strike is not None
            and abs((ce_strike or 0) - (pe_strike or 0)) < 1e-6
        ):
            positioning = classify_strike_positioning(
                underlying=underlying,
                expiry=str(expiry or ""),
                strike=float(ce_strike),
                spot=spot,
                call_oi=_safe_float(ce_leg.get("oi")),
                put_oi=_safe_float(pe_leg.get("oi")),
                call_oi_change=_safe_float(ce_leg.get("oi_change")),
                put_oi_change=_safe_float(pe_leg.get("oi_change")),
                call_price_change_pct=_safe_float(ce_leg.get("change_pct")),
                put_price_change_pct=_safe_float(pe_leg.get("change_pct")),
            )
            if positioning.bias != "neutral":
                strike_positioning.append(
                    {
                        "underlying": positioning.underlying,
                        "expiry": positioning.expiry,
                        "strike": positioning.strike,
                        "bias": positioning.bias,
                        "note": positioning.note,
                    }
                )
        # ATM straddle summary — the CE-side strike is treated as the ATM
        # since the commodity_atm_watchlist always centres on the nearest
        # strike to spot. straddle_pct = (CE+PE)/spot * 100, an approximate
        # 1-σ move expectation by expiry priced by the market.
        ce_ltp = _safe_float((row.get("ce") or {}).get("ltp"))
        pe_ltp = _safe_float((row.get("pe") or {}).get("ltp"))
        ce_oi = _safe_float((row.get("ce") or {}).get("oi"))
        pe_oi = _safe_float((row.get("pe") or {}).get("oi"))
        straddle = (ce_ltp + pe_ltp) if (ce_ltp is not None and pe_ltp is not None) else None
        straddle_pct = (straddle / spot * 100.0) if (straddle is not None and spot) else None
        straddle_summary.append(
            {
                "underlying": underlying,
                "expiry": expiry,
                "days_to_expiry": tte,
                "spot_price": _round(spot, 2),
                "atm_strike": _round(ce_strike or pe_strike, 2),
                "ce_ltp": _round(ce_ltp, 2),
                "pe_ltp": _round(pe_ltp, 2),
                "atm_straddle": _round(straddle, 2),
                "expected_move": _round(straddle, 2),
                "expected_move_pct": _round(straddle_pct, 3),
                "ce_oi": _round(ce_oi, 0),
                "pe_oi": _round(pe_oi, 0),
                "pcr_oi": _ratio(pe_oi, ce_oi),
            }
        )
        if tte is not None and tte <= NEAR_EXPIRY_WARNING_DAYS:
            devolvement_watch.append(
                {
                    "underlying": underlying or row.get("symbol"),
                    "expiry": expiry,
                    "days_to_expiry": tte,
                    "signal_side": row.get("signal_side"),
                    "trade_symbol": row.get("trade_symbol"),
                    "risk": "near_expiry_devolvement_review",
                }
            )

    futures_curves = _build_mcx_curves(contract_catalog)
    # Calendar-spread roll-up across all curves (top by abs annualized basis)
    calendar_spreads: list[dict[str, Any]] = []
    for curve in futures_curves:
        for spread in curve.get("calendar_spreads") or []:
            calendar_spreads.append(
                {
                    "underlying": curve.get("underlying"),
                    **{k: spread.get(k) for k in (
                        "near_contract_id",
                        "far_contract_id",
                        "near_expiry",
                        "far_expiry",
                        "near_price",
                        "far_price",
                        "spread",
                        "spread_pct",
                        "annualized_basis_pct",
                    )},
                }
            )
    calendar_spreads.sort(
        key=lambda item: abs(item.get("annualized_basis_pct") or 0.0),
        reverse=True,
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
            "straddle_summary": straddle_summary,
        },
        "straddle_summary": straddle_summary,
        "greeks": {
            "rows": greeks_rows,
            "mode": "exchange_10pct",
            "count": len(greeks_rows),
        },
        "futures_curve": {
            "curves": futures_curves,
            "calendar_spreads": calendar_spreads[:20],
            "count": len(futures_curves),
        },
        "positioning": {
            "strikes": strike_positioning,
        },
        "risk": {
            "devolvement_watch": devolvement_watch[:20],
            "spread_watch": calendar_spreads[:20],
            "bid_ask_watch": sorted(
                bid_ask_watch,
                key=lambda item: item.get("bid_ask_spread_pct") or -1,
                reverse=True,
            )[:20],
            "notes": [
                "MCX ITM commodity options can devolve into underlying futures.",
                "Tender, delivery and post-devolvement margin must be visible before expiry.",
                "spread_watch now lists futures calendar spreads (was bid-ask) — see bid_ask_watch for option microstructure.",
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
    nse_greeks_count = ((nse.get("greeks") or {}).get("count") or 0)
    mcx_greeks_count = ((mcx.get("greeks") or {}).get("count") or 0)
    greeks_ready = nse_greeks_count > 0 or mcx_greeks_count > 0
    mcx_curves = ((mcx.get("futures_curve") or {}).get("count") or 0)
    nse_signals = ((nse.get("oi_price_signals") or {}).get("count") or 0)
    return [
        {"stage": 1, "name": "Contract Master", "status": "ready" if nse_contracts or mcx_contracts else "missing"},
        {"stage": 2, "name": "EOD Pipeline", "status": "partial", "detail": "Existing research cache tables are present; MCX bhavcopy EOD ingestion is not yet a first-class table."},
        {"stage": 3, "name": "Option Chain", "status": "ready" if nse_snapshot_ready or mcx_rows else "missing"},
        {
            "stage": 4,
            "name": "Greeks & Vol",
            "status": "ready" if greeks_ready else "partial",
            "detail": (
                f"Black-Scholes engine live (mode=exchange_10pct); "
                f"nse_greeks={nse_greeks_count} mcx_greeks={mcx_greeks_count}; "
                "IV rank/percentile pipeline still needs full history."
            ),
        },
        {
            "stage": 5,
            "name": "Futures Curve",
            "status": "ready" if mcx_curves > 0 else "partial",
            "detail": (
                f"Curve module live (contango/backwardation/calendar spread/rollover); "
                f"mcx_curves={mcx_curves}; "
                "NSE futures need a live price source to surface curves."
            ),
        },
        {"stage": 6, "name": "Risk & Margin", "status": "partial", "detail": "Physical settlement and devolvement flags are live; margin file ingestion remains pending."},
        {
            "stage": 7,
            "name": "Live Alerts",
            "status": "partial",
            "detail": (
                "Data freshness checks exist; OI-price participant signals "
                f"({nse_signals} classified) feed the dashboard; notification rules pending."
            ),
        },
        {"stage": 8, "name": "Strategy Lab", "status": "existing", "detail": "Backtest/replay modules exist but must consume the normalized contract/risk model."},
        {"stage": 9, "name": "Research Assistant", "status": "partial", "detail": "Research tab is now data-backed; source-cited natural language layer is pending."},
    ]


def _research_modules(
    nse: dict[str, Any],
    mcx: dict[str, Any],
    fno_360: dict[str, Any] | None,
    fo_risk: dict[str, Any] | None,
) -> dict[str, Any]:
    nse_summary = ((nse.get("contract_master") or {}).get("summary") or {})
    mcx_summary = ((mcx.get("contract_master") or {}).get("summary") or {})
    nse_chain = nse.get("option_chain") or {}
    mcx_chain = mcx.get("option_chain") or {}
    nse_signals = nse.get("oi_price_signals") or {}
    mcx_risk = mcx.get("risk") or {}
    mcx_curve = mcx.get("futures_curve") or {}
    mwpl = (fo_risk or {}).get("mwpl") or {}
    ban = (fo_risk or {}).get("ban_list") or {}
    latest_time = (fno_360 or {}).get("latest_time")

    modules = [
        {
            "key": "contract_master",
            "label": "Contract Master",
            "status": "ready" if nse_summary.get("total_contracts") or mcx_summary.get("total_contracts") else "missing",
            "metric": f"{nse_summary.get('total_contracts', 0)} NSE · {mcx_summary.get('total_contracts', 0)} MCX",
            "detail": (
                f"{nse_summary.get('underlyings', 0)} NSE underlyings, "
                f"{mcx_summary.get('underlyings', 0)} MCX roots, "
                f"{mcx_summary.get('option_contracts', 0)} MCX options with devolvement mapping."
            ),
        },
        {
            "key": "option_intelligence",
            "label": "Option Intelligence",
            "status": "ready" if (fno_360 or {}).get("status") == "ready" or mcx_chain.get("rows") else "attention",
            "metric": f"{nse_chain.get('summary', {}).get('total_underlyings', 0)} NSE · {mcx_chain.get('rows', 0)} MCX",
            "detail": (
                f"PCR {nse_chain.get('summary', {}).get('pcr_oi', '--')}, "
                f"avg IV {nse_chain.get('summary', {}).get('average_iv', '--')}, "
                f"latest snapshot {latest_time or '--'}."
            ),
        },
        {
            "key": "greeks_vol",
            "label": "Greeks & Vol",
            "status": "ready" if ((nse.get("greeks") or {}).get("count") or 0) or ((mcx.get("greeks") or {}).get("count") or 0) else "attention",
            "metric": f"{(nse.get('greeks') or {}).get('count', 0)} NSE · {(mcx.get('greeks') or {}).get('count', 0)} MCX",
            "detail": "Exchange-mode Black-Scholes Greeks are computed from latest ATM premiums with expiry-aware time to expiry.",
        },
        {
            "key": "curve_roll",
            "label": "Futures Curve & Roll",
            "status": "ready" if (mcx_curve.get("count") or 0) > 0 else "attention",
            "metric": f"{mcx_curve.get('count', 0)} curves · {len(mcx_curve.get('calendar_spreads') or [])} spreads",
            "detail": "MCX curve shape, basis, annualized basis and rollover quality come from live/saved commodity futures rows.",
        },
        {
            "key": "risk_margin",
            "label": "Risk & Settlement",
            "status": "ready" if ban.get("snapshot_date") or mwpl.get("snapshot_date") or mcx_risk.get("devolvement_watch") else "attention",
            "metric": f"{ban.get('count', 0)} banned · {len(mcx_risk.get('devolvement_watch') or [])} devolvement",
            "detail": (
                f"MWPL rows {mwpl.get('row_count', 0)}, >=80% utilisation {mwpl.get('above_80_pct_count', 0)}, "
                "stock physical settlement and MCX devolvement flags are attached to contracts."
            ),
        },
        {
            "key": "live_signals",
            "label": "Live Signals",
            "status": "ready" if (nse_signals.get("count") or 0) or mcx_risk.get("spread_watch") else "attention",
            "metric": f"{nse_signals.get('count', 0)} NSE · {len(mcx_risk.get('spread_watch') or [])} MCX",
            "detail": "OI-price buildup, volatility watch, calendar spread and near-expiry devolvement signals are generated from current snapshots.",
        },
    ]

    answer_cards = [
        {
            "key": "what_happened",
            "label": "What is happening?",
            "status": nse_chain.get("status") or (fno_360 or {}).get("status") or "unknown",
            "value": f"PCR {nse_chain.get('summary', {}).get('pcr_oi', '--')}",
            "detail": (
                f"{nse_chain.get('summary', {}).get('total_underlyings', 0)} NSE underlyings, "
                f"{mcx_chain.get('rows', 0)} MCX ATM rows, "
                f"{len((nse.get('straddle_summary') or [])) + len((mcx.get('straddle_summary') or []))} straddle reads."
            ),
        },
        {
            "key": "risk_location",
            "label": "Where is the risk?",
            "status": "attention" if ban.get("count") or mcx_risk.get("devolvement_watch") else "ok",
            "value": f"{ban.get('count', 0)} ban · {len(mcx_risk.get('devolvement_watch') or [])} MCX",
            "detail": "Ban list, MWPL utilisation, physical-settlement and devolvement gates are checked before treating signals as tradeable.",
        },
        {
            "key": "changed_today",
            "label": "What changed today?",
            "status": "ready" if (nse_signals.get("count") or 0) else "attention",
            "value": f"{nse_signals.get('count', 0)} OI signals",
            "detail": f"Latest F&O snapshot {latest_time or '--'} with volatility and OI-price participant classifications.",
        },
        {
            "key": "tradeability",
            "label": "Is it tradeable?",
            "status": "ready" if (fno_360 or {}).get("status") == "ready" and not ban.get("count") else "attention",
            "value": f"{mcx_chain.get('ce_ready', 0)}/{mcx_chain.get('pe_ready', 0)} MCX CE/PE",
            "detail": "Tradeability is gated by snapshot freshness, bid/ask availability, expiry proximity, ban/MWPL and settlement rules.",
        },
    ]

    sources = [
        {
            "key": "nse_contract_catalog",
            "label": "NSE FO contract catalog",
            "status": (nse.get("source") or {}).get("status") or "unknown",
            "detail": (nse.get("source") or {}).get("source") or "fo_contract_catalog",
        },
        {
            "key": "nse_atm_snapshots",
            "label": "NSE ATM option snapshots",
            "status": (fno_360 or {}).get("status") or "unknown",
            "detail": f"latest={latest_time or '--'}",
        },
        {
            "key": "mcx_contract_catalog",
            "label": "MCX contract catalog",
            "status": "ready" if mcx_summary.get("total_contracts") else "missing",
            "detail": ((mcx.get("source") or {}).get("contract_catalog") or "commodity catalog"),
        },
        {
            "key": "mcx_atm_watchlist",
            "label": "MCX ATM watchlist",
            "status": "ready" if mcx_chain.get("rows") else "missing",
            "detail": ((mcx.get("source") or {}).get("atm_watchlist") or "commodity watchlist"),
        },
        {
            "key": "fo_risk",
            "label": "F&O MWPL / ban risk",
            "status": "ready" if ban.get("snapshot_date") or mwpl.get("snapshot_date") else "attention",
            "detail": f"snapshot={(fo_risk or {}).get('snapshot_date') or '--'}",
        },
    ]

    return {"modules": modules, "answer_cards": answer_cards, "sources": sources}


def _nse_instruments(fno_360: dict[str, Any] | None) -> list[dict[str, Any]]:
    analytics = ((fno_360 or {}).get("analytics") or {})
    instruments = analytics.get("instruments") or (fno_360 or {}).get("instruments") or []
    if instruments:
        return [item for item in instruments if isinstance(item, dict)]
    fallback = analytics.get("oi_change_contracts") or (fno_360 or {}).get("top_volume", [])
    return [item for item in fallback if isinstance(item, dict)]


def _nse_side_contracts(fno_360: dict[str, Any] | None) -> list[dict[str, Any]]:
    analytics = ((fno_360 or {}).get("analytics") or {})
    side_contracts = analytics.get("side_contracts") or []
    if side_contracts:
        return [item for item in side_contracts if isinstance(item, dict)]
    expanded: list[dict[str, Any]] = []
    for item in _nse_instruments(fno_360):
        for side in ("CE", "PE"):
            side_key = side.lower()
            leg = item.get(side_key) if isinstance(item.get(side_key), dict) else None
            expanded.append(
                {
                    "symbol": item.get("symbol") or item.get("underlying"),
                    "kind": item.get("kind"),
                    "side": side,
                    "expiry": item.get("expiry"),
                    "strike": item.get("strike"),
                    "ltp": (leg or {}).get("ltp") or item.get(f"{side_key}_ltp"),
                    "change_pct": (leg or {}).get("change_pct") or item.get(f"{side_key}_change_pct"),
                    "oi": (leg or {}).get("oi") or item.get(f"{side_key}_oi"),
                    "oi_change": (leg or {}).get("oi_change") or item.get(f"{side_key}_oi_change"),
                    "oi_change_pct": (leg or {}).get("oi_change_pct") or item.get(f"{side_key}_oi_change_pct"),
                    "volume": (leg or {}).get("volume") or item.get(f"{side_key}_volume"),
                    "iv": (leg or {}).get("iv") or item.get(f"{side_key}_iv"),
                    "buildup": item.get("buildup"),
                }
            )
    return expanded


def _nse_oi_price_signals(fno_360: dict[str, Any] | None, limit: int) -> dict[str, Any]:
    """Classify NSE ATM CE/PE instruments via the OI–price matrix.

    Uses the fno_360 instrument list which carries per-leg ``oi_change_pct``
    and ``change_pct``. Returns top items per label so the UI can render
    "long buildup / short covering / short buildup / long unwinding" cards.
    """
    side_contracts = _nse_side_contracts(fno_360)
    signals: list[dict[str, Any]] = []
    for item in side_contracts:
        symbol = str(item.get("symbol") or item.get("underlying") or "")
        if not symbol:
            continue
        side = str(item.get("side") or item.get("option_type") or "").upper()
        price_pct = _safe_float(item.get("change_pct") or item.get("price_change_pct"))
        oi_pct = _safe_float(item.get("oi_change_pct"))
        if side not in {"CE", "PE"} or (price_pct is None and oi_pct is None):
            continue
        classified = classify_oi_price(
            contract_id=f"{symbol}:{side}",
            price_change_pct=price_pct,
            oi_change_pct=oi_pct,
        )
        signals.append(
            {
                "underlying": symbol,
                "option_type": side,
                "expiry": item.get("expiry"),
                "strike": item.get("strike"),
                "label": classified.label,
                "direction": classified.direction,
                "conviction": classified.conviction,
                "price_change_pct": classified.price_change_pct,
                "oi_change_pct": classified.oi_change_pct,
                "oi": item.get("oi"),
                "volume": item.get("volume"),
                "iv": item.get("iv"),
                "notes": classified.notes,
            }
        )

    by_label: dict[str, list[dict[str, Any]]] = {}
    for sig in signals:
        by_label.setdefault(sig["label"], []).append(sig)

    return {
        "count": len(signals),
        "by_label": {
            label: sorted(rows, key=lambda r: abs(r.get("oi_change_pct") or 0.0), reverse=True)[:limit]
            for label, rows in by_label.items()
        },
        "top": sorted(
            signals,
            key=lambda r: (
                {"high": 0, "medium": 1, "low": 2}.get(r.get("conviction") or "low", 3),
                -abs(r.get("oi_change_pct") or 0.0),
            ),
        )[:limit],
    }


def _nse_option_greeks(fno_360: dict[str, Any] | None, limit: int) -> dict[str, Any]:
    """Compute Greeks for each ATM instrument that has spot+strike+expiry+premium."""
    instruments = _nse_instruments(fno_360)
    rows: list[dict[str, Any]] = []
    for item in instruments:
        if not isinstance(item, dict):
            continue
        spot = _safe_float(item.get("spot_price") or item.get("underlying_price"))
        strike = _safe_float(item.get("strike"))
        expiry = item.get("expiry")
        for side in ("ce", "pe"):
            leg = item.get(side) if isinstance(item.get(side), dict) else None
            ltp = _safe_float((leg or {}).get("ltp") or item.get(f"{side}_ltp"))
            payload = _greeks_for_leg(option_type=side.upper(), spot=spot, strike=strike, expiry=expiry, ltp=ltp)
            if payload is None:
                continue
            rows.append(
                {
                    "underlying": item.get("symbol") or item.get("underlying"),
                    "expiry": expiry,
                    "strike": strike,
                    "option_type": side.upper(),
                    "ltp": _round(ltp, 2),
                    **payload,
                }
            )
    return {"rows": rows, "mode": "exchange_10pct", "count": len(rows)}


def _nse_straddle_summary(fno_360: dict[str, Any] | None, limit: int) -> list[dict[str, Any]]:
    """Compute ATM straddle, expected move, PCR-OI per underlying.

    The fno_360 statistics endpoint emits one row per NSE underlying with
    ATM CE and PE LTP, OI, IV and the spot price. Sum the two LTPs and
    you get the market-implied 1-σ move by expiry (straddle ≈ expected
    move). Express it as a percentage of spot for cross-symbol comparison.
    """
    instruments = _nse_instruments(fno_360)
    rows: list[dict[str, Any]] = []
    for item in instruments:
        if not isinstance(item, dict):
            continue
        spot = _safe_float(item.get("spot_price") or item.get("underlying_price"))
        strike = _safe_float(item.get("strike"))
        ce = item.get("ce") if isinstance(item.get("ce"), dict) else None
        pe = item.get("pe") if isinstance(item.get("pe"), dict) else None
        ce_ltp = _safe_float((ce or {}).get("ltp") or item.get("ce_ltp"))
        pe_ltp = _safe_float((pe or {}).get("ltp") or item.get("pe_ltp"))
        ce_oi = _safe_float((ce or {}).get("oi") or item.get("ce_oi"))
        pe_oi = _safe_float((pe or {}).get("oi") or item.get("pe_oi"))
        ce_iv = _safe_float((ce or {}).get("iv") or item.get("ce_iv"))
        pe_iv = _safe_float((pe or {}).get("iv") or item.get("pe_iv"))
        if spot is None or spot <= 0 or ce_ltp is None or pe_ltp is None:
            continue
        straddle = ce_ltp + pe_ltp
        straddle_pct = straddle / spot * 100.0
        avg_iv = None
        ivs = [v for v in (ce_iv, pe_iv) if v is not None]
        if ivs:
            avg_iv = sum(ivs) / len(ivs)
        rows.append(
            {
                "underlying": item.get("symbol") or item.get("underlying"),
                "kind": item.get("kind"),
                "expiry": item.get("expiry"),
                "spot_price": _round(spot, 2),
                "atm_strike": _round(strike, 2),
                "ce_ltp": _round(ce_ltp, 2),
                "pe_ltp": _round(pe_ltp, 2),
                "atm_straddle": _round(straddle, 2),
                "expected_move": _round(straddle, 2),
                "expected_move_pct": _round(straddle_pct, 3),
                "avg_iv": _round(avg_iv, 4),
                "ce_oi": _round(ce_oi, 0),
                "pe_oi": _round(pe_oi, 0),
                "pcr_oi": _ratio(pe_oi, ce_oi),
            }
        )
    # Sort by expected_move_pct descending — highest implied move first.
    rows.sort(key=lambda r: (r.get("expected_move_pct") or 0.0), reverse=True)
    return rows


async def build_fno_analytics(*, fno_360: dict[str, Any] | None = None, limit: int = 20) -> dict[str, Any]:
    # Load the active contract master broadly; the UI limit only applies to
    # watchlists/signal rows, not to data-quality and universe counts.
    nse_contracts, nse_source = await _load_nse_contracts(limit=max(limit * 100, 10_000))
    contract_catalog, atm_watchlist = await _load_mcx_snapshot()
    mcx = _analyze_mcx(contract_catalog, atm_watchlist)
    nse_oi_signals = _nse_oi_price_signals(fno_360, limit)
    nse_greeks = _nse_option_greeks(fno_360, limit)
    nse_straddles = _nse_straddle_summary(fno_360, limit)
    # Max-pain and chain-wide PCR-OI from option_premium_candles. This
    # joins all available strikes per (underlying, expiry), not just ATM,
    # so the resistance / support reads pick up the strikes where market
    # makers are most exposed.
    chain_max_pain = await _load_chain_max_pain(limit_underlyings=max(limit * 5, 500))
    # F&O risk snapshot: MWPL utilisation + ban list. Daily file; cheap
    # to read every cycle (single SELECT).
    fo_risk: dict[str, Any] = {}
    try:
        from market_data.fo_risk_ingest import latest_fo_risk_snapshot

        fo_risk = await latest_fo_risk_snapshot()
    except Exception as exc:
        logger.debug(f"[FNOAnalytics] FO risk snapshot read failed: {exc}")
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
        "greeks": nse_greeks,
        "oi_price_signals": nse_oi_signals,
        "straddle_summary": nse_straddles,
        "max_pain": [
            row for row in chain_max_pain
            if (row.get("underlying") or "") not in {"CRUDEOIL", "GOLD", "SILVERM", "NATURALGAS"}
        ],
    }
    # Attach commodity rows to the mcx block so the UI can render
    # commodity max-pain alongside the futures curve.
    mcx_max_pain = [
        row for row in chain_max_pain
        if (row.get("underlying") or "") in {"CRUDEOIL", "GOLD", "SILVERM", "NATURALGAS"}
    ]
    mcx["max_pain"] = mcx_max_pain
    quality_checks = _build_quality_checks(nse, mcx, fno_360)
    research = _research_modules(nse, mcx, fno_360, fo_risk)
    return {
        "status": "ready" if any(check["status"] == "ok" for check in quality_checks) else "attention",
        "as_of": _utc_now().isoformat(),
        "module": "fno_contract_analytics",
        "scope": ["NSE_FO", "BSE_FO", "MCX_COM"],
        "nse": nse,
        "mcx": mcx,
        "fo_risk": fo_risk,
        "research": research,
        "quality_checks": quality_checks,
        "stage_status": _stage_status(nse, mcx, fno_360),
        "signals": {
            "nse": {
                "top_volume": (fno_360 or {}).get("top_volume", [])[:limit],
                "oi_change_contracts": ((fno_360 or {}).get("analytics") or {}).get("oi_change_contracts", [])[:limit],
                "volatility_watch": ((fno_360 or {}).get("analytics") or {}).get("volatility_watch", [])[:limit],
                "instruments": ((fno_360 or {}).get("analytics") or {}).get("instruments", []),
                "side_contracts": ((fno_360 or {}).get("analytics") or {}).get("side_contracts", []),
                "oi_price_matrix": nse_oi_signals,
                "greeks": nse_greeks,
                "straddle_summary": nse_straddles,
                "max_pain": nse.get("max_pain", []),
            },
            "mcx": {
                "devolvement_watch": (mcx.get("risk") or {}).get("devolvement_watch", [])[:limit],
                "spread_watch": (mcx.get("risk") or {}).get("spread_watch", [])[:limit],
                "calendar_spreads": (mcx.get("futures_curve") or {}).get("calendar_spreads", [])[:limit],
                "futures_curves": (mcx.get("futures_curve") or {}).get("curves", []),
                "positioning": (mcx.get("positioning") or {}).get("strikes", [])[:limit],
                "greeks": (mcx.get("greeks") or {}).get("rows", [])[:limit * 4],
                "straddle_summary": (mcx.get("option_chain") or {}).get("straddle_summary", []),
                "max_pain": mcx.get("max_pain", []),
            },
        },
    }
