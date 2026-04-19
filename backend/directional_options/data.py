"""Runtime dataset access for the directional options engine."""
from __future__ import annotations

import gzip
import json
from collections import Counter
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Optional

import pandas as pd

from directional_options.schemas import ContractMeta


def _parse_ts(value: str | datetime | pd.Timestamp) -> pd.Timestamp:
    if isinstance(value, pd.Timestamp):
        return value
    return pd.Timestamp(value)


@lru_cache(maxsize=1)
def _load_contract_index(index_path: str) -> tuple[ContractMeta, ...]:
    raw = json.loads(Path(index_path).read_text())
    rows: list[ContractMeta] = []
    for item in raw.values():
        file_path = item.get("file_path")
        candle_count = int(item.get("candle_count") or 0)
        if not file_path or candle_count <= 0:
            continue
        rows.append(
            ContractMeta(
                underlying=str(item.get("underlying") or ""),
                expiry=str(item.get("expiry") or ""),
                expiry_kind=str(item.get("expiry_kind") or "weekly"),
                option_type=str(item.get("option_type") or ""),
                strike=float(item.get("strike") or 0.0),
                trading_symbol=str(item.get("trading_symbol") or ""),
                lot_size=int(item.get("lot_size") or 1),
                tick_size=float(item.get("tick_size") or 0.05),
                file_path=str(file_path),
                earliest_candle=str(item.get("earliest_candle") or ""),
                latest_candle=str(item.get("latest_candle") or ""),
                candle_count=candle_count,
            )
        )
    return tuple(rows)


@lru_cache(maxsize=64)
def _load_gzip_csv(path_str: str) -> pd.DataFrame:
    path = Path(path_str)
    if not path.exists():
        raise FileNotFoundError(path)
    with gzip.open(path, "rt") as handle:
        frame = pd.read_csv(handle, parse_dates=["time"])
    frame = frame.sort_values("time").reset_index(drop=True)
    for column in ("open", "high", "low", "close", "volume", "oi"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
    if "time" in frame.columns:
        frame["time"] = pd.to_datetime(frame["time"], errors="coerce")
        frame = frame.dropna(subset=["time"]).reset_index(drop=True)
    return frame


class DirectionalOptionsDataStore:
    """Cached runtime data access layered over the persisted analytics dataset."""

    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self.contract_index_path = self.data_root / "contract_index.json"

    def available_underlyings(self) -> list[str]:
        values = {
            item.underlying
            for item in _load_contract_index(str(self.contract_index_path))
            if item.underlying
        }
        for path in (self.data_root / "spot").glob("underlying=*"):
            values.add(path.name.split("=", 1)[-1])
        return sorted(values)

    def load_spot_frame(self, underlying: str) -> pd.DataFrame:
        path = self.data_root / "spot" / f"underlying={underlying}" / "1minute.csv.gz"
        return _load_gzip_csv(str(path))

    def load_option_frame(self, file_path: str) -> pd.DataFrame:
        return _load_gzip_csv(str(self.data_root / file_path))

    def list_contracts(
        self,
        *,
        underlying: str,
        option_type: Optional[str] = None,
        max_days_to_expiry: Optional[float] = None,
        as_of: str | datetime | pd.Timestamp | None = None,
    ) -> list[ContractMeta]:
        as_of_ts = _parse_ts(as_of) if as_of is not None else None
        as_of_date = as_of_ts.date() if as_of_ts is not None else None
        rows: list[ContractMeta] = []
        for meta in _load_contract_index(str(self.contract_index_path)):
            if meta.underlying != underlying:
                continue
            if option_type and meta.option_type != option_type:
                continue
            if as_of_ts is not None:
                latest = _parse_ts(meta.latest_candle)
                if latest.date() < as_of_date:
                    continue
                expiry_ts = pd.Timestamp(meta.expiry)
                dte = (expiry_ts.date() - as_of_date).days
                if dte < 0:
                    continue
                if max_days_to_expiry is not None and dte > max_days_to_expiry:
                    continue
            rows.append(meta)
        return rows

    def latest_spot_timestamp(self, underlying: str) -> Optional[pd.Timestamp]:
        frame = self.load_spot_frame(underlying)
        if frame.empty:
            return None
        return pd.Timestamp(frame["time"].iloc[-1])

    def latest_common_timestamp(self, underlying: str) -> Optional[pd.Timestamp]:
        contracts = self.list_contracts(underlying=underlying)
        if not contracts:
            return self.latest_spot_timestamp(underlying)
        latest_contract = max(_parse_ts(meta.latest_candle) for meta in contracts)
        spot_latest = self.latest_spot_timestamp(underlying)
        if spot_latest is None:
            return latest_contract
        return min(latest_contract, spot_latest)

    def latest_tradeable_timestamp(
        self,
        underlying: str,
        *,
        min_contracts: int = 12,
    ) -> Optional[pd.Timestamp]:
        contracts = self.list_contracts(underlying=underlying)
        if not contracts:
            return self.latest_common_timestamp(underlying)
        counts = Counter(meta.latest_candle[:10] for meta in contracts if meta.latest_candle)
        eligible_dates = sorted(date for date, count in counts.items() if count >= min_contracts)
        if not eligible_dates:
            return self.latest_common_timestamp(underlying)
        target_date = eligible_dates[-1]
        spot = self.load_spot_frame(underlying)
        rows = spot.loc[spot["time"].dt.strftime("%Y-%m-%d") == target_date]
        if rows.empty:
            return self.latest_common_timestamp(underlying)
        return pd.Timestamp(rows.iloc[-1]["time"])

    def latest_spot_price(self, underlying: str, ts: str | datetime | pd.Timestamp) -> Optional[float]:
        frame = self.load_spot_frame(underlying)
        if frame.empty:
            return None
        ts_value = _parse_ts(ts)
        rows = frame.loc[frame["time"] <= ts_value]
        if rows.empty:
            rows = frame.loc[frame["time"] >= ts_value]
        if rows.empty:
            return None
        return float(rows.iloc[-1]["close"])

    def latest_contract_bar(
        self,
        meta: ContractMeta,
        ts: str | datetime | pd.Timestamp,
    ) -> Optional[pd.Series]:
        frame = self.load_option_frame(meta.file_path)
        ts_value = _parse_ts(ts)
        rows = frame.loc[frame["time"] <= ts_value]
        if rows.empty:
            return None
        row = rows.iloc[-1]
        if pd.Timestamp(row["time"]).date() != ts_value.date():
            return None
        return row

    def coverage_summary(self, underlying: str) -> dict[str, object]:
        spot = self.load_spot_frame(underlying)
        contracts = self.list_contracts(underlying=underlying)
        weekly = [meta for meta in contracts if meta.expiry_kind == "weekly"]
        monthly = [meta for meta in contracts if meta.expiry_kind == "monthly"]
        option_rows = int(sum(meta.candle_count for meta in contracts))
        first_option = min((_parse_ts(meta.earliest_candle) for meta in contracts), default=None)
        last_option = max((_parse_ts(meta.latest_candle) for meta in contracts), default=None)
        return {
            "underlying": underlying,
            "spot_rows": int(len(spot.index)),
            "spot_start": spot["time"].iloc[0].isoformat() if not spot.empty else None,
            "spot_end": spot["time"].iloc[-1].isoformat() if not spot.empty else None,
            "contracts": len(contracts),
            "weekly_contracts": len(weekly),
            "monthly_contracts": len(monthly),
            "option_rows": option_rows,
            "option_start": first_option.isoformat() if first_option is not None else None,
            "option_end": last_option.isoformat() if last_option is not None else None,
        }
