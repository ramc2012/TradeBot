"""Import previously downloaded Upstox candle archives into TimescaleDB.

Sources supported:
  1. data/spot_candles/*.parquet and data/option_candles/*.parquet
  2. backend/runtime/index_analytics_data/contracts/**/*.csv.gz

The Parquet archive is mostly 30-minute data across the F&O universe. The
compressed contract archive is 1-minute NIFTY/SENSEX option data.
"""
from __future__ import annotations

import argparse
import asyncio
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import text

from db.database import AsyncSessionLocal


ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = ROOT / "data"
CONTRACT_ARCHIVE_ROOT = ROOT / "backend" / "runtime" / "index_analytics_data" / "contracts"
BATCH_SIZE = 1_500


@dataclass(frozen=True)
class ImportStats:
    files: int = 0
    rows_seen: int = 0
    rows_upserted: int = 0


def _utc_series(values: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(values, utc=True, errors="coerce")
    return parsed.map(lambda value: value.to_pydatetime() if pd.notna(value) else None)


def _clean_float(value: Any) -> float | None:
    if pd.isna(value):
        return None
    return float(value)


def _clean_int(value: Any) -> int | None:
    if pd.isna(value):
        return None
    return int(value)


def _date_value(value: Any) -> date | None:
    if pd.isna(value):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return pd.Timestamp(value).date()


def _chunks(rows: list[dict[str, Any]], size: int = BATCH_SIZE):
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


async def _execute_batches(sql: str, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    total = 0
    async with AsyncSessionLocal() as session:
        for batch in _chunks(rows):
            await session.execute(text(sql), batch)
            total += len(batch)
        await session.commit()
    return total


async def import_spot_parquet(dry_run: bool = False, limit_files: int = 0) -> ImportStats:
    catalog_path = DATA_ROOT / "catalogs" / "underlyings.parquet"
    if not catalog_path.exists():
        print("spot parquet: missing underlyings catalog")
        return ImportStats()

    catalog = pd.read_parquet(catalog_path)
    key_map = {
        str(row.symbol): str(row.spot_instrument_key)
        for row in catalog.itertuples(index=False)
        if getattr(row, "spot_instrument_key", None)
    }
    files = sorted((DATA_ROOT / "spot_candles").glob("*.parquet"))
    if limit_files:
        files = files[:limit_files]

    sql = """
        INSERT INTO underlying_spot_candles (
            time, instrument_key, underlying, interval, open, high, low, close,
            volume, oi, source, synced_at
        )
        VALUES (
            :time, :instrument_key, :underlying, :interval, :open, :high, :low,
            :close, :volume, :oi, 'upstox_local_parquet', NOW()
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

    seen = 0
    upserted = 0
    for idx, path in enumerate(files, 1):
        df = pd.read_parquet(path)
        df["instrument_key"] = df["underlying"].map(key_map)
        df = df[df["instrument_key"].notna()].copy()
        df["time"] = _utc_series(df["time"])
        rows = [
            {
                "time": row.time,
                "instrument_key": str(row.instrument_key),
                "underlying": str(row.underlying),
                "interval": "30minute",
                "open": _clean_float(row.open),
                "high": _clean_float(row.high),
                "low": _clean_float(row.low),
                "close": _clean_float(row.close),
                "volume": _clean_int(row.volume) or 0,
                "oi": _clean_int(row.oi),
            }
            for row in df.itertuples(index=False)
            if row.time is not None
        ]
        seen += len(rows)
        inserted = 0 if dry_run else await _execute_batches(sql, rows)
        upserted += inserted
        print(f"[spot parquet {idx}/{len(files)}] {path.name}: rows={len(rows)} upserted={inserted}")
    return ImportStats(len(files), seen, upserted)


def _contract_catalog() -> pd.DataFrame:
    path = DATA_ROOT / "catalogs" / "contracts.parquet"
    cols = ["instrument_key", "trading_symbol", "underlying", "expiry", "strike", "option_type"]
    if not path.exists():
        return pd.DataFrame(columns=cols)
    catalog = pd.read_parquet(path, columns=cols)
    catalog["expiry"] = pd.to_datetime(catalog["expiry"]).dt.date
    catalog["strike"] = catalog["strike"].astype(float)
    catalog["option_type"] = catalog["option_type"].astype(str)
    return catalog.drop_duplicates(["underlying", "expiry", "strike", "option_type"], keep="first")


async def import_option_parquet(dry_run: bool = False, limit_files: int = 0) -> ImportStats:
    catalog = _contract_catalog()
    files = sorted((DATA_ROOT / "option_candles").glob("*.parquet"))
    if limit_files:
        files = files[:limit_files]

    sql = """
        INSERT INTO option_premium_candles (
            time, underlying, market, expiry, strike, option_type, open, high,
            low, close, volume, oi, iv, delta, underlying_price, instrument_key,
            trading_symbol, interval, gamma, theta, vega, source, synced_at,
            time_to_expiry_years
        )
        VALUES (
            :time, :underlying, :market, :expiry, :strike, :option_type, :open,
            :high, :low, :close, :volume, :oi, :iv, :delta, :underlying_price,
            :instrument_key, :trading_symbol, :interval, :gamma, :theta, :vega,
            'upstox_local_parquet', NOW(), :time_to_expiry_years
        )
        ON CONFLICT (instrument_key, interval, time) DO UPDATE
        SET open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume,
            oi = EXCLUDED.oi,
            iv = EXCLUDED.iv,
            delta = EXCLUDED.delta,
            gamma = EXCLUDED.gamma,
            theta = EXCLUDED.theta,
            vega = EXCLUDED.vega,
            underlying_price = EXCLUDED.underlying_price,
            trading_symbol = EXCLUDED.trading_symbol,
            synced_at = NOW(),
            time_to_expiry_years = EXCLUDED.time_to_expiry_years
    """

    seen = 0
    upserted = 0
    merge_keys = ["underlying", "expiry", "strike", "option_type"]
    for idx, path in enumerate(files, 1):
        df = pd.read_parquet(path)
        df["expiry"] = pd.to_datetime(df["expiry"]).dt.date
        df["strike"] = df["strike"].astype(float)
        df["option_type"] = df["option_type"].astype(str)
        if not catalog.empty:
            df = df.merge(catalog, how="left", on=merge_keys)
        else:
            df["instrument_key"] = None
            df["trading_symbol"] = None
        missing_key = df["instrument_key"].isna()
        if missing_key.any():
            df.loc[missing_key, "instrument_key"] = df.loc[missing_key].apply(
                lambda row: (
                    f"LOCAL_PARQUET|{row['underlying']}|{row['expiry']}|"
                    f"{float(row['strike']):.2f}|{row['option_type']}"
                ),
                axis=1,
            )
        df["time"] = _utc_series(df["time"])
        rows = [
            {
                "time": row.time,
                "underlying": str(row.underlying),
                "market": "BSE" if str(row.underlying) in {"SENSEX", "BANKEX"} else "NSE",
                "expiry": _date_value(row.expiry),
                "strike": _clean_float(row.strike),
                "option_type": str(row.option_type),
                "open": _clean_float(row.open),
                "high": _clean_float(row.high),
                "low": _clean_float(row.low),
                "close": _clean_float(row.close),
                "volume": _clean_int(row.volume) or 0,
                "oi": _clean_int(row.oi),
                "iv": _clean_float(getattr(row, "iv", None)),
                "delta": _clean_float(getattr(row, "delta", None)),
                "gamma": _clean_float(getattr(row, "gamma", None)),
                "theta": _clean_float(getattr(row, "theta", None)),
                "vega": _clean_float(getattr(row, "vega", None)),
                "underlying_price": _clean_float(getattr(row, "underlying_price", None)),
                "time_to_expiry_years": _clean_float(getattr(row, "time_to_expiry_years", None)),
                "instrument_key": str(row.instrument_key),
                "trading_symbol": None if pd.isna(row.trading_symbol) else str(row.trading_symbol),
                "interval": "30minute",
            }
            for row in df.itertuples(index=False)
            if row.time is not None
        ]
        seen += len(rows)
        inserted = 0 if dry_run else await _execute_batches(sql, rows)
        upserted += inserted
        print(f"[option parquet {idx}/{len(files)}] {path.name}: rows={len(rows)} upserted={inserted}")
    return ImportStats(len(files), seen, upserted)


def _parse_archive_path(path: Path) -> dict[str, Any] | None:
    values: dict[str, str] = {}
    relative = path.relative_to(CONTRACT_ARCHIVE_ROOT)
    for part in relative.parts:
        if "=" in part:
            key, value = part.split("=", 1)
            values[key] = value
    side = path.parent.name
    if side not in {"CE", "PE"}:
        return None
    trading_symbol = path.name.removesuffix(".csv.gz")
    match = re.search(r"\s([0-9]+(?:\.[0-9]+)?)\s(CE|PE)\s", trading_symbol)
    if not match:
        return None
    return {
        "underlying": values.get("underlying"),
        "expiry_kind": values.get("expiry_kind"),
        "expiry": date.fromisoformat(values["expiry"]),
        "option_type": side,
        "strike": float(match.group(1)),
        "trading_symbol": trading_symbol,
        "instrument_key": (
            f"LOCAL_CSV|{values.get('underlying')}|{values.get('expiry')}|"
            f"{float(match.group(1)):.2f}|{side}|{values.get('expiry_kind')}"
        ),
    }


async def import_contract_csv(dry_run: bool = False, limit_files: int = 0) -> ImportStats:
    files = sorted(CONTRACT_ARCHIVE_ROOT.rglob("*.csv.gz"))
    if limit_files:
        files = files[:limit_files]

    sql = """
        INSERT INTO option_premium_candles (
            time, underlying, market, expiry, strike, option_type, open, high,
            low, close, volume, oi, instrument_key, trading_symbol, interval,
            source, synced_at
        )
        VALUES (
            :time, :underlying, :market, :expiry, :strike, :option_type, :open,
            :high, :low, :close, :volume, :oi, :instrument_key, :trading_symbol,
            '1minute', 'upstox_local_csv', NOW()
        )
        ON CONFLICT (instrument_key, interval, time) DO UPDATE
        SET open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume,
            oi = EXCLUDED.oi,
            trading_symbol = EXCLUDED.trading_symbol,
            synced_at = NOW()
    """

    seen = 0
    upserted = 0
    for idx, path in enumerate(files, 1):
        meta = _parse_archive_path(path)
        if not meta:
            print(f"[contract csv {idx}/{len(files)}] skipped unparsable {path}")
            continue
        df = pd.read_csv(path, compression="gzip")
        if df.empty:
            continue
        df["time"] = _utc_series(df["time"])
        rows = [
            {
                "time": row.time,
                "underlying": str(meta["underlying"]),
                "market": "BSE" if meta["underlying"] in {"SENSEX", "BANKEX"} else "NSE",
                "expiry": meta["expiry"],
                "strike": meta["strike"],
                "option_type": meta["option_type"],
                "open": _clean_float(row.open),
                "high": _clean_float(row.high),
                "low": _clean_float(row.low),
                "close": _clean_float(row.close),
                "volume": _clean_int(row.volume) or 0,
                "oi": _clean_int(row.oi),
                "instrument_key": str(meta["instrument_key"]),
                "trading_symbol": str(meta["trading_symbol"]),
            }
            for row in df.itertuples(index=False)
            if row.time is not None
        ]
        seen += len(rows)
        inserted = 0 if dry_run else await _execute_batches(sql, rows)
        upserted += inserted
        print(
            f"[contract csv {idx}/{len(files)}] {meta['underlying']} "
            f"{meta['strike']:.0f}{meta['option_type']} {meta['expiry']}: "
            f"rows={len(rows)} upserted={inserted}"
        )
    return ImportStats(len(files), seen, upserted)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["all", "spot-parquet", "option-parquet", "contract-csv"], default="all")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit-files", type=int, default=0)
    args = parser.parse_args()

    totals: dict[str, ImportStats] = {}
    if args.source in {"all", "spot-parquet"}:
        totals["spot_parquet"] = await import_spot_parquet(args.dry_run, args.limit_files)
    if args.source in {"all", "option-parquet"}:
        totals["option_parquet"] = await import_option_parquet(args.dry_run, args.limit_files)
    if args.source in {"all", "contract-csv"}:
        totals["contract_csv"] = await import_contract_csv(args.dry_run, args.limit_files)

    print("\nsummary")
    for name, stats in totals.items():
        print(
            f"{name}: files={stats.files} rows_seen={stats.rows_seen} "
            f"rows_upserted={stats.rows_upserted}"
        )


if __name__ == "__main__":
    asyncio.run(main())
