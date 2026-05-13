"""
Upstox NSE F&O Historical Options Data Downloader
==================================================
Downloads historical OHLCV candles for expired and active NSE F&O option
contracts from Upstox's historical-candle API and stores them in the
option_premium_candles TimescaleDB hypertable.

Upstox provides:
  - 1-minute  candles: last ~2 years
  - 30-minute candles: last ~2 years
  - Daily candles:     last ~10 years

The instrument master is fetched from Upstox's public CDN (no auth needed).
Historical candles require a valid Upstox Bearer token.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, AsyncIterator, Callable, Optional

import httpx
from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import AsyncSessionLocal

# ── Constants ─────────────────────────────────────────────────────────────────

# Upstox provides instruments via both CDN (may require auth) and API
# We use the API endpoint as primary (requires Bearer token)
INSTRUMENT_API_URL = "https://api.upstox.com/v2/market-quote/instruments"
# CDN fallback (gzipped JSON, may be rate-limited or restricted)
INSTRUMENT_MASTER_URL = (
    "https://assets.upstox.com/market-quote/instruments/exchange/NSE_FO.json.gz"
)
UPSTOX_BASE = "https://api.upstox.com/v2"

# Upstox allows ~10 req/sec on historical endpoint; we stay conservative.
RATE_LIMIT_PER_SEC: float = 5.0

# Candle array index map from Upstox response
# [timestamp, open, high, low, close, volume, oi]
_TS, _O, _H, _L, _C, _V, _OI = 0, 1, 2, 3, 4, 5, 6

INDEX_UNDERLYINGS = {
    "NIFTY":       {"lot_size": 25,  "strike_step": 50},
    "BANKNIFTY":   {"lot_size": 15,  "strike_step": 100},
    "FINNIFTY":    {"lot_size": 40,  "strike_step": 50},
    "MIDCPNIFTY":  {"lot_size": 75,  "strike_step": 25},
    "SENSEX":      {"lot_size": 10,  "strike_step": 100},
    "BANKEX":      {"lot_size": 15,  "strike_step": 100},
}


# ── Progress tracking ─────────────────────────────────────────────────────────

@dataclass
class DownloadProgress:
    task_id: str
    status: str = "pending"          # pending | running | done | error
    total_instruments: int = 0
    processed: int = 0
    skipped: int = 0
    stored_candles: int = 0
    current_symbol: str = ""
    error: str = ""
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    @property
    def pct(self) -> float:
        if self.total_instruments == 0:
            return 0.0
        return round(self.processed / self.total_instruments * 100, 1)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "total_instruments": self.total_instruments,
            "processed": self.processed,
            "skipped": self.skipped,
            "stored_candles": self.stored_candles,
            "pct": self.pct,
            "current_symbol": self.current_symbol,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "elapsed_secs": (
                (self.finished_at or datetime.utcnow()) - self.started_at
            ).seconds if self.started_at else 0,
        }


# ── Downloader ────────────────────────────────────────────────────────────────

class UpstoxFODownloader:
    """
    Downloads NSE F&O historical options data from Upstox.

    Usage
    -----
    downloader = UpstoxFODownloader(access_token="Bearer <token>")
    progress   = DownloadProgress(task_id="xyz")
    await downloader.run(
        underlyings=["NIFTY", "BANKNIFTY"],
        from_date=date(2025, 1, 1),
        to_date=date(2026, 3, 28),
        interval="30minute",
        progress=progress,
    )
    """

    def __init__(self, access_token: str):
        """
        Parameters
        ----------
        access_token : str
            Upstox bearer token — just the token string (not 'Bearer ...')
        """
        self.access_token = access_token
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        self._instrument_cache: Optional[list[dict]] = None
        self._last_call_ts: float = 0.0

    # ── Instrument master ──────────────────────────────────────────────────────

    async def _fetch_instrument_master(self) -> list[dict]:
        """
        Fetch NSE_FO instruments. Tries:
          1. Upstox API endpoint (requires auth token)
          2. Public CDN URL (gzip-compressed JSON)
        """
        # Strategy 1: Upstox API (requires auth)
        if self.access_token:
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.get(
                        f"{UPSTOX_BASE}/market-quote/instruments",
                        params={"exchange": "NSE_FO"},
                        headers=self.headers,
                    )
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("data", [])
                    if items:
                        logger.info(f"Loaded {len(items):,} instruments from Upstox API")
                        return items
            except Exception as exc:
                logger.debug(f"API instrument fetch failed: {exc}")

        # Strategy 2: CDN with browser-like headers
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; NomadCurie/1.0)",
            "Accept": "application/json, application/octet-stream",
        }
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            # Try NSEFO JSON directly
            for url in [
                INSTRUMENT_MASTER_URL,
                "https://assets.upstox.com/market-quote/instruments/exchange/NSE_FO.json",
            ]:
                try:
                    resp = await client.get(url, headers=headers)
                    if resp.status_code == 200:
                        raw = resp.content
                        try:
                            raw = gzip.decompress(raw)
                        except Exception:
                            pass
                        return json.loads(raw)
                except Exception as exc:
                    logger.debug(f"CDN fetch {url} failed: {exc}")

        # Strategy 3: Upstox instruments CSV (alternative endpoint)
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.get(
                    "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz",
                    headers=headers,
                )
                if resp.status_code == 200:
                    raw = resp.content
                    try:
                        raw = gzip.decompress(raw)
                    except Exception:
                        pass
                    all_instruments = json.loads(raw)
                    # Filter to NSE_FO only
                    return [i for i in all_instruments if "NSE_FO" in i.get("exchange", "")]
        except Exception as exc:
            logger.debug(f"Complete CDN fetch failed: {exc}")

        raise RuntimeError(
            "Could not fetch Upstox NSE_FO instrument master. "
            "Ensure Upstox is connected and access_token is valid."
        )

    # ── Rate limiting ──────────────────────────────────────────────────────────

    async def _throttle(self) -> None:
        """Enforce RATE_LIMIT_PER_SEC requests per second."""
        elapsed = time.monotonic() - self._last_call_ts
        gap = 1.0 / RATE_LIMIT_PER_SEC
        if elapsed < gap:
            await asyncio.sleep(gap - elapsed)
        self._last_call_ts = time.monotonic()

    # ── Instrument master ──────────────────────────────────────────────────────

    async def get_fo_instruments(
        self,
        underlyings: list[str] | None = None,
        option_types: list[str] = ("CE", "PE"),
        from_expiry: date | None = None,
        to_expiry: date | None = None,
    ) -> list[dict]:
        """
        Fetch NSE_FO instrument master from Upstox and filter.

        Returns a list of dicts with keys:
            instrument_key, tradingsymbol, name, expiry, strike, instrument_type, lot_size
        """
        if self._instrument_cache is None:
            logger.info("Fetching Upstox NSE_FO instrument master…")
            instruments = await self._fetch_instrument_master()
            self._instrument_cache = instruments
            logger.info(f"Loaded {len(self._instrument_cache):,} NSE_FO instruments")

        results = []
        for inst in self._instrument_cache:
            # Filter instrument type (CE/PE, not FUT)
            itype = inst.get("instrument_type", "")
            if itype not in option_types:
                continue

            # Filter underlying (name field)
            name = inst.get("name", "")
            if underlyings and name not in underlyings:
                continue

            # Filter by expiry range
            expiry_str = inst.get("expiry")
            if expiry_str:
                try:
                    expiry = date.fromisoformat(expiry_str)
                    if from_expiry and expiry < from_expiry:
                        continue
                    if to_expiry and expiry > to_expiry:
                        continue
                except Exception:
                    pass

            results.append(inst)

        logger.info(
            f"Filtered to {len(results):,} instruments "
            f"(underlyings={underlyings}, types={list(option_types)})"
        )
        return results

    # ── Historical candles ─────────────────────────────────────────────────────

    async def fetch_candles(
        self,
        instrument_key: str,
        interval: str,
        from_date: date,
        to_date: date,
    ) -> list[list]:
        """
        Fetch OHLCV candles for one instrument from Upstox.

        Returns list of [timestamp, open, high, low, close, volume, oi]
        """
        # Same defensive guard as analysis/backtest._fetch_candles_from_upstox:
        # None dates would otherwise raise inside the f-string and bubble up
        # to the research-sync supervisor as a critical state.
        if from_date is None or to_date is None:
            logger.debug(
                f"fetch_candles: skipping {instrument_key} ({interval}) "
                f"because from_date={from_date} or to_date={to_date} is None"
            )
            return []
        await self._throttle()
        url = (
            f"{UPSTOX_BASE}/historical-candle"
            f"/{instrument_key}/{interval}"
            f"/{to_date.isoformat()}/{from_date.isoformat()}"
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=self.headers)

        if resp.status_code == 429:
            logger.warning("Rate limited by Upstox — sleeping 5s")
            await asyncio.sleep(5)
            return await self.fetch_candles(instrument_key, interval, from_date, to_date)

        if resp.status_code != 200:
            logger.debug(f"HTTP {resp.status_code} for {instrument_key}: {resp.text[:120]}")
            return []

        data = resp.json()
        return data.get("data", {}).get("candles", [])

    # ── Database storage ───────────────────────────────────────────────────────

    async def _store_candles(
        self,
        session: AsyncSession,
        inst: dict,
        candles: list[list],
    ) -> int:
        """
        Bulk-upsert candles into option_premium_candles.
        Returns number of rows inserted.
        """
        if not candles:
            return 0

        rows = []
        underlying = inst.get("name", "")
        expiry_str = inst.get("expiry")
        strike = inst.get("strike")
        otype = inst.get("instrument_type", "")
        lot_size = inst.get("lot_size", 1)

        for c in candles:
            try:
                ts_str = c[_TS]
                # Upstox timestamps: "2025-01-01T09:15:00+05:30"
                rows.append(
                    {
                        "time": ts_str,
                        "underlying": underlying,
                        "market": "NSE",
                        "expiry": expiry_str,
                        "strike": strike,
                        "option_type": otype,
                        "open": float(c[_O]),
                        "high": float(c[_H]),
                        "low": float(c[_L]),
                        "close": float(c[_C]),
                        "volume": int(c[_V]),
                        "oi": int(c[_OI]),
                        "iv": None,
                        "delta": None,
                        "underlying_price": None,
                    }
                )
            except Exception as exc:
                logger.debug(f"Skipping malformed candle: {exc}")

        if not rows:
            return 0

        # Use ON CONFLICT DO NOTHING for idempotent re-runs
        stmt = text("""
            INSERT INTO option_premium_candles
                (time, underlying, market, expiry, strike, option_type,
                 open, high, low, close, volume, oi, iv, delta, underlying_price)
            VALUES
                (:time, :underlying, :market, :expiry, :strike, :option_type,
                 :open, :high, :low, :close, :volume, :oi, :iv, :delta, :underlying_price)
            ON CONFLICT DO NOTHING
        """)
        await session.execute(stmt, rows)
        await session.commit()
        return len(rows)

    # ── Main orchestration ─────────────────────────────────────────────────────

    async def run(
        self,
        underlyings: list[str],
        from_date: date,
        to_date: date,
        interval: str = "30minute",
        option_types: list[str] = ("CE", "PE"),
        strike_range: Optional[tuple[float, float]] = None,
        progress: Optional[DownloadProgress] = None,
    ) -> DownloadProgress:
        """
        Full download pipeline.

        Parameters
        ----------
        underlyings : list[str]
            e.g. ["NIFTY", "BANKNIFTY"]
        from_date / to_date : date
            Historical date range to download
        interval : str
            "1minute" | "30minute" | "day" | "week" | "month"
        option_types : list[str]
            ["CE", "PE"] or just ["CE"]
        strike_range : tuple[float, float] | None
            (min_strike, max_strike) filter — None means all strikes
        progress : DownloadProgress | None
            Shared progress object for status tracking
        """
        if progress is None:
            import uuid
            progress = DownloadProgress(task_id=str(uuid.uuid4()))

        progress.status = "running"
        progress.started_at = datetime.utcnow()

        try:
            instruments = await self.get_fo_instruments(
                underlyings=underlyings,
                option_types=list(option_types),
                from_expiry=from_date,       # only instruments with expiry >= from_date
                to_expiry=to_date + timedelta(days=365),  # allow future expiries too
            )

            # Filter by strike range if provided
            if strike_range:
                lo, hi = strike_range
                instruments = [
                    i for i in instruments
                    if lo <= (i.get("strike") or 0) <= hi
                ]

            progress.total_instruments = len(instruments)
            logger.info(
                f"Starting download: {len(instruments):,} instruments, "
                f"{from_date} → {to_date}, interval={interval}"
            )

            async with AsyncSessionLocal() as session:
                for inst in instruments:
                    symbol = inst.get("tradingsymbol", "")
                    progress.current_symbol = symbol

                    try:
                        candles = await self.fetch_candles(
                            inst["instrument_key"],
                            interval,
                            from_date,
                            to_date,
                        )
                        n = await self._store_candles(session, inst, candles)
                        progress.stored_candles += n
                        if not candles:
                            progress.skipped += 1
                    except Exception as exc:
                        logger.warning(f"Error fetching {symbol}: {exc}")
                        progress.skipped += 1

                    progress.processed += 1

                    if progress.processed % 100 == 0:
                        logger.info(
                            f"Progress: {progress.processed}/{progress.total_instruments} "
                            f"({progress.pct}%) — {progress.stored_candles:,} candles stored"
                        )

            progress.status = "done"
            progress.current_symbol = ""
            progress.finished_at = datetime.utcnow()
            elapsed = (progress.finished_at - progress.started_at).seconds
            logger.info(
                f"Download complete: {progress.stored_candles:,} candles stored "
                f"in {elapsed}s for {progress.processed} instruments"
            )

        except Exception as exc:
            logger.error(f"Download failed: {exc}")
            progress.status = "error"
            progress.error = str(exc)
            progress.finished_at = datetime.utcnow()

        return progress


# ── DB helpers (for stats) ─────────────────────────────────────────────────────

async def get_stored_stats() -> dict:
    """Return summary stats of data already stored in option_premium_candles."""
    async with AsyncSessionLocal() as session:
        try:
            result = await session.execute(text("""
                SELECT
                    underlying,
                    option_type,
                    COUNT(*) AS candles,
                    MIN(time) AS earliest,
                    MAX(time) AS latest,
                    COUNT(DISTINCT expiry) AS expiries,
                    COUNT(DISTINCT strike) AS strikes
                FROM option_premium_candles
                GROUP BY underlying, option_type
                ORDER BY underlying, option_type
            """))
            rows = result.fetchall()
            return {
                "rows": [
                    {
                        "underlying": r.underlying,
                        "option_type": r.option_type,
                        "candles": r.candles,
                        "earliest": r.earliest.isoformat() if r.earliest else None,
                        "latest": r.latest.isoformat() if r.latest else None,
                        "expiries": r.expiries,
                        "strikes": r.strikes,
                    }
                    for r in rows
                ]
            }
        except Exception as exc:
            return {"rows": [], "error": str(exc)}
