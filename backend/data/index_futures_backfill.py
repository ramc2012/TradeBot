from __future__ import annotations

import asyncio
import csv
import gzip
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional

import httpx
from loguru import logger
from sqlalchemy import text

from analysis.instruments import INDEX_INSTRUMENT_KEYS
from db.database import AsyncSessionLocal


UPSTOX_BASE = "https://api.upstox.com/v2"
IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_UNDERLYINGS = ("NIFTY", "BANKNIFTY", "SENSEX")
DEFAULT_START_DATE = date(2024, 1, 1)
DEFAULT_INTERVAL = "1minute"

_TS, _O, _H, _L, _C, _V = 0, 1, 2, 3, 4, 5


INDEX_FUTURES: dict[str, dict[str, str]] = {
    "NIFTY": {
        "market": "NSE",
        "upstox_exchange": "NSE",
        "upstox_segment": "NSE_FO",
        "upstox_underlying_key": INDEX_INSTRUMENT_KEYS["NIFTY"],
        "fyers_prefix": "NSE:NIFTY",
    },
    "BANKNIFTY": {
        "market": "NSE",
        "upstox_exchange": "NSE",
        "upstox_segment": "NSE_FO",
        "upstox_underlying_key": INDEX_INSTRUMENT_KEYS["BANKNIFTY"],
        "fyers_prefix": "NSE:BANKNIFTY",
    },
    "SENSEX": {
        "market": "BSE",
        "upstox_exchange": "BSE",
        "upstox_segment": "BSE_FO",
        "upstox_underlying_key": INDEX_INSTRUMENT_KEYS["SENSEX"],
        "fyers_prefix": "BSE:SENSEX",
    },
}


@dataclass(frozen=True)
class FutureInstrument:
    underlying: str
    market: str
    instrument_key: str
    trading_symbol: str
    expiry: date | None
    expired: bool
    source: str


@dataclass
class SymbolBackfillSummary:
    underlying: str
    source: str
    instruments: int = 0
    fetched_rows: int = 0
    stored_rows: int = 0
    exported_rows: int = 0
    first_time: str | None = None
    last_time: str | None = None
    errors: list[str] = field(default_factory=list)


@dataclass
class BackfillSummary:
    started_at: str
    finished_at: str | None = None
    interval: str = DEFAULT_INTERVAL
    from_date: str = DEFAULT_START_DATE.isoformat()
    to_date: str = ""
    symbols: dict[str, SymbolBackfillSummary] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "interval": self.interval,
            "from_date": self.from_date,
            "to_date": self.to_date,
            "symbols": {
                symbol: {
                    "underlying": item.underlying,
                    "source": item.source,
                    "instruments": item.instruments,
                    "fetched_rows": item.fetched_rows,
                    "stored_rows": item.stored_rows,
                    "exported_rows": item.exported_rows,
                    "first_time": item.first_time,
                    "last_time": item.last_time,
                    "errors": item.errors,
                }
                for symbol, item in self.symbols.items()
            },
        }


def normalize_underlyings(values: list[str] | tuple[str, ...] | None) -> list[str]:
    raw = values or list(DEFAULT_UNDERLYINGS)
    result: list[str] = []
    for value in raw:
        symbol = str(value or "").strip().upper()
        if not symbol:
            continue
        if symbol not in INDEX_FUTURES:
            raise ValueError(f"Unsupported futures underlying: {symbol}")
        if symbol not in result:
            result.append(symbol)
    return result


def month_code_for_front_contract(as_of: date, underlying: str) -> str:
    contract_month = as_of.replace(day=1)
    expiry = _approx_monthly_expiry(as_of.year, as_of.month, underlying)
    if as_of > expiry:
        year = as_of.year + int(as_of.month == 12)
        month = 1 if as_of.month == 12 else as_of.month + 1
        contract_month = date(year, month, 1)
    return contract_month.strftime("%y%b").upper()


def fyers_front_month_symbol(underlying: str, as_of: date) -> str:
    meta = INDEX_FUTURES[underlying]
    return f"{meta['fyers_prefix']}{month_code_for_front_contract(as_of, underlying)}FUT"


def _approx_monthly_expiry(year: int, month: int, underlying: str) -> date:
    # Post-Sept-2025 SEBI expiry regime: NSE index contracts expire on the last
    # TUESDAY; BSE (SENSEX) on the last THURSDAY. (These were previously
    # inverted here, which subscribed the auction order-flow feed to an expired
    # NIFTY future for ~2 sessions every month.) Monday=0 … Sunday=6.
    weekday = 3 if underlying == "SENSEX" else 1
    next_month = date(year + int(month == 12), 1 if month == 12 else month + 1, 1)
    day = next_month - timedelta(days=1)
    while day.weekday() != weekday:
        day -= timedelta(days=1)
    return day


def chunk_dates(start: date, end: date, days: int) -> list[tuple[date, date]]:
    if end < start:
        return []
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=max(1, days) - 1), end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def parse_candle_time(value: Any) -> datetime:
    ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=IST)
    return ts.astimezone(timezone.utc)


class UpstoxFuturesHistoryClient:
    def __init__(self, access_token: str, *, gap_seconds: float = 0.4) -> None:
        self.headers = {
            "Authorization": f"Bearer {str(access_token or '').strip()}",
            "Accept": "application/json",
        }
        self.gap_seconds = max(float(gap_seconds), 0.0)
        self._last_call: float = 0.0

    async def _throttle(self) -> None:
        if self.gap_seconds <= 0:
            return
        loop = asyncio.get_running_loop()
        elapsed = loop.time() - self._last_call
        if elapsed < self.gap_seconds:
            await asyncio.sleep(self.gap_seconds - elapsed)
        self._last_call = loop.time()

    async def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict:
        await self._throttle()
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{UPSTOX_BASE}{path}", params=params or {}, headers=self.headers)
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(f"Upstox returned non-JSON response ({response.status_code})") from exc
        if response.status_code != 200:
            raise RuntimeError(f"Upstox HTTP {response.status_code}: {payload}")
        return payload

    async def expired_expiries(self, underlying: str) -> list[date]:
        key = INDEX_FUTURES[underlying]["upstox_underlying_key"]
        payload = await self._get_json(
            "/expired-instruments/expiries",
            params={"instrument_key": key},
        )
        expiries: list[date] = []
        for item in payload.get("data") or []:
            try:
                expiries.append(date.fromisoformat(str(item)))
            except ValueError:
                continue
        return sorted(set(expiries))

    async def expired_future_contract(self, underlying: str, expiry: date) -> FutureInstrument | None:
        meta = INDEX_FUTURES[underlying]
        payload = await self._get_json(
            "/expired-instruments/future/contract",
            params={
                "instrument_key": meta["upstox_underlying_key"],
                "expiry_date": expiry.isoformat(),
            },
        )
        contracts = list(payload.get("data") or [])
        if not contracts:
            return None
        contracts.sort(
            key=lambda row: (
                str(row.get("instrument_type") or "").upper() != "FUT",
                underlying not in str(row.get("trading_symbol") or row.get("tradingsymbol") or "").upper(),
            )
        )
        row = contracts[0]
        key = str(row.get("instrument_key") or "").strip()
        if not key:
            return None
        return FutureInstrument(
            underlying=underlying,
            market=meta["market"],
            instrument_key=key,
            trading_symbol=str(row.get("trading_symbol") or row.get("tradingsymbol") or key),
            expiry=expiry,
            expired=True,
            source="upstox_expired_futures",
        )

    async def active_future_contracts(self, underlying: str, to_date: date) -> list[FutureInstrument]:
        meta = INDEX_FUTURES[underlying]
        params: dict[str, Any] = {
            "query": underlying,
            "exchanges": meta["upstox_exchange"],
            "instrument_types": "FUT",
            "records": 30,
        }
        payload = await self._get_json("/instruments/search", params=params)
        rows = list(payload.get("data") or [])
        instruments: list[FutureInstrument] = []
        for row in rows:
            segment = str(row.get("segment") or row.get("exchange_segment") or "").upper()
            if segment and segment != meta["upstox_segment"]:
                continue
            expiry = _parse_optional_date(row.get("expiry"))
            if expiry is None or expiry < to_date:
                continue
            key = str(row.get("instrument_key") or "").strip()
            if not key:
                continue
            instruments.append(
                FutureInstrument(
                    underlying=underlying,
                    market=meta["market"],
                    instrument_key=key,
                    trading_symbol=str(row.get("trading_symbol") or row.get("tradingsymbol") or key),
                    expiry=expiry,
                    expired=False,
                    source="upstox_active_futures",
                )
            )
        instruments.sort(key=lambda item: item.expiry or date.max)
        return instruments

    async def fetch_candles(
        self,
        instrument: FutureInstrument,
        *,
        interval: str,
        start: date,
        end: date,
    ) -> list[dict[str, Any]]:
        encoded = urllib.parse.quote(instrument.instrument_key, safe="")
        prefix = "expired-instruments/historical-candle" if instrument.expired else "historical-candle"
        payload = await self._get_json(f"/{prefix}/{encoded}/{interval}/{end.isoformat()}/{start.isoformat()}")
        return normalize_upstox_candles(payload.get("data", {}).get("candles") or [])


def _parse_optional_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except Exception:
        return None


def normalize_upstox_candles(candles: list[list[Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candle in reversed(candles):
        if len(candle) < 6:
            continue
        rows.append(
            {
                "time": parse_candle_time(candle[_TS]).isoformat(),
                "open": float(candle[_O]),
                "high": float(candle[_H]),
                "low": float(candle[_L]),
                "close": float(candle[_C]),
                "volume": int(candle[_V] or 0),
            }
        )
    return rows


async def ensure_index_futures_schema() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS index_futures_candles (
                    time           TIMESTAMPTZ NOT NULL,
                    underlying     TEXT        NOT NULL,
                    market         TEXT        NOT NULL,
                    expiry         DATE,
                    instrument_key TEXT        NOT NULL,
                    trading_symbol TEXT,
                    interval       TEXT        NOT NULL DEFAULT '1minute',
                    open           NUMERIC(14,4),
                    high           NUMERIC(14,4),
                    low            NUMERIC(14,4),
                    close          NUMERIC(14,4),
                    volume         BIGINT,
                    source         TEXT        NOT NULL,
                    synced_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (instrument_key, interval, time)
                )
                """
            )
        )
        await session.execute(
            text(
                """
                SELECT create_hypertable(
                    'index_futures_candles', 'time',
                    if_not_exists => TRUE,
                    chunk_time_interval => INTERVAL '1 day'
                )
                """
            )
        )
        await session.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_index_futures_candles_symbol_time
                ON index_futures_candles (underlying, interval, time DESC)
                """
            )
        )
        await session.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_index_futures_candles_source_time
                ON index_futures_candles (source, underlying, interval, time DESC)
                """
            )
        )
        await session.commit()


async def store_candles(
    instrument: FutureInstrument,
    candles: list[dict[str, Any]],
    *,
    interval: str,
) -> int:
    if not candles:
        return 0
    rows = [
        {
            "time": parse_candle_time(candle["time"]),
            "underlying": instrument.underlying,
            "market": instrument.market,
            "expiry": instrument.expiry,
            "instrument_key": instrument.instrument_key,
            "trading_symbol": instrument.trading_symbol,
            "interval": interval,
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "volume": int(candle.get("volume") or 0),
            "source": instrument.source,
        }
        for candle in candles
    ]
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                INSERT INTO index_futures_candles (
                    time, underlying, market, expiry, instrument_key,
                    trading_symbol, interval, open, high, low, close, volume,
                    source, synced_at
                )
                VALUES (
                    :time, :underlying, :market, :expiry, :instrument_key,
                    :trading_symbol, :interval, :open, :high, :low, :close,
                    :volume, :source, NOW()
                )
                ON CONFLICT (instrument_key, interval, time) DO UPDATE
                SET open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume,
                    source = EXCLUDED.source,
                    synced_at = NOW()
                """
            ),
            rows,
        )
        await session.commit()
    return len(rows)


async def export_continuous_csv(
    *,
    underlying: str,
    interval: str,
    from_date: date,
    to_date: date,
    source_pattern: str | None,
    output_root: Path,
) -> int:
    where_source = "AND source LIKE :source_pattern" if source_pattern else ""
    params: dict[str, Any] = {
        "underlying": underlying,
        "interval": interval,
        "from_date": from_date,
        "to_date": to_date,
    }
    if source_pattern:
        params["source_pattern"] = source_pattern

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                f"""
                SELECT DISTINCT ON (time)
                       time, open, high, low, close, volume,
                       instrument_key, trading_symbol, expiry, source
                FROM index_futures_candles
                WHERE underlying = :underlying
                  AND interval = :interval
                  AND time >= CAST(:from_date AS date)
                  AND time < (CAST(:to_date AS date) + INTERVAL '1 day')
                  {where_source}
                ORDER BY time, expiry NULLS LAST, source
                """
            ),
            params,
        )
        rows = result.mappings().all()

    path = output_root / f"underlying={underlying}" / f"{interval}.csv.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "instrument_key",
                "trading_symbol",
                "expiry",
                "source",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "time": row["time"].isoformat(),
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                    "volume": row["volume"],
                    "instrument_key": row["instrument_key"],
                    "trading_symbol": row["trading_symbol"],
                    "expiry": row["expiry"].isoformat() if row["expiry"] else "",
                    "source": row["source"],
                }
            )
    return len(rows)


async def backfill_fyers_symbol(
    *,
    adapter: Any,
    underlying: str,
    from_date: date,
    to_date: date,
    interval: str,
    chunk_days: int,
    fyers_symbol: str | None = None,
) -> SymbolBackfillSummary:
    symbol = fyers_symbol or fyers_front_month_symbol(underlying, to_date)
    instrument = FutureInstrument(
        underlying=underlying,
        market=INDEX_FUTURES[underlying]["market"],
        instrument_key=symbol,
        trading_symbol=symbol,
        expiry=None,
        expired=False,
        source="fyers_continuous_futures",
    )
    summary = SymbolBackfillSummary(underlying=underlying, source=instrument.source, instruments=1)
    resolution = "1" if interval == "1minute" else interval.replace("minute", "")
    # CLASS_BULK: historical backfill — hard-capped at 25% of the shared broker
    # budget and yields instantly to queued CRITICAL work.
    from brokers.rate_limiter import CLASS_BULK, broker_class

    for start, end in chunk_dates(from_date, to_date, chunk_days):
        logger.info(f"[fyers {underlying}] {symbol} {interval} {start} -> {end}")
        try:
            with broker_class(CLASS_BULK):
                candles = await adapter.get_historical_candles(
                    symbol,
                    resolution,
                    start.isoformat(),
                    end.isoformat(),
                    cont_flag=1,
                )
        except Exception as exc:
            summary.errors.append(f"{start}->{end}: {exc}")
            logger.warning(f"[fyers {underlying}] failed {start}->{end}: {exc}")
            continue
        summary.fetched_rows += len(candles)
        summary.stored_rows += await store_candles(instrument, candles, interval=interval)

    await _fill_summary_bounds(summary, interval=interval)
    return summary


async def backfill_upstox_symbol(
    *,
    client: UpstoxFuturesHistoryClient,
    underlying: str,
    from_date: date,
    to_date: date,
    interval: str,
    chunk_days: int,
) -> SymbolBackfillSummary:
    summary = SymbolBackfillSummary(underlying=underlying, source="upstox_futures")
    expiries = [
        item for item in await client.expired_expiries(underlying)
        if from_date <= item <= to_date
    ]
    instruments: list[FutureInstrument] = []
    for expiry in expiries:
        try:
            instrument = await client.expired_future_contract(underlying, expiry)
        except Exception as exc:
            summary.errors.append(f"{expiry}: {exc}")
            continue
        if instrument is not None:
            instruments.append(instrument)

    try:
        active = await client.active_future_contracts(underlying, to_date)
        instruments.extend(active[:1])
    except Exception as exc:
        summary.errors.append(f"active_contracts: {exc}")

    instruments.sort(key=lambda item: item.expiry or date.max)
    summary.instruments = len(instruments)
    previous_expiry: date | None = None
    for instrument in instruments:
        window_start = max(from_date, (previous_expiry + timedelta(days=1)) if previous_expiry else from_date)
        window_end = min(to_date, instrument.expiry or to_date)
        previous_expiry = instrument.expiry or previous_expiry
        if window_end < window_start:
            continue
        for start, end in chunk_dates(window_start, window_end, chunk_days):
            logger.info(
                f"[upstox {underlying}] {instrument.trading_symbol} "
                f"{interval} {start} -> {end} expired={instrument.expired}"
            )
            try:
                # CLASS_BULK: historical backfill — capped, yields to CRITICAL.
                from brokers.rate_limiter import CLASS_BULK, broker_class

                with broker_class(CLASS_BULK):
                    candles = await client.fetch_candles(
                        instrument,
                        interval=interval,
                        start=start,
                        end=end,
                    )
            except Exception as exc:
                summary.errors.append(f"{instrument.trading_symbol} {start}->{end}: {exc}")
                logger.warning(f"[upstox {underlying}] failed {start}->{end}: {exc}")
                continue
            summary.fetched_rows += len(candles)
            summary.stored_rows += await store_candles(instrument, candles, interval=interval)

    await _fill_summary_bounds(summary, interval=interval)
    return summary


async def _fill_summary_bounds(summary: SymbolBackfillSummary, *, interval: str) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT MIN(time) AS first_time, MAX(time) AS last_time
                FROM index_futures_candles
                WHERE underlying = :underlying
                  AND interval = :interval
                  AND source LIKE :source_prefix
                """
            ),
            {
                "underlying": summary.underlying,
                "interval": interval,
                "source_prefix": f"{summary.source.split('_')[0]}%",
            },
        )
        row = result.fetchone()
    if row:
        summary.first_time = row.first_time.isoformat() if row.first_time else None
        summary.last_time = row.last_time.isoformat() if row.last_time else None


async def backfill_index_futures(
    *,
    source: Literal["auto", "fyers", "upstox"],
    underlyings: list[str],
    from_date: date,
    to_date: date,
    interval: str = DEFAULT_INTERVAL,
    fyers_adapter: Any | None = None,
    upstox_access_token: str | None = None,
    fyers_symbols: dict[str, str] | None = None,
    chunk_days: int = 60,
    upstox_gap_seconds: float = 0.4,
    export: bool = True,
    output_root: Path | None = None,
) -> BackfillSummary:
    await ensure_index_futures_schema()
    normalized = normalize_underlyings(underlyings)
    output_root = output_root or (
        Path(__file__).resolve().parents[1]
        / "runtime"
        / "index_analytics_data"
        / "futures"
    )
    summary = BackfillSummary(
        started_at=datetime.now(timezone.utc).isoformat(),
        interval=interval,
        from_date=from_date.isoformat(),
        to_date=to_date.isoformat(),
    )

    upstox_client = (
        UpstoxFuturesHistoryClient(upstox_access_token, gap_seconds=upstox_gap_seconds)
        if upstox_access_token
        else None
    )

    for underlying in normalized:
        symbol_summary: SymbolBackfillSummary | None = None
        if source in {"auto", "fyers"} and fyers_adapter is not None:
            symbol_summary = await backfill_fyers_symbol(
                adapter=fyers_adapter,
                underlying=underlying,
                from_date=from_date,
                to_date=to_date,
                interval=interval,
                chunk_days=chunk_days,
                fyers_symbol=(fyers_symbols or {}).get(underlying),
            )
            if source == "fyers" or symbol_summary.stored_rows > 0:
                summary.symbols[underlying] = symbol_summary
                continue

        if source in {"auto", "upstox"} and upstox_client is not None:
            symbol_summary = await backfill_upstox_symbol(
                client=upstox_client,
                underlying=underlying,
                from_date=from_date,
                to_date=to_date,
                interval=interval,
                chunk_days=chunk_days,
            )
            summary.symbols[underlying] = symbol_summary
            continue

        if symbol_summary is None:
            symbol_summary = SymbolBackfillSummary(
                underlying=underlying,
                source=source,
                errors=["No usable broker session/token was available."],
            )
        summary.symbols[underlying] = symbol_summary

    if export:
        for underlying, item in summary.symbols.items():
            source_pattern = None
            if item.stored_rows > 0:
                if item.source == "fyers_continuous_futures":
                    source_pattern = item.source
                elif item.source == "upstox_futures":
                    source_pattern = "upstox%"
            item.exported_rows = await export_continuous_csv(
                underlying=underlying,
                interval=interval,
                from_date=from_date,
                to_date=to_date,
                source_pattern=source_pattern,
                output_root=output_root,
            )

    summary.finished_at = datetime.now(timezone.utc).isoformat()
    return summary
