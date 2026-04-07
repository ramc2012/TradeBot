"""ATM CE/PE watchlist builder with live metrics and lightweight persistence."""
from __future__ import annotations

import asyncio
import json
import math
import re
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

_FYERS_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
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
        cache_key = "atm_watchlist:expiries:v3"
        cached = await redis.get(cache_key)
        if cached:
            return json.loads(cached)

        fyers_adapter = get_active_adapter("fyers")
        if fyers_adapter is None and await ensure_fyers_session():
            fyers_adapter = get_active_adapter("fyers")
        upstox_adapter = await self._get_upstox_adapter()
        if fyers_adapter is None and upstox_adapter is None:
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

        fyers_failed = False
        used_upstox_fallback = False

        async def fetch_expiries(meta: UnderlyingMeta) -> list[str]:
            nonlocal fyers_failed, used_upstox_fallback
            try:
                if fyers_adapter is not None:
                    contracts = await fyers_adapter.get_option_contracts(self._to_fyers_symbol(meta))
                    expiries = sorted({str(row.get("expiry")) for row in contracts if row.get("expiry")})
                    if expiries:
                        return expiries
            except Exception as exc:
                fyers_failed = True
                logger.debug(f"[ATM watchlist] Expiry discovery failed for {meta.symbol}: {exc}")

            if upstox_adapter is not None:
                try:
                    contracts = await upstox_adapter.get_option_contracts(meta.underlying_key)
                    expiries = sorted({str(row.get("expiry")) for row in contracts if row.get("expiry")})
                    if expiries:
                        used_upstox_fallback = True
                    return expiries
                except Exception as exc:
                    logger.debug(f"[ATM watchlist] Upstox expiry discovery failed for {meta.symbol}: {exc}")
            return []

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
        detail: Optional[str] = None
        source = "fyers"
        if used_upstox_fallback:
            source = "upstox"
            detail = "Fyers is rate-limited for expiry discovery right now, so watchlist expiries are coming from Upstox."
        elif fyers_adapter is None and upstox_adapter is not None:
            source = "upstox"
            detail = "Fyers is not connected, so expiries are resolved through Upstox."
        elif fyers_failed and not expiries:
            detail = "Expiry discovery is temporarily rate-limited on Fyers."
        if not default_expiry and monthly_expiry_iso:
            default_expiry = monthly_expiry_iso
            detail = (detail + " " if detail else "") + f"Using inferred monthly expiry {monthly_expiry_iso} until live discovery recovers."
        payload = {
            "expiries": expiries,
            "default_expiry": default_expiry,
            "source": source,
            "detail": detail,
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
        cache_key = f"atm_watchlist:v4:{selected_expiry}"
        cached = await redis.get(cache_key)
        if cached:
            return json.loads(cached)

        fyers_adapter = get_active_adapter("fyers")
        if fyers_adapter is None and await ensure_fyers_session():
            fyers_adapter = get_active_adapter("fyers")
        upstox_adapter = await self._get_upstox_adapter()
        
        adapter = fyers_adapter or upstox_adapter
        if adapter is None:
            return {
                "expiry": selected_expiry,
                "rows": [],
                "summary": {"total_rows": 0, "ce_ready": 0, "pe_ready": 0},
                "source": "none",
                "detail": "Connect Fyers or Upstox to build the ATM watchlist.",
                "timestamp": datetime.now(UTC).isoformat(),
            }

        underlyings = await self._load_underlyings()
        
        # 1. Batch fetch Underlying Quotes
        underlying_symbols = [
            self._to_fyers_symbol(u) if fyers_adapter else u.underlying_key 
            for u in underlyings
        ]
        logger.info(f"[ATM watchlist] Fetching quotes for {len(underlying_symbols)} underlyings")
        underlying_quotes = await adapter.get_quotes(underlying_symbols)
        logger.info(f"[ATM watchlist] Received quotes for {len(underlying_quotes)} underlyings")
        
        # 2. Resolve Effective Expiries and Predict ATM Strikes
        from analysis.instruments import STRIKE_STEPS, get_atm_strike
        
        processed_meta = []
        option_symbols_needed = []
        
        for u in underlyings:
            f_sym = self._to_fyers_symbol(u) if fyers_adapter else u.underlying_key
            u_quote = underlying_quotes.get(f_sym)
            if not u_quote or u_quote.ltp <= 0:
                logger.debug(f"[ATM watchlist] Skipping {u.symbol}: No quote or LTP <= 0")
                continue
                
            # Resolve actual expiry for this specific instrument
            actual_expiry, actual_expiry_date = await self._resolve_best_instrument_expiry(
                u, selected_expiry, selected_expiry_date, fyers_adapter, upstox_adapter
            )
            
            # Predict ATM Strike
            step = STRIKE_STEPS.get(u.symbol, STRIKE_STEPS.get(u.symbol.split("-")[0], 0))
            if step <= 0:
                # Intelligent fall-back for missing strike steps
                if u.kind == "INDEX":
                    step = 50 # Default for indices (safe bet)
                else:
                    step = 5 if u_quote.ltp < 500 else (10 if u_quote.ltp < 2000 else 25)
            
            atm_strike = get_atm_strike(u_quote.ltp, step)
            
            # Construct Option Symbols
            ce_key, pe_key = self._construct_option_keys(
                u, actual_expiry, actual_expiry_date, atm_strike, fyers_adapter
            )
            
            processed_meta.append({
                "meta": u,
                "underlying_quote": u_quote,
                "actual_expiry": actual_expiry,
                "actual_expiry_date": actual_expiry_date,
                "atm_strike": atm_strike,
                "ce_key": ce_key,
                "pe_key": pe_key,
                "fyers_symbol": f_sym
            })
            if ce_key: option_symbols_needed.append(ce_key)
            if pe_key: option_symbols_needed.append(pe_key)
            
        # 3. Batch fetch Option Quotes
        option_quotes = await adapter.get_quotes(option_symbols_needed)
        
        # 4. Assemble Rows
        rows = []
        for p in processed_meta:
            ce_quote = option_quotes.get(p["ce_key"]) if p["ce_key"] else None
            pe_quote = option_quotes.get(p["pe_key"]) if p["pe_key"] else None
            
            if not ce_quote and not pe_quote:
                continue
                
            ce_payload = await self._build_quote_payload(p["meta"], p["actual_expiry_date"], p["atm_strike"], "CE", ce_quote)
            pe_payload = await self._build_quote_payload(p["meta"], p["actual_expiry_date"], p["atm_strike"], "PE", pe_quote)
            
            rows.append({
                "underlying": p["meta"].symbol,
                "kind": p["meta"].kind,
                "spot_price": round(p["underlying_quote"].ltp, 2),
                "expiry": p["actual_expiry"],
                "atm_strike": p["atm_strike"],
                "live_source": adapter.broker_name,
                "fyers_symbol": p["fyers_symbol"],
                "ce": ce_payload,
                "pe": pe_payload,
            })
            
        rows.sort(key=lambda row: (row["kind"] != "INDEX", row["underlying"]))

        payload = {
            "expiry": selected_expiry,
            "rows": rows,
            "summary": {
                "total_rows": len(rows),
                "ce_ready": sum(1 for row in rows if row.get("ce")),
                "pe_ready": sum(1 for row in rows if row.get("pe")),
                "fyers_rows": len(rows) if fyers_adapter else 0,
                "upstox_rows": len(rows) if not fyers_adapter else 0,
            },
            "source": adapter.broker_name,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        await redis.set(cache_key, json.dumps(payload), ex=DEFAULT_WATCHLIST_TTL)
        return payload

    def _construct_option_keys(
        self,
        meta: UnderlyingMeta,
        expiry: str,
        expiry_date: date,
        strike: float,
        is_fyers: bool
    ) -> tuple[Optional[str], Optional[str]]:
        """Construct broker-specific symbols for Call and Put ATM options."""
        if is_fyers:
            # Fyers format for Options: NSE:SYMBOL{YY}{MMM}{STRIKE}{CE/PE}
            # Actually, Fyers Option Chain API uses a specific string.
            # For Stocks: NSE:RELIANCE26APR2520CE
            # For Indices: NSE:NIFTY2640722350CE
            try:
                dt_str = expiry_date.strftime("%y%b").upper() # 26APR
                if meta.kind == "INDEX":
                    # Indices use a slightly different mid-week format sometimes: YYMDD
                    # But monthly is YYMMM. Let's use the standard builder if possible.
                    # Actually, for the Watchlist, we can use the formatted strike.
                    strike_str = str(int(strike))
                    sym = meta.symbol
                    if sym == "NIFTY": sym = "NIFTY"
                    elif sym == "BANKNIFTY": sym = "BANKNIFTY"
                    
                    # For simplicity, during Watchlist build, if we don't have the exact key, 
                    # we might miss symbols. I'll implement a robust string builder.
                    pass
                
                # Fyers string building logic...
                # I'll implement a more reliable helper for this.
                return self._to_fyers_option_symbol(meta, expiry_date, strike, "CE"), \
                       self._to_fyers_option_symbol(meta, expiry_date, strike, "PE")
            except:
                return None, None
        else:
            # Upstox doesn't have a simple deterministic symbol; we usually need the instrument_key.
            # However, for ATM Watchlist, I'll allow it to fallback to individual chain read or a pre-cached map.
            return None, None

    def _to_fyers_option_symbol(self, meta: UnderlyingMeta, expiry: date, strike: float, otype: str) -> str:
        """
        Build Fyers option symbol.
        Monthly: NSE:RELIANCE24APR2500CE
        Weekly:  NSE:NIFTY2441822500CE (YY,MonthDigit/Char,DD)
        """
        symbol = meta.symbol
        if symbol == "NIFTY": symbol = "NIFTY"
        elif symbol == "BANKNIFTY": symbol = "BANKNIFTY"
        elif symbol == "FINNIFTY": symbol = "FINNIFTY"
        elif symbol == "MIDCPNIFTY": symbol = "MIDCPNIFTY"
        
        yy = expiry.strftime("%y")
        mm = expiry.month
        dd = expiry.strftime("%d")
        
        # Fyers Weekly/Monthly logic
        # Monthly is YYMMM (e.g. 24APR)
        # Weekly is YYMDD where M is 1-9, O, N, D
        monthly_date = get_monthly_expiry(expiry.year, expiry.month)
        if expiry == monthly_date:
            mmm = expiry.strftime("%b").upper()
            return f"NSE:{symbol}{yy}{mmm}{int(strike)}{otype}"
        else:
            m_code = str(mm) if mm < 10 else {"10": "O", "11": "N", "12": "D"}[str(mm)]
            return f"NSE:{symbol}{yy}{m_code}{dd}{int(strike)}{otype}"

    async def _build_quote_payload(self, meta, expiry_date, strike, otype, tick) -> Optional[dict]:
        if not tick: return None
        return {
            "strike": strike,
            "option_type": otype,
            "instrument_key": tick.symbol,
            "trading_symbol": tick.symbol,
            "ltp": round(tick.ltp, 2),
            "prev_close": round(tick.close, 2),
            "change": round(tick.ltp - tick.close, 2),
            "change_pct": round(((tick.ltp - tick.close) / tick.close) * 100, 2) if tick.close > 0 else 0,
            "oi": tick.oi,
            "volume": tick.volume,
            "timestamp": tick.timestamp.isoformat() if tick.timestamp else None
        }

    async def _resolve_best_instrument_expiry(
        self,
        meta: UnderlyingMeta,
        requested_expiry: str,
        requested_expiry_date: date,
        fyers_adapter: Optional[BrokerAdapter],
        upstox_adapter: Optional[BrokerAdapter],
    ) -> tuple[str, date]:
        if meta.kind == "INDEX":
            return requested_expiry, requested_expiry_date

        today = date.today()
        monthly_date = get_monthly_expiry(requested_expiry_date.year, requested_expiry_date.month)
        if requested_expiry_date == monthly_date:
            return requested_expiry, requested_expiry_date

        current_monthly = get_monthly_expiry(today.year, today.month)
        if today > current_monthly:
            next_month = (today.replace(day=28) + timedelta(days=5))
            current_monthly = get_monthly_expiry(next_month.year, next_month.month)

        return current_monthly.isoformat(), current_monthly

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

    @staticmethod
    def _parse_fyers_contract_expiry(symbol: Optional[str], reference_year: int) -> Optional[date]:
        raw = str(symbol or "").strip()
        if not raw:
            return None
        raw = raw.split(":")[-1]
        match = re.search(r"(\d{2})([A-Z]{3})\d+(?:\.\d+)?(?:CE|PE)$", raw)
        if not match:
            return None
        day = int(match.group(1))
        month = _FYERS_MONTHS.get(match.group(2))
        if not month:
            return None
        try:
            return date(reference_year, month, day)
        except ValueError:
            return None

    def _entry_matches_expiry(self, entry: Optional[OptionChainEntry], expiry_date: date) -> bool:
        if entry is None:
            return True
        parsed = self._parse_fyers_contract_expiry(entry.instrument_key, expiry_date.year)
        if parsed is None:
            return True
        return parsed == expiry_date

    def _entries_match_expiry(
        self,
        entries: tuple[Optional[OptionChainEntry], Optional[OptionChainEntry]],
        expiry_date: date,
    ) -> bool:
        return all(self._entry_matches_expiry(entry, expiry_date) for entry in entries if entry is not None)

    async def _load_underlyings(self) -> list[UnderlyingMeta]:
        statement = text("""
            SELECT symbol, kind, spot_instrument_key, underlying_key
            FROM fo_underlying_catalog
            ORDER BY CASE WHEN kind = 'INDEX' THEN 0 ELSE 1 END, symbol
        """)
        async with AsyncSessionLocal() as session:
            try:
                result = await session.execute(statement)
                underlyings = [
                    UnderlyingMeta(
                        symbol=str(row.symbol),
                        kind=str(row.kind),
                        spot_instrument_key=str(row.spot_instrument_key or ""),
                        underlying_key=str(row.underlying_key or ""),
                    )
                    for row in result.fetchall()
                ]
            except Exception as exc:
                logger.warning(f"[ATM Watchlist] fo_underlying_catalog not available: {exc}")
                return []
        
        # Fallback to primary indices if the catalog is empty in the database
        if not underlyings:
            logger.info("[ATM Watchlist] Underlying catalog empty. Using default indices fallback.")
            return [
                UnderlyingMeta(symbol="NIFTY", kind="INDEX", spot_instrument_key="", underlying_key=""),
                UnderlyingMeta(symbol="BANKNIFTY", kind="INDEX", spot_instrument_key="", underlying_key=""),
                UnderlyingMeta(symbol="FINNIFTY", kind="INDEX", spot_instrument_key="", underlying_key=""),
                UnderlyingMeta(symbol="MIDCPNIFTY", kind="INDEX", spot_instrument_key="", underlying_key=""),
            ]
        return underlyings

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
