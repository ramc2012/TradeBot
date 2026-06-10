"""Minute-bar OHLCV loader for the underlying (NIFTY / BANKNIFTY / FINNIFTY futures).

Input: parquet files in `data/raw/`, one per expired contract, with the convention
    `upstox_<underlying>_fut_<YYYYMMDD>.parquet`
e.g. `upstox_nifty_fut_20250227.parquet` for the Feb 2025 expiry.

Required columns: timestamp (UTC or IST, we normalize), open, high, low, close, volume, oi.
The loader concatenates them, deduplicates on timestamp, and returns a single tz-aware IST
DataFrame indexed by timestamp.

For Phase 0 we accept ~minute granularity. Tick data is Phase 1.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd

from nomad_sniper.utils.logging import get_logger
from nomad_sniper.utils.settings import settings
from nomad_sniper.utils.timeutil import IST

log = get_logger()

# Default index set; the pipeline now DISCOVERS all instruments present in data/raw so the
# model can train on a broad multi-instrument pool (spec §25/§31), not just these three.
UNDERLYINGS = ("nifty", "banknifty", "finnifty")


def discover_underlyings(raw_dir: Path | None = None) -> list[str]:
    """Every instrument with an underlying file in data/raw (upstox_<u>_fut_<YYYYMMDD>.parquet)."""
    import re
    raw_dir = raw_dir or settings.raw_dir
    pat = re.compile(r"^upstox_(?P<u>[a-z0-9]+)_fut_\d{8}$", re.IGNORECASE)
    out: set[str] = set()
    for f in raw_dir.glob("upstox_*_fut_*.parquet"):
        m = pat.match(f.stem)
        if m:
            out.add(m.group("u").lower())
    return sorted(out)


def load_spot_bars(underlying: str, *, raw_dir: Path | None = None) -> pd.DataFrame | None:
    """Optional INDEX SPOT bars (``spot_<underlying>.parquet``), used as the *option family's*
    underlying reference. Options are priced off index spot; the futures bars carry a basis
    premium, so the C/D features and ATM strike selection should reference spot, not futures.
    Returns None if no spot file exists (caller falls back to the futures bars)."""
    raw_dir = raw_dir or settings.raw_dir
    fp = raw_dir / f"spot_{underlying.lower()}.parquet"
    if not fp.exists():
        return None
    return _normalize_bars(pd.read_parquet(fp))


def load_minute_bars(
    underlying: str,
    *,
    start: date | None = None,
    end: date | None = None,
    raw_dir: Path | None = None,
) -> pd.DataFrame:
    """Load minute bars for a given underlying, optionally sliced to [start, end].

    Any instrument with a matching ``upstox_<underlying>_fut_<YYYYMMDD>.parquet`` is accepted
    (no hardcoded whitelist) so the pipeline can pool a broad instrument set.

    Returns:
        DataFrame indexed by IST-aware timestamp, columns
        [open, high, low, close, volume, oi, contract_expiry].
    """
    underlying = underlying.lower()

    raw_dir = raw_dir or settings.raw_dir
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
    # Provider candles are START-stamped → shift to bar-close so `index <= t` = 'closed by t'
    # (prevents the current interval leaking into the decision row; see close_stamp docstring).
    bars = close_stamp(bars)

    if start is not None:
        bars = bars[bars.index.date >= start]
    if end is not None:
        bars = bars[bars.index.date <= end]

    log.info(
        f"Loaded {len(bars):,} bars for {underlying} "
        f"({bars.index.min()} → {bars.index.max()}) from {len(frames)} contracts"
    )
    return bars


def close_stamp(df: pd.DataFrame) -> pd.DataFrame:
    """Shift a START-stamped OHLC index to BAR-CLOSE time so `index <= t` means 'closed by t'.

    Provider candles (Upstox/Alpaca) are start-stamped: a bar labelled `t` covers [t, t+interval)
    and its close/high/low are only known at t+interval. Feature builders filter `index <= t`, so
    without this shift the bar AT t leaks the current interval into the decision row (the leakage
    the contract in features/base.py forbids). Interval inferred from the smallest frequent
    intra-day gap (overnight gaps ignored).
    """
    if len(df) < 3:
        return df
    diffs = df.index.to_series().diff().dt.total_seconds()
    pos = diffs[(diffs > 0) & (diffs <= 7200)]  # ≤ 2h → within-session bar gaps only
    if pos.empty:
        return df
    interval = float(pos.median())
    out = df.copy()
    out.index = out.index + pd.Timedelta(seconds=interval)
    return out


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
