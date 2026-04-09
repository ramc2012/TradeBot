"""Shared option-history access for watchlists and paper strategies."""
from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote

import httpx
from sqlalchemy import text

from api.routers.auth import ensure_fyers_session, ensure_upstox_session, get_active_adapter, get_broker_token
from db.database import AsyncSessionLocal


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
    async def _fetch_broker_candles(
        self,
        *,
        instrument_key: str,
        from_date: date,
        to_date: date,
    ) -> list[dict[str, Any]]:
        if not instrument_key:
            return []

        if instrument_key.startswith(("NSE_FO|", "NSE_INDEX|", "BSE_FO|", "BSE_INDEX|")):
            token = get_broker_token("upstox")
            if not token and not await ensure_upstox_session():
                return []
            token = token or get_broker_token("upstox")
            if not token:
                return []
            encoded_key = quote(instrument_key, safe="")
            url = (
                "https://api.upstox.com/v2/historical-candle/"
                f"{encoded_key}/30minute/{to_date.isoformat()}/{from_date.isoformat()}"
            )
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                )
            if response.status_code != 200:
                return []
            rows = []
            for candle in reversed(response.json().get("data", {}).get("candles", [])):
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
            return rows

        adapter = get_active_adapter("fyers")
        if adapter is None and await ensure_fyers_session():
            adapter = get_active_adapter("fyers")
        get_history = getattr(adapter, "get_historical_candles", None) if adapter else None
        if not callable(get_history):
            return []
        try:
            rows = await get_history(
                instrument_key,
                "30",
                from_date.isoformat(),
                to_date.isoformat(),
            )
        except Exception:
            return []
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

        if len(merged) < 35 and instrument_key:
            # Fetch back 90 days regardless of expiry month — ensures weekly
            # contracts (listed only 1-2 weeks before expiry) still get enough
            # history to warm up the MACD signal line (needs ≥34 bars).
            fetch_from = date.today() - timedelta(days=90)
            broker_rows = await self._fetch_broker_candles(
                instrument_key=instrument_key,
                from_date=fetch_from,
                to_date=date.today(),
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
