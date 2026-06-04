"""Paper runtime for the commodity desk — MCX futures, MP + Order-Flow entries.

Single sleeve only: 1-minute closes drive a four-trigger Market-Profile + Order-Flow
evaluator (`commodity_mp_signal.evaluate_commodity_mp_signal`) that produces fresh
BUY/SELL signals. Triggers in priority order: open_drive, ib_break, failed_auction,
va_migration, lvn_fade. The existing risk harness (ATR stops, BE move at 1R, partial
lock at 1.5R, target arm at 2R, ATR trail, daily-loss cap, per-underlying cap, event-
window blocks, stop cooldown, kill switch) is preserved.

The commodity options sleeve was deprecated; historical option trades remain in the
persisted `trade_history` for audit but the agent no longer scans option chains or
opens new option positions.
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
from analysis.macd_engine import compute_ema
from analysis.signal_classifier import classify_signal_bucket
from analytics.market_profile_ext import (
    ib_extension as compute_ib_extension,
    market_profile_ext_snapshot,
)
from analytics.orderflow import (
    bar_cvd,
    cvd_agrees_with,
    orderflow_snapshot,
)
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
from core.trading_calendar import trading_calendar
from core.runtime_state import (
    load_paper_trade_book,
    load_runtime_state,
    record_paper_trade,
    save_runtime_state,
)
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
from paper_engine.commodity_mp_signal import (
    _compute_atr as _compute_atr_series,
    evaluate_commodity_mp_signal,
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
    _sort_trades_recent_first,
    _split_today_history,
)
from paper_engine.order_book import PaperOrder, PaperOrderBook
from paper_engine.portfolio import PaperPortfolio, VirtualPosition

DEFAULT_COMMODITY_SCAN_INTERVAL_SECONDS = 30
DEFAULT_COMMODITY_HISTORY_DAYS = 21
DEFAULT_COMMODITY_ATR_PERIOD = 14
DEFAULT_COMMODITY_LOTS_PER_TRADE = 1
DEFAULT_COMMODITY_MARGIN_PCT = 0.15
# Target rupee notional per commodity futures position. Lots are sized so
# EVERY contract opens at roughly this value, instead of a fixed lot count
# that left wildly different position sizes (e.g. 1 lot COPPER ≈ ₹21L vs
# 1 lot ZINCMINI ≈ ₹2.7L). With this, gold/crude/zinc/etc. all open at
# ~₹15L. Floored at 1 lot (large-lot contracts whose single lot already
# exceeds the target stay at 1 lot). Scaled by lots_per_trade so the
# existing config still multiplies the target.
COMMODITY_TARGET_POSITION_VALUE = 1_500_000.0
DEFAULT_COMMODITY_REPORTS_MAX = 40
DEFAULT_COMMODITY_ORDERS_MAX = 80
DEFAULT_COMMODITY_COMMENTARY_MAX = 80
DEFAULT_COMMODITY_SIGNAL_AUDIT_MAX = 600
DEFAULT_COMMODITY_INITIAL_CAPITAL = 5_000_000.0  # ₹50L paper capital (user, 2026-06-04)
DEFAULT_COMMODITY_ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "runtime" / "commodity_archive"
DEFAULT_COMMODITY_SCAN_TIMEOUT_SECONDS = 120

FUTURES_TIMEFRAME = "1minute"  # signal evaluator runs on closed 1-min bars
FUTURES_MP_PERIOD_MINUTES = 15  # canonical TPO period (60-min IB)
FUTURES_CVD_ANCHOR_HOUR_IST = 9
FUTURES_MAX_POSITIONS = 1000  # no practical position cap (user 2026-06-04); margin/capital is the real limit
FUTURES_MP_MIN_PERIODS = 4  # need IB to print before any trigger can fire
FUTURES_MIN_HOLD_BARS = 4
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

# Options sleeve deprecated — constants intentionally removed. Historical option
# trades remain in the persisted trade_history for audit only.


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
    return trading_calendar.is_exchange_open("MCX", now or _now_ist())


def _is_within_minutes(current_time: time, event_time: time, minutes: int) -> bool:
    current_minutes = current_time.hour * 60 + current_time.minute
    event_minutes = event_time.hour * 60 + event_time.minute
    return abs(current_minutes - event_minutes) <= minutes


def _commodity_event_block_reason(symbol_or_underlying: str, now: Optional[datetime] = None) -> Optional[str]:
    # Scheduled-report event blocks REMOVED by design (2026-06-04, user direction). The EIA
    # crude-inventory (Wed ~20:00 IST) and natural-gas-storage (Thu ~20:30 IST) reactions are PRIMARY
    # OPPORTUNITIES for the MP+OF breakout engine, not risks to sit out — a ±90-min blackout removed
    # the best directional setups of the week. Entries are already gated by confirmed 2-bar + CVD/VWAP
    # triggers, a conviction floor, and hard stops, so the engine reacts to the confirmed post-report
    # thrust rather than entering blind into the print. Re-enable per-symbol here if event risk bites.
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


def _position_open_trade_row(p: Any) -> dict[str, Any]:
    """Surface a currently-OPEN futures position as a trade-log row, in the same
    shape as a closed TradeRecord row (exit_price=None, status="open"), so a
    trade is RECORDED in the trade history the moment it opens — not only when
    it closes. Previously trade_history/today_trades held closed trades only, so
    a freshly-opened book showed an empty trade log."""
    return {
        "symbol": getattr(p, "symbol", None),
        "underlying": getattr(p, "underlying", None),
        "action": getattr(p, "action", None),
        "qty": int(getattr(p, "qty", 0) or 0),
        "lots": getattr(p, "lots", None),
        "entry_price": float(getattr(p, "entry_price", 0.0) or 0.0),
        "exit_price": None,
        "pnl": _round_or_none(getattr(p, "unrealized_pnl", None), 2),
        "unrealized_pnl": _round_or_none(getattr(p, "unrealized_pnl", None), 2),
        "return_pct": _round_or_none(getattr(p, "return_pct", None), 2),
        "entry_time": str(getattr(p, "entered_at", "") or ""),
        "exit_time": None,
        "instrument_type": getattr(p, "instrument_type", "FUT"),
        "expiry": getattr(p, "expiry", None),
        "strike": None,
        "option_type": None,
        "signal_id": getattr(p, "position_key", None),
        "setup_type": getattr(p, "signal_reason", None),
        "entry_iv_pct": None,
        "regime": getattr(p, "regime", None),
        "stop_price": getattr(p, "stop_price", None),
        "target_price": getattr(p, "target_price", None),
        "status": "open",
    }


def _db_trade_to_row(r: dict[str, Any]) -> dict[str, Any]:
    """Map a durable paper_trade_book DB row to the trade-log row shape the
    frontend renders (mirrors _serialize_trade_history, status=closed)."""
    return {
        "symbol": r.get("symbol"),
        "underlying": r.get("underlying"),
        "action": r.get("action"),
        "qty": r.get("qty"),
        "lots": r.get("lots"),
        "entry_price": r.get("entry_price"),
        "exit_price": r.get("exit_price"),
        "pnl": r.get("pnl"),
        "entry_time": r.get("entry_time"),
        "exit_time": r.get("exit_time"),
        "instrument_type": r.get("instrument_type"),
        "setup_type": r.get("setup_type"),
        "regime": r.get("regime"),
        "exit_reason": r.get("exit_reason"),
        "signal_id": r.get("signal_id"),
        "recorded_at": r.get("recorded_at"),
        "status": "closed",
    }


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


def _default_saved_state() -> dict[str, Any]:
    return {
        "config": {
            "symbols": [],
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
    # Soft-purge: legacy option keys are silently dropped during load.
    # Historical option trades inside `trade_history` are preserved for audit.
    default_state["config"] = {
        "symbols": symbols,
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


def _infer_09ist_anchor(candles: list[dict[str, Any]]) -> int:
    """Find the index of the most-recent 09:00 IST bar boundary.

    Used to anchor session-wide CVD and VWAP. MCX day session opens at 09:00 IST;
    the evening session at 17:00 IST is treated as continuation (single anchor).
    """
    if not candles:
        return 0
    last_seen_anchor = 0
    last_date: Optional[date] = None
    for idx, candle in enumerate(candles):
        ts = _parse_iso_timestamp(candle.get("time"))
        if ts is None:
            continue
        ist = ts.astimezone(IST)
        # First bar of each calendar day at or after 09:00 IST anchors the
        # session. We walk forward so the latest such bar wins.
        if last_date is None or ist.date() != last_date:
            if ist.hour >= FUTURES_CVD_ANCHOR_HOUR_IST:
                last_seen_anchor = idx
                last_date = ist.date()
    return last_seen_anchor


# classify_signal_bucket is imported from analysis.signal_classifier so all
# strategy agents bucket their lane rows the same way.
# MACD evaluator has been removed; futures lane now uses
# `paper_engine.commodity_mp_signal.evaluate_commodity_mp_signal`.


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
        title="MP+OF Futures",
        timeframe=FUTURES_TIMEFRAME,
        instrument_scope="MCX futures · MP+OF on 1-min",
        execution_mode="paper_execution",
        position_cap=FUTURES_MAX_POSITIONS,
    )

    def open_positions(self) -> int:
        return sum(1 for pos in self.owner._runtime.positions.values() if pos.strategy_key == self.descriptor.key)

    def ready_signals(self) -> int:
        return sum(1 for row in self.owner._runtime.futures_watchlist if row.get("signal_validation") == "ready")

    async def run_entries(self, rows: list[dict[str, Any]]) -> None:
        await self.owner._open_new_futures_positions(rows)


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
        ]
        # Cache of prior-session MarketProfileSnapshot per symbol, keyed by
        # (symbol, today_session_date). Built lazily by
        # `_load_prior_session_profile`; cleared when a new session rolls.
        self._prior_mp_cache: dict[tuple[str, "date"], Any] = {}
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

    def _apply_saved_state(self, saved_state: dict[str, Any], *, preserve_runtime: bool = False) -> None:
        saved_config = saved_state["config"]
        self._symbols = list(saved_config["symbols"])
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
        if preserve_runtime:
            # The running scan loop is the AUTHORITY on its own live positions,
            # portfolio ledger, and processed-signal de-dup map. Skipping the
            # runtime reload here stops a competing writer of the shared
            # `commodity_strategy_state` blob (a second worker, an admin/API call
            # on another process, or an out-of-band script) from WIPING
            # freshly-opened positions or RESURRECTING just-closed ones via
            # last-writer-wins — the root cause of the position churn that
            # produced orphan "entry @ X but never entered at that time" audits
            # and exits that vanished before they could book. Config + control
            # above are still synced, so kill-switch / symbol changes from other
            # workers apply. Single-writer invariant: only the loop mutates
            # positions; refresh never reloads them while the loop owns them.
            return
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
        self._apply_saved_state(saved_state, preserve_runtime=self._loop_active())
        if updated_at is not None:
            self._state_synced_at = updated_at
        return True

    def _strategy_agents(self) -> list[_BaseCommodityLaneAgent]:
        return list(self._lane_agents)

    def _restore_runtime_state(self, runtime_state: dict[str, Any]) -> None:
        self._runtime.futures_watchlist = [
            row for row in list(runtime_state.get("futures_watchlist") or runtime_state.get("watchlist") or []) if isinstance(row, dict)
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
        selected_option_expiries: Optional[dict[str, str]] = None,  # legacy arg, ignored
    ) -> dict[str, Any]:
        self._refresh_state_from_store()
        self._symbols = _normalize_symbols(symbols)
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
        }

    def get_symbols(self) -> list[str]:
        self._refresh_state_from_store()
        return list(self._symbols)

    # Backwards-compat stubs: the commodity options sleeve was removed but the
    # `directional_options` module (a separate index-options sleeve) still calls
    # these methods to introspect any saved commodity option selections. Return
    # empty mappings so those callers degrade gracefully.
    def get_selected_option_expiries(self) -> dict[str, str]:
        return {}

    def get_selected_option_lookup_symbols(self) -> dict[str, str]:
        return {}

    def _active_futures_symbol(self, symbol: str) -> str:
        # No option-lookup remapping any more; configured symbol IS the
        # tradable futures symbol.
        return _canonicalize_symbol(symbol)

    def _active_futures_symbols(self) -> dict[str, str]:
        return {symbol: self._active_futures_symbol(symbol) for symbol in self._symbols}

    @staticmethod
    def _cvd_agrees_loose(signal: str, cvd_window) -> bool:
        """Looser CVD-agreement check used by the entry gate.

        The plain `cvd_agrees_with` rejects whenever the strict first-to-last
        delta points the wrong way — that's too aggressive in chop, where
        CVD wobbles around zero even when the trade is fine. Here we accept
        if EITHER:
          * The recent half of the window agrees (more weight to fresh flow), OR
          * The full-window delta agrees AND is large enough to matter, OR
          * The full-window delta is small (≤ 5 % of the window magnitude)
            — i.e. CVD is essentially flat, not actively disagreeing.
        """
        if not cvd_window or len(cvd_window) < 2 or signal not in {"BUY", "SELL"}:
            return False
        full_delta = cvd_window[-1] - cvd_window[0]
        # Recent half — quickest signal in the window.
        half = max(len(cvd_window) // 2, 1)
        recent_delta = cvd_window[-1] - cvd_window[-half - 1] if len(cvd_window) > half else full_delta
        # "Magnitude" yardstick — peak-to-trough range over the window.
        window_range = max(cvd_window) - min(cvd_window)
        flat_threshold = window_range * 0.05
        # BUY accepts when CVD is up OR essentially flat; SELL accepts the inverse.
        if signal == "BUY":
            if recent_delta > 0:
                return True
            if full_delta > 0:
                return True
            return abs(full_delta) <= flat_threshold
        # SELL
        if recent_delta < 0:
            return True
        if full_delta < 0:
            return True
        return abs(full_delta) <= flat_threshold

    def _estimate_futures_margin_required(self, price: float, qty: int) -> float:
        return max(price, 0.0) * max(qty, 0) * DEFAULT_COMMODITY_MARGIN_PCT

    def _target_lots_for_contract(self, spec: "CommodityContractSpec", price: float) -> int:
        """Lots sized so the position notional ≈ COMMODITY_TARGET_POSITION_VALUE.

        This makes every contract open at roughly the same rupee size
        regardless of its lot size and price — e.g. gold, crude, and zinc
        all open at ~₹15L instead of 1 lot each (which gave ₹7L / ₹5L /
        ₹2.7L respectively). Floored at 1 lot, so a contract whose single
        lot already exceeds the target (e.g. COPPER ≈ ₹21L/lot) trades 1
        lot. Multiplied by the configured `lots_per_trade` so the existing
        config still scales the target up.
        """
        base_lots = max(1, int(self._lots_per_trade or 1))
        per_lot_notional = float(getattr(spec, "futures_lot_size", 0) or 0) * float(price or 0.0)
        if per_lot_notional <= 0:
            return base_lots
        target_value = COMMODITY_TARGET_POSITION_VALUE * base_lots
        lots = round(target_value / per_lot_notional)
        return max(1, int(lots))

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
        lane_map = {lane.descriptor.key: lane for lane in self._strategy_agents()}
        futures_positions = lane_map["commodity_futures"].open_positions()
        return [
            {
                "key": "commodity_futures",
                "title": "MP+OF Futures",
                "agent": lane_map["commodity_futures"].build_status_payload(),
                "status": "paper_execution" if self._symbols else "idle",
                "instrument": "MCX futures · MP+OF on 1-min closes",
                "tracked_symbols": len(self._symbols),
                "open_positions": futures_positions,
                "timeframe": lane_map["commodity_futures"].descriptor.timeframe,
                "execution_mode": lane_map["commodity_futures"].descriptor.execution_mode,
                "position_cap": lane_map["commodity_futures"].descriptor.position_cap,
                "lots_per_trade": self._lots_per_trade,
                "broker": "upstox primary · fyers fallback",
                "notes": (
                    "Entries are driven by the Market-Profile + Order-Flow evaluator on closed 1-minute bars. "
                    "Triggers in priority: open_drive, ib_break, failed_auction, va_migration, lvn_fade. "
                    "Stop placement honours per-trigger hints (clamped to 0.5% min); target = 2R; "
                    "BE move at 1R, partial lock at 1.5R, ATR trail at 1.25× after the runner arms."
                ),
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
            candidate_signal = signal or str(row.get("candidate_signal") or "")
            bar_time = str(row.get("bar_time") or "")
            spec = get_commodity_contract_spec(symbol)
            price = float(row.get("price") or 0.0)
            lots = self._target_lots_for_contract(spec, price)
            qty = spec.futures_lot_size * lots
            event_reason = _commodity_event_block_reason(underlying)
            risk_block = self._entry_risk_block(underlying)
            validation = "waiting_trigger"
            validation_detail = (
                "Awaiting an MP+OF trigger (open_drive, ib_break, failed_auction, "
                "va_migration, or lvn_fade) on the next closed 1-minute bar."
            )
            if row.get("reason") == "insufficient_data":
                validation = "warming_up"
                validation_detail = "More 1-minute candles required before MP+OF triggers can evaluate."
            elif row.get("mp_status") == "warming_up":
                validation = "mp_warming_up"
                validation_detail = (
                    f"Only {row.get('mp_periods', 0)} TPO periods printed — need ≥ "
                    f"{FUTURES_MP_MIN_PERIODS} for IB-based triggers."
                )
            elif signal in {"BUY", "SELL"} and (price <= 0 or float(row.get("atr") or 0.0) <= 0):
                validation = "price_unavailable"
                validation_detail = "Trigger fired, but price or 1-min ATR is missing — entry blocked."
            elif signal in {"BUY", "SELL"} and self._kill_switch_active:
                validation = "blocked_kill_switch"
                validation_detail = "Kill switch is active. Trigger recorded but the execution lane is paused."
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
                validation_detail = "This 1-minute bar already triggered an entry."
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
                        entry_style = str(row.get("entry_style") or "trigger")
                        confidence = float(row.get("confidence") or 0.0)
                        validation_detail = str(
                            row.get("signal_validation_detail")
                            or f"{entry_style} fired ({signal}, confidence {confidence:.2f}) — aligned for entry."
                        )

            # Bucket info — pass MP+OF signal context. classify_signal_bucket
            # accepts MACD-shaped fields; for the new lane we leave them None
            # and rely on `signal_validation` + `entry_style` to drive the bucket.
            bucket_info = classify_signal_bucket(
                has_position=self._has_any_underlying_position(underlying),
                signal_validation=validation,
                macd=None,
                macd_histogram=None,
                prev_macd=None,
                prev_macd_histogram=None,
                recent_cross_signal=row.get("entry_style"),
                recent_cross_bars_ago=0 if signal in {"BUY", "SELL"} else None,
            )

            decorated.append(
                {
                    **row,
                    "indicator_timeframe": FUTURES_TIMEFRAME,
                    "display_name": spec.display_name,
                    "lot_size": spec.futures_lot_size,
                    # Effective lots for THIS contract, sized to the equal
                    # notional target (see _target_lots_for_contract).
                    "lots_per_trade": lots,
                    "lots": lots,
                    "target_position_value": COMMODITY_TARGET_POSITION_VALUE,
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
        # Options sleeve removed; method kept as a no-op safety net for any
        # call site that survived the refactor. Always returns an empty list.
        return []

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

    async def _load_prior_session_profile(self, symbol: str):
        """Build the previous trading session's MP snapshot, cached in-process.

        Cache is keyed by `(symbol, today_session_date)` so it stays warm for
        the duration of the day and re-builds automatically when a new session
        rolls. Used by the open_drive and va_migration triggers; quietly None
        when fewer than 2 sessions are present in the broker's 1-min history.
        """
        today = _now_ist().date()
        cache_key = (symbol, today)
        if cache_key in self._prior_mp_cache:
            return self._prior_mp_cache[cache_key]

        prior_profile = None
        try:
            candles = await self._load_history(symbol, interval="1minute", lookback_days=5)
            if candles:
                # Split out the most recent session, then take the session
                # immediately before it.
                closed = _filter_closed_interval_rows(candles, interval="1minute")
                latest_session, latest_date = _latest_session_rows(closed)
                # Walk backwards to find the prior session.
                if latest_date is not None:
                    prior_rows = [
                        c for c in closed
                        if _parse_iso_timestamp(c.get("time")) is not None
                        and _parse_iso_timestamp(c.get("time")).astimezone(IST).date() < latest_date
                    ]
                    if prior_rows:
                        prior_session, _prior_date = _latest_session_rows(prior_rows)
                        if prior_session:
                            prior_profile = self._build_market_profile(symbol, prior_session)
        except Exception as exc:
            logger.debug(f"[CommodityStrategy] prior MP load failed for {symbol}: {exc}")
            prior_profile = None

        self._prior_mp_cache[cache_key] = prior_profile
        return prior_profile

    async def _analyze_futures_symbol(
        self,
        symbol: str,
        live_ltp: Optional[float],
    ) -> Optional[dict[str, Any]]:
        spec = get_commodity_contract_spec(symbol)
        candles = await self._load_history(symbol, interval=FUTURES_TIMEFRAME, lookback_days=2)
        if not candles:
            self._append_commentary("warning", f"{symbol}: no 1-min futures candles returned by broker.")
            return None

        closed = _filter_closed_interval_rows(candles, interval=FUTURES_TIMEFRAME)
        if not closed:
            return None

        latest_close = float(closed[-1].get("close") or 0.0)
        quote_snapshot = self._last_quote_snapshots.get(symbol) or {}
        previous_close = quote_snapshot.get("previous_close")
        if previous_close is None and len(closed) >= 2:
            previous_close = float(closed[-2].get("close") or 0.0)
        price = float(live_ltp or latest_close or 0.0)
        change = quote_snapshot.get("change")
        if change is None and price and previous_close:
            change = price - float(previous_close)
        change_pct = quote_snapshot.get("change_pct")
        if change_pct is None and price and previous_close:
            change_pct = ((price - float(previous_close)) / float(previous_close)) * 100.0

        session_rows, session_date = _latest_session_rows(closed)
        today_profile = self._build_market_profile(symbol, session_rows)
        prior_profile = await self._load_prior_session_profile(symbol)
        cvd_anchor_index = _infer_09ist_anchor(closed)
        atr_1m = _compute_atr_series(closed, period=14)

        result = evaluate_commodity_mp_signal(
            closed,
            symbol=symbol,
            today_profile=today_profile,
            prior_profile=prior_profile,
            cvd_anchor_index=cvd_anchor_index,
            atr_1m=atr_1m,
        )

        # Persist daily snapshots so the historical timeline grows on its own.
        # We snapshot:
        #   - today_profile once Initial Balance has fully printed (so we don't
        #     churn the disk with intra-IB rewrites). The latest scan of the
        #     day wins via idempotent overwrite — by EOD the file holds the
        #     final auction.
        #   - prior_profile every time we load it. This bootstraps the
        #     "Yesterday" tile on first deploy instead of forcing the desk to
        #     wait one session for history to fill in.
        try:
            from paper_engine.commodity_profile_store import (
                build_daily_profile_from_snapshot,
                save_profile,
            )

            if today_profile is not None and int(getattr(today_profile, "period_count", 0) or 0) >= FUTURES_MP_MIN_PERIODS:
                snapshot = build_daily_profile_from_snapshot(spec.root, today_profile)
                if snapshot is not None:
                    save_profile(snapshot)

            if prior_profile is not None:
                prior_snapshot = build_daily_profile_from_snapshot(spec.root, prior_profile)
                if prior_snapshot is not None:
                    save_profile(prior_snapshot)
        except Exception as persist_exc:
            logger.debug(
                f"[CommodityStrategy] daily profile persist skipped for {spec.root}: {persist_exc}"
            )

        # Attach the full prior-session profile to the row payload so the
        # detail modal can render "Last day" TPO immediately — without waiting
        # for the historical-timeline endpoint to roll. The frontend reads
        # this in preference to the persisted aggregate when present.
        if prior_profile is not None:
            try:
                tpo_letters_raw = dict(getattr(prior_profile, "tpo_letters", {}) or {})
                tpo_counts_raw = dict(getattr(prior_profile, "tpo_counts", {}) or {})
                prior_session_date = getattr(prior_profile, "session_date", None)
                result["prior_session_profile"] = {
                    "session_date": (
                        prior_session_date.isoformat()
                        if hasattr(prior_session_date, "isoformat")
                        else (str(prior_session_date) if prior_session_date else None)
                    ),
                    "poc": _round_or_none(getattr(prior_profile, "poc", None), 2),
                    "vah": _round_or_none(getattr(prior_profile, "vah", None), 2),
                    "val": _round_or_none(getattr(prior_profile, "val", None), 2),
                    "ib_high": _round_or_none(getattr(prior_profile, "initial_balance_high", None), 2),
                    "ib_low": _round_or_none(getattr(prior_profile, "initial_balance_low", None), 2),
                    "high": _round_or_none(getattr(prior_profile, "high_price", None), 2),
                    "low": _round_or_none(getattr(prior_profile, "low_price", None), 2),
                    "tick_size": _round_or_none(getattr(prior_profile, "tick_size", None), 4),
                    "tpo_letters": {str(k): str(v) for k, v in tpo_letters_raw.items()},
                    "tpo_counts": {str(k): int(v) for k, v in tpo_counts_raw.items()},
                    "single_prints": [
                        float(x)
                        for x in list(getattr(prior_profile, "single_prints", []) or [])
                    ],
                    "poor_high": bool(getattr(prior_profile, "poor_high", False)),
                    "poor_low": bool(getattr(prior_profile, "poor_low", False)),
                    "period_count": int(getattr(prior_profile, "period_count", 0) or 0),
                }
            except Exception as prior_exc:
                logger.debug(
                    f"[CommodityStrategy] prior profile payload skipped for {spec.root}: {prior_exc}"
                )

        # Attach instrument-shape fields the harness/decorator need.
        result.update(
            {
                "symbol": symbol,
                "underlying": spec.root,
                "display_name": spec.display_name,
                "price": _round_or_none(price, 2),
                "previous_close": _round_or_none(previous_close, 2),
                "change": _round_or_none(change, 2),
                "change_pct": _round_or_none(change_pct, 2),
                "indicator_timeframe": FUTURES_TIMEFRAME,
                "mp_session_date": session_date.isoformat() if session_date else result.get("mp_session_date"),
            }
        )
        return result

    async def _build_option_watchlist(self) -> list[dict[str, Any]]:
        # Options sleeve removed; this stub stays so any leftover call site
        # gets an empty list instead of an AttributeError.
        return []

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
        # Options sleeve removed.
        return [], []

    @staticmethod
    def _overlay_live_option_quotes(
        rows: list[dict[str, Any]],
        option_quote_map: dict[str, float],
    ) -> list[dict[str, Any]]:
        # Options sleeve removed.
        return rows

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

        # Options sleeve removed — no option watchlist to build.
        option_rows: list[dict[str, Any]] = []
        option_quote_map: dict[str, float] = {}

        latest_prices = {
            row["symbol"]: float(row["price"])
            for row in futures_rows
            if row.get("price") is not None
        }
        if latest_prices:
            self._runtime.portfolio.update_prices(latest_prices)

        self._runtime.futures_watchlist = futures_rows
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
            f"Market closed. Prepared for next MCX session: {len(futures_rows)} futures rows. "
            "No commodity entries are opened while MCX is closed."
        )
        retention_parts: list[str] = []
        if retained_futures:
            retention_parts.append(f"retained {len(retained_futures)} futures rows")
        if retention_parts:
            self._last_message = f"{self._last_message} Reused the last good snapshot for {', '.join(retention_parts)}."
        health_warning = self._option_history_warning(option_history_health)
        if health_warning:
            self._last_message = f"{self._last_message} {health_warning}"
            self._append_commentary("warning", health_warning)
        self._append_commentary("info", self._last_message)
        return self.get_status(refresh=False)

    async def _analyze_option_row(self, row: dict[str, Any]) -> Optional[dict[str, Any]]:
        # Options sleeve removed; method retained as a no-op safety stub.
        return None

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

                if not _in_commodity_hours(started_at):
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
                # Persist immediately so concurrent get_status refreshes from
                # the API (e.g. dashboard polling) cannot reload an older DB
                # snapshot and wipe the audit additions we just made.
                self._persist_state()
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

                # Options sleeve removed — no option watchlist to build.
                option_rows: list[dict[str, Any]] = []
                option_quote_map: dict[str, float] = {}

                latest_prices = {
                    row["symbol"]: float(row["price"])
                    for row in futures_rows
                    if row.get("price") is not None
                }
                if latest_prices:
                    self._runtime.portfolio.update_prices(latest_prices)

                await self._manage_positions(adapter, futures_rows, option_rows, option_quote_map=option_quote_map)
                current_drawdown_pct = self._current_drawdown_pct()
                if current_drawdown_pct >= COMMODITY_MAX_DRAWDOWN_PCT:
                    await self._record_drawdown_risk_block(drawdown_pct=current_drawdown_pct)
                if self._kill_switch_active:
                    actionable_futures = [row for row in futures_rows if row.get("signal_validation") == "ready"]
                    if actionable_futures:
                        self._append_commentary(
                            "warning",
                            f"Commodity kill switch active. {len(actionable_futures)} actionable signals observed, but no new entries were placed.",
                        )
                else:
                    lane_rows = {
                        "commodity_futures": futures_rows,
                    }
                    for lane in self._strategy_agents():
                        await lane.run_entries(lane_rows.get(lane.descriptor.key, []))

                self._runtime.futures_watchlist = futures_rows

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
                    f"Scanned {len(futures_rows)} futures rows. {open_positions} open positions."
                )
                # Options were deprecated from the commodity desk; only the
                # futures watchlist is stabilized/retained now. (The previous
                # `retained_options` reference here was never assigned in this
                # scope and raised NameError on every scan where no futures
                # rows were retained.)
                if retained_futures:
                    self._last_message = (
                        f"{self._last_message} Reused the last good snapshot for "
                        f"retained {len(retained_futures)} futures rows."
                    )
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

        # Build a root → active-row lookup so we can detect stranded
        # positions that survived a contract rollover (e.g. a MAY position
        # left behind after the agent rolled to JUN). Without this they
        # never get managed and quietly stay open forever.
        rows_by_root: dict[str, dict[str, Any]] = {}
        for row in futures_rows:
            root = (extract_commodity_root(str(row.get("symbol") or "")) or "").upper()
            if root and root not in rows_by_root:
                rows_by_root[root] = row

        for position_key, position in list(self._runtime.positions.items()):
            reason: Optional[str] = None
            if position.strategy_key == "commodity_futures":
                row = futures_map.get(position.symbol)
                if not row:
                    # The exact symbol isn't being scanned this cycle. Two
                    # cases: (a) data gap — skip and wait; (b) contract
                    # rolled over — the active futures for this root is a
                    # different symbol now and the position is stranded.
                    root = (extract_commodity_root(position.symbol) or "").upper()
                    rolled_row = rows_by_root.get(root)
                    rolled_symbol = str((rolled_row or {}).get("symbol") or "")
                    if rolled_row and rolled_symbol and rolled_symbol != position.symbol:
                        # Close on the new active contract's latest price —
                        # the legacy contract no longer trades, so the next
                        # active month's mark is the best available exit.
                        exit_price = float(rolled_row.get("price") or position.current_price or 0.0)
                        await self._close_futures_position(
                            position_key,
                            position,
                            exit_price,
                            "expired_contract",
                            actor="strategy_agent_rollover",
                        )
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
                # Commodity exit cascade (2026-06-02) trimmed to match the
                # canonical "ride large directional moves" design:
                #   KEEP   — initial stop_loss (the original placement, NOT
                #            ratcheted before the +2R target hit)
                #   KEEP   — target_reached marker at +2R + ATR-trail on the
                #            runner (peak-trail with max(ATR×1.25, 1R) buffer)
                #   KEEP   — macd_reversal on opposite signal after 4-bar
                #            min hold (structural exit)
                #   REMOVE — BE move at +1R: it ratcheted the stop up to
                #            entry on the first +1R move, then any normal
                #            intra-move pullback knocked the trade out at
                #            break-even. This was the NG/Crude killer the
                #            user flagged: "trended but closed in loss"
                #            (after slippage/commission, BE exits are net
                #            loss). Now the trade either hits its ORIGINAL
                #            stop or rides to +2R where the trail engages.
                #   REMOVE — 1.5R partial lock at +0.5R: same flaw at smaller
                #            scale — knocks out the runner before structure
                #            actually breaks. No analog in the S1 design.
                trailing_label: Optional[str] = None
                if risk_distance > 0:
                    if position.action == "BUY":
                        favorable_move = current_price - position.entry_price
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
                    # Unified close path: stop_loss / trail_stop / macd_reversal /
                    # target exits all route through _close_futures_position so the
                    # trade is BOOKED (on_fill or self-heal book_close), audited, and
                    # written to the durable DB ledger consistently. The old inline
                    # block here audited the exit but omitted the self-heal book_close
                    # — so a close could be audited yet leave realized P&L unbooked
                    # ("exited but not logged"). The exit DECISION (`reason`) is
                    # unchanged; only the booking mechanism is unified.
                    await self._close_futures_position(position_key, position, current_price, reason)
                continue

            # Any non-futures position (legacy `commodity_options` rows
            # surviving in the persisted runtime) is ignored — the options
            # sleeve was deprecated and we no longer manage option exits.
            # Closed option trades remain in trade_history for audit; any
            # *open* legacy option position can be manually flat-closed via
            # the reset-paper endpoint if it exists.
            continue

    def _record_close_to_book(self, position: "CommodityPositionState", current_price: float, reason: str) -> None:
        """Append the just-closed trade to the durable DB ledger
        (paper_trade_book) with entry/exit timestamps. Best-effort — never
        raises into the close path. Survives paper-account resets, unlike the
        mutable runtime-state blob."""
        try:
            mult = 1.0 if str(getattr(position, "action", "") or "").upper() == "BUY" else -1.0
            entry = float(getattr(position, "entry_price", 0.0) or 0.0)
            qty = int(getattr(position, "qty", 0) or 0)
            record_paper_trade(
                market="commodity",
                strategy_key=getattr(position, "strategy_key", "commodity_futures"),
                session_id=getattr(self._runtime.portfolio, "session_id", None),
                symbol=getattr(position, "live_symbol", None) or getattr(position, "symbol", None),
                underlying=getattr(position, "underlying", None),
                instrument_type="FUT",
                action=getattr(position, "action", None),
                qty=qty,
                lots=getattr(position, "lots", None),
                entry_price=entry,
                exit_price=float(current_price),
                pnl=mult * (float(current_price) - entry) * qty,
                entry_time=_parse_datetime(getattr(position, "entered_at", None)),
                exit_time=datetime.now(timezone.utc),
                setup_type=getattr(position, "signal_reason", None),
                regime=getattr(position, "regime", None),
                exit_reason=reason,
                signal_id=getattr(position, "position_key", None),
            )
        except Exception as exc:
            logger.warning(f"[CommodityStrategy] trade-book DB record failed: {exc}")

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
        portfolio = self._runtime.portfolio
        trades_before = len(portfolio._trade_history)
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
        # Book the trade FIRST (on_fill above, or the self-heal here) so the exit is
        # never audited-but-unbooked. The self-heal is idempotent — book_close only
        # fires when order_book→on_fill did NOT already append a TradeRecord (the
        # ~6-day futures-ledger freeze where closes booked nothing → realized/Day/Life
        # P&L stuck). It also drops any phantom position on_fill may have opened.
        if len(portfolio._trade_history) == trades_before:
            portfolio._positions.pop(order.order_id, None)  # drop any phantom on_fill opened
            portfolio.book_close(
                symbol=position.live_symbol,
                entry_action=position.action,
                qty=position.qty,
                entry_price=position.entry_price,
                exit_price=current_price,
                opened_at=_parse_datetime(position.entered_at),
                instrument_type="FUT",
                signal_id=position.position_key,
                setup_type=position.signal_reason,
                regime=position.regime,
            )
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
        self._record_close_to_book(position, current_price, reason)

    async def _close_option_position(
        self,
        position_key: str,
        position: "CommodityPositionState",
        current_price: float,
        reason: str,
        exit_qty: int,
        *,
        keep_open: bool = False,
    ) -> None:
        # Options sleeve removed; no-op stub left in case any caller
        # survived the refactor. Open option positions in the historical
        # ledger remain as audit-only records.
        return None

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
            # Equal-notional sizing: lots chosen so this position ≈ the
            # target rupee value, the same for every contract.
            lots = self._target_lots_for_contract(spec, price)
            qty = spec.futures_lot_size * lots
            required_margin = self._estimate_futures_margin_required(price, qty)
            if required_margin > self._runtime.portfolio.available_capital:
                continue

            # Honour the MP+OF evaluator's `stop_hint` when present, clamped to
            # the FUTURES_MIN_STOP_PCT floor so we don't end up with absurdly
            # tight stops. Falls back to the ATR/MP-level rule for any signal
            # that didn't carry an explicit hint.
            min_stop_distance = max(atr, price * FUTURES_MIN_STOP_PCT)
            stop_hint = row.get("stop_hint")
            try:
                stop_hint_f = float(stop_hint) if stop_hint is not None else None
            except (TypeError, ValueError):
                stop_hint_f = None
            if row.get("signal") == "BUY":
                stop_candidates = [price - min_stop_distance]
                if stop_hint_f is not None and stop_hint_f < price and (price - stop_hint_f) >= min_stop_distance:
                    stop_candidates.append(stop_hint_f)
                for level in (row.get("mp_val"), row.get("mp_ib_low")):
                    if level is not None:
                        level_value = float(level)
                        if level_value < price and (price - level_value) >= min_stop_distance:
                            stop_candidates.append(level_value)
                stop_price = max(stop_candidates)
                target_price = price + ((price - stop_price) * 2.0)
            else:
                stop_candidates = [price + min_stop_distance]
                if stop_hint_f is not None and stop_hint_f > price and (stop_hint_f - price) >= min_stop_distance:
                    stop_candidates.append(stop_hint_f)
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
                lots=lots,
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
                lots=lots,
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
                entry_style=str(row.get("entry_style") or "mp_signal"),
                last_reviewed_bar_time=bar_time,
            )
            self._runtime.processed_signals[f"commodity_futures:{symbol}"] = bar_time
            self._append_commentary(
                "trade",
                f"ENTRY {spec.display_name} {row.get('signal')} @{fill_price:.2f} | {lots} lot | "
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
                    f"({lots} lot; MP {row.get('mp_day_type')}; stop ₹{stop_price:,.2f})"
                ),
                new_state="open",
                payload={
                    "side": str(row.get("signal") or ""),
                    "fill_price": round(fill_price, 2),
                    "stop_price": round(stop_price, 2),
                    "target_price": round(target_price, 2),
                    "qty": qty,
                    "lots": lots,
                    "regime": str(row.get("mp_day_type") or row.get("regime") or ""),
                    "entry_style": str(row.get("entry_style") or "mp_signal"),
                    "confidence": float(row.get("confidence") or 0.0),
                    "stop_hint": row.get("stop_hint"),
                    "trigger_evidence": row.get("trigger_evidence") or {},
                },
            )

    async def _open_new_option_positions(self, option_rows: list[dict[str, Any]]) -> None:
        # Options sleeve removed; no-op stub.
        return None

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
            # Heartbeat audit: log one row per (bar, symbol, validation, signal)
            # tuple so the operator can see the agent is scanning even on calm
            # bars. Skip only data-warmup states where no decision could be
            # made — logging those would be noise.
            if validation in {"warming_up", "mp_warming_up"}:
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

    def _audit_option_watchlist(self, rows: list[dict[str, Any]]) -> None:
        # Options sleeve removed; no-op stub.
        return None

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

    def _durable_closed_trades(self) -> list[dict[str, Any]]:
        """Closed trades for the trade log, sourced from the DURABLE DB ledger
        (paper_trade_book — survives paper-account resets) merged with any
        in-session trades not yet persisted there. Deduped by
        (symbol, entry_price, exit_price, qty)."""
        try:
            rows = [_db_trade_to_row(r) for r in load_paper_trade_book(market="commodity", limit=500)]
        except Exception:
            rows = []

        def _key(t: dict[str, Any]) -> tuple[Any, ...]:
            return (
                str(t.get("symbol")),
                round(float(t.get("entry_price") or 0.0), 2),
                round(float(t.get("exit_price") or 0.0), 2),
                int(t.get("qty") or 0),
            )

        seen = {_key(r) for r in rows}
        for t in _serialize_trade_history(self._runtime.portfolio):
            if _key(t) not in seen:
                rows.append({**t, "status": "closed"})
                seen.add(_key(t))
        return rows

    def get_status(self, *, refresh: bool = True) -> dict[str, Any]:
        if refresh:
            self._refresh_state_from_store()
        summary = self._runtime.portfolio.get_summary()
        lane_agents = self._strategy_agents()
        lane_map = {lane.descriptor.key: lane for lane in lane_agents}
        futures_ready = lane_map["commodity_futures"].ready_signals()
        last_error = self._last_error
        last_message = self._last_message
        if (
            isinstance(last_error, str)
            and last_error.startswith("No commodity broker adapter is available.")
            and self._runtime.futures_watchlist
        ):
            last_error = None
            last_message = "Using prepared MCX futures watchlist until the next scan refreshes broker data."
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
            "trading_calendar": trading_calendar.exchange_status("MCX"),
            "config": {
                "symbols": list(self._symbols),
                "futures_timeframe": FUTURES_TIMEFRAME,
                "mp_period_minutes": FUTURES_MP_PERIOD_MINUTES,
                "cvd_anchor_hour_ist": FUTURES_CVD_ANCHOR_HOUR_IST,
                "mp_min_periods": FUTURES_MP_MIN_PERIODS,
                "lots_per_trade": self._lots_per_trade,
                "target_position_value": COMMODITY_TARGET_POSITION_VALUE,
                "futures_min_hold_bars": FUTURES_MIN_HOLD_BARS,
                "futures_min_stop_pct": FUTURES_MIN_STOP_PCT,
                "futures_trail_atr_multiplier": FUTURES_TRAIL_ATR_MULTIPLIER,
                "futures_target_arm_r_multiplier": FUTURES_TARGET_ARM_R_MULTIPLIER,
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
                "ready_option_signals": 0,
            },
            "watchlist": list(self._runtime.futures_watchlist),
            "futures_watchlist": list(self._runtime.futures_watchlist),
            "option_watchlist": [],
            "positions": [
                {
                    **asdict(position),
                    "unrealized_pnl": _round_or_none(position.unrealized_pnl, 2),
                    "return_pct": _round_or_none(position.return_pct, 2),
                    "notional_value": _round_or_none(position.current_price * position.qty, 2),
                }
                for position in self._runtime.positions.values()
            ],
            # Trade log = closed round-trips (from the ledger) PLUS the
            # currently-open positions surfaced as open trade rows, so a trade is
            # recorded the instant it opens. _split_today_history buckets by
            # exit_time→entry_time, so today's opens land in today_trades.
            **(lambda closed, open_rows: {
                "trade_history": _sort_trades_recent_first(open_rows + closed),
                "today_trades": _split_today_history(open_rows + closed)[0],
                "historical_trades": _split_today_history(open_rows + closed)[1],
            })(
                self._durable_closed_trades(),
                [_position_open_trade_row(p) for p in self._runtime.positions.values()],
            ),
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
        # Defensively wipe the portfolio's history series so any prior
        # snapshot accumulation (drawdown / sharpe / peak_equity) doesn't
        # bleed into the fresh paper account on the dashboard.
        self._runtime.portfolio._trade_history = []
        self._runtime.portfolio._equity_curve = []
        self._runtime.portfolio._daily_pnl = defaultdict(float)
        self._runtime.portfolio._peak_equity = self._runtime.portfolio.initial_capital
        self._runtime.portfolio._positions = {}
        # Without this, any prior realized-PnL that had been refunded to
        # cash before the reset survives as phantom equity.
        self._runtime.portfolio.available_capital = self._runtime.portfolio.initial_capital
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
