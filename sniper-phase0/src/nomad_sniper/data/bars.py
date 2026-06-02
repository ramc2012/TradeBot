"""Minute-bar OHLCV loader for the underlying futures.

Input: parquet files in `data/raw/`, one per expired contract, with the convention
    `upstox_<underlying>_fut_<YYYYMMDD>.parquet`
e.g. `upstox_nifty_fut_20250227.parquet` for the Feb 2025 expiry.

The loader also accepts the backend futures cache exported by
`backend/scripts/backfill_index_futures_1minute.py`:
    `backend/runtime/index_analytics_data/futures/underlying=NIFTY/1minute.csv.gz`

Required columns: timestamp (UTC or IST, we normalize), open, high, low, close, volume, oi.
The loader concatenates them, deduplicates on timestamp, and returns a single tz-aware IST
DataFrame indexed by timestamp.

For Phase 0 we accept ~minute granularity. Tick data is Phase 1.
"""

from __future__ import annotations

from datetime import date, datetime
import os
from pathlib import Path

import pandas as pd

from nomad_sniper.utils.logging import get_logger
from nomad_sniper.utils.settings import settings
from nomad_sniper.utils.timeutil import IST

log = get_logger()

UNDERLYINGS = ("nifty", "banknifty", "finnifty", "sensex")


def load_minute_bars(
    underlying: str,
    *,
    start: date | None = None,
    end: date | None = None,
    raw_dir: Path | None = None,
    futures_dir: Path | None = None,
) -> pd.DataFrame:
    """Load minute bars for a given underlying, optionally sliced to [start, end].

    Returns:
        DataFrame indexed by IST-aware timestamp, columns
        [open, high, low, close, volume, oi, contract_expiry].
    """
    underlying = underlying.lower()
    if underlying not in UNDERLYINGS:
        raise ValueError(f"underlying must be one of {UNDERLYINGS}, got {underlying!r}")

    raw_dir = raw_dir or settings.raw_dir
    cache = _load_backend_futures_cache(underlying, futures_dir=futures_dir)
    if cache is not None:
        bars = cache
        if start is not None:
            bars = bars[bars.index.date >= start]
        if end is not None:
            bars = bars[bars.index.date <= end]
        log.info(
            f"Loaded {len(bars):,} cached futures bars for {underlying} "
            f"({bars.index.min()} -> {bars.index.max()})"
        )
        return bars

    pattern = f"upstox_{underlying}_fut_*.parquet"
    files = sorted(raw_dir.glob(pattern))
    if not files:
        raise FileNotFoundError(
            f"No bar files matching {pattern!r} in {raw_dir}. "
            f"Expected naming: upstox_<underlying>_fut_<YYYYMMDD>.parquet"
        )

    frames = []
    for f in files:
        expiry_str = f.stem.split("_")[-1]  # YYYYMMDD
        try:
            expiry = datetime.strptime(expiry_str, "%Y%m%d").date()
        except ValueError:
            log.warning(f"Cannot parse expiry from filename {f.name}, skipping")
            continue
        df = pd.read_parquet(f)
        df = _normalize_bars(df)
        df["contract_expiry"] = expiry
        frames.append(df)

    if not frames:
        raise RuntimeError("No bar files could be loaded.")

    bars = pd.concat(frames).sort_index()
    # Deduplicate on (timestamp, contract_expiry) — overlapping front/back contracts are OK
    bars = bars[~bars.index.duplicated(keep="first")]

    if start is not None:
        bars = bars[bars.index.date >= start]
    if end is not None:
        bars = bars[bars.index.date <= end]

    log.info(
        f"Loaded {len(bars):,} bars for {underlying} "
        f"({bars.index.min()} -> {bars.index.max()}) from {len(frames)} contracts"
    )
    return bars


def _load_backend_futures_cache(
    underlying: str,
    *,
    futures_dir: Path | None = None,
) -> pd.DataFrame | None:
    """Load the backend 1-minute futures CSV cache when available."""
    root = futures_dir or _default_futures_dir()
    if root is None:
        return None
    path = root / f"underlying={underlying.upper()}" / "1minute.csv.gz"
    if not path.exists():
        return None

    df = pd.read_csv(path)
    bars = _normalize_bars(df)
    if "expiry" in df.columns:
        expiry = pd.to_datetime(df["expiry"], errors="coerce").dt.date
        bars["contract_expiry"] = expiry.to_numpy()
    else:
        bars["contract_expiry"] = pd.NA
    if "instrument_key" in df.columns:
        bars["instrument_key"] = df["instrument_key"].to_numpy()
    if "trading_symbol" in df.columns:
        bars["trading_symbol"] = df["trading_symbol"].to_numpy()
    return bars


def _default_futures_dir() -> Path | None:
    env = os.environ.get("SNIPER_FUTURES_DATA_DIR")
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates.extend(
        [
            settings.data_dir / "futures",
            settings.raw_dir / "futures",
            Path("../backend/runtime/index_analytics_data/futures"),
            Path("backend/runtime/index_analytics_data/futures"),
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _normalize_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Force OHLCV column names, ensure IST tz-aware index."""
    df = df.copy()
    df.columns = [c.strip().lower() for c in df.columns]

    # Find the timestamp column
    ts_col = None
    for candidate in ("timestamp", "datetime", "date", "time"):
        if candidate in df.columns:
            ts_col = candidate
            break
    if ts_col is None and not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError(f"No timestamp column found. Got: {list(df.columns)}")

    if ts_col is not None:
        df[ts_col] = pd.to_datetime(df[ts_col])
        df = df.set_index(ts_col)

    # Localize / convert to IST
    if df.index.tz is None:
        # Upstox returns IST naively; assume IST
        df.index = df.index.tz_localize(IST, ambiguous="raise", nonexistent="raise")
    else:
        df.index = df.index.tz_convert(IST)

    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Bars missing required columns: {sorted(missing)}")

    if "oi" not in df.columns:
        df["oi"] = pd.NA

    return df[["open", "high", "low", "close", "volume", "oi"]]


def get_active_contract_bars(
    underlying: str,
    on_date: date,
    *,
    raw_dir: Path | None = None,
) -> pd.DataFrame:
    """Return the bars for the front-month (active) contract on a given date.

    "Active" = the nearest expiry on or after `on_date`. Phase 0 uses front-month only;
    roll behaviour and back-month logic are Phase 1.
    """
    bars = load_minute_bars(underlying, raw_dir=raw_dir)
    # Pick the contract whose expiry is the nearest future expiry
    contracts = bars["contract_expiry"].unique()
    eligible = sorted([c for c in contracts if c >= on_date])
    if not eligible:
        raise ValueError(f"No active contract for {underlying} on {on_date}")
    front = eligible[0]
    return bars[bars["contract_expiry"] == front]
