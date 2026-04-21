"""Central market-intelligence refresh and local-runtime reads for strategies."""
from __future__ import annotations

import asyncio
import csv
import gzip
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from loguru import logger
from sqlalchemy import text

from api.routers.auth import ensure_fyers_session, ensure_upstox_session, get_active_adapter
from core.config import settings
from db.database import AsyncSessionLocal
from market_data.option_chain import option_chain_service


UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
SESSION_OPEN = time(9, 15)
NSE_INDEX_SCOPE = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX")
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
        monthly_expiries = sorted(
            {
                str(expiry)
                for expiry in dict(expiry_payload.get("index_monthlies") or {}).values()
                if str(expiry or "").strip()
            }
        )
        full_refresh_due = self._last_full_watchlist_refresh_at is None or (
            now - self._last_full_watchlist_refresh_at
        ).total_seconds() >= max(int(settings.MARKET_INTELLIGENCE_FULL_WATCHLIST_REFRESH_MINUTES), 1) * 60
        watchlist_requests: list[tuple[str | None, list[str] | None]] = []
        if full_refresh_due:
            watchlist_requests.extend((expiry, None) for expiry in monthly_expiries)
        watchlist_requests.append((None, list(NSE_INDEX_SCOPE)))
        payloads = await asyncio.gather(
            *(
                atm_watchlist_service.get_watchlist(
                    expiry=expiry,
                    symbols=symbols,
                    live_refresh=True,
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
            "monthly_expiries": monthly_expiries,
            "full_refresh_due": full_refresh_due,
            "requests": results,
        }

    async def refresh_index_option_chains(self) -> dict[str, Any]:
        fyers_adapter = get_active_adapter("fyers")
        if fyers_adapter is None and await ensure_fyers_session(force_validate=True):
            fyers_adapter = get_active_adapter("fyers")
        upstox_adapter = get_active_adapter("upstox")
        if upstox_adapter is None:
            await ensure_upstox_session(force_validate=False)
            upstox_adapter = get_active_adapter("upstox")

        broker = fyers_adapter or upstox_adapter
        if broker is None:
            return {
                "status": "offline",
                "source": "none",
                "requests": [],
            }

        option_chain_service.set_broker(broker)
        requests = await self._load_chain_refresh_candidates()
        results: list[dict[str, Any]] = []
        for symbol_code, expiry_iso in requests:
            app_symbol = APP_SYMBOLS.get(symbol_code)
            if not app_symbol:
                continue
            try:
                await option_chain_service._refresh(app_symbol, expiry_iso)
                results.append(
                    {
                        "symbol_code": symbol_code,
                        "expiry": expiry_iso,
                        "status": "refreshed",
                    }
                )
            except Exception as exc:
                logger.warning(f"[MarketIntelligence] Option-chain refresh failed for {symbol_code} {expiry_iso}: {exc}")
                results.append(
                    {
                        "symbol_code": symbol_code,
                        "expiry": expiry_iso,
                        "status": "error",
                        "detail": str(exc),
                    }
                )
            await asyncio.sleep(0.1)

        self._last_chain_refresh_at = datetime.now(IST)
        return {
            "status": "ok",
            "source": getattr(broker, "broker_name", "unknown"),
            "requests": results,
        }

    async def refresh_nse_runtime(self) -> dict[str, Any]:
        spot_gap_fill = await self.gap_fill_spot_history(
            symbols=list(NSE_INDEX_SCOPE),
            lookback_days=max(int(settings.MARKET_INTELLIGENCE_GAP_FILL_LOOKBACK_DAYS), 1),
        )
        watchlists = await self.refresh_nse_watchlists()
        option_chains = await self.refresh_index_option_chains()
        return {
            "spot_gap_fill": spot_gap_fill,
            "watchlists": watchlists,
            "option_chains": option_chains,
        }

    async def get_strategy_health(self) -> dict[str, Any]:
        now = datetime.now(IST)
        today_start = datetime.combine(now.date(), SESSION_OPEN, tzinfo=IST).astimezone(UTC)
        async with AsyncSessionLocal() as session:
            watchlist_time = await session.scalar(
                text(
                    """
                    SELECT MAX(time)
                    FROM atm_option_watchlist_snapshots
                    WHERE time >= :today_start
                    """
                ),
                {"today_start": today_start},
            )
            watchlist_rows = await session.scalar(
                text(
                    """
                    SELECT COUNT(*)::int
                    FROM atm_option_watchlist_snapshots
                    WHERE time >= :today_start
                    """
                ),
                {"today_start": today_start},
            )
            spot_rows = await session.execute(
                text(
                    """
                    SELECT underlying, MAX(time) AS latest_time
                    FROM underlying_spot_candles
                    WHERE interval = '1minute'
                      AND underlying = ANY(:underlyings)
                      AND time >= :today_start
                    GROUP BY underlying
                    """
                ),
                {"underlyings": list(NSE_INDEX_SCOPE), "today_start": today_start},
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

        ready = bool(watchlist_rows) and (
            watchlist_age_seconds is None or watchlist_age_seconds <= 600
        )
        return {
            "ready": ready,
            "watchlist_rows_today": int(watchlist_rows or 0),
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


market_intelligence_runtime = MarketIntelligenceRuntime()
