"""Adapters that feed the CBE scanner from Nomad Curie's existing datasets."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd
from loguru import logger
from sqlalchemy import bindparam, text

from analytics.sector import SECTOR_CONFIGS, SECTOR_STOCKS
from db.database import AsyncSessionLocal

from .data_provider import DataProvider


IST = timezone(timedelta(hours=5, minutes=30))


def normalize_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def _to_float_frame(frame: pd.DataFrame, columns: Iterable[str]) -> pd.DataFrame:
    out = frame.copy()
    for column in columns:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce").astype(float)
    return out


def _stock_to_sector_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for config in SECTOR_CONFIGS:
        for symbol in config.members:
            mapping.setdefault(normalize_symbol(symbol), config.code)
    return mapping


@dataclass
class ProjectTimescaleDataProvider(DataProvider):
    """In-memory DataProvider built from existing Timescale/Postgres tables.

    The original CBE handoff assumes purpose-built tables such as
    `ohlc_daily` and `options_snapshot`. This adapter reuses the tables already
    present in Nomad Curie: `underlying_spot_candles`,
    `atm_option_watchlist_snapshots`, `option_premium_candles`, and the static
    sector taxonomy from `analytics.sector`.
    """

    ohlc_by_symbol: dict[str, pd.DataFrame] = field(default_factory=dict)
    options_by_symbol: dict[str, pd.DataFrame] = field(default_factory=dict)
    iv_history_by_symbol: dict[str, pd.Series] = field(default_factory=dict)
    pcr_history_by_symbol: dict[str, pd.Series] = field(default_factory=dict)
    sector_returns_by_symbol: dict[str, pd.Series] = field(default_factory=dict)
    source_status: dict[str, Any] = field(default_factory=dict)

    def get_ohlc(self, symbol: str, lookback_days: int = 300) -> Optional[pd.DataFrame]:
        frame = self.ohlc_by_symbol.get(normalize_symbol(symbol))
        if frame is None or frame.empty:
            return None
        return frame.tail(max(int(lookback_days), 1)).copy()

    def get_options_chain(self, symbol: str, expiry: Optional[pd.Timestamp] = None) -> Optional[pd.DataFrame]:
        frame = self.options_by_symbol.get(normalize_symbol(symbol))
        if frame is None or frame.empty:
            return None
        if expiry is not None and "expiry" in frame.columns:
            expiry_date = pd.Timestamp(expiry).date()
            frame = frame[pd.to_datetime(frame["expiry"], errors="coerce").dt.date == expiry_date]
        return frame.copy() if not frame.empty else None

    def get_iv_history(self, symbol: str, lookback_days: int = 300) -> Optional[pd.Series]:
        series = self.iv_history_by_symbol.get(normalize_symbol(symbol))
        if series is None or series.empty:
            return None
        return series.tail(max(int(lookback_days), 1)).copy()

    def get_pcr_history(self, symbol: str, lookback_days: int = 300) -> Optional[pd.Series]:
        series = self.pcr_history_by_symbol.get(normalize_symbol(symbol))
        if series is None or series.empty:
            return None
        return series.tail(max(int(lookback_days), 1)).copy()

    def get_sector_returns(self, symbol: str) -> Optional[pd.Series]:
        series = self.sector_returns_by_symbol.get(normalize_symbol(symbol))
        if series is None or series.empty:
            return None
        return series.copy()

    def get_events(self, symbol: str, lookahead_days: int = 10) -> list:
        return []

    def get_spread_history(self, symbol: str) -> Optional[pd.Series]:
        return None

    def get_block_deals(self, symbol: str) -> Optional[pd.DataFrame]:
        return None

    def get_fii_dii_flow(self, symbol: str) -> Optional[pd.Series]:
        return None


async def load_project_universe(limit: int = 500) -> list[str]:
    """Return active F&O stock underlyings from the local contract catalog."""
    excluded = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX")
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    SELECT DISTINCT UPPER(underlying) AS underlying
                    FROM fo_contract_catalog
                    WHERE COALESCE(market, 'NSE') = 'NSE'
                      AND option_type IN ('CE', 'PE')
                      AND expiry >= CURRENT_DATE
                      AND underlying NOT IN :excluded
                    ORDER BY underlying
                    LIMIT :limit
                    """
                ).bindparams(bindparam("excluded", expanding=True)),
                {"limit": int(limit), "excluded": excluded},
            )
            symbols = [str(row.underlying) for row in result.fetchall() if row.underlying]
            if len(symbols) >= min(int(limit), 20):
                return symbols
            snapshot_result = await session.execute(
                text(
                    """
                    SELECT DISTINCT UPPER(underlying) AS underlying
                    FROM atm_option_watchlist_snapshots
                    WHERE time >= NOW() - INTERVAL '30 days'
                      AND UPPER(underlying) NOT IN :excluded
                    ORDER BY underlying
                    LIMIT :limit
                    """
                ).bindparams(bindparam("excluded", expanding=True)),
                {"limit": int(limit), "excluded": excluded},
            )
            snapshot_symbols = [str(row.underlying) for row in snapshot_result.fetchall() if row.underlying]
            if len(snapshot_symbols) >= min(int(limit), 20):
                return snapshot_symbols
            premium_result = await session.execute(
                text(
                    """
                    SELECT DISTINCT UPPER(underlying) AS underlying
                    FROM option_premium_candles
                    WHERE time >= NOW() - INTERVAL '45 days'
                      AND UPPER(underlying) NOT IN :excluded
                    ORDER BY underlying
                    LIMIT :limit
                    """
                ).bindparams(bindparam("excluded", expanding=True)),
                {"limit": int(limit), "excluded": excluded},
            )
            premium_symbols = [str(row.underlying) for row in premium_result.fetchall() if row.underlying]
            if premium_symbols:
                return premium_symbols
    except Exception as exc:
        logger.warning(f"[CBE] Could not load F&O universe from DB: {exc}")
    return sorted(SECTOR_STOCKS)[: int(limit)]


async def load_project_timescale_provider(
    universe: Iterable[str],
    *,
    lookback_days: int = 300,
    as_of: datetime | None = None,
) -> ProjectTimescaleDataProvider:
    symbols = sorted({normalize_symbol(symbol) for symbol in universe if normalize_symbol(symbol)})
    if not symbols:
        return ProjectTimescaleDataProvider(source_status={"symbols": 0})

    start_time = as_of or datetime.now(IST)
    since = start_time - timedelta(days=max(int(lookback_days * 2), 30))
    status: dict[str, Any] = {"symbols": len(symbols), "lookback_days": int(lookback_days)}

    spot_rows: list[dict[str, Any]] = []
    option_rows: list[dict[str, Any]] = []
    premium_underlying_rows: list[dict[str, Any]] = []
    try:
        async with AsyncSessionLocal() as session:
            spot_query = text(
                """
                SELECT time, UPPER(underlying) AS underlying, interval, open, high, low, close, volume
                FROM underlying_spot_candles
                WHERE UPPER(underlying) IN :symbols
                  AND time >= :since
                  AND time <= :as_of
                ORDER BY underlying, time
                """
            ).bindparams(bindparam("symbols", expanding=True))
            spot_result = await session.execute(
                spot_query,
                {"symbols": symbols, "since": since, "as_of": start_time},
            )
            spot_rows = [dict(row) for row in spot_result.mappings().all()]

            option_query = text(
                """
                WITH latest AS (
                    SELECT DISTINCT ON (UPPER(underlying), expiry, strike, option_type)
                        time,
                        UPPER(underlying) AS underlying,
                        expiry,
                        strike,
                        option_type,
                        oi,
                        oi_change,
                        volume,
                        iv,
                        underlying_price
                    FROM atm_option_watchlist_snapshots
                    WHERE UPPER(underlying) IN :symbols
                      AND expiry >= CURRENT_DATE
                      AND time <= :as_of
                    ORDER BY UPPER(underlying), expiry, strike, option_type, time DESC
                )
                SELECT *
                FROM latest
                ORDER BY underlying, expiry, strike, option_type
                """
            ).bindparams(bindparam("symbols", expanding=True))
            option_result = await session.execute(
                option_query,
                {"symbols": symbols, "as_of": start_time},
            )
            option_rows = [dict(row) for row in option_result.mappings().all()]

            missing_spot_symbols = sorted(set(symbols) - {str(row.get("underlying")) for row in spot_rows})
            if missing_spot_symbols:
                premium_query = text(
                    """
                    SELECT time,
                           UPPER(underlying) AS underlying,
                           underlying_price,
                           close,
                           volume
                    FROM option_premium_candles
                    WHERE UPPER(underlying) IN :symbols
                      AND time >= :since
                      AND time <= :as_of
                      AND (underlying_price IS NOT NULL OR close IS NOT NULL)
                    ORDER BY underlying, time
                    """
                ).bindparams(bindparam("symbols", expanding=True))
                premium_result = await session.execute(
                    premium_query,
                    {"symbols": missing_spot_symbols, "since": since, "as_of": start_time},
                )
                premium_underlying_rows = [dict(row) for row in premium_result.mappings().all()]
    except Exception as exc:
        logger.warning(f"[CBE] Project provider DB load failed: {exc}")
        status["error"] = str(exc)

    spot_ohlc = _build_daily_ohlc(spot_rows, lookback_days=lookback_days)
    ohlc_by_symbol = dict(spot_ohlc)
    option_price_ohlc = _build_daily_ohlc_from_option_underlying(
        premium_underlying_rows,
        lookback_days=lookback_days,
    )
    for symbol, frame in option_price_ohlc.items():
        ohlc_by_symbol.setdefault(symbol, frame)
    options_by_symbol, iv_history_by_symbol, pcr_history_by_symbol = _build_option_inputs(option_rows)
    sector_returns_by_symbol = _build_sector_returns(ohlc_by_symbol)
    status.update(
        {
            "spot_rows": len(spot_rows),
            "options_rows": len(option_rows),
            "option_underlying_rows": len(premium_underlying_rows),
            "ohlc_symbols": len(ohlc_by_symbol),
            "ohlc_from_spot_symbols": len(spot_ohlc),
            "ohlc_from_option_symbols": len(option_price_ohlc),
            "options_symbols": len(options_by_symbol),
            "sector_symbols": len(sector_returns_by_symbol),
        }
    )
    return ProjectTimescaleDataProvider(
        ohlc_by_symbol=ohlc_by_symbol,
        options_by_symbol=options_by_symbol,
        iv_history_by_symbol=iv_history_by_symbol,
        pcr_history_by_symbol=pcr_history_by_symbol,
        sector_returns_by_symbol=sector_returns_by_symbol,
        source_status=status,
    )


def _build_daily_ohlc(rows: list[dict[str, Any]], *, lookback_days: int) -> dict[str, pd.DataFrame]:
    if not rows:
        return {}
    frame = pd.DataFrame(rows)
    frame["time"] = pd.to_datetime(frame["time"], errors="coerce", utc=True)
    frame = frame.dropna(subset=["time"])
    if frame.empty:
        return {}
    frame = _to_float_frame(frame, ["open", "high", "low", "close", "volume"])
    frame["session"] = frame["time"].dt.tz_convert(IST).dt.normalize()
    out: dict[str, pd.DataFrame] = {}
    for symbol, symbol_frame in frame.groupby("underlying", sort=False):
        symbol_frame = symbol_frame.sort_values("time")
        daily = (
            symbol_frame.groupby("session", sort=True)
            .agg(
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
                volume=("volume", "sum"),
            )
            .dropna(subset=["open", "high", "low", "close"])
            .tail(max(int(lookback_days), 1))
        )
        if not daily.empty:
            daily.index = pd.DatetimeIndex(daily.index)
            out[str(symbol)] = daily
    return out


def _build_daily_ohlc_from_option_underlying(
    rows: list[dict[str, Any]],
    *,
    lookback_days: int,
) -> dict[str, pd.DataFrame]:
    if not rows:
        return {}
    frame = pd.DataFrame(rows)
    frame["time"] = pd.to_datetime(frame["time"], errors="coerce", utc=True)
    frame = frame.dropna(subset=["time"])
    if frame.empty:
        return {}
    frame = _to_float_frame(frame, ["underlying_price", "close", "volume"])
    frame["price"] = frame["underlying_price"].where(frame["underlying_price"].notna(), frame["close"])
    frame = frame.dropna(subset=["price"])
    if frame.empty:
        return {}
    # Multiple option contracts repeat the same underlying price at a timestamp.
    # Collapse to one price series per symbol before making daily bars.
    intraday = (
        frame.groupby(["underlying", "time"], sort=True)
        .agg(price=("price", "mean"), volume=("volume", "sum"))
        .reset_index()
    )
    intraday["session"] = intraday["time"].dt.tz_convert(IST).dt.normalize()
    out: dict[str, pd.DataFrame] = {}
    for symbol, symbol_frame in intraday.groupby("underlying", sort=False):
        daily = (
            symbol_frame.groupby("session", sort=True)
            .agg(
                open=("price", "first"),
                high=("price", "max"),
                low=("price", "min"),
                close=("price", "last"),
                volume=("volume", "sum"),
            )
            .dropna(subset=["open", "high", "low", "close"])
            .tail(max(int(lookback_days), 1))
        )
        if not daily.empty:
            daily.index = pd.DatetimeIndex(daily.index)
            out[str(symbol)] = daily
    return out


def _build_option_inputs(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.Series], dict[str, pd.Series]]:
    if not rows:
        return {}, {}, {}
    frame = pd.DataFrame(rows)
    frame = _to_float_frame(frame, ["strike", "oi", "oi_change", "volume", "iv", "underlying_price"])
    if "time" in frame.columns:
        frame["time"] = pd.to_datetime(frame["time"], errors="coerce", utc=True)
        frame["session"] = frame["time"].dt.tz_convert(IST).dt.normalize()
    options_by_symbol: dict[str, pd.DataFrame] = {}
    iv_history_by_symbol: dict[str, pd.Series] = {}
    pcr_history_by_symbol: dict[str, pd.Series] = {}

    for symbol, symbol_frame in frame.groupby("underlying", sort=False):
        chain = symbol_frame.rename(
            columns={"option_type": "type", "oi_change": "oi_change_1d"}
        )[["strike", "type", "oi", "oi_change_1d", "volume", "iv"]].copy()
        chain["delta"] = _estimate_delta(chain)
        options_by_symbol[str(symbol)] = chain.dropna(subset=["strike", "type"])

        if "session" in symbol_frame.columns and "iv" in symbol_frame.columns:
            iv_series = symbol_frame.dropna(subset=["iv"]).groupby("session")["iv"].mean()
            if not iv_series.empty:
                iv_history_by_symbol[str(symbol)] = iv_series.astype(float)

        if "session" in symbol_frame.columns:
            ce = symbol_frame[symbol_frame["option_type"] == "CE"].groupby("session")["oi"].sum()
            pe = symbol_frame[symbol_frame["option_type"] == "PE"].groupby("session")["oi"].sum()
            pcr = (pe / ce.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).dropna()
            if not pcr.empty:
                pcr_history_by_symbol[str(symbol)] = pcr.astype(float)

    return options_by_symbol, iv_history_by_symbol, pcr_history_by_symbol


def _estimate_delta(chain: pd.DataFrame) -> pd.Series:
    if chain.empty:
        return pd.Series(dtype="float64")
    atm = float(pd.to_numeric(chain["strike"], errors="coerce").median() or 0.0)
    if atm <= 0:
        return pd.Series([0.5] * len(chain), index=chain.index)
    moneyness = (pd.to_numeric(chain["strike"], errors="coerce") - atm) / atm
    call_delta = (0.5 + moneyness * 5.0).clip(0.05, 0.95)
    put_delta = -(0.5 - moneyness * 5.0).clip(0.05, 0.95)
    return pd.Series(np.where(chain["type"] == "PE", put_delta, call_delta), index=chain.index)


def _build_sector_returns(ohlc_by_symbol: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
    stock_to_sector = _stock_to_sector_map()
    members_by_sector: dict[str, list[str]] = defaultdict(list)
    for symbol in ohlc_by_symbol:
        sector = stock_to_sector.get(symbol)
        if sector:
            members_by_sector[sector].append(symbol)

    sector_returns: dict[str, pd.Series] = {}
    for sector, members in members_by_sector.items():
        member_returns: list[pd.Series] = []
        for symbol in members:
            close = pd.to_numeric(ohlc_by_symbol[symbol]["close"], errors="coerce")
            returns = np.log(close / close.shift(1)).dropna()
            if not returns.empty:
                member_returns.append(returns)
        if not member_returns:
            continue
        sector_series = pd.concat(member_returns, axis=1).mean(axis=1).dropna()
        for symbol in members:
            sector_returns[symbol] = sector_series
    return sector_returns
