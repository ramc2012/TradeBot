"""Audit option history coverage before gamma-wall research.

The directional wall tests need a real option-chain surface at each timestamp.
ATM-only or two-strike history is useful for premium studies, but it cannot
produce reliable call/put gamma walls. This script reads the local parquet
cache and emits a coverage report that marks sparse expiry buckets as unfit for
wall research.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SYMBOLS = [
    "NIFTY",
    "BANKNIFTY",
    "RELIANCE",
    "HDFCBANK",
    "ICICIBANK",
    "SBIN",
    "TCS",
    "INFY",
    "AXISBANK",
    "ITC",
]
DEFAULT_OUT_DIR = (
    REPO_ROOT
    / "backend"
    / "runtime"
    / "directional_options"
    / "research"
    / "wall_data_audit"
)
INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}


@dataclass
class CoverageRow:
    underlying: str
    kind: str
    expiry: str
    catalog_contracts: int = 0
    catalog_strikes: int = 0
    catalog_contracts_with_candles: int = 0
    catalog_candle_rows: int = 0
    cache_rows: int = 0
    cache_timestamps: int = 0
    cache_unique_strikes: int = 0
    min_strikes_per_time: float | None = None
    median_strikes_per_time: float | None = None
    p75_strikes_per_time: float | None = None
    max_strikes_per_time: float | None = None
    required_strikes_per_time: int = 0
    contract_candle_coverage_pct: float = 0.0
    wall_ready: bool = False
    reason: str = ""


def _parse_symbols(raw: str | None) -> list[str]:
    if not raw:
        return DEFAULT_SYMBOLS.copy()
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    return date.fromisoformat(raw)


def _date_mask(series: pd.Series, start: date | None, end: date | None) -> pd.Series:
    parsed = pd.to_datetime(series, errors="coerce", utc=True)
    mask = parsed.notna()
    if start is not None:
        mask &= parsed.dt.date >= start
    if end is not None:
        mask &= parsed.dt.date <= end
    return mask


def _kind_for(symbol: str, underlyings: pd.DataFrame | None) -> str:
    if underlyings is not None and not underlyings.empty:
        match = underlyings[underlyings["symbol"].astype(str).str.upper() == symbol]
        if not match.empty:
            return str(match.iloc[0].get("kind") or "").upper() or "INDEX" if symbol in INDEX_SYMBOLS else "STOCK"
    return "INDEX" if symbol in INDEX_SYMBOLS else "STOCK"


def _read_underlying_catalog(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    if "symbol" not in df.columns:
        return None
    return df


def _read_contract_catalog(path: Path, symbols: Iterable[str], start: date | None, end: date | None) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path) if path.suffix.lower() == ".csv" else pd.read_parquet(path)
    required = {"underlying", "expiry", "strike", "instrument_key"}
    if not required.issubset(df.columns):
        return pd.DataFrame()
    df = df[df["underlying"].astype(str).str.upper().isin(set(symbols))].copy()
    df["underlying"] = df["underlying"].astype(str).str.upper()
    df["expiry"] = pd.to_datetime(df["expiry"], errors="coerce").dt.date
    if start is not None:
        df = df[df["expiry"] >= start]
    if end is not None:
        df = df[df["expiry"] <= end]
    return df


def _read_option_cache(path: Path, symbols: Iterable[str], start: date | None, end: date | None) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    symbol_set = set(symbols)
    for parquet in sorted(path.glob("*.parquet")):
        df = pd.read_parquet(parquet)
        needed = {"time", "underlying", "expiry", "strike", "option_type"}
        if not needed.issubset(df.columns):
            continue
        df = df[df["underlying"].astype(str).str.upper().isin(symbol_set)].copy()
        if df.empty:
            continue
        df["underlying"] = df["underlying"].astype(str).str.upper()
        df = df[_date_mask(df["time"], start, end)].copy()
        if df.empty:
            continue
        df["expiry"] = pd.to_datetime(df["expiry"], errors="coerce").dt.date
        if start is not None:
            df = df[df["expiry"] >= start]
        if end is not None:
            df = df[df["expiry"] <= end]
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _finite_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return numeric


def _pct(numer: int, denom: int) -> float:
    if denom <= 0:
        return 0.0
    return round(100.0 * numer / denom, 2)


def build_report(
    *,
    symbols: list[str],
    start: date | None,
    end: date | None,
    option_cache_dir: Path,
    contracts_path: Path,
    underlyings_path: Path,
    min_index_strikes: int,
    min_stock_strikes: int,
) -> tuple[pd.DataFrame, dict]:
    underlyings = _read_underlying_catalog(underlyings_path)
    contracts = _read_contract_catalog(contracts_path, symbols, start, end)
    cache = _read_option_cache(option_cache_dir, symbols, start, end)

    keys: set[tuple[str, date]] = set()
    if not contracts.empty:
        keys.update(
            (str(row.underlying), row.expiry)
            for row in contracts[["underlying", "expiry"]].itertuples(index=False)
            if pd.notna(row.expiry)
        )
    if not cache.empty:
        keys.update(
            (str(row.underlying), row.expiry)
            for row in cache[["underlying", "expiry"]].itertuples(index=False)
            if pd.notna(row.expiry)
        )

    rows: list[CoverageRow] = []
    for symbol, expiry in sorted(keys, key=lambda item: (item[0], item[1])):
        kind = _kind_for(symbol, underlyings)
        required = min_index_strikes if kind == "INDEX" else min_stock_strikes
        cgroup = (
            contracts[(contracts["underlying"] == symbol) & (contracts["expiry"] == expiry)]
            if not contracts.empty
            else pd.DataFrame()
        )
        kgroup = (
            cache[(cache["underlying"] == symbol) & (cache["expiry"] == expiry)]
            if not cache.empty
            else pd.DataFrame()
        )

        catalog_contracts = int(cgroup["instrument_key"].nunique()) if not cgroup.empty else 0
        catalog_strikes = int(cgroup["strike"].nunique()) if not cgroup.empty else 0
        if not cgroup.empty and "candle_count" in cgroup.columns:
            with_candles = int((pd.to_numeric(cgroup["candle_count"], errors="coerce").fillna(0) > 0).sum())
            candle_rows = int(pd.to_numeric(cgroup["candle_count"], errors="coerce").fillna(0).sum())
        else:
            with_candles = 0
            candle_rows = 0

        if not kgroup.empty:
            per_time = kgroup.groupby("time")["strike"].nunique()
            cache_rows = int(len(kgroup))
            cache_timestamps = int(per_time.size)
            cache_unique_strikes = int(kgroup["strike"].nunique())
            min_strikes = _finite_float(per_time.min())
            median_strikes = _finite_float(per_time.median())
            p75_strikes = _finite_float(per_time.quantile(0.75))
            max_strikes = _finite_float(per_time.max())
        else:
            cache_rows = 0
            cache_timestamps = 0
            cache_unique_strikes = 0
            min_strikes = median_strikes = p75_strikes = max_strikes = None

        coverage_pct = _pct(with_candles, catalog_contracts)
        wall_ready = bool(
            cache_timestamps > 0
            and median_strikes is not None
            and max_strikes is not None
            and median_strikes >= required
            and max_strikes >= required
        )
        if wall_ready:
            reason = "ready"
        elif catalog_contracts <= 0:
            reason = "no contract catalog"
        elif cache_rows <= 0:
            reason = "no option candles"
        else:
            reason = (
                f"sparse chain: median_strikes_per_time={median_strikes}, "
                f"required={required}"
            )

        rows.append(
            CoverageRow(
                underlying=symbol,
                kind=kind,
                expiry=expiry.isoformat(),
                catalog_contracts=catalog_contracts,
                catalog_strikes=catalog_strikes,
                catalog_contracts_with_candles=with_candles,
                catalog_candle_rows=candle_rows,
                cache_rows=cache_rows,
                cache_timestamps=cache_timestamps,
                cache_unique_strikes=cache_unique_strikes,
                min_strikes_per_time=min_strikes,
                median_strikes_per_time=median_strikes,
                p75_strikes_per_time=p75_strikes,
                max_strikes_per_time=max_strikes,
                required_strikes_per_time=required,
                contract_candle_coverage_pct=coverage_pct,
                wall_ready=wall_ready,
                reason=reason,
            )
        )

    report = pd.DataFrame([asdict(row) for row in rows])
    if report.empty:
        summary = {
            "symbols": symbols,
            "ready_expiry_buckets": 0,
            "failed_expiry_buckets": 0,
            "message": "No matching local option history found.",
        }
        return report, summary

    ready = int(report["wall_ready"].sum())
    failed = int(len(report) - ready)
    by_symbol = (
        report.groupby("underlying")
        .agg(
            expiry_buckets=("expiry", "count"),
            ready_expiry_buckets=("wall_ready", "sum"),
            max_strikes_per_time=("max_strikes_per_time", "max"),
            median_strikes_per_time=("median_strikes_per_time", "median"),
            catalog_contracts=("catalog_contracts", "sum"),
            catalog_contracts_with_candles=("catalog_contracts_with_candles", "sum"),
        )
        .reset_index()
    )
    summary = {
        "symbols": symbols,
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "ready_expiry_buckets": ready,
        "failed_expiry_buckets": failed,
        "min_index_strikes": min_index_strikes,
        "min_stock_strikes": min_stock_strikes,
        "by_symbol": by_symbol.to_dict(orient="records"),
    }
    return report, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", help="Comma-separated underlyings. Defaults to the 10-instrument pilot universe.")
    parser.add_argument("--from-date", help="Optional candle/expiry start date, YYYY-MM-DD.")
    parser.add_argument("--to-date", help="Optional candle/expiry end date, YYYY-MM-DD.")
    parser.add_argument(
        "--option-cache-dir",
        type=Path,
        default=REPO_ROOT / "data" / "option_candles",
        help="Directory containing option candle parquet files.",
    )
    parser.add_argument(
        "--contracts-path",
        type=Path,
        default=REPO_ROOT / "data" / "catalogs" / "contracts.parquet",
        help="Contract catalog parquet path.",
    )
    parser.add_argument(
        "--underlyings-path",
        type=Path,
        default=REPO_ROOT / "data" / "catalogs" / "underlyings.parquet",
        help="Underlying catalog parquet path.",
    )
    parser.add_argument("--min-index-strikes", type=int, default=30)
    parser.add_argument("--min-stock-strikes", type=int, default=10)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--fail-on-sparse",
        action="store_true",
        help="Exit non-zero when any audited expiry bucket is not wall-ready.",
    )
    args = parser.parse_args()

    symbols = _parse_symbols(args.symbols)
    start = _parse_date(args.from_date)
    end = _parse_date(args.to_date)
    report, summary = build_report(
        symbols=symbols,
        start=start,
        end=end,
        option_cache_dir=args.option_cache_dir,
        contracts_path=args.contracts_path,
        underlyings_path=args.underlyings_path,
        min_index_strikes=args.min_index_strikes,
        min_stock_strikes=args.min_stock_strikes,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.out_dir / "coverage.csv"
    summary_path = args.out_dir / "summary.json"
    report.to_csv(report_path, index=False)
    summary_path.write_text(json.dumps(summary, indent=2, default=str))

    print(json.dumps({**summary, "coverage_csv": str(report_path)}, indent=2, default=str))
    if args.fail_on_sparse and int(summary.get("failed_expiry_buckets") or 0) > 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
