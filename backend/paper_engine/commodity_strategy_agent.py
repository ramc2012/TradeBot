"""Fyers-first paper strategy agent for MCX commodity futures.

This agent is intentionally independent from the NSE options workflow:
- no Upstox dependency
- no expiry catalog dependency
- commodity config and paper runtime state persisted locally
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

from analysis.macd_engine import compute_ema
from api.routers.auth import ensure_fyers_session, get_active_adapter
from brokers.base import BrokerAdapter
from paper_engine.order_book import PaperOrder, PaperOrderBook
from paper_engine.portfolio import PaperPortfolio, TradeRecord, VirtualPosition

IST = timezone(timedelta(hours=5, minutes=30))

DEFAULT_COMMODITY_SCAN_INTERVAL_SECONDS = 30
DEFAULT_COMMODITY_HISTORY_DAYS = 14
DEFAULT_COMMODITY_FAST_EMA = 8
DEFAULT_COMMODITY_SLOW_EMA = 21
DEFAULT_COMMODITY_ATR_PERIOD = 14
DEFAULT_COMMODITY_BREAKOUT_LOOKBACK = 6
DEFAULT_COMMODITY_STOP_ATR = 1.2
DEFAULT_COMMODITY_TARGET_ATR = 2.4
DEFAULT_COMMODITY_MAX_POSITIONS = 3
DEFAULT_COMMODITY_POSITION_QTY = 1
DEFAULT_COMMODITY_REPORTS_MAX = 40
DEFAULT_COMMODITY_ORDERS_MAX = 80
DEFAULT_COMMODITY_COMMENTARY_MAX = 80
DEFAULT_COMMODITY_INITIAL_CAPITAL = 1_000_000.0


def _resolve_commodity_config_file() -> Path:
    env_path = os.environ.get("COMMODITY_CONFIG_FILE", "").strip()
    if env_path:
        return Path(env_path)
    docker_path = Path("/app/commodity_strategy.json")
    if docker_path.parent.is_dir():
        return docker_path
    return Path(__file__).resolve().parent.parent / "commodity_strategy.json"


_COMMODITY_CONFIG_FILE = _resolve_commodity_config_file()


def _canonicalize_symbol(raw_symbol: str) -> str:
    symbol = str(raw_symbol or "").strip().upper()
    if symbol.startswith("MCX:SILVERMIC") and symbol.endswith("FUT"):
        return symbol.replace("MCX:SILVERMIC", "MCX:SILVERM", 1)
    return symbol


def _now_ist() -> datetime:
    return datetime.now(IST)


def _in_commodity_hours(now: Optional[datetime] = None) -> bool:
    current = now or _now_ist()
    if current.weekday() >= 5:
        return False
    return time(9, 0) <= current.time() <= time(23, 30)


def _round_or_none(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric != numeric or numeric in (float("inf"), float("-inf")):
        return None
    return round(numeric, digits)


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


def _default_saved_state() -> dict[str, Any]:
    return {
        "config": {
            "symbols": [],
            "selected_option_expiries": {},
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
            "positions": [],
            "orders": [],
            "reports": [],
            "commentary": [],
            "processed_signals": {},
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


def _parse_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _serialize_trade_history(portfolio: PaperPortfolio) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for trade in getattr(portfolio, "_trade_history", []):
        rows.append(
            {
                "symbol": trade.symbol,
                "action": trade.action,
                "qty": int(trade.qty),
                "entry_price": float(trade.entry_price),
                "exit_price": float(trade.exit_price),
                "pnl": float(trade.pnl),
                "entry_time": trade.entry_time.isoformat(),
                "exit_time": trade.exit_time.isoformat(),
                "instrument_type": trade.instrument_type,
                "expiry": trade.expiry,
                "strike": trade.strike,
                "option_type": trade.option_type,
            }
        )
    return rows


def _deserialize_trade_history(rows: list[dict[str, Any]]) -> list[TradeRecord]:
    trades: list[TradeRecord] = []
    for row in rows:
        entry_time = _parse_datetime(row.get("entry_time"))
        exit_time = _parse_datetime(row.get("exit_time"))
        if entry_time is None or exit_time is None:
            continue
        try:
            trades.append(
                TradeRecord(
                    symbol=str(row.get("symbol") or ""),
                    action=str(row.get("action") or ""),
                    qty=int(row.get("qty") or 0),
                    entry_price=float(row.get("entry_price") or 0.0),
                    exit_price=float(row.get("exit_price") or 0.0),
                    pnl=float(row.get("pnl") or 0.0),
                    entry_time=entry_time,
                    exit_time=exit_time,
                    instrument_type=str(row.get("instrument_type") or "FUT"),
                    expiry=row.get("expiry"),
                    strike=float(row["strike"]) if row.get("strike") is not None else None,
                    option_type=row.get("option_type"),
                )
            )
        except (TypeError, ValueError):
            continue
    return trades


def _load_saved_state() -> dict[str, Any]:
    if not _COMMODITY_CONFIG_FILE.exists():
        return _default_saved_state()
    try:
        payload = json.loads(_COMMODITY_CONFIG_FILE.read_text())
    except Exception as exc:
        logger.warning(f"[CommodityStrategy] Failed to load {_COMMODITY_CONFIG_FILE}: {exc}")
        return _default_saved_state()

    default_state = _default_saved_state()
    if "config" in payload or "runtime" in payload or "control" in payload:
        config_payload = payload.get("config") or {}
        control_payload = payload.get("control") or {}
        runtime_payload = payload.get("runtime") or {}
    else:
        config_payload = payload
        control_payload = {}
        runtime_payload = {}

    symbols = _normalize_symbols(list(config_payload.get("symbols") or []))
    selected_option_expiries = _normalize_selected_option_expiries(
        symbols,
        config_payload.get("selected_option_expiries"),
    )

    default_state["config"] = {
        "symbols": symbols,
        "selected_option_expiries": selected_option_expiries,
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
        str(_canonicalize_symbol(symbol)): str(bar_time)
        for symbol, bar_time in dict(runtime_payload.get("processed_signals") or {}).items()
        if str(symbol or "").strip() and str(bar_time or "").strip()
    }
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


def _load_saved_config() -> dict[str, Any]:
    return dict(_load_saved_state()["config"])


def _save_state(state: dict[str, Any]) -> None:
    try:
        _COMMODITY_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _COMMODITY_CONFIG_FILE.write_text(
            json.dumps(state, indent=2)
        )
    except Exception as exc:
        logger.warning(f"[CommodityStrategy] Failed to persist {_COMMODITY_CONFIG_FILE}: {exc}")

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


def evaluate_commodity_signal(
    candles: list[dict[str, Any]],
    *,
    fast_ema: int = DEFAULT_COMMODITY_FAST_EMA,
    slow_ema: int = DEFAULT_COMMODITY_SLOW_EMA,
    atr_period: int = DEFAULT_COMMODITY_ATR_PERIOD,
    breakout_lookback: int = DEFAULT_COMMODITY_BREAKOUT_LOOKBACK,
) -> dict[str, Any]:
    required = max(slow_ema + 2, atr_period + 2, breakout_lookback + 2)
    if len(candles) < required:
        return {
            "signal": None,
            "reason": "insufficient_data",
            "regime": "unknown",
            "latest_close": None,
            "previous_close": None,
            "ema_fast": None,
            "ema_slow": None,
            "atr": None,
            "breakout_high": None,
            "breakout_low": None,
            "bar_time": None,
        }

    closes = [float(candle.get("close") or 0.0) for candle in candles]
    highs = [float(candle.get("high") or candle.get("close") or 0.0) for candle in candles]
    lows = [float(candle.get("low") or candle.get("close") or 0.0) for candle in candles]

    ema_fast_values = compute_ema(closes, fast_ema)
    ema_slow_values = compute_ema(closes, slow_ema)
    atr_values = _compute_atr(candles, atr_period)

    latest_close = closes[-1]
    previous_close = closes[-2]
    latest_fast = ema_fast_values[-1]
    latest_slow = ema_slow_values[-1]
    latest_atr = atr_values[-1]
    breakout_high = max(highs[-(breakout_lookback + 1):-1])
    breakout_low = min(lows[-(breakout_lookback + 1):-1])
    bar_time = str(candles[-1].get("time") or "")

    if latest_fast is None or latest_slow is None:
        return {
            "signal": None,
            "reason": "ema_not_ready",
            "regime": "unknown",
            "latest_close": latest_close,
            "previous_close": previous_close,
            "ema_fast": None,
            "ema_slow": None,
            "atr": _round_or_none(latest_atr, 4),
            "breakout_high": breakout_high,
            "breakout_low": breakout_low,
            "bar_time": bar_time,
        }

    if latest_close > latest_fast > latest_slow:
        regime = "bullish"
    elif latest_close < latest_fast < latest_slow:
        regime = "bearish"
    else:
        regime = "range"

    signal: Optional[str] = None
    reason = "no_breakout"
    if regime == "bullish" and latest_close > breakout_high and previous_close <= breakout_high:
        signal = "BUY"
        reason = "bullish_breakout"
    elif regime == "bearish" and latest_close < breakout_low and previous_close >= breakout_low:
        signal = "SELL"
        reason = "bearish_breakdown"

    return {
        "signal": signal,
        "reason": reason,
        "regime": regime,
        "latest_close": latest_close,
        "previous_close": previous_close,
        "ema_fast": _round_or_none(latest_fast, 2),
        "ema_slow": _round_or_none(latest_slow, 2),
        "atr": _round_or_none(latest_atr, 4),
        "breakout_high": _round_or_none(breakout_high, 2),
        "breakout_low": _round_or_none(breakout_low, 2),
        "bar_time": bar_time,
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
    symbol: str
    action: str
    qty: int
    entry_price: float
    current_price: float
    stop_price: float
    target_price: float
    regime: str
    signal_reason: str
    atr: Optional[float]
    entered_at: str
    entry_bar_time: str

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
    watchlist: list[dict[str, Any]] = field(default_factory=list)
    processed_signals: dict[str, str] = field(default_factory=dict)


class CommodityStrategyAgent:
    scan_interval_seconds = DEFAULT_COMMODITY_SCAN_INTERVAL_SECONDS

    def __init__(self) -> None:
        saved_state = _load_saved_state()
        portfolio_state = saved_state["runtime"]["portfolio"]
        portfolio = PaperPortfolio(
            initial_capital=float(portfolio_state.get("initial_capital") or DEFAULT_COMMODITY_INITIAL_CAPITAL),
            session_id="commodity-strategy-paper",
        )
        self._runtime = CommodityRuntime(
            portfolio=portfolio,
            order_book=PaperOrderBook(on_fill=portfolio.on_fill),
        )
        saved_config = saved_state["config"]
        self._symbols: list[str] = list(saved_config["symbols"])
        self._selected_option_expiries: dict[str, str] = dict(saved_config["selected_option_expiries"])
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._enabled = True
        self._auto_run_enabled = True
        self._kill_switch_active = bool(saved_state["control"].get("kill_switch_active", False))
        self._start_required = bool(saved_state["control"].get("start_required", self._kill_switch_active))
        self._running = False
        self._last_run_at: Optional[str] = saved_state["control"].get("last_run_at")
        self._last_error: Optional[str] = saved_state["control"].get("last_error")
        self._last_message = (
            saved_state["control"].get("last_message")
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
        self._commentary: list[CommodityCommentaryEntry] = []
        self._restore_runtime_state(saved_state["runtime"])
        self._persist_state()

    def _restore_runtime_state(self, runtime_state: dict[str, Any]) -> None:
        self._runtime.watchlist = [
            row for row in list(runtime_state.get("watchlist") or []) if isinstance(row, dict)
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
            try:
                position = CommodityPositionState(
                    symbol=_canonicalize_symbol(row.get("symbol")),
                    action=str(row.get("action") or ""),
                    qty=int(row.get("qty") or 0),
                    entry_price=float(row.get("entry_price") or 0.0),
                    current_price=float(row.get("current_price") or 0.0),
                    stop_price=float(row.get("stop_price") or 0.0),
                    target_price=float(row.get("target_price") or 0.0),
                    regime=str(row.get("regime") or "unknown"),
                    signal_reason=str(row.get("signal_reason") or "signal"),
                    atr=float(row["atr"]) if row.get("atr") is not None else None,
                    entered_at=str(row.get("entered_at") or ""),
                    entry_bar_time=str(row.get("entry_bar_time") or ""),
                )
            except (TypeError, ValueError):
                continue
            if position.symbol:
                restored_positions[position.symbol] = position
        self._runtime.positions = restored_positions
        self._runtime.processed_signals = {
            _canonicalize_symbol(symbol): str(bar_time)
            for symbol, bar_time in dict(runtime_state.get("processed_signals") or {}).items()
            if str(symbol or "").strip() and str(bar_time or "").strip()
        }

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
        portfolio.available_capital = float(
            portfolio_payload.get("available_capital") or portfolio.initial_capital
        )
        portfolio._trade_history = _deserialize_trade_history(
            list(portfolio_payload.get("trade_history") or [])
        )
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
        portfolio._peak_equity = float(
            portfolio_payload.get("peak_equity") or portfolio.initial_capital
        )
        portfolio._positions = {
            position.symbol: VirtualPosition(
                symbol=position.symbol,
                action=position.action,
                qty=position.qty,
                avg_price=position.entry_price,
                current_price=position.current_price,
                instrument_type="FUT",
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
            },
            "control": {
                "kill_switch_active": self._kill_switch_active,
                "start_required": self._start_required,
                "last_run_at": self._last_run_at,
                "last_error": self._last_error,
                "last_message": self._last_message,
            },
            "runtime": {
                "watchlist": list(self._runtime.watchlist),
                "positions": [asdict(position) for position in self._runtime.positions.values()],
                "orders": list(self._runtime.orders),
                "reports": [asdict(report) for report in self._runtime.reports],
                "commentary": [asdict(entry) for entry in self._commentary],
                "processed_signals": dict(self._runtime.processed_signals),
                "portfolio": {
                    "initial_capital": float(portfolio.initial_capital),
                    "available_capital": float(portfolio.available_capital),
                    "trade_history": _serialize_trade_history(portfolio),
                    "daily_pnl": {
                        day.isoformat(): float(pnl)
                        for day, pnl in getattr(portfolio, "_daily_pnl", {}).items()
                    },
                    "equity_curve": [
                        {"time": timestamp.isoformat(), "equity": float(equity)}
                        for timestamp, equity in getattr(portfolio, "_equity_curve", [])
                    ],
                    "peak_equity": float(getattr(portfolio, "_peak_equity", portfolio.initial_capital)),
                },
            },
        }

    def _persist_state(self) -> None:
        _save_state(self._build_saved_state())

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
        self._enabled = False
        await self._stop_loop()
        self._persist_state()

    async def start_loop(self) -> dict[str, Any]:
        if self._kill_switch_active:
            self._last_message = "Commodity kill switch is active. Release it before starting the agent."
            self._append_commentary("warning", self._last_message)
            self._persist_state()
            return self.get_status()
        await self.start(force=True)
        if self._symbols:
            self._last_message = "Commodity agent started. Continuous scan loop is active."
        else:
            self._last_message = "Commodity agent started. Add MCX symbols to begin scanning."
        self._append_commentary("success", self._last_message)
        self._persist_state()
        return self.get_status()

    def update_symbols(
        self,
        symbols: list[str],
        *,
        selected_option_expiries: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        self._symbols = _normalize_symbols(symbols)
        base_selection = (
            selected_option_expiries
            if selected_option_expiries is not None
            else self._selected_option_expiries
        )
        self._selected_option_expiries = _normalize_selected_option_expiries(
            self._symbols,
            base_selection,
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
        }

    def get_symbols(self) -> list[str]:
        return list(self._symbols)

    def get_selected_option_expiries(self) -> dict[str, str]:
        return dict(self._selected_option_expiries)

    def update_selected_option_expiries(self, selected_option_expiries: dict[str, str]) -> dict[str, Any]:
        self._selected_option_expiries = _normalize_selected_option_expiries(
            self._symbols,
            selected_option_expiries,
        )
        if self._selected_option_expiries:
            self._append_commentary(
                "success",
                f"Saved {len(self._selected_option_expiries)} commodity option expiry selections.",
            )
        else:
            self._append_commentary("warning", "Commodity option expiry selections cleared.")
        self._persist_state()
        return {"selected_option_expiries": dict(self._selected_option_expiries)}

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
        if adapter and await ensure_fyers_session(force_validate=True):
            return adapter
        if await ensure_fyers_session(force_validate=True):
            return get_active_adapter("fyers")
        return None

    async def run_once(self, *, force: bool = False) -> dict[str, Any]:
        if self._lock.locked() and not force:
            return self.get_status()

        async with self._lock:
            self._running = True
            started_at = _now_ist()
            self._last_error = None
            try:
                if not self._symbols:
                    self._last_message = "Configure MCX symbols to start the commodity agent."
                    self._append_commentary("warning", self._last_message)
                    return self.get_status()

                if not force and not _in_commodity_hours(started_at):
                    self._last_message = "Waiting for MCX market hours."
                    self._append_commentary("idle", "Commodity market closed. Agent idle.")
                    return self.get_status()

                adapter = await self._get_fyers_adapter()
                if not adapter:
                    self._last_message = "Fyers is not connected. Commodity agent cannot scan."
                    self._append_commentary("error", self._last_message)
                    return self.get_status()

                quote_map = await self._safe_get_ltp(adapter, self._symbols)
                watch_rows: list[dict[str, Any]] = []
                for symbol in self._symbols:
                    row = await self._analyze_symbol(adapter, symbol, quote_map.get(symbol))
                    if row:
                        watch_rows.append(row)

                self._runtime.watchlist = watch_rows
                self._runtime.portfolio.update_prices(
                    {
                        row["symbol"]: float(row["price"])
                        for row in watch_rows
                        if row.get("price") is not None
                    }
                )

                await self._manage_positions(watch_rows)
                if self._kill_switch_active:
                    actionable = [row for row in watch_rows if row.get("signal") in {"BUY", "SELL"}]
                    if actionable:
                        self._append_commentary(
                            "warning",
                            f"Commodity kill switch active. {len(actionable)} signals observed, but no new entries were placed.",
                        )
                else:
                    await self._open_new_positions(watch_rows)

                self._last_run_at = started_at.isoformat()
                open_positions = len(self._runtime.positions)
                self._last_message = (
                    f"Scanned {len(watch_rows)} commodity symbols. {open_positions} open positions."
                )
                self._append_commentary("success", self._last_message)
                self._append_report()
                return self.get_status()
            except Exception as exc:
                self._last_error = str(exc)
                self._last_message = f"Commodity strategy error: {exc}"
                self._append_commentary("error", self._last_message)
                raise
            finally:
                self._running = False
                self._persist_state()

    async def _safe_get_ltp(self, adapter: BrokerAdapter, symbols: list[str]) -> dict[str, float]:
        try:
            return await adapter.get_ltp(symbols)
        except Exception as exc:
            logger.warning(f"[CommodityStrategy] LTP fetch failed: {exc}")
            self._append_commentary("warning", f"Live LTP fetch failed. Using candle closes where available. ({exc})")
            return {}

    async def _analyze_symbol(
        self,
        adapter: BrokerAdapter,
        symbol: str,
        live_ltp: Optional[float],
    ) -> Optional[dict[str, Any]]:
        from_date = (_now_ist() - timedelta(days=DEFAULT_COMMODITY_HISTORY_DAYS)).date()
        to_date = _now_ist().date()
        try:
            candles = await adapter.get_historical_candles(
                symbol,
                "30",
                from_date.isoformat(),
                to_date.isoformat(),
            )
        except Exception as exc:
            self._append_commentary("warning", f"{symbol}: unable to load Fyers history ({exc})")
            return None

        if not candles:
            self._append_commentary("warning", f"{symbol}: no historical candles returned by Fyers.")
            return None

        analysis = evaluate_commodity_signal(candles)
        latest_close = analysis.get("latest_close")
        previous_close = analysis.get("previous_close")
        price = float(live_ltp or latest_close or 0.0)
        change_pct = None
        if price and previous_close:
            change_pct = ((price - previous_close) / previous_close) * 100.0

        return {
            "symbol": symbol,
            "price": _round_or_none(price, 2),
            "previous_close": _round_or_none(previous_close, 2),
            "change_pct": _round_or_none(change_pct, 2),
            "signal": analysis.get("signal"),
            "reason": analysis.get("reason"),
            "regime": analysis.get("regime"),
            "ema_fast": analysis.get("ema_fast"),
            "ema_slow": analysis.get("ema_slow"),
            "atr": analysis.get("atr"),
            "breakout_high": analysis.get("breakout_high"),
            "breakout_low": analysis.get("breakout_low"),
            "bar_time": analysis.get("bar_time"),
        }

    async def _manage_positions(self, watch_rows: list[dict[str, Any]]) -> None:
        for row in watch_rows:
            symbol = str(row["symbol"])
            position = self._runtime.positions.get(symbol)
            if not position:
                continue

            current_price = float(row.get("price") or position.current_price)
            position.current_price = current_price
            signal = row.get("signal")
            reason: Optional[str] = None

            if position.action == "BUY":
                if current_price <= position.stop_price:
                    reason = "stop_loss"
                elif current_price >= position.target_price:
                    reason = "target_hit"
                elif signal == "SELL":
                    reason = "trend_reversal"
            else:
                if current_price >= position.stop_price:
                    reason = "stop_loss"
                elif current_price <= position.target_price:
                    reason = "target_hit"
                elif signal == "BUY":
                    reason = "trend_reversal"

            if not reason:
                continue

            exit_action = "SELL" if position.action == "BUY" else "BUY"
            order = self._runtime.order_book.place_order(
                symbol=symbol,
                action=exit_action,
                order_type="MARKET",
                qty=position.qty,
                instrument_type="FUT",
                session_id=self._runtime.portfolio.session_id,
                ltp=current_price,
            )
            self._record_order(order, reason)
            self._append_commentary(
                "trade",
                f"EXIT {symbol} {exit_action} @{current_price:.2f} ({reason})",
            )
            self._runtime.positions.pop(symbol, None)

    async def _open_new_positions(self, watch_rows: list[dict[str, Any]]) -> None:
        if len(self._runtime.positions) >= DEFAULT_COMMODITY_MAX_POSITIONS:
            return

        for row in watch_rows:
            if len(self._runtime.positions) >= DEFAULT_COMMODITY_MAX_POSITIONS:
                break

            symbol = str(row["symbol"])
            signal = str(row.get("signal") or "")
            bar_time = str(row.get("bar_time") or "")
            if signal not in {"BUY", "SELL"} or not bar_time:
                continue
            if symbol in self._runtime.positions:
                continue
            if self._runtime.processed_signals.get(symbol) == bar_time:
                continue

            price = float(row.get("price") or 0.0)
            atr = float(row.get("atr") or 0.0)
            if price <= 0 or atr <= 0:
                continue

            if signal == "BUY":
                stop_price = price - (atr * DEFAULT_COMMODITY_STOP_ATR)
                target_price = price + (atr * DEFAULT_COMMODITY_TARGET_ATR)
            else:
                stop_price = price + (atr * DEFAULT_COMMODITY_STOP_ATR)
                target_price = price - (atr * DEFAULT_COMMODITY_TARGET_ATR)

            order = self._runtime.order_book.place_order(
                symbol=symbol,
                action=signal,
                order_type="MARKET",
                qty=DEFAULT_COMMODITY_POSITION_QTY,
                instrument_type="FUT",
                session_id=self._runtime.portfolio.session_id,
                ltp=price,
            )
            self._record_order(order, str(row.get("reason") or "signal"))
            fill_price = float(order.fill_price or price)
            self._runtime.positions[symbol] = CommodityPositionState(
                symbol=symbol,
                action=signal,
                qty=DEFAULT_COMMODITY_POSITION_QTY,
                entry_price=fill_price,
                current_price=fill_price,
                stop_price=round(stop_price, 2),
                target_price=round(target_price, 2),
                regime=str(row.get("regime") or "unknown"),
                signal_reason=str(row.get("reason") or "signal"),
                atr=_round_or_none(atr, 4),
                entered_at=_now_ist().isoformat(),
                entry_bar_time=bar_time,
            )
            self._runtime.processed_signals[symbol] = bar_time
            self._append_commentary(
                "trade",
                f"ENTRY {symbol} {signal} @{fill_price:.2f} | stop {stop_price:.2f} | target {target_price:.2f}",
            )

    def _record_order(self, order: PaperOrder, reason: str) -> None:
        self._runtime.orders.insert(
            0,
            {
                "time": (order.fill_time or datetime.utcnow()).isoformat(),
                "order_id": order.order_id,
                "symbol": order.symbol,
                "action": order.action,
                "qty": order.qty,
                "order_type": order.order_type,
                "status": order.status,
                "fill_price": _round_or_none(order.fill_price, 2),
                "reason": reason,
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
        return {
            "market": "commodity",
            "auto_run_enabled": self._auto_run_enabled,
            "kill_switch_active": self._kill_switch_active,
            "loop_active": self._loop_active(),
            "start_required": self._start_required,
            "cancelled_orders": cancelled_orders,
        }

    def get_status(self) -> dict[str, Any]:
        summary = self._runtime.portfolio.get_summary()
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
                "timeframe": "30minute",
                "fast_ema": DEFAULT_COMMODITY_FAST_EMA,
                "slow_ema": DEFAULT_COMMODITY_SLOW_EMA,
                "atr_period": DEFAULT_COMMODITY_ATR_PERIOD,
                "breakout_lookback": DEFAULT_COMMODITY_BREAKOUT_LOOKBACK,
                "position_qty": DEFAULT_COMMODITY_POSITION_QTY,
            },
            "summary": {
                **summary,
                "open_positions": len(self._runtime.positions),
                "tracked_symbols": len(self._symbols),
                "open_orders": len(self._runtime.order_book.get_open_orders(self._runtime.portfolio.session_id)),
            },
            "watchlist": list(self._runtime.watchlist),
            "positions": [
                {
                    **asdict(position),
                    "unrealized_pnl": _round_or_none(position.unrealized_pnl, 2),
                    "return_pct": _round_or_none(position.return_pct, 2),
                }
                for position in self._runtime.positions.values()
            ],
            "orders": list(self._runtime.orders),
            "reports": [asdict(report) for report in self._runtime.reports],
            "commentary": [asdict(entry) for entry in self._commentary],
        }

    def get_orders(self) -> list[dict[str, Any]]:
        return list(self._runtime.orders)

    def get_positions(self) -> list[dict[str, Any]]:
        return self.get_status()["positions"]

    def get_reports(self) -> list[dict[str, Any]]:
        return [asdict(report) for report in self._runtime.reports]


commodity_strategy_agent = CommodityStrategyAgent()
