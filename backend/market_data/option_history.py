"""Shared option-history access for watchlists and paper strategies."""
from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
import math
from typing import Any, Optional
from urllib.parse import quote

import httpx
from loguru import logger
from sqlalchemy import text

from api.routers.auth import ensure_fyers_session, ensure_upstox_session, get_active_adapter, get_broker_token
from db.database import AsyncSessionLocal


IST = timezone(timedelta(hours=5, minutes=30))


def _normalize_time(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _parse_time(value: Any) -> datetime:
    """Parse an ISO timestamp string (with or without timezone) to datetime."""
    if isinstance(value, datetime):
        return value
    s = str(value)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        # Fallback: strip trailing 'Z' and treat as UTC
        return datetime.fromisoformat(s.rstrip("Z")).replace(tzinfo=timezone.utc)


class OptionHistoryService:
    _UPSTOX_KEYS = ("NSE_FO|", "NSE_INDEX|", "BSE_FO|", "BSE_INDEX|")

    def __init__(self) -> None:
        self.reset_health()

    def reset_health(self) -> None:
        self._health: dict[str, Any] = {
            "events": [],
            "failure_count": 0,
            "success_count": 0,
            "brokers": {},
        }

    def get_health_snapshot(self) -> dict[str, Any]:
        return {
            "events": [dict(event) for event in self._health.get("events", [])],
            "failure_count": int(self._health.get("failure_count", 0)),
            "success_count": int(self._health.get("success_count", 0)),
            "brokers": {
                broker: dict(values)
                for broker, values in dict(self._health.get("brokers", {})).items()
            },
        }

    def _record_health(
        self,
        *,
        broker: str,
        instrument_key: str,
        interval: str,
        status: str,
        detail: str,
        rows: int = 0,
    ) -> None:
        broker_state = self._health.setdefault("brokers", {}).setdefault(
            broker,
            {"success": 0, "failure": 0, "last_status": None, "last_detail": None},
        )
        event = {
            "time": datetime.now(IST).isoformat(),
            "broker": broker,
            "instrument_key": instrument_key,
            "interval": interval,
            "status": status,
            "detail": detail,
            "rows": rows,
        }
        events = self._health.setdefault("events", [])
        events.append(event)
        if len(events) > 25:
            del events[:-25]
        if status == "success":
            self._health["success_count"] = int(self._health.get("success_count", 0)) + 1
            broker_state["success"] = int(broker_state.get("success", 0)) + 1
        else:
            self._health["failure_count"] = int(self._health.get("failure_count", 0)) + 1
            broker_state["failure"] = int(broker_state.get("failure", 0)) + 1
        broker_state["last_status"] = status
        broker_state["last_detail"] = detail

    @staticmethod
    def _upstox_interval(interval: str) -> str:
        mapping = {
            "1minute": "1minute",
            "5minute": "5minute",
            "15minute": "15minute",
            "30minute": "30minute",
            "1day": "day",
        }
        return mapping.get(str(interval or "30minute"), "30minute")

    @staticmethod
    def _fyers_resolution(interval: str) -> str:
        mapping = {
            "1minute": "1",
            "5minute": "5",
            "15minute": "15",
            "30minute": "30",
            "1day": "D",
        }
        return mapping.get(str(interval or "30minute"), "30")

    @classmethod
    def _is_upstox_key(cls, instrument_key: str) -> bool:
        return bool(instrument_key) and instrument_key.startswith(cls._UPSTOX_KEYS)

    @classmethod
    def _needs_upstox_minute_fallback(cls, instrument_key: str, interval: str) -> bool:
        return cls._is_upstox_key(instrument_key) and interval in {"5minute", "15minute"}

    @staticmethod
    def _broker_lookback_days(interval: str, *, limit: int) -> int:
        normalized = str(interval or "30minute")
        if normalized == "1minute":
            return 5
        if normalized in {"5minute", "15minute"}:
            return 5
        if normalized == "30minute":
            estimated_days = max(3, math.ceil(max(limit, 1) / 13) + 7)
            return min(max(estimated_days, 14), 90)
        if normalized == "1day":
            estimated_days = max(30, limit * 2)
            return min(estimated_days, 365)
        return 30

    @staticmethod
    def _today_ist_date() -> date:
        return datetime.now(IST).date()

    @classmethod
    def _is_intraday_interval(cls, interval: str) -> bool:
        return str(interval or "30minute") in {"1minute", "5minute", "15minute", "30minute"}

    @classmethod
    def _range_includes_current_ist_day(cls, from_date: date, to_date: date) -> bool:
        today = cls._today_ist_date()
        return from_date <= today <= to_date

    @classmethod
    def _latest_row_is_stale_for_today(cls, rows: list[dict[str, Any]], interval: str) -> bool:
        if not rows or not cls._is_intraday_interval(interval):
            return False
        latest_seen: Optional[datetime] = None
        for row in rows:
            raw_time = row.get("time")
            if not raw_time:
                continue
            try:
                parsed = _parse_time(raw_time).astimezone(IST)
            except Exception:
                continue
            if latest_seen is None or parsed > latest_seen:
                latest_seen = parsed
        if latest_seen is None:
            return False
        return latest_seen.date() < cls._today_ist_date()

    @staticmethod
    def _aggregate_rows(rows: list[dict[str, Any]], interval_minutes: int) -> list[dict[str, Any]]:
        if not rows or interval_minutes <= 1:
            return rows

        aggregated: list[dict[str, Any]] = []
        bucket_start: Optional[datetime] = None
        bucket: Optional[dict[str, Any]] = None

        for row in sorted(rows, key=lambda item: str(item.get("time") or "")):
            ts = _parse_time(row.get("time"))
            current_start = ts.replace(
                minute=(ts.minute // interval_minutes) * interval_minutes,
                second=0,
                microsecond=0,
            )

            if bucket_start != current_start:
                if bucket is not None:
                    aggregated.append(bucket)
                bucket_start = current_start
                bucket = {
                    "time": current_start.isoformat(),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "volume": int(row.get("volume") or 0),
                    "oi": row.get("oi"),
                    "iv": row.get("iv"),
                    "delta": row.get("delta"),
                    "gamma": row.get("gamma"),
                    "theta": row.get("theta"),
                    "vega": row.get("vega"),
                    "underlying_price": row.get("underlying_price"),
                }
                continue

            if bucket is None:
                continue

            high = row.get("high")
            low = row.get("low")
            close = row.get("close")
            if high is not None:
                bucket["high"] = max(float(bucket["high"]), float(high)) if bucket.get("high") is not None else float(high)
            if low is not None:
                bucket["low"] = min(float(bucket["low"]), float(low)) if bucket.get("low") is not None else float(low)
            if close is not None:
                bucket["close"] = close
            bucket["volume"] = int(bucket.get("volume", 0)) + int(row.get("volume") or 0)
            for field in ("oi", "iv", "delta", "gamma", "theta", "vega", "underlying_price"):
                if row.get(field) is not None:
                    bucket[field] = row.get(field)

        if bucket is not None:
            aggregated.append(bucket)
        return aggregated

    async def _fetch_upstox_rows(
        self,
        *,
        instrument_key: str,
        interval: str,
        token: str,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        intraday: bool = False,
    ) -> list[dict[str, Any]]:
        encoded_key = quote(instrument_key, safe="")
        if intraday:
            url = (
                "https://api.upstox.com/v2/historical-candle/intraday/"
                f"{encoded_key}/{self._upstox_interval(interval)}"
            )
        else:
            if from_date is None or to_date is None:
                return []
            url = (
                "https://api.upstox.com/v2/historical-candle/"
                f"{encoded_key}/{self._upstox_interval(interval)}/{to_date.isoformat()}/{from_date.isoformat()}"
            )

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                )
            if response.status_code != 200:
                self._record_health(
                    broker="upstox",
                    instrument_key=instrument_key,
                    interval=interval,
                    status="failure",
                    detail=f"historical API returned HTTP {response.status_code}",
                )
                logger.warning(
                    f"[OptionHistory] Upstox historical fetch failed for {instrument_key} {interval}: "
                    f"HTTP {response.status_code}"
                )
                return []
            payload = response.json()
        except Exception as exc:
            self._record_health(
                broker="upstox",
                instrument_key=instrument_key,
                interval=interval,
                status="failure",
                detail=str(exc),
            )
            logger.warning(
                f"[OptionHistory] Upstox historical fetch failed for {instrument_key} {interval}: {exc}"
            )
            return []

        rows: list[dict[str, Any]] = []
        for candle in reversed(payload.get("data", {}).get("candles", [])):
            rows.append(
                {
                    "time": str(candle[0]),
                    "open": float(candle[1]),
                    "high": float(candle[2]),
                    "low": float(candle[3]),
                    "close": float(candle[4]),
                    "volume": int(candle[5] or 0),
                    "oi": int(candle[6] or 0) if len(candle) > 6 and candle[6] is not None else None,
                    "iv": None,
                    "delta": None,
                    "gamma": None,
                    "theta": None,
                    "vega": None,
                    "underlying_price": None,
                }
            )
        self._record_health(
            broker="upstox",
            instrument_key=instrument_key,
            interval=interval,
            status="success",
            detail=f"loaded {len(rows)} rows",
            rows=len(rows),
        )
        return rows

    async def _fetch_broker_candles(
        self,
        *,
        instrument_key: str,
        from_date: date,
        to_date: date,
        interval: str,
    ) -> list[dict[str, Any]]:
        if not instrument_key:
            return []

        if self._needs_upstox_minute_fallback(instrument_key, interval):
            return []

        if self._is_upstox_key(instrument_key):
            token = get_broker_token("upstox")
            if not token and not await ensure_upstox_session(force_validate=True):
                self._record_health(
                    broker="upstox",
                    instrument_key=instrument_key,
                    interval=interval,
                    status="failure",
                    detail="No valid Upstox session is available.",
                )
                return []
            token = token or get_broker_token("upstox")
            if not token:
                self._record_health(
                    broker="upstox",
                    instrument_key=instrument_key,
                    interval=interval,
                    status="failure",
                    detail="Upstox access token is missing after restore.",
                )
                return []
            rows_by_time: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
            today = self._today_ist_date()

            if self._is_intraday_interval(interval) and self._range_includes_current_ist_day(from_date, to_date):
                historical_to = min(to_date, today - timedelta(days=1))
                if from_date <= historical_to:
                    for row in await self._fetch_upstox_rows(
                        instrument_key=instrument_key,
                        interval=interval,
                        token=token,
                        from_date=from_date,
                        to_date=historical_to,
                    ):
                        rows_by_time[_normalize_time(row.get("time"))] = row

                intraday_from = max(from_date, today)
                for row in await self._fetch_upstox_rows(
                    instrument_key=instrument_key,
                    interval=interval,
                    token=token,
                    intraday=True,
                ):
                    try:
                        row_time = _parse_time(row.get("time")).astimezone(IST)
                    except Exception:
                        continue
                    if intraday_from <= row_time.date() <= to_date:
                        rows_by_time[_normalize_time(row.get("time"))] = row
                return list(rows_by_time.values())

            return await self._fetch_upstox_rows(
                instrument_key=instrument_key,
                interval=interval,
                token=token,
                from_date=from_date,
                to_date=to_date,
            )

        adapter = get_active_adapter("fyers")
        if adapter is None and await ensure_fyers_session(force_validate=True):
            adapter = get_active_adapter("fyers")
        get_history = getattr(adapter, "get_historical_candles", None) if adapter else None
        if not callable(get_history):
            self._record_health(
                broker="fyers",
                instrument_key=instrument_key,
                interval=interval,
                status="failure",
                detail="No valid Fyers session is available.",
            )
            return []
        try:
            rows = await get_history(
                instrument_key,
                self._fyers_resolution(interval),
                from_date.isoformat(),
                to_date.isoformat(),
            )
        except Exception as exc:
            self._record_health(
                broker="fyers",
                instrument_key=instrument_key,
                interval=interval,
                status="failure",
                detail=str(exc),
            )
            logger.warning(
                f"[OptionHistory] Fyers historical fetch failed for {instrument_key} {interval}: {exc}"
            )
            return []
        self._record_health(
            broker="fyers",
            instrument_key=instrument_key,
            interval=interval,
            status="success",
            detail=f"loaded {len(rows)} rows",
            rows=len(rows),
        )
        return [
            {
                "time": str(row.get("time")),
                "open": float(row.get("open")) if row.get("open") is not None else None,
                "high": float(row.get("high")) if row.get("high") is not None else None,
                "low": float(row.get("low")) if row.get("low") is not None else None,
                "close": float(row.get("close")) if row.get("close") is not None else None,
                "volume": int(row.get("volume") or 0),
                "oi": None,
                "iv": None,
                "delta": None,
                "gamma": None,
                "theta": None,
                "vega": None,
                "underlying_price": None,
            }
            for row in rows
        ]

    async def load_candles(
        self,
        *,
        underlying: str,
        expiry: date,
        strike: float,
        option_type: str,
        instrument_key: Optional[str] = None,
        interval: str = "30minute",
        limit: int = 80,
    ) -> list[dict[str, Any]]:
        async with AsyncSessionLocal() as session:
            merged: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

            async def load_rows(query: str, params: dict[str, Any]) -> None:
                result = await session.execute(text(query), params)
                for row in reversed(result.fetchall()):
                    time_key = _normalize_time(row.time)
                    merged[time_key] = {
                        "time": time_key,
                        "open": float(row.open) if row.open is not None else None,
                        "high": float(row.high) if row.high is not None else None,
                        "low": float(row.low) if row.low is not None else None,
                        "close": float(row.close) if row.close is not None else None,
                        "volume": int(row.volume) if row.volume is not None else None,
                        "oi": int(row.oi) if row.oi is not None else None,
                        "iv": float(row.iv) if row.iv is not None else None,
                        "delta": float(row.delta) if row.delta is not None else None,
                        "gamma": float(row.gamma) if row.gamma is not None else None,
                        "theta": float(row.theta) if row.theta is not None else None,
                        "vega": float(row.vega) if row.vega is not None else None,
                        "underlying_price": float(row.underlying_price)
                        if row.underlying_price is not None
                        else None,
                    }

            if instrument_key:
                await load_rows(
                    """
                    SELECT
                        time, open, high, low, close, volume, oi, iv, delta, gamma, theta, vega, underlying_price
                    FROM option_premium_candles
                    WHERE instrument_key = :instrument_key
                      AND interval = :interval
                    ORDER BY time DESC
                    LIMIT :limit
                    """,
                    {
                        "instrument_key": instrument_key,
                        "interval": interval,
                        "limit": limit,
                    },
                )

            if len(merged) < limit:
                await load_rows(
                    """
                    SELECT
                        time, open, high, low, close, volume, oi, iv, delta, gamma, theta, vega, underlying_price
                    FROM option_premium_candles
                    WHERE underlying = :underlying
                      AND expiry = :expiry
                      AND strike = :strike
                      AND option_type = :option_type
                      AND interval = :interval
                    ORDER BY time DESC
                    LIMIT :limit
                    """,
                    {
                        "underlying": underlying,
                        "expiry": expiry,
                        "strike": strike,
                        "option_type": option_type,
                        "interval": interval,
                        "limit": limit,
                    },
                )

        needs_live_refresh = instrument_key and self._latest_row_is_stale_for_today(list(merged.values()), interval)
        if instrument_key and (len(merged) < 35 or needs_live_refresh):
            # Fetch back 90 days regardless of expiry month — ensures weekly
            # contracts (listed only 1-2 weeks before expiry) still get enough
            # history to warm up the MACD signal line (needs ≥34 bars).
            to_date = self._today_ist_date()
            direct_lookback_days = self._broker_lookback_days(interval, limit=limit)
            fetch_from = to_date - timedelta(days=direct_lookback_days)
            broker_rows: list[dict[str, Any]] = []

            if not self._needs_upstox_minute_fallback(instrument_key, interval):
                broker_rows = await self._fetch_broker_candles(
                    instrument_key=instrument_key,
                    from_date=fetch_from,
                    to_date=to_date,
                    interval=interval,
                )

            if not broker_rows and interval in {"5minute", "15minute"}:
                minute_fetch_from = to_date - timedelta(
                    days=self._broker_lookback_days("1minute", limit=limit * (5 if interval == "5minute" else 15))
                )
                minute_rows = await self._fetch_broker_candles(
                    instrument_key=instrument_key,
                    from_date=minute_fetch_from,
                    to_date=to_date,
                    interval="1minute",
                )
                if minute_rows:
                    broker_rows = self._aggregate_rows(
                        minute_rows,
                        5 if interval == "5minute" else 15,
                    )
            if broker_rows:
                # Persist new rows to DB so subsequent calls skip the API
                await self._persist_broker_candles(
                    rows=broker_rows,
                    underlying=underlying,
                    expiry=expiry,
                    strike=strike,
                    option_type=option_type,
                    instrument_key=instrument_key,
                    interval=interval,
                    already_in_db=set(merged.keys()),
                )
            for row in broker_rows:
                time_key = _normalize_time(row.get("time"))
                if time_key:
                    merged[time_key] = row

        candles = list(merged.values())
        candles.sort(key=lambda row: row["time"])
        return candles[-limit:]

    async def _persist_broker_candles(
        self,
        *,
        rows: list[dict[str, Any]],
        underlying: str,
        expiry: date,
        strike: float,
        option_type: str,
        instrument_key: str,
        interval: str,
        already_in_db: set[str],
    ) -> None:
        """Upsert broker-fetched candles into option_premium_candles."""
        new_rows = [
            r for r in rows
            if _normalize_time(r.get("time")) not in already_in_db
            and r.get("close") is not None
        ]
        if not new_rows:
            return
        async with AsyncSessionLocal() as session:
            for r in new_rows:
                await session.execute(
                    text(
                        """
                        INSERT INTO option_premium_candles (
                            time, underlying, market, expiry, strike, option_type,
                            open, high, low, close, volume, oi, iv,
                            delta, gamma, theta, vega, underlying_price,
                            instrument_key, trading_symbol, interval, source, synced_at
                        ) VALUES (
                            :time, :underlying, 'NSE', :expiry, :strike, :option_type,
                            :open, :high, :low, :close, :volume, :oi, :iv,
                            :delta, :gamma, :theta, :vega, :underlying_price,
                            :instrument_key, :trading_symbol, :interval, 'upstox', now()
                        )
                        ON CONFLICT (instrument_key, interval, time) DO NOTHING
                        """
                    ),
                    {
                        "time": _parse_time(r.get("time")),
                        "underlying": underlying,
                        "expiry": expiry,
                        "strike": strike,
                        "option_type": option_type,
                        "open": r.get("open"),
                        "high": r.get("high"),
                        "low": r.get("low"),
                        "close": r.get("close"),
                        "volume": r.get("volume", 0),
                        "oi": r.get("oi"),
                        "iv": r.get("iv"),
                        "delta": r.get("delta"),
                        "gamma": r.get("gamma"),
                        "theta": r.get("theta"),
                        "vega": r.get("vega"),
                        "underlying_price": r.get("underlying_price"),
                        "instrument_key": instrument_key,
                        "trading_symbol": None,
                        "interval": interval,
                    },
                )
            await session.commit()

    async def load_closes(self, **kwargs: Any) -> list[float]:
        candles = await self.load_candles(**kwargs)
        return [float(row["close"]) for row in candles if row.get("close") is not None]

    async def resolve_lot_size(
        self,
        *,
        underlying: str,
        expiry: date,
        strike: float,
        option_type: str,
        instrument_key: Optional[str] = None,
    ) -> Optional[int]:
        """Return NSE-mandated lot size for this contract.

        Resolution order:
        1. fo_contract_catalog by instrument_key (exact match, most specific)
        2. fo_contract_catalog by underlying/expiry/strike/option_type
        3. fo_underlying_catalog.lot_size (per-underlying default, populated from broker)
        4. None  →  caller uses PaperPortfolio.DEFAULT_LOT_SIZE
        """
        async with AsyncSessionLocal() as session:
            # 1. Exact instrument_key lookup
            if instrument_key:
                result = await session.execute(
                    text(
                        """
                        SELECT lot_size
                        FROM fo_contract_catalog
                        WHERE instrument_key = :instrument_key
                        LIMIT 1
                        """
                    ),
                    {"instrument_key": instrument_key},
                )
                row = result.first()
                if row and row.lot_size:
                    return int(row.lot_size)

            # 2. Underlying / expiry / strike / option_type lookup
            result = await session.execute(
                text(
                    """
                    SELECT lot_size
                    FROM fo_contract_catalog
                    WHERE underlying = :underlying
                      AND expiry = :expiry
                      AND strike = :strike
                      AND option_type = :option_type
                    ORDER BY last_synced_at DESC NULLS LAST, updated_at DESC
                    LIMIT 1
                    """
                ),
                {
                    "underlying": underlying,
                    "expiry": expiry,
                    "strike": strike,
                    "option_type": option_type,
                },
            )
            row = result.first()
            if row and row.lot_size:
                return int(row.lot_size)

            # 3. Per-underlying default from fo_underlying_catalog (broker-sourced)
            result = await session.execute(
                text(
                    "SELECT lot_size FROM fo_underlying_catalog WHERE symbol = :sym LIMIT 1"
                ),
                {"sym": underlying},
            )
            row = result.first()
            return int(row.lot_size) if row and row.lot_size else None


option_history_service = OptionHistoryService()
