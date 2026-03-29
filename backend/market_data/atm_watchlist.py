"""ATM CE/PE watchlist builder with live metrics and lightweight persistence."""
from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from loguru import logger
from sqlalchemy import text

from analytics.technicals import latest_macd_rsi
from analysis.instruments import get_monthly_expiry
from api.routers.auth import ensure_fyers_session, ensure_upstox_session, get_active_adapter
from brokers.base import BrokerAdapter, OptionChain, OptionChainEntry
from db.database import AsyncSessionLocal
from db.redis_client import get_redis
from market_data.option_history import option_history_service


UTC = timezone.utc
DEFAULT_WATCHLIST_TTL = 45
DEFAULT_EXPIRY_TTL = 300

INDEX_FYERS_SYMBOLS = {
    "NIFTY": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
    "FINNIFTY": "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
    "NIFTYNXT50": "NSE:NIFTYNXT50-INDEX",
}


@dataclass(frozen=True)
class UnderlyingMeta:
    symbol: str
    kind: str
    spot_instrument_key: str
    underlying_key: str


class ATMWatchlistService:
    """Build an all-F&O ATM call/put watchlist using live chain data."""

    async def get_expiries(self) -> dict[str, Any]:
        redis = await get_redis()
        cache_key = "atm_watchlist:expiries:v2"
        cached = await redis.get(cache_key)
        if cached:
            return json.loads(cached)

        fyers_adapter = get_active_adapter("fyers")
        if fyers_adapter is None and await ensure_fyers_session():
            fyers_adapter = get_active_adapter("fyers")
        upstox_adapter = await self._get_upstox_adapter()
        adapter = fyers_adapter or upstox_adapter
        if adapter is None:
            payload = {
                "expiries": [],
                "default_expiry": None,
                "source": "none",
                "detail": "Connect Fyers or Upstox to resolve watchlist expiries.",
            }
            await redis.set(cache_key, json.dumps(payload), ex=60)
            return payload

        underlyings = await self._load_underlyings()
        representative = [
            row for row in underlyings
            if row.symbol in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "TCS"}
        ]
        if not representative:
            representative = underlyings[:10]

        async def fetch_expiries(meta: UnderlyingMeta) -> list[str]:
            try:
                lookup_symbol = self._to_fyers_symbol(meta) if getattr(adapter, "broker_name", "") == "fyers" else meta.underlying_key
                contracts = await adapter.get_option_contracts(lookup_symbol)
            except Exception as exc:
                logger.debug(f"[ATM watchlist] Expiry discovery failed for {meta.symbol}: {exc}")
                return []
            return sorted({str(row.get("expiry")) for row in contracts if row.get("expiry")})

        expiry_lists = await asyncio.gather(*(fetch_expiries(meta) for meta in representative))
        expiries = sorted({expiry for items in expiry_lists for expiry in items if expiry})
        today = date.today().isoformat()
        monthly_expiry = get_monthly_expiry(date.today().year, date.today().month)
        if date.today() > monthly_expiry:
            next_month = date.today().replace(day=28) + timedelta(days=4)
            monthly_expiry = get_monthly_expiry(next_month.year, next_month.month)
        monthly_expiry_iso = monthly_expiry.isoformat()
        default_expiry = (
            monthly_expiry_iso
            if monthly_expiry_iso in expiries
            else next((expiry for expiry in expiries if expiry >= today), expiries[0] if expiries else None)
        )
        payload = {
            "expiries": expiries,
            "default_expiry": default_expiry,
            "source": "fyers" if getattr(adapter, "broker_name", "") == "fyers" else "upstox",
            "detail": None if getattr(adapter, "broker_name", "") == "fyers" else "Fyers is not connected, so expiries are resolved through Upstox.",
        }
        await redis.set(cache_key, json.dumps(payload), ex=DEFAULT_EXPIRY_TTL)
        return payload

    async def get_watchlist(self, expiry: Optional[str] = None) -> dict[str, Any]:
        expiry_payload = await self.get_expiries()
        selected_expiry = expiry or expiry_payload.get("default_expiry")
        selected_expiry_date = self._parse_expiry(selected_expiry)
        if not selected_expiry or selected_expiry_date is None:
            return {
                "expiry": None,
                "rows": [],
                "summary": {"total_rows": 0, "ce_ready": 0, "pe_ready": 0},
                "source": "none",
                "detail": expiry_payload.get("detail") or "No expiry is available for the ATM watchlist.",
                "timestamp": datetime.now(UTC).isoformat(),
            }

        redis = await get_redis()
        cache_key = f"atm_watchlist:v2:{selected_expiry}"
        cached = await redis.get(cache_key)
        if cached:
            return json.loads(cached)

        fyers_adapter = get_active_adapter("fyers")
        if fyers_adapter is None and await ensure_fyers_session():
            fyers_adapter = get_active_adapter("fyers")
        upstox_adapter = await self._get_upstox_adapter()
        if upstox_adapter is None and fyers_adapter is None:
            payload = {
                "expiry": selected_expiry,
                "rows": [],
                "summary": {"total_rows": 0, "ce_ready": 0, "pe_ready": 0},
                "source": "none",
                "detail": "Connect Fyers or Upstox to build the ATM watchlist.",
                "timestamp": datetime.now(UTC).isoformat(),
            }
            await redis.set(cache_key, json.dumps(payload), ex=30)
            return payload

        underlyings = await self._load_underlyings()
        semaphore = asyncio.Semaphore(8)

        async def build(meta: UnderlyingMeta) -> Optional[dict[str, Any]]:
            async with semaphore:
                try:
                    return await self._build_row(
                        meta,
                        selected_expiry,
                        selected_expiry_date,
                        upstox_adapter,
                        fyers_adapter,
                    )
                except Exception as exc:
                    logger.warning(f"[ATM watchlist] Failed to build {meta.symbol}: {exc}")
                    return None

        rows = [row for row in await asyncio.gather(*(build(meta) for meta in underlyings)) if row]
        rows.sort(key=lambda row: (row["kind"] != "INDEX", row["underlying"]))

        await self._archive_expired_contracts()
        payload = {
            "expiry": selected_expiry,
            "rows": rows,
            "summary": {
                "total_rows": len(rows),
                "ce_ready": sum(1 for row in rows if row.get("ce")),
                "pe_ready": sum(1 for row in rows if row.get("pe")),
                "fyers_rows": sum(1 for row in rows if row.get("live_source") == "fyers"),
                "upstox_rows": sum(1 for row in rows if row.get("live_source") == "upstox"),
            },
            "source": "fyers" if fyers_adapter else "upstox",
            "detail": None if fyers_adapter else "Fyers is not connected, so the watchlist is using Upstox live chain data.",
            "timestamp": datetime.now(UTC).isoformat(),
        }
        await redis.set(cache_key, json.dumps(payload), ex=DEFAULT_WATCHLIST_TTL)
        return payload

    async def _build_row(
        self,
        meta: UnderlyingMeta,
        expiry: str,
        expiry_date: date,
        upstox_adapter: Optional[BrokerAdapter],
        fyers_adapter: Optional[BrokerAdapter],
    ) -> Optional[dict[str, Any]]:
        contracts = await self._get_contracts_for_expiry(meta, expiry, upstox_adapter) if upstox_adapter else []

        chain: Optional[OptionChain] = None
        live_source = "upstox"
        fyers_symbol = self._to_fyers_symbol(meta)
        if fyers_adapter:
            try:
                chain = await fyers_adapter.get_option_chain(fyers_symbol, expiry)
                if chain.entries:
                    live_source = "fyers"
            except Exception as exc:
                logger.debug(f"[ATM watchlist] Fyers chain failed for {meta.symbol}: {exc}")

        if chain is None or not chain.entries:
            if upstox_adapter is None:
                return None
            try:
                chain = await upstox_adapter.get_option_chain(meta.underlying_key, expiry)
                live_source = "upstox"
            except Exception as exc:
                logger.debug(f"[ATM watchlist] Upstox chain failed for {meta.symbol}: {exc}")
                return None

        if not chain.entries:
            return None

        spot_price = float(chain.spot_price or 0.0)
        strikes = sorted({float(entry.strike) for entry in chain.entries})
        if not strikes:
            return None
        atm_strike = min(strikes, key=lambda strike: abs(strike - spot_price))
        ce_entry = next((entry for entry in chain.entries if entry.option_type == "CE" and float(entry.strike) == atm_strike), None)
        pe_entry = next((entry for entry in chain.entries if entry.option_type == "PE" and float(entry.strike) == atm_strike), None)
        if not ce_entry and not pe_entry:
            return None

        contract_map = {
            (float(contract["strike_price"]), str(contract["instrument_type"])): contract
            for contract in contracts
        }
        ce_contract = contract_map.get((atm_strike, "CE"))
        pe_contract = contract_map.get((atm_strike, "PE"))

        ce_payload = await self._build_option_payload(
            meta,
            expiry,
            expiry_date,
            spot_price,
            atm_strike,
            ce_entry,
            ce_contract,
            live_source,
        )
        pe_payload = await self._build_option_payload(
            meta,
            expiry,
            expiry_date,
            spot_price,
            atm_strike,
            pe_entry,
            pe_contract,
            live_source,
        )
        return {
            "underlying": meta.symbol,
            "kind": meta.kind,
            "spot_price": round(spot_price, 2),
            "expiry": expiry,
            "atm_strike": atm_strike,
            "live_source": live_source,
            "fyers_symbol": fyers_symbol,
            "ce": ce_payload,
            "pe": pe_payload,
        }

    async def _build_option_payload(
        self,
        meta: UnderlyingMeta,
        expiry: str,
        expiry_date: date,
        spot_price: float,
        strike: float,
        entry: Optional[OptionChainEntry],
        contract: Optional[dict[str, Any]],
        source_broker: str,
    ) -> Optional[dict[str, Any]]:
        if entry is None:
            return None

        catalog_instrument_key = str((contract or {}).get("instrument_key") or "").strip() or None
        live_instrument_key = str(entry.instrument_key or "").strip() or None
        instrument_key = (
            live_instrument_key
            if source_broker == "fyers" and live_instrument_key
            else catalog_instrument_key or live_instrument_key
        )
        trading_symbol = str((contract or {}).get("trading_symbol") or "").strip() or None
        technicals = await self._load_technicals(
            underlying=meta.symbol,
            expiry=expiry_date,
            strike=strike,
            option_type=entry.option_type,
            instrument_key=instrument_key,
            fallback_close=float(entry.ltp or 0.0),
        )
        payload = {
            "strike": strike,
            "option_type": entry.option_type,
            "instrument_key": instrument_key,
            "trading_symbol": trading_symbol,
            "ltp": round(float(entry.ltp or 0.0), 2),
            "prev_close": round(float(entry.prev_close or 0.0), 2) if entry.prev_close is not None else None,
            "change": round(float(entry.ltp or 0.0) - float(entry.prev_close or 0.0), 2)
            if entry.prev_close is not None
            else None,
            "change_pct": round(
                ((float(entry.ltp or 0.0) - float(entry.prev_close or 0.0)) / float(entry.prev_close or 1.0)) * 100.0,
                2,
            ) if entry.prev_close not in (None, 0) else None,
            "oi": int(entry.oi or 0),
            "prev_oi": int(entry.prev_oi or 0) if entry.prev_oi is not None else None,
            "oi_change": int((entry.oi or 0) - int(entry.prev_oi or 0)) if entry.prev_oi is not None else None,
            "oi_change_pct": round(
                (((entry.oi or 0) - int(entry.prev_oi or 0)) / float(entry.prev_oi or 1.0)) * 100.0,
                2,
            ) if entry.prev_oi not in (None, 0) else None,
            "volume": int(entry.volume or 0),
            "iv": round(float(entry.iv or 0.0), 4) if entry.iv is not None else None,
            "delta": round(float(entry.delta), 4) if entry.delta is not None else None,
            "gamma": round(float(entry.gamma), 6) if entry.gamma is not None else None,
            "theta": round(float(entry.theta), 4) if entry.theta is not None else None,
            "vega": round(float(entry.vega), 4) if entry.vega is not None else None,
            **technicals,
        }
        await self._persist_snapshot(
            meta=meta,
            expiry=expiry_date,
            strike=strike,
            spot_price=spot_price,
            option=payload,
            source_broker=source_broker,
        )
        return payload

    async def _get_contracts_for_expiry(
        self,
        meta: UnderlyingMeta,
        expiry: str,
        upstox_adapter: Optional[BrokerAdapter],
    ) -> list[dict[str, Any]]:
        if upstox_adapter is None:
            return []
        redis = await get_redis()
        cache_key = f"atm_watchlist:contracts:{meta.symbol}:{expiry}"
        cached = await redis.get(cache_key)
        if cached:
            return json.loads(cached)

        try:
            contracts = await upstox_adapter.get_option_contracts(meta.underlying_key, expiry)
        except Exception as exc:
            logger.debug(f"[ATM watchlist] Contract discovery failed for {meta.symbol}: {exc}")
            return []

        normalized = [
            {
                "instrument_key": row.get("instrument_key"),
                "trading_symbol": row.get("trading_symbol"),
                "strike_price": float(row.get("strike_price", 0) or 0.0),
                "instrument_type": row.get("instrument_type"),
                "expiry": row.get("expiry"),
            }
            for row in contracts
            if row.get("instrument_key") and row.get("instrument_type") in {"CE", "PE"}
        ]
        await redis.set(cache_key, json.dumps(normalized), ex=DEFAULT_EXPIRY_TTL)
        return normalized

    async def _load_technicals(
        self,
        *,
        underlying: str,
        expiry: date,
        strike: float,
        option_type: str,
        instrument_key: Optional[str],
        fallback_close: float,
    ) -> dict[str, Any]:
        closes = await self._load_history_closes(
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            instrument_key=instrument_key,
        )
        if not closes and fallback_close > 0:
            closes = [fallback_close]
        return latest_macd_rsi(closes)

    async def _load_history_closes(
        self,
        *,
        underlying: str,
        expiry: date,
        strike: float,
        option_type: str,
        instrument_key: Optional[str],
    ) -> list[float]:
        premium_closes = await option_history_service.load_closes(
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            instrument_key=instrument_key,
            interval="30minute",
            limit=80,
        )
        if premium_closes:
            return premium_closes

        async with AsyncSessionLocal() as session:
            snapshot_rows = await session.execute(
                text("""
                    SELECT ltp
                    FROM atm_option_watchlist_snapshots
                    WHERE underlying = :underlying
                      AND expiry = :expiry
                      AND strike = :strike
                      AND option_type = :option_type
                    ORDER BY time DESC
                    LIMIT 60
                """),
                {
                    "underlying": underlying,
                    "expiry": expiry,
                    "strike": strike,
                    "option_type": option_type,
                },
            )
            return [float(row.ltp) for row in reversed(snapshot_rows.fetchall()) if row.ltp is not None][-60:]

    async def _persist_snapshot(
        self,
        *,
        meta: UnderlyingMeta,
        expiry: date,
        strike: float,
        spot_price: float,
        option: dict[str, Any],
        source_broker: str,
    ) -> None:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO atm_option_watchlist_snapshots (
                        time, underlying, kind, expiry, strike, option_type, source_broker,
                        instrument_key, trading_symbol, underlying_price, ltp, prev_close,
                        change, change_pct, oi, prev_oi, oi_change, oi_change_pct,
                        volume, iv, macd, macd_signal, macd_histogram, rsi
                    )
                    VALUES (
                        NOW(), :underlying, :kind, :expiry, :strike, :option_type, :source_broker,
                        :instrument_key, :trading_symbol, :underlying_price, :ltp, :prev_close,
                        :change, :change_pct, :oi, :prev_oi, :oi_change, :oi_change_pct,
                        :volume, :iv, :macd, :macd_signal, :macd_histogram, :rsi
                    )
                """),
                {
                    "underlying": meta.symbol,
                    "kind": meta.kind,
                    "expiry": expiry,
                    "strike": strike,
                    "option_type": option["option_type"],
                    "source_broker": source_broker,
                    "instrument_key": option.get("instrument_key"),
                    "trading_symbol": option.get("trading_symbol"),
                    "underlying_price": spot_price,
                    "ltp": option.get("ltp"),
                    "prev_close": option.get("prev_close"),
                    "change": option.get("change"),
                    "change_pct": option.get("change_pct"),
                    "oi": option.get("oi"),
                    "prev_oi": option.get("prev_oi"),
                    "oi_change": option.get("oi_change"),
                    "oi_change_pct": option.get("oi_change_pct"),
                    "volume": option.get("volume"),
                    "iv": option.get("iv"),
                    "macd": option.get("macd"),
                    "macd_signal": option.get("macd_signal"),
                    "macd_histogram": option.get("macd_histogram"),
                    "rsi": option.get("rsi"),
                },
            )
            await session.commit()

    async def _archive_expired_contracts(self) -> None:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    WITH expired_snapshots AS (
                        SELECT
                            COALESCE(NULLIF(instrument_key, ''), CONCAT(underlying, ':', expiry::text, ':', strike::text, ':', option_type)) AS archive_key,
                            *
                        FROM atm_option_watchlist_snapshots
                        WHERE expiry < CURRENT_DATE
                    ),
                    latest AS (
                        SELECT DISTINCT ON (archive_key)
                            archive_key,
                            underlying,
                            kind,
                            expiry,
                            strike,
                            option_type,
                            source_broker,
                            trading_symbol,
                            time AS last_seen_at,
                            underlying_price AS last_underlying_price,
                            ltp AS last_ltp,
                            change_pct AS last_change_pct,
                            oi AS last_oi,
                            oi_change AS last_oi_change,
                            volume AS last_volume,
                            iv AS last_iv,
                            macd AS last_macd,
                            macd_signal AS last_macd_signal,
                            macd_histogram AS last_macd_histogram,
                            rsi AS last_rsi
                        FROM expired_snapshots
                        ORDER BY archive_key, time DESC
                    ),
                    summary AS (
                        SELECT
                            archive_key,
                            MIN(time) AS first_seen_at,
                            MAX(time) AS last_seen_at,
                            COUNT(*)::INT AS snapshot_count
                        FROM expired_snapshots
                        GROUP BY archive_key
                    )
                    INSERT INTO expired_option_contract_archive (
                        instrument_key,
                        underlying,
                        kind,
                        expiry,
                        strike,
                        option_type,
                        source_broker,
                        trading_symbol,
                        first_seen_at,
                        last_seen_at,
                        last_underlying_price,
                        last_ltp,
                        last_change_pct,
                        last_oi,
                        last_oi_change,
                        last_volume,
                        last_iv,
                        last_macd,
                        last_macd_signal,
                        last_macd_histogram,
                        last_rsi,
                        snapshot_count,
                        archived_at
                    )
                    SELECT
                        latest.archive_key,
                        latest.underlying,
                        latest.kind,
                        latest.expiry,
                        latest.strike,
                        latest.option_type,
                        latest.source_broker,
                        latest.trading_symbol,
                        summary.first_seen_at,
                        summary.last_seen_at,
                        latest.last_underlying_price,
                        latest.last_ltp,
                        latest.last_change_pct,
                        latest.last_oi,
                        latest.last_oi_change,
                        latest.last_volume,
                        latest.last_iv,
                        latest.last_macd,
                        latest.last_macd_signal,
                        latest.last_macd_histogram,
                        latest.last_rsi,
                        summary.snapshot_count,
                        NOW()
                    FROM latest
                    JOIN summary
                      ON summary.archive_key = latest.archive_key
                    ON CONFLICT (instrument_key) DO UPDATE
                    SET
                        last_seen_at = EXCLUDED.last_seen_at,
                        last_underlying_price = EXCLUDED.last_underlying_price,
                        last_ltp = EXCLUDED.last_ltp,
                        last_change_pct = EXCLUDED.last_change_pct,
                        last_oi = EXCLUDED.last_oi,
                        last_oi_change = EXCLUDED.last_oi_change,
                        last_volume = EXCLUDED.last_volume,
                        last_iv = EXCLUDED.last_iv,
                        last_macd = EXCLUDED.last_macd,
                        last_macd_signal = EXCLUDED.last_macd_signal,
                        last_macd_histogram = EXCLUDED.last_macd_histogram,
                        last_rsi = EXCLUDED.last_rsi,
                        snapshot_count = EXCLUDED.snapshot_count,
                        archived_at = NOW()
                """)
            )
            await session.commit()

    @staticmethod
    def _parse_expiry(expiry: Optional[str]) -> Optional[date]:
        if not expiry:
            return None
        try:
            return date.fromisoformat(str(expiry))
        except ValueError:
            return None

    async def _load_underlyings(self) -> list[UnderlyingMeta]:
        statement = text("""
            SELECT symbol, kind, spot_instrument_key, underlying_key
            FROM fo_underlying_catalog
            WHERE spot_instrument_key IS NOT NULL
              AND underlying_key IS NOT NULL
            ORDER BY CASE WHEN kind = 'INDEX' THEN 0 ELSE 1 END, symbol
        """)
        async with AsyncSessionLocal() as session:
            result = await session.execute(statement)
            return [
                UnderlyingMeta(
                    symbol=str(row.symbol),
                    kind=str(row.kind),
                    spot_instrument_key=str(row.spot_instrument_key),
                    underlying_key=str(row.underlying_key),
                )
                for row in result.fetchall()
            ]

    async def _get_upstox_adapter(self) -> Optional[BrokerAdapter]:
        await ensure_upstox_session(force_validate=False)
        adapter = get_active_adapter("upstox")
        if adapter:
            return adapter
        return None

    @staticmethod
    def _to_fyers_symbol(meta: UnderlyingMeta) -> str:
        if meta.kind == "INDEX":
            return INDEX_FYERS_SYMBOLS.get(meta.symbol, f"NSE:{meta.symbol}-INDEX")
        return f"NSE:{meta.symbol}-EQ"


atm_watchlist_service = ATMWatchlistService()
