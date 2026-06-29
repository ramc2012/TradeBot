"""Dataset access for MACD Refined.

Reads the canonical research dataset at the repo root `data/`:
  - option_candles/expiry_*.parquet  — 30-min premium OHLC + volume + OI + IV + greeks
  - spot_candles/spot_*.parquet      — 30-min underlying spot
  - catalogs/{underlyings,contracts}.parquet — symbols + lot/tick sizes

It also resolves the current + next monthly expiry (for next-month positioning)
and builds a per-underlying ATM-IV history used by the IV-rank gate.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Optional

import pandas as pd

from analysis.instruments import (
    ALL_FO_INDICES,
    get_index_monthly_expiry,
    get_monthly_expiries,
)

_EXPIRY_FILE_RE = re.compile(r"expiry_(\d{4}-\d{2}-\d{2})\.parquet$")


@lru_cache(maxsize=4)
def _load_expiry_full(path_str: str) -> pd.DataFrame:
    """Full per-expiry option-candle frame (cached; bounded to 4 files)."""
    path = Path(path_str)
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_parquet(path)
    frame["time"] = pd.to_datetime(frame["time"], errors="coerce")
    frame = frame.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    return frame


@lru_cache(maxsize=32)
def _load_expiry_iv_cols(path_str: str) -> pd.DataFrame:
    """Light loader (underlying, time, iv, delta) for ATM-IV history."""
    path = Path(path_str)
    if not path.exists():
        return pd.DataFrame()
    cols = ["underlying", "time", "iv", "delta"]
    frame = pd.read_parquet(path, columns=cols)
    frame["time"] = pd.to_datetime(frame["time"], errors="coerce")
    return frame.dropna(subset=["time"])


@lru_cache(maxsize=32)
def _load_spot(path_str: str, underlying: str) -> pd.DataFrame:
    path = Path(path_str)
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_parquet(path)
    frame = frame[frame["underlying"] == underlying].copy()
    frame["time"] = pd.to_datetime(frame["time"], errors="coerce")
    return frame.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)


class MacdRefinedDataStore:
    """Cached access to the repo-root `data/` research dataset."""

    def __init__(self, data_root: Path | str):
        self.data_root = Path(data_root)
        self.option_dir = self.data_root / "option_candles"
        self.spot_dir = self.data_root / "spot_candles"
        self.catalog_dir = self.data_root / "catalogs"
        # Per-instance caches (NOT @lru_cache on methods — that pins instances
        # and keys on self). Module-level loaders below still cache by path.
        self._contracts_df: pd.DataFrame | None = None
        self._underlyings_df: pd.DataFrame | None = None
        self._spot_index: dict[str, str] | None = None
        self._lot_cache: dict[str, int] = {}
        self._iv_cache: dict[str, pd.Series] = {}

    # ── Expiry files ──────────────────────────────────────────────────────
    def list_expiry_files(self) -> list[tuple[date, Path]]:
        out: list[tuple[date, Path]] = []
        if not self.option_dir.exists():
            return out
        for path in self.option_dir.glob("expiry_*.parquet"):
            m = _EXPIRY_FILE_RE.search(path.name)
            if not m:
                continue
            try:
                out.append((date.fromisoformat(m.group(1)), path))
            except ValueError:
                continue
        return sorted(out, key=lambda item: item[0])

    def recent_expiry_files(self, count: int | None = None) -> list[tuple[date, Path]]:
        files = self.list_expiry_files()
        if count is None or count <= 0:
            return files
        return files[-count:]

    def load_expiry_frame(self, path: Path | str) -> pd.DataFrame:
        return _load_expiry_full(str(path))

    # ── Catalog ───────────────────────────────────────────────────────────
    def _contracts(self) -> pd.DataFrame:
        if self._contracts_df is None:
            path = self.catalog_dir / "contracts.parquet"
            self._contracts_df = pd.read_parquet(path) if path.exists() else pd.DataFrame()
        return self._contracts_df

    def _underlyings(self) -> pd.DataFrame:
        if self._underlyings_df is None:
            path = self.catalog_dir / "underlyings.parquet"
            self._underlyings_df = pd.read_parquet(path) if path.exists() else pd.DataFrame()
        return self._underlyings_df

    def available_underlyings(self) -> list[str]:
        cat = self._underlyings()
        if not cat.empty and "symbol" in cat.columns:
            return sorted(str(s) for s in cat["symbol"].dropna().unique())
        # Fallback: scan one expiry file.
        files = self.list_expiry_files()
        if not files:
            return []
        frame = self.load_expiry_frame(files[-1][1])
        return sorted(str(s) for s in frame["underlying"].dropna().unique())

    def lot_size_for(self, underlying: str) -> int:
        if underlying in self._lot_cache:
            return self._lot_cache[underlying]
        cat = self._contracts()
        lot = 1
        if not cat.empty:
            rows = cat[cat["underlying"] == underlying]
            if not rows.empty and "lot_size" in rows.columns:
                try:
                    lot = int(rows["lot_size"].mode().iloc[0])
                except Exception:
                    lot = int(rows["lot_size"].iloc[0])
        self._lot_cache[underlying] = lot
        return lot

    def contract_meta(
        self, underlying: str, expiry: str, strike: float, option_type: str
    ) -> dict:
        cat = self._contracts()
        if cat.empty:
            return {}
        rows = cat[
            (cat["underlying"] == underlying)
            & (cat["expiry"].astype(str).str[:10] == str(expiry)[:10])
            & (cat["strike"].astype(float) == float(strike))
            & (cat["option_type"] == option_type)
        ]
        if rows.empty:
            return {"lot_size": self.lot_size_for(underlying)}
        row = rows.iloc[0]
        return {
            "instrument_key": str(row.get("instrument_key") or ""),
            "trading_symbol": str(row.get("trading_symbol") or ""),
            "lot_size": int(row.get("lot_size") or self.lot_size_for(underlying)),
            "tick_size": float(row.get("tick_size") or 0.05),
        }

    # ── Spot ──────────────────────────────────────────────────────────────
    def _spot_file_index(self) -> dict[str, str]:
        if self._spot_index is not None:
            return self._spot_index
        index: dict[str, str] = {}
        if self.spot_dir.exists():
            for path in self.spot_dir.glob("spot_*.parquet"):
                try:
                    syms = pd.read_parquet(path, columns=["underlying"])["underlying"].unique()
                except Exception:
                    continue
                for sym in syms:
                    index[str(sym)] = str(path)
        self._spot_index = index
        return index

    def load_spot(self, underlying: str) -> pd.DataFrame:
        path = self._spot_file_index().get(underlying)
        if not path:
            return pd.DataFrame()
        return _load_spot(path, underlying)

    # ── ATM IV history (for IV-rank) ──────────────────────────────────────
    def atm_iv_daily(self, underlying: str) -> pd.Series:
        """Per-session ATM IV for an underlying, blended across all expiries.

        ATM is proxied by 0.40 ≤ |delta| ≤ 0.60 (the greeks ship in the
        dataset, so no strike-step lookup is needed). Returns a Series indexed
        by session date — the rolling distribution the IV-rank gate ranks
        against.
        """
        if underlying in self._iv_cache:
            return self._iv_cache[underlying]
        frames: list[pd.Series] = []
        for _exp, path in self.list_expiry_files():
            light = _load_expiry_iv_cols(str(path))
            if light.empty:
                continue
            sub = light[light["underlying"] == underlying]
            if sub.empty:
                continue
            atm = sub[(sub["delta"].abs() >= 0.40) & (sub["delta"].abs() <= 0.60)]
            if atm.empty:
                atm = sub
            grouped = atm.assign(_d=atm["time"].dt.date).groupby("_d")["iv"].median()
            frames.append(grouped)
        if not frames:
            result = pd.Series(dtype="float64")
        else:
            combined = pd.concat(frames)
            # Multiple expiries can contribute the same date — keep their median.
            result = combined.groupby(level=0).median().sort_index()
        self._iv_cache[underlying] = result
        return result

    # ── Expiry resolution (current + next monthly) ────────────────────────
    @staticmethod
    def resolve_monthly_expiries(symbol: str, today: date, ahead: int = 2) -> list[date]:
        """Current + next `ahead-1` monthly expiries for `symbol` from `today`.

        Uses the index-aware calendar for indices (SENSEX Thu etc.) and the
        standard NSE Tuesday calendar for stocks.
        """
        out: list[date] = []
        sym = str(symbol or "").upper().strip()
        is_index = sym in set(ALL_FO_INDICES)
        year, month = today.year, today.month
        while len(out) < max(ahead, 1):
            if is_index:
                exp = get_index_monthly_expiry(sym, year, month)
            else:
                # Stocks share the NSE Tuesday monthly calendar.
                month_expiries = get_monthly_expiries(
                    date(year, month, 1), date(year, month, 28) + timedelta(days=10)
                )
                exp = month_expiries[0] if month_expiries else None
            if exp is not None and exp >= today and exp not in out:
                out.append(exp)
            if month == 12:
                year, month = year + 1, 1
            else:
                month += 1
            if year > today.year + 2:  # safety
                break
        return out

    def dataset_date_range(self) -> tuple[Optional[date], Optional[date]]:
        files = self.list_expiry_files()
        if not files:
            return None, None
        return files[0][0], files[-1][0]
