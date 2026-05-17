"""Market data routes."""
from __future__ import annotations
import asyncio
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, List, Optional
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Query
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from market_data import (
    atm_watchlist_service,
    data_router,
    market_profile_builder,
    option_chain_service,
)
from db.database import AsyncSessionLocal
from market_data.market_intelligence_runtime import APP_SYMBOLS
from market_data.symbols import to_app_symbol, to_broker_symbol, to_fyers_symbol
from analytics.greeks import bs_greeks, implied_volatility
from analytics.sector import sector_tracker
from macro_research import macro_research_service
from sector_interaction.india_live import india_live_sector_service
from market_data.source_policy import route_order, source_policy_snapshot
from market_data.fno_analytics import build_fno_analytics
from api.routers.auth import (
    ensure_fyers_session,
    ensure_upstox_session,
    get_active_adapter,
    get_broker_token,
)

router = APIRouter(prefix="/api/market", tags=["market"])

_PROFILE_TIMEFRAMES = {"day", "week", "month", "daily", "hourly"}
_INDEX_UNDERLYING_BY_APP_SYMBOL = {app_symbol: symbol_code for symbol_code, app_symbol in APP_SYMBOLS.items()}
_INDEX_PRICE_BANDS: dict[str, tuple[float, float]] = {
    "NIFTY": (10_000.0, 50_000.0),
    "BANKNIFTY": (20_000.0, 100_000.0),
    "FINNIFTY": (10_000.0, 60_000.0),
    "MIDCPNIFTY": (5_000.0, 40_000.0),
    "SENSEX": (30_000.0, 150_000.0),
}
_MARKET_SNAPSHOT_STALE_AFTER_SECONDS = 15 * 60
_LATEST_TICKS_LIVE_TIMEOUT_SECONDS = 1.75
_FNO_360_STALE_AFTER_SECONDS = 36 * 60 * 60


def _safe_float(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _optional_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_float(value: float | int | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _ratio(numerator: float, denominator: float, digits: int = 3) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, digits)


def _mean(values: list[float | None]) -> float | None:
    usable = [value for value in values if value is not None]
    if not usable:
        return None
    return sum(usable) / len(usable)


def _iso_datetime(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None:
        return None
    return str(value)


def _classify_fno_buildup(avg_change_pct: float | None, net_oi_change: float) -> str:
    if avg_change_pct is None:
        return "neutral"
    if avg_change_pct > 0 and net_oi_change > 0:
        return "bullish_long_buildup"
    if avg_change_pct < 0 and net_oi_change < 0:
        return "bearish_short_buildup"
    if avg_change_pct > 0 and net_oi_change < 0:
        return "short_covering"
    if avg_change_pct < 0 and net_oi_change > 0:
        return "long_unwinding"
    return "neutral"


def _bucket_fno_move(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < -5:
        return "lt_minus_5"
    if value < -2:
        return "minus_5_to_minus_2"
    if value < 0:
        return "minus_2_to_0"
    if value <= 2:
        return "zero_to_2"
    if value <= 5:
        return "two_to_5"
    return "gt_5"


def _top_by(items: list[dict[str, Any]], key: str, limit: int, reverse: bool = True) -> list[dict[str, Any]]:
    fallback = -999999999.0 if reverse else 999999999.0
    return sorted(items, key=lambda item: item.get(key) if item.get(key) is not None else fallback, reverse=reverse)[:limit]


def _utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _empty_fno_360_payload(
    status: str,
    *,
    error: str | None = None,
    latest_time: datetime | None = None,
    stale_seconds: float | None = None,
) -> dict:
    latest_time = _utc_datetime(latest_time)
    payload = {
        "status": status,
        "source": "atm_option_watchlist_snapshots",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "latest_time": latest_time.isoformat() if latest_time else None,
        "stale_seconds": stale_seconds,
        "market": {"total_underlyings": 0, "ce_ready": 0, "pe_ready": 0},
        "breadth": {"advancers": 0, "decliners": 0, "unchanged": 0},
        "buildup_counts": {},
        "top_volume": [],
        "top_oi": [],
        "top_gainers": [],
        "top_losers": [],
        "top_iv": [],
        "analytics": {},
    }
    if error:
        payload["error"] = error
    return payload


async def _fno_360_statistics(limit: int = 10) -> dict:
    """Build persisted F&O breadth, PCR, IV, OI and buildup statistics."""
    now_utc = datetime.now(timezone.utc)
    try:
        async with AsyncSessionLocal() as session:
            latest_snapshot_time = await session.scalar(
                text(
                    """
                    SELECT MAX(time)
                    FROM atm_option_watchlist_snapshots
                    WHERE expiry >= CURRENT_DATE
                      AND option_type IN ('CE', 'PE')
                    """
                )
            )
            latest_snapshot_time = _utc_datetime(latest_snapshot_time)
            stale_seconds = (
                max((now_utc - latest_snapshot_time).total_seconds(), 0.0)
                if latest_snapshot_time is not None
                else None
            )
            if latest_snapshot_time is None:
                return _empty_fno_360_payload("missing")
            if stale_seconds is not None and stale_seconds > _FNO_360_STALE_AFTER_SECONDS:
                return _empty_fno_360_payload("stale", latest_time=latest_snapshot_time, stale_seconds=stale_seconds)

            result = await session.execute(
                text(
                    """
                    WITH latest AS (
                        SELECT DISTINCT ON (underlying, option_type)
                            time,
                            underlying,
                            kind,
                            expiry,
                            strike,
                            option_type,
                            underlying_price,
                            ltp,
                            change_pct,
                            oi,
                            oi_change,
                            oi_change_pct,
                            volume,
                            iv
                        FROM atm_option_watchlist_snapshots
                        WHERE expiry >= CURRENT_DATE
                          AND option_type IN ('CE', 'PE')
                        ORDER BY underlying, option_type, time DESC
                    )
                    SELECT *
                    FROM latest
                    ORDER BY underlying ASC, option_type ASC
                    """
                )
            )
            rows = [dict(row) for row in result.mappings().all()]
    except Exception as exc:
        logger.warning(f"[Market] FNO 360 statistics unavailable: {exc}")
        return _empty_fno_360_payload("unavailable", error=str(exc))

    by_underlying: dict[str, dict[str, Any]] = {}
    latest_times: list[datetime] = []
    for row in rows:
        underlying = str(row.get("underlying") or "").upper()
        side = str(row.get("option_type") or "").upper()
        if not underlying or side not in {"CE", "PE"}:
            continue

        row_time = row.get("time")
        if isinstance(row_time, datetime):
            latest_times.append(row_time)

        item = by_underlying.setdefault(
            underlying,
            {
                "symbol": underlying,
                "kind": row.get("kind") or "stock",
                "expiry": row.get("expiry").isoformat() if hasattr(row.get("expiry"), "isoformat") else row.get("expiry"),
                "strike": _safe_float(row.get("strike")),
                "spot_price": _safe_float(row.get("underlying_price")),
                "latest_time": _iso_datetime(row_time),
            },
        )
        if isinstance(row_time, datetime):
            current_time = item.get("_latest_dt")
            if not isinstance(current_time, datetime) or row_time > current_time:
                item["_latest_dt"] = row_time
                item["latest_time"] = row_time.isoformat()
        if _safe_float(row.get("underlying_price")):
            item["spot_price"] = _safe_float(row.get("underlying_price"))
        if row.get("kind"):
            item["kind"] = row.get("kind")

        item[side.lower()] = {
            "ltp": _safe_float(row.get("ltp")),
            "oi": _safe_float(row.get("oi")),
            "oi_change": _safe_float(row.get("oi_change")),
            "oi_change_pct": _safe_float(row.get("oi_change_pct")),
            "volume": _safe_float(row.get("volume")),
            "iv": _optional_float(row.get("iv")),
            "change_pct": _optional_float(row.get("change_pct")),
        }

    instruments: list[dict[str, Any]] = []
    totals = {
        "ce_oi": 0.0,
        "pe_oi": 0.0,
        "ce_volume": 0.0,
        "pe_volume": 0.0,
        "ce_oi_change": 0.0,
        "pe_oi_change": 0.0,
    }
    breadth = {"advancers": 0, "decliners": 0, "unchanged": 0}
    buildup_counts: dict[str, int] = {
        "bullish_long_buildup": 0,
        "bearish_short_buildup": 0,
        "short_covering": 0,
        "long_unwinding": 0,
        "neutral": 0,
    }
    iv_values: list[float] = []
    change_values: list[float] = []
    side_contracts: list[dict[str, Any]] = []
    momentum_distribution = {
        "lt_minus_5": 0,
        "minus_5_to_minus_2": 0,
        "minus_2_to_0": 0,
        "zero_to_2": 0,
        "two_to_5": 0,
        "gt_5": 0,
        "unknown": 0,
    }

    for item in by_underlying.values():
        ce = item.get("ce") or {}
        pe = item.get("pe") or {}
        ce_oi = _safe_float(ce.get("oi"))
        pe_oi = _safe_float(pe.get("oi"))
        ce_volume = _safe_float(ce.get("volume"))
        pe_volume = _safe_float(pe.get("volume"))
        ce_oi_change = _safe_float(ce.get("oi_change"))
        pe_oi_change = _safe_float(pe.get("oi_change"))
        avg_iv = _mean([_optional_float(ce.get("iv")), _optional_float(pe.get("iv"))])
        avg_change_pct = _mean([_optional_float(ce.get("change_pct")), _optional_float(pe.get("change_pct"))])
        net_oi_change = ce_oi_change - pe_oi_change
        buildup = _classify_fno_buildup(avg_change_pct, net_oi_change)
        buildup_counts[buildup] = buildup_counts.get(buildup, 0) + 1
        momentum_distribution[_bucket_fno_move(avg_change_pct)] += 1

        if avg_change_pct is None or abs(avg_change_pct) < 0.01:
            breadth["unchanged"] += 1
        elif avg_change_pct > 0:
            breadth["advancers"] += 1
        else:
            breadth["decliners"] += 1
        if avg_iv is not None:
            iv_values.append(avg_iv)
        if avg_change_pct is not None:
            change_values.append(avg_change_pct)

        totals["ce_oi"] += ce_oi
        totals["pe_oi"] += pe_oi
        totals["ce_volume"] += ce_volume
        totals["pe_volume"] += pe_volume
        totals["ce_oi_change"] += ce_oi_change
        totals["pe_oi_change"] += pe_oi_change

        instrument = {
            "symbol": item["symbol"],
            "kind": item.get("kind"),
            "expiry": item.get("expiry"),
            "strike": _round_float(item.get("strike"), 2),
            "spot_price": _round_float(item.get("spot_price"), 2),
            "latest_time": item.get("latest_time"),
            "ce_ltp": _round_float(_safe_float(ce.get("ltp")), 2),
            "pe_ltp": _round_float(_safe_float(pe.get("ltp")), 2),
            "ce_oi": _round_float(ce_oi, 0),
            "pe_oi": _round_float(pe_oi, 0),
            "total_oi": _round_float(ce_oi + pe_oi, 0),
            "ce_volume": _round_float(ce_volume, 0),
            "pe_volume": _round_float(pe_volume, 0),
            "total_volume": _round_float(ce_volume + pe_volume, 0),
            "ce_oi_change": _round_float(ce_oi_change, 0),
            "pe_oi_change": _round_float(pe_oi_change, 0),
            "net_oi_change": _round_float(net_oi_change, 0),
            "pcr_oi": _ratio(pe_oi, ce_oi),
            "pcr_volume": _ratio(pe_volume, ce_volume),
            "ce_iv": _round_float(_optional_float(ce.get("iv")), 2),
            "pe_iv": _round_float(_optional_float(pe.get("iv")), 2),
            "avg_iv": _round_float(avg_iv, 2),
            "ce_change_pct": _round_float(_optional_float(ce.get("change_pct")), 2),
            "pe_change_pct": _round_float(_optional_float(pe.get("change_pct")), 2),
            "avg_change_pct": _round_float(avg_change_pct, 2),
            "buildup": buildup,
        }
        instrument.pop("_latest_dt", None)
        instruments.append(instrument)

        for side, side_row in (("CE", ce), ("PE", pe)):
            if not side_row:
                continue
            side_contracts.append(
                {
                    "symbol": item["symbol"],
                    "kind": item.get("kind"),
                    "side": side,
                    "expiry": item.get("expiry"),
                    "strike": _round_float(item.get("strike"), 2),
                    "ltp": _round_float(_safe_float(side_row.get("ltp")), 2),
                    "change_pct": _round_float(_optional_float(side_row.get("change_pct")), 2),
                    "oi": _round_float(_safe_float(side_row.get("oi")), 0),
                    "oi_change": _round_float(_safe_float(side_row.get("oi_change")), 0),
                    "oi_change_pct": _round_float(_safe_float(side_row.get("oi_change_pct")), 2),
                    "volume": _round_float(_safe_float(side_row.get("volume")), 0),
                    "iv": _round_float(_optional_float(side_row.get("iv")), 2),
                    "buildup": buildup,
                }
            )

    latest_time = _utc_datetime(max(latest_times) if latest_times else None)
    stale_seconds = max((now_utc - latest_time).total_seconds(), 0.0) if latest_time is not None else None
    total_underlyings = len(instruments)
    status = "ready" if total_underlyings else "missing"
    total_oi = totals["ce_oi"] + totals["pe_oi"]
    total_volume = totals["ce_volume"] + totals["pe_volume"]
    top_oi_all = _top_by(instruments, "total_oi", limit)
    top_volume_all = _top_by(instruments, "total_volume", limit)
    long_count = buildup_counts.get("bullish_long_buildup", 0)
    short_count = buildup_counts.get("bearish_short_buildup", 0)
    short_covering_count = buildup_counts.get("short_covering", 0)
    long_unwinding_count = buildup_counts.get("long_unwinding", 0)
    directional_total = long_count + short_count + short_covering_count + long_unwinding_count
    market_bias_score = (
        ((long_count + short_covering_count) - (short_count + long_unwinding_count)) / directional_total * 100.0
        if directional_total
        else 0.0
    )
    index_order = {"NIFTY": 0, "BANKNIFTY": 1, "FINNIFTY": 2, "MIDCPNIFTY": 3, "SENSEX": 4}
    index_watch = sorted(
        [item for item in instruments if str(item.get("kind")).lower() == "index"],
        key=lambda item: index_order.get(str(item.get("symbol") or ""), 99),
    )
    stock_instruments = [item for item in instruments if str(item.get("kind")).lower() == "stock"]
    contributor_rows = [
        {
            **item,
            "impact_score": _round_float((_safe_float(item.get("avg_change_pct")) * _safe_float(item.get("total_volume"))) / 1_000_000, 2),
        }
        for item in stock_instruments
    ]
    volatility_rows = [
        {
            **item,
            "iv_spread": _round_float(abs(_safe_float(item.get("ce_iv")) - _safe_float(item.get("pe_iv"))), 2),
        }
        for item in instruments
    ]
    high_low_candidates = sorted(
        stock_instruments,
        key=lambda item: (abs(_safe_float(item.get("avg_change_pct"))), _safe_float(item.get("total_volume"))),
        reverse=True,
    )[:limit]
    return {
        "status": status,
        "source": "atm_option_watchlist_snapshots",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "latest_time": latest_time.isoformat() if latest_time else None,
        "stale_seconds": stale_seconds,
        "market": {
            "total_underlyings": total_underlyings,
            "stock_underlyings": sum(1 for item in instruments if str(item.get("kind")).lower() == "stock"),
            "index_underlyings": sum(1 for item in instruments if str(item.get("kind")).lower() == "index"),
            "ce_ready": sum(1 for item in instruments if item.get("ce_oi", 0) > 0 or item.get("ce_volume", 0) > 0),
            "pe_ready": sum(1 for item in instruments if item.get("pe_oi", 0) > 0 or item.get("pe_volume", 0) > 0),
            "ce_oi_total": _round_float(totals["ce_oi"], 0),
            "pe_oi_total": _round_float(totals["pe_oi"], 0),
            "pcr_oi": _ratio(totals["pe_oi"], totals["ce_oi"]),
            "ce_volume_total": _round_float(totals["ce_volume"], 0),
            "pe_volume_total": _round_float(totals["pe_volume"], 0),
            "pcr_volume": _ratio(totals["pe_volume"], totals["ce_volume"]),
            "ce_oi_change_total": _round_float(totals["ce_oi_change"], 0),
            "pe_oi_change_total": _round_float(totals["pe_oi_change"], 0),
            "net_oi_change_total": _round_float(totals["ce_oi_change"] - totals["pe_oi_change"], 0),
            "average_iv": _round_float(_mean(iv_values), 2),
            "average_change_pct": _round_float(_mean(change_values), 2),
        },
        "breadth": breadth,
        "buildup_counts": buildup_counts,
        "instruments": sorted(instruments, key=lambda item: str(item.get("symbol") or "")),
        "top_volume": top_volume_all,
        "top_oi": top_oi_all,
        "top_gainers": _top_by(instruments, "avg_change_pct", limit),
        "top_losers": _top_by(instruments, "avg_change_pct", limit, reverse=False),
        "top_iv": _top_by(instruments, "avg_iv", limit),
        "analytics": {
            "index_watch": index_watch,
            "market_bias": {
                "score": _round_float(market_bias_score, 2),
                "label": "bullish" if market_bias_score > 15 else "bearish" if market_bias_score < -15 else "balanced",
                "long": long_count,
                "short": short_count,
                "short_covering": short_covering_count,
                "long_unwinding": long_unwinding_count,
                "directional_total": directional_total,
            },
            "momentum_distribution": momentum_distribution,
            "oi_concentration": {
                "top_10_oi_share_pct": _round_float((sum(_safe_float(item.get("total_oi")) for item in top_oi_all) / total_oi * 100.0) if total_oi else None, 2),
                "top_10_volume_share_pct": _round_float((sum(_safe_float(item.get("total_volume")) for item in top_volume_all) / total_volume * 100.0) if total_volume else None, 2),
                "largest_oi_symbol": top_oi_all[0]["symbol"] if top_oi_all else None,
                "largest_volume_symbol": top_volume_all[0]["symbol"] if top_volume_all else None,
            },
            "instruments": sorted(instruments, key=lambda item: str(item.get("symbol") or "")),
            "side_contracts": sorted(
                side_contracts,
                key=lambda item: (str(item.get("symbol") or ""), str(item.get("side") or "")),
            ),
            "active_options": _top_by(side_contracts, "volume", limit),
            "oi_change_contracts": sorted(
                side_contracts,
                key=lambda item: abs(_safe_float(item.get("oi_change"))),
                reverse=True,
            )[:limit],
            "futures_gainers": _top_by(stock_instruments, "avg_change_pct", limit),
            "futures_losers": _top_by(stock_instruments, "avg_change_pct", limit, reverse=False),
            "high_low_candidates": high_low_candidates,
            "volatility_watch": _top_by(volatility_rows, "avg_iv", limit),
            "iv_spread_watch": _top_by(volatility_rows, "iv_spread", limit),
            "positive_contributors": _top_by(contributor_rows, "impact_score", limit),
            "negative_contributors": _top_by(contributor_rows, "impact_score", limit, reverse=False),
        },
    }


@router.get("/intelligence-context")
async def market_intelligence_context() -> dict:
    """Merged market-intelligence context for UI and strategy agents."""
    sector_interaction, macro_research, fno_360 = await asyncio.gather(
        india_live_sector_service.market_intelligence_payload(),
        macro_research_service.overview(refresh=False),
        _fno_360_statistics(),
    )
    active_brokers = [
        source
        for source in ("fyers", "upstox")
        if get_active_adapter(source) is not None
    ]
    return {
        "module": "market_intelligence_context",
        "country": "IN",
        "sector_interaction": sector_interaction,
        "macro_research": macro_research,
        "fno_360": fno_360,
        "market_read": macro_research.get("market_read", {}),
        "source_policy": source_policy_snapshot(active_brokers=active_brokers),
    }


@router.get("/fno-analytics")
async def fno_analytics(limit: int = Query(20, ge=1, le=100)) -> dict:
    """Contract-first NSE + MCX F&O analytics and data-quality context."""
    fno_360 = await _fno_360_statistics(limit=limit)
    return await build_fno_analytics(fno_360=fno_360, limit=limit)


@router.post("/fo-risk/refresh")
async def fo_risk_refresh() -> dict:
    """Force a fresh fetch of the NSE MWPL + F&O ban-list CSVs.

    Normally driven by the research-sync daemon; this endpoint exists
    for the first-run backfill and for operator-triggered refreshes
    when NSE posts an intraday update.
    """
    from market_data.fo_risk_ingest import ingest_fo_risk_snapshot, latest_fo_risk_snapshot

    summary = await ingest_fo_risk_snapshot()
    latest = await latest_fo_risk_snapshot()
    return {"summary": summary.to_dict(), "latest": latest}


@dataclass(frozen=True)
class _ResolvedMarketSymbol:
    app_symbol: str
    broker_symbol: str
    fyers_symbol: str


def _resolve_market_symbol(symbol: str) -> _ResolvedMarketSymbol:
    app_symbol = to_app_symbol(symbol)
    return _ResolvedMarketSymbol(
        app_symbol=app_symbol,
        broker_symbol=to_broker_symbol(app_symbol),
        fyers_symbol=to_fyers_symbol(app_symbol),
    )


def _market_symbol_for_adapter(adapter, market_symbol: _ResolvedMarketSymbol) -> str:
    if getattr(adapter, "broker_name", "") == "fyers":
        return market_symbol.fyers_symbol
    return market_symbol.broker_symbol


async def _get_market_adapter():
    for source in route_order("option_chain"):
        if source == "fyers":
            fyers = get_active_adapter("fyers")
            if fyers:
                return fyers, "fyers"
            if await ensure_fyers_session():
                fyers = get_active_adapter("fyers")
                if fyers:
                    return fyers, "fyers"
        elif source == "upstox":
            upstox = get_active_adapter("upstox")
            if upstox:
                return upstox, "upstox"
            if await ensure_upstox_session():
                upstox = get_active_adapter("upstox")
                if upstox:
                    return upstox, "upstox"

    return None, "none"


async def _resolve_option_expiry(adapter, broker_symbol: str, requested_expiry: Optional[str]) -> Optional[str]:
    get_contracts = getattr(adapter, "get_option_contracts", None)
    if not callable(get_contracts):
        return requested_expiry

    try:
        contracts = await get_contracts(broker_symbol)
    except Exception:
        return requested_expiry

    expiries = sorted({row.get("expiry") for row in contracts if row.get("expiry")})
    if not expiries:
        return requested_expiry
    if requested_expiry and requested_expiry in expiries:
        return requested_expiry

    today = date.today().isoformat()
    for expiry in expiries:
        if expiry >= today:
            return expiry
    return expiries[0]


async def _local_option_expiries(app_symbol: str) -> list[str]:
    underlying = _INDEX_UNDERLYING_BY_APP_SYMBOL.get(app_symbol)
    if not underlying:
        return []
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT DISTINCT expiry
                FROM fo_contract_catalog
                WHERE underlying = :underlying
                  AND expiry >= CURRENT_DATE
                ORDER BY expiry ASC
                LIMIT 12
                """
            ),
            {"underlying": underlying},
        )
        rows = result.fetchall()
    return [row.expiry.isoformat() for row in rows if getattr(row, "expiry", None) is not None]


def _normalize_profile_timeframe(timeframe: str) -> str:
    normalized = (timeframe or "day").lower()
    if normalized not in _PROFILE_TIMEFRAMES:
        raise HTTPException(
            status_code=400,
            detail="Unsupported timeframe. Use one of: day, week, month, daily, hourly.",
        )
    return "day" if normalized == "daily" else normalized


def _is_valid_index_price(app_symbol: str, value: float | int | None) -> bool:
    try:
        price = float(value or 0.0)
    except (TypeError, ValueError):
        return False
    if price <= 0:
        return False
    underlying = _INDEX_UNDERLYING_BY_APP_SYMBOL.get(app_symbol)
    band = _INDEX_PRICE_BANDS.get(str(underlying or "").upper())
    if not band:
        return True
    low, high = band
    return low <= price <= high


class LatestTickSnapshot(BaseModel):
    symbol: str
    ltp: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    oi: float = 0.0
    timestamp: str | None = None
    source: str = "unavailable"
    stale_seconds: float | None = None
    stale: bool = True


def _snapshot_age_seconds(timestamp: datetime | None) -> float | None:
    if timestamp is None:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = timestamp.astimezone(timezone.utc)
    return max((datetime.now(timezone.utc) - timestamp).total_seconds(), 0.0)


def _build_tick_snapshot(
    *,
    app_symbol: str,
    ltp: float | int | None,
    open_: float | int | None = None,
    high: float | int | None = None,
    low: float | int | None = None,
    close: float | int | None = None,
    volume: float | int | None = 0.0,
    oi: float | int | None = 0.0,
    timestamp: datetime | None = None,
    source: str,
) -> LatestTickSnapshot | None:
    if not _is_valid_index_price(app_symbol, ltp):
        return None

    price = float(ltp or 0.0)
    safe_open = float(open_ or price)
    safe_high = float(high or price)
    safe_low = float(low or price)
    safe_close = float(close or price)
    age = _snapshot_age_seconds(timestamp)
    return LatestTickSnapshot(
        symbol=app_symbol,
        ltp=price,
        open=safe_open if _is_valid_index_price(app_symbol, safe_open) else price,
        high=safe_high if _is_valid_index_price(app_symbol, safe_high) else max(price, safe_close),
        low=safe_low if _is_valid_index_price(app_symbol, safe_low) else min(price, safe_close),
        close=safe_close if _is_valid_index_price(app_symbol, safe_close) else price,
        volume=float(volume or 0.0),
        oi=float(oi or 0.0),
        timestamp=timestamp.isoformat() if timestamp else None,
        source=source,
        stale_seconds=age,
        stale=age is None or age > _MARKET_SNAPSHOT_STALE_AFTER_SECONDS,
    )


async def _latest_market_tick_snapshot(market_symbol: _ResolvedMarketSymbol) -> LatestTickSnapshot | None:
    underlying = _INDEX_UNDERLYING_BY_APP_SYMBOL.get(market_symbol.app_symbol)
    band = _INDEX_PRICE_BANDS.get(str(underlying or "").upper())
    if not underlying or not band:
        return None

    low, high = band
    candidates = [market_symbol.app_symbol, market_symbol.broker_symbol, market_symbol.fyers_symbol]
    try:
        async with AsyncSessionLocal() as session:
            tick_result = await session.execute(
                text(
                    """
                    SELECT time, ltp, open, high, low, close, volume, oi
                    FROM market_ticks
                    WHERE symbol = ANY(:symbols)
                      AND ltp IS NOT NULL
                      AND ltp BETWEEN :low AND :high
                    ORDER BY time DESC
                    LIMIT 1
                    """
                ),
                {"symbols": candidates, "low": low, "high": high},
            )
            tick = tick_result.first()
            if tick:
                return _build_tick_snapshot(
                    app_symbol=market_symbol.app_symbol,
                    ltp=getattr(tick, "ltp", None),
                    open_=getattr(tick, "open", None),
                    high=getattr(tick, "high", None),
                    low=getattr(tick, "low", None),
                    close=getattr(tick, "close", None),
                    volume=getattr(tick, "volume", None),
                    oi=getattr(tick, "oi", None),
                    timestamp=getattr(tick, "time", None),
                    source="market_ticks",
                )

            candle_result = await session.execute(
                text(
                    """
                    SELECT time, open, high, low, close, volume, oi
                    FROM underlying_spot_candles
                    WHERE underlying = :underlying
                      AND close IS NOT NULL
                      AND close BETWEEN :low AND :high
                    ORDER BY time DESC
                    LIMIT 1
                    """
                ),
                {"underlying": underlying, "low": low, "high": high},
            )
            candle = candle_result.first()
            if candle:
                return _build_tick_snapshot(
                    app_symbol=market_symbol.app_symbol,
                    ltp=getattr(candle, "close", None),
                    open_=getattr(candle, "open", None),
                    high=getattr(candle, "high", None),
                    low=getattr(candle, "low", None),
                    close=getattr(candle, "close", None),
                    volume=getattr(candle, "volume", None),
                    oi=getattr(candle, "oi", None),
                    timestamp=getattr(candle, "time", None),
                    source="underlying_spot_candles",
                )
    except Exception as exc:
        logger.trace(f"[Market] Local latest tick unavailable for {market_symbol.app_symbol}: {exc}")
    return None


async def _latest_index_tick_snapshot(
    app_symbol: str,
    *candidates: float | int | None,
    source: str = "data_router",
) -> LatestTickSnapshot:
    for candidate in candidates:
        if _is_valid_index_price(app_symbol, candidate):
            db_snapshot = await _latest_market_tick_snapshot(_resolve_market_symbol(app_symbol))
            price = float(candidate or 0.0)
            snapshot = _build_tick_snapshot(
                app_symbol=app_symbol,
                ltp=price,
                open_=db_snapshot.open if db_snapshot else None,
                high=max(db_snapshot.high, price) if db_snapshot and db_snapshot.high else None,
                low=min(db_snapshot.low, price) if db_snapshot and db_snapshot.low else None,
                close=db_snapshot.close if db_snapshot and db_snapshot.close else None,
                volume=db_snapshot.volume if db_snapshot else 0.0,
                oi=db_snapshot.oi if db_snapshot else 0.0,
                timestamp=datetime.now(timezone.utc),
                source=source,
            )
            if snapshot:
                return snapshot

    db_snapshot = await _latest_market_tick_snapshot(_resolve_market_symbol(app_symbol))
    if db_snapshot:
        return db_snapshot
    return LatestTickSnapshot(symbol=app_symbol, source="unavailable")


async def _latest_local_spot_close(app_symbol: str) -> float:
    snapshot = await _latest_market_tick_snapshot(_resolve_market_symbol(app_symbol))
    return snapshot.ltp if snapshot else 0.0


async def _best_index_ltp(app_symbol: str, *candidates: float | int | None) -> float:
    snapshot = await _latest_index_tick_snapshot(app_symbol, *candidates)
    return snapshot.ltp


async def _fetch_fyers_historical_rows(
    symbol: str,
    resolution: str,
    from_date: date,
    to_date: date,
) -> list[dict]:
    adapter = get_active_adapter("fyers")
    if adapter is None:
        if not await ensure_fyers_session():
            return []
        adapter = get_active_adapter("fyers")
    get_history = getattr(adapter, "get_historical_candles", None) if adapter else None
    if not callable(get_history):
        return []
    try:
        return await get_history(
            symbol,
            resolution,
            from_date.isoformat(),
            to_date.isoformat(),
        )
    except Exception:
        return []


async def _fetch_upstox_historical_rows(
    instrument_key: str,
    interval: str,
    from_date: date,
    to_date: date,
) -> list[dict]:
    token = get_broker_token("upstox")
    if not token:
        return []

    encoded_key = quote(instrument_key, safe="")
    url = (
        f"https://api.upstox.com/v2/historical-candle/"
        f"{encoded_key}/{interval}/{to_date.isoformat()}/{from_date.isoformat()}"
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
    if resp.status_code != 200:
        return []

    rows = []
    for candle in reversed(resp.json().get("data", {}).get("candles", [])):
        rows.append(
            {
                "time": str(candle[0]),
                "open": float(candle[1]),
                "high": float(candle[2]),
                "low": float(candle[3]),
                "close": float(candle[4]),
                "volume": int(candle[5] or 0),
            }
        )
    return rows


async def _fetch_market_historical_rows(
    *,
    broker_symbol: str,
    fyers_symbol: str,
    fyers_resolution: str,
    upstox_interval: str,
    from_date: date,
    to_date: date,
) -> tuple[list[dict], str]:
    fyers_rows = await _fetch_fyers_historical_rows(
        fyers_symbol,
        fyers_resolution,
        from_date,
        to_date,
    )
    if fyers_rows:
        return fyers_rows, "fyers"

    upstox_rows = await _fetch_upstox_historical_rows(
        broker_symbol,
        upstox_interval,
        from_date,
        to_date,
    )
    if upstox_rows:
        return upstox_rows, "upstox"

    return [], "none"


def _merge_rows(primary: list[dict], secondary: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for row in primary + secondary:
        key = str(row.get("time") or row.get("timestamp"))
        if key:
            merged[key] = row
    return [merged[key] for key in sorted(merged)]


@router.get("/option-chain/{symbol}")
async def get_option_chain(symbol: str, expiry: Optional[str] = Query(None)):
    """Get full option chain with PCR, max pain, greeks."""
    market_symbol = _resolve_market_symbol(symbol)
    local_expiries = await _local_option_expiries(market_symbol.app_symbol)
    expiry = expiry or (local_expiries[0] if local_expiries else None)
    if expiry:
        cached = await option_chain_service.get_cached(market_symbol.app_symbol, expiry)
        if cached and cached.get("entries"):
            return cached

    adapter, source = await _get_market_adapter()
    adapter_symbol = _market_symbol_for_adapter(adapter, market_symbol) if adapter else market_symbol.broker_symbol
    expiry = await _resolve_option_expiry(adapter, adapter_symbol, expiry) if adapter else expiry
    if not expiry:
        from agent.trading_agent import TradingAgent
        expiry = TradingAgent._next_expiry()

    cached = await option_chain_service.get_cached(market_symbol.app_symbol, expiry)
    if cached and cached.get("entries"):
        return cached

    if adapter:
        option_chain_service.set_broker(adapter)
        option_chain_service.track(market_symbol.app_symbol, expiry)
        await option_chain_service._refresh(market_symbol.app_symbol, expiry)
        cached = await option_chain_service.get_cached(market_symbol.app_symbol, expiry)
        if cached:
            return cached
    return {
        "symbol": market_symbol.app_symbol,
        "expiry": expiry,
        "entries": [],
        "source": source,
        "error": "No data available",
    }


@router.get("/atm-watchlist/expiries")
async def get_atm_watchlist_expiries(
    expiry: Optional[str] = Query(None),
    live_refresh: bool = Query(False),
):
    return await atm_watchlist_service.get_expiries(expiry, live_refresh=live_refresh)


@router.get("/atm-watchlist")
async def get_atm_watchlist(
    expiry: Optional[str] = Query(None),
    symbols: Optional[list[str]] = Query(None),
    live_refresh: bool = Query(False),
):
    return await atm_watchlist_service.get_watchlist(
        expiry,
        symbols=symbols,
        live_refresh=live_refresh,
    )


@router.get("/market-profile/{symbol}")
async def get_market_profile(
    symbol: str,
    timeframe: str = Query("day"),
):
    """Get Market Profile (POC, VAH, VAL, IB, TPO data)."""
    market_symbol = _resolve_market_symbol(symbol)
    normalized_timeframe = _normalize_profile_timeframe(timeframe)
    profile = await market_profile_builder.get_cached_profile(market_symbol.app_symbol, normalized_timeframe)
    if not profile:
        built = None
        source = "live"
        today = date.today()
        if normalized_timeframe == "hourly":
            built = market_profile_builder.build_hourly_profile(market_symbol.app_symbol)
        elif normalized_timeframe == "day":
            live_rows = market_profile_builder.get_tick_rows(market_symbol.app_symbol)
            rows = live_rows
            source_interval = "tick"
            if len(rows) < 50:
                rows, source = await _fetch_market_historical_rows(
                    broker_symbol=market_symbol.broker_symbol,
                    fyers_symbol=market_symbol.fyers_symbol,
                    fyers_resolution="1",
                    upstox_interval="1minute",
                    from_date=today - timedelta(days=5),
                    to_date=today,
                )
                source_interval = "1minute" if source == "upstox" else "1"
            built = market_profile_builder.build_profile_from_rows(
                market_symbol.app_symbol,
                rows,
                normalized_timeframe,
                source_interval,
            )
        elif normalized_timeframe == "week":
            historical_rows, source = await _fetch_market_historical_rows(
                broker_symbol=market_symbol.broker_symbol,
                fyers_symbol=market_symbol.fyers_symbol,
                fyers_resolution="1",
                upstox_interval="1minute",
                from_date=today - timedelta(days=9),
                to_date=today,
            )
            rows = market_profile_builder.aggregate_rows(historical_rows, 3)
            rows = _merge_rows(rows, market_profile_builder.get_three_minute_rows(market_symbol.app_symbol))
            built = market_profile_builder.build_profile_from_rows(
                market_symbol.app_symbol,
                rows,
                normalized_timeframe,
                "3minute" if source == "upstox" else "3minute",
            )
        elif normalized_timeframe == "month":
            historical_rows, source = await _fetch_market_historical_rows(
                broker_symbol=market_symbol.broker_symbol,
                fyers_symbol=market_symbol.fyers_symbol,
                fyers_resolution="30",
                upstox_interval="30minute",
                from_date=today - timedelta(days=40),
                to_date=today,
            )
            live_month_rows = market_profile_builder.aggregate_rows(
                market_profile_builder.get_three_minute_rows(market_symbol.app_symbol),
                30,
            )
            rows = _merge_rows(historical_rows, live_month_rows)
            built = market_profile_builder.build_profile_from_rows(
                market_symbol.app_symbol,
                rows,
                normalized_timeframe,
                "30minute",
            )
        if built:
            payload = asdict(built)
            payload["tpo_data"] = {str(k): v for k, v in built.tpo_data.items()}
            if "source" not in payload:
                payload["source"] = source
            await market_profile_builder.store_profile(built)
            return payload
        return {
            "symbol": market_symbol.app_symbol,
            "timeframe": normalized_timeframe,
            "error": "No market profile data. Waiting for live tick feed.",
        }
    return profile


@router.get("/iv-rank/{symbol}")
async def get_iv_rank(symbol: str):
    return await sector_tracker.get_iv_rank(symbol)


@router.get("/pcr/{symbol}")
async def get_pcr(symbol: str, expiry: Optional[str] = Query(None)):
    if not expiry:
        from agent.trading_agent import TradingAgent
        expiry = TradingAgent._next_expiry()
    app_symbol = to_app_symbol(symbol)
    chain = await option_chain_service.get_cached(app_symbol, expiry)
    if chain:
        return {
            "symbol": app_symbol,
            "pcr_oi": chain.get("pcr_oi", 1.0),
            "pcr_volume": chain.get("pcr_volume", 1.0),
            "total_ce_oi": chain.get("total_ce_oi", 0),
            "total_pe_oi": chain.get("total_pe_oi", 0),
        }
    return {"symbol": app_symbol, "pcr_oi": 1.0, "pcr_volume": 1.0}


class LTPRequest(BaseModel):
    symbols: List[str]


@router.post("/ltp")
async def get_ltp(req: LTPRequest):
    snapshots = await _resolve_latest_tick_snapshots(req.symbols)
    return {symbol: snapshot.ltp for symbol, snapshot in snapshots.items()}


async def _resolve_latest_tick_snapshots(symbols: list[str]) -> dict[str, LatestTickSnapshot]:
    market_symbols = [_resolve_market_symbol(symbol) for symbol in symbols]
    adapter, source = await _get_market_adapter()
    live: dict[str, float] = {}
    mapped: dict[str, str] = {}

    if not adapter:
        return {
            item.app_symbol: await _latest_index_tick_snapshot(
                item.app_symbol,
                data_router.get_ltp(item.app_symbol),
                source="data_router",
            )
            for item in market_symbols
        }

    try:
        mapped = {
            item.app_symbol: _market_symbol_for_adapter(adapter, item)
            for item in market_symbols
        }
        live = await asyncio.wait_for(
            adapter.get_ltp(list(mapped.values())),
            timeout=_LATEST_TICKS_LIVE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"[Market] Live latest tick lookup via {source} exceeded "
            f"{_LATEST_TICKS_LIVE_TIMEOUT_SECONDS:.1f}s; using local snapshots"
        )
        live = {}
    except Exception as exc:
        logger.trace(f"[Market] Live latest tick lookup failed via {source}: {exc}")
        live = {}

    snapshots: dict[str, LatestTickSnapshot] = {}
    for item in market_symbols:
        adapter_symbol = mapped.get(item.app_symbol, "")
        live_price = live.get(adapter_symbol, 0.0) if adapter_symbol else 0.0
        router_price = data_router.get_ltp(item.app_symbol)
        snapshots[item.app_symbol] = await _latest_index_tick_snapshot(
            item.app_symbol,
            live_price,
            router_price,
            source=source if _is_valid_index_price(item.app_symbol, live_price) else "data_router",
        )
    return snapshots


@router.post("/latest-ticks")
async def get_latest_ticks(req: LTPRequest) -> dict[str, LatestTickSnapshot]:
    return await _resolve_latest_tick_snapshots(req.symbols)


@router.get("/greeks/{symbol}/{strike}/{expiry}/{option_type}")
async def get_greeks(
    symbol: str,
    strike: float,
    expiry: str,
    option_type: str,
    spot: float = Query(...),
    iv: float = Query(0.20),
    r: float = Query(0.065),
):
    """Calculate Black-Scholes Greeks for a specific option."""
    from datetime import datetime
    try:
        expiry_dt = datetime.strptime(expiry, "%Y-%m-%d")
        T = max(0, (expiry_dt - datetime.utcnow()).days) / 365
    except ValueError:
        raise HTTPException(400, f"Invalid expiry format: {expiry}, use YYYY-MM-DD")

    greeks = bs_greeks(S=spot, K=strike, T=T, r=r, sigma=iv, option_type=option_type)
    iv_calc = implied_volatility(
        market_price=0,  # would pass LTP if known
        S=spot, K=strike, T=T, r=r, option_type=option_type
    )
    return {
        "symbol": symbol,
        "strike": strike,
        "expiry": expiry,
        "option_type": option_type,
        "spot": spot,
        "delta": greeks.delta,
        "gamma": greeks.gamma,
        "theta": greeks.theta,
        "vega": greeks.vega,
        "rho": greeks.rho,
        "iv": greeks.iv,
        "T_years": round(T, 4),
    }


@router.get("/expiries/{symbol}")
async def get_expiries(symbol: str):
    """Get available expiry dates for a symbol."""
    market_symbol = _resolve_market_symbol(symbol)
    local_expiries = await _local_option_expiries(market_symbol.app_symbol)
    if local_expiries:
        return {
            "symbol": market_symbol.app_symbol,
            "expiries": local_expiries,
            "default_expiry": local_expiries[0],
            "count": len(local_expiries),
            "source": "catalog",
        }
    adapter, source = await _get_market_adapter()
    if not adapter:
        return {
            "symbol": market_symbol.app_symbol,
            "expiries": local_expiries,
            "default_expiry": local_expiries[0] if local_expiries else None,
            "source": "catalog" if local_expiries else "none",
        }
    try:
        get_contracts = getattr(adapter, "get_option_contracts", None)
        if not callable(get_contracts):
            return {
                "symbol": market_symbol.app_symbol,
                "expiries": local_expiries,
                "default_expiry": local_expiries[0] if local_expiries else None,
                "source": "catalog" if local_expiries else source,
            }

        adapter_symbol = _market_symbol_for_adapter(adapter, market_symbol)
        contracts = await get_contracts(adapter_symbol)
        expiries = sorted({row.get("expiry") for row in contracts if row.get("expiry")})
        expiries = sorted({*expiries, *local_expiries})
        default_expiry = (
            await _resolve_option_expiry(
                adapter,
                adapter_symbol,
                None,
            )
            if expiries
            else None
        )
        if not default_expiry and expiries:
            default_expiry = expiries[0]
        return {
            "symbol": market_symbol.app_symbol,
            "expiries": expiries,
            "default_expiry": default_expiry,
            "count": len(expiries),
            "source": source,
        }
    except Exception:
        return {
            "symbol": market_symbol.app_symbol,
            "expiries": local_expiries,
            "default_expiry": local_expiries[0] if local_expiries else None,
            "source": "catalog" if local_expiries else source,
        }
