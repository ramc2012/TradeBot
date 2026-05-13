"""
MACD F&O Options Backtester
============================
Strategy:
  For each underlying in F&O indices/stocks:
    For each monthly expiry in the date range:
      1. Get spot price on the first trading day after previous monthly expiry
      2. Compute/select ATM strike
      3. Download 30-min candles for ATM CE and PE (if not in DB, download from Upstox)
      4. Compute MACD(12,26,9)
      5. Find zero-line crossovers (MACD crosses above 0 = buy signal)
      6. Analyze max move from entry to expiry
      7. Record opportunity
"""
from __future__ import annotations

import asyncio
import time
import urllib.parse
from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import median
from typing import Callable, Optional

import httpx
from loguru import logger
from sqlalchemy import text

from analysis.instruments import (
    INDEX_INSTRUMENT_KEYS,
    BREEZE_INDEX_CODES,
    BREEZE_RIGHT_MAP,
    get_first_trading_day_after,
)
from analysis.macd_engine import (
    analyze_trade,
    compute_macd,
    find_zero_crossovers,
    simulate_exit_strategies,
)
from db.database import AsyncSessionLocal

# ── Constants ─────────────────────────────────────────────────────────────────

UPSTOX_BASE = "https://api.upstox.com/v2"
CANDLE_INTERVAL = "30minute"
MIN_CANDLES_REQUIRED = 20
CONCURRENCY_LIMIT = 5
RATE_LIMIT_DELAY = 0.2  # seconds between API calls = 5 req/sec max
MAX_429_RETRIES = 5


class UpstoxAuthError(RuntimeError):
    """Raised when Upstox rejects an authenticated expired-data request."""

# Candle array index positions from Upstox
_TS, _O, _H, _L, _C, _V, _OI = 0, 1, 2, 3, 4, 5, 6


# ── MACDBacktester ────────────────────────────────────────────────────────────


class MACDBacktester:
    """
    MACD zero-line crossover backtester for NSE F&O options.

    For each underlying and each monthly expiry in the given date range:
      - Identifies the ATM strike on the first trading day of that month
      - Fetches 30-minute OHLCV candles for ATM CE and PE (from DB or Upstox)
      - Runs MACD(12,26,9) and detects zero-line buy crossovers
      - Analyses max move from each crossover to the expiry date

    Usage
    -----
    backtester = MACDBacktester(access_token="<upstox_token>")
    results = await backtester.run(
        underlyings=["NIFTY", "BANKNIFTY"],
        from_date=date(2025, 1, 1),
        to_date=date(2026, 3, 28),
    )
    """

    def __init__(self, access_token: str) -> None:
        """
        Parameters
        ----------
        access_token : str
            Upstox bearer token (just the token string, not 'Bearer ...')
        """
        self.access_token = access_token
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        self.rate_limit_delay = RATE_LIMIT_DELAY
        self._fo_universe_cache: Optional[dict[str, list[str]]] = None
        # Cache expired contracts per (underlying|expiry_iso) key
        self._expired_contracts_cache: dict[str, list[dict]] = {}
        # Cache all available expiries per underlying
        self._expiry_cache: dict[str, list[date]] = {}
        # Cache underlying metadata (symbol → spot key / underlying key)
        self._underlying_meta_cache: dict[str, dict] = {}
        # Cache spot candle series (symbol → daily candles)
        self._spot_series_cache: dict[str, list[dict]] = {}
        self._semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
        self._last_call_ts: float = 0.0
        self._spot_series_from: Optional[date] = None
        self._spot_series_to: Optional[date] = None
        self._db_available: Optional[bool] = None
        self.rate_limit_hits: int = 0
        self.rate_limit_backoff_seconds: float = 0.0
        self.api_call_counts: dict[str, int] = defaultdict(int)

    def set_access_token(self, access_token: str) -> None:
        self.access_token = access_token
        self.headers["Authorization"] = f"Bearer {access_token}"

    def reset_rate_limit_stats(self) -> None:
        self.rate_limit_hits = 0
        self.rate_limit_backoff_seconds = 0.0
        self.api_call_counts = defaultdict(int)

    def _record_api_call(self, endpoint: str) -> None:
        self.api_call_counts[endpoint] += 1

    # ── Rate limiting ──────────────────────────────────────────────────────────

    async def _throttle(self) -> None:
        """Enforce RATE_LIMIT_DELAY seconds between consecutive Upstox API calls."""
        elapsed = time.monotonic() - self._last_call_ts
        if elapsed < self.rate_limit_delay:
            await asyncio.sleep(self.rate_limit_delay - elapsed)
        self._last_call_ts = time.monotonic()

    # ── Universe / instrument lookup ──────────────────────────────────────────

    async def fetch_fo_universe(self) -> dict[str, list[str]]:
        """
        Fetch the current NSE F&O universe from NSE's own underlying-information API.
        """
        if self._fo_universe_cache is not None:
            return self._fo_universe_cache

        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; NomadCurie/1.0)",
            "Accept": "application/json,text/plain,*/*",
            "Accept-Encoding": "identity",
            "Referer": (
                "https://www.nseindia.com/products-services/"
                "equity-derivatives-list-underlyings-information"
            ),
        }

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            await client.get("https://www.nseindia.com", headers=headers)
            resp = await client.get(
                "https://www.nseindia.com/api/underlying-information",
                headers=headers,
            )
            resp.raise_for_status()
            payload = resp.json().get("data", {})

        indices = sorted(
            item.get("symbol", "").upper()
            for item in payload.get("IndexList", [])
            if item.get("symbol")
        )
        stocks = sorted(
            item.get("symbol", "").upper()
            for item in payload.get("UnderlyingList", [])
            if item.get("symbol")
        )
        self._fo_universe_cache = {"indices": indices, "stocks": stocks}
        logger.info(
            f"Fetched NSE F&O universe: {len(indices)} indices, {len(stocks)} stocks"
        )
        return self._fo_universe_cache

    async def _search_instruments(self, **params) -> list[dict]:
        """Call Upstox instrument search API and return the result rows."""
        url = f"{UPSTOX_BASE}/instruments/search"
        async with self._semaphore:
            await self._throttle()
            async with httpx.AsyncClient(timeout=30.0) as client:
                self._record_api_call("instrument_search")
                resp = await client.get(url, params=params, headers=self.headers)
        if resp.status_code != 200:
            logger.debug(
                f"Instrument search failed ({resp.status_code}) for params={params}: "
                f"{resp.text[:160]}"
            )
            return []
        return resp.json().get("data", [])

    async def _fetch_expiry_dates(self, underlying: str) -> list[date]:
        """
        Fetch all known expiry dates for one underlying from Upstox.
        """
        underlying = underlying.upper()
        if underlying in self._expiry_cache:
            return self._expiry_cache[underlying]

        underlying_key = await self._get_underlying_key(underlying)
        if not underlying_key:
            self._expiry_cache[underlying] = []
            return []

        url = (
            f"{UPSTOX_BASE}/expired-instruments/expiries"
            f"?instrument_key={urllib.parse.quote(underlying_key, safe='')}"
        )
        async with self._semaphore:
            await self._throttle()
            async with httpx.AsyncClient(timeout=30.0) as client:
                self._record_api_call("expired_expiries")
                resp = await client.get(url, headers=self.headers)
        if resp.status_code != 200:
            logger.debug(
                f"Expiry fetch failed ({resp.status_code}) for {underlying}: "
                f"{resp.text[:160]}"
            )
            self._expiry_cache[underlying] = []
            return []

        parsed = sorted(
            date.fromisoformat(item)
            for item in resp.json().get("data", [])
            if item
        )
        self._expiry_cache[underlying] = parsed
        return parsed

    @staticmethod
    def _select_monthly_expiries(
        expiry_dates: list[date],
        from_date: date,
        to_date: date,
    ) -> tuple[list[date], dict[date, Optional[date]]]:
        """
        Reduce all expiry dates to the last expiry available in each month.
        """
        monthly_map: dict[tuple[int, int], date] = {}
        for expiry in expiry_dates:
            if expiry > to_date:
                continue
            key = (expiry.year, expiry.month)
            prev = monthly_map.get(key)
            if prev is None or expiry > prev:
                monthly_map[key] = expiry

        monthly_all = sorted(monthly_map.values())
        previous_map: dict[date, Optional[date]] = {}
        previous_expiry: Optional[date] = None
        for expiry in monthly_all:
            previous_map[expiry] = previous_expiry
            previous_expiry = expiry

        monthly_in_range = [
            expiry for expiry in monthly_all
            if from_date <= expiry <= to_date
        ]
        return monthly_in_range, previous_map

    async def _resolve_underlying_metadata(self, underlying: str) -> Optional[dict]:
        """
        Resolve the Upstox spot instrument key / underlying key for an index or stock.
        """
        underlying = underlying.upper()
        if underlying in self._underlying_meta_cache:
            return self._underlying_meta_cache[underlying]

        search_params = {
            "query": underlying,
            "exchanges": "NSE",
            "records": 30,
        }
        if underlying.endswith("NIFTY") or underlying in INDEX_INSTRUMENT_KEYS:
            search_params["segments"] = "INDEX"
        else:
            search_params["segments"] = "EQ"

        results = await self._search_instruments(**search_params)

        def _score(item: dict) -> tuple[int, int]:
            symbol = str(item.get("trading_symbol", "")).upper()
            name = str(item.get("name", "")).upper()
            short_name = str(item.get("short_name", "")).upper()
            exact = int(symbol == underlying or name == underlying or short_name == underlying)
            nse_eq = int(item.get("segment") in ("NSE_EQ", "NSE_INDEX"))
            return (exact, nse_eq)

        if results:
            results.sort(key=_score, reverse=True)
            chosen = results[0]
            meta = {
                "spot_instrument_key": chosen.get("instrument_key"),
                "underlying_key": chosen.get("instrument_key"),
                "segment": chosen.get("segment", ""),
                "display_name": chosen.get("name") or chosen.get("trading_symbol") or underlying,
            }
            self._underlying_meta_cache[underlying] = meta
            return meta

        # Fallback to static mapping for known indices
        static_key = INDEX_INSTRUMENT_KEYS.get(underlying)
        if static_key:
            meta = {
                "spot_instrument_key": static_key,
                "underlying_key": static_key,
                "segment": "NSE_INDEX",
                "display_name": underlying,
            }
            self._underlying_meta_cache[underlying] = meta
            return meta

        logger.warning(f"Could not resolve Upstox metadata for {underlying}")
        self._underlying_meta_cache[underlying] = {}
        return None

    async def _get_underlying_key(self, underlying: str) -> Optional[str]:
        meta = await self._resolve_underlying_metadata(underlying)
        if not meta:
            return None
        return meta.get("underlying_key")

    async def _get_spot_instrument_key(self, underlying: str) -> Optional[str]:
        meta = await self._resolve_underlying_metadata(underlying)
        if not meta:
            return None
        return meta.get("spot_instrument_key")

    # ── Spot price ─────────────────────────────────────────────────────────────

    async def _get_spot_daily_series(self, underlying: str) -> list[dict]:
        """
        Fetch and cache daily candles for the full run window for one underlying.
        """
        if underlying in self._spot_series_cache:
            return self._spot_series_cache[underlying]

        instrument_key = await self._get_spot_instrument_key(underlying)
        if not instrument_key or not self._spot_series_from or not self._spot_series_to:
            return []

        encoded_key = urllib.parse.quote(instrument_key, safe="")
        url = (
            f"{UPSTOX_BASE}/historical-candle"
            f"/{encoded_key}/day"
            f"/{self._spot_series_to.isoformat()}/{self._spot_series_from.isoformat()}"
        )

        async with self._semaphore:
            await self._throttle()
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    self._record_api_call("historical_day")
                    resp = await client.get(url, headers=self.headers)

                if resp.status_code in (400, 401, 403):
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        self._record_api_call("historical_day")
                        resp = await client.get(url, headers={"Accept": "application/json"})

                if resp.status_code != 200:
                    logger.debug(
                        f"Spot series fetch HTTP {resp.status_code} for {underlying}: "
                        f"{resp.text[:120]}"
                    )
                    return []

                raw_candles = resp.json().get("data", {}).get("candles", [])
                raw_candles = list(reversed(raw_candles))
                candles = [
                    {
                        "time": str(c[_TS]),
                        "open": float(c[_O]),
                        "high": float(c[_H]),
                        "low": float(c[_L]),
                        "close": float(c[_C]),
                        "volume": int(c[_V]),
                        "oi": int(c[_OI]),
                    }
                    for c in raw_candles
                ]
                self._spot_series_cache[underlying] = candles
                return candles

            except Exception as exc:
                logger.warning(f"Error fetching spot series for {underlying}: {exc}")
                return []

    async def _get_spot_reference(self, underlying: str, target_date: date) -> tuple[float, Optional[date]]:
        """
        Return the first available daily close on or after target_date.
        """
        candles = await self._get_spot_daily_series(underlying)
        for candle in candles:
            candle_date = datetime.fromisoformat(str(candle["time"]).replace("Z", "+00:00")).date()
            if candle_date >= target_date:
                return float(candle["close"]), candle_date
        return 0.0, None

    # ── Option candles from DB ─────────────────────────────────────────────────

    async def _get_candles_from_db(
        self,
        underlying: str,
        expiry: date,
        strike: float,
        option_type: str,
        from_date: date,
        to_date: date,
        instrument_key: Optional[str] = None,
    ) -> list[dict]:
        """
        Query option_premium_candles table for existing candle data.

        Returns
        -------
        list[dict]
            List of candle dicts {time, open, high, low, close, volume, oi},
            sorted chronologically. Empty list if no data found.
        """
        if self._db_available is False:
            return []
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text("""
                        SELECT
                            time, open, high, low, close, volume, oi
                        FROM option_premium_candles
                        WHERE
                            underlying = :underlying
                            AND expiry = :expiry
                            AND strike = :strike
                            AND option_type = :option_type
                            AND interval = '30minute'
                            AND time >= :from_ts
                            AND time <= :to_ts
                            AND (
                                :instrument_key IS NULL
                                OR instrument_key = :instrument_key
                                OR instrument_key IS NULL
                            )
                        ORDER BY time ASC
                    """),
                    {
                        "underlying": underlying,
                        "expiry": expiry.isoformat(),
                        "strike": strike,
                        "option_type": option_type,
                        "from_ts": datetime.combine(from_date, datetime.min.time()),
                        "to_ts": datetime.combine(to_date, datetime.max.time()),
                        "instrument_key": instrument_key,
                    },
                )
                rows = result.fetchall()
                candles = [
                    {
                        "time": row.time.isoformat() if hasattr(row.time, "isoformat") else str(row.time),
                        "open": float(row.open),
                        "high": float(row.high),
                        "low": float(row.low),
                        "close": float(row.close),
                        "volume": int(row.volume) if row.volume is not None else 0,
                        "oi": int(row.oi) if row.oi is not None else 0,
                    }
                    for row in rows
                ]
                self._db_available = True
                return candles
        except Exception as exc:
            if self._db_available is not False:
                logger.warning(
                    f"Disabling DB candle cache after lookup failure: {exc}"
                )
            self._db_available = False
            logger.debug(f"DB query failed for {underlying} {expiry} {strike}{option_type}: {exc}")
            return []

    # ── Instrument key lookup ──────────────────────────────────────────────────

    @staticmethod
    def _parse_upstox_expiry(expiry_val) -> Optional[date]:
        """
        Parse an Upstox expiry field which can be:
          - ISO date string: "2025-01-30"
          - Unix milliseconds integer/float: 1738166400000
          - ISO datetime string: "2025-01-30T00:00:00"
        Returns a date or None on failure.
        """
        if expiry_val is None:
            return None
        if isinstance(expiry_val, (int, float)):
            # Unix ms → date
            try:
                return datetime.utcfromtimestamp(expiry_val / 1000.0).date()
            except Exception:
                return None
        s = str(expiry_val).strip()
        # Try most-specific first (ISO datetime with milliseconds, then plain date)
        for fmt, slen in [
            ("%Y-%m-%dT%H:%M:%S.%f", 26),
            ("%Y-%m-%dT%H:%M:%S",    19),
            ("%Y-%m-%d",             10),
        ]:
            try:
                return datetime.strptime(s[:slen], fmt).date()
            except Exception:
                pass
        return None

    async def _fetch_expired_contracts(
        self, underlying: str, expiry: date
    ) -> list[dict]:
        """
        Call Upstox GET /v2/expired-instruments/option/contract to retrieve all
        option contracts for a given underlying+expiry.

        This API returns instrument_keys for expired options so their 30-minute
        candle history can be fetched (30min data goes back 1 year on Upstox).

        Requires: valid Upstox Bearer token + Upstox Pro/Plus plan (UDAPI1149 if not).

        Returns
        -------
        list[dict]
            Raw contract objects from the API, or [] on error / plan restriction.
        """
        cache_key = f"{underlying}|{expiry.isoformat()}"
        if cache_key in self._expired_contracts_cache:
            return self._expired_contracts_cache[cache_key]

        underlying_key = await self._get_underlying_key(underlying)
        if not underlying_key:
            self._expired_contracts_cache[cache_key] = []
            return []

        encoded_uk = urllib.parse.quote(underlying_key, safe="")
        url = (
            f"{UPSTOX_BASE}/expired-instruments/option/contract"
            f"?instrument_key={encoded_uk}&expiry_date={expiry.isoformat()}"
        )

        async with self._semaphore:
            await self._throttle()
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    self._record_api_call("expired_contracts")
                    resp = await client.get(url, headers=self.headers)

                if resp.status_code == 200:
                    contracts = resp.json().get("data", [])
                    logger.info(
                        f"Expired contracts API: {len(contracts)} contracts "
                        f"for {underlying} {expiry}"
                    )
                    self._expired_contracts_cache[cache_key] = contracts
                    return contracts

                if resp.status_code in (400, 403):
                    # Detect plan restriction (UDAPI1149)
                    try:
                        errs = resp.json().get("errors", [])
                        codes = [e.get("errorCode", "") for e in errs]
                        if "UDAPI1149" in codes:
                            logger.warning(
                                "Upstox Pro/Plus plan required for expired-instruments API "
                                "(error UDAPI1149). Candles for expired options will be "
                                "unavailable via Upstox. Consider upgrading your Upstox plan."
                            )
                            # Mark all calls as plan-restricted so we stop retrying
                            self._upstox_plan_restricted = True
                            self._expired_contracts_cache[cache_key] = []
                            return []
                    except Exception:
                        pass

                if resp.status_code in (401, 403):
                    raise UpstoxAuthError(
                        f"Expired contracts API rejected the Upstox token for {underlying} {expiry} "
                        f"(HTTP {resp.status_code})."
                    )

                logger.debug(
                    f"Expired contracts HTTP {resp.status_code} for "
                    f"{underlying} {expiry}: {resp.text[:200]}"
                )

            except UpstoxAuthError:
                raise
            except Exception as exc:
                logger.warning(
                    f"Expired contracts API error for {underlying} {expiry}: {exc}"
                )

        self._expired_contracts_cache[cache_key] = []
        return []

    async def _select_atm_contracts(
        self,
        underlying: str,
        expiry: date,
        spot_price: float,
    ) -> Optional[dict]:
        """
        Fetch all expired contracts for the month and pick the nearest available
        common ATM strike across CE and PE.
        """
        contracts = await self._fetch_expired_contracts(underlying, expiry)
        if not contracts:
            return None

        by_type: dict[str, dict[float, dict]] = {"CE": {}, "PE": {}}
        for contract in contracts:
            option_type = str(contract.get("instrument_type", "")).upper()
            if option_type not in ("CE", "PE"):
                continue
            try:
                strike = float(
                    contract.get("strike_price")
                    or contract.get("strike")
                    or 0.0
                )
            except (TypeError, ValueError):
                continue
            if strike <= 0:
                continue
            by_type[option_type][strike] = contract

        common_strikes = sorted(set(by_type["CE"]) & set(by_type["PE"]))
        if common_strikes:
            atm_strike = min(common_strikes, key=lambda strike: (abs(strike - spot_price), strike))
            return {
                "atm_strike": atm_strike,
                "CE": by_type["CE"].get(atm_strike),
                "PE": by_type["PE"].get(atm_strike),
                "available_strikes": len(common_strikes),
            }

        # Fallback: independently choose nearest CE / PE if a common strike is absent.
        result: dict[str, object] = {"available_strikes": 0}
        for option_type in ("CE", "PE"):
            strikes = sorted(by_type[option_type])
            if not strikes:
                continue
            nearest = min(strikes, key=lambda strike: (abs(strike - spot_price), strike))
            result[option_type] = by_type[option_type][nearest]
            result[f"{option_type}_strike"] = nearest

        if "CE" not in result and "PE" not in result:
            return None
        result["atm_strike"] = result.get("CE_strike") or result.get("PE_strike") or 0.0
        return result  # type: ignore[return-value]

    # ── Fetch candles from Upstox API ─────────────────────────────────────────

    async def _fetch_candles_from_upstox(
        self,
        instrument_key: str,
        from_date: date,
        to_date: date,
        retry_count: int = 0,
    ) -> list[dict]:
        """
        Fetch 30-minute OHLCV candles for an instrument from Upstox.

        Parameters
        ----------
        instrument_key : str
            Upstox instrument key (will be URL-encoded)
        from_date : date
            Start date
        to_date : date
            End date

        Returns
        -------
        list[dict]
            Candles as dicts {time, open, high, low, close, volume, oi}, sorted
            chronologically. Empty on error or no data.
        """
        # Defensive guard: callers occasionally pass None for one of the
        # window endpoints when the source row hasn't backfilled candle
        # boundaries yet. Without this guard the f-string below raises
        # `'NoneType' object has no attribute 'isoformat'`, which then
        # propagates up to the recurring research_sync runner and writes
        # `state="error"` to the runtime file — turning a single bad row
        # into a desk-wide critical health status.
        if from_date is None or to_date is None:
            logger.debug(
                f"_fetch_candles_from_upstox: skipping {instrument_key} "
                f"because from_date={from_date} or to_date={to_date} is None"
            )
            return []

        encoded_key = urllib.parse.quote(instrument_key, safe="")
        is_expired_key = instrument_key.count("|") >= 2
        if is_expired_key:
            url = (
                f"{UPSTOX_BASE}/expired-instruments/historical-candle"
                f"/{encoded_key}/{CANDLE_INTERVAL}"
                f"/{to_date.isoformat()}/{from_date.isoformat()}"
            )
        else:
            url = (
                f"{UPSTOX_BASE}/historical-candle"
                f"/{encoded_key}/{CANDLE_INTERVAL}"
                f"/{to_date.isoformat()}/{from_date.isoformat()}"
            )

        async with self._semaphore:
            await self._throttle()
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    self._record_api_call("expired_historical_candle" if is_expired_key else "historical_candle")
                    resp = await client.get(url, headers=self.headers)

                if resp.status_code == 429:
                    self.rate_limit_hits += 1
                    if retry_count >= MAX_429_RETRIES:
                        logger.warning(
                            f"Skipping {instrument_key} after {retry_count} rate-limit retries"
                        )
                        return []
                    backoff = min(30, 5 * (retry_count + 1))
                    self.rate_limit_backoff_seconds += backoff
                    logger.warning(
                        f"Rate limited for {instrument_key} — sleeping {backoff}s "
                        f"(retry {retry_count + 1}/{MAX_429_RETRIES})"
                    )
                    await asyncio.sleep(backoff)
                    return await self._fetch_candles_from_upstox(
                        instrument_key,
                        from_date,
                        to_date,
                        retry_count=retry_count + 1,
                    )

                # Auth failed — regular historical data often works without auth,
                # but expired historical data requires the connected plan.
                if not is_expired_key and resp.status_code in (400, 401, 403):
                    logger.debug(
                        f"Auth error {resp.status_code} for {instrument_key} — retrying without auth"
                    )
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        self._record_api_call("historical_candle")
                        resp = await client.get(url, headers={"Accept": "application/json"})

                if is_expired_key and resp.status_code in (401, 403):
                    raise UpstoxAuthError(
                        f"Expired candle API rejected the Upstox token for {instrument_key} "
                        f"(HTTP {resp.status_code})."
                    )

                if resp.status_code != 200:
                    logger.debug(
                        f"HTTP {resp.status_code} for {instrument_key}: {resp.text[:120]}"
                    )
                    return []

                data = resp.json()
                raw_candles = data.get("data", {}).get("candles", [])
                if not raw_candles:
                    return []

                # Upstox returns newest first; reverse to chronological order
                raw_candles = list(reversed(raw_candles))
                candles = []
                for c in raw_candles:
                    try:
                        candles.append(
                            {
                                "time": str(c[_TS]),
                                "open": float(c[_O]),
                                "high": float(c[_H]),
                                "low": float(c[_L]),
                                "close": float(c[_C]),
                                "volume": int(c[_V]),
                                "oi": int(c[_OI]),
                            }
                        )
                    except Exception as exc:
                        logger.debug(f"Malformed candle skipped: {exc}")

                return candles

            except UpstoxAuthError:
                raise
            except Exception as exc:
                logger.warning(f"Error fetching candles for {instrument_key}: {exc}")
                return []

    # ── ICICI Breeze data source ───────────────────────────────────────────────

    @staticmethod
    def _get_breeze_adapter():
        """
        Return the connected ICICIBreezeAdapter instance, or None if not connected.
        Imports lazily to avoid circular import at module level.
        """
        try:
            from api.routers.auth import _active_brokers  # type: ignore
            info = _active_brokers.get("icici_breeze")
            if info and info.get("adapter"):
                return info["adapter"]
        except Exception:
            pass
        return None

    @staticmethod
    def _to_breeze_date(d: date) -> str:
        """Format a date as Breeze API expects: '2025-01-30T07:00:00.000Z'"""
        return f"{d.isoformat()}T07:00:00.000Z"

    async def _fetch_candles_from_breeze(
        self,
        underlying: str,
        expiry: date,
        strike: float,
        option_type: str,
        from_date: date,
        to_date: date,
    ) -> list[dict]:
        """
        Fetch 30-minute option candles from ICICI Breeze.

        Breeze has up to 3 years of F&O historical data including expired contracts —
        this is the primary source for backtesting on expired options.

        Parameters
        ----------
        underlying : str
            Underlying symbol e.g. "NIFTY", "RELIANCE"
        expiry : date
            Option expiry date
        strike : float
            Strike price
        option_type : str
            "CE" or "PE"
        from_date, to_date : date
            Date range for candles

        Returns
        -------
        list[dict]
            Candles as dicts {time, open, high, low, close, volume, oi},
            sorted chronologically. Empty if Breeze unavailable or on error.
        """
        adapter = self._get_breeze_adapter()
        if adapter is None:
            return []

        # Resolve stock_code for this underlying
        breeze_meta = BREEZE_INDEX_CODES.get(underlying)
        if breeze_meta:
            stock_code = breeze_meta["stock_code"]
        else:
            # For F&O stocks, stock_code = trading symbol
            stock_code = underlying

        right = BREEZE_RIGHT_MAP.get(option_type, option_type.lower())
        strike_str = str(int(strike))
        expiry_str = self._to_breeze_date(expiry)
        from_str = self._to_breeze_date(from_date)
        to_str = self._to_breeze_date(to_date)

        label = f"{underlying} {expiry} {int(strike)}{option_type}"
        logger.debug(
            f"Breeze fetch: {label} | {from_date} → {to_date} "
            f"| stock_code={stock_code} right={right} strike={strike_str}"
        )

        try:
            loop = asyncio.get_event_loop()
            rows = await loop.run_in_executor(
                None,
                lambda: adapter._get_breeze().get_historical_data_v2(
                    interval="30minute",
                    from_date=from_str,
                    to_date=to_str,
                    stock_code=stock_code,
                    exchange_code="NFO",
                    product_type="options",
                    expiry_date=expiry_str,
                    right=right,
                    strike_price=strike_str,
                )
            )

            if isinstance(rows, dict):
                rows = rows.get("Success") or rows.get("success") or []
            if not rows:
                logger.debug(f"Breeze: no data for {label}")
                return []

            candles = []
            for r in rows:
                try:
                    dt_str = r.get("datetime", "") or r.get("date", "")
                    candles.append({
                        "time": dt_str,
                        "open":   float(r.get("open",  0)),
                        "high":   float(r.get("high",  0)),
                        "low":    float(r.get("low",   0)),
                        "close":  float(r.get("close", 0)),
                        "volume": int(float(r.get("volume", 0))),
                        "oi":     int(float(r.get("open_interest", 0))),
                    })
                except Exception as exc:
                    logger.debug(f"Breeze malformed candle skipped: {exc}")

            # Sort chronologically (Breeze may return any order)
            candles.sort(key=lambda c: c["time"])
            logger.info(f"Breeze: {len(candles)} candles for {label}")
            return candles

        except Exception as exc:
            logger.warning(f"Breeze fetch failed for {label}: {exc}")
            return []

    async def _get_spot_price_from_breeze(
        self, underlying: str, target_date: date
    ) -> float:
        """
        Fallback: fetch closing spot/equity price from ICICI Breeze for stock underlyings.
        Used when Upstox futures instrument key is unavailable (expired contract).
        """
        adapter = self._get_breeze_adapter()
        if adapter is None:
            return 0.0

        # For index underlyings use NFO futures; for stocks use NSE equity
        breeze_meta = BREEZE_INDEX_CODES.get(underlying)
        if breeze_meta:
            exchange_code = "NSE"  # index spot via NSE cash segment
        else:
            exchange_code = "NSE"

        # Fetch a 5-day window
        from_dt = target_date - timedelta(days=5)
        to_dt = target_date
        from_str = self._to_breeze_date(from_dt)
        to_str = self._to_breeze_date(to_dt)

        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: adapter._get_breeze().get_historical_data_v2(
                    interval="1day",
                    from_date=from_str,
                    to_date=to_str,
                    stock_code=underlying,
                    exchange_code=exchange_code,
                    product_type="cash",
                    expiry_date="",
                    right="",
                    strike_price="",
                )
            )
            rows = (resp or {}).get("Success") or []
            if not rows:
                return 0.0
            # Sort and take the last row (closest to target_date)
            rows.sort(key=lambda r: r.get("datetime", ""))
            last = rows[-1]
            close = float(last.get("close", 0))
            logger.debug(f"Breeze spot for {underlying} on {target_date}: {close}")
            return close
        except Exception as exc:
            logger.debug(f"Breeze spot fetch failed for {underlying}: {exc}")
            return 0.0

    # ── Option candles (DB → Upstox active master → Upstox expired API → Breeze) ─

    async def _get_option_candles(
        self,
        underlying: str,
        expiry: date,
        strike: float,
        option_type: str,
        from_date: date,
        to_date: date,
        instrument_key: Optional[str] = None,
    ) -> list[dict]:
        """
        Retrieve 30-minute option candles. Data source priority:

        1. TimescaleDB (cached from previous runs)
        2. Upstox expired-instruments historical-candle API
           (works for expired contracts within the last 1 year; requires Upstox Plus plan)
        3. ICICI Breeze get_historical_data_v2
           (fallback when Upstox Plus not available; requires valid Breeze connection)

        Parameters
        ----------
        underlying : str
            Index/stock name e.g. "NIFTY"
        expiry : date
            Contract expiry
        strike : float
            Strike price
        option_type : str
            "CE" or "PE"
        from_date : date
            Range start
        to_date : date
            Range end (usually expiry date)

        Returns
        -------
        list[dict]
            Chronologically sorted candles. Empty if unavailable.
        """
        label = f"{underlying} {expiry} {int(strike)}{option_type}"

        # 1. Try DB first (fast — avoids API calls for repeated runs)
        db_candles = await self._get_candles_from_db(
            underlying, expiry, strike, option_type, from_date, to_date, instrument_key
        )
        if len(db_candles) >= MIN_CANDLES_REQUIRED:
            logger.debug(f"DB hit: {len(db_candles)} candles for {label}")
            return db_candles

        # 2. Upstox expired contract candles
        logger.debug(f"DB miss for {label} — fetching Upstox expired candles")
        if instrument_key:
            candles = await self._fetch_candles_from_upstox(
                instrument_key, from_date, to_date
            )
            if len(candles) >= MIN_CANDLES_REQUIRED:
                logger.info(f"Upstox: {len(candles)} candles for {label}")
                return candles
            if candles:
                logger.debug(
                    f"Upstox returned only {len(candles)} candles for {label} "
                    f"(need {MIN_CANDLES_REQUIRED})"
                )
        else:
            candles = []
            logger.debug(f"No instrument key found for {label}")

        # 3. ICICI Breeze fallback (when Upstox lacks data or Plus plan not available)
        logger.debug(f"Trying Breeze as fallback for {label}")
        breeze_candles = await self._fetch_candles_from_breeze(
            underlying, expiry, strike, option_type, from_date, to_date
        )
        if len(breeze_candles) >= MIN_CANDLES_REQUIRED:
            logger.info(f"Breeze fallback: {len(breeze_candles)} candles for {label}")
            return breeze_candles

        # Return whichever source gave more data (may still be below threshold)
        best = breeze_candles if len(breeze_candles) > len(candles) else candles
        if best:
            logger.debug(f"Best partial data: {len(best)} candles for {label}")
        else:
            logger.debug(f"No candles from any source for {label}")
        return best

    @staticmethod
    def _resample_candles(candles: list[dict], timeframe: str) -> list[dict]:
        """
        Resample 30-minute candles to a coarser timeframe.

        Currently supports:
          - 30m: no change
          - 1h: pairwise merge of consecutive 30-minute bars within each trading day
        """
        normalized = timeframe.strip().lower()
        if normalized in ("30m", "30min", "30minute"):
            return candles
        if normalized not in ("1h", "60m", "60min", "1hour"):
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        resampled: list[dict] = []
        by_day: dict[date, list[dict]] = defaultdict(list)
        for candle in candles:
            ts = datetime.fromisoformat(str(candle["time"]).replace("Z", "+00:00"))
            by_day[ts.date()].append(candle)

        for trading_day in sorted(by_day):
            day_candles = sorted(
                by_day[trading_day],
                key=lambda item: datetime.fromisoformat(
                    str(item["time"]).replace("Z", "+00:00")
                ),
            )
            for idx in range(0, len(day_candles) - 1, 2):
                pair = day_candles[idx: idx + 2]
                if len(pair) < 2:
                    continue
                first = pair[0]
                last = pair[-1]
                resampled.append(
                    {
                        "time": first["time"],
                        "open": float(first["open"]),
                        "high": max(float(item["high"]) for item in pair),
                        "low": min(float(item["low"]) for item in pair),
                        "close": float(last["close"]),
                        "volume": sum(int(item.get("volume", 0)) for item in pair),
                        "oi": int(last.get("oi", 0)),
                    }
                )

        return resampled

    # ── Results aggregation ────────────────────────────────────────────────────

    def _build_results(self, all_trades: list[dict]) -> dict:
        """
        Aggregate all individual trade records into a structured summary.

        Parameters
        ----------
        all_trades : list[dict]
            List of trade dicts produced by the run() loop

        Returns
        -------
        dict
            Structured results with total counts, per-underlying stats,
            monthly breakdown, and exit analysis
        """
        if not all_trades:
            return {
                "total_opportunities": 0,
                "by_underlying": {},
                "by_month": {},
                "exit_analysis": {
                    "best_strategy": "insufficient_data",
                    "strategy_ranking": [],
                },
                "all_trades": [],
            }

        total = len(all_trades)

        # ── By underlying ──
        by_underlying: dict[str, dict] = {}
        for trade in all_trades:
            und = trade.get("underlying", "UNKNOWN")
            if und not in by_underlying:
                by_underlying[und] = {
                    "opportunities": 0,
                    "total_max_return": 0.0,
                    "total_held_return": 0.0,
                    "target_50_hits": 0,
                    "target_100_hits": 0,
                    "best_trades": [],
                    "monthly_breakdown": {},
                }
            rec = by_underlying[und]
            rec["opportunities"] += 1
            rec["total_max_return"] += trade.get("max_return_pct", 0.0)
            rec["total_held_return"] += trade.get("held_return_pct", 0.0)
            if trade.get("target_50pct_hit"):
                rec["target_50_hits"] += 1
            if trade.get("target_100pct_hit"):
                rec["target_100_hits"] += 1

            # Monthly breakdown
            month_key = trade.get("expiry_month", "")[:7]  # "YYYY-MM"
            if month_key:
                mb = rec["monthly_breakdown"]
                if month_key not in mb:
                    mb[month_key] = {"count": 0, "total_max_return": 0.0}
                mb[month_key]["count"] += 1
                mb[month_key]["total_max_return"] += trade.get("max_return_pct", 0.0)

        # Compute averages per underlying
        for und, rec in by_underlying.items():
            opp = rec["opportunities"]
            rec["avg_max_return"] = round(rec["total_max_return"] / opp, 4) if opp else 0.0
            rec["avg_held_return"] = round(rec["total_held_return"] / opp, 4) if opp else 0.0

            # Sort best trades by max_return_pct desc, keep top 10
            all_for_und = [t for t in all_trades if t.get("underlying") == und]
            all_for_und.sort(key=lambda t: t.get("max_return_pct", 0.0), reverse=True)
            rec["best_trades"] = all_for_und[:10]

            # Monthly breakdown: compute averages
            for month_key, mb in rec["monthly_breakdown"].items():
                cnt = mb["count"]
                mb["avg_max_return"] = (
                    round(mb["total_max_return"] / cnt, 4) if cnt else 0.0
                )

            # Clean up internal running totals from the exposed dict
            del rec["total_max_return"]
            del rec["total_held_return"]
            del rec["target_50_hits"]
            del rec["target_100_hits"]

        # Re-build by_underlying with cleaner structure including hit rates
        for und, rec in by_underlying.items():
            opp = rec["opportunities"]
            t50 = sum(1 for t in all_trades if t.get("underlying") == und and t.get("target_50pct_hit"))
            t100 = sum(1 for t in all_trades if t.get("underlying") == und and t.get("target_100pct_hit"))
            rec["target_50_hit_rate"] = round(t50 / opp * 100, 2) if opp else 0.0
            rec["target_100_hit_rate"] = round(t100 / opp * 100, 2) if opp else 0.0

        # ── By month (global) ──
        by_month: dict[str, dict] = {}
        for trade in all_trades:
            month_key = trade.get("expiry_month", "")[:7]
            if not month_key:
                continue
            if month_key not in by_month:
                by_month[month_key] = {"count": 0, "total_max_return": 0.0}
            by_month[month_key]["count"] += 1
            by_month[month_key]["total_max_return"] += trade.get("max_return_pct", 0.0)

        for month_key, mb in by_month.items():
            cnt = mb["count"]
            mb["avg_max_return"] = round(mb["total_max_return"] / cnt, 4) if cnt else 0.0
            del mb["total_max_return"]

        # ── Exit analysis ──
        total_held_returns = [t.get("held_return_pct", 0.0) for t in all_trades]
        total_max_returns = [t.get("max_return_pct", 0.0) for t in all_trades]
        t50_hits = sum(1 for t in all_trades if t.get("target_50pct_hit"))
        t100_hits = sum(1 for t in all_trades if t.get("target_100pct_hit"))

        hold_to_expiry_avg = sum(total_held_returns) / total if total else 0.0
        target_50_hit_rate = t50_hits / total * 100.0 if total else 0.0
        target_100_hit_rate = t100_hits / total * 100.0 if total else 0.0
        max_move_avg = sum(total_max_returns) / total if total else 0.0

        strategy_buckets: dict[str, list[float]] = defaultdict(list)
        for trade in all_trades:
            for name, return_pct in (trade.get("strategy_returns") or {}).items():
                strategy_buckets[name].append(float(return_pct))

        strategy_ranking: list[dict] = []
        for name, returns in strategy_buckets.items():
            avg_return = sum(returns) / len(returns)
            med_return = median(returns)
            positive_pct = sum(1 for r in returns if r > 0) / len(returns) * 100.0
            capture_pct = (avg_return / max_move_avg * 100.0) if max_move_avg else 0.0
            strategy_ranking.append(
                {
                    "strategy": name,
                    "trades": len(returns),
                    "avg_return_pct": round(avg_return, 4),
                    "median_return_pct": round(med_return, 4),
                    "positive_pct": round(positive_pct, 2),
                    "avg_mfe_capture_pct": round(capture_pct, 2),
                }
            )

        strategy_ranking.sort(
            key=lambda row: (
                row["avg_return_pct"],
                row["median_return_pct"],
                row["positive_pct"],
            ),
            reverse=True,
        )
        best_strategy = (
            strategy_ranking[0]["strategy"] if strategy_ranking else "insufficient_data"
        )

        exit_analysis = {
            "hold_to_expiry_avg": round(hold_to_expiry_avg, 4),
            "max_move_avg": round(max_move_avg, 4),
            "target_50_hit_rate": round(target_50_hit_rate, 2),
            "target_100_hit_rate": round(target_100_hit_rate, 2),
            "best_strategy": best_strategy,
            "strategy_ranking": strategy_ranking,
            "total_opportunities": total,
            "hold_to_expiry_positive_pct": round(
                sum(1 for r in total_held_returns if r > 0) / total * 100, 2
            ) if total else 0.0,
            "max_move_positive_pct": round(
                sum(1 for r in total_max_returns if r > 0) / total * 100, 2
            ) if total else 0.0,
        }

        return {
            "total_opportunities": total,
            "by_underlying": by_underlying,
            "by_month": dict(sorted(by_month.items())),
            "exit_analysis": exit_analysis,
            "all_trades": all_trades,
        }

    # ── Main backtest runner ───────────────────────────────────────────────────

    async def run(
        self,
        underlyings: list[str],
        from_date: date,
        to_date: date,
        timeframe: str = "30m",
        progress_cb: Optional[Callable[[dict], None]] = None,
    ) -> dict:
        """
        Run MACD zero-line crossover backtest across all underlyings and months.

        Parameters
        ----------
        underlyings : list[str]
            List of underlying names e.g. ["NIFTY", "BANKNIFTY"]
        from_date : date
            Backtest start date
        to_date : date
            Backtest end date
        progress_cb : Callable[[dict], None] | None
            Optional callback invoked with progress dict at each step

        Returns
        -------
        dict
            Aggregated backtest results (see _build_results)
        """
        logger.info(
            f"MACD Backtest starting: underlyings={underlyings}, "
            f"{from_date} → {to_date}, timeframe={timeframe}"
        )

        all_trades: list[dict] = []

        expiry_map: dict[str, list[date]] = {}
        previous_expiry_map: dict[str, dict[date, Optional[date]]] = {}
        earliest_anchor: Optional[date] = None
        total_work = 0

        for underlying in underlyings:
            expiry_dates = await self._fetch_expiry_dates(underlying)
            monthly_expiries, prev_map = self._select_monthly_expiries(
                expiry_dates,
                from_date,
                to_date,
            )
            expiry_map[underlying] = monthly_expiries
            previous_expiry_map[underlying] = prev_map
            total_work += len(monthly_expiries) * 2
            if monthly_expiries:
                first_expiry = monthly_expiries[0]
                prev_expiry = prev_map.get(first_expiry)
                if prev_expiry is not None:
                    anchor = get_first_trading_day_after(prev_expiry)
                    if earliest_anchor is None or anchor < earliest_anchor:
                        earliest_anchor = anchor

        if total_work == 0:
            logger.warning(f"No monthly expiries found between {from_date} and {to_date}")
            return self._build_results([])

        self._spot_series_from = (earliest_anchor or from_date) - timedelta(days=7)
        self._spot_series_to = to_date
        completed = 0

        def _emit_progress(msg: str) -> None:
            nonlocal completed
            completed += 1
            pct = round(completed / total_work * 100, 1) if total_work else 0.0
            if progress_cb:
                try:
                    progress_cb(
                        {
                            "completed": completed,
                            "total": total_work,
                            "pct": pct,
                            "message": msg,
                            "trades_found": len(all_trades),
                        }
                    )
                except Exception:
                    pass
            if completed % 10 == 0 or completed == total_work:
                logger.info(f"Backtest progress: {completed}/{total_work} ({pct}%) — {msg}")

        for underlying in underlyings:
            expiries = expiry_map.get(underlying, [])
            logger.info(f"Processing {underlying} — {len(expiries)} expiries")

            for expiry in expiries:
                prev_expiry = previous_expiry_map.get(underlying, {}).get(expiry)
                if prev_expiry is None:
                    for _ in ("CE", "PE"):
                        _emit_progress(f"{underlying} {expiry} — missing previous monthly expiry")
                    continue

                selection_date = get_first_trading_day_after(prev_expiry)
                spot, spot_date = await self._get_spot_reference(
                    underlying, selection_date
                )
                if spot <= 0:
                    fallback_spot = await self._get_spot_price_from_breeze(
                        underlying, selection_date
                    )
                    if fallback_spot > 0:
                        spot = fallback_spot
                        spot_date = selection_date
                if spot <= 0:
                    logger.warning(
                        f"Could not get spot for {underlying} on {selection_date} "
                        f"(expiry {expiry}) — skipping"
                    )
                    for _ in ("CE", "PE"):
                        _emit_progress(f"{underlying} {expiry} — no spot data")
                    continue

                contract_selection = await self._select_atm_contracts(
                    underlying, expiry, spot
                )
                if not contract_selection:
                    logger.warning(
                        f"No expired contracts available for {underlying} {expiry}"
                    )
                    for _ in ("CE", "PE"):
                        _emit_progress(f"{underlying} {expiry} — no expired contracts")
                    continue

                reference_strike = float(contract_selection.get("atm_strike", 0.0))
                candle_fetch_start = expiry - timedelta(days=365)
                logger.debug(
                    f"{underlying} expiry={expiry}: spot={spot}, "
                    f"selection_date={selection_date}, reference_atm={reference_strike}"
                )

                for option_type in ("CE", "PE"):
                    contract = contract_selection.get(option_type)
                    if not contract:
                        _emit_progress(f"{underlying} {expiry} {option_type} — no contract")
                        continue

                    contract_key = str(contract.get("instrument_key", ""))
                    contract_strike = float(
                        contract.get("strike_price")
                        or contract.get("strike")
                        or reference_strike
                        or 0.0
                    )
                    label = f"{underlying} {expiry} {int(contract_strike)}{option_type}"
                    try:
                        candles = await self._get_option_candles(
                            underlying,
                            expiry,
                            contract_strike,
                            option_type,
                            from_date=candle_fetch_start,
                            to_date=expiry,
                            instrument_key=contract_key,
                        )
                        candles = self._resample_candles(candles, timeframe)

                        if len(candles) < MIN_CANDLES_REQUIRED:
                            logger.debug(
                                f"Insufficient candles for {label}: "
                                f"{len(candles)} < {MIN_CANDLES_REQUIRED} — skipping"
                            )
                            _emit_progress(f"{label} — insufficient candles ({len(candles)})")
                            continue

                        # Compute MACD
                        closes = [c["close"] for c in candles]
                        macd_line, signal_line, histogram = compute_macd(closes)

                        # Find zero-line buy crossovers, but only after the contract
                        # was selected on the first trading day after the prior expiry.
                        crossover_indices = [
                            idx
                            for idx in find_zero_crossovers(macd_line)
                            if datetime.fromisoformat(
                                str(candles[idx]["time"]).replace("Z", "+00:00")
                            ).date() >= selection_date
                        ]

                        if not crossover_indices:
                            logger.debug(f"No MACD crossovers for {label}")
                            _emit_progress(f"{label} — no crossovers found")
                            continue

                        logger.debug(
                            f"{label}: {len(crossover_indices)} crossover(s) "
                            f"from {len(candles)} candles"
                        )

                        # Analyse each crossover
                        for cidx in crossover_indices:
                            try:
                                trade_analysis = analyze_trade(candles, cidx)
                                strategy_results = simulate_exit_strategies(candles, cidx)
                                strategy_returns = {
                                    name: result["return_pct"]
                                    for name, result in strategy_results.items()
                                }
                                best_strategy_name, best_strategy = max(
                                    strategy_results.items(),
                                    key=lambda item: item[1]["return_pct"],
                                )
                                trade_record = {
                                    "underlying": underlying,
                                    "expiry": expiry.isoformat(),
                                    "expiry_month": expiry.isoformat(),
                                    "strike": contract_strike,
                                    "option_type": option_type,
                                    "selection_date": selection_date.isoformat(),
                                    "spot_reference_date": (
                                        spot_date.isoformat() if spot_date else None
                                    ),
                                    "spot_at_selection": round(spot, 2),
                                    "atm_strike": reference_strike,
                                    "contract_instrument_key": contract_key,
                                    "contract_trading_symbol": contract.get("trading_symbol"),
                                    "history_start_time": candles[0]["time"],
                                    "total_candles": len(candles),
                                    "crossover_count": len(crossover_indices),
                                    "strategy_returns": strategy_returns,
                                    "best_exit_strategy": best_strategy_name,
                                    "best_exit_return_pct": best_strategy["return_pct"],
                                    **trade_analysis,
                                }
                                all_trades.append(trade_record)
                            except Exception as exc:
                                logger.warning(
                                    f"Trade analysis failed for {label} at idx {cidx}: {exc}"
                                )

                        _emit_progress(
                            f"{label} — {len(crossover_indices)} crossover(s)"
                        )

                    except Exception as exc:
                        logger.error(f"Error processing {label}: {exc}")
                        _emit_progress(f"{label} — error: {str(exc)[:60]}")

        logger.info(
            f"MACD Backtest complete: {len(all_trades)} trades from "
            f"{len(underlyings)} underlyings × {len(expiries)} expiries"
        )

        results = self._build_results(all_trades)
        results["timeframe"] = timeframe
        return results
