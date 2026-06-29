"""Service helpers for running CBE scans without coupling callers to pandas."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Literal, Optional

import numpy as np
import pandas as pd

from .data_provider import DataProvider, SyntheticDataProvider
from .features import CBEConfig, compute_cbe_score, generate_watchlist, scan_universe
from .paper import cbe_paper_book
from .project_provider import load_project_timescale_provider, load_project_universe
from .repository import persist_scan_payload


IST = timezone(timedelta(hours=5, minutes=30))

DEFAULT_SYNTHETIC_UNIVERSE: tuple[str, ...] = (
    "RELIANCE",
    "TCS",
    "ICICIBANK",
    "TATAMOTORS",
    "BAJFINANCE",
    "INFY",
    "HDFCBANK",
    "SBIN",
    "BHARTIARTL",
    "KOTAKBANK",
    "HCLTECH",
    "WIPRO",
    "MARUTI",
    "ASIANPAINT",
    "AXISBANK",
    "LT",
    "ITC",
    "NTPC",
    "POWERGRID",
    "ULTRACEMCO",
)


def build_config(
    *,
    watchlist_min_score: float | None = None,
    watchlist_max_size: int | None = None,
) -> CBEConfig:
    cfg = CBEConfig()
    if watchlist_min_score is not None:
        cfg.watchlist_min_score = float(watchlist_min_score)
    if watchlist_max_size is not None:
        cfg.watchlist_max_size = int(watchlist_max_size)
    return cfg


def _scan_with_provider(
    provider: DataProvider,
    universe: Iterable[str],
    *,
    scan_date: date | datetime | pd.Timestamp | None = None,
    cfg: CBEConfig | None = None,
    source: str,
    source_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = cfg or CBEConfig()
    timestamp = pd.Timestamp(scan_date or datetime.now(IST))
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert(IST).tz_localize(None)

    symbols = [str(symbol).strip().upper() for symbol in universe if str(symbol).strip()]
    scan_df = scan_universe(symbols, provider, timestamp, cfg)
    watchlist_df = generate_watchlist(scan_df, cfg)
    return {
        "source": source,
        "source_status": source_status or {},
        "scan_date": timestamp.date().isoformat(),
        "universe_size": len(symbols),
        "scored_count": int(len(scan_df)),
        "watchlist_count": int(len(watchlist_df)),
        "config": _jsonable(cfg.__dict__),
        "results": _records(scan_df),
        "watchlist": _records(watchlist_df),
    }


def run_synthetic_scan(
    *,
    universe: Iterable[str] | None = None,
    scan_date: date | datetime | pd.Timestamp | None = None,
    seed: int = 42,
    cfg: CBEConfig | None = None,
) -> dict[str, Any]:
    symbols = list(universe or DEFAULT_SYNTHETIC_UNIVERSE)
    provider = SyntheticDataProvider(seed=seed, today=pd.Timestamp(scan_date or "2024-12-27"))
    return _scan_with_provider(
        provider,
        symbols,
        scan_date=scan_date or provider.today,
        cfg=cfg,
        source="synthetic",
        source_status={"seed": seed},
    )


async def run_project_scan(
    *,
    universe: Iterable[str] | None = None,
    scan_date: date | datetime | pd.Timestamp | None = None,
    lookback_days: int = 300,
    cfg: CBEConfig | None = None,
) -> dict[str, Any]:
    symbols = list(universe or await load_project_universe(limit=500))
    as_of = _as_datetime(scan_date) if scan_date is not None else datetime.now(IST)
    provider = await load_project_timescale_provider(symbols, lookback_days=lookback_days, as_of=as_of)
    return _scan_with_provider(
        provider,
        symbols,
        scan_date=scan_date or as_of,
        cfg=cfg,
        source="project_timescale",
        source_status=provider.source_status,
    )


async def load_project_instrument_analytics(
    symbol: str,
    *,
    scan_date: date | datetime | pd.Timestamp | None = None,
    lookback_days: int = 300,
    cfg: CBEConfig | None = None,
) -> dict[str, Any]:
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        return {"symbol": normalized, "available": False, "reason": "Symbol is required"}

    as_of = _as_datetime(scan_date) if scan_date is not None else datetime.now(IST)
    provider = await load_project_timescale_provider([normalized], lookback_days=lookback_days, as_of=as_of)
    cfg = cfg or CBEConfig()
    ohlc = provider.get_ohlc(normalized, lookback_days=lookback_days)
    options = provider.get_options_chain(normalized)
    iv_history = provider.get_iv_history(normalized, lookback_days=lookback_days)
    pcr_history = provider.get_pcr_history(normalized, lookback_days=lookback_days)
    sector_returns = provider.get_sector_returns(normalized)

    score = None
    if ohlc is not None and not ohlc.empty:
        score = compute_cbe_score(
            normalized,
            pd.Timestamp(as_of),
            ohlc,
            options_chain=options,
            iv_history=iv_history,
            pcr_history=pcr_history,
            sector_returns=sector_returns,
            cfg=cfg,
        )

    return {
        "symbol": normalized,
        "available": ohlc is not None and not ohlc.empty,
        "scan_date": as_of.date().isoformat(),
        "source_status": provider.source_status,
        "score": _jsonable(score.to_dict()) if score is not None else None,
        "ohlc": _ohlc_records(ohlc),
        "option_chain": _option_chain_records(options),
        "iv_history": _series_records(iv_history, "iv"),
        "pcr_history": _series_records(pcr_history, "pcr"),
        "sector_returns": _series_records(sector_returns, "sector_return"),
    }


async def run_scan(
    *,
    source: Literal["synthetic", "project_timescale", "alpha_engine"] = "alpha_engine",
    universe: Iterable[str] | None = None,
    scan_date: date | datetime | pd.Timestamp | None = None,
    seed: int = 42,
    lookback_days: int = 300,
    cfg: CBEConfig | None = None,
    sync_paper_book: bool = True,
    alpha_config: "AlphaEngineConfig | None" = None,
) -> dict[str, Any]:
    """Run the scanner. Default source is the alpha engine.

    The legacy "project_timescale" (composite vol-compression scoring) and
    "synthetic" sources stay reachable for backwards-compatible research
    queries but are no longer used by the paper book or supervisor.
    """
    if source == "alpha_engine":
        from .alpha_engine import AlphaEngineConfig, run_alpha_pipeline

        cfg_obj = alpha_config or AlphaEngineConfig()
        payload = await run_alpha_pipeline(cfg_obj)
    elif source == "synthetic":
        payload = run_synthetic_scan(universe=universe, scan_date=scan_date, seed=seed, cfg=cfg)
    else:
        payload = await run_project_scan(
            universe=universe,
            scan_date=scan_date,
            lookback_days=lookback_days,
            cfg=cfg,
        )
    run_id = await persist_scan_payload(payload)
    payload["run_id"] = run_id
    if sync_paper_book and source == "alpha_engine":
        # Drive the cash-equity paper book from this scan. Failure is logged
        # but never blocks scan persistence — the scan is the canonical
        # record; the book is a derived artifact.
        try:
            paper_summary = await cbe_paper_book.sync_from_scan(payload)
            payload["paper_summary"] = paper_summary
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(f"[CBE] paper-book sync failed: {exc}")
            payload["paper_summary"] = {"error": str(exc)}
    elif sync_paper_book:
        payload["paper_summary"] = {
            "skipped": True,
            "reason": "legacy research scans cannot mutate the alpha paper book",
        }
    return payload


async def refresh_paper_marks() -> dict[str, Any]:
    """Lightweight LTP refresh for CBE open paper positions only.

    Re-marks held cash-equity positions off the latest 30-min spot bar that the
    FNO spot ingest has ALREADY written to ``underlying_spot_candles`` — no new
    broker fetch and no alpha pipeline. One batched query covers every held
    symbol, so it is cheap enough for a 5-minute cadence during market hours,
    keeping the UI's LTP fresh between the heavier end-of-day-design scans.
    """
    from sqlalchemy import text

    from db.database import AsyncSessionLocal

    from .paper import _norm_symbol, cbe_paper_book

    snapshot = await cbe_paper_book.list_positions(status="open", limit=500)
    symbols = sorted(
        {
            _norm_symbol(pos.get("instrument"))
            for pos in (snapshot.get("open_positions") or [])
            if _norm_symbol(pos.get("instrument"))
        }
    )
    if not symbols:
        return {"refreshed": 0, "symbols": [], "paper_summary": snapshot.get("summary")}

    prices: dict[str, float] = {}
    async with AsyncSessionLocal() as session:
        # Latest 30-min bar close per held symbol, straight from the spot
        # history already being ingested. DISTINCT ON keeps it to one row each.
        result = await session.execute(
            text(
                """
                SELECT DISTINCT ON (underlying) underlying, close
                FROM underlying_spot_candles
                WHERE underlying = ANY(:symbols)
                  AND interval = '30minute'
                  AND time >= NOW() - INTERVAL '5 days'
                ORDER BY underlying, time DESC
                """
            ),
            {"symbols": symbols},
        )
        for sym, close in result.fetchall():
            if close is not None:
                prices[_norm_symbol(sym)] = float(close)

    summary = await cbe_paper_book.refresh_open_marks(prices)
    return {
        "refreshed": len(prices),
        "symbols": list(prices.keys()),
        "paper_summary": summary,
    }


def _as_datetime(value: date | datetime | pd.Timestamp) -> datetime:
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, time(23, 59, 59), tzinfo=IST)
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize(IST)
    else:
        ts = ts.tz_convert(IST)
    return ts.to_pydatetime()


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    return [_jsonable(row) for row in frame.to_dict(orient="records")]


def _ohlc_records(frame: pd.DataFrame | None) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    out = frame.tail(120).copy()
    out = out.reset_index(names="date")
    return [_jsonable(row) for row in out.to_dict(orient="records")]


def _series_records(series: pd.Series | None, value_name: str) -> list[dict[str, Any]]:
    if series is None or series.empty:
        return []
    frame = series.tail(120).rename(value_name).reset_index()
    date_column = str(frame.columns[0])
    frame = frame.rename(columns={date_column: "date"})
    return [_jsonable(row) for row in frame.to_dict(orient="records")]


def _option_chain_records(frame: pd.DataFrame | None) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    chain = frame.copy()
    chain["type"] = chain["type"].astype(str).str.upper()
    for column in ["strike", "oi", "oi_change_1d", "volume", "iv", "delta"]:
        if column in chain.columns:
            chain[column] = pd.to_numeric(chain[column], errors="coerce")
    rows: list[dict[str, Any]] = []
    for strike, strike_frame in chain.groupby("strike", sort=True):
        if pd.isna(strike):
            continue
        call = strike_frame[strike_frame["type"] == "CE"]
        put = strike_frame[strike_frame["type"] == "PE"]
        rows.append(
            {
                "strike": float(strike),
                "call_oi": _sum_or_none(call, "oi"),
                "put_oi": _sum_or_none(put, "oi"),
                "call_volume": _sum_or_none(call, "volume"),
                "put_volume": _sum_or_none(put, "volume"),
                "call_iv": _mean_or_none(call, "iv"),
                "put_iv": _mean_or_none(put, "iv"),
                "call_oi_change": _sum_or_none(call, "oi_change_1d"),
                "put_oi_change": _sum_or_none(put, "oi_change_1d"),
            }
        )
    return [_jsonable(row) for row in rows[:120]]


def _sum_or_none(frame: pd.DataFrame, column: str) -> float | None:
    if frame.empty or column not in frame:
        return None
    value = pd.to_numeric(frame[column], errors="coerce").sum(min_count=1)
    return None if pd.isna(value) else float(value)


def _mean_or_none(frame: pd.DataFrame, column: str) -> float | None:
    if frame.empty or column not in frame:
        return None
    value = pd.to_numeric(frame[column], errors="coerce").mean()
    return None if pd.isna(value) else float(value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, float) and (np.isnan(value) or np.isinf(value)):
        return None
    return value
