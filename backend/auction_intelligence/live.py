from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from fastapi.encoders import jsonable_encoder
from sqlalchemy import text

from api.routers.auth import (
    ensure_fyers_session,
    ensure_upstox_session,
    get_active_adapter,
    get_broker_token,
)
from auction_intelligence.config import clone_default_config
from auction_intelligence.service import AuctionIntelligenceService
from auction_intelligence.schemas import (
    DepthLevel,
    DepthSnapshot,
    MarketBar,
    PortfolioSnapshot,
    QuoteSnapshot,
    SessionContext,
    TradePrint,
)
from brokers.base import Tick
from db.database import AsyncSessionLocal
from market_data import data_router as market_data_router
from market_data.symbols import to_broker_symbol, to_fyers_symbol


IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
DEFAULT_CONFIG = clone_default_config()
CONTRACT_SPECS = DEFAULT_CONFIG.get("contract_specs", {})
SYMBOL_MAP = {
    "NIFTY": {
        "app_symbol": "NSE:NIFTY50-INDEX",
        "display": "NIFTY",
        "instrument_proxy": "continuous_futures_proxy",
        "lot_size": 65,
        "tick_size": 0.5,
    },
    "BANKNIFTY": {
        "app_symbol": "NSE:BANKNIFTY-INDEX",
        "display": "BANKNIFTY",
        "instrument_proxy": "continuous_futures_proxy",
        "lot_size": 30,
        "tick_size": 0.5,
    },
    "FINNIFTY": {
        "app_symbol": "NSE:FINNIFTY-INDEX",
        "display": "FINNIFTY",
        "instrument_proxy": "continuous_futures_proxy",
        "lot_size": 40,
        "tick_size": 0.5,
    },
    "MIDCPNIFTY": {
        "app_symbol": "NSE:MIDCPNIFTY-INDEX",
        "display": "MIDCPNIFTY",
        "instrument_proxy": "continuous_futures_proxy",
        "lot_size": 75,
        "tick_size": 0.5,
    },
    "SENSEX": {
        "app_symbol": "BSE:SENSEX-INDEX",
        "display": "SENSEX",
        "instrument_proxy": "continuous_futures_proxy",
        "lot_size": 10,
        "tick_size": 0.5,
    },
}


def available_live_symbols() -> list[str]:
    return list(SYMBOL_MAP.keys())


def _fyers_continuous_futures_symbol(symbol_code: str, as_of: date) -> str:
    normalized_symbol = symbol_code.upper()
    return f"NSE:{normalized_symbol}{as_of.strftime('%y')}{as_of.strftime('%b').upper()}FUT"


async def _fetch_chunked_fyers_history(
    get_history,
    symbol: str,
    resolution: str,
    from_date: date,
    to_date: date,
    *,
    chunk_days: int,
    cont_flag: int,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    chunk_start = from_date
    while chunk_start <= to_date:
        chunk_end = min(chunk_start + timedelta(days=chunk_days - 1), to_date)
        rows = await get_history(
            symbol,
            resolution,
            chunk_start.isoformat(),
            chunk_end.isoformat(),
            cont_flag=cont_flag,
        )
        for row in rows:
            merged[str(row["time"])] = row
        chunk_start = chunk_end + timedelta(days=1)
    return [merged[key] for key in sorted(merged)]


async def _fetch_fyers_quote(symbol: str) -> dict[str, Any] | None:
    fyers = get_active_adapter("fyers")
    if fyers is None and await ensure_fyers_session():
        fyers = get_active_adapter("fyers")
    if fyers is None:
        return None

    get_json = getattr(fyers, "_get_data_json", None)
    if not callable(get_json):
        return None

    try:
        payload = await get_json("/quotes", {"symbols": symbol})
    except Exception:
        return None

    rows = payload.get("d", [])
    if not rows:
        return None
    quote = rows[0].get("v", {})
    if str(quote.get("s", "ok")).lower() == "error":
        return None
    last_trade_time = quote.get("tt")
    timestamp = None
    if last_trade_time:
        try:
            timestamp = datetime.fromtimestamp(int(str(last_trade_time)), timezone.utc).isoformat()
        except Exception:
            timestamp = None
    return {
        "timestamp": timestamp,
        "bid": float(quote.get("bid") or 0.0),
        "ask": float(quote.get("ask") or 0.0),
        "bid_size": float(quote.get("bid_size") or quote.get("bq") or 0.0),
        "ask_size": float(quote.get("ask_size") or quote.get("aq") or 0.0),
        "last_price": float(quote.get("lp") or 0.0),
    }


async def build_live_analysis(symbol_code: str = "NIFTY") -> dict[str, Any]:
    normalized_symbol = symbol_code.upper()
    if normalized_symbol not in SYMBOL_MAP:
        raise ValueError(f"Unsupported live symbol: {symbol_code}")

    recent_rows, history_source, history_symbol = await _fetch_recent_minute_rows(normalized_symbol)
    if not recent_rows:
        raise RuntimeError("No local or broker minute data returned for the requested symbol.")

    sessions = _group_rows_by_session(recent_rows)
    session_dates = sorted(sessions.keys())
    if len(session_dates) < 2:
        raise RuntimeError("At least two completed sessions are required for live validation.")

    latest_session_date = session_dates[-1]
    prior_session_date = session_dates[-2]
    return await _build_analysis_from_session_rows(
        normalized_symbol,
        current_session_rows=sessions[latest_session_date],
        prior_session_rows=sessions[prior_session_date],
        history_source=history_source,
        history_symbol=history_symbol,
        snapshot_cutoff=None,
    )


async def build_shadow_backfill_snapshots(
    symbol_code: str = "BANKNIFTY",
    *,
    lookback_days: int = 45,
    max_sessions: int = 20,
    observation_bars: int | None = None,
    snapshot_cutoff: time | None = None,
    shadow_net_liquidation: float | None = None,
) -> dict[str, Any]:
    normalized_symbol = symbol_code.upper()
    if normalized_symbol not in SYMBOL_MAP:
        raise ValueError(f"Unsupported live symbol: {symbol_code}")

    recent_rows, history_source, history_symbol = await _fetch_recent_minute_rows(normalized_symbol, lookback_days=lookback_days)
    if not recent_rows:
        raise RuntimeError("No local or broker minute data returned for the requested symbol.")

    sessions = _group_rows_by_session(recent_rows)
    session_dates = sorted(sessions.keys())
    if len(session_dates) < 2:
        raise RuntimeError("At least two completed sessions are required for shadow backfill.")

    selected_dates = session_dates[-max_sessions:] if max_sessions > 0 else session_dates[:]
    snapshots: list[dict[str, Any]] = []
    skipped_sessions: list[dict[str, Any]] = []
    session_symbol = _session_symbol(normalized_symbol, history_source)
    shadow_portfolio = await _build_shadow_portfolio_snapshot(
        session_symbol,
        shadow_net_liquidation=shadow_net_liquidation,
    )

    for session_date in selected_dates:
        session_index = session_dates.index(session_date)
        if session_index == 0:
            skipped_sessions.append(
                {"session_date": session_date.isoformat(), "error": "missing_prior_session"}
            )
            continue
        prior_session_date = session_dates[session_index - 1]
        try:
            snapshot = await _build_analysis_from_session_rows(
                normalized_symbol,
                current_session_rows=sessions[session_date],
                prior_session_rows=sessions[prior_session_date],
                history_source=history_source,
                history_symbol=history_symbol,
                portfolio_payload={**shadow_portfolio, "symbol_exposure": {session_symbol: 0.0}},
                observation_bars=observation_bars,
                snapshot_cutoff=snapshot_cutoff,
            )
            snapshots.append(snapshot)
        except RuntimeError as exc:
            skipped_sessions.append(
                {"session_date": session_date.isoformat(), "error": str(exc)}
            )

    return {
        "symbol_code": normalized_symbol,
        "source": history_source,
        "history_symbol": history_symbol,
        "snapshot_count": len(snapshots),
        "skipped_sessions": skipped_sessions,
        "snapshots": snapshots,
    }


async def _build_analysis_from_session_rows(
    normalized_symbol: str,
    *,
    current_session_rows: list[dict[str, Any]],
    prior_session_rows: list[dict[str, Any]],
    history_source: str,
    history_symbol: str,
    portfolio_payload: dict[str, Any] | None = None,
    observation_bars: int | None = None,
    snapshot_cutoff: time | None = None,
) -> dict[str, Any]:
    config = SYMBOL_MAP[normalized_symbol]
    app_symbol = str(config["app_symbol"])
    tick_size = float(config["tick_size"])
    session_date = _row_time(current_session_rows[-1]).date()
    session_symbol = _session_symbol(normalized_symbol, history_source)
    is_futures_source = "futures" in history_source

    current_rows, snapshot_time_local, snapshot_mode = _select_snapshot_rows(
        current_session_rows,
        observation_bars=observation_bars,
        snapshot_cutoff=snapshot_cutoff,
    )
    if len(current_rows) < 120:
        raise RuntimeError("The selected live snapshot does not have enough minute history yet.")

    current_bars = _aggregate_rows(current_rows, interval_minutes=30)
    prior_bars = _aggregate_rows(prior_session_rows, interval_minutes=30)
    if len(current_bars) < 4 or len(prior_bars) < 4:
        raise RuntimeError("Insufficient 30-minute bars were built from the broker history.")

    current_quote_tick = market_data_router.get_latest_tick(app_symbol)
    futures_quote = await _fetch_fyers_quote(history_symbol) if is_futures_source and snapshot_mode == "live_session" else None
    order_flow_inputs = await _build_order_flow_inputs(
        app_symbol=app_symbol,
        current_rows=current_rows,
        current_tick=current_quote_tick,
        quote_override=futures_quote,
        tick_size=tick_size,
        snapshot_mode=snapshot_mode,
    )
    quote_payload = order_flow_inputs["quote"]
    depth_payload = order_flow_inputs["depth"]
    trades_payload = order_flow_inputs["trades"]
    quote_history_payload = order_flow_inputs["quote_history"]
    quote_source = str(order_flow_inputs["quote_source"])
    order_flow_source = str(order_flow_inputs["order_flow_source"])
    stale_data_seconds = float(order_flow_inputs["stale_data_seconds"])
    portfolio_payload = portfolio_payload or await _load_portfolio_snapshot(session_symbol=session_symbol)

    request = {
        "session": {
            "symbol": session_symbol,
            "session_date": session_date.isoformat(),
            "last_price": round(float(quote_payload["last_price"]), 2),
            "stale_data_seconds": round(float(stale_data_seconds), 3),
            "minutes_to_close": max(
                0,
                int(
                    (
                        datetime.combine(session_date, SESSION_CLOSE, tzinfo=IST)
                        - snapshot_time_local
                    ).total_seconds()
                    // 60
                ),
            ),
            "broker_connected": True,
        },
        "portfolio": portfolio_payload,
        "quote": {
            "timestamp": quote_payload["timestamp"],
            "bid": quote_payload["bid"],
            "ask": quote_payload["ask"],
            "bid_size": quote_payload["bid_size"],
            "ask_size": quote_payload["ask_size"],
        },
        "depth": depth_payload,
        "quote_history": quote_history_payload,
        "bars": current_bars,
        "prior_bars": prior_bars,
        "trades": trades_payload,
        "metadata": {
            "symbol_code": normalized_symbol,
            "scenario": "live_snapshot",
            "scenario_label": "Live broker validation snapshot",
            "lot_size": config["lot_size"],
            "history_source": history_source,
            "history_symbol": history_symbol,
            "quote_source": quote_source,
            "order_flow_source": order_flow_source,
            "quote_event_count": len(quote_history_payload),
            "trade_print_count": len(trades_payload),
            "snapshot_mode": snapshot_mode,
            "snapshot_time": snapshot_time_local.isoformat(),
            "instrument_proxy": config["instrument_proxy"] if is_futures_source else "spot_index_proxy",
        },
    }

    service = AuctionIntelligenceService()
    bundle = await service.analyze_with_options(
        session=SessionContext(**request["session"]),
        bars=[MarketBar(**_parse_bar(item)) for item in request["bars"]],
        prior_bars=[MarketBar(**_parse_bar(item)) for item in request["prior_bars"]],
        quote=QuoteSnapshot(**_parse_quote(request["quote"])),
        trades=[TradePrint(**_parse_trade(item)) for item in request["trades"]],
        depth=DepthSnapshot(
            timestamp=datetime.fromisoformat(request["depth"]["timestamp"]),
            bids=[DepthLevel(**item) for item in request["depth"]["bids"]],
            asks=[DepthLevel(**item) for item in request["depth"]["asks"]],
        ),
        portfolio=PortfolioSnapshot(**request["portfolio"]),
        quote_history=[QuoteSnapshot(**_parse_quote(item)) for item in quote_history_payload],
    )
    return {
        "mode": "live",
        "scenario": "live_snapshot",
        "scenario_label": "Live broker validation snapshot",
        "symbol_code": normalized_symbol,
        "session_date": session_date.isoformat(),
        "available_symbols": available_live_symbols(),
        "available_scenarios": [],
        "request": request,
        "analysis": jsonable_encoder(asdict(bundle)),
    }


async def _fetch_recent_minute_rows(symbol_code: str, *, lookback_days: int = 7) -> tuple[list[dict[str, Any]], str, str]:
    today = datetime.now(IST).date()
    from_date = today - timedelta(days=lookback_days)
    futures_symbol = _fyers_continuous_futures_symbol(symbol_code, today)
    config = SYMBOL_MAP[symbol_code.upper()]
    app_symbol = str(config["app_symbol"])
    fyers_symbol = to_fyers_symbol(app_symbol)
    upstox_symbol = to_broker_symbol(app_symbol)
    best_available: tuple[list[dict[str, Any]], str, str] | None = None

    def _choose_source(
        rows: list[dict[str, Any]],
        source: str,
        history_symbol: str,
    ) -> tuple[list[dict[str, Any]], str, str] | None:
        nonlocal best_available
        if not rows:
            return None
        if best_available is None:
            best_available = (rows, source, history_symbol)
        if len(_group_rows_by_session(rows)) >= 2:
            return rows, source, history_symbol
        return None

    persisted_rows = await _fetch_persisted_spot_rows(symbol_code.upper(), from_date=from_date)
    selected = _choose_source(persisted_rows, "timescaledb_spot_1minute", app_symbol)
    if selected is not None:
        return selected

    fyers = get_active_adapter("fyers")
    if fyers is None and await ensure_fyers_session():
        fyers = get_active_adapter("fyers")
    get_history = getattr(fyers, "get_historical_candles", None) if fyers else None
    if callable(get_history):
        try:
            rows = await _fetch_chunked_fyers_history(
                get_history,
                futures_symbol,
                "1",
                from_date,
                today,
                chunk_days=4,
                cont_flag=1,
            )
            selected = _choose_source(rows, "fyers_continuous_futures", futures_symbol)
            if selected is not None:
                return selected
        except Exception:
            pass
        try:
            rows = await _fetch_chunked_fyers_history(
                get_history,
                fyers_symbol,
                "1",
                from_date,
                today,
                chunk_days=4,
                cont_flag=1,
            )
            selected = _choose_source(rows, "fyers_spot_index", fyers_symbol)
            if selected is not None:
                return selected
        except Exception:
            pass

    upstox = get_active_adapter("upstox")
    if upstox is None:
        await ensure_upstox_session()
    token = get_broker_token("upstox")
    if token:
        encoded_key = quote(upstox_symbol, safe="")
        url = (
            "https://api.upstox.com/v2/historical-candle/"
            f"{encoded_key}/1minute/{today.isoformat()}/{from_date.isoformat()}"
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )
        if response.status_code == 200:
            candles = response.json().get("data", {}).get("candles", [])
            rows = [
                {
                    "time": str(candle[0]),
                    "open": float(candle[1]),
                    "high": float(candle[2]),
                    "low": float(candle[3]),
                    "close": float(candle[4]),
                    "volume": int(candle[5] or 0),
                }
                for candle in reversed(candles)
            ]
            selected = _choose_source(rows, "upstox_spot_index", upstox_symbol)
            if selected is not None:
                return selected

    # ── CSV fallback: use locally downloaded 1-min spot data ─────────────────
    try:
        import csv, gzip, os
        csv_path = (
            _resolve_analytics_root()
            / "spot"
            / f"underlying={symbol_code.upper()}"
            / "1minute.csv.gz"
        )
        if csv_path.exists():
            cutoff = (datetime.now(IST) - timedelta(days=lookback_days)).date()
            rows = []
            with gzip.open(csv_path, "rt") as fh:
                for r in csv.DictReader(fh):
                    try:
                        ts = datetime.fromisoformat(r["time"])
                        if ts.astimezone(IST).date() >= cutoff:
                            rows.append({
                                "time": ts.isoformat(),
                                "open": float(r["open"]),
                                "high": float(r["high"]),
                                "low": float(r["low"]),
                                "close": float(r["close"]),
                                "volume": int(float(r.get("volume") or 0)),
                            })
                    except Exception:
                        continue
            selected = _choose_source(rows, "local_csv_spot", csv_path.name)
            if selected is not None:
                return selected
    except Exception:
        pass

    if best_available is not None:
        return best_available
    return [], "none", futures_symbol


async def _fetch_persisted_spot_rows(
    symbol_code: str,
    *,
    from_date: date,
) -> list[dict[str, Any]]:
    from_time = datetime.combine(from_date, time.min, tzinfo=IST).astimezone(timezone.utc)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT time, open, high, low, close, volume
                FROM underlying_spot_candles
                WHERE underlying = :underlying
                  AND interval = '1minute'
                  AND time >= :from_time
                ORDER BY time ASC
                """
            ),
            {
                "underlying": symbol_code,
                "from_time": from_time,
            },
        )
        rows = result.mappings().all()

    return [
        {
            "time": row["time"].astimezone(timezone.utc).isoformat()
            if isinstance(row["time"], datetime) and row["time"].tzinfo is not None
            else (
                row["time"].replace(tzinfo=timezone.utc).isoformat()
                if isinstance(row["time"], datetime)
                else str(row["time"])
            ),
            "open": float(row["open"] or row["close"] or 0.0),
            "high": float(row["high"] or row["close"] or 0.0),
            "low": float(row["low"] or row["close"] or 0.0),
            "close": float(row["close"] or 0.0),
            "volume": int(row["volume"] or 0),
        }
        for row in rows
    ]


def _resolve_analytics_root():
    from pathlib import Path
    import os
    env_path = os.environ.get("INDEX_ANALYTICS_DATA_DIR", "").strip()
    if env_path:
        return Path(env_path)
    docker_root = Path("/app/runtime/index_analytics_data")
    if docker_root.parent.is_dir():
        return docker_root
    return Path(__file__).resolve().parent.parent / "runtime" / "index_analytics_data"


async def build_live_validation_series(
    symbol_code: str = "BANKNIFTY",
    *,
    lookback_days: int = 45,
    max_sessions: int = 20,
) -> dict[str, Any]:
    normalized_symbol = symbol_code.upper()
    if normalized_symbol not in SYMBOL_MAP:
        raise ValueError(f"Unsupported live symbol: {symbol_code}")

    rows, history_source, _ = await _fetch_recent_minute_rows(normalized_symbol, lookback_days=lookback_days)
    sessions = _group_rows_by_session(rows)
    session_dates = sorted(sessions.keys())[-max_sessions:]

    return {
        "symbol_code": normalized_symbol,
        "source": history_source,
        "sessions": [
            {
                "session_date": session_date.isoformat(),
                "bars": _aggregate_rows(sessions[session_date], interval_minutes=30),
            }
            for session_date in session_dates
        ],
    }


def _session_symbol(symbol_code: str, history_source: str) -> str:
    display_symbol = str(SYMBOL_MAP[symbol_code.upper()]["display"])
    return f"{display_symbol} FUT" if "futures" in history_source else f"{display_symbol} INDEX"


async def _build_shadow_portfolio_snapshot(
    session_symbol: str,
    *,
    shadow_net_liquidation: float | None = None,
) -> dict[str, Any]:
    target_net_liquidation = float(
        shadow_net_liquidation
        or DEFAULT_CONFIG.get("paper_trading", {}).get("shadow_net_liquidation", 1_000_000.0)
    )
    return {
        "net_liquidation": round(target_net_liquidation, 2),
        "daily_realized_pnl": 0.0,
        "open_positions": 0,
        "symbol_exposure": {session_symbol: 0.0},
        "correlated_exposure": 0.0,
        "agent_drawdowns": {"positional": 0.0, "swing": 0.0, "scalp": 0.0},
    }


def _group_rows_by_session(rows: list[dict[str, Any]]) -> dict[date, list[dict[str, Any]]]:
    sessions: dict[date, list[dict[str, Any]]] = {}
    for row in rows:
        timestamp = _row_time(row)
        local_time = timestamp.astimezone(IST)
        if local_time.time() < SESSION_OPEN or local_time.time() > SESSION_CLOSE:
            continue
        normalized = {
            "time": local_time.isoformat(),
            "open": float(row.get("open", row.get("close", 0.0)) or 0.0),
            "high": float(row.get("high", row.get("close", 0.0)) or 0.0),
            "low": float(row.get("low", row.get("close", 0.0)) or 0.0),
            "close": float(row.get("close", 0.0) or 0.0),
            "volume": float(row.get("volume", 0.0) or 0.0),
        }
        sessions.setdefault(local_time.date(), []).append(normalized)

    for session_rows in sessions.values():
        session_rows.sort(key=lambda item: item["time"])
    return {
        key: value
        for key, value in sessions.items()
        if len(value) >= 180
    }


def _select_snapshot_rows(
    rows: list[dict[str, Any]],
    *,
    observation_bars: int | None = None,
    snapshot_cutoff: time | None = None,
) -> tuple[list[dict[str, Any]], datetime, str]:
    now_ist = datetime.now(IST)
    latest_time = _row_time(rows[-1])
    if latest_time.date() == now_ist.date() and SESSION_OPEN <= now_ist.time() < SESSION_CLOSE:
        return rows, latest_time, "live_session"

    if observation_bars and observation_bars > 0:
        aggregated = _aggregate_rows(rows, interval_minutes=30)
        if len(aggregated) >= observation_bars:
            cutoff_time = (
                datetime.fromisoformat(str(aggregated[observation_bars]["timestamp"]))
                if len(aggregated) > observation_bars
                else datetime.combine(_row_time(rows[-1]).date(), SESSION_CLOSE, tzinfo=IST) + timedelta(minutes=1)
            )
            selected = [row for row in rows if _row_time(row) < cutoff_time]
            if selected:
                return selected, _row_time(selected[-1]), "historical_replay"

    target_time = snapshot_cutoff or time(12, 20)
    eligible = [row for row in rows if _row_time(row).time() <= target_time]
    if eligible:
        snapshot_time = _row_time(eligible[-1])
        return eligible, snapshot_time, "historical_replay"

    fallback_index = max(0, min(len(rows) - 1, int(len(rows) * 0.7)))
    snapshot_rows = rows[: fallback_index + 1]
    return snapshot_rows, _row_time(snapshot_rows[-1]), "historical_replay"


def _aggregate_rows(rows: list[dict[str, Any]], *, interval_minutes: int) -> list[dict[str, Any]]:
    aggregated: list[dict[str, Any]] = []
    bucket_start: Optional[datetime] = None
    bucket: Optional[dict[str, Any]] = None

    for row in rows:
        timestamp = _row_time(row)
        session_start = datetime.combine(timestamp.date(), SESSION_OPEN, tzinfo=IST)
        elapsed_minutes = int((timestamp - session_start).total_seconds() // 60)
        bucket_index = max(0, elapsed_minutes // interval_minutes)
        current_bucket_start = session_start + timedelta(minutes=bucket_index * interval_minutes)

        if bucket_start != current_bucket_start:
            if bucket is not None:
                aggregated.append(bucket)
            bucket_start = current_bucket_start
            bucket = {
                "timestamp": current_bucket_start.isoformat(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0.0) or 0.0),
            }
            continue

        if bucket is None:
            continue

        bucket["high"] = max(float(bucket["high"]), float(row["high"]))
        bucket["low"] = min(float(bucket["low"]), float(row["low"]))
        bucket["close"] = float(row["close"])
        bucket["volume"] = float(bucket["volume"]) + float(row.get("volume", 0.0) or 0.0)

    if bucket is not None:
        aggregated.append(bucket)
    return aggregated


async def _build_order_flow_inputs(
    *,
    app_symbol: str,
    current_rows: list[dict[str, Any]],
    current_tick: Tick | None,
    quote_override: dict[str, Any] | None,
    tick_size: float,
    snapshot_mode: str,
) -> dict[str, Any]:
    recent_ticks = await _fetch_recent_tick_rows(
        app_symbol,
        snapshot_end=_row_time(current_rows[-1]).astimezone(timezone.utc),
    )
    if current_tick is not None:
        recent_ticks = _append_live_tick_row(recent_ticks, current_tick)

    quote_history_payload = _build_quote_history_from_ticks(recent_ticks, tick_size=tick_size)
    if snapshot_mode == "live_session" and len(quote_history_payload) >= 4:
        latest_quote = quote_history_payload[-1]
        depth_payload = _build_depth_from_tick_history(
            quote_history_payload,
            tick_size=tick_size,
        )
        trades_payload = _build_trade_prints_from_ticks(
            recent_ticks,
            tick_size=tick_size,
        )
        if len(trades_payload) < 4:
            trades_payload = _infer_trade_prints(current_rows)

        quote_timestamp = datetime.fromisoformat(str(latest_quote["timestamp"]))
        stale_data_seconds = max(
            0.0,
            (datetime.now(timezone.utc) - quote_timestamp.astimezone(timezone.utc)).total_seconds(),
        )
        return {
            "quote": {
                **latest_quote,
                "last_price": latest_quote["last_price"],
            },
            "depth": depth_payload,
            "trades": trades_payload,
            "quote_history": quote_history_payload,
            "quote_source": "market_ticks",
            "order_flow_source": "tick_reconstruction",
            "stale_data_seconds": stale_data_seconds,
        }

    quote_payload, quote_source, stale_data_seconds = _build_quote_from_snapshot(
        current_rows,
        current_tick,
        quote_override=quote_override,
        tick_size=tick_size,
        snapshot_mode=snapshot_mode,
    )
    return {
        "quote": quote_payload,
        "depth": _build_depth_from_quote(quote_payload, tick_size=tick_size),
        "trades": _infer_trade_prints(current_rows),
        "quote_history": [
            {
                "timestamp": quote_payload["timestamp"],
                "bid": quote_payload["bid"],
                "ask": quote_payload["ask"],
                "bid_size": quote_payload["bid_size"],
                "ask_size": quote_payload["ask_size"],
                "last_price": quote_payload["last_price"],
            }
        ],
        "quote_source": quote_source,
        "order_flow_source": "bar_inference",
        "stale_data_seconds": stale_data_seconds,
    }


async def _fetch_recent_tick_rows(
    symbol: str,
    *,
    snapshot_end: datetime,
) -> list[dict[str, Any]]:
    lookback_minutes = int(DEFAULT_CONFIG.get("order_flow", {}).get("tick_lookback_minutes", 20))
    from_time = max(
        snapshot_end - timedelta(minutes=max(lookback_minutes, 1)),
        datetime.combine(snapshot_end.astimezone(IST).date(), SESSION_OPEN, tzinfo=IST).astimezone(timezone.utc),
    )
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT time, ltp, bid, ask, bid_qty, ask_qty, volume, oi
                FROM market_ticks
                WHERE symbol = :symbol
                  AND time >= :from_time
                  AND time <= :snapshot_end
                ORDER BY time ASC
                LIMIT 600
                """
            ),
            {
                "symbol": symbol,
                "from_time": from_time,
                "snapshot_end": snapshot_end,
            },
        )
        rows = result.mappings().all()

    return [
        {
            "timestamp": _row_time_from_value(row["time"]).astimezone(timezone.utc),
            "ltp": float(row["ltp"] or 0.0),
            "bid": float(row["bid"] or 0.0),
            "ask": float(row["ask"] or 0.0),
            "bid_qty": float(row["bid_qty"] or 0.0),
            "ask_qty": float(row["ask_qty"] or 0.0),
            "volume": float(row["volume"] or 0.0),
            "oi": float(row["oi"] or 0.0),
        }
        for row in rows
    ]


def _append_live_tick_row(
    rows: list[dict[str, Any]],
    tick: Tick,
) -> list[dict[str, Any]]:
    timestamp = tick.timestamp or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = timestamp.astimezone(timezone.utc)

    if rows and rows[-1]["timestamp"] >= timestamp:
        return rows

    return rows + [
        {
            "timestamp": timestamp,
            "ltp": float(tick.ltp or 0.0),
            "bid": float(tick.bid or 0.0),
            "ask": float(tick.ask or 0.0),
            "bid_qty": float(tick.bid_qty or 0.0),
            "ask_qty": float(tick.ask_qty or 0.0),
            "volume": float(tick.volume or 0.0),
            "oi": float(tick.oi or 0.0),
        }
    ]


def _build_quote_history_from_ticks(
    rows: list[dict[str, Any]],
    *,
    tick_size: float,
) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for row in rows:
        ltp = float(row.get("ltp") or 0.0)
        if ltp <= 0:
            continue
        bid = float(row.get("bid") or 0.0)
        ask = float(row.get("ask") or 0.0)
        if bid <= 0 and ask <= 0:
            bid = round(ltp - (tick_size / 2.0), 4)
            ask = round(ltp + (tick_size / 2.0), 4)
        elif bid <= 0:
            bid = round(ask - max(tick_size, 0.01), 4)
        elif ask <= 0:
            ask = round(bid + max(tick_size, 0.01), 4)
        history.append(
            {
                "timestamp": _row_time_from_value(row["timestamp"]).astimezone(timezone.utc).isoformat(),
                "bid": bid,
                "ask": ask,
                "bid_size": max(float(row.get("bid_qty") or 0.0), 1.0),
                "ask_size": max(float(row.get("ask_qty") or 0.0), 1.0),
                "last_price": ltp,
            }
        )
    return history


def _build_depth_from_tick_history(
    quote_history: list[dict[str, Any]],
    *,
    tick_size: float,
) -> dict[str, Any]:
    latest = quote_history[-1]
    bid = float(latest["bid"])
    ask = float(latest["ask"])
    bid_size = max(float(latest["bid_size"]), 1.0)
    ask_size = max(float(latest["ask_size"]), 1.0)
    avg_bid_size = sum(float(item["bid_size"]) for item in quote_history[-12:]) / max(len(quote_history[-12:]), 1)
    avg_ask_size = sum(float(item["ask_size"]) for item in quote_history[-12:]) / max(len(quote_history[-12:]), 1)
    bid_anchor = max(bid_size, avg_bid_size)
    ask_anchor = max(ask_size, avg_ask_size)
    decay = (1.0, 0.72, 0.51)
    return {
        "timestamp": str(latest["timestamp"]),
        "bids": [
            {
                "price": round(bid - (tick_size * level), 4),
                "quantity": round(max(bid_anchor * factor, 1.0), 2),
            }
            for level, factor in enumerate(decay)
        ],
        "asks": [
            {
                "price": round(ask + (tick_size * level), 4),
                "quantity": round(max(ask_anchor * factor, 1.0), 2),
            }
            for level, factor in enumerate(decay)
        ],
    }


def _build_trade_prints_from_ticks(
    rows: list[dict[str, Any]],
    *,
    tick_size: float,
) -> list[dict[str, Any]]:
    if len(rows) < 2:
        return []

    prints: list[dict[str, Any]] = []
    prev = rows[0]
    for row in rows[1:]:
        price = float(row.get("ltp") or 0.0)
        if price <= 0:
            prev = row
            continue
        prev_price = float(prev.get("ltp") or 0.0)
        volume_delta = max(float(row.get("volume") or 0.0) - float(prev.get("volume") or 0.0), 0.0)
        side = _classify_trade_side(row, prev, tick_size=tick_size)
        if volume_delta <= 0 and side == "unknown":
            prev = row
            continue
        quantity = volume_delta if volume_delta > 0 else 1.0
        if quantity <= 0 and price != prev_price:
            quantity = 1.0
        prints.append(
            {
                "timestamp": _row_time_from_value(row["timestamp"]).astimezone(timezone.utc).isoformat(),
                "price": price,
                "quantity": round(quantity, 2),
                "aggressor_side": side,
            }
        )
        prev = row

    return prints[-120:]


def _classify_trade_side(
    row: dict[str, Any],
    prev: dict[str, Any],
    *,
    tick_size: float,
) -> str:
    price = float(row.get("ltp") or 0.0)
    prev_price = float(prev.get("ltp") or 0.0)
    prev_bid = float(prev.get("bid") or 0.0)
    prev_ask = float(prev.get("ask") or 0.0)
    tolerance = max(tick_size / 2.0, 0.01)

    if prev_ask > 0 and price >= (prev_ask - tolerance):
        return "buy"
    if prev_bid > 0 and price <= (prev_bid + tolerance):
        return "sell"
    if price > prev_price:
        return "buy"
    if price < prev_price:
        return "sell"
    if float(row.get("bid") or 0.0) > prev_bid:
        return "buy"
    if float(row.get("ask") or 0.0) < prev_ask:
        return "sell"
    return "unknown"


def _build_quote_from_snapshot(
    rows: list[dict[str, Any]],
    tick: Optional[Tick],
    *,
    quote_override: dict[str, Any] | None = None,
    tick_size: float,
    snapshot_mode: str,
) -> tuple[dict[str, Any], str, float]:
    latest_row = rows[-1]
    latest_close = float(latest_row["close"])
    latest_time = _row_time(latest_row)

    if quote_override and snapshot_mode == "live_session":
        bid = float(quote_override.get("bid") or 0.0)
        ask = float(quote_override.get("ask") or 0.0)
        last_price = float(quote_override.get("last_price") or 0.0)
        if bid > 0 and ask > 0 and last_price > 0:
            quote_timestamp = latest_time
            raw_timestamp = quote_override.get("timestamp")
            if raw_timestamp:
                try:
                    quote_timestamp = datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
                    if quote_timestamp.tzinfo is None:
                        quote_timestamp = quote_timestamp.replace(tzinfo=timezone.utc)
                except Exception:
                    quote_timestamp = latest_time
            stale_seconds = max(
                0.0,
                (datetime.now(timezone.utc) - quote_timestamp.astimezone(timezone.utc)).total_seconds(),
            )
            return (
                {
                    "timestamp": quote_timestamp.isoformat(),
                    "bid": bid,
                    "ask": ask,
                    "bid_size": float(quote_override.get("bid_size") or 120.0),
                    "ask_size": float(quote_override.get("ask_size") or 120.0),
                    "last_price": last_price,
                },
                "rest_quote",
                stale_seconds,
            )

    if (
        tick
        and tick.ltp > 0
        and tick.bid > 0
        and tick.ask > 0
        and tick.timestamp is not None
        and snapshot_mode == "live_session"
    ):
        stale_seconds = max(0.0, (datetime.now(timezone.utc) - tick.timestamp.replace(tzinfo=timezone.utc)).total_seconds())
        bid_size = float(tick.bid_qty or 120.0)
        ask_size = float(tick.ask_qty or 120.0)
        return (
            {
                "timestamp": tick.timestamp.isoformat(),
                "bid": float(tick.bid),
                "ask": float(tick.ask),
                "bid_size": bid_size,
                "ask_size": ask_size,
                "last_price": float(tick.ltp),
            },
            "websocket_tick",
            stale_seconds,
        )

    latest_open = float(latest_row["open"])
    base_size = max(float(latest_row.get("volume", 0.0) or 0.0) / 12.0, 60.0)
    bid_bias = 1.15 if latest_close >= latest_open else 0.85
    ask_bias = 0.85 if latest_close >= latest_open else 1.15
    return (
        {
            "timestamp": latest_time.isoformat(),
            "bid": round(latest_close - tick_size, 2),
            "ask": round(latest_close + tick_size, 2),
            "bid_size": round(base_size * bid_bias, 2),
            "ask_size": round(base_size * ask_bias, 2),
            "last_price": latest_close,
        },
        "historical_bar_inference",
        0.0,
    )


def _build_depth_from_quote(quote: dict[str, Any], *, tick_size: float) -> dict[str, Any]:
    bid = float(quote["bid"])
    ask = float(quote["ask"])
    bid_size = float(quote["bid_size"])
    ask_size = float(quote["ask_size"])
    return {
        "timestamp": str(quote["timestamp"]),
        "bids": [
            {"price": round(bid - (tick_size * level), 2), "quantity": round(bid_size * (1 - (0.18 * level)), 2)}
            for level in range(3)
        ],
        "asks": [
            {"price": round(ask + (tick_size * level), 2), "quantity": round(ask_size * (1 - (0.18 * level)), 2)}
            for level in range(3)
        ],
    }


def _infer_trade_prints(rows: list[dict[str, Any]], lookback: int = 24) -> list[dict[str, Any]]:
    prints: list[dict[str, Any]] = []
    for row in rows[-lookback:]:
        open_price = float(row["open"])
        close_price = float(row["close"])
        quantity = max(float(row.get("volume", 0.0) or 0.0) / 10.0, 1.0)
        side = "unknown"
        if close_price > open_price:
            side = "buy"
        elif close_price < open_price:
            side = "sell"
        prints.append(
            {
                "timestamp": str(row["time"]),
                "price": close_price,
                "quantity": round(quantity, 2),
                "aggressor_side": side,
            }
        )
    return prints


def _margin_fraction_for_symbol(symbol: str) -> float:
    normalized_symbol = str(symbol or "").upper().replace(" INDEX", "").replace(" FUT", "").strip()
    return float(CONTRACT_SPECS.get(normalized_symbol, {}).get("margin_fraction_per_lot", 1.0))


def _normalize_portfolio_symbol(raw_symbol: str, instrument_type: str | None = None) -> str:
    symbol_upper = str(raw_symbol or "").upper()
    instrument_upper = str(instrument_type or "").upper()
    for symbol_code in sorted(SYMBOL_MAP.keys(), key=len, reverse=True):
        if symbol_code not in symbol_upper:
            continue
        if "FUT" in symbol_upper or "FUT" in instrument_upper:
            return f"{symbol_code} FUT"
        if "INDEX" in symbol_upper:
            return f"{symbol_code} INDEX"
        return symbol_code
    return symbol_upper.strip() or str(raw_symbol or "")


def _position_exposure_ratio(position: Any, net_liquidation: float) -> float:
    quantity = abs(float(getattr(position, "qty", 0) or 0.0))
    mark_price = abs(float(getattr(position, "ltp", 0.0) or getattr(position, "avg_price", 0.0) or 0.0))
    if quantity <= 0 or mark_price <= 0:
        return 0.0
    normalized_symbol = _normalize_portfolio_symbol(
        getattr(position, "symbol", ""),
        getattr(position, "instrument_type", None),
    )
    notional = quantity * mark_price
    return round((notional * _margin_fraction_for_symbol(normalized_symbol)) / max(net_liquidation, 1.0), 4)


async def _load_portfolio_snapshot(session_symbol: str) -> dict[str, Any]:
    adapter = get_active_adapter("fyers") or get_active_adapter("upstox") or get_active_adapter()
    if adapter is None:
        return PortfolioSnapshot(symbol_exposure={session_symbol: 0.0}).__dict__

    total_balance = 1_000_000.0
    daily_realized_pnl = 0.0
    positions = []

    try:
        funds = await adapter.get_funds()
        total_balance = float(funds.total_balance or funds.available_cash or total_balance)
        daily_realized_pnl = float(getattr(funds, "realized_pnl", 0.0) or 0.0)
    except Exception:
        pass

    try:
        positions = await adapter.get_positions()
    except Exception:
        positions = []

    symbol_exposure: dict[str, float] = {session_symbol: 0.0}
    total_exposure_ratio = 0.0
    open_positions = 0
    for position in positions:
        quantity = int(getattr(position, "qty", 0) or 0)
        if quantity == 0:
            continue
        open_positions += 1
        normalized_symbol = _normalize_portfolio_symbol(
            getattr(position, "symbol", ""),
            getattr(position, "instrument_type", None),
        )
        exposure_ratio = _position_exposure_ratio(position, total_balance)
        if exposure_ratio <= 0:
            continue
        symbol_exposure[normalized_symbol] = round(symbol_exposure.get(normalized_symbol, 0.0) + exposure_ratio, 4)
        total_exposure_ratio += exposure_ratio

    return {
        "net_liquidation": round(total_balance, 2),
        "daily_realized_pnl": round(daily_realized_pnl, 2),
        "open_positions": open_positions,
        "symbol_exposure": symbol_exposure,
        "agent_drawdowns": {"positional": 0.0, "swing": 0.0, "scalp": 0.0},
        "correlated_exposure": round(total_exposure_ratio, 4),
    }


def _row_time(row: dict[str, Any]) -> datetime:
    return _row_time_from_value(row.get("time") or row.get("timestamp") or "").astimezone(IST)


def _row_time_from_value(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_bar(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": datetime.fromisoformat(str(item["timestamp"])),
        "open": item["open"],
        "high": item["high"],
        "low": item["low"],
        "close": item["close"],
        "volume": item.get("volume", 0.0),
    }


def _parse_quote(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": datetime.fromisoformat(str(item["timestamp"])),
        "bid": item["bid"],
        "ask": item["ask"],
        "bid_size": item.get("bid_size", 0.0),
        "ask_size": item.get("ask_size", 0.0),
    }


def _parse_trade(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": datetime.fromisoformat(str(item["timestamp"])),
        "price": item["price"],
        "quantity": item["quantity"],
        "aggressor_side": item.get("aggressor_side", "unknown"),
    }
