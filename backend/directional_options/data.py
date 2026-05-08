"""Runtime dataset access for the directional options engine."""
from __future__ import annotations

import gzip
import json
import math
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from sqlalchemy import text

from analysis.instruments import get_index_monthly_expiry, get_monthly_expiry
from db.database import AsyncSessionLocal
from directional_options.schemas import ContractMeta
from market_data.market_intelligence_runtime import market_intelligence_runtime
from market_data.option_history import option_history_service


def _empty_price_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "time": pd.Series(dtype="datetime64[ns]"),
            "open": pd.Series(dtype="float64"),
            "high": pd.Series(dtype="float64"),
            "low": pd.Series(dtype="float64"),
            "close": pd.Series(dtype="float64"),
            "volume": pd.Series(dtype="float64"),
            "oi": pd.Series(dtype="float64"),
        }
    )


UTC = timezone.utc


def _parse_ts(value: str | datetime | pd.Timestamp) -> pd.Timestamp:
    if isinstance(value, pd.Timestamp):
        return value
    return pd.Timestamp(value)


def _frame_from_rows(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return _empty_price_frame()
    frame = pd.DataFrame(rows)
    if "time" not in frame.columns:
        return _empty_price_frame()
    frame["time"] = pd.to_datetime(frame["time"], errors="coerce", utc=True).dt.tz_convert(None)
    frame = frame.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    for column in ("open", "high", "low", "close", "volume", "oi"):
        if column not in frame.columns:
            frame[column] = 0.0
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
    return frame[["time", "open", "high", "low", "close", "volume", "oi"]]


@lru_cache(maxsize=1)
def _load_contract_index(index_path: str) -> tuple[ContractMeta, ...]:
    path = Path(index_path)
    if not path.exists():
        return tuple()
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError:
        return tuple()
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
        return _empty_price_frame()
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

    async def load_live_spot_frame(
        self,
        underlying: str,
        *,
        lookback_days: int = 10,
    ) -> tuple[pd.DataFrame, str, str]:
        if underlying.upper() == "CRUDEOIL":
            try:
                from market_data.commodity_runtime_history import load_commodity_history_rows

                rows, history_symbol = await load_commodity_history_rows(
                    underlying,
                    interval="1minute",
                    lookback_days=lookback_days,
                )
                frame = _frame_from_rows(rows)
                if not frame.empty:
                    return frame, "commodity_broker_history", history_symbol
            except Exception:
                pass

        rows, source, history_symbol = await market_intelligence_runtime.load_local_spot_rows(
            underlying,
            lookback_days=max(int(lookback_days), 1),
        )
        frame = _frame_from_rows(rows)
        return frame, source, history_symbol

    async def list_live_contract_snapshots(
        self,
        *,
        underlying: str,
        option_type: str,
        spot_price: float,
        as_of: str | datetime | pd.Timestamp | None = None,
        max_days_to_expiry: float = 45.0,
        limit: int = 54,
    ) -> list[dict[str, Any]]:
        as_of_ts = _parse_ts(as_of or datetime.now(UTC))
        max_expiry = as_of_ts.date() + timedelta(days=max(int(math.ceil(max_days_to_expiry)), 1))
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    WITH latest AS (
                        SELECT DISTINCT ON (expiry, strike, option_type)
                            time,
                            underlying,
                            expiry,
                            strike,
                            option_type,
                            source_broker,
                            instrument_key,
                            trading_symbol,
                            underlying_price,
                            ltp,
                            volume,
                            oi,
                            iv
                        FROM atm_option_watchlist_snapshots
                        WHERE underlying = :underlying
                          AND option_type = :option_type
                          AND expiry >= CURRENT_DATE
                          AND expiry <= :max_expiry
                          AND ltp IS NOT NULL
                          AND time <= :as_of
                        ORDER BY expiry, strike, option_type, time DESC
                    )
                    SELECT
                        latest.time,
                        latest.underlying,
                        latest.expiry,
                        latest.strike,
                        latest.option_type,
                        latest.source_broker,
                        latest.instrument_key,
                        latest.trading_symbol,
                        latest.underlying_price,
                        latest.ltp,
                        latest.volume,
                        latest.oi,
                        latest.iv,
                        COALESCE(
                            catalog_key.lot_size,
                            catalog_row.lot_size,
                            underlying_catalog.lot_size,
                            1
                        ) AS lot_size
                    FROM latest
                    LEFT JOIN fo_contract_catalog catalog_key
                      ON catalog_key.instrument_key = latest.instrument_key
                    LEFT JOIN LATERAL (
                        SELECT lot_size
                        FROM fo_contract_catalog
                        WHERE underlying = latest.underlying
                          AND expiry = latest.expiry
                          AND strike = latest.strike
                          AND option_type = latest.option_type
                        ORDER BY last_synced_at DESC NULLS LAST, updated_at DESC
                        LIMIT 1
                    ) catalog_row ON TRUE
                    LEFT JOIN fo_underlying_catalog underlying_catalog
                      ON underlying_catalog.symbol = latest.underlying
                    ORDER BY latest.expiry ASC, ABS(latest.strike - :spot_price) ASC, latest.time DESC
                    LIMIT :limit
                    """
                ),
                {
                    "underlying": underlying.upper(),
                    "option_type": option_type,
                    "spot_price": float(spot_price or 0.0),
                    "as_of": as_of_ts.to_pydatetime(),
                    "max_expiry": max_expiry,
                    "limit": int(limit),
                },
            )
            rows = result.fetchall()

        payload: list[dict[str, Any]] = []
        for row in rows:
            expiry_value = getattr(row, "expiry", None)
            if expiry_value is None:
                continue
            expiry_date = expiry_value if isinstance(expiry_value, date) else date.fromisoformat(str(expiry_value))
            payload.append(
                {
                    "time": _parse_ts(getattr(row, "time")).isoformat(),
                    "underlying": str(getattr(row, "underlying") or "").upper(),
                    "expiry": expiry_date.isoformat(),
                    "expiry_kind": self._expiry_kind(str(getattr(row, "underlying") or ""), expiry_date),
                    "strike": float(getattr(row, "strike") or 0.0),
                    "option_type": str(getattr(row, "option_type") or option_type),
                    "source_broker": str(getattr(row, "source_broker") or "local_watchlist"),
                    "instrument_key": str(getattr(row, "instrument_key") or ""),
                    "trading_symbol": str(getattr(row, "trading_symbol") or ""),
                    "underlying_price": float(getattr(row, "underlying_price") or spot_price or 0.0),
                    "ltp": float(getattr(row, "ltp") or 0.0),
                    "volume": float(getattr(row, "volume") or 0.0),
                    "oi": float(getattr(row, "oi") or 0.0),
                    "iv": float(getattr(row, "iv") or 0.0),
                    "lot_size": int(getattr(row, "lot_size") or 1),
                    "tick_size": 0.05,
                }
            )
        return payload

    async def latest_live_watchlist_status(
        self,
        *,
        underlying: str,
        as_of: str | datetime | pd.Timestamp | None = None,
    ) -> dict[str, Any]:
        as_of_ts = _parse_ts(as_of or datetime.now(UTC))
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    WITH latest AS (
                        SELECT DISTINCT ON (expiry, strike, option_type)
                            time,
                            expiry,
                            strike,
                            option_type
                        FROM atm_option_watchlist_snapshots
                        WHERE underlying = :underlying
                          AND expiry >= CURRENT_DATE
                          AND ltp IS NOT NULL
                          AND time <= :as_of
                        ORDER BY expiry, strike, option_type, time DESC
                    )
                    SELECT COUNT(*) AS rows, MAX(time) AS latest_time
                    FROM latest
                    """
                ),
                {
                    "underlying": underlying.upper(),
                    "as_of": as_of_ts.to_pydatetime(),
                },
            )
            row = result.first()

        latest_time = getattr(row, "latest_time", None) if row is not None else None
        return {
            "rows": int(getattr(row, "rows", 0) or 0) if row is not None else 0,
            "latest_time": _parse_ts(latest_time).isoformat() if latest_time is not None else None,
        }

    async def latest_local_option_mark(
        self,
        *,
        underlying: str,
        expiry: str | date,
        strike: float,
        option_type: str,
        instrument_key: str | None = None,
    ) -> tuple[Optional[float], Optional[str], str]:
        params = {
            "underlying": underlying.upper(),
            "expiry": date.fromisoformat(str(expiry)) if not isinstance(expiry, date) else expiry,
            "strike": float(strike),
            "option_type": option_type,
            "instrument_key": str(instrument_key or "").strip(),
        }
        async with AsyncSessionLocal() as session:
            if params["instrument_key"]:
                result = await session.execute(
                    text(
                        """
                        SELECT time, ltp
                        FROM atm_option_watchlist_snapshots
                        WHERE instrument_key = :instrument_key
                          AND ltp IS NOT NULL
                        ORDER BY time DESC
                        LIMIT 1
                        """
                    ),
                    {"instrument_key": params["instrument_key"]},
                )
                row = result.first()
                if row is not None:
                    return float(row.ltp), _parse_ts(row.time).isoformat(), "local_watchlist"
            result = await session.execute(
                text(
                    """
                    SELECT time, ltp
                    FROM atm_option_watchlist_snapshots
                    WHERE underlying = :underlying
                      AND expiry = :expiry
                      AND strike = :strike
                      AND option_type = :option_type
                      AND ltp IS NOT NULL
                    ORDER BY time DESC
                    LIMIT 1
                    """
                ),
                params,
            )
            row = result.first()
            if row is not None:
                return float(row.ltp), _parse_ts(row.time).isoformat(), "local_watchlist"
        candles = await option_history_service.load_candles(
            underlying=underlying.upper(),
            expiry=params["expiry"],
            strike=float(strike),
            option_type=option_type,
            instrument_key=params["instrument_key"] or None,
            interval="1minute",
            limit=3,
            allow_broker_refresh=False,
        )
        if not candles:
            return None, None, "none"
        latest = candles[-1]
        close_value = latest.get("close")
        if close_value is None:
            return None, None, "none"
        return float(close_value), str(latest.get("time") or ""), "local_option_history"

    async def load_local_option_frame(
        self,
        *,
        underlying: str,
        expiry: str | date,
        strike: float,
        option_type: str,
        instrument_key: str | None = None,
        interval: str = "5minute",
        limit: int = 80,
    ) -> pd.DataFrame:
        candles = await option_history_service.load_candles(
            underlying=underlying.upper(),
            expiry=date.fromisoformat(str(expiry)) if not isinstance(expiry, date) else expiry,
            strike=float(strike),
            option_type=option_type,
            instrument_key=str(instrument_key or "").strip() or None,
            interval=interval,
            limit=max(int(limit), 1),
            allow_broker_refresh=False,
        )
        return _frame_from_rows(candles)

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

    @staticmethod
    def _expiry_kind(underlying: str, expiry_value: date) -> str:
        normalized = str(underlying or "").upper().strip()
        try:
            monthly = get_index_monthly_expiry(normalized, expiry_value.year, expiry_value.month)
        except Exception:
            monthly = get_monthly_expiry(expiry_value.year, expiry_value.month)
        return "monthly" if expiry_value == monthly else "weekly"
