"""ATM option minute-bar loader.

Option data is optional in Phase 0. When present, files are expected in `data/raw/` using:

    upstox_<underlying>_<expiryYYYYMMDD>_<strike>_<CE|PE>.parquet

The loader normalizes timestamps to IST, deduplicates, and exposes a resolved ATM CE/PE/straddle
series for a session. Missing option data is reported clearly so the feature pipeline can degrade
to null option features without pretending bar-derived proxies are real option structure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Literal

import pandas as pd

from nomad_sniper.utils.settings import settings
from nomad_sniper.utils.timeutil import IST

OptionType = Literal["CE", "PE"]


@dataclass(frozen=True)
class ATMOptionSeries:
    underlying: str
    session_date: date
    expiry: date
    strike: float
    ce: pd.DataFrame
    pe: pd.DataFrame
    straddle: pd.DataFrame


def discover_option_contracts(
    underlying: str,
    *,
    raw_dir: Path | None = None,
) -> pd.DataFrame:
    """Return available option files as rows: expiry, strike, option_type, path."""
    raw_dir = raw_dir or settings.raw_dir
    rows = []
    for path in sorted(raw_dir.glob(f"upstox_{underlying.lower()}_*_*_*.parquet")):
        parts = path.stem.split("_")
        if len(parts) < 5:
            continue
        opt_type = parts[-1].upper()
        if opt_type not in {"CE", "PE"}:
            continue
        try:
            expiry = datetime.strptime(parts[-3], "%Y%m%d").date()
            strike = float(parts[-2])
        except ValueError:
            continue
        rows.append({"expiry": expiry, "strike": strike, "option_type": opt_type, "path": path})
    return pd.DataFrame(rows)


def load_option_bars(
    underlying: str,
    expiry: date,
    strike: float,
    option_type: OptionType,
    *,
    raw_dir: Path | None = None,
) -> pd.DataFrame:
    """Load one option bar series, normalized to IST and canonical OHLCV/OI columns."""
    raw_dir = raw_dir or settings.raw_dir
    strike_token = _strike_token(strike)
    path = raw_dir / (
        f"upstox_{underlying.lower()}_{expiry:%Y%m%d}_{strike_token}_{option_type}.parquet"
    )
    if not path.exists():
        raise FileNotFoundError(
            f"Missing option bars: {path.name}. Expected "
            "upstox_<underlying>_<expiryYYYYMMDD>_<strike>_<CE|PE>.parquet"
        )
    return _normalize_option_bars(pd.read_parquet(path))


def resolve_atm_series(
    underlying: str,
    session_date: date,
    bars_underlying: pd.DataFrame,
    *,
    raw_dir: Path | None = None,
    reference_time: time = time(9, 20),
    expiry: date | None = None,
) -> ATMOptionSeries:
    """Resolve the ATM CE/PE series nearest spot at the session reference time."""
    raw_dir = raw_dir or settings.raw_dir
    contracts = discover_option_contracts(underlying, raw_dir=raw_dir)
    if contracts.empty:
        raise FileNotFoundError(f"No option bars found for {underlying} in {raw_dir}")

    ref_dt = IST.localize(datetime.combine(session_date, reference_time))
    day_bars = bars_underlying[
        (bars_underlying.index.date == session_date) & (bars_underlying.index <= ref_dt)
    ]
    if day_bars.empty:
        raise ValueError(f"No underlying bars for {underlying} at or before {ref_dt}")
    spot = float(day_bars["close"].iloc[-1])

    expiries = sorted(set(contracts["expiry"]))
    if expiry is None:
        future = [e for e in expiries if e >= session_date]
        if not future:
            raise FileNotFoundError(f"No option expiry on/after {session_date} for {underlying}")
        expiry = future[0]
    expiry_contracts = contracts[contracts["expiry"] == expiry]
    strikes_with_both = _strikes_with_both_sides(expiry_contracts)
    if not strikes_with_both:
        raise FileNotFoundError(f"No CE/PE pair found for {underlying} expiry {expiry:%Y%m%d}")

    strike = min(strikes_with_both, key=lambda s: abs(s - spot))
    ce = load_option_bars(underlying, expiry, strike, "CE", raw_dir=raw_dir)
    pe = load_option_bars(underlying, expiry, strike, "PE", raw_dir=raw_dir)
    straddle = _build_straddle(ce, pe)
    return ATMOptionSeries(
        underlying=underlying.lower(),
        session_date=session_date,
        expiry=expiry,
        strike=strike,
        ce=ce,
        pe=pe,
        straddle=straddle,
    )


def _normalize_option_bars(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [c.strip().lower() for c in df.columns]
    ts_col = None
    for candidate in ("timestamp", "datetime", "date", "time", "ts"):
        if candidate in df.columns:
            ts_col = candidate
            break
    if ts_col is not None:
        df[ts_col] = pd.to_datetime(df[ts_col])
        df = df.set_index(ts_col)
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("Option bars require a DatetimeIndex or timestamp column")
    if df.index.tz is None:
        df.index = df.index.tz_localize(IST, ambiguous="raise", nonexistent="raise")
    else:
        df.index = df.index.tz_convert(IST)

    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Option bars missing required columns: {sorted(missing)}")
    if "oi" not in df.columns:
        df["oi"] = pd.NA
    if "iv" not in df.columns:
        df["iv"] = pd.NA
    cols = ["open", "high", "low", "close", "volume", "oi", "iv"]
    return df[cols].sort_index()[~df.index.duplicated(keep="last")]


def _build_straddle(ce: pd.DataFrame, pe: pd.DataFrame) -> pd.DataFrame:
    joined = ce.add_prefix("ce_").join(pe.add_prefix("pe_"), how="inner")
    out = pd.DataFrame(index=joined.index)
    for col in ("open", "high", "low", "close"):
        out[col] = joined[f"ce_{col}"].astype(float) + joined[f"pe_{col}"].astype(float)
    out["volume"] = joined["ce_volume"].astype(float) + joined["pe_volume"].astype(float)
    out["oi"] = joined["ce_oi"].astype(float) + joined["pe_oi"].astype(float)
    out["iv"] = joined[["ce_iv", "pe_iv"]].astype(float).mean(axis=1)
    return out


def _strikes_with_both_sides(contracts: pd.DataFrame) -> list[float]:
    sides = contracts.groupby("strike")["option_type"].agg(lambda s: set(s))
    return [float(strike) for strike, opts in sides.items() if {"CE", "PE"} <= opts]


def _strike_token(strike: float) -> str:
    return str(int(strike)) if float(strike).is_integer() else str(strike).rstrip("0").rstrip(".")
