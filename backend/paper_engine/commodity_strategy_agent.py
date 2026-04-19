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
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from analysis.macd_engine import compute_ema, compute_macd
from analytics.technicals import latest_macd_rsi
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
from core.runtime_state import load_runtime_state, save_runtime_state
from market_data.commodity_atm_watchlist import commodity_atm_watchlist_service
from market_data.commodity_contract_specs import (
    get_commodity_contract_spec,
    get_commodity_display_name,
)
from market_data.option_history import option_history_service
from market_data.upstox_commodity import load_upstox_mcx_quotes, resolve_upstox_mcx_future
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
FUTURES_TARGET_ARM_R_MULTIPLIER = 2.0

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
OPTIONS_CAPITAL_FRACTION = 0.20
OPTIONS_MAX_POSITIONS = 2


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


def _parse_datetime(value: Any) -> Optional[datetime]:
    return _parse_iso_timestamp(value)


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
    default_state["control"] = {
        "kill_switch_active": bool(control_payload.get("kill_switch_active", False)),
        "start_required": bool(
            control_payload.get(
                "start_required",
                control_payload.get("kill_switch_active", False),
            )
        ),
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


def evaluate_commodity_signal(
    candles: list[dict[str, Any]],
    *,
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
            "atr": None,
            "bar_time": None,
        }

    closes = [float(candle.get("close") or 0.0) for candle in candles]
    macd_line, signal_line, histogram = compute_macd(closes, fast, slow, signal_period)
    latest_macd = macd_line[-1]
    previous_macd = macd_line[-2]
    latest_signal = signal_line[-1]
    latest_hist = histogram[-1]
    latest_close = closes[-1]
    previous_close = closes[-2]
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

    return {
        "signal": signal,
        "reason": reason,
        "regime": regime,
        "latest_close": latest_close,
        "previous_close": previous_close,
        "macd": _round_or_none(latest_macd, 4),
        "macd_signal": _round_or_none(latest_signal, 4),
        "macd_histogram": _round_or_none(latest_hist, 4),
        "atr": _round_or_none(latest_atr, 4),
        "bar_time": str(candles[-1].get("time") or ""),
        "recent_cross_signal": recent_cross_signal,
        "recent_cross_bars_ago": recent_cross_bars_ago,
        "continuation_signal": continuation_signal,
        "continuation_reason": continuation_reason,
    }


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
    expiry: Optional[str] = None
    strike: Optional[float] = None
    option_type: Optional[str] = None
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
        self._commentary: list[CommodityCommentaryEntry] = []
        self._state_synced_at: Optional[datetime] = None
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
                    expiry=row.get("expiry"),
                    strike=float(row["strike"]) if row.get("strike") is not None else None,
                    option_type=row.get("option_type"),
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
                opened_at=_parse_datetime(position.entered_at) or datetime.utcnow(),
            )
            for position in self._runtime.positions.values()
        }

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
        if self._start_required and not force:
            self._persist_state()
            return
        if self._loop_active():
            return
        self._start_required = False
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
                "notes": "Entries use closed 15-minute bars only, accept fresh zero-crosses plus continuation breakouts after a recent cross, and keep hard stops live while delaying soft exits until the trade has had time to work. MCX futures quotes/history prefer Upstox and fall back to FYERS.",
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
                "broker": "fyers chain · upstox spot assist",
                "notes": "Entries use liquid near-ATM contracts, 30-minute MACD zero-cross, 25% hard stop, and 20% capital budget per trade. Underlying MCX spot quotes prefer Upstox before FYERS.",
            },
        ]

    def _decorate_futures_rows(self, watch_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        decorated: list[dict[str, Any]] = []
        futures_positions = sum(1 for pos in self._runtime.positions.values() if pos.strategy_key == "commodity_futures")
        at_capacity = futures_positions >= FUTURES_MAX_POSITIONS
        for row in watch_rows:
            symbol = str(row.get("symbol") or "")
            signal = str(row.get("signal") or "")
            raw_signal = str(row.get("raw_signal") or "")
            continuation_signal = str(row.get("continuation_signal") or "")
            candidate_signal = signal or raw_signal or continuation_signal
            bar_time = str(row.get("bar_time") or "")
            spec = get_commodity_contract_spec(symbol)
            price = float(row.get("price") or 0.0)
            qty = spec.futures_lot_size * self._lots_per_trade
            validation = "waiting_cross"
            validation_detail = "Waiting for a closed 15-minute MACD cross or a continuation breakout."
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
            elif self._has_underlying_position("commodity_futures", str(row.get("underlying") or spec.root)):
                validation = "position_open"
                validation_detail = "A futures position is already open for this underlying."
            elif signal in {"BUY", "SELL"} and self._runtime.processed_signals.get(f"commodity_futures:{symbol}") == bar_time:
                validation = "bar_consumed"
                validation_detail = "This 15-minute bar already triggered an entry."
            elif signal in {"BUY", "SELL"} and at_capacity:
                validation = "max_positions"
                validation_detail = "The futures sleeve is already at max open-position capacity."
            elif signal in {"BUY", "SELL"}:
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

            decorated.append(
                {
                    **row,
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
            validation = "waiting_cross"
            validation_detail = "Waiting for a fresh 30-minute option MACD cross."
            if row.get("regime") == "warmup":
                validation = "warming_up"
                validation_detail = "CE and PE candles need more history before the MACD quadrant is tradable."
            elif row.get("regime") == "dead_zone":
                validation = "dead_zone"
                validation_detail = "Both CE and PE MACD are below zero. Strategy 1 skips the dead zone."
            elif signal_side and not row.get("is_trade_contract_liquid"):
                validation = "illiquid_contract"
                validation_detail = "The nearest liquid contract filter rejected the current CE/PE candidate."
            elif signal_side and self._kill_switch_active:
                validation = "blocked_kill_switch"
                validation_detail = "Kill switch is active. Option entries are paused."
            elif signal_side and self._has_underlying_position("commodity_options", str(row.get("underlying") or "")):
                validation = "position_open"
                validation_detail = "An options position is already open for this underlying."
            elif signal_side and self._runtime.processed_signals.get(f"commodity_options:{trade_symbol}") == bar_time:
                validation = "bar_consumed"
                validation_detail = "This 30-minute option bar already triggered an entry."
            elif signal_side and at_capacity:
                validation = "max_positions"
                validation_detail = "The options sleeve is already at max open-position capacity."
            elif signal_side and int(row.get("lots_affordable") or 0) <= 0:
                validation = "insufficient_capital"
                validation_detail = "The 20% capital budget cannot fund one option lot at the current premium."
            elif signal_side:
                validation = "ready"
                validation_detail = "The selected CE/PE contract has a fresh 30-minute MACD trigger and passes the liquidity check."
            elif row.get("regime") in {"bullish", "bearish", "vol_spike"}:
                validation = "trend_aligned"
                validation_detail = "The MACD quadrant is aligned, but a fresh zero-cross has not fired yet."

            decorated.append(
                {
                    **row,
                    "signal_validation": validation,
                    "signal_validation_detail": validation_detail,
                    "strategy_title": get_commodity_contract_spec(str(row.get("symbol") or "")).options_label,
                }
            )
        return decorated

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once(force=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                self._last_message = f"Commodity strategy error: {exc}"
                self._append_commentary("error", f"Loop failure: {exc}")
                logger.exception("[CommodityStrategy] loop failure")
            await asyncio.sleep(self.scan_interval_seconds)

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
        return _filter_closed_interval_rows(rows, interval=interval)

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

        analysis = evaluate_commodity_signal(candles)
        latest_close = analysis.get("latest_close")
        previous_close = analysis.get("previous_close")
        price = float(live_ltp or latest_close or 0.0)
        change_pct = None
        if price and previous_close:
            change_pct = ((price - previous_close) / previous_close) * 100.0

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
            if candidate_signal in {"BUY", "SELL"} and candidate_signal == mp_direction:
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
            fresh = fresh_by_symbol.get(symbol)
            if fresh is not None:
                stabilized.append(fresh)
                continue
            previous = previous_by_symbol.get(symbol)
            if previous is None:
                continue

            retained = self._mark_retained_watchlist_row(
                previous,
                note="Retained after a temporary futures history gap.",
            )
            quote = float((live_quotes or {}).get(symbol) or 0.0)
            previous_close = retained.get("previous_close")
            if quote > 0:
                retained["price"] = _round_or_none(quote, 2)
                try:
                    prior_close = float(previous_close or 0.0)
                except (TypeError, ValueError):
                    prior_close = 0.0
                if prior_close > 0:
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
        ce_candles = _filter_closed_interval_rows(ce_candles, interval=OPTIONS_TIMEFRAME)
        pe_candles = _filter_closed_interval_rows(pe_candles, interval=OPTIONS_TIMEFRAME)

        ce_closes = [float(item["close"]) for item in ce_candles if item.get("close") is not None]
        pe_closes = [float(item["close"]) for item in pe_candles if item.get("close") is not None]
        ce_analysis = evaluate_commodity_signal(ce_candles, fast=OPTIONS_MACD_FAST, slow=OPTIONS_MACD_SLOW, signal_period=OPTIONS_MACD_SIGNAL) if ce_candles else {"signal": None, "reason": "missing", "regime": "unknown", "bar_time": None}
        pe_analysis = evaluate_commodity_signal(pe_candles, fast=OPTIONS_MACD_FAST, slow=OPTIONS_MACD_SLOW, signal_period=OPTIONS_MACD_SIGNAL) if pe_candles else {"signal": None, "reason": "missing", "regime": "unknown", "bar_time": None}
        ce_indicators = latest_macd_rsi(ce_closes) if ce_closes else {"macd": None, "macd_signal": None, "macd_histogram": None, "rsi": None}
        pe_indicators = latest_macd_rsi(pe_closes) if pe_closes else {"macd": None, "macd_signal": None, "macd_histogram": None, "rsi": None}

        ce.update(ce_indicators)
        pe.update(pe_indicators)
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
        if regime == "bullish" and ce_analysis.get("signal") == "BUY":
            signal_side = "CE"
            signal_reason = "ce_macd_zero_cross"
            selected_side = ce
        elif regime == "bearish" and pe_analysis.get("signal") == "SELL":
            signal_side = "PE"
            signal_reason = "pe_macd_zero_cross"
            selected_side = pe
        elif regime == "vol_spike":
            if ce_analysis.get("signal") == "BUY":
                signal_side = "CE"
                signal_reason = "ce_macd_zero_cross_vol_spike"
                selected_side = ce
            elif pe_analysis.get("signal") == "SELL":
                signal_side = "PE"
                signal_reason = "pe_macd_zero_cross_vol_spike"
                selected_side = pe

        trade_price = 0.0
        trade_symbol = ""
        trade_strike: Optional[float] = None
        trade_bar_time: Optional[str] = None
        lots_affordable = 0
        capital_per_trade = self._runtime.portfolio.available_capital * OPTIONS_CAPITAL_FRACTION
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
            cost_per_lot = trade_price * max(spec.futures_lot_size, 1)
            lots_affordable = int(capital_per_trade // cost_per_lot) if cost_per_lot > 0 else 0
            is_trade_contract_liquid = bool(selected_side.get("is_liquid"))

        return {
            **row,
            "display_name": spec.display_name,
            "contract_unit_label": spec.contract_unit_label,
            "quote_unit_label": spec.quote_unit_label,
            "regime": regime,
            "signal_side": signal_side,
            "signal_reason": signal_reason,
            "trade_symbol": trade_symbol,
            "trade_strike": trade_strike,
            "trade_price": _round_or_none(trade_price, 2),
            "trade_bar_time": trade_bar_time,
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
                    self._last_message = "Waiting for MCX market hours."
                    self._append_commentary("idle", "Commodity market closed. Agent idle.")
                    return self.get_status(refresh=False)

                await ensure_fyers_session(force_validate=True)
                await ensure_upstox_session(force_validate=True)
                fyers_health = await get_fyers_token_health(force=True)
                upstox_health = await get_upstox_token_health(force=True)
                self._last_data_health = {
                    "fyers_token_health": fyers_health,
                    "upstox_token_health": upstox_health,
                    "option_history": option_history_service.get_health_snapshot(),
                }
                if not fyers_health.get("valid"):
                    message = self._fyers_failure_message(fyers_health)
                    self._last_error = message
                    self._last_message = message
                    self._append_commentary("error", message)
                    return self.get_status(refresh=False)

                adapter = await self._get_fyers_adapter()
                if not adapter:
                    self._last_message = "Fyers adapter is unavailable. Commodity agent cannot scan."
                    self._last_error = self._last_message
                    self._append_commentary("error", self._last_message)
                    return self.get_status(refresh=False)

                quote_map = await self._safe_get_ltp(adapter, self._symbols)
                futures_rows: list[dict[str, Any]] = []
                for symbol in self._symbols:
                    row = await self._analyze_futures_symbol(symbol, quote_map.get(symbol))
                    if row:
                        futures_rows.append(row)
                futures_rows = self._decorate_futures_rows(futures_rows)
                futures_rows, retained_futures = self._stabilize_futures_watchlist(
                    futures_rows,
                    live_quotes=quote_map,
                )
                self._audit_futures_watchlist(futures_rows)

                option_rows = self._decorate_option_rows(await self._build_option_watchlist())
                option_rows, retained_options = self._stabilize_option_watchlist(option_rows)

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

                await self._manage_positions(adapter, futures_rows, option_rows)
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

    async def _safe_get_ltp(self, adapter: BrokerAdapter, symbols: list[str]) -> dict[str, float]:
        quotes = await load_upstox_mcx_quotes(symbols)
        remaining_symbols = [symbol for symbol in symbols if symbol not in quotes]
        if not remaining_symbols:
            return quotes
        try:
            payload = await adapter.get_ltp(remaining_symbols)
        except Exception as exc:
            logger.warning(f"[CommodityStrategy] LTP fetch failed: {exc}")
            self._append_commentary("warning", f"Live LTP fetch failed. Using candle closes where available. ({exc})")
            return quotes
        for symbol in remaining_symbols:
            try:
                value = float(payload.get(symbol, 0) or 0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                quotes[symbol] = value
        return quotes

    async def _manage_positions(
        self,
        adapter: BrokerAdapter,
        futures_rows: list[dict[str, Any]],
        option_rows: list[dict[str, Any]],
    ) -> None:
        futures_map = {str(row["symbol"]): row for row in futures_rows}
        option_map: dict[str, dict[str, Any]] = {}
        for row in option_rows:
            for symbol_key in ("ce_symbol", "pe_symbol"):
                live_symbol = str(row.get(symbol_key) or "")
                if live_symbol:
                    option_map[live_symbol] = row
        missing_option_symbols = [
            pos.live_symbol
            for pos in self._runtime.positions.values()
            if pos.strategy_key == "commodity_options" and pos.live_symbol not in option_map
        ]
        option_quote_map = await self._safe_get_ltp(adapter, missing_option_symbols) if missing_option_symbols else {}

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
                    self._runtime.positions.pop(position_key, None)
                continue

            row = option_map.get(position.live_symbol)
            price_key = "ce_trade_price" if position.option_type == "CE" else "pe_trade_price"
            current_price = float((row or {}).get(price_key) or option_quote_map.get(position.live_symbol) or position.current_price)
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

    async def _close_option_position(
        self,
        position_key: str,
        position: CommodityPositionState,
        current_price: float,
        reason: str,
        qty: int,
        *,
        keep_open: bool = False,
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
            price = float(row.get("price") or 0.0)
            atr = float(row.get("atr") or 0.0)
            if price <= 0 or atr <= 0:
                continue
            qty = spec.futures_lot_size * self._lots_per_trade
            required_margin = self._estimate_futures_margin_required(price, qty)
            if required_margin > self._runtime.portfolio.available_capital:
                continue

            if row.get("signal") == "BUY":
                stop_candidates = [price - atr]
                for level in (row.get("mp_val"), row.get("mp_ib_low")):
                    if level is not None and float(level) < price:
                        stop_candidates.append(float(level))
                stop_price = max(stop_candidates)
                target_price = price + ((price - stop_price) * 2.0)
            else:
                stop_candidates = [price + atr]
                for level in (row.get("mp_vah"), row.get("mp_ib_high")):
                    if level is not None and float(level) > price:
                        stop_candidates.append(float(level))
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
            qty = spec.futures_lot_size * lots
            side = row.get("ce") if signal_side == "CE" else row.get("pe")
            if not side:
                continue

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
            )
            self._runtime.processed_signals[f"commodity_options:{trade_symbol}"] = trade_bar_time
            self._append_commentary(
                "trade",
                f"ENTRY {spec.display_name} {signal_side} @{fill_price:.2f} | {lots} lot | "
                f"20% capital budget | stop {stop_price:.2f}",
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
                "time": (order.fill_time or datetime.utcnow()).isoformat(),
                "order_id": order.order_id,
                "symbol": order.symbol,
                "action": order.action,
                "qty": order.qty,
                "lots": lots,
                "lot_size": lot_size,
                "order_type": order.order_type,
                "status": order.status,
                "fill_price": _round_or_none(order.fill_price, 2),
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
        self._kill_switch_active = bool(active)
        cancelled_orders = 0
        for order in list(self._runtime.order_book.get_open_orders(self._runtime.portfolio.session_id)):
            if self._runtime.order_book.cancel_order(order.order_id):
                cancelled_orders += 1

        if self._kill_switch_active:
            self._start_required = True
            await self._stop_loop()
            self._last_message = "Commodity kill switch active. Agent stopped. Release it and start the agent to resume scanning."
            self._append_commentary("warning", self._last_message)
        else:
            self._start_required = True
            self._last_message = "Commodity kill switch released. Start the commodity agent to resume scanning."
            self._append_commentary("success", self._last_message)

        self._persist_state()
        return self.get_control_state(cancelled_orders=cancelled_orders)

    def get_control_state(self, *, cancelled_orders: int = 0) -> dict[str, Any]:
        self._refresh_state_from_store()
        return {
            "market": "commodity",
            "auto_run_enabled": self._auto_run_enabled,
            "kill_switch_active": self._kill_switch_active,
            "loop_active": self._loop_active(),
            "start_required": self._start_required,
            "cancelled_orders": cancelled_orders,
        }

    def get_status(self, *, refresh: bool = True) -> dict[str, Any]:
        if refresh:
            self._refresh_state_from_store()
        summary = self._runtime.portfolio.get_summary()
        lane_agents = self._strategy_agents()
        lane_map = {lane.descriptor.key: lane for lane in lane_agents}
        option_ready = lane_map["commodity_options"].ready_signals()
        futures_ready = lane_map["commodity_futures"].ready_signals()
        return {
            "enabled": self._enabled,
            "auto_run_enabled": self._auto_run_enabled,
            "kill_switch_active": self._kill_switch_active,
            "loop_active": self._loop_active(),
            "start_required": self._start_required,
            "running": self._running,
            "scan_interval_seconds": self.scan_interval_seconds,
            "last_run_at": self._last_run_at,
            "last_error": self._last_error,
            "last_message": self._last_message,
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
                "futures_trail_atr_multiplier": FUTURES_TRAIL_ATR_MULTIPLIER,
                "futures_target_arm_r_multiplier": FUTURES_TARGET_ARM_R_MULTIPLIER,
                "option_capital_fraction": OPTIONS_CAPITAL_FRACTION,
                "option_hard_stop_pct": OPTIONS_HARD_STOP_PCT,
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


commodity_strategy_agent = CommodityStrategyAgent()
