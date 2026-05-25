"""Backfill data required for ATM CE/PE 15-minute Renko walk-forward.

This script is intentionally targeted. It does not download every option
strike. It backfills:
  1. Valid Upstox spot/underlying instrument keys for F&O symbols.
  2. Intraday spot candles for ATM selection.
  3. 1-minute option candles only for contracts that can be selected as ATM
     CE/PE from cached spot and available option-contract metadata.

The 1-minute option candles can then be resampled to a 15-minute Renko chart.

Run:
    docker compose exec backend python -m scripts.backfill_atm_renko_required_data

Useful smaller runs:
    docker compose exec backend python -m scripts.backfill_atm_renko_required_data --kind INDEX
    docker compose exec backend python -m scripts.backfill_atm_renko_required_data --symbols NIFTY BANKNIFTY
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pandas as pd
from sqlalchemy import text

from api.routers.auth import get_broker_token, refresh_persistent_credentials
from db.database import AsyncSessionLocal


UPSTOX_BASE = "https://api.upstox.com/v2"
REPORT_DIR = Path(__file__).parent.parent / "reports" / "backfill_atm_renko_required_data"
STATE_FILE = REPORT_DIR / "state.json"
IST = timezone(timedelta(hours=5, minutes=30))

INDEX_STEPS = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "FINNIFTY": 50,
    "MIDCPNIFTY": 25,
    "SENSEX": 100,
    "BANKEX": 100,
    "NIFTYNXT50": 50,
}


@dataclass(frozen=True)
class SymbolRow:
    symbol: str
    kind: str
    spot_key: str | None
    underlying_key: str | None


@dataclass(frozen=True)
class ContractNeed:
    instrument_key: str
    trading_symbol: str | None
    underlying: str
    market: str
    expiry: date
    strike: float
    option_type: str
    first_needed_day: date
    last_needed_day: date


def parse_iso_time(value: object) -> datetime:
    ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def ist_day(ts: pd.Timestamp) -> date:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert("Asia/Kolkata").date()


def chunk_dates(start: date, end: date, days: int) -> list[tuple[date, date]]:
    chunks: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=days - 1), end)
        chunks.append((cur, nxt))
        cur = nxt + timedelta(days=1)
    return chunks


class UpstoxClient:
    def __init__(self, token: str, gap_seconds: float) -> None:
        self.token = token
        self.gap_seconds = gap_seconds
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self.last_call = 0.0
        self.calls = 0
        self.rate_limit_hits = 0
        self._nse_master: list[dict] | None = None
        self._option_chain_cache: dict[tuple[str, date], list[dict]] = {}

    async def throttle(self) -> None:
        elapsed = time.monotonic() - self.last_call
        if elapsed < self.gap_seconds:
            await asyncio.sleep(self.gap_seconds - elapsed)
        self.last_call = time.monotonic()

    async def get(self, url: str, *, params: dict[str, Any] | None = None, timeout: float = 30.0) -> httpx.Response:
        response: httpx.Response | None = None
        for attempt in range(8):
            await self.throttle()
            self.calls += 1
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.get(url, params=params, headers=self.headers)
            except httpx.HTTPError:
                if attempt == 7:
                    raise
                await asyncio.sleep(min(30, 2 * (attempt + 1)))
                continue
            if response.status_code != 429:
                return response
            self.rate_limit_hits += 1
            await asyncio.sleep(5 * (attempt + 1))
        assert response is not None
        return response

    async def search_instrument(self, symbol: str, kind: str) -> dict | None:
        params = {
            "query": symbol,
            "exchanges": "NSE" if kind != "INDEX_BSE" else "BSE",
            "records": 20,
            "segments": "INDEX" if kind in {"INDEX", "INDEX_BSE"} else "EQ",
        }
        response = await self.get(f"{UPSTOX_BASE}/instruments/search", params=params)
        if response.status_code != 200:
            return None
        rows = response.json().get("data") or []
        if not rows:
            return None

        def score(row: dict) -> tuple[int, int, int]:
            trading_symbol = str(row.get("trading_symbol") or "").upper()
            name = str(row.get("name") or "").upper()
            short_name = str(row.get("short_name") or "").upper()
            segment = str(row.get("segment") or "")
            exact = int(symbol.upper() in {trading_symbol, name, short_name})
            preferred = int(segment in {"NSE_EQ", "NSE_INDEX", "BSE_INDEX"})
            isin_key = int(str(row.get("instrument_key") or "").startswith(("NSE_EQ|", "NSE_INDEX|", "BSE_INDEX|")))
            return exact, preferred, isin_key

        rows.sort(key=score, reverse=True)
        return rows[0]

    async def nse_master(self) -> list[dict]:
        if self._nse_master is not None:
            return self._nse_master
        response = await self.get(
            "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz",
            timeout=90.0,
        )
        if response.status_code != 200:
            self._nse_master = []
            return self._nse_master
        raw = response.content
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
        self._nse_master = json.loads(raw)
        return self._nse_master

    async def find_stock_in_master(self, symbol: str) -> dict | None:
        rows = await self.nse_master()
        symbol_upper = symbol.upper()
        for row in rows:
            if (
                str(row.get("segment") or "") == "NSE_EQ"
                and str(row.get("instrument_type") or "") == "EQ"
                and str(row.get("trading_symbol") or "").upper() == symbol_upper
            ):
                return row
        return None

    async def fetch_candles(
        self,
        instrument_key: str,
        interval: str,
        start: date,
        end: date,
        *,
        expired: bool = False,
    ) -> list[list]:
        encoded = quote(instrument_key, safe="")
        prefix = "expired-instruments/historical-candle" if expired else "historical-candle"
        url = f"{UPSTOX_BASE}/{prefix}/{encoded}/{interval}/{end.isoformat()}/{start.isoformat()}"
        response = await self.get(url)
        if response.status_code == 200:
            return response.json().get("data", {}).get("candles", []) or []
        return []

    async def option_chain(self, underlying_key: str, expiry: date) -> list[dict]:
        cache_key = (underlying_key, expiry)
        if cache_key in self._option_chain_cache:
            return self._option_chain_cache[cache_key]
        encoded = quote(underlying_key, safe="")
        url = f"{UPSTOX_BASE}/option/chain?instrument_key={encoded}&expiry_date={expiry.isoformat()}"
        response = await self.get(url)
        if response.status_code != 200:
            self._option_chain_cache[cache_key] = []
            return []
        rows = response.json().get("data") or []
        self._option_chain_cache[cache_key] = rows
        return rows


async def load_symbols(kind: str, symbols: list[str]) -> list[SymbolRow]:
    params: dict[str, Any] = {}
    clauses = ["kind IN ('INDEX', 'STOCK')"]
    if kind != "ALL":
        clauses.append("kind = :kind")
        params["kind"] = kind
    if symbols:
        clauses.append("symbol = ANY(:symbols)")
        params["symbols"] = [s.upper() for s in symbols]
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT symbol, kind, spot_instrument_key, underlying_key
                    FROM fo_underlying_catalog
                    WHERE {' AND '.join(clauses)}
                    ORDER BY CASE WHEN kind = 'INDEX' THEN 0 ELSE 1 END, symbol
                    """
                ),
                params,
            )
        ).mappings().all()
    return [
        SymbolRow(
            symbol=str(row["symbol"]),
            kind=str(row["kind"]),
            spot_key=row["spot_instrument_key"],
            underlying_key=row["underlying_key"],
        )
        for row in rows
    ]


async def repair_symbol_keys(client: UpstoxClient, symbols: list[SymbolRow], force: bool) -> int:
    repaired = 0
    for row in symbols:
        needs_repair = force or not row.spot_key or not row.underlying_key
        if row.kind == "STOCK" and row.spot_key and not row.spot_key.startswith("NSE_EQ|"):
            needs_repair = True
        if row.kind == "INDEX" and row.symbol == "BANKEX":
            search_kind = "INDEX_BSE"
        else:
            search_kind = row.kind
        if not needs_repair:
            continue

        found = await client.search_instrument(row.symbol, search_kind)
        if not found and row.kind == "STOCK":
            found = await client.find_stock_in_master(row.symbol)
        if not found:
            print(f"  key repair skipped {row.symbol}: no Upstox search result")
            continue
        key = str(found.get("instrument_key") or "")
        if not key:
            print(f"  key repair skipped {row.symbol}: missing instrument_key")
            continue
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    """
                    UPDATE fo_underlying_catalog
                    SET spot_instrument_key = :key,
                        underlying_key = :key,
                        updated_at = NOW()
                    WHERE symbol = :symbol
                    """
                ),
                {"symbol": row.symbol, "key": key},
            )
            await session.commit()
        repaired += 1
        print(f"  repaired {row.symbol}: {row.spot_key} -> {key}")
    return repaired


async def load_repaired_symbols(kind: str, symbols: list[str]) -> list[SymbolRow]:
    return await load_symbols(kind, symbols)


async def persist_spot(symbol: SymbolRow, interval: str, candles: list[list]) -> int:
    if not candles or not symbol.spot_key:
        return 0
    rows = []
    for candle in candles:
        rows.append(
            {
                "time": parse_iso_time(candle[0]),
                "instrument_key": symbol.spot_key,
                "underlying": symbol.symbol,
                "interval": interval,
                "open": float(candle[1]),
                "high": float(candle[2]),
                "low": float(candle[3]),
                "close": float(candle[4]),
                "volume": int(candle[5] or 0),
                "oi": int(candle[6] or 0) if len(candle) > 6 and candle[6] is not None else None,
            }
        )
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                INSERT INTO underlying_spot_candles (
                    time, instrument_key, underlying, interval, open, high, low,
                    close, volume, oi, source, synced_at
                )
                VALUES (
                    :time, :instrument_key, :underlying, :interval, :open, :high,
                    :low, :close, :volume, :oi, 'upstox', NOW()
                )
                ON CONFLICT (instrument_key, interval, time) DO UPDATE
                SET open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume,
                    oi = EXCLUDED.oi,
                    synced_at = NOW()
                """
            ),
            rows,
        )
        await session.commit()
    return len(rows)


async def existing_spot_rows(symbol: SymbolRow, interval: str, start: date, end: date) -> int:
    async with AsyncSessionLocal() as session:
        count = await session.scalar(
            text(
                """
                SELECT COUNT(*)
                FROM underlying_spot_candles
                WHERE underlying = :symbol
                  AND interval = :interval
                  AND time >= :start
                  AND time < (CAST(:end AS date) + INTERVAL '1 day')
                """
            ),
            {"symbol": symbol.symbol, "interval": interval, "start": start, "end": end},
        )
    return int(count or 0)


async def backfill_spot(
    client: UpstoxClient,
    symbols: list[SymbolRow],
    interval: str,
    start: date,
    end: date,
    chunk_days: int,
) -> int:
    stored = 0
    for idx, symbol in enumerate(symbols, 1):
        if not symbol.spot_key:
            print(f"[spot {idx}/{len(symbols)}] {symbol.symbol}: missing key")
            continue
        symbol_rows = 0
        for chunk_start, chunk_end in chunk_dates(start, end, chunk_days):
            existing = await existing_spot_rows(symbol, interval, chunk_start, chunk_end)
            if existing >= 100:
                print(
                    f"[spot {idx}/{len(symbols)}] {symbol.symbol} "
                    f"{chunk_start}->{chunk_end}: skip cached rows={existing}"
                )
                continue
            candles = await client.fetch_candles(symbol.spot_key, interval, chunk_start, chunk_end)
            if not candles:
                continue
            symbol_rows += await persist_spot(symbol, interval, candles)
        stored += symbol_rows
        print(f"[spot {idx}/{len(symbols)}] {symbol.symbol}: stored/upserted {symbol_rows}")
    return stored


async def load_spot_frame(symbol: str, interval: str, start: date, end: date) -> pd.DataFrame:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT time, close
                    FROM underlying_spot_candles
                    WHERE underlying = :symbol
                      AND interval = :interval
                      AND time >= :start
                      AND time < (CAST(:end AS date) + INTERVAL '1 day')
                      AND close > 0
                    ORDER BY time
                    """
                ),
                {"symbol": symbol, "interval": interval, "start": start, "end": end},
            )
        ).mappings().all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(row) for row in rows])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df["close"] = df["close"].astype(float)
    return df.drop_duplicates("time", keep="last").sort_values("time").reset_index(drop=True)


async def load_contract_catalog(symbol: str, start: date, end: date) -> pd.DataFrame:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT
                        underlying,
                        COALESCE(instrument_key, '') AS instrument_key,
                        trading_symbol,
                        market,
                        expiry,
                        strike::float AS strike,
                        option_type,
                        MIN(timezone('Asia/Kolkata', time)::date) AS first_day,
                        MAX(timezone('Asia/Kolkata', time)::date) AS last_day
                    FROM option_premium_candles
                    WHERE underlying = :symbol
                      AND interval = '30minute'
                      AND instrument_key IS NOT NULL
                      AND instrument_key <> ''
                      AND time >= (CAST(:start AS date) - INTERVAL '30 days')
                      AND time < (CAST(:end AS date) + INTERVAL '1 day')
                    GROUP BY underlying, instrument_key, trading_symbol, market, expiry, strike, option_type
                    ORDER BY expiry, strike, option_type
                    """
                ),
                {"symbol": symbol, "start": start, "end": end},
            )
        ).mappings().all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(row) for row in rows])
    df["expiry"] = pd.to_datetime(df["expiry"]).dt.date
    df["strike"] = df["strike"].astype(float)
    return df


def strike_step(symbol: str, contracts: pd.DataFrame) -> int:
    if symbol in INDEX_STEPS:
        return INDEX_STEPS[symbol]
    strikes = sorted({int(round(s)) for s in contracts["strike"].tolist()})
    gaps = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
    return int(min(gaps)) if gaps else 1


def round_atm(price: float, step: int) -> int:
    return int(round(price / step) * step)


def choose_contract(
    contracts: pd.DataFrame,
    atm: int,
    option_type: str,
    trading_day: date,
) -> dict | None:
    candidates = contracts[
        (contracts["strike"].round().astype(int) == atm)
        & (contracts["option_type"] == option_type)
        & (contracts["expiry"] >= trading_day)
    ].copy()
    if candidates.empty:
        return None
    candidates["days_to_expiry"] = candidates["expiry"].map(lambda expiry: (expiry - trading_day).days)
    candidates = candidates.sort_values(["days_to_expiry", "expiry"])
    return dict(candidates.iloc[0])


async def compute_needed_contracts(symbols: list[SymbolRow], spot_interval: str, start: date, end: date) -> list[ContractNeed]:
    needs: dict[str, ContractNeed] = {}
    for idx, symbol in enumerate(symbols, 1):
        spot = await load_spot_frame(symbol.symbol, spot_interval, start, end)
        contracts = await load_contract_catalog(symbol.symbol, start, end)
        if spot.empty or contracts.empty:
            print(f"[needs {idx}/{len(symbols)}] {symbol.symbol}: spot={len(spot)} contracts={len(contracts)}")
            continue
        step = strike_step(symbol.symbol, contracts)
        spot_points = spot.copy()
        spot_points["trading_day"] = spot_points["time"].map(ist_day)
        spot_points["atm"] = (spot_points["close"] / step).round().astype(int) * step
        spot_points = spot_points[["trading_day", "atm"]].drop_duplicates()
        found = 0
        missing = 0
        for row in spot_points.itertuples(index=False):
            trading_day = row.trading_day
            atm = int(row.atm)
            for option_type in ("CE", "PE"):
                selected = choose_contract(contracts, atm, option_type, trading_day)
                if not selected:
                    missing += 1
                    continue
                key = str(selected["instrument_key"])
                prev = needs.get(key)
                first_day = trading_day if prev is None else min(prev.first_needed_day, trading_day)
                last_day = trading_day if prev is None else max(prev.last_needed_day, trading_day)
                needs[key] = ContractNeed(
                    instrument_key=key,
                    trading_symbol=selected.get("trading_symbol"),
                    underlying=symbol.symbol,
                    market=str(selected.get("market") or ("BSE" if symbol.symbol in {"SENSEX", "BANKEX"} else "NSE")),
                    expiry=selected["expiry"],
                    strike=float(selected["strike"]),
                    option_type=str(selected["option_type"]),
                    first_needed_day=first_day,
                    last_needed_day=last_day,
                )
                found += 1
        print(
            f"[needs {idx}/{len(symbols)}] {symbol.symbol}: "
            f"spot_rows={len(spot)} atm_points={len(spot_points)} "
            f"contracts={len({n.instrument_key for n in needs.values() if n.underlying == symbol.symbol})} "
            f"found_points={found} missing_points={missing}"
        )
    return list(needs.values())


async def existing_option_rows(instrument_key: str, interval: str, start: date, end: date) -> int:
    async with AsyncSessionLocal() as session:
        count = await session.scalar(
            text(
                """
                SELECT COUNT(*)
                FROM option_premium_candles
                WHERE instrument_key = :instrument_key
                  AND interval = :interval
                  AND time >= :start
                  AND time < (CAST(:end AS date) + INTERVAL '1 day')
                """
            ),
            {"instrument_key": instrument_key, "interval": interval, "start": start, "end": end},
        )
    return int(count or 0)


async def get_underlying_key(symbol: str) -> str | None:
    async with AsyncSessionLocal() as session:
        value = await session.scalar(
            text("SELECT underlying_key FROM fo_underlying_catalog WHERE symbol = :symbol"),
            {"symbol": symbol},
        )
    return str(value) if value else None


async def persist_option(contract: ContractNeed, candles: list[list], interval: str) -> int:
    if not candles:
        return 0
    rows = []
    for candle in candles:
        rows.append(
            {
                "time": parse_iso_time(candle[0]),
                "instrument_key": contract.instrument_key,
                "trading_symbol": contract.trading_symbol,
                "underlying": contract.underlying,
                "market": contract.market,
                "expiry": contract.expiry,
                "strike": contract.strike,
                "option_type": contract.option_type,
                "interval": interval,
                "open": float(candle[1]),
                "high": float(candle[2]),
                "low": float(candle[3]),
                "close": float(candle[4]),
                "volume": int(candle[5] or 0),
                "oi": int(candle[6] or 0) if len(candle) > 6 and candle[6] is not None else None,
            }
        )
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                INSERT INTO option_premium_candles (
                    time, instrument_key, trading_symbol, underlying, market, expiry,
                    strike, option_type, interval, open, high, low, close, volume,
                    oi, source, synced_at
                )
                VALUES (
                    :time, :instrument_key, :trading_symbol, :underlying, :market,
                    :expiry, :strike, :option_type, :interval, :open, :high, :low,
                    :close, :volume, :oi, 'upstox', NOW()
                )
                ON CONFLICT (instrument_key, interval, time) DO UPDATE
                SET trading_symbol = EXCLUDED.trading_symbol,
                    underlying = EXCLUDED.underlying,
                    market = EXCLUDED.market,
                    expiry = EXCLUDED.expiry,
                    strike = EXCLUDED.strike,
                    option_type = EXCLUDED.option_type,
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume,
                    oi = EXCLUDED.oi,
                    synced_at = NOW()
                """
            ),
            rows,
        )
        await session.commit()
    return len(rows)


async def fetch_option_with_fallback(
    client: UpstoxClient,
    contract: ContractNeed,
    interval: str,
    start: date,
    end: date,
) -> tuple[ContractNeed, list[list]]:
    expired = contract.expiry < date.today()
    candles = await client.fetch_candles(contract.instrument_key, interval, start, end, expired=expired)
    if candles:
        return contract, candles
    # Some locally cached contracts use legacy keys. Try the other endpoint too.
    candles = await client.fetch_candles(contract.instrument_key, interval, start, end, expired=not expired)
    if candles:
        return contract, candles

    if expired:
        return contract, []

    underlying_key = await get_underlying_key(contract.underlying)
    if not underlying_key:
        return contract, []
    chain = await client.option_chain(underlying_key, contract.expiry)
    if not chain:
        return contract, []
    target_strike = int(round(contract.strike))
    side_key = "call_options" if contract.option_type == "CE" else "put_options"
    for row in chain:
        strike = int(round(float(row.get("strike_price") or 0)))
        if strike != target_strike:
            continue
        option_row = row.get(side_key) or {}
        active_key = str(option_row.get("instrument_key") or "")
        if not active_key:
            return contract, []
        active_contract = ContractNeed(
            instrument_key=active_key,
            trading_symbol=contract.trading_symbol,
            underlying=contract.underlying,
            market=contract.market,
            expiry=contract.expiry,
            strike=contract.strike,
            option_type=contract.option_type,
            first_needed_day=contract.first_needed_day,
            last_needed_day=contract.last_needed_day,
        )
        candles = await client.fetch_candles(active_key, interval, start, end, expired=False)
        return active_contract, candles
    return contract, []


async def backfill_options(
    client: UpstoxClient,
    contracts: list[ContractNeed],
    start: date,
    end: date,
    chunk_days: int,
    contract_limit: int,
    option_interval: str,
    skip_expired: bool,
) -> tuple[int, int]:
    stored = 0
    processed = 0
    for idx, contract in enumerate(contracts, 1):
        if contract_limit and processed >= contract_limit:
            break
        if skip_expired and contract.expiry < date.today():
            print(
                f"[opt {idx}/{len(contracts)}] skip expired "
                f"{contract.underlying} {contract.strike:.0f}{contract.option_type} {contract.expiry}"
            )
            continue
        fetch_start = max(start, contract.first_needed_day - timedelta(days=14))
        fetch_end = min(end, contract.expiry, contract.last_needed_day + timedelta(days=1))
        if fetch_end < fetch_start:
            continue
        contract_rows = 0
        for chunk_start, chunk_end in chunk_dates(fetch_start, fetch_end, chunk_days):
            existing = await existing_option_rows(contract.instrument_key, option_interval, chunk_start, chunk_end)
            if existing >= 50:
                print(
                    f"[opt {idx}/{len(contracts)}] skip cached "
                    f"{contract.underlying} {contract.strike:.0f}{contract.option_type} "
                    f"{contract.expiry} {option_interval} {chunk_start}->{chunk_end} rows={existing}"
                )
                continue
            store_contract, candles = await fetch_option_with_fallback(client, contract, option_interval, chunk_start, chunk_end)
            if not candles:
                continue
            contract_rows += await persist_option(store_contract, candles, option_interval)
        processed += 1
        stored += contract_rows
        print(f"[opt {idx}/{len(contracts)}] {contract.underlying} {contract.strike:.0f}{contract.option_type} {contract.expiry}: upserted {contract_rows}")
    return processed, stored


def write_state(payload: dict) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(payload, indent=2, default=str))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-date", default="2026-01-01")
    parser.add_argument("--to-date", default=date.today().isoformat())
    parser.add_argument("--kind", choices=["ALL", "INDEX", "STOCK"], default="ALL")
    parser.add_argument("--symbols", nargs="*", default=[])
    parser.add_argument("--spot-interval", default="5minute")
    parser.add_argument("--spot-chunk-days", type=int, default=30)
    parser.add_argument("--option-interval", default="1minute")
    parser.add_argument("--option-chunk-days", type=int, default=14)
    parser.add_argument("--gap-seconds", type=float, default=0.35)
    parser.add_argument("--skip-key-repair", action="store_true")
    parser.add_argument("--force-key-repair", action="store_true")
    parser.add_argument("--skip-spot", action="store_true")
    parser.add_argument("--skip-options", action="store_true")
    parser.add_argument("--skip-expired-options", action="store_true")
    parser.add_argument("--contract-limit", type=int, default=0)
    parser.add_argument("--max-symbols", type=int, default=0)
    return parser.parse_args()


async def main_async() -> None:
    args = parse_args()
    start = date.fromisoformat(args.from_date)
    end = date.fromisoformat(args.to_date)

    refresh_persistent_credentials(force=True)
    token = get_broker_token("upstox", allow_analytics_token=False)
    if not token:
        print("No Upstox token available.")
        sys.exit(1)

    client = UpstoxClient(token, args.gap_seconds)
    symbols = await load_symbols(args.kind, args.symbols)
    if args.max_symbols:
        symbols = symbols[: args.max_symbols]
    print(f"symbols selected: {len(symbols)} kind={args.kind} range={start} -> {end}")

    repaired = 0
    if not args.skip_key_repair:
        repaired = await repair_symbol_keys(client, symbols, args.force_key_repair)
        symbols = await load_repaired_symbols(args.kind, args.symbols)
        if args.max_symbols:
            symbols = symbols[: args.max_symbols]

    spot_rows = 0
    if not args.skip_spot:
        spot_rows = await backfill_spot(client, symbols, args.spot_interval, start, end, args.spot_chunk_days)

    option_contracts = 0
    option_processed = 0
    option_rows = 0
    if not args.skip_options:
        needs = await compute_needed_contracts(symbols, args.spot_interval, start, end)
        needs.sort(key=lambda c: (c.underlying, c.expiry, c.strike, c.option_type))
        option_contracts = len(needs)
        print(f"required option contracts: {option_contracts}")
        option_processed, option_rows = await backfill_options(
            client,
            needs,
            start,
            end,
            args.option_chunk_days,
            args.contract_limit,
            args.option_interval,
            args.skip_expired_options,
        )

    payload = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "kind": args.kind,
        "symbols": [s.symbol for s in symbols],
        "key_repairs": repaired,
        "spot_rows_upserted": spot_rows,
        "required_option_contracts": option_contracts,
        "option_contracts_processed": option_processed,
        "option_rows_upserted": option_rows,
        "option_interval": args.option_interval,
        "api_calls": client.calls,
        "rate_limit_hits": client.rate_limit_hits,
    }
    write_state(payload)
    print(json.dumps(payload, indent=2))


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
