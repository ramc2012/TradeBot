"""Paper runtime for the commodity desk.

Strategy split on this desk:
- Strategy 1: MCX options, 30-minute MACD zero-cross on liquid CE/PE contracts
- Strategy 2: MCX futures, 15-minute MACD zero-cross with Market Profile confirmation
"""
from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from agentic_rag.audit_agent import record_audit_event
from analysis.indicators_agent import IndicatorContext, indicators_agent
from analysis.macd_engine import compute_ema
from analysis.signal_classifier import classify_signal_bucket
from analytics.technicals import compute_rsi, latest_macd_rsi
from api.routers.auth import (
    ensure_fyers_session,
    ensure_upstox_session,
    get_active_adapter,
    get_fyers_token_health,
    get_upstox_token_health,
)
from auction_intelligence.market_profile.engine import MarketProfileEngine
from auction_intelligence.schemas import MarketBar
from brokers.base import BrokerAdapter
from core.config import settings
from core.runtime_state import load_runtime_state, save_runtime_state
from market_data.commodity_atm_watchlist import commodity_atm_watchlist_service
from market_data.commodity_contract_specs import (
    extract_commodity_root,
    get_commodity_contract_spec,
    get_commodity_display_name,
)
from market_data.option_history import option_history_service
from market_data.upstox_commodity import (
    load_upstox_mcx_quote_snapshots,
    load_upstox_mcx_quotes,
    resolve_upstox_mcx_future,
)
from paper_engine.base_strategy_agent import (
    BaseStrategyAgent,
    IST,
    _deserialize_trade_history,
    _latest_session_rows,
    _now_ist,
    _parse_iso_timestamp,
    _round_or_none,
    _serialize_trade_history,
)
from paper_engine.order_book import PaperOrder, PaperOrderBook
from paper_engine.portfolio import PaperPortfolio, VirtualPosition

DEFAULT_COMMODITY_SCAN_INTERVAL_SECONDS = 30
DEFAULT_COMMODITY_HISTORY_DAYS = 21
DEFAULT_COMMODITY_ATR_PERIOD = 14
DEFAULT_COMMODITY_LOTS_PER_TRADE = 1
DEFAULT_COMMODITY_MARGIN_PCT = 0.15
DEFAULT_COMMODITY_REPORTS_MAX = 40
DEFAULT_COMMODITY_ORDERS_MAX = 80
DEFAULT_COMMODITY_COMMENTARY_MAX = 80
DEFAULT_COMMODITY_SIGNAL_AUDIT_MAX = 120
DEFAULT_COMMODITY_INITIAL_CAPITAL = 1_000_000.0
DEFAULT_COMMODITY_ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "runtime" / "commodity_archive"
DEFAULT_COMMODITY_SCAN_TIMEOUT_SECONDS = 120

FUTURES_MACD_FAST = 12
FUTURES_MACD_SLOW = 26
FUTURES_MACD_SIGNAL = 9
FUTURES_MACD_MIN_BARS = 35
FUTURES_TIMEFRAME = "15minute"
FUTURES_MAX_POSITIONS = 3
FUTURES_MP_MIN_PERIODS = 8
FUTURES_MIN_HOLD_BARS = 4
FUTURES_CONTINUATION_LOOKBACK_BARS = 12
FUTURES_CONTINUATION_BREAKOUT_LOOKBACK = 4
FUTURES_TRAIL_ATR_MULTIPLIER = 1.25
FUTURES_BREAK_EVEN_R_MULTIPLIER = 1.0
# NEW: intermediate stage between BE (1R) and full target arm (2R).
# At +1.5R, lock half-R of profit (stop moves to entry ± 0.5R). Rescues
# trades that retrace from +1.5R back to BE — currently a common loss mode.
# The 21 May NATURALGAS SELL @ 290.05 → exit @ 275.14 trail_stop (+18,647)
# is the pattern we're trying to make more frequent.
FUTURES_PARTIAL_LOCK_R_MULTIPLIER = 1.5
FUTURES_TARGET_ARM_R_MULTIPLIER = 2.0
FUTURES_MIN_STOP_PCT = 0.005
COMMODITY_DAILY_LOSS_LIMIT = 25_000.0
COMMODITY_UNDERLYING_DAILY_LOSS_LIMIT = 15_000.0
COMMODITY_STOP_COOLDOWN_MINUTES = 60
COMMODITY_EVENT_BLOCK_MINUTES = 90
COMMODITY_MAX_DRAWDOWN_PCT = 15.0

OPTIONS_MACD_FAST = 12
OPTIONS_MACD_SLOW = 26
OPTIONS_MACD_SIGNAL = 9
OPTIONS_MACD_MIN_BARS = 35
OPTIONS_TIMEFRAME = "30minute"
OPTIONS_HARD_STOP_PCT = 25.0
OPTIONS_TARGET_PCT = 50.0
OPTIONS_RUNNER_ARM_PCT = 100.0
OPTIONS_RUNNER_TRAIL_PCT = 20.0
OPTIONS_RUNNER_MACD_EXIT_PROFIT_PCT = 30.0
OPTIONS_CAPITAL_FRACTION = 0.05
OPTIONS_MAX_POSITIONS = 2
OPTIONS_MIN_TTE_DAYS = 5
OPTIONS_IV_HALF_SIZE_PCT = 40.0
OPTIONS_IV_REJECT_PCT = 55.0


def _resolve_commodity_config_file() -> Path:
    env_path = os.environ.get("COMMODITY_CONFIG_FILE", "").strip()
    if env_path:
        return Path(env_path)
    docker_path = Path("/app/commodity_strategy.json")
    if docker_path.parent.is_dir():
        return docker_path
    return Path(__file__).resolve().parent.parent / "commodity_strategy.json"


_COMMODITY_CONFIG_FILE = _resolve_commodity_config_file()
_COMMODITY_STATE_DB_KEY = "commodity_strategy_state"


def _canonicalize_symbol(raw_symbol: str) -> str:
    symbol = str(raw_symbol or "").strip().upper()
    if symbol.startswith("MCX:SILVERMIC") and symbol.endswith("FUT"):
        return symbol.replace("MCX:SILVERMIC", "MCX:SILVERM", 1)
    return symbol


def _in_commodity_hours(now: Optional[datetime] = None) -> bool:
    current = now or _now_ist()
    if current.weekday() >= 5:
        return False
    return time(9, 0) <= current.time() <= time(23, 30)


def _normalize_iv_pct(value: Any) -> Optional[float]:
    try:
        iv_pct = float(value)
    except (TypeError, ValueError):
        return None
    if iv_pct <= 0:
        return None
    if iv_pct <= 1:
        iv_pct *= 100.0
    return round(iv_pct, 2)


def _normalized_option_budget_cap(initial_capital: float, available_capital: float) -> float:
    safe_initial = max(float(initial_capital or 0.0), 0.0)
    safe_available = max(float(available_capital or 0.0), 0.0)
    budget_base = min(safe_initial, safe_available)
    return round(budget_base * OPTIONS_CAPITAL_FRACTION, 2)


def _is_within_minutes(current_time: time, event_time: time, minutes: int) -> bool:
    current_minutes = current_time.hour * 60 + current_time.minute
    event_minutes = event_time.hour * 60 + event_time.minute
    return abs(current_minutes - event_minutes) <= minutes


def _commodity_event_block_reason(symbol_or_underlying: str, now: Optional[datetime] = None) -> Optional[str]:
    current = (now or _now_ist()).astimezone(IST)
    underlying = extract_commodity_root(str(symbol_or_underlying or ""))
    if underlying == "CRUDEOIL" and current.weekday() == 2 and _is_within_minutes(
        current.time(),
        time(20, 30),
        COMMODITY_EVENT_BLOCK_MINUTES,
    ):
        return "scheduled_crude_inventory_window"
    if underlying == "NATURALGAS" and current.weekday() == 3 and _is_within_minutes(
        current.time(),
        time(20, 30),
        COMMODITY_EVENT_BLOCK_MINUTES,
    ):
        return "scheduled_ng_storage_window"
    return None


def _symbol_matches_underlying(symbol: str, underlying: str) -> bool:
    normalized_underlying = extract_commodity_root(str(underlying or ""))
    if not normalized_underlying:
        return False
    normalized_symbol = str(symbol or "").upper()
    extracted = extract_commodity_root(normalized_symbol)
    return extracted == normalized_underlying or normalized_underlying in normalized_symbol


def _parse_datetime(value: Any) -> Optional[datetime]:
    return _parse_iso_timestamp(value)


def _order_fill_time_ist(order: PaperOrder) -> datetime:
    fill_time = order.fill_time or datetime.now(timezone.utc)
    if fill_time.tzinfo is None:
        fill_time = fill_time.replace(tzinfo=timezone.utc)
    return fill_time.astimezone(IST)


def _repair_portfolio_ledger(portfolio: PaperPortfolio) -> None:
    """Normalize commodity ledger timestamps and rebuild cash from realized P&L."""
    repaired_history = []
    for trade in getattr(portfolio, "_trade_history", []):
        entry_time = trade.entry_time
        exit_time = trade.exit_time
        if entry_time.tzinfo is None:
            entry_time = entry_time.replace(tzinfo=IST)
        else:
            entry_time = entry_time.astimezone(IST)
        if exit_time.tzinfo is None:
            exit_time = exit_time.replace(tzinfo=IST)
        else:
            exit_time = exit_time.astimezone(IST)

        trade.entry_time = entry_time
        trade.exit_time = exit_time
        repaired_history.append(trade)

    repaired_history.sort(key=lambda item: item.exit_time)
    portfolio._trade_history = repaired_history
    portfolio._daily_pnl = defaultdict(float)
    for trade in repaired_history:
        portfolio._daily_pnl[trade.exit_time.date()] += float(trade.pnl)
    portfolio.reconcile_available_capital()


def _is_rate_limit_error(exc: Exception | str) -> bool:
    text = str(exc).lower()
    return "429" in text or "limit reached" in text or "too many requests" in text


def _normalize_symbols(symbols: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = _canonicalize_symbol(raw)
        if not symbol or ":" not in symbol:
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        cleaned.append(symbol)
    return cleaned


def _normalize_selected_option_expiries(
    symbols: list[str],
    selected_option_expiries: Optional[dict[str, Any]],
) -> dict[str, str]:
    allowed = set(_normalize_symbols(symbols))
    normalized: dict[str, str] = {}
    for raw_symbol, raw_expiry in dict(selected_option_expiries or {}).items():
        symbol = _canonicalize_symbol(raw_symbol)
        expiry = str(raw_expiry or "").strip()
        if symbol not in allowed or not expiry:
            continue
        try:
            date.fromisoformat(expiry)
        except ValueError:
            continue
        normalized[symbol] = expiry
    return normalized


def _normalize_selected_option_lookup_symbols(
    symbols: list[str],
    selected_option_lookup_symbols: Optional[dict[str, Any]],
    *,
    selected_option_expiries: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    allowed = set(_normalize_symbols(symbols))
    active_expiries = dict(selected_option_expiries or {})
    normalized: dict[str, str] = {}
    for raw_symbol, raw_lookup_symbol in dict(selected_option_lookup_symbols or {}).items():
        symbol = _canonicalize_symbol(raw_symbol)
        lookup_symbol = _canonicalize_symbol(raw_lookup_symbol)
        if symbol not in allowed or symbol not in active_expiries or not lookup_symbol:
            continue
        normalized[symbol] = lookup_symbol
    return normalized


def _default_saved_state() -> dict[str, Any]:
    return {
        "config": {
            "symbols": [],
            "selected_option_expiries": {},
            "selected_option_lookup_symbols": {},
            "lots_per_trade": DEFAULT_COMMODITY_LOTS_PER_TRADE,
        },
        "control": {
            "kill_switch_active": False,
            "start_required": False,
            "manual_restart_required": False,
            "last_run_at": None,
            "last_error": None,
            "last_message": None,
        },
        "runtime": {
            "watchlist": [],
            "futures_watchlist": [],
            "option_watchlist": [],
            "positions": [],
            "orders": [],
            "reports": [],
            "commentary": [],
            "processed_signals": {},
            "signal_audit": [],
            "portfolio": {
                "initial_capital": DEFAULT_COMMODITY_INITIAL_CAPITAL,
                "available_capital": DEFAULT_COMMODITY_INITIAL_CAPITAL,
                "trade_history": [],
                "daily_pnl": {},
                "equity_curve": [],
                "peak_equity": DEFAULT_COMMODITY_INITIAL_CAPITAL,
            },
        },
    }



def _normalize_saved_state(payload: Optional[dict[str, Any]]) -> dict[str, Any]:
    default_state = _default_saved_state()
    raw_payload = payload or {}
    if "config" in raw_payload or "runtime" in raw_payload or "control" in raw_payload:
        config_payload = raw_payload.get("config") or {}
        control_payload = raw_payload.get("control") or {}
        runtime_payload = raw_payload.get("runtime") or {}
    else:
        config_payload = raw_payload
        control_payload = {}
        runtime_payload = {}

    symbols = _normalize_symbols(list(config_payload.get("symbols") or []))
    selected_option_expiries = _normalize_selected_option_expiries(
        symbols,
        config_payload.get("selected_option_expiries"),
    )
    selected_option_lookup_symbols = _normalize_selected_option_lookup_symbols(
        symbols,
        config_payload.get("selected_option_lookup_symbols"),
        selected_option_expiries=selected_option_expiries,
    )

    default_state["config"] = {
        "symbols": symbols,
        "selected_option_expiries": selected_option_expiries,
        "selected_option_lookup_symbols": selected_option_lookup_symbols,
        "lots_per_trade": max(1, int(config_payload.get("lots_per_trade") or DEFAULT_COMMODITY_LOTS_PER_TRADE)),
    }
    kill_switch_active = bool(control_payload.get("kill_switch_active", False))
    manual_restart_required = bool(control_payload.get("manual_restart_required", kill_switch_active))
    default_state["control"] = {
        "kill_switch_active": kill_switch_active,
        "start_required": bool(kill_switch_active or manual_restart_required),
        "manual_restart_required": manual_restart_required,
        "last_run_at": control_payload.get("last_run_at"),
        "last_error": control_payload.get("last_error"),
        "last_message": control_payload.get("last_message"),
    }
    runtime_state = default_state["runtime"]
    runtime_state["watchlist"] = [
        row for row in list(runtime_payload.get("watchlist") or []) if isinstance(row, dict)
    ]
    runtime_state["futures_watchlist"] = [
        row for row in list(runtime_payload.get("futures_watchlist") or runtime_payload.get("watchlist") or []) if isinstance(row, dict)
    ]
    runtime_state["option_watchlist"] = [
        row for row in list(runtime_payload.get("option_watchlist") or []) if isinstance(row, dict)
    ]
    runtime_state["positions"] = [
        row for row in list(runtime_payload.get("positions") or []) if isinstance(row, dict)
    ]
    runtime_state["orders"] = [
        row for row in list(runtime_payload.get("orders") or []) if isinstance(row, dict)
    ]
    runtime_state["reports"] = [
        row for row in list(runtime_payload.get("reports") or []) if isinstance(row, dict)
    ]
    runtime_state["commentary"] = [
        row for row in list(runtime_payload.get("commentary") or []) if isinstance(row, dict)
    ]
    runtime_state["processed_signals"] = {
        str(key): str(bar_time)
        for key, bar_time in dict(runtime_payload.get("processed_signals") or {}).items()
        if str(key or "").strip() and str(bar_time or "").strip()
    }
    runtime_state["signal_audit"] = [
        row for row in list(runtime_payload.get("signal_audit") or []) if isinstance(row, dict)
    ]
    portfolio_payload = runtime_payload.get("portfolio") or {}
    runtime_state["portfolio"] = {
        "initial_capital": float(portfolio_payload.get("initial_capital") or DEFAULT_COMMODITY_INITIAL_CAPITAL),
        "available_capital": float(portfolio_payload.get("available_capital") or DEFAULT_COMMODITY_INITIAL_CAPITAL),
        "trade_history": [
            row for row in list(portfolio_payload.get("trade_history") or []) if isinstance(row, dict)
        ],
        "daily_pnl": {
            str(day): float(value or 0.0)
            for day, value in dict(portfolio_payload.get("daily_pnl") or {}).items()
            if str(day or "").strip()
        },
        "equity_curve": [
            row for row in list(portfolio_payload.get("equity_curve") or []) if isinstance(row, dict)
        ],
        "peak_equity": float(portfolio_payload.get("peak_equity") or DEFAULT_COMMODITY_INITIAL_CAPITAL),
    }
    return default_state


def _load_saved_state_from_disk() -> Optional[dict[str, Any]]:
    if not _COMMODITY_CONFIG_FILE.exists():
        return None
    try:
        return json.loads(_COMMODITY_CONFIG_FILE.read_text())
    except Exception as exc:
        logger.warning(f"[CommodityStrategy] Failed to load {_COMMODITY_CONFIG_FILE}: {exc}")
        return None


def _load_saved_state_from_database() -> tuple[Optional[dict[str, Any]], Optional[datetime]]:
    payload, updated_at = load_runtime_state(_COMMODITY_STATE_DB_KEY)
    if not isinstance(payload, dict):
        return None, updated_at
    return _normalize_saved_state(payload), updated_at


def _load_saved_state() -> tuple[dict[str, Any], Optional[datetime]]:
    database_state, updated_at = _load_saved_state_from_database()
    if database_state is not None:
        return database_state, updated_at

    disk_payload = _load_saved_state_from_disk()
    normalized = _normalize_saved_state(disk_payload)
    if disk_payload is not None:
        migrated_at = save_runtime_state(_COMMODITY_STATE_DB_KEY, normalized)
        return normalized, migrated_at or updated_at
    return normalized, updated_at


def _save_state(state: dict[str, Any]) -> Optional[datetime]:
    try:
        _COMMODITY_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _COMMODITY_CONFIG_FILE.write_text(json.dumps(state, indent=2))
    except Exception as exc:
        logger.warning(f"[CommodityStrategy] Failed to persist {_COMMODITY_CONFIG_FILE}: {exc}")
    return save_runtime_state(_COMMODITY_STATE_DB_KEY, state)


def _compute_atr(candles: list[dict[str, Any]], period: int) -> list[Optional[float]]:
    if period <= 0:
        raise ValueError("ATR period must be positive")
    tr_values: list[float] = []
    previous_close: Optional[float] = None
    for candle in candles:
        high = float(candle.get("high") or candle.get("close") or 0.0)
        low = float(candle.get("low") or candle.get("close") or 0.0)
        close = float(candle.get("close") or 0.0)
        if previous_close is None:
            true_range = max(high - low, 0.0)
        else:
            true_range = max(high - low, abs(high - previous_close), abs(low - previous_close))
        tr_values.append(true_range)
        previous_close = close

    return compute_ema(tr_values, period)


def _interval_minutes(interval: str) -> int:
    mapping = {
        "1minute": 1,
        "3minute": 3,
        "5minute": 5,
        "15minute": 15,
        "30minute": 30,
        "1day": 1440,
    }
    return int(mapping.get(str(interval or "15minute"), 15))


def _filter_closed_interval_rows(
    candles: list[dict[str, Any]],
    *,
    interval: str,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    if not candles:
        return []
    current = now or _now_ist()
    interval_minutes = _interval_minutes(interval)
    filtered = list(candles)
    while filtered:
        last_time = _parse_iso_timestamp(filtered[-1].get("time"))
        if last_time is None:
            break
        if last_time + timedelta(minutes=interval_minutes) <= current:
            break
        filtered.pop()
    return filtered


def _bars_between(
    start_time: Optional[str],
    end_time: Optional[str],
    *,
    interval: str,
) -> int:
    start_dt = _parse_iso_timestamp(start_time)
    end_dt = _parse_iso_timestamp(end_time)
    if start_dt is None or end_dt is None or end_dt <= start_dt:
        return 0
    interval_minutes = _interval_minutes(interval)
    elapsed_minutes = int((end_dt - start_dt).total_seconds() // 60)
    return max(0, elapsed_minutes // max(interval_minutes, 1))


def _recent_zero_cross(macd_line: list[Optional[float]], *, lookback_bars: int) -> tuple[Optional[str], Optional[int]]:
    if len(macd_line) < 2:
        return None, None
    latest_index = len(macd_line) - 1
    min_index = max(1, latest_index - max(lookback_bars, 1))
    for index in range(latest_index, min_index - 1, -1):
        current_macd = macd_line[index]
        previous_macd = macd_line[index - 1]
        if current_macd is None or previous_macd is None:
            continue
        if previous_macd <= 0 < current_macd:
            return "BUY", latest_index - index
        if previous_macd >= 0 > current_macd:
            return "SELL", latest_index - index
    return None, None


def _indicator_context(
    *,
    symbol: Optional[str],
    timeframe: str,
    candles: list[dict[str, Any]],
    closes: list[float],
) -> IndicatorContext:
    last_bar_time = str(candles[-1].get("time") or "") if candles else None
    if symbol:
        cache_symbol = str(symbol)
    else:
        first_close = closes[0] if closes else 0.0
        last_close = closes[-1] if closes else 0.0
        cache_symbol = f"commodity_signal:{len(candles)}:{first_close:.6f}:{last_close:.6f}"
    return IndicatorContext(symbol=cache_symbol, timeframe=timeframe, last_bar_time=last_bar_time)


def evaluate_commodity_signal(
    candles: list[dict[str, Any]],
    *,
    symbol: Optional[str] = None,
    timeframe: str = FUTURES_TIMEFRAME,
    fast: int = FUTURES_MACD_FAST,
    slow: int = FUTURES_MACD_SLOW,
    signal_period: int = FUTURES_MACD_SIGNAL,
) -> dict[str, Any]:
    required = max(FUTURES_MACD_MIN_BARS, slow + signal_period)
    if len(candles) < required:
        return {
            "signal": None,
            "reason": "insufficient_data",
            "regime": "unknown",
            "latest_close": None,
            "previous_close": None,
            "macd": None,
            "macd_signal": None,
            "macd_histogram": None,
            "rsi": None,
            "atr": None,
            "bar_time": None,
            "indicator_timeframe": timeframe,
        }

    closes = [float(candle.get("close") or 0.0) for candle in candles]
    ctx = _indicator_context(symbol=symbol, timeframe=timeframe, candles=candles, closes=closes)
    macd_result = indicators_agent.macd(ctx=ctx, closes=closes, fast=fast, slow=slow, signal=signal_period)
    macd_line = macd_result.macd
    signal_line = macd_result.signal
    histogram = macd_result.histogram
    latest_macd = macd_line[-1]
    previous_macd = macd_line[-2]
    latest_signal = signal_line[-1]
    latest_hist = histogram[-1]
    latest_close = closes[-1]
    previous_close = closes[-2]
    rsi_values = compute_rsi(closes)
    latest_rsi = rsi_values[-1] if rsi_values else None
    latest_atr = _compute_atr(candles, DEFAULT_COMMODITY_ATR_PERIOD)[-1]
    recent_cross_signal, recent_cross_bars_ago = _recent_zero_cross(
        macd_line,
        lookback_bars=FUTURES_CONTINUATION_LOOKBACK_BARS,
    )

    signal: Optional[str] = None
    reason = "no_cross"
    if latest_macd is not None and previous_macd is not None:
        if previous_macd <= 0 < latest_macd:
            signal = "BUY"
            reason = "macd_zero_cross_up"
        elif previous_macd >= 0 > latest_macd:
            signal = "SELL"
            reason = "macd_zero_cross_down"

    regime = "neutral"
    if latest_macd is not None:
        if latest_macd > 0:
            regime = "bullish"
        elif latest_macd < 0:
            regime = "bearish"

    continuation_signal: Optional[str] = None
    continuation_reason: Optional[str] = None
    recent_window = candles[-(FUTURES_CONTINUATION_BREAKOUT_LOOKBACK + 1):-1]
    recent_high = max(
        (float(item.get("high") or item.get("close") or 0.0) for item in recent_window),
        default=latest_close,
    )
    recent_low = min(
        (float(item.get("low") or item.get("close") or 0.0) for item in recent_window),
        default=latest_close,
    )
    if (
        signal is None
        and recent_cross_signal in {"BUY", "SELL"}
        and recent_cross_bars_ago is not None
        and 0 < recent_cross_bars_ago <= FUTURES_CONTINUATION_LOOKBACK_BARS
        and latest_macd is not None
        and latest_hist is not None
    ):
        if (
            recent_cross_signal == "BUY"
            and latest_macd > 0
            and latest_hist > 0
            and latest_close >= recent_high
        ):
            continuation_signal = "BUY"
            continuation_reason = "macd_continuation_breakout_up"
        elif (
            recent_cross_signal == "SELL"
            and latest_macd < 0
            and latest_hist < 0
            and latest_close <= recent_low
        ):
            continuation_signal = "SELL"
            continuation_reason = "macd_continuation_breakdown_down"

    prev_hist = histogram[-2] if len(histogram) >= 2 else None
    return {
        "signal": signal,
        "reason": reason,
        "regime": regime,
        "latest_close": latest_close,
        "previous_close": previous_close,
        "macd": _round_or_none(latest_macd, 4),
        "macd_signal": _round_or_none(latest_signal, 4),
        "macd_histogram": _round_or_none(latest_hist, 4),
        "rsi": _round_or_none(latest_rsi, 2),
        "prev_macd_histogram": _round_or_none(prev_hist, 4),
        "prev_macd": _round_or_none(previous_macd, 4),
        "atr": _round_or_none(latest_atr, 4),
        "bar_time": str(candles[-1].get("time") or ""),
        "indicator_timeframe": timeframe,
        "recent_cross_signal": recent_cross_signal,
        "recent_cross_bars_ago": recent_cross_bars_ago,
        "continuation_signal": continuation_signal,
        "continuation_reason": continuation_reason,
    }


# classify_signal_bucket is imported from analysis.signal_classifier so all
# strategy agents bucket their lane rows the same way.


def _data_quality_block_reason(symbol: str, source: str) -> Optional[str]:
    if not settings.DATA_QUALITY_SCAN_GATE_ENABLED:
        return None
    symbol = str(symbol or "").strip()
    if not symbol:
        return "Data quality gate blocked entry because no tradable symbol was available."
    fallback_sources = {
        "broker_futures_quote": ("broker_quote",),
        "broker_option_quote": ("broker_quote",),
    }
    try:
        from market_data.data_quality_agent import data_quality_agent

        verdict = data_quality_agent.assess_freshness(symbol=symbol, source=source)
        if verdict.stale and str(verdict.reason or "").startswith("No observation recorded"):
            for fallback_source in fallback_sources.get(source, ()):
                fallback = data_quality_agent.assess_freshness(symbol=symbol, source=fallback_source)
                if not fallback.stale:
                    return None
    except Exception as exc:
        return f"Data quality gate could not verify {symbol}: {exc}"
    if verdict.stale:
        return verdict.reason or f"Data quality gate blocked stale {source} for {symbol}."
    return None


@dataclass
class CommodityCommentaryEntry:
    time: str
    tone: str
    message: str


@dataclass
class CommodityReportSnapshot:
    time: str
    total_equity: float
    realized_pnl: float
    unrealized_pnl: float
    open_positions: int
    tracked_symbols: int
    last_message: str


@dataclass
class CommodityPositionState:
    position_key: str
    symbol: str
    live_symbol: str
    underlying: str
    strategy_key: str
    strategy_title: str
    instrument_type: str
    action: str
    qty: int
    lots: int
    lot_size: int
    entry_price: float
    current_price: float
    stop_price: float
    target_price: Optional[float]
    regime: str
    signal_reason: str
    atr: Optional[float]
    macd_value: Optional[float]
    mp_poc: Optional[float]
    mp_vah: Optional[float]
    mp_val: Optional[float]
    entered_at: str
    entry_bar_time: str
    contract_unit_label: str
    quote_unit_label: str
    display_name: str
    initial_qty: int
    peak_price: float
    target_reached: bool = False
    # True once price hits +1.5R and stop has been moved to lock +0.5R.
    # Idempotent — set once per position so the lock can't be re-armed.
    partial_lock_armed: bool = False
    expiry: Optional[str] = None
    strike: Optional[float] = None
    option_type: Optional[str] = None
    entry_iv_pct: Optional[float] = None
    entry_style: Optional[str] = None
    last_reviewed_bar_time: Optional[str] = None

    @property
    def unrealized_pnl(self) -> float:
        multiplier = 1 if self.action == "BUY" else -1
        return multiplier * (self.current_price - self.entry_price) * self.qty

    @property
    def return_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        multiplier = 1 if self.action == "BUY" else -1
        return multiplier * ((self.current_price - self.entry_price) / self.entry_price) * 100.0


@dataclass
class CommodityRuntime:
    portfolio: PaperPortfolio
    order_book: PaperOrderBook
    positions: dict[str, CommodityPositionState] = field(default_factory=dict)
    orders: list[dict[str, Any]] = field(default_factory=list)
    reports: list[CommodityReportSnapshot] = field(default_factory=list)
    futures_watchlist: list[dict[str, Any]] = field(default_factory=list)
    option_watchlist: list[dict[str, Any]] = field(default_factory=list)
    processed_signals: dict[str, str] = field(default_factory=dict)
    signal_audit: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class CommodityLaneDescriptor:
    key: str
    title: str
    timeframe: str
    instrument_scope: str
    execution_mode: str
    position_cap: int


class _BaseCommodityLaneAgent:
    descriptor: CommodityLaneDescriptor

    def __init__(self, owner: "CommodityStrategyAgent") -> None:
        self.owner = owner

    def build_status_payload(self) -> dict[str, Any]:
        return {
            "key": self.descriptor.key,
            "title": self.descriptor.title,
            "timeframe": self.descriptor.timeframe,
            "instrument_scope": self.descriptor.instrument_scope,
            "execution_mode": self.descriptor.execution_mode,
            "position_cap": self.descriptor.position_cap,
            "tracked_symbols": len(self.owner.get_symbols()),
            "open_positions": self.open_positions(),
            "ready_signals": self.ready_signals(),
        }

    def open_positions(self) -> int:
        raise NotImplementedError

    def ready_signals(self) -> int:
        raise NotImplementedError

    async def run_entries(self, rows: list[dict[str, Any]]) -> None:
        raise NotImplementedError


class _CommodityFuturesLaneAgent(_BaseCommodityLaneAgent):
    descriptor = CommodityLaneDescriptor(
        key="commodity_futures",
        title="Strategy 2 · Futures",
        timeframe=FUTURES_TIMEFRAME,
        instrument_scope="MCX futures",
        execution_mode="paper_execution",
        position_cap=FUTURES_MAX_POSITIONS,
    )

    def open_positions(self) -> int:
        return sum(1 for pos in self.owner._runtime.positions.values() if pos.strategy_key == self.descriptor.key)

    def ready_signals(self) -> int:
        return sum(1 for row in self.owner._runtime.futures_watchlist if row.get("signal_validation") == "ready")

    async def run_entries(self, rows: list[dict[str, Any]]) -> None:
        await self.owner._open_new_futures_positions(rows)


class _CommodityOptionsLaneAgent(_BaseCommodityLaneAgent):
    descriptor = CommodityLaneDescriptor(
        key="commodity_options",
        title="Strategy 1 · Options",
        timeframe=OPTIONS_TIMEFRAME,
        instrument_scope="MCX liquid CE / PE contracts",
        execution_mode="paper_execution",
        position_cap=OPTIONS_MAX_POSITIONS,
    )

    def open_positions(self) -> int:
        return sum(1 for pos in self.owner._runtime.positions.values() if pos.strategy_key == self.descriptor.key)

    def ready_signals(self) -> int:
        return sum(1 for row in self.owner._runtime.option_watchlist if row.get("signal_validation") == "ready")

    async def run_entries(self, rows: list[dict[str, Any]]) -> None:
        await self.owner._open_new_option_positions(rows)


class CommodityStrategyAgent(BaseStrategyAgent):
    scan_interval_seconds = DEFAULT_COMMODITY_SCAN_INTERVAL_SECONDS

    def __init__(self) -> None:
        saved_state, saved_updated_at = _load_saved_state()
        portfolio_state = saved_state["runtime"]["portfolio"]
        portfolio = PaperPortfolio(
            initial_capital=float(portfolio_state.get("initial_capital") or DEFAULT_COMMODITY_INITIAL_CAPITAL),
            session_id="commodity-strategy-paper",
        )
        self._runtime = CommodityRuntime(
            portfolio=portfolio,
            order_book=PaperOrderBook(on_fill=portfolio.on_fill),
        )
        self._lane_agents: list[_BaseCommodityLaneAgent] = [
            _CommodityFuturesLaneAgent(self),
            _CommodityOptionsLaneAgent(self),
        ]
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._enabled = True
        self._auto_run_enabled = True
        self._running = False
        self._last_data_health: dict[str, Any] = {}
        self._last_quote_snapshots: dict[str, dict[str, Any]] = {}
        self._commentary: list[CommodityCommentaryEntry] = []
        self._state_synced_at: Optional[datetime] = None
        self._fyers_ltp_backoff_until: Optional[datetime] = None
        self._apply_saved_state(saved_state)
        self._state_synced_at = saved_updated_at
        self._persist_state()

    def _apply_saved_state(self, saved_state: dict[str, Any]) -> None:
        saved_config = saved_state["config"]
        self._symbols = list(saved_config["symbols"])
        self._selected_option_expiries = dict(saved_config["selected_option_expiries"])
        self._selected_option_lookup_symbols = dict(saved_config.get("selected_option_lookup_symbols") or {})
        self._lots_per_trade = max(1, int(saved_config.get("lots_per_trade") or DEFAULT_COMMODITY_LOTS_PER_TRADE))

        saved_control = saved_state["control"]
        self._kill_switch_active = bool(saved_control.get("kill_switch_active", False))
        self._start_required = bool(saved_control.get("start_required", self._kill_switch_active))
        self._manual_restart_required = bool(saved_control.get("manual_restart_required", self._start_required))
        self._last_run_at = saved_control.get("last_run_at")
        self._last_error = saved_control.get("last_error")
        self._last_message = (
            saved_control.get("last_message")
            or (
                "Commodity kill switch active. Release it and start the agent to resume scanning."
                if self._kill_switch_active
                else None
            )
            or (
                "Commodity agent paused. Start the agent to resume scanning."
                if self._start_required
                else None
            )
            or (
                f"Tracking {len(self._symbols)} commodity symbols."
                if self._symbols
                else "Configure MCX symbols to start the commodity agent."
            )
        )
        self._restore_runtime_state(saved_state["runtime"])

    def _refresh_state_from_store(self, *, force: bool = False) -> bool:
        saved_state, updated_at = _load_saved_state_from_database()
        if saved_state is None:
            return False
        if (
            not force
            and updated_at is not None
            and self._state_synced_at is not None
            and updated_at <= self._state_synced_at
        ):
            return False
        self._apply_saved_state(saved_state)
        if updated_at is not None:
            self._state_synced_at = updated_at
        return True

    def _strategy_agents(self) -> list[_BaseCommodityLaneAgent]:
        return list(self._lane_agents)

    def _restore_runtime_state(self, runtime_state: dict[str, Any]) -> None:
        self._runtime.futures_watchlist = [
            row for row in list(runtime_state.get("futures_watchlist") or runtime_state.get("watchlist") or []) if isinstance(row, dict)
        ]
        self._runtime.option_watchlist = [
            row for row in list(runtime_state.get("option_watchlist") or []) if isinstance(row, dict)
        ]
        self._runtime.orders = [
            row for row in list(runtime_state.get("orders") or []) if isinstance(row, dict)
        ][:DEFAULT_COMMODITY_ORDERS_MAX]

        self._runtime.reports = []
        for row in list(runtime_state.get("reports") or []):
            try:
                self._runtime.reports.append(
                    CommodityReportSnapshot(
                        time=str(row.get("time") or ""),
                        total_equity=float(row.get("total_equity") or 0.0),
                        realized_pnl=float(row.get("realized_pnl") or 0.0),
                        unrealized_pnl=float(row.get("unrealized_pnl") or 0.0),
                        open_positions=int(row.get("open_positions") or 0),
                        tracked_symbols=int(row.get("tracked_symbols") or 0),
                        last_message=str(row.get("last_message") or ""),
                    )
                )
            except (TypeError, ValueError):
                continue
        self._runtime.reports = self._runtime.reports[:DEFAULT_COMMODITY_REPORTS_MAX]

        restored_positions: dict[str, CommodityPositionState] = {}
        for row in list(runtime_state.get("positions") or []):
            symbol = _canonicalize_symbol(row.get("symbol")) if str(row.get("symbol") or "").startswith("MCX:") else str(row.get("symbol") or "")
            underlying = str(row.get("underlying") or get_commodity_contract_spec(symbol).root)
            spec = get_commodity_contract_spec(underlying)
            raw_qty = int(row.get("qty") or 0)
            lot_size = int(row.get("lot_size") or spec.futures_lot_size or 1)
            lots = int(row.get("lots") or 0)
            if lots <= 0:
                lots = max(1, raw_qty // max(lot_size, 1)) if raw_qty > 0 else 1
            try:
                position = CommodityPositionState(
                    position_key=str(row.get("position_key") or row.get("strategy_key") or symbol),
                    symbol=symbol,
                    live_symbol=str(row.get("live_symbol") or symbol),
                    underlying=underlying,
                    strategy_key=str(row.get("strategy_key") or "commodity_futures"),
                    strategy_title=str(row.get("strategy_title") or spec.futures_label),
                    instrument_type=str(row.get("instrument_type") or "FUT"),
                    action=str(row.get("action") or "BUY"),
                    qty=raw_qty,
                    lots=lots,
                    lot_size=lot_size,
                    entry_price=float(row.get("entry_price") or 0.0),
                    current_price=float(row.get("current_price") or 0.0),
                    stop_price=float(row.get("stop_price") or 0.0),
                    target_price=float(row["target_price"]) if row.get("target_price") is not None else None,
                    regime=str(row.get("regime") or "unknown"),
                    signal_reason=str(row.get("signal_reason") or "signal"),
                    atr=float(row["atr"]) if row.get("atr") is not None else None,
                    macd_value=float(row["macd_value"]) if row.get("macd_value") is not None else None,
                    mp_poc=float(row["mp_poc"]) if row.get("mp_poc") is not None else None,
                    mp_vah=float(row["mp_vah"]) if row.get("mp_vah") is not None else None,
                    mp_val=float(row["mp_val"]) if row.get("mp_val") is not None else None,
                    entered_at=str(row.get("entered_at") or ""),
                    entry_bar_time=str(row.get("entry_bar_time") or ""),
                    contract_unit_label=str(row.get("contract_unit_label") or spec.contract_unit_label),
                    quote_unit_label=str(row.get("quote_unit_label") or spec.quote_unit_label),
                    display_name=str(row.get("display_name") or spec.display_name),
                    initial_qty=int(row.get("initial_qty") or raw_qty),
                    peak_price=float(row.get("peak_price") or row.get("current_price") or row.get("entry_price") or 0.0),
                    target_reached=bool(row.get("target_reached", False)),
                    partial_lock_armed=bool(row.get("partial_lock_armed", False)),
                    expiry=row.get("expiry"),
                    strike=float(row["strike"]) if row.get("strike") is not None else None,
                    option_type=row.get("option_type"),
                    entry_iv_pct=_round_or_none(row.get("entry_iv_pct"), 1),
                    entry_style=str(row.get("entry_style") or "") or None,
                    last_reviewed_bar_time=str(row.get("last_reviewed_bar_time") or row.get("entry_bar_time") or "") or None,
                )
            except (TypeError, ValueError):
                continue
            if position.position_key:
                restored_positions[position.position_key] = position
        self._runtime.positions = restored_positions
        self._runtime.processed_signals = {
            str(key): str(bar_time)
            for key, bar_time in dict(runtime_state.get("processed_signals") or {}).items()
            if str(key or "").strip() and str(bar_time or "").strip()
        }
        self._runtime.signal_audit = [
            row for row in list(runtime_state.get("signal_audit") or []) if isinstance(row, dict)
        ][:DEFAULT_COMMODITY_SIGNAL_AUDIT_MAX]

        self._commentary = []
        for row in list(runtime_state.get("commentary") or []):
            message = str(row.get("message") or "").strip()
            if not message:
                continue
            self._commentary.append(
                CommodityCommentaryEntry(
                    time=str(row.get("time") or ""),
                    tone=str(row.get("tone") or "info"),
                    message=message,
                )
            )
        self._commentary = self._commentary[:DEFAULT_COMMODITY_COMMENTARY_MAX]

        portfolio_payload = runtime_state.get("portfolio") or {}
        portfolio = self._runtime.portfolio
        portfolio.available_capital = float(portfolio_payload.get("available_capital") or portfolio.initial_capital)
        portfolio._trade_history = _deserialize_trade_history(list(portfolio_payload.get("trade_history") or []))
        portfolio._daily_pnl = defaultdict(float)
        for day_text, pnl in dict(portfolio_payload.get("daily_pnl") or {}).items():
            try:
                portfolio._daily_pnl[date.fromisoformat(str(day_text))] = float(pnl or 0.0)
            except (TypeError, ValueError):
                continue
        portfolio._equity_curve = []
        for row in list(portfolio_payload.get("equity_curve") or []):
            timestamp = _parse_datetime(row.get("time"))
            if timestamp is None:
                continue
            try:
                portfolio._equity_curve.append((timestamp, float(row.get("equity") or 0.0)))
            except (TypeError, ValueError):
                continue
        portfolio._peak_equity = float(portfolio_payload.get("peak_equity") or portfolio.initial_capital)
        portfolio._positions = {
            position.position_key: VirtualPosition(
                symbol=position.live_symbol,
                action=position.action,
                qty=position.qty,
                avg_price=position.entry_price,
                current_price=position.current_price,
                instrument_type=position.instrument_type,
                expiry=position.expiry,
                strike=position.strike,
                option_type=position.option_type,
                entry_iv_pct=position.entry_iv_pct,
                opened_at=_parse_datetime(position.entered_at) or datetime.now(timezone.utc),
            )
            for position in self._runtime.positions.values()
        }
        _repair_portfolio_ledger(portfolio)

    def _build_saved_state(self) -> dict[str, Any]:
        portfolio = self._runtime.portfolio
        return {
            "config": {
                "symbols": list(self._symbols),
                "selected_option_expiries": dict(self._selected_option_expiries),
                "selected_option_lookup_symbols": dict(self._selected_option_lookup_symbols),
                "lots_per_trade": self._lots_per_trade,
            },
            "control": {
                "kill_switch_active": self._kill_switch_active,
                "start_required": self._start_required,
                "manual_restart_required": self._manual_restart_required,
                "last_run_at": self._last_run_at,
                "last_error": self._last_error,
                "last_message": self._last_message,
            },
            "runtime": {
                "watchlist": list(self._runtime.futures_watchlist),
                "futures_watchlist": list(self._runtime.futures_watchlist),
                "option_watchlist": list(self._runtime.option_watchlist),
                "positions": [asdict(position) for position in self._runtime.positions.values()],
                "orders": list(self._runtime.orders),
                "reports": [asdict(report) for report in self._runtime.reports],
                "commentary": [asdict(entry) for entry in self._commentary],
                "processed_signals": dict(self._runtime.processed_signals),
                "signal_audit": list(self._runtime.signal_audit),
                "portfolio": {
                    "initial_capital": float(portfolio.initial_capital),
                    "available_capital": float(portfolio.available_capital),
                    "trade_history": _serialize_trade_history(portfolio),
                    "daily_pnl": {day.isoformat(): float(pnl) for day, pnl in getattr(portfolio, "_daily_pnl", {}).items()},
                    "equity_curve": [
                        {"time": timestamp.isoformat(), "equity": float(equity)}
                        for timestamp, equity in getattr(portfolio, "_equity_curve", [])
                    ],
                    "peak_equity": float(getattr(portfolio, "_peak_equity", portfolio.initial_capital)),
                },
            },
        }

    def _persist_state(self) -> None:
        self._state_synced_at = _save_state(self._build_saved_state()) or self._state_synced_at

    def _loop_active(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _stop_loop(self) -> None:
        task = self._task
        self._task = None
        if not task:
            return
        if task is asyncio.current_task():
            self._running = False
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

    async def start(self, *, force: bool = False) -> None:
        self._refresh_state_from_store()
        self._enabled = True
        if not self._auto_run_enabled or self._kill_switch_active:
            self._persist_state()
            return
        if self._manual_restart_required and not force:
            if _in_commodity_hours():
                self._manual_restart_required = False
                self._start_required = False
                self._last_message = "Commodity agent auto-resumed because MCX market is open."
            else:
                self._persist_state()
                return
        if self._start_required and not force:
            if _in_commodity_hours():
                self._start_required = False
                self._last_message = "Commodity agent auto-resumed because MCX market is open."
            else:
                self._persist_state()
                return
        if self._loop_active():
            return
        self._start_required = False
        self._manual_restart_required = False
        self._last_error = None
        if self._symbols:
            self._last_message = "Commodity agent running continuously."
        self._task = asyncio.create_task(self._loop(), name="commodity-strategy-agent")
        self._persist_state()

    async def stop(self) -> None:
        self._refresh_state_from_store()
        self._enabled = False
        await self._stop_loop()
        self._persist_state()

    async def start_loop(self) -> dict[str, Any]:
        self._refresh_state_from_store()
        if self._kill_switch_active:
            self._last_message = "Commodity kill switch is active. Release it before starting the agent."
            self._append_commentary("warning", self._last_message)
            self._persist_state()
            return self.get_status(refresh=False)
        await self.start(force=True)
        self._manual_restart_required = False
        self._start_required = False
        if self._symbols:
            self._last_message = "Commodity agent started. Futures and options sleeves are armed."
        else:
            self._last_message = "Commodity agent started. Add MCX symbols to begin scanning."
        self._append_commentary("success", self._last_message)
        self._persist_state()
        return self.get_status(refresh=False)

    def update_symbols(
        self,
        symbols: list[str],
        *,
        selected_option_expiries: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        self._refresh_state_from_store()
        self._symbols = _normalize_symbols(symbols)
        base_selection = selected_option_expiries if selected_option_expiries is not None else self._selected_option_expiries
        self._selected_option_expiries = _normalize_selected_option_expiries(self._symbols, base_selection)
        self._selected_option_lookup_symbols = _normalize_selected_option_lookup_symbols(
            self._symbols,
            self._selected_option_lookup_symbols,
            selected_option_expiries=self._selected_option_expiries,
        )
        if self._symbols:
            self._append_commentary(
                "success",
                f"Tracking {len(self._symbols)} commodity symbols: {', '.join(self._symbols)}",
            )
            self._last_message = f"Tracking {len(self._symbols)} commodity symbols."
        else:
            self._append_commentary("warning", "Commodity symbol list cleared.")
            self._last_message = "Configure MCX symbols to start the commodity agent."
        self._persist_state()
        return {
            "symbols": list(self._symbols),
            "selected_option_expiries": dict(self._selected_option_expiries),
            "selected_option_lookup_symbols": dict(self._selected_option_lookup_symbols),
        }

    def get_symbols(self) -> list[str]:
        self._refresh_state_from_store()
        return list(self._symbols)

    def get_selected_option_expiries(self) -> dict[str, str]:
        self._refresh_state_from_store()
        return dict(self._selected_option_expiries)

    def get_selected_option_lookup_symbols(self) -> dict[str, str]:
        self._refresh_state_from_store()
        return dict(self._selected_option_lookup_symbols)

    def _active_futures_symbol(self, symbol: str) -> str:
        configured_symbol = _canonicalize_symbol(symbol)
        lookup_symbol = _canonicalize_symbol(self._selected_option_lookup_symbols.get(configured_symbol) or "")
        if (
            lookup_symbol
            and lookup_symbol.endswith("FUT")
            and extract_commodity_root(lookup_symbol) == extract_commodity_root(configured_symbol)
        ):
            return lookup_symbol
        return configured_symbol

    def _active_futures_symbols(self) -> dict[str, str]:
        return {symbol: self._active_futures_symbol(symbol) for symbol in self._symbols}

    async def ensure_selected_option_setup_locks(self) -> dict[str, str]:
        self._refresh_state_from_store()
        missing_symbols = {
            symbol: expiry
            for symbol, expiry in self._selected_option_expiries.items()
            if expiry and not str(self._selected_option_lookup_symbols.get(symbol) or "").strip()
        }
        if not missing_symbols:
            return dict(self._selected_option_lookup_symbols)

        catalog = await commodity_atm_watchlist_service.get_contract_catalog(
            self._symbols,
            self._selected_option_expiries,
            None,
        )
        updated_lookup_symbols = dict(self._selected_option_lookup_symbols)
        for contract in list(catalog.get("contracts") or []):
            symbol = _canonicalize_symbol(contract.get("symbol"))
            selected_expiry = missing_symbols.get(symbol)
            if not selected_expiry:
                continue
            expiry_mappings = list(contract.get("expiry_mappings") or [])
            resolved_lookup_symbol = next(
                (
                    _canonicalize_symbol(item.get("lookup_symbol"))
                    for item in expiry_mappings
                    if str(item.get("expiry")) == selected_expiry and str(item.get("lookup_symbol") or "").strip()
                ),
                "",
            )
            if not resolved_lookup_symbol:
                resolved_lookup_symbol = _canonicalize_symbol(
                    contract.get("active_lookup_symbol")
                    or contract.get("lookup_symbol")
                    or contract.get("default_lookup_symbol")
                    or symbol
                )
            if resolved_lookup_symbol:
                updated_lookup_symbols[symbol] = resolved_lookup_symbol

        normalized_lookup_symbols = _normalize_selected_option_lookup_symbols(
            self._symbols,
            updated_lookup_symbols,
            selected_option_expiries=self._selected_option_expiries,
        )
        if normalized_lookup_symbols == self._selected_option_lookup_symbols:
            return dict(self._selected_option_lookup_symbols)

        self._selected_option_lookup_symbols = normalized_lookup_symbols
        self._persist_state()
        return dict(self._selected_option_lookup_symbols)

    async def update_selected_option_expiries(self, selected_option_expiries: dict[str, str]) -> dict[str, Any]:
        self._refresh_state_from_store()
        normalized_expiries = _normalize_selected_option_expiries(self._symbols, selected_option_expiries)
        selected_lookup_symbols: dict[str, str] = {}
        if normalized_expiries:
            catalog = await commodity_atm_watchlist_service.get_contract_catalog(
                self._symbols,
                normalized_expiries,
                None,
            )
            for contract in list(catalog.get("contracts") or []):
                symbol = _canonicalize_symbol(contract.get("symbol"))
                selected_expiry = normalized_expiries.get(symbol)
                if not selected_expiry:
                    continue
                expiry_mappings = list(contract.get("expiry_mappings") or [])
                resolved_lookup_symbol = next(
                    (
                        _canonicalize_symbol(item.get("lookup_symbol"))
                        for item in expiry_mappings
                        if str(item.get("expiry")) == selected_expiry and str(item.get("lookup_symbol") or "").strip()
                    ),
                    "",
                )
                if not resolved_lookup_symbol:
                    resolved_lookup_symbol = _canonicalize_symbol(
                        contract.get("active_lookup_symbol")
                        or contract.get("lookup_symbol")
                        or contract.get("default_lookup_symbol")
                        or symbol
                    )
                if resolved_lookup_symbol:
                    selected_lookup_symbols[symbol] = resolved_lookup_symbol

        self._selected_option_expiries = normalized_expiries
        self._selected_option_lookup_symbols = _normalize_selected_option_lookup_symbols(
            self._symbols,
            selected_lookup_symbols,
            selected_option_expiries=self._selected_option_expiries,
        )
        if self._selected_option_expiries:
            self._append_commentary(
                "success",
                f"Saved {len(self._selected_option_expiries)} commodity option expiry selections.",
            )
        else:
            self._append_commentary("warning", "Commodity option expiry selections cleared.")
        self._persist_state()
        return {
            "selected_option_expiries": dict(self._selected_option_expiries),
            "selected_option_lookup_symbols": dict(self._selected_option_lookup_symbols),
        }

    def _estimate_futures_margin_required(self, price: float, qty: int) -> float:
        return max(price, 0.0) * max(qty, 0) * DEFAULT_COMMODITY_MARGIN_PCT

    def _has_underlying_position(self, strategy_key: str, underlying: str) -> bool:
        return any(
            position.strategy_key == strategy_key and position.underlying == underlying
            for position in self._runtime.positions.values()
        )

    def _has_any_underlying_position(self, underlying: str) -> bool:
        normalized = extract_commodity_root(underlying)
        return any(
            extract_commodity_root(position.underlying) == normalized
            for position in self._runtime.positions.values()
        )

    def _today_realized_pnl(self, now: Optional[datetime] = None) -> float:
        current = now or _now_ist()
        total = 0.0
        for trade in getattr(self._runtime.portfolio, "_trade_history", []):
            exit_time = trade.exit_time
            if exit_time.tzinfo is None:
                exit_time = exit_time.replace(tzinfo=IST)
            else:
                exit_time = exit_time.astimezone(IST)
            if exit_time.date() == current.date():
                total += float(trade.pnl)
        return total

    def _underlying_today_realized_pnl(self, underlying: str, now: Optional[datetime] = None) -> float:
        current = now or _now_ist()
        total = 0.0
        for trade in getattr(self._runtime.portfolio, "_trade_history", []):
            exit_time = trade.exit_time
            if exit_time.tzinfo is None:
                exit_time = exit_time.replace(tzinfo=IST)
            else:
                exit_time = exit_time.astimezone(IST)
            if exit_time.date() != current.date():
                continue
            if _symbol_matches_underlying(trade.symbol, underlying):
                total += float(trade.pnl)
        return total

    def _recent_stop_cooldown_reason(self, underlying: str, now: Optional[datetime] = None) -> Optional[str]:
        current = now or _now_ist()
        stop_reasons = {"stop_loss", "hard_stop", "trail_stop", "runner_trail_stop", "trailing_stoploss"}
        for order in self._runtime.orders:
            if str(order.get("flow") or "") != "exit":
                continue
            reason = str(order.get("reason") or "")
            if reason not in stop_reasons:
                continue
            if not _symbol_matches_underlying(str(order.get("symbol") or ""), underlying):
                continue
            exit_time = _parse_datetime(order.get("time"))
            if exit_time is None:
                continue
            minutes_since_stop = (current - exit_time.astimezone(IST)).total_seconds() / 60.0
            if 0 <= minutes_since_stop < COMMODITY_STOP_COOLDOWN_MINUTES:
                remaining = max(1, int(COMMODITY_STOP_COOLDOWN_MINUTES - minutes_since_stop))
                return f"recent stop-out on {extract_commodity_root(underlying)}; cooldown {remaining}m remaining"
        return None

    def _entry_risk_block(self, underlying: str, now: Optional[datetime] = None) -> Optional[dict[str, str]]:
        current = now or _now_ist()
        drawdown_pct = self._current_drawdown_pct()
        if drawdown_pct >= COMMODITY_MAX_DRAWDOWN_PCT:
            return {
                "code": "max_drawdown_limit",
                "detail": (
                    f"Commodity desk drawdown is {drawdown_pct:.1f}% from peak; "
                    "new entries are blocked pending manual review."
                ),
            }
        daily_pnl = self._today_realized_pnl(current)
        if daily_pnl <= -COMMODITY_DAILY_LOSS_LIMIT:
            return {
                "code": "daily_loss_limit",
                "detail": f"Commodity desk daily loss is {daily_pnl:.0f}; new entries are blocked.",
            }
        underlying_pnl = self._underlying_today_realized_pnl(underlying, current)
        if underlying_pnl <= -COMMODITY_UNDERLYING_DAILY_LOSS_LIMIT:
            return {
                "code": "underlying_loss_limit",
                "detail": f"{extract_commodity_root(underlying)} daily loss is {underlying_pnl:.0f}; new entries are blocked.",
            }
        cooldown_reason = self._recent_stop_cooldown_reason(underlying, current)
        if cooldown_reason:
            return {"code": "stop_cooldown", "detail": cooldown_reason}
        return None

    def _current_drawdown_pct(self) -> float:
        equity = float(self._runtime.portfolio.total_equity or 0.0)
        peak_equity = float(getattr(self._runtime.portfolio, "_peak_equity", 0.0) or 0.0)
        if peak_equity <= 0:
            return 0.0
        return max(0.0, ((peak_equity - equity) / peak_equity) * 100.0)

    async def _record_drawdown_risk_block(self, *, drawdown_pct: Optional[float] = None) -> None:
        drawdown = float(drawdown_pct if drawdown_pct is not None else self._current_drawdown_pct())
        self._last_message = (
            f"Commodity drawdown risk block observed: drawdown {drawdown:.1f}% exceeded "
            f"the {COMMODITY_MAX_DRAWDOWN_PCT:.1f}% cap. Entries remain blocked by risk rules; "
            "the kill switch is reserved for manual operator action."
        )
        self._persist_state()
        await record_audit_event(
            market="commodity",
            event_type="risk_block_observed",
            actor="risk_governor",
            severity="warning",
            message=self._last_message,
            previous_state="active",
            new_state="active",
            payload={
                "drawdown_pct": round(drawdown, 4),
                "cap_pct": COMMODITY_MAX_DRAWDOWN_PCT,
                "total_equity": float(self._runtime.portfolio.total_equity or 0.0),
            },
        )
        self._append_commentary("warning", self._last_message)

    async def _engage_drawdown_kill_switch(self, *, drawdown_pct: Optional[float] = None) -> dict[str, Any]:
        drawdown = float(drawdown_pct if drawdown_pct is not None else self._current_drawdown_pct())
        state = await self.set_kill_switch(True)
        self._last_message = (
            f"Commodity drawdown kill switch active: drawdown {drawdown:.1f}% exceeded "
            f"the {COMMODITY_MAX_DRAWDOWN_PCT:.1f}% cap. Manual restart is required."
        )
        self._append_commentary("warning", self._last_message)
        self._persist_state()
        return state

    def _strategy_catalog(self) -> list[dict[str, Any]]:
        option_contracts_ready = sum(1 for expiry in self._selected_option_expiries.values() if expiry)
        lane_map = {lane.descriptor.key: lane for lane in self._strategy_agents()}
        option_positions = lane_map["commodity_options"].open_positions()
        futures_positions = lane_map["commodity_futures"].open_positions()
        return [
            {
                "key": "commodity_futures",
                "title": "Strategy 2 · Futures",
                "agent": lane_map["commodity_futures"].build_status_payload(),
                "status": "paper_execution" if self._symbols else "idle",
                "instrument": "MCX futures · 15m MACD + MP",
                "tracked_symbols": len(self._symbols),
                "open_positions": futures_positions,
                "timeframe": lane_map["commodity_futures"].descriptor.timeframe,
                "execution_mode": lane_map["commodity_futures"].descriptor.execution_mode,
                "position_cap": lane_map["commodity_futures"].descriptor.position_cap,
                "lots_per_trade": self._lots_per_trade,
                "broker": "upstox primary · fyers fallback",
                "notes": "Entries use live 15-minute bars, accept fresh zero-crosses plus continuation breakouts only on trend-day MP, and keep hard stops live while delaying soft exits until the trade has had time to work. MCX futures quotes/history prefer Upstox and fall back to FYERS.",
            },
            {
                "key": "commodity_options",
                "title": "Strategy 1 · Options",
                "agent": lane_map["commodity_options"].build_status_payload(),
                "status": "paper_execution" if option_contracts_ready else "monitoring",
                "instrument": "MCX liquid CE / PE · 30m MACD",
                "tracked_symbols": len(self._symbols),
                "configured_contracts": option_contracts_ready,
                "open_positions": option_positions,
                "timeframe": lane_map["commodity_options"].descriptor.timeframe,
                "execution_mode": lane_map["commodity_options"].descriptor.execution_mode,
                "position_cap": lane_map["commodity_options"].descriptor.position_cap,
                "broker": "upstox primary · fyers fallback",
                "notes": f"Entries use liquid near-ATM contracts, 30-minute MACD zero-cross, 25% hard stop, and {OPTIONS_CAPITAL_FRACTION:.0%} capital budget per trade. Underlying MCX spot quotes prefer Upstox before FYERS.",
            },
        ]

    def _decorate_futures_rows(self, watch_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        decorated: list[dict[str, Any]] = []
        futures_positions = sum(1 for pos in self._runtime.positions.values() if pos.strategy_key == "commodity_futures")
        at_capacity = futures_positions >= FUTURES_MAX_POSITIONS
        for row in watch_rows:
            symbol = str(row.get("symbol") or "")
            underlying = str(row.get("underlying") or get_commodity_contract_spec(symbol).root)
            signal = str(row.get("signal") or "")
            raw_signal = str(row.get("raw_signal") or "")
            continuation_signal = str(row.get("continuation_signal") or "")
            candidate_signal = signal or raw_signal or continuation_signal
            bar_time = str(row.get("bar_time") or "")
            spec = get_commodity_contract_spec(symbol)
            price = float(row.get("price") or 0.0)
            qty = spec.futures_lot_size * self._lots_per_trade
            event_reason = _commodity_event_block_reason(underlying)
            risk_block = self._entry_risk_block(underlying)
            validation = "waiting_cross"
            validation_detail = "Waiting for a live 15-minute MACD cross or a continuation breakout."
            if row.get("reason") == "insufficient_data":
                validation = "warming_up"
                validation_detail = "More 15-minute candles are required before futures MACD is valid."
            elif row.get("mp_status") == "warming_up":
                validation = "mp_warming_up"
                validation_detail = "Market Profile needs more intraday periods before confirming direction."
            elif candidate_signal in {"BUY", "SELL"} and signal != candidate_signal:
                validation = "mp_conflict" if row.get("mp_direction") else "mp_pending"
                validation_detail = str(
                    row.get("signal_validation_detail")
                    or "Signal candidate fired, but Market Profile confirmation is missing or opposite."
                )
            elif signal in {"BUY", "SELL"} and (price <= 0 or float(row.get("atr") or 0.0) <= 0):
                validation = "price_unavailable"
                validation_detail = "Signal exists, but price or ATR is missing so the entry is blocked."
            elif signal in {"BUY", "SELL"} and self._kill_switch_active:
                validation = "blocked_kill_switch"
                validation_detail = "Kill switch is active. Signal is recorded but the execution lane is paused."
            elif signal in {"BUY", "SELL"} and self._has_any_underlying_position(underlying):
                validation = "position_open"
                validation_detail = "A commodity position is already open for this underlying."
            elif signal in {"BUY", "SELL"} and event_reason:
                validation = "event_window"
                validation_detail = f"{event_reason.replace('_', ' ')} is active; entries are blocked around scheduled data releases."
            elif signal in {"BUY", "SELL"} and risk_block:
                validation = risk_block["code"]
                validation_detail = risk_block["detail"]
            elif signal in {"BUY", "SELL"} and self._runtime.processed_signals.get(f"commodity_futures:{symbol}") == bar_time:
                validation = "bar_consumed"
                validation_detail = "This 15-minute bar already triggered an entry."
            elif signal in {"BUY", "SELL"} and at_capacity:
                validation = "max_positions"
                validation_detail = "The futures sleeve is already at max open-position capacity."
            elif signal in {"BUY", "SELL"}:
                data_quality_block = _data_quality_block_reason(symbol, "broker_futures_quote")
                if data_quality_block:
                    validation = "data_stale"
                    validation_detail = data_quality_block
                else:
                    required_margin = self._estimate_futures_margin_required(price, qty)
                    if required_margin > self._runtime.portfolio.available_capital:
                        validation = "insufficient_margin"
                        validation_detail = "Available paper capital cannot fund the next futures lot."
                    else:
                        validation = "ready"
                        validation_detail = str(
                            row.get("signal_validation_detail")
                            or (
                                "15-minute continuation setup and Market Profile are aligned for entry."
                                if row.get("entry_style") == "continuation"
                                else "15-minute MACD and Market Profile are aligned for entry."
                            )
                        )

            bucket_info = classify_signal_bucket(
                has_position=self._has_any_underlying_position(underlying),
                signal_validation=validation,
                macd=row.get("macd"),
                macd_histogram=row.get("macd_histogram"),
                prev_macd=row.get("prev_macd"),
                prev_macd_histogram=row.get("prev_macd_histogram"),
                recent_cross_signal=row.get("recent_cross_signal"),
                recent_cross_bars_ago=row.get("recent_cross_bars_ago"),
            )

            decorated.append(
                {
                    **row,
                    "indicator_timeframe": FUTURES_TIMEFRAME,
                    "display_name": spec.display_name,
                    "lot_size": spec.futures_lot_size,
                    "lots_per_trade": self._lots_per_trade,
                    "default_qty": qty,
                    "contract_unit_label": spec.contract_unit_label,
                    "quote_unit_label": spec.quote_unit_label,
                    "strategy_title": spec.futures_label,
                    "signal_validation": validation,
                    "signal_validation_detail": validation_detail,
                    "execution_lane": "paper_futures",
                    "required_margin": _round_or_none(self._estimate_futures_margin_required(price, qty), 2),
                    "bias_side": "CE" if signal == "BUY" else "PE" if signal == "SELL" else None,
                    **bucket_info,
                }
            )
        return decorated

    def _decorate_option_rows(self, option_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        decorated: list[dict[str, Any]] = []
        option_positions = sum(1 for pos in self._runtime.positions.values() if pos.strategy_key == "commodity_options")
        at_capacity = option_positions >= OPTIONS_MAX_POSITIONS
        for row in option_rows:
            signal_side = str(row.get("signal_side") or "")
            trade_symbol = str(row.get("trade_symbol") or "")
            bar_time = str(row.get("trade_bar_time") or "")
            underlying = str(row.get("underlying") or row.get("symbol") or "")
            event_reason = _commodity_event_block_reason(underlying)
            risk_block = self._entry_risk_block(underlying)
            entry_iv_pct = row.get("entry_iv_pct")
            try:
                days_to_expiry = (date.fromisoformat(str(row.get("expiry") or "")) - _now_ist().date()).days
            except ValueError:
                days_to_expiry = None
            validation = "waiting_cross"
            validation_detail = "Waiting for a fresh 30-minute option MACD cross."
            if row.get("regime") == "warmup":
                validation = "warming_up"
                validation_detail = "CE and PE candles need more history before 30-minute option MACD is valid."
            elif signal_side and not row.get("is_trade_contract_liquid"):
                validation = "illiquid_contract"
                validation_detail = "The nearest liquid contract filter rejected the current CE/PE candidate."
            elif signal_side and self._kill_switch_active:
                validation = "blocked_kill_switch"
                validation_detail = "Kill switch is active. Option entries are paused."
            elif signal_side and self._has_any_underlying_position(underlying):
                validation = "position_open"
                validation_detail = "A commodity position is already open for this underlying."
            elif signal_side and event_reason:
                validation = "event_window"
                validation_detail = f"{event_reason.replace('_', ' ')} is active; entries are blocked around scheduled data releases."
            elif signal_side and risk_block:
                validation = risk_block["code"]
                validation_detail = risk_block["detail"]
            elif signal_side and row.get("regime") == "vol_spike":
                validation = "regime_blocked"
                validation_detail = "Vol-spike regime is evaluation-only for options; automatic entries are blocked."
            elif signal_side and days_to_expiry is not None and days_to_expiry < OPTIONS_MIN_TTE_DAYS:
                validation = "tte_filter"
                validation_detail = f"Only {days_to_expiry} day(s) to expiry; minimum is {OPTIONS_MIN_TTE_DAYS}."
            elif signal_side and entry_iv_pct is None:
                validation = "iv_unavailable"
                validation_detail = "Entry IV is unavailable, so the documented IV filter cannot approve the trade."
            elif signal_side and float(entry_iv_pct or 0.0) > OPTIONS_IV_REJECT_PCT:
                validation = "iv_reject"
                validation_detail = f"Entry IV {float(entry_iv_pct):.1f}% is above the {OPTIONS_IV_REJECT_PCT:.0f}% hard cap."
            elif signal_side and self._runtime.processed_signals.get(f"commodity_options:{trade_symbol}") == bar_time:
                validation = "bar_consumed"
                validation_detail = "This 30-minute option bar already triggered an entry."
            elif signal_side and at_capacity:
                validation = "max_positions"
                validation_detail = "The options sleeve is already at max open-position capacity."
            elif signal_side and int(row.get("lots_affordable") or 0) <= 0:
                validation = "insufficient_capital"
                validation_detail = f"The {OPTIONS_CAPITAL_FRACTION:.0%} capital budget cannot fund one option lot at the current premium."
            elif signal_side:
                data_quality_block = _data_quality_block_reason(trade_symbol, "broker_option_quote")
                if data_quality_block:
                    validation = "data_stale"
                    validation_detail = data_quality_block
                else:
                    validation = "ready"
                    validation_detail = "The selected CE/PE contract has a fresh 30-minute MACD trigger and passes the liquidity check."
            elif row.get("regime") in {"bullish", "bearish", "dead_zone", "vol_spike"}:
                validation = "trend_aligned"
                validation_detail = "Option MACD context is available, but a fresh CE/PE zero-cross has not fired yet."

            side_payload = (row.get("ce") or {}) if signal_side == "CE" else (row.get("pe") or {})
            side_macd = side_payload.get("macd")
            side_hist = side_payload.get("macd_histogram")
            side_prev_hist = side_payload.get("prev_macd_histogram")
            side_prev_macd = side_payload.get("prev_macd")
            side_recent_cross = side_payload.get("recent_cross_signal")
            side_recent_bars_ago = side_payload.get("recent_cross_bars_ago")
            bucket_info = classify_signal_bucket(
                has_position=self._has_any_underlying_position(underlying),
                signal_validation=validation,
                macd=side_macd,
                macd_histogram=side_hist,
                prev_macd=side_prev_macd,
                prev_macd_histogram=side_prev_hist,
                recent_cross_signal=side_recent_cross,
                recent_cross_bars_ago=side_recent_bars_ago,
            )

            decorated.append(
                {
                    **row,
                    "signal_validation": validation,
                    "signal_validation_detail": validation_detail,
                    "strategy_title": get_commodity_contract_spec(str(row.get("symbol") or "")).options_label,
                    **bucket_info,
                }
            )
        return decorated

    async def _loop(self) -> None:
        try:
            while self._enabled and not self._kill_switch_active and not self._start_required:
                try:
                    await asyncio.wait_for(
                        self.run_once(force=False),
                        timeout=DEFAULT_COMMODITY_SCAN_TIMEOUT_SECONDS,
                    )
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    self._last_error = (
                        f"Commodity strategy scan exceeded {DEFAULT_COMMODITY_SCAN_TIMEOUT_SECONDS}s and was cancelled."
                    )
                    self._last_message = (
                        "Commodity strategy scan timed out; the loop will retry on the next cycle."
                    )
                    self._append_commentary("warning", self._last_message)
                    self._persist_state()
                    logger.warning("[CommodityStrategy] scan timed out; retrying next cycle")
                except Exception as exc:
                    self._last_error = str(exc)
                    self._last_message = f"Commodity strategy error: {exc}"
                    self._append_commentary("error", f"Loop failure: {exc}")
                    logger.exception("[CommodityStrategy] loop failure")
                if not self._enabled or self._kill_switch_active or self._start_required:
                    break
                await asyncio.sleep(self.scan_interval_seconds)
        finally:
            if self._task is asyncio.current_task():
                self._task = None

    async def _get_fyers_adapter(self) -> Optional[BrokerAdapter]:
        adapter = get_active_adapter("fyers")
        if adapter:
            return adapter
        if await ensure_fyers_session(force_validate=True):
            return get_active_adapter("fyers")
        return None

    async def _get_upstox_adapter(self) -> Optional[BrokerAdapter]:
        adapter = get_active_adapter("upstox")
        if adapter:
            return adapter
        if await ensure_upstox_session(force_validate=True):
            return get_active_adapter("upstox")
        return None

    @staticmethod
    def _fyers_failure_message(health: dict[str, Any]) -> str:
        status = str(health.get("status") or "disconnected")
        return (
            "No valid Fyers session is available for the commodity scan. "
            f"Fyers={status.replace('_', ' ')}."
        )

    @staticmethod
    def _commodity_broker_failure_message(
        fyers_health: dict[str, Any],
        upstox_health: dict[str, Any],
    ) -> str:
        fyers_status = str(fyers_health.get("status") or "disconnected").replace("_", " ")
        upstox_status = str(upstox_health.get("status") or "disconnected").replace("_", " ")
        return (
            "No valid commodity broker session is available for the commodity scan. "
            f"Upstox={upstox_status}; Fyers={fyers_status}."
        )

    async def _get_scan_adapter(self) -> Optional[BrokerAdapter]:
        """Use the healthiest commodity-capable broker; Upstox is preferred for MCX data."""
        return await self._get_upstox_adapter() or await self._get_fyers_adapter()

    def _commodity_futures_quality_blocked(
        self,
        data_quality_snapshot: Optional[dict[str, Any]],
        quote_map: dict[str, float],
    ) -> tuple[bool, str]:
        if not data_quality_snapshot:
            return False, ""
        health_rows = {
            str(row.get("symbol") or ""): row
            for row in list(data_quality_snapshot.get("symbol_health") or [])
            if isinstance(row, dict)
        }
        stale_symbols: list[str] = []
        missing_symbols: list[str] = []
        for symbol in self._symbols:
            if float(quote_map.get(symbol) or 0.0) <= 0:
                missing_symbols.append(symbol)
                continue
            row = health_rows.get(symbol)
            if row and bool(row.get("stale")):
                stale_symbols.append(symbol)
        if not missing_symbols and not stale_symbols:
            return False, ""
        parts: list[str] = []
        if missing_symbols:
            parts.append(f"missing quotes for {', '.join(missing_symbols)}")
        if stale_symbols:
            parts.append(f"stale quotes for {', '.join(stale_symbols)}")
        return True, "; ".join(parts)

    def _commodity_data_quality_summary(
        self,
        data_quality_snapshot: Optional[dict[str, Any]],
        quote_map: dict[str, float],
        option_quote_map: Optional[dict[str, float]] = None,
    ) -> dict[str, Any]:
        """Return the commodity desk quality without unrelated NSE/BSE symbols."""
        option_quote_map = option_quote_map or {}
        expected_symbols = sorted(
            {
                str(symbol).strip()
                for symbol in [*self._symbols, *quote_map.keys(), *option_quote_map.keys()]
                if str(symbol or "").strip()
            }
        )
        health_rows = {
            str(row.get("symbol") or ""): row
            for row in list((data_quality_snapshot or {}).get("symbol_health") or [])
            if isinstance(row, dict)
        }
        entries_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for entry in list((data_quality_snapshot or {}).get("entries") or []):
            if not isinstance(entry, dict):
                continue
            symbol = str(entry.get("symbol") or "")
            if symbol in expected_symbols:
                entries_by_symbol.setdefault(symbol, []).append(entry)

        symbol_health: list[dict[str, Any]] = []
        missing_symbols: list[str] = []
        stale_symbols: list[str] = []
        flagged_symbols: list[str] = []

        for symbol in expected_symbols:
            quote = quote_map.get(symbol, option_quote_map.get(symbol))
            has_live_quote = False
            try:
                has_live_quote = float(quote or 0.0) > 0.0
            except (TypeError, ValueError):
                has_live_quote = False
            row = dict(health_rows.get(symbol) or {})
            if not row:
                row = {
                    "symbol": symbol,
                    "stale": not has_live_quote,
                    "flagged": False,
                    "freshest_source": "direct_quote" if has_live_quote else None,
                    "freshest_age_seconds": 0 if has_live_quote else None,
                    "sources": 0,
                }
            if not has_live_quote and symbol in self._symbols:
                missing_symbols.append(symbol)
                row["stale"] = True
            if bool(row.get("stale")):
                stale_symbols.append(symbol)
            if bool(row.get("flagged")):
                flagged_symbols.append(symbol)
            symbol_health.append(row)

        overall = "healthy"
        if not expected_symbols:
            overall = "unknown"
        elif flagged_symbols:
            overall = "critical"
        elif missing_symbols or stale_symbols:
            overall = "degraded"

        return {
            "overall": overall,
            "global_overall": (data_quality_snapshot or {}).get("overall"),
            "symbol_count": len(symbol_health),
            "stale_count": len(stale_symbols),
            "flagged_count": len(flagged_symbols),
            "missing_count": len(missing_symbols),
            "missing_symbols": missing_symbols,
            "stale_symbols": stale_symbols,
            "flagged_symbols": flagged_symbols,
            "symbol_health": symbol_health,
            "entries": [
                entry
                for symbol in expected_symbols
                for entry in entries_by_symbol.get(symbol, [])
            ],
        }

    @staticmethod
    def _option_history_warning(health: dict[str, Any]) -> Optional[str]:
        if int(health.get("failure_count", 0)) <= 0:
            return None
        broker_parts: list[str] = []
        for broker, state in dict(health.get("brokers") or {}).items():
            failures = int(state.get("failure", 0) or 0)
            if failures <= 0:
                continue
            detail = str(state.get("last_detail") or "fetch failed")
            broker_parts.append(f"{broker}: {failures} failure(s), latest '{detail}'")
        if not broker_parts:
            return None
        return "Commodity history warnings detected: " + "; ".join(broker_parts) + "."

    async def _load_history(
        self,
        symbol: str,
        *,
        interval: str,
        lookback_days: int = DEFAULT_COMMODITY_HISTORY_DAYS,
    ) -> list[dict[str, Any]]:
        fetch_from = _now_ist().date() - timedelta(days=lookback_days)
        fetch_to = _now_ist().date()
        rows: list[dict[str, Any]] = []
        resolved_upstox = await resolve_upstox_mcx_future(symbol)
        upstox_key = str((resolved_upstox or {}).get("instrument_key") or "")
        if upstox_key:
            if interval in {"5minute", "15minute"}:
                minute_rows = await option_history_service._fetch_broker_candles(
                    instrument_key=upstox_key,
                    from_date=fetch_from,
                    to_date=fetch_to,
                    interval="1minute",
                )
                if minute_rows:
                    rows = option_history_service._aggregate_rows(
                        minute_rows,
                        15 if interval == "15minute" else 5,
                    )
            else:
                rows = await option_history_service._fetch_broker_candles(
                    instrument_key=upstox_key,
                    from_date=fetch_from,
                    to_date=fetch_to,
                    interval=interval,
                )
        if not rows:
            rows = await option_history_service._fetch_broker_candles(
                instrument_key=symbol,
                from_date=fetch_from,
                to_date=fetch_to,
                interval=interval,
            )
        if not rows and interval in {"5minute", "15minute"}:
            minute_rows = await option_history_service._fetch_broker_candles(
                instrument_key=symbol,
                from_date=fetch_from,
                to_date=fetch_to,
                interval="1minute",
            )
            if minute_rows:
                rows = option_history_service._aggregate_rows(
                    minute_rows,
                    15 if interval == "15minute" else 5,
                )
        return rows

    def _build_market_profile(self, symbol: str, rows: list[dict[str, Any]]):
        if not rows:
            return None
        spec = get_commodity_contract_spec(symbol)
        bars: list[MarketBar] = []
        for row in rows:
            parsed = _parse_iso_timestamp(row.get("time"))
            close = row.get("close")
            if parsed is None or close is None:
                continue
            bars.append(
                MarketBar(
                    timestamp=parsed,
                    open=float(row.get("open", close) or close),
                    high=float(row.get("high", close) or close),
                    low=float(row.get("low", close) or close),
                    close=float(close),
                    volume=float(row.get("volume") or 0.0),
                )
            )
        if len(bars) < 2:
            return None
        engine = MarketProfileEngine(
            {
                "period_minutes": 15,
                "tick_size": spec.mp_tick_size,
                "initial_balance_periods": 4,
                "value_area_pct": 0.70,
                "min_tail_tpos": 2,
            }
        )
        try:
            return engine.build_profile(symbol=symbol, bars=bars)
        except Exception as exc:
            logger.debug(f"[CommodityStrategy] MP build failed for {symbol}: {exc}")
            return None

    def _classify_market_profile(
        self,
        *,
        profile: Any,
        current_price: float,
        session_rows: list[dict[str, Any]],
    ) -> tuple[Optional[str], str, str]:
        recent_move = 0.0
        if len(session_rows) >= 4:
            try:
                recent_move = current_price - float(session_rows[-4].get("close") or current_price)
            except Exception:
                recent_move = 0.0

        if current_price >= profile.vah and current_price >= profile.initial_balance_high and recent_move >= 0:
            return "BUY", "trend_up", "mp_trend_up"
        if current_price <= profile.val and current_price <= profile.initial_balance_low and recent_move <= 0:
            return "SELL", "trend_down", "mp_trend_down"
        if profile.poor_low and current_price >= profile.poc:
            return "BUY", "failed_auction_low", "poor_low_recovery"
        if profile.poor_high and current_price <= profile.poc:
            return "SELL", "failed_auction_high", "poor_high_reversal"
        if current_price > profile.poc and recent_move >= 0:
            return "BUY", "balance_above_poc", "holding_above_poc"
        if current_price < profile.poc and recent_move <= 0:
            return "SELL", "balance_below_poc", "holding_below_poc"
        return None, "balance", "mp_balanced"

    async def _analyze_futures_symbol(
        self,
        symbol: str,
        live_ltp: Optional[float],
    ) -> Optional[dict[str, Any]]:
        spec = get_commodity_contract_spec(symbol)
        candles = await self._load_history(symbol, interval=FUTURES_TIMEFRAME)
        if not candles:
            self._append_commentary("warning", f"{symbol}: no futures candles returned by broker.")
            return None

        analysis = evaluate_commodity_signal(candles, symbol=symbol, timeframe=FUTURES_TIMEFRAME)
        latest_close = analysis.get("latest_close")
        quote_snapshot = self._last_quote_snapshots.get(symbol) or {}
        previous_close = quote_snapshot.get("previous_close") or analysis.get("previous_close")
        price = float(live_ltp or latest_close or 0.0)
        change = quote_snapshot.get("change")
        if change is None and price and previous_close:
            change = price - float(previous_close)
        change_pct = quote_snapshot.get("change_pct")
        if change_pct is None and price and previous_close:
            change_pct = ((price - float(previous_close)) / float(previous_close)) * 100.0

        session_rows, session_date = _latest_session_rows(candles)
        profile = self._build_market_profile(symbol, session_rows)
        mp_direction = None
        mp_day_type = "balance"
        mp_reason = "mp_pending"
        mp_status = "warming_up"
        signal = None
        signal_reason = analysis.get("reason")
        entry_style: Optional[str] = "fresh_cross" if analysis.get("signal") in {"BUY", "SELL"} else None
        candidate_signal = analysis.get("signal")
        candidate_reason = analysis.get("reason")
        if candidate_signal not in {"BUY", "SELL"} and analysis.get("continuation_signal") in {"BUY", "SELL"}:
            candidate_signal = analysis.get("continuation_signal")
            candidate_reason = analysis.get("continuation_reason") or analysis.get("reason")
            entry_style = "continuation"
        validation_detail = ""
        if profile and len(session_rows) >= FUTURES_MP_MIN_PERIODS:
            mp_status = "ready"
            mp_direction, mp_day_type, mp_reason = self._classify_market_profile(
                profile=profile,
                current_price=price or float(latest_close or 0.0),
                session_rows=session_rows,
            )
            if (
                candidate_signal in {"BUY", "SELL"}
                and entry_style == "continuation"
                and mp_day_type not in {"trend_up", "trend_down"}
            ):
                validation_detail = (
                    f"15-minute continuation fired {candidate_signal}, but continuation entries require a trend-day MP gate."
                )
            elif candidate_signal in {"BUY", "SELL"} and candidate_signal == mp_direction:
                signal = candidate_signal
                signal_reason = f"{candidate_reason}_{mp_reason}"
                validation_detail = (
                    "15-minute continuation setup still aligns with the current MP gate."
                    if entry_style == "continuation"
                    else "15-minute MACD cross matches the current MP gate."
                )
            elif candidate_signal in {"BUY", "SELL"}:
                setup_label = "continuation" if entry_style == "continuation" else "MACD cross"
                validation_detail = (
                    f"15-minute {setup_label} fired {candidate_signal}, but MP gate is {mp_direction or 'neutral'}."
                )
        elif session_rows:
            validation_detail = f"Only {len(session_rows)} intraday periods are available for Market Profile."
        else:
            validation_detail = "No current-session futures rows are available for Market Profile."

        return {
            "symbol": symbol,
            "underlying": spec.root,
            "display_name": spec.display_name,
            "price": _round_or_none(price, 2),
            "previous_close": _round_or_none(previous_close, 2),
            "change": _round_or_none(change, 2),
            "change_pct": _round_or_none(change_pct, 2),
            "signal": signal,
            "raw_signal": analysis.get("signal"),
            "continuation_signal": analysis.get("continuation_signal"),
            "continuation_reason": analysis.get("continuation_reason"),
            "recent_cross_signal": analysis.get("recent_cross_signal"),
            "recent_cross_bars_ago": analysis.get("recent_cross_bars_ago"),
            "reason": signal_reason,
            "entry_style": entry_style,
            "signal_validation_detail": validation_detail,
            "regime": analysis.get("regime"),
            "macd": analysis.get("macd"),
            "macd_signal": analysis.get("macd_signal"),
            "macd_histogram": analysis.get("macd_histogram"),
            "atr": analysis.get("atr"),
            "bar_time": analysis.get("bar_time"),
            "mp_status": mp_status,
            "mp_direction": mp_direction,
            "mp_day_type": mp_day_type,
            "mp_reason": mp_reason,
            "mp_poc": _round_or_none(getattr(profile, "poc", None), 2),
            "mp_vah": _round_or_none(getattr(profile, "vah", None), 2),
            "mp_val": _round_or_none(getattr(profile, "val", None), 2),
            "mp_ib_high": _round_or_none(getattr(profile, "initial_balance_high", None), 2),
            "mp_ib_low": _round_or_none(getattr(profile, "initial_balance_low", None), 2),
            "mp_periods": getattr(profile, "period_count", len(session_rows)),
            "mp_session_date": session_date.isoformat() if session_date else None,
        }

    async def _build_option_watchlist(self) -> list[dict[str, Any]]:
        await self.ensure_selected_option_setup_locks()
        payload = await commodity_atm_watchlist_service.get_watchlist(
            self._symbols,
            self._selected_option_expiries,
            self._selected_option_lookup_symbols,
        )
        rows = list(payload.get("rows") or [])
        decorated_rows: list[dict[str, Any]] = []
        for row in rows:
            decorated = await self._analyze_option_row(row)
            if decorated:
                decorated_rows.append(decorated)
        return decorated_rows

    @staticmethod
    def _mark_retained_watchlist_row(row: dict[str, Any], *, note: str) -> dict[str, Any]:
        retained = dict(row)
        retained["runtime_retained"] = True
        retained["runtime_retention_note"] = note
        return retained

    def _stabilize_futures_watchlist(
        self,
        rows: list[dict[str, Any]],
        *,
        live_quotes: Optional[dict[str, float]] = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        previous_by_symbol = {
            str(row.get("symbol") or ""): row
            for row in self._runtime.futures_watchlist
            if str(row.get("symbol") or "").strip()
        }
        fresh_by_symbol = {
            str(row.get("symbol") or ""): row
            for row in rows
            if str(row.get("symbol") or "").strip()
        }

        stabilized: list[dict[str, Any]] = []
        retained_symbols: list[str] = []
        for symbol in self._symbols:
            active_symbol = self._active_futures_symbol(symbol)
            fresh = fresh_by_symbol.get(symbol) or fresh_by_symbol.get(active_symbol)
            if fresh is not None:
                stabilized.append(fresh)
                continue
            previous = previous_by_symbol.get(symbol) or previous_by_symbol.get(active_symbol)
            if previous is None:
                continue

            retained = self._mark_retained_watchlist_row(
                previous,
                note="Retained after a temporary futures history gap.",
            )
            quote = float((live_quotes or {}).get(active_symbol) or (live_quotes or {}).get(symbol) or 0.0)
            previous_close = retained.get("previous_close")
            if quote > 0:
                retained["price"] = _round_or_none(quote, 2)
                try:
                    prior_close = float(previous_close or 0.0)
                except (TypeError, ValueError):
                    prior_close = 0.0
                if prior_close > 0:
                    retained["change"] = _round_or_none(quote - prior_close, 2)
                    retained["change_pct"] = _round_or_none(((quote - prior_close) / prior_close) * 100.0, 2)
            stabilized.append(retained)
            retained_symbols.append(symbol)

        return stabilized, retained_symbols

    def _stabilize_option_watchlist(self, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        previous_by_symbol = {
            str(row.get("symbol") or ""): row
            for row in self._runtime.option_watchlist
            if str(row.get("symbol") or "").strip()
        }
        fresh_by_symbol = {
            str(row.get("symbol") or ""): row
            for row in rows
            if str(row.get("symbol") or "").strip()
        }

        stabilized: list[dict[str, Any]] = []
        retained_symbols: list[str] = []
        for symbol in self._symbols:
            selected_expiry = str(self._selected_option_expiries.get(symbol) or "").strip()
            selected_lookup_symbol = str(self._selected_option_lookup_symbols.get(symbol) or "").strip()
            fresh = fresh_by_symbol.get(symbol)
            if fresh is not None:
                stabilized.append(fresh)
                continue
            previous = previous_by_symbol.get(symbol)
            if previous is None or not selected_expiry:
                continue
            try:
                expiry_date = date.fromisoformat(selected_expiry)
            except ValueError:
                continue
            if expiry_date < _now_ist().date():
                continue
            previous_expiry = str(previous.get("selected_expiry") or previous.get("active_expiry") or previous.get("expiry") or "").strip()
            previous_lookup_symbol = str(
                previous.get("selected_lookup_symbol")
                or previous.get("lookup_symbol")
                or previous.get("fyers_symbol")
                or ""
            ).strip()
            if previous_expiry != selected_expiry:
                continue
            if selected_lookup_symbol and previous_lookup_symbol and previous_lookup_symbol != selected_lookup_symbol:
                continue
            stabilized.append(
                self._mark_retained_watchlist_row(
                    previous,
                    note="Retained after a temporary options history gap.",
                )
            )
            retained_symbols.append(symbol)

        return stabilized, retained_symbols

    @staticmethod
    def _overlay_live_option_quotes(
        rows: list[dict[str, Any]],
        option_quote_map: dict[str, float],
    ) -> list[dict[str, Any]]:
        if not option_quote_map:
            return rows

        enriched_rows: list[dict[str, Any]] = []
        for row in rows:
            enriched = dict(row)
            ce_symbol = str(enriched.get("ce_symbol") or "")
            pe_symbol = str(enriched.get("pe_symbol") or "")
            trade_symbol = str(enriched.get("trade_symbol") or "")

            ce_quote = float(option_quote_map.get(ce_symbol) or 0.0) if ce_symbol else 0.0
            pe_quote = float(option_quote_map.get(pe_symbol) or 0.0) if pe_symbol else 0.0
            trade_quote = float(option_quote_map.get(trade_symbol) or 0.0) if trade_symbol else 0.0

            if ce_quote > 0:
                enriched["ce_trade_price"] = _round_or_none(ce_quote, 2)
                if isinstance(enriched.get("ce"), dict):
                    enriched["ce"] = {**dict(enriched["ce"]), "live_ltp": ce_quote, "price_source": "direct_ltp"}
            elif isinstance(enriched.get("ce"), dict):
                enriched["ce"] = {**dict(enriched["ce"]), "price_source": "chain_ltp"}

            if pe_quote > 0:
                enriched["pe_trade_price"] = _round_or_none(pe_quote, 2)
                if isinstance(enriched.get("pe"), dict):
                    enriched["pe"] = {**dict(enriched["pe"]), "live_ltp": pe_quote, "price_source": "direct_ltp"}
            elif isinstance(enriched.get("pe"), dict):
                enriched["pe"] = {**dict(enriched["pe"]), "price_source": "chain_ltp"}

            if trade_quote > 0:
                enriched["trade_price"] = _round_or_none(trade_quote, 2)
                enriched["trade_price_source"] = "direct_ltp"
            else:
                enriched["trade_price_source"] = "chain_ltp"

            enriched_rows.append(enriched)

        return enriched_rows

    async def _prepare_closed_market_state(self, started_at: datetime) -> dict[str, Any]:
        """Refresh commodity watchlists outside MCX hours without opening entries."""
        await ensure_fyers_session(force_validate=False)
        await ensure_upstox_session(force_validate=False)
        fyers_health = await get_fyers_token_health(force=False)
        upstox_health = await get_upstox_token_health(force=False)
        self._last_data_health = {
            "fyers_token_health": fyers_health,
            "upstox_token_health": upstox_health,
            "option_history": option_history_service.get_health_snapshot(),
            "mode": "closed_market_preparation",
        }
        if not fyers_health.get("valid") and not upstox_health.get("valid"):
            message = self._commodity_broker_failure_message(fyers_health, upstox_health)
            self._last_error = message
            self._last_message = message
            self._append_commentary("error", message)
            return self.get_status(refresh=False)

        adapter = await self._get_scan_adapter()
        if not adapter:
            self._append_commentary(
                "warning",
                "No commodity broker adapter is available. Preparing from MCX futures history and cached option watchlists.",
            )

        await self.ensure_selected_option_setup_locks()
        active_futures_symbols = self._active_futures_symbols()
        quote_map = await self._safe_get_ltp(adapter, sorted(set(active_futures_symbols.values())))
        futures_quote_map = dict(quote_map)
        for configured_symbol, active_symbol in active_futures_symbols.items():
            if active_symbol in quote_map:
                futures_quote_map.setdefault(configured_symbol, quote_map[active_symbol])
        data_quality_snapshot: dict[str, Any] | None = None
        try:
            from market_data.data_quality_agent import data_quality_agent

            for symbol, quote in futures_quote_map.items():
                if quote is not None:
                    data_quality_agent.record_tick(
                        symbol=symbol,
                        source="broker_futures_quote",
                        observed_at=started_at,
                        last_value=float(quote),
                    )
            data_quality_snapshot = data_quality_agent.snapshot()
        except Exception as exc:
            data_quality_snapshot = {"overall": "unknown", "error": str(exc)}
        self._last_data_health["data_quality"] = data_quality_snapshot
        self._last_data_health["commodity_data_quality"] = self._commodity_data_quality_summary(
            data_quality_snapshot,
            futures_quote_map,
        )
        futures_rows: list[dict[str, Any]] = []
        for configured_symbol, active_symbol in active_futures_symbols.items():
            row = await self._analyze_futures_symbol(active_symbol, quote_map.get(active_symbol))
            if row:
                row["configured_symbol"] = configured_symbol
                if active_symbol != configured_symbol:
                    row["active_lookup_symbol"] = active_symbol
                    row["rollover_detail"] = f"Scanning active futures {active_symbol} for configured {configured_symbol}."
                prepared_row = dict(row)
                prepared_row["preparation_mode"] = "closed_market"
                prepared_row["can_enter"] = False
                futures_rows.append(prepared_row)
        futures_rows = self._decorate_futures_rows(futures_rows)
        futures_rows, retained_futures = self._stabilize_futures_watchlist(
            futures_rows,
            live_quotes=futures_quote_map,
        )

        option_rows = self._decorate_option_rows(await self._build_option_watchlist())
        option_rows, retained_options = self._stabilize_option_watchlist(option_rows)
        option_symbols_to_quote = sorted(
            {
                str(symbol).strip()
                for row in option_rows
                for symbol in (row.get("ce_symbol"), row.get("pe_symbol"))
                if str(symbol or "").strip()
            }
        )
        option_quote_map = (
            await self._safe_get_ltp(adapter, option_symbols_to_quote)
            if option_symbols_to_quote
            else {}
        )
        option_rows = self._overlay_live_option_quotes(option_rows, option_quote_map)
        option_rows = [
            {
                **dict(row),
                "preparation_mode": "closed_market",
                "can_enter": False,
            }
            for row in option_rows
        ]

        latest_prices = {
            row["symbol"]: float(row["price"])
            for row in futures_rows
            if row.get("price") is not None
        }
        for row in option_rows:
            for symbol_key, price_key in (("ce_symbol", "ce_trade_price"), ("pe_symbol", "pe_trade_price")):
                live_symbol = str(row.get(symbol_key) or "")
                live_price = row.get(price_key)
                if live_symbol and live_price is not None:
                    latest_prices[live_symbol] = float(live_price)
        if latest_prices:
            self._runtime.portfolio.update_prices(latest_prices)

        self._runtime.futures_watchlist = futures_rows
        self._runtime.option_watchlist = option_rows
        self._last_run_at = started_at.isoformat()
        self._last_error = None
        option_history_health = option_history_service.get_health_snapshot()
        self._last_data_health = {
            "fyers_token_health": fyers_health,
            "upstox_token_health": upstox_health,
            "option_history": option_history_health,
            "mode": "closed_market_preparation",
            "data_quality": data_quality_snapshot,
            "commodity_data_quality": self._commodity_data_quality_summary(
                data_quality_snapshot,
                futures_quote_map,
                option_quote_map,
            ),
        }
        self._last_message = (
            f"Market closed. Prepared for next MCX session: {len(futures_rows)} futures rows "
            f"and {len(option_rows)} option rows. No commodity entries are opened while MCX is closed."
        )
        retention_parts: list[str] = []
        if retained_futures:
            retention_parts.append(f"retained {len(retained_futures)} futures rows")
        if retained_options:
            retention_parts.append(f"retained {len(retained_options)} option rows")
        if retention_parts:
            self._last_message = f"{self._last_message} Reused the last good snapshot for {', '.join(retention_parts)}."
        health_warning = self._option_history_warning(option_history_health)
        if health_warning:
            self._last_message = f"{self._last_message} {health_warning}"
            self._append_commentary("warning", health_warning)
        self._append_commentary("info", self._last_message)
        return self.get_status(refresh=False)

    async def _analyze_option_row(self, row: dict[str, Any]) -> Optional[dict[str, Any]]:
        symbol = str(row.get("symbol") or "")
        underlying = str(row.get("underlying") or get_commodity_display_name(symbol))
        spec = get_commodity_contract_spec(symbol)
        expiry_text = str(row.get("expiry") or "")
        try:
            expiry_date = date.fromisoformat(expiry_text)
        except ValueError:
            return None

        ce = dict(row.get("ce") or {})
        pe = dict(row.get("pe") or {})
        ce_candles: list[dict[str, Any]] = []
        pe_candles: list[dict[str, Any]] = []
        if ce.get("instrument_key"):
            ce_candles = await option_history_service.load_candles(
                underlying=underlying,
                expiry=expiry_date,
                strike=float(ce.get("strike") or 0.0),
                option_type="CE",
                instrument_key=ce.get("instrument_key"),
                interval=OPTIONS_TIMEFRAME,
                limit=96,
            )
        if pe.get("instrument_key"):
            pe_candles = await option_history_service.load_candles(
                underlying=underlying,
                expiry=expiry_date,
                strike=float(pe.get("strike") or 0.0),
                option_type="PE",
                instrument_key=pe.get("instrument_key"),
                interval=OPTIONS_TIMEFRAME,
                limit=96,
            )
        ce_closes = [float(item["close"]) for item in ce_candles if item.get("close") is not None]
        pe_closes = [float(item["close"]) for item in pe_candles if item.get("close") is not None]
        ce_analysis = evaluate_commodity_signal(
            ce_candles,
            symbol=str(ce.get("instrument_key") or ce.get("trading_symbol") or f"{underlying}:CE"),
            timeframe=OPTIONS_TIMEFRAME,
            fast=OPTIONS_MACD_FAST,
            slow=OPTIONS_MACD_SLOW,
            signal_period=OPTIONS_MACD_SIGNAL,
        ) if ce_candles else {"signal": None, "reason": "missing", "regime": "unknown", "bar_time": None}
        pe_analysis = evaluate_commodity_signal(
            pe_candles,
            symbol=str(pe.get("instrument_key") or pe.get("trading_symbol") or f"{underlying}:PE"),
            timeframe=OPTIONS_TIMEFRAME,
            fast=OPTIONS_MACD_FAST,
            slow=OPTIONS_MACD_SLOW,
            signal_period=OPTIONS_MACD_SIGNAL,
        ) if pe_candles else {"signal": None, "reason": "missing", "regime": "unknown", "bar_time": None}
        ce_indicators = latest_macd_rsi(ce_closes) if ce_closes else {"macd": None, "macd_signal": None, "macd_histogram": None, "rsi": None}
        pe_indicators = latest_macd_rsi(pe_closes) if pe_closes else {"macd": None, "macd_signal": None, "macd_histogram": None, "rsi": None}

        ce.update(ce_indicators)
        pe.update(pe_indicators)
        ce["indicator_timeframe"] = OPTIONS_TIMEFRAME
        pe["indicator_timeframe"] = OPTIONS_TIMEFRAME
        ce["bar_time"] = ce_analysis.get("bar_time")
        pe["bar_time"] = pe_analysis.get("bar_time")
        ce["zero_cross"] = "fresh_up" if ce_analysis.get("signal") == "BUY" else "above_zero" if (ce_indicators.get("macd") or 0) > 0 else "below_zero"
        pe["zero_cross"] = "fresh_down" if pe_analysis.get("signal") == "SELL" else "below_zero" if (pe_indicators.get("macd") or 0) < 0 else "above_zero"

        ce_macd = ce_indicators.get("macd")
        pe_macd = pe_indicators.get("macd")
        regime = "warmup"
        if ce_macd is not None and pe_macd is not None:
            if ce_macd >= 0 and pe_macd < 0:
                regime = "bullish"
            elif ce_macd < 0 and pe_macd >= 0:
                regime = "bearish"
            elif ce_macd < 0 and pe_macd < 0:
                regime = "dead_zone"
            else:
                regime = "vol_spike"

        signal_side: Optional[str] = None
        signal_reason = "waiting_cross"
        selected_side: dict[str, Any] | None = None
        ce_cross = ce_analysis.get("signal") == "BUY"
        pe_cross = pe_analysis.get("signal") == "SELL"
        if ce_cross and pe_cross:
            ce_strength = abs(float(ce_indicators.get("macd") or 0.0))
            pe_strength = abs(float(pe_indicators.get("macd") or 0.0))
            if ce_strength >= pe_strength:
                signal_side = "CE"
                signal_reason = "ce_macd_zero_cross_stronger_than_pe"
                selected_side = ce
            else:
                signal_side = "PE"
                signal_reason = "pe_macd_zero_cross_stronger_than_ce"
                selected_side = pe
        elif ce_cross:
            signal_side = "CE"
            signal_reason = "ce_macd_zero_cross"
            selected_side = ce
        elif pe_cross:
            signal_side = "PE"
            signal_reason = "pe_macd_zero_cross"
            selected_side = pe

        trade_price = 0.0
        trade_symbol = ""
        trade_strike: Optional[float] = None
        trade_bar_time: Optional[str] = None
        entry_iv_pct: Optional[float] = None
        iv_sizing_mode = "unknown"
        lots_affordable = 0
        capital_per_trade = _normalized_option_budget_cap(
            self._runtime.portfolio.initial_capital,
            self._runtime.portfolio.available_capital,
        )
        is_trade_contract_liquid = False
        ce_symbol = str(ce.get("instrument_key") or ce.get("trading_symbol") or "")
        pe_symbol = str(pe.get("instrument_key") or pe.get("trading_symbol") or "")
        ce_trade_price = float(ce.get("ltp") or ce_analysis.get("latest_close") or 0.0) if ce else 0.0
        pe_trade_price = float(pe.get("ltp") or pe_analysis.get("latest_close") or 0.0) if pe else 0.0
        if selected_side:
            trade_price = float(selected_side.get("ltp") or 0.0)
            live_close = ce_analysis.get("latest_close") if signal_side == "CE" else pe_analysis.get("latest_close")
            if not trade_price and live_close:
                trade_price = float(live_close)
            trade_symbol = str(selected_side.get("instrument_key") or selected_side.get("trading_symbol") or "")
            trade_strike = float(selected_side.get("strike")) if selected_side.get("strike") is not None else None
            trade_bar_time = str(selected_side.get("bar_time") or "")
            entry_iv_pct = _normalize_iv_pct(
                selected_side.get("iv")
                or selected_side.get("iv_pct")
                or selected_side.get("implied_volatility")
                or selected_side.get("implied_vol")
                or selected_side.get("atm_iv")
            )
            cost_per_lot = trade_price * max(spec.futures_lot_size, 1)
            lots_affordable = int(capital_per_trade // cost_per_lot) if cost_per_lot > 0 else 0
            if entry_iv_pct is not None and entry_iv_pct > OPTIONS_IV_HALF_SIZE_PCT:
                iv_sizing_mode = "reject" if entry_iv_pct > OPTIONS_IV_REJECT_PCT else "half_size"
                lots_affordable = max(0, lots_affordable // 2)
            elif entry_iv_pct is not None:
                iv_sizing_mode = "normal"
            is_trade_contract_liquid = bool(selected_side.get("is_liquid"))

        return {
            **row,
            "display_name": spec.display_name,
            "contract_unit_label": spec.contract_unit_label,
            "quote_unit_label": spec.quote_unit_label,
            "regime": regime,
            "indicator_timeframe": OPTIONS_TIMEFRAME,
            "signal_side": signal_side,
            "signal_reason": signal_reason,
            "ce_cross": ce_cross,
            "pe_cross": pe_cross,
            "trade_symbol": trade_symbol,
            "trade_strike": trade_strike,
            "trade_price": _round_or_none(trade_price, 2),
            "trade_bar_time": trade_bar_time,
            "entry_iv_pct": entry_iv_pct,
            "iv_sizing_mode": iv_sizing_mode,
            "ce_symbol": ce_symbol,
            "pe_symbol": pe_symbol,
            "ce_trade_price": _round_or_none(ce_trade_price, 2),
            "pe_trade_price": _round_or_none(pe_trade_price, 2),
            "capital_per_trade": _round_or_none(capital_per_trade, 2),
            "lots_affordable": lots_affordable,
            "is_trade_contract_liquid": is_trade_contract_liquid,
            "ce": ce,
            "pe": pe,
        }

    async def run_once(self, *, force: bool = False) -> dict[str, Any]:
        self._refresh_state_from_store()
        if self._lock.locked() and not force:
            return self.get_status(refresh=False)

        async with self._lock:
            self._running = True
            started_at = _now_ist()
            self._last_error = None
            option_history_service.reset_health()
            try:
                if not self._symbols:
                    self._last_message = "Configure MCX symbols to start the commodity agent."
                    self._append_commentary("warning", self._last_message)
                    return self.get_status(refresh=False)

                if not force and not _in_commodity_hours(started_at):
                    self._append_commentary("idle", "Commodity market closed. Refreshing next-session preparation state.")
                    return await self._prepare_closed_market_state(started_at)

                await ensure_fyers_session(force_validate=False)
                await ensure_upstox_session(force_validate=False)
                fyers_health = await get_fyers_token_health(force=False)
                upstox_health = await get_upstox_token_health(force=False)
                self._last_data_health = {
                    "fyers_token_health": fyers_health,
                    "upstox_token_health": upstox_health,
                    "option_history": option_history_service.get_health_snapshot(),
                }
                if not fyers_health.get("valid") and not upstox_health.get("valid"):
                    message = self._commodity_broker_failure_message(fyers_health, upstox_health)
                    self._last_error = message
                    self._last_message = message
                    self._append_commentary("error", message)
                    return self.get_status(refresh=False)

                adapter = await self._get_scan_adapter()
                if not adapter:
                    self._append_commentary(
                        "warning",
                        "No commodity broker adapter is available. Scanning from MCX futures history and cached option watchlists.",
                    )

                await self.ensure_selected_option_setup_locks()
                active_futures_symbols = self._active_futures_symbols()
                quote_map = await self._safe_get_ltp(adapter, sorted(set(active_futures_symbols.values())))
                futures_quote_map = dict(quote_map)
                for configured_symbol, active_symbol in active_futures_symbols.items():
                    if active_symbol in quote_map:
                        futures_quote_map.setdefault(configured_symbol, quote_map[active_symbol])
                data_quality_snapshot: dict[str, Any] | None = None
                try:
                    from market_data.data_quality_agent import data_quality_agent

                    for symbol, quote in futures_quote_map.items():
                        if quote is not None:
                            # MCX futures use a dedicated source so the
                            # 90s budget matches the 30s scan cadence
                            # without false-flagging stale every cycle.
                            data_quality_agent.record_tick(
                                symbol=symbol,
                                source="broker_futures_quote",
                                observed_at=started_at,
                                last_value=float(quote),
                            )
                    data_quality_snapshot = data_quality_agent.snapshot()
                    self._last_data_health["data_quality"] = data_quality_snapshot
                except Exception as exc:
                    data_quality_snapshot = {"overall": "unknown", "error": str(exc)}
                    self._last_data_health["data_quality"] = data_quality_snapshot
                self._last_data_health["commodity_data_quality"] = self._commodity_data_quality_summary(
                    data_quality_snapshot,
                    futures_quote_map,
                )
                commodity_quality_blocked, commodity_quality_reason = self._commodity_futures_quality_blocked(
                    data_quality_snapshot,
                    futures_quote_map,
                )
                if settings.DATA_QUALITY_SCAN_GATE_ENABLED and commodity_quality_blocked and adapter is not None:
                    message = (
                        "Data quality gate blocked the commodity scan: "
                        f"{commodity_quality_reason}."
                    )
                    self._last_error = message
                    self._last_message = message
                    self._append_commentary("warning", message)
                    return self.get_status(refresh=False)
                futures_rows: list[dict[str, Any]] = []
                for configured_symbol, active_symbol in active_futures_symbols.items():
                    row = await self._analyze_futures_symbol(active_symbol, quote_map.get(active_symbol))
                    if row:
                        row["configured_symbol"] = configured_symbol
                        if active_symbol != configured_symbol:
                            row["active_lookup_symbol"] = active_symbol
                            row["rollover_detail"] = f"Scanning active futures {active_symbol} for configured {configured_symbol}."
                        futures_rows.append(row)
                futures_rows = self._decorate_futures_rows(futures_rows)
                futures_rows, retained_futures = self._stabilize_futures_watchlist(
                    futures_rows,
                    live_quotes=futures_quote_map,
                )
                self._audit_futures_watchlist(futures_rows)
                # Unified commodity data path: every scan, push fresh 1-minute
                # bars for each configured commodity into underlying_spot_candles
                # so every downstream strategy (directional long options, MP,
                # auction intelligence, future agents) reads the same table
                # instead of each one hitting the broker independently.
                # Fire-and-forget — the persist runs in the background and
                # never blocks the scan cycle. The persist code itself uses
                # an in-memory "latest persisted" cache so each call only
                # writes the bars that have arrived since the last persist.
                try:
                    from market_data.commodity_runtime_history import load_commodity_history_rows
                    from market_data.commodity_contract_specs import extract_commodity_root

                    seen_roots: set[str] = set()
                    for symbol in self._symbols:
                        root = (extract_commodity_root(symbol) or "").upper()
                        if not root or root in seen_roots:
                            continue
                        seen_roots.add(root)
                        # 2-day lookback keeps the call fast after the initial
                        # sync; the first cold call still backfills 10 days
                        # because _LATEST_PERSISTED is empty.
                        asyncio.create_task(
                            load_commodity_history_rows(root, interval="1minute", lookback_days=2),
                            name=f"commodity-unified-persist-{root}",
                        )
                except Exception as exc:
                    logger.debug(
                        f"[CommodityStrategy] unified 1-min persist hook skipped: {exc}"
                    )

                option_rows = self._decorate_option_rows(await self._build_option_watchlist())
                option_rows, retained_options = self._stabilize_option_watchlist(option_rows)
                option_symbols_to_quote = sorted(
                    {
                        str(symbol).strip()
                        for row in option_rows
                        for symbol in (row.get("ce_symbol"), row.get("pe_symbol"))
                        if str(symbol or "").strip()
                    }
                    | {
                        str(pos.live_symbol).strip()
                        for pos in self._runtime.positions.values()
                        if pos.strategy_key == "commodity_options" and str(pos.live_symbol or "").strip()
                    }
                )
                option_quote_map = (
                    await self._safe_get_ltp(adapter, option_symbols_to_quote)
                    if option_symbols_to_quote
                    else {}
                )
                if option_quote_map:
                    try:
                        from market_data.data_quality_agent import data_quality_agent

                        for symbol, quote in option_quote_map.items():
                            if quote is not None:
                                # Use a separate source name for option contracts so
                                # the freshness budget (5 min) matches the watchlist
                                # refresh cadence rather than the 30s futures budget.
                                data_quality_agent.record_tick(
                                    symbol=symbol,
                                    source="broker_option_quote",
                                    observed_at=started_at,
                                    last_value=float(quote),
                                )
                        data_quality_snapshot = data_quality_agent.snapshot()
                    except Exception as exc:
                        data_quality_snapshot = {"overall": "unknown", "error": str(exc)}
                option_rows = self._overlay_live_option_quotes(option_rows, option_quote_map)

                latest_prices = {
                    row["symbol"]: float(row["price"])
                    for row in futures_rows
                    if row.get("price") is not None
                }
                for row in option_rows:
                    for symbol_key, price_key in (("ce_symbol", "ce_trade_price"), ("pe_symbol", "pe_trade_price")):
                        live_symbol = str(row.get(symbol_key) or "")
                        live_price = row.get(price_key)
                        if live_symbol and live_price is not None:
                            latest_prices[live_symbol] = float(live_price)
                if latest_prices:
                    self._runtime.portfolio.update_prices(latest_prices)

                await self._manage_positions(adapter, futures_rows, option_rows, option_quote_map=option_quote_map)
                current_drawdown_pct = self._current_drawdown_pct()
                if current_drawdown_pct >= COMMODITY_MAX_DRAWDOWN_PCT:
                    await self._record_drawdown_risk_block(drawdown_pct=current_drawdown_pct)
                if self._kill_switch_active:
                    actionable_futures = [row for row in futures_rows if row.get("signal_validation") == "ready"]
                    actionable_options = [row for row in option_rows if row.get("signal_validation") == "ready"]
                    total_actionable = len(actionable_futures) + len(actionable_options)
                    if total_actionable:
                        self._append_commentary(
                            "warning",
                            f"Commodity kill switch active. {total_actionable} actionable signals observed, but no new entries were placed.",
                        )
                else:
                    lane_rows = {
                        "commodity_futures": futures_rows,
                        "commodity_options": option_rows,
                    }
                    for lane in self._strategy_agents():
                        await lane.run_entries(lane_rows.get(lane.descriptor.key, []))

                self._runtime.futures_watchlist = futures_rows
                self._runtime.option_watchlist = option_rows

                self._last_run_at = started_at.isoformat()
                open_positions = len(self._runtime.positions)
                option_history_health = option_history_service.get_health_snapshot()
                self._last_data_health = {
                    "fyers_token_health": fyers_health,
                    "upstox_token_health": upstox_health,
                    "option_history": option_history_health,
                    "data_quality": data_quality_snapshot,
                    "commodity_data_quality": self._commodity_data_quality_summary(
                        data_quality_snapshot,
                        futures_quote_map,
                        option_quote_map,
                    ),
                }
                self._last_message = (
                    f"Scanned {len(futures_rows)} futures rows and {len(option_rows)} option rows. "
                    f"{open_positions} open positions."
                )
                if retained_futures or retained_options:
                    retention_parts: list[str] = []
                    if retained_futures:
                        retention_parts.append(f"retained {len(retained_futures)} futures rows")
                    if retained_options:
                        retention_parts.append(f"retained {len(retained_options)} option rows")
                    self._last_message = f"{self._last_message} Reused the last good snapshot for {', '.join(retention_parts)}."
                health_warning = self._option_history_warning(option_history_health)
                if health_warning:
                    self._last_message = f"{self._last_message} {health_warning}"
                    self._append_commentary("warning", health_warning)
                tone = "success"
                if not futures_rows and not option_rows:
                    tone = "warning"
                if health_warning:
                    tone = "warning"
                self._append_commentary(tone, self._last_message)
                self._append_report()
                return self.get_status(refresh=False)
            except Exception as exc:
                self._last_error = str(exc)
                self._last_message = f"Commodity strategy error: {exc}"
                self._append_commentary("error", self._last_message)
                raise
            finally:
                self._running = False
                self._persist_state()

    async def _safe_get_ltp(self, adapter: Optional[BrokerAdapter], symbols: list[str]) -> dict[str, float]:
        try:
            quote_snapshots = await load_upstox_mcx_quote_snapshots(symbols)
        except Exception as exc:
            logger.debug(f"[CommodityStrategy] Upstox quote snapshot fetch skipped: {exc}")
            quote_snapshots = {}
        if quote_snapshots:
            self._last_quote_snapshots.update(quote_snapshots)
            quotes = {
                symbol: float(snapshot.get("price") or 0.0)
                for symbol, snapshot in quote_snapshots.items()
                if float(snapshot.get("price") or 0.0) > 0
            }
        else:
            quotes = {}
        if not quotes:
            quotes = await load_upstox_mcx_quotes(symbols)
        for symbol, value in quotes.items():
            self._last_quote_snapshots.setdefault(symbol, {"price": value, "source": "upstox_ltp"})
        remaining_symbols = [symbol for symbol in symbols if symbol not in quotes]
        if not remaining_symbols:
            return quotes
        if adapter is None:
            return quotes
        now = datetime.now(IST)
        if self._fyers_ltp_backoff_until is not None and now < self._fyers_ltp_backoff_until:
            return quotes
        try:
            payload = await adapter.get_ltp(remaining_symbols)
        except Exception as exc:
            if _is_rate_limit_error(exc):
                self._fyers_ltp_backoff_until = now + timedelta(
                    seconds=max(int(settings.COMMODITY_FYERS_RATE_LIMIT_BACKOFF_SECONDS), 15)
                )
            logger.warning(f"[CommodityStrategy] LTP fetch failed: {exc}")
            self._append_commentary("warning", f"Live LTP fetch failed. Using candle closes where available. ({exc})")
            return quotes
        self._fyers_ltp_backoff_until = None
        for symbol in remaining_symbols:
            try:
                value = float(payload.get(symbol, 0) or 0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                quotes[symbol] = value
                self._last_quote_snapshots.setdefault(symbol, {"price": value, "source": "broker_ltp"})
        return quotes

    async def _manage_positions(
        self,
        adapter: Optional[BrokerAdapter],
        futures_rows: list[dict[str, Any]],
        option_rows: list[dict[str, Any]],
        option_quote_map: Optional[dict[str, float]] = None,
    ) -> None:
        futures_map = {str(row["symbol"]): row for row in futures_rows}
        option_map: dict[str, dict[str, Any]] = {}
        for row in option_rows:
            for symbol_key in ("ce_symbol", "pe_symbol"):
                live_symbol = str(row.get(symbol_key) or "")
                if live_symbol:
                    option_map[live_symbol] = row
        live_option_quotes = dict(option_quote_map or {})
        missing_option_symbols = [
            pos.live_symbol
            for pos in self._runtime.positions.values()
            if pos.strategy_key == "commodity_options" and pos.live_symbol not in live_option_quotes
        ]
        if missing_option_symbols:
            live_option_quotes.update(await self._safe_get_ltp(adapter, missing_option_symbols))

        for position_key, position in list(self._runtime.positions.items()):
            reason: Optional[str] = None
            if position.strategy_key == "commodity_futures":
                row = futures_map.get(position.symbol)
                if not row:
                    continue
                current_price = float(row.get("price") or position.current_price)
                position.current_price = current_price
                position.macd_value = row.get("macd")
                position.mp_poc = row.get("mp_poc")
                position.mp_vah = row.get("mp_vah")
                position.mp_val = row.get("mp_val")
                row_bar_time = str(row.get("bar_time") or "")
                if row_bar_time:
                    position.last_reviewed_bar_time = row_bar_time
                if position.action == "BUY":
                    position.peak_price = max(position.peak_price, current_price)
                else:
                    favorable_anchor = position.peak_price if position.peak_price > 0 else position.entry_price
                    position.peak_price = min(favorable_anchor, current_price)

                risk_distance = 0.0
                if position.target_price is not None and FUTURES_TARGET_ARM_R_MULTIPLIER > 0:
                    risk_distance = abs(position.target_price - position.entry_price) / FUTURES_TARGET_ARM_R_MULTIPLIER
                if risk_distance <= 0:
                    risk_distance = abs(position.entry_price - position.stop_price)
                hold_bars = _bars_between(
                    position.entry_bar_time,
                    row_bar_time or position.last_reviewed_bar_time,
                    interval=FUTURES_TIMEFRAME,
                )
                trailing_label: Optional[str] = None
                if risk_distance > 0:
                    if position.action == "BUY":
                        favorable_move = current_price - position.entry_price
                        if favorable_move >= risk_distance * FUTURES_BREAK_EVEN_R_MULTIPLIER:
                            position.stop_price = max(position.stop_price, round(position.entry_price, 2))
                        # +1.5R intermediate lock: stop = entry + 0.5R. Activates
                        # before full target_reached so trades retracing from
                        # +1.5R back toward entry exit with locked profit
                        # instead of break-even.
                        if (
                            not position.target_reached
                            and not position.partial_lock_armed
                            and favorable_move >= risk_distance * FUTURES_PARTIAL_LOCK_R_MULTIPLIER
                        ):
                            position.partial_lock_armed = True
                            position.stop_price = max(
                                position.stop_price,
                                round(position.entry_price + (risk_distance * 0.5), 2),
                            )
                            self._append_commentary(
                                "info",
                                f"{position.display_name}: 1.5R partial lock armed (+0.5R secured).",
                            )
                        if not position.target_reached and position.target_price is not None and current_price >= position.target_price:
                            position.target_reached = True
                            position.stop_price = max(
                                position.stop_price,
                                round(position.entry_price + (risk_distance * 0.5), 2),
                            )
                            self._append_commentary(
                                "info",
                                f"{position.display_name}: 2R reached, runner trail armed from {row_bar_time or 'current bar'}.",
                            )
                        if position.target_reached:
                            trail_buffer = max(float(position.atr or 0.0) * FUTURES_TRAIL_ATR_MULTIPLIER, risk_distance)
                            position.stop_price = max(position.stop_price, round(position.peak_price - trail_buffer, 2))
                            trailing_label = "trail_stop"
                    else:
                        favorable_move = position.entry_price - current_price
                        if favorable_move >= risk_distance * FUTURES_BREAK_EVEN_R_MULTIPLIER:
                            position.stop_price = min(position.stop_price, round(position.entry_price, 2))
                        # +1.5R intermediate lock — SELL side.
                        if (
                            not position.target_reached
                            and not position.partial_lock_armed
                            and favorable_move >= risk_distance * FUTURES_PARTIAL_LOCK_R_MULTIPLIER
                        ):
                            position.partial_lock_armed = True
                            position.stop_price = min(
                                position.stop_price,
                                round(position.entry_price - (risk_distance * 0.5), 2),
                            )
                            self._append_commentary(
                                "info",
                                f"{position.display_name}: 1.5R partial lock armed (+0.5R secured).",
                            )
                        if not position.target_reached and position.target_price is not None and current_price <= position.target_price:
                            position.target_reached = True
                            position.stop_price = min(
                                position.stop_price,
                                round(position.entry_price - (risk_distance * 0.5), 2),
                            )
                            self._append_commentary(
                                "info",
                                f"{position.display_name}: 2R reached, runner trail armed from {row_bar_time or 'current bar'}.",
                            )
                        if position.target_reached:
                            trail_buffer = max(float(position.atr or 0.0) * FUTURES_TRAIL_ATR_MULTIPLIER, risk_distance)
                            position.stop_price = min(position.stop_price, round(position.peak_price + trail_buffer, 2))
                            trailing_label = "trail_stop"

                if position.action == "BUY":
                    if current_price <= position.stop_price:
                        reason = trailing_label or "stop_loss"
                    elif hold_bars >= FUTURES_MIN_HOLD_BARS and row.get("raw_signal") == "SELL":
                        reason = "macd_reversal"
                else:
                    if current_price >= position.stop_price:
                        reason = trailing_label or "stop_loss"
                    elif hold_bars >= FUTURES_MIN_HOLD_BARS and row.get("raw_signal") == "BUY":
                        reason = "macd_reversal"

                if reason:
                    exit_action = "SELL" if position.action == "BUY" else "BUY"
                    order = self._runtime.order_book.place_order(
                        symbol=position.live_symbol,
                        action=exit_action,
                        order_type="MARKET",
                        qty=position.qty,
                        instrument_type="FUT",
                        session_id=self._runtime.portfolio.session_id,
                        ltp=current_price,
                    )
                    self._record_order(
                        order,
                        reason,
                        flow="exit",
                        lot_size=position.lot_size,
                        lots=position.lots,
                        strategy_key=position.strategy_key,
                        strategy_title=position.strategy_title,
                    )
                    self._append_commentary(
                        "trade",
                        f"EXIT {position.display_name} {exit_action} @{current_price:.2f} ({reason}) | {position.lots} lot",
                    )
                    multiplier = 1 if position.action == "BUY" else -1
                    realized_pnl = multiplier * (current_price - position.entry_price) * position.qty
                    await record_audit_event(
                        market="commodity",
                        strategy_key=position.strategy_key,
                        event_type="position_exit",
                        actor="strategy_agent",
                        symbol=position.symbol,
                        underlying=position.underlying,
                        severity="trade",
                        message=(
                            f"{position.display_name} {exit_action} @ ₹{current_price:,.2f} "
                            f"({reason}); P&L ₹{realized_pnl:,.0f}"
                        ),
                        previous_state="open",
                        new_state="closed",
                        payload={
                            "reason": reason,
                            "entry_price": round(position.entry_price, 2),
                            "exit_price": round(current_price, 2),
                            "qty": position.qty,
                            "lots": position.lots,
                            "realized_pnl": round(realized_pnl, 2),
                            "return_pct": round(position.return_pct, 2),
                        },
                    )
                    self._runtime.positions.pop(position_key, None)
                continue

            row = option_map.get(position.live_symbol)
            price_key = "ce_trade_price" if position.option_type == "CE" else "pe_trade_price"
            current_price = float(
                live_option_quotes.get(position.live_symbol)
                or (row or {}).get(price_key)
                or position.current_price
            )
            if current_price <= 0:
                continue
            position.current_price = current_price
            position.peak_price = max(position.peak_price, current_price)
            side_payload = None
            if row and position.option_type == "CE":
                side_payload = row.get("ce")
            elif row and position.option_type == "PE":
                side_payload = row.get("pe")
            option_macd = (side_payload or {}).get("macd")
            position.macd_value = option_macd
            position.regime = str((row or {}).get("regime") or position.regime)

            return_pct = position.return_pct
            if current_price <= position.stop_price:
                reason = "hard_stop"
            elif not position.target_reached and position.target_price is not None and current_price >= position.target_price:
                half_lots = position.lots // 2
                exit_lots = half_lots if half_lots > 0 else position.lots
                exit_qty = exit_lots * position.lot_size
                if exit_qty >= position.qty:
                    await self._close_option_position(position_key, position, current_price, "target_hit", position.qty)
                else:
                    await self._close_option_position(position_key, position, current_price, "target_partial", exit_qty, keep_open=True)
                    position.target_reached = True
                    position.stop_price = max(position.stop_price, position.entry_price)
                continue
            elif position.target_reached and position.peak_price >= position.entry_price * (1 + (OPTIONS_RUNNER_ARM_PCT / 100.0)):
                trail_floor = position.peak_price * (1 - (OPTIONS_RUNNER_TRAIL_PCT / 100.0))
                if current_price <= trail_floor:
                    reason = "runner_trail_stop"
            elif return_pct >= OPTIONS_RUNNER_MACD_EXIT_PROFIT_PCT:
                if position.option_type == "CE" and option_macd is not None and option_macd < 0:
                    reason = "macd_reversal"
                elif position.option_type == "PE" and option_macd is not None and option_macd > 0:
                    reason = "macd_reversal"

            try:
                expiry_date = date.fromisoformat(str(position.expiry or ""))
                if expiry_date <= (_now_ist().date() + timedelta(days=1)):
                    reason = reason or "expiry_guard"
            except ValueError:
                pass

            if position.regime == "dead_zone":
                reason = reason or "dead_zone"

            if reason:
                await self._close_option_position(position_key, position, current_price, reason, position.qty)

    async def _close_futures_position(
        self,
        position_key: str,
        position: CommodityPositionState,
        current_price: float,
        reason: str,
        *,
        actor: str = "strategy_agent",
    ) -> None:
        exit_action = "SELL" if position.action == "BUY" else "BUY"
        order = self._runtime.order_book.place_order(
            symbol=position.live_symbol,
            action=exit_action,
            order_type="MARKET",
            qty=position.qty,
            instrument_type="FUT",
            session_id=self._runtime.portfolio.session_id,
            ltp=current_price,
        )
        self._record_order(
            order,
            reason,
            flow="exit",
            lot_size=position.lot_size,
            lots=position.lots,
            strategy_key=position.strategy_key,
            strategy_title=position.strategy_title,
        )
        self._append_commentary(
            "trade",
            f"EXIT {position.display_name} {exit_action} @{current_price:.2f} ({reason}) | {position.lots} lot",
        )
        multiplier = 1 if position.action == "BUY" else -1
        realized_pnl = multiplier * (current_price - position.entry_price) * position.qty
        await record_audit_event(
            market="commodity",
            strategy_key=position.strategy_key,
            event_type="position_exit",
            actor=actor,
            symbol=position.symbol,
            underlying=position.underlying,
            severity="trade",
            message=(
                f"{position.display_name} {exit_action} @ ₹{current_price:,.2f} "
                f"({reason}); P&L ₹{realized_pnl:,.0f}"
            ),
            previous_state="open",
            new_state="closed",
            payload={
                "reason": reason,
                "entry_price": round(position.entry_price, 2),
                "exit_price": round(current_price, 2),
                "qty": position.qty,
                "lots": position.lots,
                "realized_pnl": round(realized_pnl, 2),
                "return_pct": round(position.return_pct, 2),
            },
        )
        self._runtime.positions.pop(position_key, None)

    async def _close_option_position(
        self,
        position_key: str,
        position: CommodityPositionState,
        current_price: float,
        reason: str,
        qty: int,
        *,
        keep_open: bool = False,
        actor: str = "strategy_agent",
    ) -> None:
        order = self._runtime.order_book.place_order(
            symbol=position.live_symbol,
            action="SELL",
            order_type="MARKET",
            qty=qty,
            instrument_type="OPT",
            expiry=position.expiry,
            strike=position.strike,
            option_type=position.option_type,
            session_id=self._runtime.portfolio.session_id,
            ltp=current_price,
        )
        exit_lots = max(1, qty // max(position.lot_size, 1))
        self._record_order(
            order,
            reason,
            flow="exit",
            lot_size=position.lot_size,
            lots=exit_lots,
            strategy_key=position.strategy_key,
            strategy_title=position.strategy_title,
        )
        self._append_commentary(
            "trade",
            f"EXIT {position.display_name} {position.option_type or ''} @{current_price:.2f} ({reason}) | {exit_lots} lot",
        )
        multiplier = 1 if position.action == "BUY" else -1
        pnl = multiplier * (current_price - position.entry_price) * qty
        partial_close = bool(keep_open and qty < position.qty)
        await record_audit_event(
            market="commodity",
            strategy_key=position.strategy_key,
            event_type="position_exit_partial" if partial_close else "position_exit",
            actor=actor,
            symbol=position.symbol,
            underlying=position.underlying,
            severity="trade",
            message=(
                f"{position.display_name} {position.option_type or ''} @ ₹{current_price:,.2f} "
                f"({reason}); {exit_lots} lot; P&L ₹{pnl:,.0f}"
            ),
            previous_state="open",
            new_state="open_runner" if partial_close else "closed",
            payload={
                "reason": reason,
                "entry_price": round(position.entry_price, 2),
                "exit_price": round(current_price, 2),
                "qty_closed": qty,
                "lots_closed": exit_lots,
                "realized_pnl": round(pnl, 2),
                "return_pct": round(position.return_pct, 2),
                "option_type": position.option_type,
                "strike": position.strike,
            },
        )
        from agentic_rag.trade_memory import build_strategy_trade_case, record_trade_case

        trade_case = build_strategy_trade_case(
            runtime_key=position.strategy_key,
            runtime_label=position.strategy_title,
            position=position,
            exit_price=current_price,
            reason=reason,
            close_qty=qty,
            pnl=pnl,
            partial=keep_open and qty < position.qty,
            source="paper_commodity_strategy_agent",
        )
        await record_trade_case(trade_case)
        if keep_open and qty < position.qty:
            position.qty -= qty
            position.lots = max(1, position.qty // max(position.lot_size, 1))
            position.current_price = current_price
        else:
            self._runtime.positions.pop(position_key, None)

    async def _open_new_futures_positions(self, futures_rows: list[dict[str, Any]]) -> None:
        futures_positions = sum(1 for pos in self._runtime.positions.values() if pos.strategy_key == "commodity_futures")
        if futures_positions >= FUTURES_MAX_POSITIONS:
            return

        for row in futures_rows:
            futures_positions = sum(1 for pos in self._runtime.positions.values() if pos.strategy_key == "commodity_futures")
            if futures_positions >= FUTURES_MAX_POSITIONS:
                break
            if row.get("signal_validation") != "ready":
                continue

            symbol = str(row["symbol"])
            bar_time = str(row.get("bar_time") or "")
            if self._runtime.processed_signals.get(f"commodity_futures:{symbol}") == bar_time:
                continue

            spec = get_commodity_contract_spec(symbol)
            underlying = str(row.get("underlying") or spec.root)
            if self._has_any_underlying_position(underlying):
                continue
            if _commodity_event_block_reason(underlying):
                continue
            if self._entry_risk_block(underlying):
                continue
            price = float(row.get("price") or 0.0)
            atr = float(row.get("atr") or 0.0)
            if price <= 0 or atr <= 0:
                continue
            qty = spec.futures_lot_size * self._lots_per_trade
            required_margin = self._estimate_futures_margin_required(price, qty)
            if required_margin > self._runtime.portfolio.available_capital:
                continue

            min_stop_distance = max(atr, price * FUTURES_MIN_STOP_PCT)
            if row.get("signal") == "BUY":
                stop_candidates = [price - min_stop_distance]
                for level in (row.get("mp_val"), row.get("mp_ib_low")):
                    if level is not None:
                        level_value = float(level)
                        if level_value < price and (price - level_value) >= min_stop_distance:
                            stop_candidates.append(level_value)
                stop_price = max(stop_candidates)
                target_price = price + ((price - stop_price) * 2.0)
            else:
                stop_candidates = [price + min_stop_distance]
                for level in (row.get("mp_vah"), row.get("mp_ib_high")):
                    if level is not None:
                        level_value = float(level)
                        if level_value > price and (level_value - price) >= min_stop_distance:
                            stop_candidates.append(level_value)
                stop_price = min(stop_candidates)
                target_price = price - ((stop_price - price) * 2.0)

            order = self._runtime.order_book.place_order(
                symbol=symbol,
                action=str(row.get("signal")),
                order_type="MARKET",
                qty=qty,
                instrument_type="FUT",
                session_id=self._runtime.portfolio.session_id,
                ltp=price,
            )
            self._record_order(
                order,
                str(row.get("reason") or "futures_signal"),
                flow="entry",
                lot_size=spec.futures_lot_size,
                lots=self._lots_per_trade,
                strategy_key="commodity_futures",
                strategy_title=spec.futures_label,
            )
            fill_price = float(order.fill_price or price)
            position_key = f"commodity_futures:{symbol}"
            self._runtime.positions[position_key] = CommodityPositionState(
                position_key=position_key,
                symbol=symbol,
                live_symbol=symbol,
                underlying=spec.root,
                strategy_key="commodity_futures",
                strategy_title=spec.futures_label,
                instrument_type="FUT",
                action=str(row.get("signal") or "BUY"),
                qty=qty,
                lots=self._lots_per_trade,
                lot_size=spec.futures_lot_size,
                entry_price=fill_price,
                current_price=fill_price,
                stop_price=round(stop_price, 2),
                target_price=round(target_price, 2),
                regime=str(row.get("mp_day_type") or row.get("regime") or "balance"),
                signal_reason=str(row.get("reason") or "futures_signal"),
                atr=_round_or_none(atr, 4),
                macd_value=row.get("macd"),
                mp_poc=row.get("mp_poc"),
                mp_vah=row.get("mp_vah"),
                mp_val=row.get("mp_val"),
                entered_at=_now_ist().isoformat(),
                entry_bar_time=bar_time,
                contract_unit_label=spec.contract_unit_label,
                quote_unit_label=spec.quote_unit_label,
                display_name=spec.display_name,
                initial_qty=qty,
                peak_price=fill_price,
                entry_style=str(row.get("entry_style") or "fresh_cross"),
                last_reviewed_bar_time=bar_time,
            )
            self._runtime.processed_signals[f"commodity_futures:{symbol}"] = bar_time
            self._append_commentary(
                "trade",
                f"ENTRY {spec.display_name} {row.get('signal')} @{fill_price:.2f} | {self._lots_per_trade} lot | "
                f"{str(row.get('entry_style') or 'fresh_cross').replace('_', ' ')} | MP {row.get('mp_day_type')} | stop {stop_price:.2f}",
            )
            await record_audit_event(
                market="commodity",
                strategy_key="commodity_futures",
                event_type="position_entry",
                actor="strategy_agent",
                symbol=symbol,
                underlying=spec.root,
                severity="trade",
                message=(
                    f"{spec.display_name} {row.get('signal')} @ ₹{fill_price:,.2f} "
                    f"({self._lots_per_trade} lot; MP {row.get('mp_day_type')}; stop ₹{stop_price:,.2f})"
                ),
                new_state="open",
                payload={
                    "side": str(row.get("signal") or ""),
                    "fill_price": round(fill_price, 2),
                    "stop_price": round(stop_price, 2),
                    "target_price": round(target_price, 2),
                    "qty": qty,
                    "lots": self._lots_per_trade,
                    "regime": str(row.get("mp_day_type") or row.get("regime") or ""),
                    "entry_style": str(row.get("entry_style") or "fresh_cross"),
                },
            )

    async def _open_new_option_positions(self, option_rows: list[dict[str, Any]]) -> None:
        option_positions = sum(1 for pos in self._runtime.positions.values() if pos.strategy_key == "commodity_options")
        if option_positions >= OPTIONS_MAX_POSITIONS:
            return

        for row in option_rows:
            option_positions = sum(1 for pos in self._runtime.positions.values() if pos.strategy_key == "commodity_options")
            if option_positions >= OPTIONS_MAX_POSITIONS:
                break
            if row.get("signal_validation") != "ready":
                continue

            signal_side = str(row.get("signal_side") or "")
            trade_symbol = str(row.get("trade_symbol") or "")
            trade_bar_time = str(row.get("trade_bar_time") or "")
            trade_price = float(row.get("trade_price") or 0.0)
            lots = int(row.get("lots_affordable") or 0)
            if signal_side not in {"CE", "PE"} or not trade_symbol or not trade_bar_time or trade_price <= 0 or lots <= 0:
                continue
            if self._runtime.processed_signals.get(f"commodity_options:{trade_symbol}") == trade_bar_time:
                continue

            symbol = str(row.get("symbol") or "")
            spec = get_commodity_contract_spec(symbol)
            underlying = str(row.get("underlying") or spec.root)
            if self._has_any_underlying_position(underlying):
                continue
            if _commodity_event_block_reason(underlying):
                continue
            if self._entry_risk_block(underlying):
                continue
            if str(row.get("regime") or "") == "vol_spike":
                continue
            try:
                days_to_expiry = (date.fromisoformat(str(row.get("expiry") or "")) - _now_ist().date()).days
            except ValueError:
                days_to_expiry = None
            if days_to_expiry is None or days_to_expiry < OPTIONS_MIN_TTE_DAYS:
                continue
            qty = spec.futures_lot_size * lots
            side = row.get("ce") if signal_side == "CE" else row.get("pe")
            if not side:
                continue
            entry_iv_pct = _normalize_iv_pct(
                row.get("entry_iv_pct")
                or side.get("iv")
                or side.get("iv_pct")
                or side.get("implied_volatility")
                or side.get("implied_vol")
                or side.get("atm_iv")
            )
            if entry_iv_pct is None or entry_iv_pct > OPTIONS_IV_REJECT_PCT:
                continue
            max_capital = _normalized_option_budget_cap(
                self._runtime.portfolio.initial_capital,
                self._runtime.portfolio.available_capital,
            )
            cost_per_lot = trade_price * max(spec.futures_lot_size, 1)
            if cost_per_lot <= 0:
                continue
            allowed_lots = int(max_capital // cost_per_lot)
            if entry_iv_pct > OPTIONS_IV_HALF_SIZE_PCT:
                allowed_lots = max(0, allowed_lots // 2)
            lots = min(lots, allowed_lots)
            if lots <= 0:
                continue
            qty = spec.futures_lot_size * lots

            order = self._runtime.order_book.place_order(
                symbol=trade_symbol,
                action="BUY",
                order_type="MARKET",
                qty=qty,
                instrument_type="OPT",
                expiry=row.get("expiry"),
                strike=float(side.get("strike") or 0.0),
                option_type=signal_side,
                session_id=self._runtime.portfolio.session_id,
                entry_iv_pct=_round_or_none(entry_iv_pct, 1),
                regime=str(row.get("regime") or "neutral"),
                ltp=trade_price,
            )
            self._record_order(
                order,
                str(row.get("signal_reason") or "option_signal"),
                flow="entry",
                lot_size=spec.futures_lot_size,
                lots=lots,
                strategy_key="commodity_options",
                strategy_title=spec.options_label,
            )
            fill_price = float(order.fill_price or trade_price)
            position_key = f"commodity_options:{trade_symbol}"
            target_price = fill_price * (1 + (OPTIONS_TARGET_PCT / 100.0))
            stop_price = fill_price * (1 - (OPTIONS_HARD_STOP_PCT / 100.0))
            self._runtime.positions[position_key] = CommodityPositionState(
                position_key=position_key,
                symbol=symbol,
                live_symbol=trade_symbol,
                underlying=str(row.get("underlying") or spec.root),
                strategy_key="commodity_options",
                strategy_title=spec.options_label,
                instrument_type="OPT",
                action="BUY",
                qty=qty,
                lots=lots,
                lot_size=spec.futures_lot_size,
                entry_price=fill_price,
                current_price=fill_price,
                stop_price=round(stop_price, 2),
                target_price=round(target_price, 2),
                regime=str(row.get("regime") or "neutral"),
                signal_reason=str(row.get("signal_reason") or "option_signal"),
                atr=None,
                macd_value=(side or {}).get("macd"),
                mp_poc=None,
                mp_vah=None,
                mp_val=None,
                entered_at=_now_ist().isoformat(),
                entry_bar_time=trade_bar_time,
                contract_unit_label=spec.contract_unit_label,
                quote_unit_label=spec.quote_unit_label,
                display_name=spec.display_name,
                initial_qty=qty,
                peak_price=fill_price,
                expiry=row.get("expiry"),
                strike=float(side.get("strike") or 0.0),
                option_type=signal_side,
                entry_iv_pct=_round_or_none(entry_iv_pct, 1),
            )
            self._runtime.processed_signals[f"commodity_options:{trade_symbol}"] = trade_bar_time
            self._append_commentary(
                "trade",
                f"ENTRY {spec.display_name} {signal_side} @{fill_price:.2f} | {lots} lot | "
                f"{OPTIONS_CAPITAL_FRACTION:.0%} capital budget | stop {stop_price:.2f}",
            )
            await record_audit_event(
                market="commodity",
                strategy_key="commodity_options",
                event_type="position_entry",
                actor="strategy_agent",
                symbol=trade_symbol,
                underlying=str(row.get("underlying") or spec.root),
                severity="trade",
                message=(
                    f"{spec.display_name} {signal_side} @ ₹{fill_price:,.2f} "
                    f"({lots} lot; {OPTIONS_CAPITAL_FRACTION:.0%} budget; stop ₹{stop_price:,.2f})"
                ),
                new_state="open",
                payload={
                    "option_type": signal_side,
                    "strike": float(side.get("strike") or 0.0),
                    "expiry": str(row.get("expiry") or ""),
                    "fill_price": round(fill_price, 2),
                    "stop_price": round(stop_price, 2),
                    "target_price": round(target_price, 2),
                    "qty": qty,
                    "lots": lots,
                    "entry_iv_pct": _round_or_none(entry_iv_pct, 1),
                },
            )

    def _record_order(
        self,
        order: PaperOrder,
        reason: str,
        *,
        flow: str,
        lot_size: int,
        lots: int,
        strategy_key: str,
        strategy_title: str,
    ) -> None:
        self._runtime.orders.insert(
            0,
            {
                "time": _order_fill_time_ist(order).isoformat(),
                "order_id": order.order_id,
                "symbol": order.symbol,
                "action": order.action,
                "qty": order.qty,
                "lots": lots,
                "lot_size": lot_size,
                "order_type": order.order_type,
                "status": order.status,
                "fill_price": _round_or_none(order.fill_price, 2),
                "entry_iv_pct": _round_or_none(order.entry_iv_pct, 1),
                "reason": reason,
                "flow": flow,
                "strategy_key": strategy_key,
                "strategy_title": strategy_title,
            },
        )
        del self._runtime.orders[DEFAULT_COMMODITY_ORDERS_MAX:]

    def _append_commentary(self, tone: str, message: str) -> None:
        if not message:
            return
        previous = self._commentary[0] if self._commentary else None
        if previous and previous.tone == tone and previous.message == message:
            return
        self._commentary.insert(
            0,
            CommodityCommentaryEntry(
                time=_now_ist().isoformat(),
                tone=tone,
                message=message,
            ),
        )
        del self._commentary[DEFAULT_COMMODITY_COMMENTARY_MAX:]

    def _append_report(self) -> None:
        summary = self._runtime.portfolio.get_summary()
        self._runtime.reports.insert(
            0,
            CommodityReportSnapshot(
                time=_now_ist().isoformat(),
                total_equity=float(summary.get("total_equity") or 0.0),
                realized_pnl=float(summary.get("realized_pnl") or 0.0),
                unrealized_pnl=float(summary.get("unrealized_pnl") or 0.0),
                open_positions=len(self._runtime.positions),
                tracked_symbols=len(self._symbols),
                last_message=self._last_message,
            ),
        )
        del self._runtime.reports[DEFAULT_COMMODITY_REPORTS_MAX:]

    def _append_signal_audit(self, entry: dict[str, Any]) -> None:
        audit_entry = dict(entry)
        audit_key = str(audit_entry.get("audit_key") or "").strip()
        if not audit_key:
            return
        if any(str(existing.get("audit_key") or "") == audit_key for existing in self._runtime.signal_audit[:12]):
            return
        self._runtime.signal_audit.insert(0, audit_entry)
        del self._runtime.signal_audit[DEFAULT_COMMODITY_SIGNAL_AUDIT_MAX:]

    def _audit_futures_watchlist(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            symbol = str(row.get("symbol") or "").strip()
            bar_time = str(row.get("bar_time") or "").strip()
            validation = str(row.get("signal_validation") or "").strip()
            entry_style = str(row.get("entry_style") or "").strip() or "fresh_cross"
            candidate_signal = (
                str(row.get("signal") or "")
                or str(row.get("raw_signal") or "")
                or str(row.get("continuation_signal") or "")
            )
            if not symbol or not bar_time:
                continue
            if not candidate_signal and validation in {"waiting_cross", "warming_up", "mp_warming_up"}:
                continue
            self._append_signal_audit(
                {
                    "audit_key": ":".join(
                        [
                            "commodity_futures",
                            symbol,
                            bar_time,
                            validation or "unknown",
                            candidate_signal or "none",
                            entry_style,
                        ]
                    ),
                    "time": _now_ist().isoformat(),
                    "lane": "commodity_futures",
                    "symbol": symbol,
                    "underlying": row.get("underlying"),
                    "bar_time": bar_time,
                    "entry_style": entry_style,
                    "signal": row.get("signal"),
                    "raw_signal": row.get("raw_signal"),
                    "continuation_signal": row.get("continuation_signal"),
                    "recent_cross_signal": row.get("recent_cross_signal"),
                    "recent_cross_bars_ago": row.get("recent_cross_bars_ago"),
                    "mp_direction": row.get("mp_direction"),
                    "mp_day_type": row.get("mp_day_type"),
                    "validation": validation,
                    "detail": row.get("signal_validation_detail"),
                    "price": row.get("price"),
                    "regime": row.get("regime"),
                    "runtime_retained": bool(row.get("runtime_retained")),
                }
            )

    async def set_kill_switch(self, active: bool) -> dict[str, Any]:
        previous = self._kill_switch_active
        if not active:
            current_drawdown = self._current_drawdown_pct()
            release_blocked = bool(settings.COMMODITY_KILL_LOCK) or current_drawdown >= COMMODITY_MAX_DRAWDOWN_PCT
            if release_blocked:
                self._kill_switch_active = True
                self._start_required = True
                self._manual_restart_required = True
                await self._stop_loop()
                if settings.COMMODITY_KILL_LOCK:
                    reason = "deployment commodity kill lock is enabled"
                else:
                    reason = (
                        f"drawdown {current_drawdown:.1f}% still exceeds "
                        f"the {COMMODITY_MAX_DRAWDOWN_PCT:.1f}% cap"
                    )
                self._last_message = f"Commodity kill switch release blocked: {reason}."
                self._append_commentary("warning", self._last_message)
                self._persist_state()
                await record_audit_event(
                    market="commodity",
                    event_type="kill_switch_release_blocked",
                    actor="manual",
                    severity="warning",
                    message=self._last_message,
                    previous_state="killed",
                    new_state="killed",
                    payload={
                        "reason": reason,
                        "current_drawdown_pct": _round_or_none(current_drawdown, 2),
                    },
                )
                return {
                    **self.get_control_state(),
                    "release_blocked": True,
                    "release_block_reason": reason,
                    "current_drawdown_pct": _round_or_none(current_drawdown, 2),
                }

        self._kill_switch_active = bool(active)
        cancelled_orders = 0
        closed_positions: list[dict[str, Any]] = []
        for order in list(self._runtime.order_book.get_open_orders(self._runtime.portfolio.session_id)):
            if self._runtime.order_book.cancel_order(order.order_id):
                cancelled_orders += 1

        if self._kill_switch_active:
            self._start_required = True
            self._manual_restart_required = True
            for position_key, position in list(self._runtime.positions.items()):
                current_price = float(position.current_price or position.entry_price or 0.0)
                if current_price <= 0:
                    continue
                if position.instrument_type == "FUT":
                    await self._close_futures_position(
                        position_key,
                        position,
                        current_price,
                        "manual_kill_switch",
                        actor="manual",
                    )
                else:
                    await self._close_option_position(
                        position_key,
                        position,
                        current_price,
                        "manual_kill_switch",
                        position.qty,
                        actor="manual",
                    )
                closed_positions.append(
                    {
                        "position_key": position_key,
                        "symbol": position.live_symbol,
                        "qty": position.qty,
                        "exit_price": round(current_price, 2),
                    }
                )
            await self._stop_loop()
            self._last_message = (
                f"Commodity kill switch active. Closed {len(closed_positions)} position(s), "
                f"cancelled {cancelled_orders} order(s), and stopped the agent. "
                "Release it and start the agent manually to resume scanning."
            )
            self._append_commentary("warning", self._last_message)
        else:
            market_open = _in_commodity_hours()
            self._start_required = not market_open
            self._manual_restart_required = False
            self._last_message = (
                "Commodity kill switch released. Agent will resume automatically now because MCX is open."
                if market_open
                else "Commodity kill switch released. Agent is armed for the next MCX session."
            )
            self._append_commentary("success", self._last_message)

        self._persist_state()
        if not self._kill_switch_active and _in_commodity_hours() and self._auto_run_enabled:
            await self.start(force=True)
        if previous != self._kill_switch_active:
            await record_audit_event(
                market="commodity",
                event_type="kill_switch_toggled",
                actor="manual",
                severity="warning" if self._kill_switch_active else "success",
                message=self._last_message,
                previous_state="killed" if previous else "active",
                new_state="killed" if self._kill_switch_active else "active",
                payload={
                    "cancelled_orders": cancelled_orders,
                    "closed_positions": closed_positions,
                },
            )
        return self.get_control_state(cancelled_orders=cancelled_orders, closed_positions=closed_positions)

    def get_control_state(
        self,
        *,
        cancelled_orders: int = 0,
        closed_positions: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        self._refresh_state_from_store()
        return {
            "market": "commodity",
            "auto_run_enabled": self._auto_run_enabled,
            "kill_switch_active": self._kill_switch_active,
            "loop_active": self._loop_active(),
            "start_required": self._start_required,
            "manual_restart_required": self._manual_restart_required,
            "cancelled_orders": cancelled_orders,
            "closed_positions": closed_positions or [],
        }

    def get_status(self, *, refresh: bool = True) -> dict[str, Any]:
        if refresh:
            self._refresh_state_from_store()
        summary = self._runtime.portfolio.get_summary()
        lane_agents = self._strategy_agents()
        lane_map = {lane.descriptor.key: lane for lane in lane_agents}
        option_ready = lane_map["commodity_options"].ready_signals()
        futures_ready = lane_map["commodity_futures"].ready_signals()
        last_error = self._last_error
        last_message = self._last_message
        if (
            isinstance(last_error, str)
            and last_error.startswith("No commodity broker adapter is available.")
            and (self._runtime.futures_watchlist or self._runtime.option_watchlist)
        ):
            last_error = None
            last_message = "Using prepared MCX futures/options watchlists until the next scan refreshes broker data."
        return {
            "enabled": self._enabled,
            "auto_run_enabled": self._auto_run_enabled,
            "kill_switch_active": self._kill_switch_active,
            "loop_active": self._loop_active(),
            "start_required": self._start_required,
            "manual_restart_required": self._manual_restart_required,
            "running": self._running,
            "scan_interval_seconds": self.scan_interval_seconds,
            "last_run_at": self._last_run_at,
            "last_error": last_error,
            "last_message": last_message,
            "config": {
                "symbols": list(self._symbols),
                "selected_option_expiries": dict(self._selected_option_expiries),
                "selected_option_lookup_symbols": dict(self._selected_option_lookup_symbols),
                "futures_timeframe": FUTURES_TIMEFRAME,
                "options_timeframe": OPTIONS_TIMEFRAME,
                "futures_macd_fast": FUTURES_MACD_FAST,
                "futures_macd_slow": FUTURES_MACD_SLOW,
                "futures_macd_signal": FUTURES_MACD_SIGNAL,
                "options_macd_fast": OPTIONS_MACD_FAST,
                "options_macd_slow": OPTIONS_MACD_SLOW,
                "options_macd_signal": OPTIONS_MACD_SIGNAL,
                "mp_period_minutes": 15,
                "lots_per_trade": self._lots_per_trade,
                "futures_min_hold_bars": FUTURES_MIN_HOLD_BARS,
                "futures_continuation_lookback_bars": FUTURES_CONTINUATION_LOOKBACK_BARS,
                "futures_min_stop_pct": FUTURES_MIN_STOP_PCT,
                "futures_trail_atr_multiplier": FUTURES_TRAIL_ATR_MULTIPLIER,
                "futures_target_arm_r_multiplier": FUTURES_TARGET_ARM_R_MULTIPLIER,
                "option_capital_fraction": OPTIONS_CAPITAL_FRACTION,
                "option_hard_stop_pct": OPTIONS_HARD_STOP_PCT,
                "option_min_tte_days": OPTIONS_MIN_TTE_DAYS,
                "option_iv_half_size_pct": OPTIONS_IV_HALF_SIZE_PCT,
                "option_iv_reject_pct": OPTIONS_IV_REJECT_PCT,
                "commodity_daily_loss_limit": COMMODITY_DAILY_LOSS_LIMIT,
                "commodity_underlying_daily_loss_limit": COMMODITY_UNDERLYING_DAILY_LOSS_LIMIT,
                "commodity_max_drawdown_pct": COMMODITY_MAX_DRAWDOWN_PCT,
                "commodity_stop_cooldown_minutes": COMMODITY_STOP_COOLDOWN_MINUTES,
            },
            "strategy_agents": [lane.build_status_payload() for lane in lane_agents],
            "strategies": self._strategy_catalog(),
            "summary": {
                **summary,
                "open_positions": len(self._runtime.positions),
                "tracked_symbols": len(self._symbols),
                "open_orders": len(self._runtime.order_book.get_open_orders(self._runtime.portfolio.session_id)),
                "ready_futures_signals": futures_ready,
                "ready_option_signals": option_ready,
            },
            "watchlist": list(self._runtime.futures_watchlist),
            "futures_watchlist": list(self._runtime.futures_watchlist),
            "option_watchlist": list(self._runtime.option_watchlist),
            "positions": [
                {
                    **asdict(position),
                    "unrealized_pnl": _round_or_none(position.unrealized_pnl, 2),
                    "return_pct": _round_or_none(position.return_pct, 2),
                    "notional_value": _round_or_none(position.current_price * position.qty, 2),
                }
                for position in self._runtime.positions.values()
            ],
            "trade_history": _serialize_trade_history(self._runtime.portfolio),
            "orders": list(self._runtime.orders),
            "reports": [asdict(report) for report in self._runtime.reports],
            "commentary": [asdict(entry) for entry in self._commentary],
            "signal_audit": [
                {key: value for key, value in entry.items() if key != "audit_key"}
                for entry in self._runtime.signal_audit
            ],
            "data_health": self._last_data_health,
        }

    def get_orders(self) -> list[dict[str, Any]]:
        self._refresh_state_from_store()
        return list(self._runtime.orders)

    def get_positions(self) -> list[dict[str, Any]]:
        return self.get_status()["positions"]

    def get_trade_history(self) -> list[dict[str, Any]]:
        self._refresh_state_from_store()
        return _serialize_trade_history(self._runtime.portfolio)

    def get_reports(self) -> list[dict[str, Any]]:
        self._refresh_state_from_store()
        return [asdict(report) for report in self._runtime.reports]

    async def archive_and_reset_paper_account(self, *, actor: str = "manual") -> dict[str, Any]:
        self._refresh_state_from_store(force=True)
        await self._stop_loop()
        snapshot = self.get_status(refresh=False)
        prior_realized = float((snapshot.get("summary") or {}).get("realized_pnl") or 0.0)
        prior_trades = int((snapshot.get("summary") or {}).get("total_trades") or 0)
        archived_at = _now_ist()
        archive_dir = DEFAULT_COMMODITY_ARCHIVE_DIR
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_stamp = archived_at.strftime("%Y%m%dT%H%M%S%f%z")
        archive_path = archive_dir / f"{archive_stamp}_pre_reset.json"
        archive_payload = {
            "archived_at": archived_at.isoformat(),
            "reason": "manual_paper_reset",
            "snapshot": snapshot,
        }
        archive_path.write_text(json.dumps(archive_payload, indent=2))

        self._runtime = CommodityRuntime(
            portfolio=PaperPortfolio(
                initial_capital=DEFAULT_COMMODITY_INITIAL_CAPITAL,
                session_id="commodity-strategy-paper",
            ),
            order_book=PaperOrderBook(on_fill=None),
        )
        self._runtime.order_book._on_fill = self._runtime.portfolio.on_fill
        self._commentary = []
        self._runtime.processed_signals = {}
        self._kill_switch_active = False
        self._start_required = False
        self._manual_restart_required = False
        self._last_error = None
        self._last_run_at = None
        self._last_message = (
            "Commodity paper account reset to ₹1,000,000. "
            "Archived prior state; automatic scanning can resume on the next supervisor/startup cycle."
        )
        self._append_commentary("warning", self._last_message)
        self._persist_state()
        await record_audit_event(
            market="commodity",
            event_type="paper_account_reset",
            actor=actor,
            severity="warning",
            message=self._last_message,
            previous_state="damaged",
            new_state="fresh",
            payload={
                "archive_path": str(archive_path),
                "prior_realized_pnl": prior_realized,
                "prior_total_trades": prior_trades,
                "new_initial_capital": DEFAULT_COMMODITY_INITIAL_CAPITAL,
            },
        )
        return {
            "archived": True,
            "archive_path": str(archive_path),
            "initial_capital": DEFAULT_COMMODITY_INITIAL_CAPITAL,
            "kill_switch_active": self._kill_switch_active,
            "start_required": self._start_required,
            "manual_restart_required": self._manual_restart_required,
            "prior_realized_pnl": prior_realized,
            "prior_total_trades": prior_trades,
        }


commodity_strategy_agent = CommodityStrategyAgent()
