"""Timeframe-aware sector rotation and stock-vs-sector RRG analytics."""
from __future__ import annotations

import asyncio
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from statistics import mean
from typing import Any, Optional
from urllib.parse import quote

import httpx
from loguru import logger
from sqlalchemy import bindparam, text

from db.database import AsyncSessionLocal
from db.redis_client import get_redis


UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
UPSTOX_BENCHMARK_SYMBOL = "NSE_INDEX|Nifty 50"
BENCHMARK_APP_SYMBOL = "NSE:NIFTY50-INDEX"


@dataclass(frozen=True)
class SectorConfig:
    code: str
    label: str
    app_symbol: str
    members: tuple[str, ...]
    upstox_symbol: Optional[str] = None


SECTOR_CONFIGS: tuple[SectorConfig, ...] = (
    SectorConfig(
        code="BANKING",
        label="Banking",
        app_symbol="NSE:NIFTYBANK-INDEX",
        upstox_symbol="NSE_INDEX|Nifty Bank",
        members=(
            "AUBANK", "AXISBANK", "BANDHANBNK", "BANKBARODA", "BANKINDIA",
            "CANBK", "FEDERALBNK", "HDFCBANK", "ICICIBANK", "IDFCFIRSTB",
            "INDIANB", "INDUSINDBK", "KOTAKBANK", "PNB", "RBLBANK", "SBIN",
            "UNIONBANK", "YESBANK",
        ),
    ),
    SectorConfig(
        code="FINANCIALS",
        label="Financials",
        app_symbol="SECTOR:FINANCIALS",
        members=(
            "360ONE", "ABCAPITAL", "ANGELONE", "BAJAJFINSV", "BAJAJHLDNG",
            "BAJFINANCE", "BSE", "CAMS", "CDSL", "CHOLAFIN", "HDFCAMC",
            "HDFCLIFE", "HUDCO", "ICICIGI", "ICICIPRULI", "IEX", "IREDA",
            "IRFC", "JIOFIN", "KFINTECH", "LICHSGFIN", "LICI", "LTF",
            "MANAPPURAM", "MCX", "MFSL", "MUTHOOTFIN", "NUVAMA", "PFC",
            "PNBHOUSING", "POLICYBZR", "RECLTD", "SAMMAANCAP", "SBICARD",
            "SBILIFE", "SHRIRAMFIN",
        ),
        upstox_symbol="NSE_INDEX|Nifty Financial Services",
    ),
    SectorConfig(
        code="IT",
        label="IT",
        app_symbol="NSE:NIFTYIT-INDEX",
        upstox_symbol="NSE_INDEX|Nifty IT",
        members=(
            "COFORGE", "HCLTECH", "INFY", "KPITTECH", "LTIM", "LTM",
            "MPHASIS", "NAUKRI", "OFSS", "PERSISTENT", "TATAELXSI",
            "TATATECH", "TCS", "TECHM", "WIPRO",
        ),
    ),
    SectorConfig(
        code="AUTO",
        label="Auto",
        app_symbol="NSE:NIFTYAUTO-INDEX",
        upstox_symbol="NSE_INDEX|Nifty Auto",
        members=(
            "ASHOKLEY", "BAJAJ-AUTO", "BHARATFORG", "BOSCHLTD",
            "EICHERMOT", "EXIDEIND", "HEROMOTOCO", "M&M", "MARUTI",
            "MOTHERSON", "SONACOMS", "TVSMOTOR", "UNOMINDA",
        ),
    ),
    SectorConfig(
        code="HEALTHCARE",
        label="Healthcare",
        app_symbol="NSE:NIFTYPHARMA-INDEX",
        upstox_symbol="NSE_INDEX|Nifty Pharma",
        members=(
            "ALKEM", "APOLLOHOSP", "AUROPHARMA", "BIOCON", "CIPLA",
            "DIVISLAB", "DRREDDY", "FORTIS", "GLENMARK", "LAURUSLABS",
            "LUPIN", "MANKIND", "MAXHEALTH", "PPLPHARMA", "SUNPHARMA",
            "SYNGENE", "TORNTPHARM", "ZYDUSLIFE",
        ),
    ),
    SectorConfig(
        code="FMCG",
        label="FMCG",
        app_symbol="NSE:NIFTYFMCG-INDEX",
        upstox_symbol="NSE_INDEX|Nifty FMCG",
        members=(
            "BRITANNIA", "COLPAL", "DABUR", "GODREJCP", "HINDUNILVR",
            "ITC", "MARICO", "NESTLEIND", "PATANJALI", "TATACONSUM",
            "UNITDSPR", "VBL",
        ),
    ),
    SectorConfig(
        code="METALS",
        label="Metals",
        app_symbol="NSE:NIFTYMETAL-INDEX",
        upstox_symbol="NSE_INDEX|Nifty Metal",
        members=(
            "COALINDIA", "HINDALCO", "HINDZINC", "JINDALSTEL", "JSWSTEEL",
            "NATIONALUM", "NMDC", "SAIL", "TATASTEEL", "VEDL",
        ),
    ),
    SectorConfig(
        code="ENERGY",
        label="Energy",
        app_symbol="NSE:NIFTYENERGY-INDEX",
        upstox_symbol="NSE_INDEX|Nifty Energy",
        members=(
            "ADANIENSOL", "ADANIGREEN", "ADANIPOWER", "BPCL", "GAIL",
            "INOXWIND", "IOC", "JSWENERGY", "NHPC", "NTPC", "OIL", "ONGC",
            "PETRONET", "POWERGRID", "POWERINDIA", "PREMIERENE",
            "RELIANCE", "SUZLON", "TATAPOWER", "TORNTPOWER", "WAAREEENER",
        ),
    ),
    SectorConfig(
        code="REALTY_INFRA",
        label="Realty & Infra",
        app_symbol="NSE:NIFTYREALTY-INDEX",
        upstox_symbol="NSE_INDEX|Nifty Realty",
        members=(
            "ADANIPORTS", "AMBUJACEM", "APLAPOLLO", "CONCOR", "DALBHARAT",
            "DELHIVERY", "DLF", "GMRAIRPORT", "GODREJPROP", "LODHA", "NBCC",
            "OBEROIRLTY", "PHOENIXLTD", "PRESTIGE", "RVNL", "SHREECEM",
            "ULTRACEMCO",
        ),
    ),
    SectorConfig(
        code="INDUSTRIALS",
        label="Industrials",
        app_symbol="SECTOR:INDUSTRIALS",
        members=(
            "ABB", "ADANIENT", "BDL", "BEL", "BHEL", "CGPOWER", "CUMMINSIND",
            "HAL", "KAYNES", "KEI", "LT", "MAZDOCK", "SIEMENS", "SOLARINDS",
            "TMPV",
        ),
    ),
    SectorConfig(
        code="MATERIALS",
        label="Materials",
        app_symbol="SECTOR:MATERIALS",
        members=(
            "ASTRAL", "ASIANPAINT", "GRASIM", "PIDILITIND", "PIIND", "SRF",
            "SUPREMEIND", "UPL",
        ),
    ),
    SectorConfig(
        code="CONSUMER_DURABLES",
        label="Consumer Durables",
        app_symbol="SECTOR:CONSUMER_DURABLES",
        members=(
            "AMBER", "BLUESTARCO", "CROMPTON", "DIXON", "DMART", "HAVELLS",
            "KALYANKJIL", "PAGEIND", "PGEL", "POLYCAB", "TITAN", "TRENT",
            "VOLTAS",
        ),
        upstox_symbol="NSE_INDEX|Nifty Consumer Durables",
    ),
    SectorConfig(
        code="DIGITAL_SERVICES",
        label="Digital & Services",
        app_symbol="SECTOR:DIGITAL_SERVICES",
        members=(
            "BHARTIARTL", "ETERNAL", "IDEA", "INDHOTEL", "INDIGO",
            "INDUSTOWER", "JUBLFOOD", "NYKAA", "PAYTM", "SWIGGY",
        ),
    ),
)

SECTOR_CONFIG_MAP = {config.code: config for config in SECTOR_CONFIGS}
SECTOR_STOCKS = {symbol for config in SECTOR_CONFIGS for symbol in config.members}

TIMEFRAME_ALIASES = {
    "hour": "hourly",
    "hourly": "hourly",
    "day": "daily",
    "daily": "daily",
    "week": "weekly",
    "weekly": "weekly",
    "month": "monthly",
    "monthly": "monthly",
}

TIMEFRAME_CONFIG = {
    "hourly": {
        "redis_ttl": 60,
        "upstox_interval": "30minute",
        "history_days": 10,
        "bucket": "hourly",
        "rrg_lookback": 10,
        "trail": 8,
    },
    "daily": {
        "redis_ttl": 180,
        "upstox_interval": "day",
        "history_days": 120,
        "bucket": "daily",
        "rrg_lookback": 12,
        "trail": 8,
    },
    "weekly": {
        "redis_ttl": 300,
        "upstox_interval": "day",
        "history_days": 400,
        "bucket": "weekly",
        "rrg_lookback": 10,
        "trail": 8,
    },
    "monthly": {
        "redis_ttl": 600,
        "upstox_interval": "day",
        "history_days": 900,
        "bucket": "monthly",
        "rrg_lookback": 8,
        "trail": 8,
    },
}


class SectorRotationTracker:
    """Build sector-vs-benchmark and stock-vs-sector RRG payloads."""

    async def get_sector_rotation(self, timeframe: str = "daily") -> dict:
        normalized = self._normalize_timeframe(timeframe)
        redis = await get_redis()
        cache_key = f"sector_rotation:{normalized}"
        cached = await redis.get(cache_key)
        if cached:
            return json.loads(cached)

        payload = await self._calculate_relative_strength(normalized)
        await redis.set(cache_key, json.dumps(payload), ex=TIMEFRAME_CONFIG[normalized]["redis_ttl"])
        return payload

    async def _calculate_relative_strength(self, timeframe: str) -> dict:
        index_series, source, detail = await self._load_index_series(timeframe)
        if BENCHMARK_APP_SYMBOL not in index_series:
            return {
                "timeframe": timeframe,
                "benchmark": None,
                "watchlist": [],
                "rrg": {"benchmark_symbol": BENCHMARK_APP_SYMBOL, "points": [], "quadrant_counts": self._quadrant_counts([])},
                "stocks_by_sector": {},
                "unassigned_symbols": [],
                "source": source,
                "detail": detail or "Connect Fyers or Upstox to populate sector rotation history.",
                "timestamp": datetime.now(UTC).isoformat(),
            }

        stock_series = await self._load_stock_history_map(timeframe)
        benchmark_series = index_series[BENCHMARK_APP_SYMBOL]
        benchmark_closes = [close for _, close in benchmark_series]
        benchmark_change_pct = self._change_pct(benchmark_closes)

        benchmark = {
            "symbol": BENCHMARK_APP_SYMBOL,
            "name": "NIFTY 50",
            "price": round(benchmark_closes[-1], 2) if benchmark_closes else 0.0,
            "tracked_change_pct": round(benchmark_change_pct, 2),
            "samples": len(benchmark_closes),
        }

        watchlist: list[dict[str, Any]] = []
        sector_rrg_points: list[dict[str, Any]] = []
        stocks_by_sector: dict[str, Any] = {}
        assigned_symbols: set[str] = set()

        for config in SECTOR_CONFIGS:
            series = index_series.get(config.code) or self._build_synthetic_sector_series(config.members, stock_series)
            closes = [close for _, close in series]
            if len(closes) < 2:
                continue
            sector_source = "official" if index_series.get(config.code) else "synthetic"

            sector_entry = self._build_rotation_row(
                code=config.code,
                name=config.label,
                symbol=config.app_symbol,
                closes=closes,
                benchmark_closes=benchmark_closes,
                trail_limit=TIMEFRAME_CONFIG[timeframe]["trail"],
                sample_count=len(closes),
                series_source=sector_source,
                member_count=len(config.members),
            )
            watchlist.append({key: value for key, value in sector_entry.items() if key != "trail"})
            sector_rrg_points.append(sector_entry)

            sector_stock_points: list[dict[str, Any]] = []
            for symbol in config.members:
                stock_rows = stock_series.get(symbol)
                if not stock_rows:
                    continue
                stock_closes = [close for _, close in stock_rows]
                if len(stock_closes) < 2:
                    continue
                assigned_symbols.add(symbol)
                stock_entry = self._build_rotation_row(
                    code=symbol,
                    name=symbol,
                    symbol=symbol,
                    closes=stock_closes,
                    benchmark_closes=closes,
                    trail_limit=TIMEFRAME_CONFIG[timeframe]["trail"],
                    sample_count=len(stock_closes),
                )
                sector_stock_points.append(stock_entry)

            sector_stock_points.sort(
                key=lambda row: (row["quadrant"] != "leading", -row["relative_strength_pct"], row["name"])
            )
            stocks_by_sector[config.code] = {
                "sector": {key: value for key, value in sector_entry.items() if key != "trail"},
                "stocks": [{key: value for key, value in row.items() if key != "trail"} for row in sector_stock_points],
                "rrg": {
                    "points": sector_stock_points,
                    "quadrant_counts": self._quadrant_counts(sector_stock_points),
                },
                "source": sector_source,
                "configured_members": len(config.members),
                "available_members": len(sector_stock_points),
            }

        all_known_symbols = sorted(stock_series.keys())
        unassigned_symbols = sorted(symbol for symbol in all_known_symbols if symbol not in assigned_symbols)
        if len(unassigned_symbols) >= 2:
            fallback_series = self._build_synthetic_sector_series(unassigned_symbols, stock_series)
            fallback_closes = [close for _, close in fallback_series]
            if len(fallback_closes) >= 2:
                fallback_entry = self._build_rotation_row(
                    code="BROAD_MARKET",
                    name="Broad Market",
                    symbol="SECTOR:BROAD_MARKET",
                    closes=fallback_closes,
                    benchmark_closes=benchmark_closes,
                    trail_limit=TIMEFRAME_CONFIG[timeframe]["trail"],
                    sample_count=len(fallback_closes),
                    series_source="synthetic",
                    member_count=len(unassigned_symbols),
                )
                watchlist.append({key: value for key, value in fallback_entry.items() if key != "trail"})
                sector_rrg_points.append(fallback_entry)

                fallback_stock_points: list[dict[str, Any]] = []
                for symbol in unassigned_symbols:
                    stock_rows = stock_series.get(symbol)
                    if not stock_rows:
                        continue
                    stock_closes = [close for _, close in stock_rows]
                    if len(stock_closes) < 2:
                        continue
                    fallback_stock_points.append(
                        self._build_rotation_row(
                            code=symbol,
                            name=symbol,
                            symbol=symbol,
                            closes=stock_closes,
                            benchmark_closes=fallback_closes,
                            trail_limit=TIMEFRAME_CONFIG[timeframe]["trail"],
                            sample_count=len(stock_closes),
                        )
                    )

                fallback_stock_points.sort(
                    key=lambda row: (row["quadrant"] != "leading", -row["relative_strength_pct"], row["name"])
                )
                stocks_by_sector["BROAD_MARKET"] = {
                    "sector": {key: value for key, value in fallback_entry.items() if key != "trail"},
                    "stocks": [{key: value for key, value in row.items() if key != "trail"} for row in fallback_stock_points],
                    "rrg": {
                        "points": fallback_stock_points,
                        "quadrant_counts": self._quadrant_counts(fallback_stock_points),
                    },
                    "source": "synthetic",
                    "configured_members": len(unassigned_symbols),
                    "available_members": len(fallback_stock_points),
                }
                assigned_symbols.update(unassigned_symbols)
                unassigned_symbols = []

        watchlist.sort(
            key=lambda row: (row["quadrant"] != "leading", -row["relative_strength_pct"], row["name"])
        )
        sector_rrg_points.sort(
            key=lambda row: (row["quadrant"] != "leading", -row["relative_strength_pct"], row["name"])
        )
        quadrant_counts = self._quadrant_counts(sector_rrg_points)

        return {
            "timeframe": timeframe,
            "benchmark": benchmark,
            "watchlist": watchlist,
            "rrg": {
                "benchmark_symbol": BENCHMARK_APP_SYMBOL,
                "points": sector_rrg_points,
                "quadrant_counts": quadrant_counts,
            },
            "stocks_by_sector": stocks_by_sector,
            "unassigned_symbols": unassigned_symbols,
            "source": source,
            "detail": detail,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    async def _load_index_series(
        self,
        timeframe: str,
    ) -> tuple[dict[str, list[tuple[datetime, float]]], str, Optional[str]]:
        config = TIMEFRAME_CONFIG[timeframe]
        from_date = date.today() - timedelta(days=config["history_days"])
        to_date = date.today()
        semaphore = asyncio.Semaphore(6)
        symbols = [(BENCHMARK_APP_SYMBOL, BENCHMARK_APP_SYMBOL, UPSTOX_BENCHMARK_SYMBOL), *[
            (sector.code, sector.app_symbol, sector.upstox_symbol)
            for sector in SECTOR_CONFIGS
            if sector.upstox_symbol or sector.app_symbol.startswith("NSE:")
        ]]

        async def fetch_series(cache_key: str, app_symbol: str, instrument_key: str) -> tuple[str, list[tuple[datetime, float]]]:
            async with semaphore:
                rows = await self._fetch_cached_market_series(
                    app_symbol=app_symbol,
                    instrument_key=instrument_key,
                    interval=config["upstox_interval"],
                    from_date=from_date,
                    to_date=to_date,
                    bucket=timeframe,
                )
                return cache_key, rows

        results = await asyncio.gather(
            *(fetch_series(cache_key, app_symbol, instrument_key) for cache_key, app_symbol, instrument_key in symbols)
        )
        series_map = {cache_key: rows for cache_key, rows in results if rows}
        if not series_map:
            return {}, "none", "Fyers or Upstox connection is required for sector index history."

        from api.routers.auth import get_active_adapter, get_broker_token

        has_fyers = bool(get_active_adapter("fyers"))
        has_upstox = bool(get_broker_token("upstox"))
        if has_fyers and has_upstox:
            source = "fyers+upstox+timescale"
        elif has_fyers:
            source = "fyers+timescale"
        else:
            source = "upstox+timescale"
        return series_map, source, None

    async def _fetch_cached_market_series(
        self,
        *,
        app_symbol: str,
        instrument_key: Optional[str],
        interval: str,
        from_date: date,
        to_date: date,
        bucket: str,
    ) -> list[tuple[datetime, float]]:
        fyers_rows = await self._fetch_cached_fyers_series(
            app_symbol=app_symbol,
            interval=interval,
            from_date=from_date,
            to_date=to_date,
            bucket=bucket,
        )
        if fyers_rows:
            return fyers_rows

        if not instrument_key:
            return []
        return await self._fetch_cached_upstox_series(
            app_symbol=app_symbol,
            instrument_key=instrument_key,
            interval=interval,
            from_date=from_date,
            to_date=to_date,
            bucket=bucket,
        )

    async def _fetch_cached_fyers_series(
        self,
        *,
        app_symbol: str,
        interval: str,
        from_date: date,
        to_date: date,
        bucket: str,
    ) -> list[tuple[datetime, float]]:
        if not app_symbol.startswith("NSE:"):
            return []
        from api.routers.auth import ensure_fyers_session, get_active_adapter

        adapter = get_active_adapter("fyers")
        if adapter is None:
            if not await ensure_fyers_session():
                return []
            adapter = get_active_adapter("fyers")
        get_history = getattr(adapter, "get_historical_candles", None) if adapter else None
        if not callable(get_history):
            return []

        redis = await get_redis()
        cache_key = (
            f"sector_history:fyers:{app_symbol}:{interval}:{bucket}:{from_date.isoformat()}:{to_date.isoformat()}"
        )
        cached = await redis.get(cache_key)
        if cached:
            payload = json.loads(cached)
            return [
                (datetime.fromisoformat(item["time"]), float(item["close"]))
                for item in payload
            ]

        resolution = "30" if interval == "30minute" else "D"
        try:
            candles = await get_history(
                app_symbol,
                resolution,
                from_date.isoformat(),
                to_date.isoformat(),
            )
        except Exception as exc:
            logger.debug(f"[Sector] Fyers historical fetch failed for {app_symbol}: {exc}")
            return []

        rows = [
            (
                datetime.fromisoformat(str(candle["time"]).replace("Z", "+00:00")),
                float(candle["close"]),
            )
            for candle in candles
            if candle.get("close") is not None
        ]
        aggregated = self._aggregate_close_series(rows, bucket)
        await redis.set(
            cache_key,
            json.dumps([{"time": ts.isoformat(), "close": close} for ts, close in aggregated]),
            ex=TIMEFRAME_CONFIG[bucket]["redis_ttl"],
        )
        return aggregated

    async def _fetch_cached_upstox_series(
        self,
        *,
        app_symbol: str,
        instrument_key: str,
        interval: str,
        from_date: date,
        to_date: date,
        bucket: str,
    ) -> list[tuple[datetime, float]]:
        redis = await get_redis()
        cache_key = (
            f"sector_history:{app_symbol}:{interval}:{bucket}:{from_date.isoformat()}:{to_date.isoformat()}"
        )
        cached = await redis.get(cache_key)
        if cached:
            payload = json.loads(cached)
            return [
                (datetime.fromisoformat(item["time"]), float(item["close"]))
                for item in payload
            ]

        from api.routers.auth import get_broker_token

        token = get_broker_token("upstox")
        if not token:
            return []

        encoded_key = quote(instrument_key, safe="")
        url = (
            "https://api.upstox.com/v2/historical-candle/"
            f"{encoded_key}/{interval}/{to_date.isoformat()}/{from_date.isoformat()}"
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
            logger.debug(f"[Sector] Historical fetch failed for {app_symbol}: {response.status_code}")
            return []

        candles = response.json().get("data", {}).get("candles", [])
        rows = [
            (
                datetime.fromisoformat(str(candle[0]).replace("Z", "+00:00")),
                float(candle[4]),
            )
            for candle in reversed(candles)
            if candle and candle[4] is not None
        ]
        aggregated = self._aggregate_close_series(rows, bucket)
        await redis.set(
            cache_key,
            json.dumps([{"time": ts.isoformat(), "close": close} for ts, close in aggregated]),
            ex=TIMEFRAME_CONFIG[bucket]["redis_ttl"],
        )
        return aggregated

    async def _load_stock_history_map(self, timeframe: str) -> dict[str, list[tuple[datetime, float]]]:
        symbols = sorted(SECTOR_STOCKS)
        if not symbols:
            return {}

        config = TIMEFRAME_CONFIG[timeframe]
        from_ts = datetime.now(UTC) - timedelta(days=config["history_days"])
        statement = text("""
            SELECT underlying, time, close
            FROM underlying_spot_candles
            WHERE interval = '30minute'
              AND close IS NOT NULL
              AND time >= :from_ts
              AND underlying IN :symbols
            ORDER BY underlying, time ASC
        """).bindparams(bindparam("symbols", expanding=True))

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                statement,
                {"from_ts": from_ts, "symbols": symbols},
            )
            rows = result.fetchall()

        grouped: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
        for row in rows:
            grouped[str(row.underlying)].append((row.time.astimezone(UTC), float(row.close)))

        return {
            symbol: self._aggregate_close_series(series, timeframe)
            for symbol, series in grouped.items()
            if series
        }

    @staticmethod
    def _build_synthetic_sector_series(
        members: tuple[str, ...] | list[str],
        stock_series: dict[str, list[tuple[datetime, float]]],
    ) -> list[tuple[datetime, float]]:
        grouped: dict[datetime, list[float]] = defaultdict(list)
        for symbol in members:
            for ts, close in stock_series.get(symbol, []):
                if math.isfinite(close):
                    grouped[ts].append(close)
        synthetic = [
            (ts, round(mean(values), 4))
            for ts, values in grouped.items()
            if values
        ]
        synthetic.sort(key=lambda item: item[0])
        return synthetic

    def _aggregate_close_series(
        self,
        rows: list[tuple[datetime, float]],
        timeframe: str,
    ) -> list[tuple[datetime, float]]:
        if timeframe == "hourly":
            bucket_mode = "hourly"
        elif timeframe == "daily":
            bucket_mode = "daily"
        elif timeframe == "weekly":
            bucket_mode = "weekly"
        elif timeframe == "monthly":
            bucket_mode = "monthly"
        else:
            bucket_mode = timeframe

        aggregated: dict[datetime, float] = {}
        for ts, close in rows:
            local_ts = ts.astimezone(IST)
            if bucket_mode == "hourly":
                bucket = local_ts.replace(minute=0, second=0, microsecond=0)
            elif bucket_mode == "daily":
                bucket = datetime.combine(local_ts.date(), time(0, 0), tzinfo=IST)
            elif bucket_mode == "weekly":
                monday = local_ts.date() - timedelta(days=local_ts.weekday())
                bucket = datetime.combine(monday, time(0, 0), tzinfo=IST)
            else:
                month_start = date(local_ts.year, local_ts.month, 1)
                bucket = datetime.combine(month_start, time(0, 0), tzinfo=IST)
            aggregated[bucket.astimezone(UTC)] = close

        return sorted(aggregated.items(), key=lambda item: item[0])

    def _build_rotation_row(
        self,
        *,
        code: str,
        name: str,
        symbol: str,
        closes: list[float],
        benchmark_closes: list[float],
        trail_limit: int,
        sample_count: int,
        series_source: Optional[str] = None,
        member_count: Optional[int] = None,
    ) -> dict[str, Any]:
        tracked_change_pct = self._change_pct(closes)
        benchmark_change_pct = self._change_pct(benchmark_closes)
        relative_strength_pct = tracked_change_pct - benchmark_change_pct
        rrg_series = self._build_rrg_series(closes, benchmark_closes)
        ratio = rrg_series[-1]["ratio"] if rrg_series else 100.0
        momentum = rrg_series[-1]["momentum"] if rrg_series else 100.0
        quadrant = self._quadrant(ratio, momentum)
        trend = self._trend_label(ratio, momentum)

        return {
            "code": code,
            "name": name,
            "symbol": symbol,
            "price": round(closes[-1], 2) if closes and math.isfinite(closes[-1]) else 0.0,
            "tracked_change_pct": round(tracked_change_pct, 2) if math.isfinite(tracked_change_pct) else 0.0,
            "relative_strength_pct": round(relative_strength_pct, 2) if math.isfinite(relative_strength_pct) else 0.0,
            "rrg_ratio": round(ratio, 2) if math.isfinite(ratio) else 100.0,
            "rrg_momentum": round(momentum, 2) if math.isfinite(momentum) else 100.0,
            "quadrant": quadrant,
            "trend": trend,
            "samples": sample_count,
            "series_source": series_source,
            "member_count": member_count,
            "trail": rrg_series[-trail_limit:],
        }

    @staticmethod
    def _normalize_timeframe(timeframe: str) -> str:
        normalized = TIMEFRAME_ALIASES.get((timeframe or "daily").lower())
        if not normalized:
            return "daily"
        return normalized

    @staticmethod
    def _change_pct(closes: list[float]) -> float:
        if len(closes) < 2 or closes[0] == 0:
            return 0.0
        return ((closes[-1] / closes[0]) - 1.0) * 100.0

    @staticmethod
    def _build_rrg_series(
        closes: list[float],
        benchmark_closes: list[float],
    ) -> list[dict[str, float]]:
        length = min(len(closes), len(benchmark_closes))
        if length < 2:
            return []

        relative_strength = [
            stock_close / benchmark_close
            for stock_close, benchmark_close in zip(closes[-length:], benchmark_closes[-length:])
            if benchmark_close and math.isfinite(stock_close) and math.isfinite(benchmark_close)
        ]
        if len(relative_strength) < 2:
            return []

        ratio_lookback = min(10, len(relative_strength))
        momentum_lookback = min(4, len(relative_strength))
        ratio_values: list[float] = []
        momentum_values: list[float] = []

        for index, ratio in enumerate(relative_strength):
            ratio_window = relative_strength[max(0, index - ratio_lookback + 1): index + 1]
            avg_ratio = mean(ratio_window) if ratio_window else ratio
            ratio_value = 100.0 if avg_ratio == 0 else 100.0 + ((ratio / avg_ratio) - 1.0) * 100.0
            if not math.isfinite(ratio_value):
                ratio_value = 100.0
            ratio_values.append(ratio_value)

            momentum_window = ratio_values[max(0, index - momentum_lookback + 1): index + 1]
            avg_momentum = mean(momentum_window) if momentum_window else ratio_value
            momentum_value = 100.0 if avg_momentum == 0 else 100.0 + ((ratio_value / avg_momentum) - 1.0) * 100.0
            if not math.isfinite(momentum_value):
                momentum_value = 100.0
            momentum_values.append(momentum_value)

        return [
            {"ratio": round(ratio_values[index], 4), "momentum": round(momentum_values[index], 4)}
            for index in range(len(ratio_values))
        ]

    @staticmethod
    def _quadrant_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            quadrant = str(row.get("quadrant") or "")
            if quadrant:
                counts[quadrant] = counts.get(quadrant, 0) + 1
        return counts

    @staticmethod
    def _quadrant(ratio: float, momentum: float) -> str:
        if ratio >= 100 and momentum >= 100:
            return "leading"
        if ratio < 100 and momentum >= 100:
            return "improving"
        if ratio >= 100 and momentum < 100:
            return "weakening"
        return "lagging"

    @staticmethod
    def _trend_label(ratio: float, momentum: float) -> str:
        if ratio >= 100 and momentum >= 100:
            return "outperforming"
        if ratio < 100 and momentum >= 100:
            return "improving"
        if ratio >= 100 and momentum < 100:
            return "rolling-over"
        return "underperforming"

    async def get_macro_dashboard(self) -> dict:
        redis = await get_redis()
        cached = await redis.get("macro_dashboard")
        if cached:
            return json.loads(cached)

        result = {
            "india_vix": await self._get_india_vix(),
            "crude_mcx": {"price": 0, "change_pct": 0, "sparkline": []},
            "gold_mcx": {"price": 0, "change_pct": 0, "sparkline": []},
            "dxy": {"price": 0, "change_pct": 0, "sparkline": []},
            "us10y": {"price": 0, "change_pct": 0, "sparkline": []},
            "timestamp": datetime.now(UTC).isoformat(),
        }
        await redis.set("macro_dashboard", json.dumps(result), ex=300)
        return result

    async def _get_india_vix(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    "https://www.nseindia.com/api/allIndices",
                    headers={"User-Agent": "Mozilla/5.0"},
                )
            data = response.json().get("data", [])
            for item in data:
                if item.get("index") == "India VIX":
                    return {
                        "price": item.get("last", 0),
                        "change_pct": item.get("percentChange", 0),
                        "sparkline": [],
                    }
        except Exception:
            pass
        return {"price": 0, "change_pct": 0, "sparkline": []}

    async def get_iv_rank(self, symbol: str) -> dict:
        redis = await get_redis()
        cached = await redis.get(f"iv_rank:{symbol}")
        if cached:
            return json.loads(cached)

        result = {
            "symbol": symbol,
            "current_iv": 0.0,
            "iv_rank": 0.0,
            "iv_percentile": 0.0,
            "iv_52w_high": 0.0,
            "iv_52w_low": 0.0,
        }
        await redis.set(f"iv_rank:{symbol}", json.dumps(result), ex=300)
        return result


sector_tracker = SectorRotationTracker()
