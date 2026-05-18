"""Central market-intelligence refresh and local-runtime reads for strategies."""
from __future__ import annotations

import asyncio
import csv
import gzip
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from loguru import logger
from sqlalchemy import text

from api.routers.auth import ensure_fyers_session, ensure_upstox_session, get_active_adapter
from db.redis_client import get_redis
from core.config import settings
from db.database import AsyncSessionLocal
from market_data.option_chain import OC_TTL, option_chain_service
from market_data.symbols import to_broker_symbol, to_fyers_symbol
from macro_research import macro_research_service
from sector_interaction.india_live import india_live_sector_service


UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
SESSION_OPEN = time(9, 15)
NSE_INDEX_SCOPE = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX")
STRATEGY_LIVE_WATCHLIST_MAX_AGE_SECONDS = 10 * 60
STRATEGY_EXECUTION_MAX_WATCHLIST_AGE_SECONDS = 36 * 60 * 60
STRATEGY_MIN_LATEST_UNDERLYINGS = 50
APP_SYMBOLS = {
    "NIFTY": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:BANKNIFTY-INDEX",
    "FINNIFTY": "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
    "SENSEX": "BSE:SENSEX-INDEX",
}


def _runtime_root() -> Path:
    return Path(__file__).resolve().parents[1] / "runtime" / "index_analytics_data"


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _strategy_readiness_fields(
    *,
    watchlist_rows_today: int,
    watchlist_rows_latest: int,
    watchlist_age_seconds: Optional[float],
) -> dict[str, Any]:
    today_ready = (
        bool(watchlist_rows_today)
        and watchlist_age_seconds is not None
        and watchlist_age_seconds <= STRATEGY_LIVE_WATCHLIST_MAX_AGE_SECONDS
    )
    latest_session_ready = watchlist_rows_latest >= STRATEGY_MIN_LATEST_UNDERLYINGS
    latest_session_execution_ready = (
        latest_session_ready
        and watchlist_age_seconds is not None
        and watchlist_age_seconds <= STRATEGY_EXECUTION_MAX_WATCHLIST_AGE_SECONDS
    )
    readiness_mode = "live" if today_ready else "latest_session" if latest_session_ready else "missing"
    if today_ready:
        execution_mode = "live"
    elif latest_session_execution_ready:
        execution_mode = "latest_session"
    elif latest_session_ready and watchlist_age_seconds is not None:
        execution_mode = "stale_latest_session"
    elif latest_session_ready:
        execution_mode = "catalog_only"
    else:
        execution_mode = "missing"
    return {
        "ready": today_ready or latest_session_ready,
        "execution_ready": today_ready or latest_session_execution_ready,
        "readiness_mode": readiness_mode,
        "execution_mode": execution_mode,
        "latest_session_ready": latest_session_ready,
        "max_live_age_seconds": STRATEGY_LIVE_WATCHLIST_MAX_AGE_SECONDS,
        "max_execution_age_seconds": STRATEGY_EXECUTION_MAX_WATCHLIST_AGE_SECONDS,
    }


class MarketIntelligenceRuntime:
    def __init__(self) -> None:
        self._last_full_watchlist_refresh_at: datetime | None = None
        self._last_chain_refresh_at: datetime | None = None

    async def load_local_spot_rows(
        self,
        symbol_code: str,
        *,
        lookback_days: int = 10,
    ) -> tuple[list[dict[str, Any]], str, str]:
        from_time = datetime.combine(
            date.today() - timedelta(days=max(int(lookback_days), 1)),
            time.min,
            tzinfo=IST,
        ).astimezone(UTC)
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    SELECT time, open, high, low, close, volume
                    FROM underlying_spot_candles
                    WHERE underlying = :underlying
                      AND interval = '1minute'
                      AND time >= :from_time
                    ORDER BY time ASC
                    """
                ),
                {"underlying": symbol_code.upper(), "from_time": from_time},
            )
            rows = result.mappings().all()

        payload = [
            {
                "time": _parse_time(row["time"]).isoformat(),
                "open": float(row["open"] or row["close"] or 0.0),
                "high": float(row["high"] or row["close"] or 0.0),
                "low": float(row["low"] or row["close"] or 0.0),
                "close": float(row["close"] or 0.0),
                "volume": float(row["volume"] or 0.0),
            }
            for row in rows
        ]
        if payload:
            return payload, "timescaledb_spot_1minute", symbol_code.upper()

        csv_path = _runtime_root() / "spot" / f"underlying={symbol_code.upper()}" / "1minute.csv.gz"
        if not csv_path.exists():
            return [], "none", symbol_code.upper()

        cutoff_date = (datetime.now(IST) - timedelta(days=max(int(lookback_days), 1))).date()
        local_rows: list[dict[str, Any]] = []
        with gzip.open(csv_path, "rt", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                try:
                    timestamp = _parse_time(row["time"]).astimezone(IST)
                except Exception:
                    continue
                if timestamp.date() < cutoff_date:
                    continue
                local_rows.append(
                    {
                        "time": timestamp.astimezone(UTC).isoformat(),
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row.get("volume") or 0.0),
                    }
                )
        return local_rows, "local_csv_spot", csv_path.name

    async def gap_fill_spot_history(
        self,
        *,
        symbols: Optional[list[str]] = None,
        lookback_days: int = 10,
    ) -> dict[str, Any]:
        from auction_intelligence.live import _fetch_recent_minute_rows

        requested = [str(symbol).upper() for symbol in (symbols or list(NSE_INDEX_SCOPE))]
        results: list[dict[str, Any]] = []
        stored_total = 0

        for symbol in requested:
            try:
                rows, source, history_symbol = await _fetch_recent_minute_rows(
                    symbol,
                    lookback_days=lookback_days,
                    allow_live_broker_refresh=True,
                )
                stored = await self._upsert_spot_rows(
                    symbol_code=symbol,
                    history_symbol=history_symbol,
                    rows=rows,
                    source=source,
                )
                stored_total += stored
                results.append(
                    {
                        "symbol_code": symbol,
                        "source": source,
                        "history_symbol": history_symbol,
                        "rows_seen": len(rows),
                        "rows_stored": stored,
                    }
                )
            except Exception as exc:
                logger.warning(f"[MarketIntelligence] Spot gap-fill failed for {symbol}: {exc}")
                results.append(
                    {
                        "symbol_code": symbol,
                        "source": "error",
                        "error": str(exc),
                        "rows_seen": 0,
                        "rows_stored": 0,
                    }
                )
            await asyncio.sleep(0.1)

        return {
            "symbols_requested": requested,
            "stored_total": stored_total,
            "results": results,
        }

    async def refresh_nse_watchlists(self) -> dict[str, Any]:
        from market_data.atm_watchlist import atm_watchlist_service

        now = datetime.now(IST)
        expiry_payload = await atm_watchlist_service.get_expiries(None, live_refresh=True)
        stock_monthly_expiry = str(expiry_payload.get("stock_monthly_expiry") or "").strip()
        index_expiries = sorted(
            {
                str(expiry)
                for expiry in dict(expiry_payload.get("index_monthlies") or {}).values()
                if str(expiry or "").strip()
            }
        )
        full_refresh_due = self._last_full_watchlist_refresh_at is None or (
            now - self._last_full_watchlist_refresh_at
        ).total_seconds() >= max(int(settings.MARKET_INTELLIGENCE_FULL_WATCHLIST_REFRESH_MINUTES), 1) * 60

        # DB-state override: if today's STOCK coverage in
        # atm_option_watchlist_snapshots is below the last-session count, the
        # in-memory timer is lying (stale Redis cache, prior crash, container
        # restart, etc.). Force a full refresh until the stock universe
        # actually populates for today. Without this, S1 keeps scanning 215
        # rows where 208 are Friday's snapshots and MACD never crosses.
        if not full_refresh_due:
            try:
                today_start = datetime.combine(now.date(), time.min, tzinfo=IST).astimezone(UTC)
                async with AsyncSessionLocal() as session:
                    stock_today = await session.scalar(
                        text(
                            """
                            SELECT COUNT(DISTINCT underlying)
                            FROM atm_option_watchlist_snapshots
                            WHERE kind = 'STOCK'
                              AND time >= :today_start
                            """
                        ),
                        {"today_start": today_start},
                    )
                    stock_latest = await session.scalar(
                        text(
                            """
                            SELECT COUNT(DISTINCT underlying)
                            FROM atm_option_watchlist_snapshots
                            WHERE kind = 'STOCK'
                            """
                        ),
                    )
                stock_today = int(stock_today or 0)
                stock_latest = int(stock_latest or 0)
                if stock_latest > 0 and stock_today < int(stock_latest * 0.5):
                    logger.info(
                        "[MarketIntelligence] forcing full watchlist refresh: "
                        "stocks today={today} vs latest_session={latest}",
                        today=stock_today,
                        latest=stock_latest,
                    )
                    full_refresh_due = True
                    self._last_full_watchlist_refresh_at = None
            except Exception as exc:
                logger.warning(
                    "[MarketIntelligence] stock freshness probe failed: {}", exc
                )
        watchlist_requests: list[tuple[str | None, list[str] | None]] = []
        if full_refresh_due:
            full_universe_expiry = stock_monthly_expiry or str(expiry_payload.get("monthly_expiry") or "").strip()
            if full_universe_expiry:
                watchlist_requests.append((full_universe_expiry, None))
                # Bust the shared-universe Redis cache so the live_refresh
                # rebuild actually iterates the stock universe instead of
                # returning the cached blob. Without this, a stale "ready"
                # payload satisfies _watchlist_rows_are_fresh() and the BG
                # build for stocks never fires.
                try:
                    redis = await get_redis()
                    cache_keys = [
                        f"atm_watchlist:v12:live:{full_universe_expiry}:all",
                        f"atm_watchlist:partial:v12:live:{full_universe_expiry}:all",
                    ]
                    for key in cache_keys:
                        await redis.delete(key)
                except Exception as exc:
                    logger.warning(
                        "[MarketIntelligence] failed to invalidate full-universe cache: {}",
                        exc,
                    )
        for expiry in index_expiries:
            watchlist_requests.append((expiry, list(NSE_INDEX_SCOPE)))
        if not index_expiries:
            watchlist_requests.append((None, list(NSE_INDEX_SCOPE)))
        payloads = await asyncio.gather(
            *(
                atm_watchlist_service.get_watchlist(
                    expiry=expiry,
                    symbols=symbols,
                    live_refresh=True,
                    # Force a true rebuild for the full-universe call so the
                    # stale-row filter at line 864 actually runs. Index-scope
                    # calls keep the cache fast path.
                    force_rebuild=(symbols is None),
                )
                for expiry, symbols in watchlist_requests
            ),
            return_exceptions=True,
        )

        results: list[dict[str, Any]] = []
        for (expiry, symbols), payload in zip(watchlist_requests, payloads):
            if isinstance(payload, Exception):
                results.append(
                    {
                        "expiry": expiry,
                        "symbols": symbols or [],
                        "status": "error",
                        "detail": str(payload),
                        "rows": 0,
                    }
                )
                continue
            results.append(
                {
                    "expiry": expiry,
                    "symbols": symbols or [],
                    "status": str(payload.get("build_status") or "ready"),
                    "detail": payload.get("detail"),
                    "rows": int((payload.get("summary") or {}).get("total_rows") or 0),
                }
            )

        if full_refresh_due and any(item.get("symbols") == [] and item.get("status") in {"ready", "building"} for item in results):
            self._last_full_watchlist_refresh_at = now

        return {
            "monthly_expiries": index_expiries,
            "stock_monthly_expiry": stock_monthly_expiry or None,
            "full_refresh_due": full_refresh_due,
            "requests": results,
        }

    async def refresh_index_option_chains(self) -> dict[str, Any]:
        now = datetime.now(IST)
        cooldown_seconds = max(int(settings.MARKET_INTELLIGENCE_REFRESH_INTERVAL_SECONDS), 30)
        if self._last_chain_refresh_at is not None:
            elapsed = (now - self._last_chain_refresh_at).total_seconds()
            if elapsed < cooldown_seconds:
                return {
                    "status": "cooldown",
                    "source": "cached",
                    "requests": [],
                    "last_refresh_at": self._last_chain_refresh_at.isoformat(),
                    "cooldown_seconds": cooldown_seconds,
                }

        fyers_adapter = get_active_adapter("fyers")
        if fyers_adapter is None and await ensure_fyers_session(force_validate=False):
            fyers_adapter = get_active_adapter("fyers")
        upstox_adapter = get_active_adapter("upstox")
        if upstox_adapter is None and await ensure_upstox_session(force_validate=False):
            upstox_adapter = get_active_adapter("upstox")

        if fyers_adapter is None and upstox_adapter is None:
            return {
                "status": "offline",
                "source": "none",
                "requests": [],
            }

        requests = await self._load_chain_refresh_candidates()
        results: list[dict[str, Any]] = []
        for symbol_code, expiry_iso in requests:
            result = await self._refresh_cached_index_option_chain(
                symbol_code,
                expiry_iso,
                upstox_adapter=upstox_adapter,
                fyers_adapter=fyers_adapter,
            )
            if result.get("status") == "error":
                logger.warning(
                    "[MarketIntelligence] Option-chain refresh failed for "
                    f"{symbol_code} {expiry_iso}: {result.get('detail')}"
                )
            results.append(result)
            await asyncio.sleep(0.1)

        self._last_chain_refresh_at = datetime.now(IST)
        return {
            "status": "ok",
            "source": "+".join(
                [
                    name
                    for name, adapter in (("upstox", upstox_adapter), ("fyers", fyers_adapter))
                    if adapter is not None
                ]
            ) or "none",
            "last_refresh_at": self._last_chain_refresh_at.isoformat(),
            "requests": results,
        }

    async def refresh_nse_runtime(self) -> dict[str, Any]:
        spot_gap_fill = await self.gap_fill_spot_history(
            symbols=list(NSE_INDEX_SCOPE),
            lookback_days=max(int(settings.MARKET_INTELLIGENCE_GAP_FILL_LOOKBACK_DAYS), 1),
        )
        watchlists = await self.refresh_nse_watchlists()
        option_chains = await self.refresh_index_option_chains()
        try:
            sector_interaction = await india_live_sector_service.market_intelligence_payload()
        except Exception as exc:
            logger.warning(f"[MarketIntelligence] Sector interaction refresh failed: {exc}")
            sector_interaction = {
                "module": "sector_interaction",
                "source_mode": "error",
                "error": str(exc),
            }
        try:
            macro_research = await macro_research_service.overview(refresh=False)
        except Exception as exc:
            logger.warning(f"[MarketIntelligence] Macro research refresh failed: {exc}")
            macro_research = {
                "module": "macro_research",
                "source_mode": "error",
                "error": str(exc),
            }
        return {
            "spot_gap_fill": spot_gap_fill,
            "watchlists": watchlists,
            "option_chains": option_chains,
            "sector_interaction": sector_interaction,
            "macro_research": macro_research,
        }

    async def get_strategy_health(self) -> dict[str, Any]:
        now = datetime.now(IST)
        today_start = datetime.combine(now.date(), time.min, tzinfo=IST).astimezone(UTC)
        tomorrow_start = today_start + timedelta(days=1)
        async with AsyncSessionLocal() as session:
            watchlist_time = await session.scalar(
                text(
                    """
                    SELECT MAX(time)
                    FROM atm_option_watchlist_snapshots
                    WHERE expiry >= CURRENT_DATE
                    """
                ),
            )
            if watchlist_time is None:
                watchlist_time = await session.scalar(
                    text("SELECT MAX(time) FROM atm_option_watchlist_snapshots")
                )

            today_stats = await session.execute(
                text(
                    """
                    SELECT
                        COUNT(DISTINCT underlying)::INT AS underlyings,
                        COUNT(DISTINCT underlying) FILTER (WHERE option_type = 'CE' AND ltp IS NOT NULL)::INT AS ce_ready,
                        COUNT(DISTINCT underlying) FILTER (WHERE option_type = 'PE' AND ltp IS NOT NULL)::INT AS pe_ready,
                        COUNT(DISTINCT underlying) FILTER (WHERE kind = 'STOCK')::INT AS stocks,
                        COUNT(DISTINCT underlying) FILTER (WHERE kind = 'INDEX')::INT AS indices
                    FROM atm_option_watchlist_snapshots
                    WHERE time >= :today_start
                      AND time < :tomorrow_start
                      AND expiry >= CURRENT_DATE
                    """
                ),
                {"today_start": today_start, "tomorrow_start": tomorrow_start},
            )
            today_row = today_stats.mappings().first() or {}

            latest_row: dict[str, Any] = {}
            latest_session_start: datetime | None = None
            latest_session_end: datetime | None = None
            if watchlist_time is not None:
                latest_ist = _parse_time(watchlist_time).astimezone(IST)
                latest_session_start = datetime.combine(latest_ist.date(), time.min, tzinfo=IST).astimezone(UTC)
                latest_session_end = latest_session_start + timedelta(days=1)
                latest_stats = await session.execute(
                    text(
                        """
                        SELECT
                            COUNT(DISTINCT underlying)::INT AS underlyings,
                            COUNT(DISTINCT underlying) FILTER (WHERE option_type = 'CE' AND ltp IS NOT NULL)::INT AS ce_ready,
                            COUNT(DISTINCT underlying) FILTER (WHERE option_type = 'PE' AND ltp IS NOT NULL)::INT AS pe_ready,
                            COUNT(DISTINCT underlying) FILTER (WHERE kind = 'STOCK')::INT AS stocks,
                            COUNT(DISTINCT underlying) FILTER (WHERE kind = 'INDEX')::INT AS indices
                        FROM atm_option_watchlist_snapshots
                        WHERE time >= :session_start
                          AND time < :session_end
                          AND expiry >= CURRENT_DATE
                        """
                    ),
                    {"session_start": latest_session_start, "session_end": latest_session_end},
                )
                latest_row = dict(latest_stats.mappings().first() or {})

            premium_row: dict[str, Any] = {}
            if int(latest_row.get("underlyings") or 0) < STRATEGY_MIN_LATEST_UNDERLYINGS:
                premium_stats = await session.execute(
                    text(
                        """
                        WITH latest AS (
                            SELECT DISTINCT ON (underlying, option_type)
                                time,
                                underlying,
                                option_type,
                                close AS ltp
                            FROM option_premium_candles
                            WHERE expiry >= CURRENT_DATE
                              AND option_type IN ('CE', 'PE')
                              AND close IS NOT NULL
                            ORDER BY underlying, option_type, time DESC
                        )
                        SELECT
                            MAX(time) AS latest_time,
                            COUNT(DISTINCT underlying)::INT AS underlyings,
                            COUNT(DISTINCT underlying) FILTER (WHERE option_type = 'CE' AND ltp IS NOT NULL)::INT AS ce_ready,
                            COUNT(DISTINCT underlying) FILTER (WHERE option_type = 'PE' AND ltp IS NOT NULL)::INT AS pe_ready
                        FROM latest
                        """
                    )
                )
                premium_row = dict(premium_stats.mappings().first() or {})
                premium_time = premium_row.get("latest_time")
                premium_is_newer = (
                    premium_time is not None
                    and (
                        watchlist_time is None
                        or _parse_time(premium_time) > _parse_time(watchlist_time)
                    )
                )
                if (
                    int(premium_row.get("underlyings") or 0) > int(latest_row.get("underlyings") or 0)
                    and (watchlist_time is None or premium_is_newer)
                ):
                    latest_row = {
                        **latest_row,
                        **premium_row,
                        "stocks": max(int(latest_row.get("stocks") or 0), int(premium_row.get("underlyings") or 0) - 7),
                        "indices": max(int(latest_row.get("indices") or 0), 7),
                    }
                    if premium_row.get("latest_time") is not None:
                        watchlist_time = premium_row.get("latest_time")
                        latest_ist = _parse_time(watchlist_time).astimezone(IST)
                        latest_session_start = datetime.combine(latest_ist.date(), time.min, tzinfo=IST).astimezone(UTC)
                        latest_session_end = latest_session_start + timedelta(days=1)

            if int(latest_row.get("underlyings") or 0) < STRATEGY_MIN_LATEST_UNDERLYINGS:
                catalog_stats = await session.execute(
                    text(
                        """
                        WITH eligible AS (
                            SELECT DISTINCT catalog.underlying, catalog.option_type
                            FROM fo_contract_catalog catalog
                            WHERE catalog.expiry >= CURRENT_DATE
                              AND catalog.option_type IN ('CE', 'PE')
                              AND catalog.instrument_key IS NOT NULL
                        )
                        SELECT
                            COUNT(DISTINCT underlying)::INT AS underlyings,
                            COUNT(DISTINCT underlying) FILTER (WHERE option_type = 'CE')::INT AS ce_ready,
                            COUNT(DISTINCT underlying) FILTER (WHERE option_type = 'PE')::INT AS pe_ready
                        FROM eligible
                        """
                    )
                )
                catalog_row = dict(catalog_stats.mappings().first() or {})
                if int(catalog_row.get("underlyings") or 0) > int(latest_row.get("underlyings") or 0):
                    latest_row = {
                        **latest_row,
                        **catalog_row,
                        "stocks": max(int(latest_row.get("stocks") or 0), int(catalog_row.get("underlyings") or 0) - 7),
                        "indices": max(int(latest_row.get("indices") or 0), 7),
                    }

            spot_rows = await session.execute(
                text(
                    """
                    SELECT underlying, MAX(time) AS latest_time
                    FROM underlying_spot_candles
                    WHERE interval = '1minute'
                      AND time >= NOW() - INTERVAL '10 days'
                    GROUP BY underlying
                    """
                ),
            )
            per_symbol = {
                str(row.underlying): _parse_time(row.latest_time).isoformat()
                for row in spot_rows.fetchall()
                if row.latest_time is not None
            }

        latest_watchlist_iso = _parse_time(watchlist_time).isoformat() if watchlist_time is not None else None
        watchlist_age_seconds: Optional[float] = None
        if watchlist_time is not None:
            watchlist_age_seconds = max(
                0.0,
                (datetime.now(UTC) - _parse_time(watchlist_time)).total_seconds(),
            )

        watchlist_rows_today = int(today_row.get("underlyings") or 0)
        watchlist_rows_latest = int(latest_row.get("underlyings") or 0)
        readiness = _strategy_readiness_fields(
            watchlist_rows_today=watchlist_rows_today,
            watchlist_rows_latest=watchlist_rows_latest,
            watchlist_age_seconds=watchlist_age_seconds,
        )
        return {
            **readiness,
            "watchlist_rows_today": watchlist_rows_today,
            "watchlist_rows_latest": watchlist_rows_latest,
            "latest_ce_ready": int(latest_row.get("ce_ready") or 0),
            "latest_pe_ready": int(latest_row.get("pe_ready") or 0),
            "latest_stock_underlyings": int(latest_row.get("stocks") or 0),
            "latest_index_underlyings": int(latest_row.get("indices") or 0),
            "today_ce_ready": int(today_row.get("ce_ready") or 0),
            "today_pe_ready": int(today_row.get("pe_ready") or 0),
            "latest_watchlist_session": (
                latest_session_start.astimezone(IST).date().isoformat()
                if latest_session_start is not None
                else None
            ),
            "latest_watchlist_time": latest_watchlist_iso,
            "watchlist_age_seconds": watchlist_age_seconds,
            "latest_spot_rows": per_symbol,
        }

    async def _load_chain_refresh_candidates(self) -> list[tuple[str, str]]:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    SELECT underlying, expiry
                    FROM (
                        SELECT
                            underlying,
                            expiry,
                            ROW_NUMBER() OVER (
                                PARTITION BY underlying
                                ORDER BY expiry ASC
                            ) AS rn
                        FROM (
                            SELECT DISTINCT underlying, expiry
                            FROM fo_contract_catalog
                            WHERE underlying = ANY(:underlyings)
                              AND expiry >= CURRENT_DATE
                        ) ladder
                    ) ranked
                    WHERE rn <= 3
                    ORDER BY underlying ASC, expiry ASC
                    """
                ),
                {"underlyings": list(NSE_INDEX_SCOPE)},
            )
            rows = result.fetchall()
        return [
            (str(row.underlying), row.expiry.isoformat())
            for row in rows
            if getattr(row, "expiry", None) is not None
        ]

    async def _upsert_spot_rows(
        self,
        *,
        symbol_code: str,
        history_symbol: str,
        rows: list[dict[str, Any]],
        source: str,
    ) -> int:
        payload = []
        for row in rows:
            try:
                timestamp = _parse_time(row["time"])
            except Exception:
                continue
            payload.append(
                {
                    "time": timestamp,
                    "instrument_key": history_symbol,
                    "underlying": symbol_code.upper(),
                    "interval": "1minute",
                    "open": float(row.get("open", row.get("close") or 0.0) or 0.0),
                    "high": float(row.get("high", row.get("close") or 0.0) or 0.0),
                    "low": float(row.get("low", row.get("close") or 0.0) or 0.0),
                    "close": float(row.get("close") or 0.0),
                    "volume": int(float(row.get("volume") or 0.0)),
                    "oi": int(float(row.get("oi") or 0.0)),
                    "source": source,
                }
            )
        if not payload:
            return 0

        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO underlying_spot_candles (
                        time, instrument_key, underlying, interval, open, high,
                        low, close, volume, oi, source, synced_at
                    ) VALUES (
                        :time, :instrument_key, :underlying, :interval, :open, :high,
                        :low, :close, :volume, :oi, :source, NOW()
                    )
                    ON CONFLICT (instrument_key, interval, time) DO UPDATE
                    SET open = EXCLUDED.open,
                        high = EXCLUDED.high,
                        low = EXCLUDED.low,
                        close = EXCLUDED.close,
                        volume = EXCLUDED.volume,
                        oi = EXCLUDED.oi,
                        source = EXCLUDED.source,
                        synced_at = NOW()
                    """
                ),
                payload,
            )
            await session.commit()
        return len(payload)

    async def _refresh_cached_index_option_chain(
        self,
        symbol_code: str,
        expiry_iso: str,
        *,
        upstox_adapter: Any | None,
        fyers_adapter: Any | None,
    ) -> dict[str, Any]:
        app_symbol = APP_SYMBOLS.get(symbol_code)
        if not app_symbol:
            return {
                "symbol_code": symbol_code,
                "expiry": expiry_iso,
                "status": "skipped",
                "detail": "unknown app symbol",
            }

        errors: list[str] = []
        for source, adapter, lookup_symbol in (
            ("upstox", upstox_adapter, to_broker_symbol(app_symbol)),
            ("fyers", fyers_adapter, to_fyers_symbol(app_symbol)),
        ):
            if adapter is None:
                continue
            try:
                chain = await adapter.get_option_chain(lookup_symbol, expiry_iso)
            except Exception as exc:
                errors.append(f"{source}: {exc}")
                continue
            if not getattr(chain, "entries", None):
                errors.append(f"{source}: empty chain")
                continue
            await self._cache_option_chain_payload(
                app_symbol=app_symbol,
                expiry_iso=expiry_iso,
                chain=chain,
                source=source,
            )
            return {
                "symbol_code": symbol_code,
                "expiry": expiry_iso,
                "status": "refreshed",
                "source": source,
            }

        return {
            "symbol_code": symbol_code,
            "expiry": expiry_iso,
            "status": "error",
            "detail": " | ".join(errors) if errors else "no broker available",
        }

    async def _cache_option_chain_payload(
        self,
        *,
        app_symbol: str,
        expiry_iso: str,
        chain: Any,
        source: str,
    ) -> None:
        analytics = option_chain_service._calculate_analytics(chain)
        payload = {
            "symbol": app_symbol,
            "expiry": expiry_iso,
            "spot_price": float(chain.spot_price or 0.0),
            "timestamp": datetime.now(UTC).isoformat(),
            "source": source,
            "entries": [
                {
                    "strike": entry.strike,
                    "option_type": entry.option_type,
                    "ltp": entry.ltp,
                    "oi": entry.oi,
                    "volume": entry.volume,
                    "bid": entry.bid,
                    "ask": entry.ask,
                    "iv": entry.iv,
                    "delta": entry.delta,
                    "gamma": entry.gamma,
                    "theta": entry.theta,
                    "vega": entry.vega,
                    "prev_oi": entry.prev_oi,
                    "prev_close": entry.prev_close,
                    "oi_change": round(float(entry.oi) - float(entry.prev_oi or 0.0), 2),
                    "oi_change_pct": round(
                        ((float(entry.oi) - float(entry.prev_oi or 0.0)) / float(entry.prev_oi or 1.0)) * 100.0,
                        2,
                    ) if entry.prev_oi else None,
                    "ltp_change": round(float(entry.ltp) - float(entry.prev_close or 0.0), 2),
                    "ltp_change_pct": round(
                        ((float(entry.ltp) - float(entry.prev_close or 0.0)) / float(entry.prev_close or 1.0)) * 100.0,
                        2,
                    ) if entry.prev_close else None,
                    "instrument_key": entry.instrument_key,
                }
                for entry in chain.entries
            ],
            **analytics,
        }
        redis = await get_redis()
        await redis.set(f"oc:{app_symbol}:{expiry_iso}", json.dumps(payload), ex=OC_TTL)


market_intelligence_runtime = MarketIntelligenceRuntime()
