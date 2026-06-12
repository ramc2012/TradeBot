"""Deterministic paper-trading agent driven by STRATEGY_DOCUMENT.md.

Implements the full MACD zero-cross strategy on 30-minute ATM option
premium candles with:
- Physical-delivery trading window (prev_expiry−7 to current_expiry−7)
- CE/PE leg-specific MACD zero-cross entries
- Layered exit management (target +50%, runner, trail, hard stop)
- Spot MA context classification (breakout/trend/reversal)
- IV filtering and Kelly-based position sizing
- MACD death signal exit
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional

import httpx
import pandas as pd
from loguru import logger
from sqlalchemy import text

from analysis.indicators_agent import IndicatorContext, indicators_agent
from analysis.macd_engine import (
    compute_spot_ma_context,
    check_iv_filter,
)
from analysis.instruments import get_monthly_expiry
from analysis.signal_classifier import classify_signal_bucket, classify_status_bucket
from agent.macd_quadrant import QuadrantResult
from agent.strategy_config import (
    MACD_FAST,
    MACD_SLOW,
    MACD_SIGNAL,
    MACD_MIN_BARS,
    MIN_TTE_DAYS,
    MIN_PREMIUM,
    MAX_PREMIUM,
    MAX_ENTRY_IV_PCT,
    HARD_MAX_IV_PCT,
    MIN_CANDLE_BARS,
    KELLY_FRACTION,
    KELLY_PREMIUM_FRACTION,
    KELLY_CAUTIOUS_FRACTION,
    MAX_SIMULTANEOUS_POSITIONS,
    MAX_PER_UNDERLYING,
    CASH_RESERVE_PCT,
    EXIT,
    REGIME_BULLISH,
    REGIME_BEARISH,
    REGIME_DEAD,
    SETUP_BREAKOUT,
    SETUP_PREMIUM,
    SPOT_MA_FAST,
    SPOT_MA_SLOW,
    OPTION_ENTRY_MA_FAST,
    OPTION_ENTRY_MA_SLOW,
    OPTION_ENTRY_REQUIRE_ABOVE_MA20,
    FIRST_PULLBACK_IGNORE_BARS,
    EXCLUDED_UNDERLYINGS,
    COMMENTARY_MAX,
)
from agent.window_calculator import (
    get_all_active_windows,
    get_all_strategy1_scan_windows,
    days_remaining_in_window,
)
from analytics.orderflow import bar_cvd, cvd_agrees_with, orderflow_snapshot
from analytics.technicals import latest_macd_rsi
from core.config import settings
from core.trading_calendar import trading_calendar
from api.routers.auth import (
    ensure_fyers_session,
    get_active_adapter,
    get_broker_connection_snapshot,
)
from db.database import AsyncSessionLocal
from market_data import atm_watchlist_service, market_intelligence_runtime, market_profile_builder, option_history_service
from market_data.fo_universe_bootstrap import ensure_fo_underlying_catalog
from paper_engine import strategy_agent_state as strategy_state_module
from paper_engine.base_strategy_agent import (
    BaseStrategyAgent,
    IST,
    _ensure_ist_datetime,
    _latest_runtime_day,
    _latest_session_rows,
    _now_ist,
    _parse_iso_timestamp,
    _round_or_none,
    _serialize_equity_curve,
    _serialize_trade_history,
    _split_today_history,
    _deserialize_equity_curve,
    _deserialize_trade_history,
)
from paper_engine.order_book import PaperOrderBook
from paper_engine.portfolio import PaperPortfolio, VirtualPosition
from paper_engine.strategy_agent_entries import StrategyEntryMixin
from paper_engine.strategy_agent_exits import StrategyExitMixin
from paper_engine.strategy_learning import strategy_learning_service
from paper_engine.strategy_agent_state import (
    CommentaryEntry,
    StrategyEvent,
    StrategyPosition,
    StrategyRuntime,
    _NSE_STRATEGY_STATE_FILE,
    _load_saved_strategy_state,
    _load_saved_strategy_state_from_database,
    _save_strategy_state,
)


def _in_market_hours(now: Optional[datetime] = None) -> bool:
    return trading_calendar.is_exchange_open("NSE", now or _now_ist())

def _looks_like_stale_blocking_message(message: Optional[str]) -> bool:
    text = str(message or "").lower()
    return any(
        marker in text
        for marker in (
            "market-intelligence data is not ready",
            "broker_unavailable",
            "broker unavailable",
            "no valid nse broker session",
            "atm watchlist empty",
        )
    )

def _contract_symbol(underlying: str, expiry: str, strike: float, option_type: str) -> str:
    return f"OPT:{underlying}:{expiry}:{int(round(strike))}:{option_type}"


def _coerce_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _bars_since_entry(candles: list[dict[str, Any]], entered_at: Optional[str]) -> Optional[int]:
    entry_time = _parse_iso_timestamp(entered_at)
    if entry_time is None or not candles:
        return None

    bars = 0
    for candle in candles:
        candle_time = _parse_iso_timestamp(candle.get("time"))
        if candle_time is not None and candle_time >= entry_time:
            bars += 1
    return bars or None


def _report_interval_seconds(value: str) -> int:
    mapping = {"30m": 1800, "1h": 3600, "4h": 14400, "daily": 86400}
    return mapping.get(str(value or "1h"), 3600)


# S2 trades INDEX WEEKLIES on a 15-min MACD + Market-Profile gate.
# Universe is currently {NIFTY, SENSEX}:
#   - FINNIFTY + MIDCPNIFTY were dropped earlier to keep the per-scan
#     MP+OF cost within the box's single-core budget (running MP+OF for
#     all five pegged the CPU).
#   - BANKNIFTY was dropped on 2026-06-02 because NSE retired BANKNIFTY
#     weekly options in November 2024. Only the monthly contract exists,
#     but S2_CONTRACT_PROFILE asks for `index_expiry="weekly"` — so MI
#     consistently served only 2 weekly rows per cycle (NIFTY + SENSEX)
#     and silently dropped BANKNIFTY from the scan. The journal shows it
#     as `[Strategy2] MI served 2 weekly rows ... expiries=['2026-06-02',
#     '2026-06-30', '2026-06-04']` — the 2026-06-30 entry was MI
#     correctly resolving BANKNIFTY's next available expiry as the
#     monthly, which the weekly-only profile then filtered out.
#
# If BANKNIFTY coverage matters more than weekly precision, the right
# fix is a new profile (e.g. s2_banknifty_monthly) with its own MACD/MP
# tuning — not stretching the weekly-tuned strike + decay logic onto
# month-long contracts.
STRATEGY2_UNDERLYINGS = ("NIFTY", "SENSEX")
STRATEGY2_OPTION_TIMEFRAME = "15minute"
STRATEGY2_OPTION_BAR_MINUTES = 15
STRATEGY2_ENTRY_CUTOFF = time(15, 0)
STRATEGY2_FORCE_EXIT = time(15, 20)
# Anti-churn: ignore a market-profile direction flip if the position has been
# open less than this many signal bars. Friday's session had four entries
# all closed within 0-2 bars on `mp_gate_flip` for losses between -0% and -6%.
# Stop / target / macd_reversal exits still fire immediately.
STRATEGY2_MIN_HOLD_BARS = 3
STRATEGY2_MIN_HOLD_SECONDS = STRATEGY2_MIN_HOLD_BARS * STRATEGY2_OPTION_BAR_MINUTES * 60
STRATEGY2_SPOT_CACHE_TTL_SECONDS = 90
STRATEGY2_HARD_STOP_PCT = 18.0
STRATEGY2_TARGET_PCT = 35.0
STRATEGY2_KELLY_SCALE = 0.12
STRATEGY2_MAX_POSITIONS = 1000  # cap lifted 2026-06-09 (exploration); restore 4 for prod
STRATEGY2_SIGNAL_HISTORY = 8
STRATEGY2_FYERS_SYMBOLS = {
    "NIFTY": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
    "FINNIFTY": "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
    "SENSEX": "BSE:SENSEX-INDEX",
}


def _strategy2_expected_session_date(started_at: datetime) -> date:
    return started_at.date() - timedelta(days=1) if started_at.time() < time(9, 15) else started_at.date()


def _strategy2_is_regular_session(started_at: datetime) -> bool:
    return time(9, 15) <= started_at.time() <= time(15, 30)


def _indicator_cache_symbol(symbol: Optional[str], closes: list[float]) -> str:
    if symbol:
        return str(symbol)
    first_close = closes[0] if closes else 0.0
    last_close = closes[-1] if closes else 0.0
    return f"nse_option:{len(closes)}:{first_close:.6f}:{last_close:.6f}"


def _strategy_macd(
    closes: list[float],
    *,
    symbol: Optional[str] = None,
    timeframe: str = "5minute",
    last_bar_time: Optional[str] = None,
) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    ctx = IndicatorContext(
        symbol=_indicator_cache_symbol(symbol, closes),
        timeframe=timeframe,
        last_bar_time=last_bar_time,
    )
    result = indicators_agent.macd(ctx=ctx, closes=closes, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
    return result.macd, result.signal, result.histogram


def _data_quality_observation_block_reason(
    *,
    symbol: str,
    source: str,
    observed_at: Optional[str],
    now: Optional[datetime] = None,
) -> Optional[str]:
    if not settings.DATA_QUALITY_SCAN_GATE_ENABLED:
        return None
    symbol = str(symbol or "").strip()
    observed = _parse_iso_timestamp(observed_at)
    try:
        from market_data.data_quality_agent import data_quality_agent

        verdict = data_quality_agent.assess_observation(
            symbol=symbol,
            source=source,
            observed_at=observed,
            now=now or _now_ist(),
        )
    except Exception as exc:
        return f"Data quality gate could not verify {symbol or 'option snapshot'}: {exc}"
    if verdict.stale:
        return verdict.reason or f"Data quality gate blocked stale {source} for {symbol}."
    return None


def detect_macd_zero_cross(
    closes: list[float],
    option_type: str = "CE",
    *,
    symbol: Optional[str] = None,
    timeframe: str = "5minute",
    last_bar_time: Optional[str] = None,
) -> tuple[bool, Optional[float], Optional[str]]:
    """Detect MACD zero-line crossover on option premium closes.

    CE: MACD crosses from ≤0 to >0 (bullish)
    PE: MACD crosses from ≥0 to <0 (bearish — put premium rising)
    """
    if len(closes) < MACD_MIN_BARS:
        return False, None, None
    macd_line, _, _ = _strategy_macd(
        closes,
        symbol=symbol,
        timeframe=timeframe,
        last_bar_time=last_bar_time,
    )
    current = macd_line[-1]
    previous = macd_line[-2]
    if current is None or previous is None:
        return False, None, None

    if option_type == "CE":
        should_enter = previous <= 0 < current
    else:
        should_enter = previous >= 0 > current

    return should_enter, float(current), "macd_zero_cross" if should_enter else None


def _latest_populated_session_rows(
    rows: list[dict[str, Any]],
    *,
    min_rows: int = 30,
) -> tuple[list[dict[str, Any]], Optional[date]]:
    parsed_rows: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        parsed = _parse_iso_timestamp(row.get("time"))
        if parsed is not None:
            parsed_rows.append((parsed, row))
    if not parsed_rows:
        return [], None
    grouped: dict[date, list[tuple[datetime, dict[str, Any]]]] = {}
    for parsed, row in parsed_rows:
        grouped.setdefault(parsed.date(), []).append((parsed, row))
    latest_rows: list[dict[str, Any]] = []
    latest_date: Optional[date] = None
    for session_date in sorted(grouped, reverse=True):
        items = sorted(grouped[session_date], key=lambda item: item[0])
        session_rows = [row for _parsed, row in items]
        if latest_date is None:
            latest_date = session_date
            latest_rows = session_rows
        if len(session_rows) >= min_rows:
            return session_rows, session_date
    return latest_rows, latest_date


def detect_greeks_signal(
    candles: list[dict[str, Any]],
    option_type: str = "CE",
) -> tuple[bool, Optional[float], Optional[str]]:
    """Backward-compatible composite signal used by legacy tests.

    The runtime strategy is MACD-led now, but a lightweight Greeks sync helper
    keeps the historical test contract intact for supportive premium/Greeks
    breakouts.
    """
    if len(candles) < 12:
        return False, None, None

    series = candles[-12:]
    latest = series[-1]
    previous = series[-2]
    baseline = series[:-1]

    def _num(row: dict[str, Any], key: str, *, absolute: bool = False) -> Optional[float]:
        value = row.get(key)
        if value is None:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if absolute:
            numeric = abs(numeric)
        return numeric if numeric == numeric else None

    latest_close = _num(latest, "close")
    previous_close = _num(previous, "close")
    latest_iv = _num(latest, "iv")
    latest_delta = _num(latest, "delta", absolute=True)
    previous_delta = _num(previous, "delta", absolute=True)
    latest_gamma = _num(latest, "gamma", absolute=True)
    latest_vega = _num(latest, "vega", absolute=True)
    latest_underlying = _num(latest, "underlying_price")
    previous_underlying = _num(previous, "underlying_price")

    if None in {
        latest_close,
        previous_close,
        latest_iv,
        latest_delta,
        previous_delta,
        latest_gamma,
        latest_vega,
        latest_underlying,
        previous_underlying,
    }:
        return False, None, None

    baseline_highs = [_num(row, "high") for row in baseline]
    baseline_closes = [_num(row, "close") for row in baseline]
    baseline_ivs = [_num(row, "iv") for row in baseline]
    baseline_gammas = [_num(row, "gamma", absolute=True) for row in baseline]
    baseline_vegas = [_num(row, "vega", absolute=True) for row in baseline]

    if any(value is None for value in baseline_highs + baseline_closes + baseline_ivs):
        return False, None, None

    breakout_high = max(value for value in baseline_highs if value is not None)
    mean_iv = sum(value for value in baseline_ivs if value is not None) / max(len(baseline_ivs), 1)
    mean_gamma = sum(value for value in baseline_gammas if value is not None) / max(len(baseline_gammas), 1)
    mean_vega = sum(value for value in baseline_vegas if value is not None) / max(len(baseline_vegas), 1)

    score = 0.0

    if latest_close > breakout_high and latest_close > previous_close:
        score += 35.0
    elif latest_close > previous_close:
        score += 20.0

    if latest_iv >= mean_iv * 1.1:
        score += 15.0

    if latest_delta >= max(previous_delta + 0.05, 0.45):
        score += 20.0

    if latest_gamma >= max(mean_gamma * 2.0, 0.01):
        score += 15.0

    if latest_vega >= max(mean_vega * 1.5, 10.0):
        score += 10.0

    if option_type == "CE":
        if latest_underlying > previous_underlying:
            score += 5.0
    elif latest_underlying < previous_underlying:
        score += 5.0

    strength = round(score, 2)
    should_enter = strength >= 70.0
    return should_enter, strength, "greeks_sync_signal" if should_enter else None


# ── Position Phases ──────────────────────────────────────────────────────────

PHASE_1 = "phase1"           # full position, awaiting target +50%
PHASE_2 = "phase2"           # half exited at target, runner held
PHASE_TRAILING = "trailing"  # runner with active trail after +100%
PHASE_EXITED = "exited"


@dataclass(frozen=True)
class StrategyLaneDescriptor:
    key: str
    label: str
    timeframe: str
    instrument_scope: str
    execution_mode: str
    position_cap: int


class _BaseNSEStrategyLaneAgent:
    descriptor: StrategyLaneDescriptor

    def __init__(self, owner: "PaperStrategyAgent", runtime: StrategyRuntime) -> None:
        self.owner = owner
        self.runtime = runtime

    def mark_scan_started(self, started_at: datetime) -> None:
        self.runtime.last_scan_at = started_at.isoformat()

    def on_market_closed(self, started_at: datetime, last_live_scan: Optional[str]) -> None:
        raise NotImplementedError

    def on_broker_unavailable(self, started_at: datetime, broker_snapshot: dict[str, Any], message: str) -> None:
        self.runtime.last_message = message
        self.runtime.meta = {
            **(self.runtime.meta or {}),
            "mode": "broker_unavailable",
            "updated_at": started_at.isoformat(),
            "broker_snapshot": broker_snapshot,
        }

    def on_watchlist_unavailable(
        self,
        started_at: datetime,
        *,
        detail_messages: list[str],
        history_warning: Optional[str],
    ) -> None:
        raise NotImplementedError

    async def run_cycle(
        self,
        rows: list[dict[str, Any]],
        started_at: datetime,
        *,
        window_map: dict[str, dict[str, Any]],
        expiries: list[str],
    ) -> None:
        raise NotImplementedError

    def build_status_payload(self) -> dict[str, Any]:
        summary = self.runtime.last_run_summary if isinstance(self.runtime.last_run_summary, dict) else {}
        return {
            "key": self.descriptor.key,
            "label": self.descriptor.label,
            "timeframe": self.descriptor.timeframe,
            "instrument_scope": self.descriptor.instrument_scope,
            "execution_mode": self.descriptor.execution_mode,
            "position_cap": self.descriptor.position_cap,
            "last_scan_at": self.runtime.last_scan_at,
            "last_message": self.runtime.last_message,
            "open_positions": len(self.runtime.positions),
            "signals": len(self.runtime.signal_lane),
            "mode": self.runtime.meta.get("mode") if self.runtime.meta else None,
            "last_run_summary": {
                "counters": dict(summary.get("counters") or {}),
                "blocked_reasons": dict(summary.get("blocked_reasons") or {}),
                "last_observation_at": summary.get("last_observation_at"),
            },
        }


class _Strategy1LaneAgent(_BaseNSEStrategyLaneAgent):
    descriptor = StrategyLaneDescriptor(
        key="macd_strategy",
        label="Strategy 1 · 30m ATM MACD",
        timeframe="30minute",
        instrument_scope="NSE F&O ATM options",
        execution_mode="paper_execution",
        position_cap=MAX_SIMULTANEOUS_POSITIONS,
    )

    def on_market_closed(self, started_at: datetime, last_live_scan: Optional[str]) -> None:
        message = (
            f"Market closed. Showing last live strategy state from {last_live_scan}."
            if last_live_scan
            else "Waiting for NSE market hours."
        )
        if not self.runtime.last_message or self.runtime.last_message.startswith("Waiting for NSE market hours."):
            self.runtime.last_message = message
        self.runtime.meta = {
            **(self.runtime.meta or {}),
            "mode": "market_closed",
            "updated_at": started_at.isoformat(),
            "market_state": "closed",
        }

    def on_watchlist_unavailable(
        self,
        started_at: datetime,
        *,
        detail_messages: list[str],
        history_warning: Optional[str],
    ) -> None:
        message = "ATM watchlist empty for active windows."
        if detail_messages:
            message = f"{message} {' '.join(detail_messages[:2])}"
        if history_warning:
            message = f"{message} {history_warning}"
        self.runtime.last_message = message
        self.runtime.meta = {
            **(self.runtime.meta or {}),
            "mode": "no_watchlist",
            "updated_at": started_at.isoformat(),
        }

    async def run_cycle(
        self,
        rows: list[dict[str, Any]],
        started_at: datetime,
        *,
        window_map: dict[str, dict[str, Any]],
        expiries: list[str],
    ) -> None:
        # Reset per-cycle rejection counters so the status payload reflects
        # only the most recent scan, not cumulative since boot.
        self.runtime.last_run_summary = {
            "counters": {},
            "blocked_reasons": {},
            "last_observation_at": None,
            "scanned_rows": len(rows),
        }
        await self.owner._manage_exits(self.runtime, rows)
        if window_map:
            await self.owner._scan_entries(self.runtime, rows, window_map)
            self.runtime.last_message = (
                f"Scanned {len(rows)} instruments across {len(expiries)} expiries. "
                f"{len(self.runtime.positions)} open positions."
            )
        else:
            self.runtime.last_message = "No active Strategy 1 monthly trading windows."
            self.owner._append_commentary(
                "System",
                "No active prev_expiry−7 to current_expiry−7 windows found for Strategy 1.",
                tone="warning",
            )
        self.owner._refresh_prices_from_watchlist(self.runtime, rows)
        self.runtime.meta = {
            **(self.runtime.meta or {}),
            "mode": "live_scan" if window_map else "idle_no_window",
            "scan_interval": "30minute",
            "watchlist_rows": len(rows),
            "updated_at": started_at.isoformat(),
        }


class _Strategy2LaneAgent(_BaseNSEStrategyLaneAgent):
    descriptor = StrategyLaneDescriptor(
        key="index_mp_strategy",
        label="Strategy 2 · 15m Index MACD + MP",
        timeframe=STRATEGY2_OPTION_TIMEFRAME,
        instrument_scope="NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX ATM options",
        execution_mode="paper_execution",
        position_cap=STRATEGY2_MAX_POSITIONS,
    )

    def on_market_closed(self, started_at: datetime, last_live_scan: Optional[str]) -> None:
        self.runtime.last_message = self.runtime.last_message or "Market closed. Showing last Strategy 2 scan state."
        self.runtime.meta = {
            **(self.runtime.meta or {}),
            "mode": "market_closed",
            "scan_interval": STRATEGY2_OPTION_TIMEFRAME,
            "watchlist_rows": self.runtime.meta.get("watchlist_rows", len(self.runtime.signal_lane)) if self.runtime.meta else len(self.runtime.signal_lane),
            "updated_at": started_at.isoformat(),
            "market_state": "closed",
        }

    def on_broker_unavailable(self, started_at: datetime, broker_snapshot: dict[str, Any], message: str) -> None:
        super().on_broker_unavailable(started_at, broker_snapshot, message)
        self.runtime.meta = {
            **(self.runtime.meta or {}),
            "scan_interval": STRATEGY2_OPTION_TIMEFRAME,
            "watchlist_rows": 0,
        }

    def on_watchlist_unavailable(
        self,
        started_at: datetime,
        *,
        detail_messages: list[str],
        history_warning: Optional[str],
    ) -> None:
        self.runtime.last_message = "ATM watchlist empty for index scan."
        self.runtime.meta = {
            **(self.runtime.meta or {}),
            "mode": "no_watchlist",
            "scan_interval": STRATEGY2_OPTION_TIMEFRAME,
            "watchlist_rows": 0,
            "updated_at": started_at.isoformat(),
        }

    async def run_cycle(
        self,
        rows: list[dict[str, Any]],
        started_at: datetime,
        *,
        window_map: dict[str, dict[str, Any]],
        expiries: list[str],
    ) -> None:
        self.runtime.last_run_summary = {
            "counters": {},
            "blocked_reasons": {},
            "last_observation_at": None,
            "scanned_rows": len(rows),
        }
        await self.owner._run_strategy2(self.runtime, rows, started_at)


class PaperStrategyAgent(StrategyExitMixin, StrategyEntryMixin, BaseStrategyAgent):
    """Autonomous paper-trading agent implementing STRATEGY_DOCUMENT.md."""

    scan_interval_seconds = 60
    max_positions = MAX_SIMULTANEOUS_POSITIONS
    PHASE_1 = PHASE_1
    PHASE_2 = PHASE_2
    PHASE_TRAILING = PHASE_TRAILING
    PHASE_EXITED = PHASE_EXITED
    _contract_symbol = staticmethod(_contract_symbol)
    _bars_since_entry = staticmethod(_bars_since_entry)

    @staticmethod
    def _sync_state_file_override() -> None:
        strategy_state_module._NSE_STRATEGY_STATE_FILE = _NSE_STRATEGY_STATE_FILE

    def __init__(self) -> None:
        self._sync_state_file_override()
        self._strategy1 = self._build_runtime("macd_strategy", "Strategy 1 · 30m ATM MACD")
        # Strategy 2 (2026-06-02): DELETED per user instruction. The runtime
        # is still built and `self._strategy2` stays defined so persisted
        # state files (which include `index_mp_strategy` blocks from prior
        # sessions) deserialize without errors and an Auction-Intelligence
        # workflow can later read S2's last-known positions for audit. But
        # S2 is REMOVED from `_strategy_agents` so no scan, no run_cycle,
        # no positions, no signal lane surfaces — the lane is dark to the
        # UI and to every downstream consumer.
        self._strategy2 = self._build_runtime("index_mp_strategy", "Strategy 2 · 15m Index MACD + MP")
        self._strategy_agents: list[_BaseNSEStrategyLaneAgent] = [
            _Strategy1LaneAgent(self, self._strategy1),
        ]
        self._strategy = self._strategy1
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._enabled = True
        self._auto_run_enabled = True   # enabled by default; paper mode is safe
        self._kill_switch_active = False
        self._manual_restart_required = False
        self._running = False
        self._last_run_at: Optional[str] = None
        self._last_paper_reset_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._last_message: str = "Waiting for first strategy scan."
        self._last_expiry: Optional[str] = None
        self._last_candidate_expiries: list[str] = []
        self._telegram_last_sent_at: Optional[datetime] = None
        self._commentary: list[CommentaryEntry] = []
        self._active_windows: list[dict] = []
        self._scan_windows: list[dict] = []
        self._regime_cache: dict[str, QuadrantResult] = {}
        self._scan_count: int = 0
        self._last_spot_sync: Optional[datetime] = None
        self._strategy2_spot_cache: dict[str, tuple[datetime, list[dict[str, Any]], str]] = {}
        self._last_data_health: dict[str, Any] = {}
        self._historical_recovery_attempted = False
        self._state_synced_at: Optional[datetime] = None
        saved_state, saved_updated_at = _load_saved_strategy_state()
        self._restore_saved_state(saved_state)
        self._state_synced_at = saved_updated_at

    def _runtimes(self) -> list[StrategyRuntime]:
        return [self._strategy1, self._strategy2]

    def _lane_agents(self) -> list[_BaseNSEStrategyLaneAgent]:
        return list(self._strategy_agents)

    def get_runtime(self, key: str) -> Optional[StrategyRuntime]:
        for runtime in self._runtimes():
            if runtime.key == key:
                return runtime
        return None

    def _build_runtime(self, key: str, label: str) -> StrategyRuntime:
        session_id = f"{key}-paper"
        portfolio = PaperPortfolio(initial_capital=1_000_000.0, session_id=session_id)
        order_book = PaperOrderBook(on_fill=portfolio.on_fill)
        return StrategyRuntime(key=key, label=label, portfolio=portfolio, order_book=order_book)

    def _restore_saved_state(self, payload: dict[str, Any]) -> None:
        if not payload:
            return

        self._last_run_at = payload.get("last_run_at") or self._last_run_at
        self._last_paper_reset_at = payload.get("last_paper_reset_at") or self._last_paper_reset_at
        self._last_error = payload.get("last_error") or self._last_error
        self._last_message = payload.get("last_message") or self._last_message
        control_payload = payload.get("control") or {}
        if isinstance(control_payload, dict):
            self._auto_run_enabled = bool(control_payload.get("auto_run_enabled", self._auto_run_enabled))
            self._kill_switch_active = bool(control_payload.get("kill_switch_active", self._kill_switch_active))
            self._manual_restart_required = bool(
                control_payload.get(
                    "manual_restart_required",
                    self._kill_switch_active,
                )
            )
        self._last_expiry = payload.get("last_expiry") or self._last_expiry
        self._last_candidate_expiries = [
            str(item)
            for item in list(payload.get("last_candidate_expiries") or [])
            if str(item or "").strip()
        ]

        commentary: list[CommentaryEntry] = []
        for row in list(payload.get("commentary") or []):
            if not isinstance(row, dict):
                continue
            commentary.append(
                CommentaryEntry(
                    time=str(row.get("time") or ""),
                    scope=str(row.get("scope") or "System"),
                    tone=str(row.get("tone") or "info"),
                    message=str(row.get("message") or ""),
                )
            )
        self._commentary = commentary[:COMMENTARY_MAX]

        strategies = payload.get("strategies") or {}
        if isinstance(strategies, dict):
            for runtime in self._runtimes():
                runtime_payload = strategies.get(runtime.key)
                if isinstance(runtime_payload, dict):
                    self._restore_runtime_state(runtime, runtime_payload)

    def _restore_runtime_state(self, runtime: StrategyRuntime, payload: dict[str, Any]) -> None:
        runtime.entries = int(payload.get("entries") or 0)
        runtime.exits = int(payload.get("exits") or 0)
        runtime.last_scan_at = payload.get("last_scan_at")
        runtime.last_message = payload.get("last_message")
        runtime.processed_signals = {
            str(key): str(value)
            for key, value in dict(payload.get("processed_signals") or {}).items()
            if str(key or "").strip() and str(value or "").strip()
        }
        runtime.signal_lane = [
            row for row in list(payload.get("signal_lane") or []) if isinstance(row, dict)
        ]
        runtime.meta = dict(payload.get("meta") or {})

        runtime.recent_events = []
        for row in list(payload.get("recent_events") or []):
            if not isinstance(row, dict):
                continue
            try:
                runtime.recent_events.append(
                    StrategyEvent(
                        time=str(row.get("time") or ""),
                        event=str(row.get("event") or ""),
                        symbol=str(row.get("symbol") or ""),
                        underlying=str(row.get("underlying") or ""),
                        option_type=str(row.get("option_type") or ""),
                        strike=float(row.get("strike") or 0.0),
                        price=float(row.get("price") or 0.0),
                        qty=int(row.get("qty") or 0),
                        reason=str(row.get("reason") or ""),
                        signal_strength=_round_or_none(row.get("signal_strength"), 2),
                        pnl=_round_or_none(row.get("pnl"), 2),
                        phase=row.get("phase"),
                    )
                )
            except (TypeError, ValueError):
                continue
        runtime.recent_events = runtime.recent_events[:20]

        portfolio_payload = dict(payload.get("portfolio") or {})
        runtime.portfolio.initial_capital = float(portfolio_payload.get("initial_capital") or runtime.portfolio.initial_capital)
        runtime.portfolio.available_capital = float(portfolio_payload.get("available_capital") or runtime.portfolio.initial_capital)
        runtime.portfolio._peak_equity = float(portfolio_payload.get("peak_equity") or runtime.portfolio.initial_capital)
        runtime.portfolio._trade_history = _deserialize_trade_history(
            [row for row in list(portfolio_payload.get("trade_history") or []) if isinstance(row, dict)]
        )
        runtime.portfolio._daily_pnl.clear()
        for raw_day, pnl in dict(portfolio_payload.get("daily_pnl") or {}).items():
            try:
                runtime.portfolio._daily_pnl[date.fromisoformat(str(raw_day))] = float(pnl or 0.0)
            except (TypeError, ValueError):
                continue
        runtime.portfolio._equity_curve = _deserialize_equity_curve(
            [row for row in list(portfolio_payload.get("equity_curve") or []) if isinstance(row, dict)]
        )

        runtime.positions.clear()
        runtime.portfolio._positions.clear()
        for row in list(payload.get("positions") or []):
            if not isinstance(row, dict):
                continue
            try:
                position = StrategyPosition(
                    signal_id=row.get("signal_id"),
                    symbol=str(row.get("symbol") or ""),
                    underlying=str(row.get("underlying") or ""),
                    expiry=str(row.get("expiry") or ""),
                    strike=float(row.get("strike") or 0.0),
                    option_type=str(row.get("option_type") or ""),
                    instrument_key=row.get("instrument_key"),
                    trading_symbol=row.get("trading_symbol"),
                    qty=int(row.get("qty") or 0),
                    initial_qty=int(row.get("initial_qty") or row.get("qty") or 0),
                    entry_price=float(row.get("entry_price") or 0.0),
                    current_price=float(row.get("current_price") or 0.0),
                    peak_price=float(row.get("peak_price") or row.get("current_price") or 0.0),
                    entry_bar_time=str(row.get("entry_bar_time") or ""),
                    entered_at=str(row.get("entered_at") or ""),
                    signal_reason=str(row.get("signal_reason") or ""),
                    signal_strength=_round_or_none(row.get("signal_strength"), 2),
                    latest_rsi=_round_or_none(row.get("latest_rsi"), 2),
                    phase=str(row.get("phase") or PHASE_1),
                    trailing_stop=_round_or_none(row.get("trailing_stop"), 2),
                    entry_iv_pct=_round_or_none(row.get("entry_iv_pct"), 1),
                    spot_setup=row.get("spot_setup"),
                    regime=row.get("regime"),
                    option_ma20=_round_or_none(row.get("option_ma20"), 2),
                    option_ma50=_round_or_none(row.get("option_ma50"), 2),
                    above_option_ma20=bool(row.get("above_option_ma20")),
                    above_option_ma50=bool(row.get("above_option_ma50")),
                    first_pullback_ignored_at=row.get("first_pullback_ignored_at"),
                    window_end=row.get("window_end"),
                    lot_size=int(row.get("lot_size")) if row.get("lot_size") is not None else None,
                    price_updated_at=row.get("price_updated_at"),
                    macd_line=row.get("macd_line"),
                )
            except (TypeError, ValueError):
                continue
            runtime.positions[position.symbol] = position
            opened_at = _parse_iso_timestamp(position.entered_at) or datetime.utcnow().replace(tzinfo=IST)
            runtime.portfolio._positions[position.symbol] = VirtualPosition(
                symbol=position.symbol,
                action="BUY",
                qty=position.qty,
                avg_price=position.entry_price,
                current_price=position.current_price,
                instrument_type=position.option_type,
                expiry=position.expiry,
                strike=position.strike,
                option_type=position.option_type,
                signal_id=position.signal_id,
                setup_type=position.spot_setup,
                entry_iv_pct=position.entry_iv_pct,
                regime=position.regime,
                opened_at=opened_at,
            )

        if runtime.positions:
            runtime.portfolio.update_prices({symbol: pos.current_price for symbol, pos in runtime.positions.items()})

    def _serialize_runtime_state(self, runtime: StrategyRuntime) -> dict[str, Any]:
        return {
            "entries": runtime.entries,
            "exits": runtime.exits,
            "last_scan_at": runtime.last_scan_at,
            "last_message": runtime.last_message,
            "processed_signals": runtime.processed_signals,
            "signal_lane": runtime.signal_lane,
            "meta": runtime.meta,
            "recent_events": [asdict(event) for event in runtime.recent_events],
            "positions": [asdict(position) for position in runtime.positions.values()],
            "portfolio": {
                "initial_capital": runtime.portfolio.initial_capital,
                "available_capital": runtime.portfolio.available_capital,
                "peak_equity": getattr(runtime.portfolio, "_peak_equity", runtime.portfolio.initial_capital),
                "trade_history": _serialize_trade_history(runtime.portfolio),
                "daily_pnl": {str(day): pnl for day, pnl in getattr(runtime.portfolio, "_daily_pnl", {}).items()},
                "equity_curve": _serialize_equity_curve(runtime.portfolio),
            },
        }

    def _build_state_payload(self) -> dict[str, Any]:
        # Built on the event loop (pure dict/list comprehension over instance
        # state, ~microseconds). Keeping the build on-loop avoids a cross-thread
        # "list changed size during iteration" race on self._commentary /
        # self._runtimes() that would exist if the whole method were offloaded.
        self._sync_state_file_override()
        return {
            "last_run_at": self._last_run_at,
            "last_paper_reset_at": self._last_paper_reset_at,
            "last_error": self._last_error,
            "last_message": self._last_message,
            "last_expiry": self._last_expiry,
            "last_candidate_expiries": self._last_candidate_expiries,
            "control": {
                "auto_run_enabled": self._auto_run_enabled,
                "kill_switch_active": self._kill_switch_active,
                "manual_restart_required": self._manual_restart_required,
            },
            "commentary": [asdict(entry) for entry in self._commentary],
            "strategies": {
                runtime.key: self._serialize_runtime_state(runtime)
                for runtime in self._runtimes()
            },
        }

    def _persist_state(self) -> None:
        # Synchronous persist — retained for the few sync callers (e.g.
        # set_kill_switch). _save_strategy_state writes a JSON file + DB row and
        # blocks; acceptable on the rare operator paths.
        payload = self._build_state_payload()
        updated_at = _save_strategy_state(payload)
        if updated_at is not None:
            self._state_synced_at = updated_at

    async def _apersist_state(self) -> None:
        # Async persist for hot async paths (run_once's per-scan finally, ~60s):
        # build the payload on the loop, then offload only the blocking
        # json.dumps + file write + DB INSERT to a worker thread so a slow disk
        # / DB (observed spiking to ~2s) cannot freeze the event loop.
        payload = self._build_state_payload()
        updated_at = await asyncio.to_thread(_save_strategy_state, payload)
        if updated_at is not None:
            self._state_synced_at = updated_at

    def _refresh_state_from_store(self, *, force: bool = False) -> bool:
        payload, updated_at = _load_saved_strategy_state_from_database()
        if payload is None:
            return False
        if (
            not force
            and updated_at is not None
            and self._state_synced_at is not None
            and updated_at <= self._state_synced_at
        ):
            return False
        self._restore_saved_state(payload)
        if updated_at is not None:
            self._state_synced_at = updated_at
        return True

    def _select_candidate_expiries(self, as_of: date, expiries: list[str]) -> list[str]:
        """Legacy expiry chooser retained for compatibility with older tests."""
        parsed: list[date] = []
        for raw in expiries:
            try:
                expiry = date.fromisoformat(str(raw))
            except ValueError:
                continue
            if expiry >= as_of:
                parsed.append(expiry)

        parsed = sorted(set(parsed))
        if not parsed:
            return []

        selected = [parsed[0]]
        if len(parsed) > 1 and (parsed[0] - as_of).days <= 3:
            selected.append(parsed[1])
        return [item.isoformat() for item in selected]

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._refresh_state_from_store()
        self._enabled = True
        # Restore equity curve from Redis (survives backend restart)
        for runtime in self._runtimes():
            if getattr(runtime.portfolio, "_equity_curve", None):
                continue
            try:
                await runtime.portfolio.restore_from_redis()
            except Exception as exc:
                logger.debug(f"[Strategy] Equity curve restore skipped for {runtime.key}: {exc}")
        await self.ensure_recovered_state()
        self._persist_state()
        if (
            not self._auto_run_enabled
            or self._kill_switch_active
            or self._manual_restart_required
            or (self._task and not self._task.done())
        ):
            return
        self._task = asyncio.create_task(self._loop(), name="paper-strategy-agent")

    async def stop(self) -> None:
        self._refresh_state_from_store()
        self._enabled = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once(force=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                self._last_message = f"Paper strategy agent error: {exc}"
                logger.exception("[Strategy] loop failure")
            await asyncio.sleep(self.scan_interval_seconds)

    async def ensure_recovered_state(self) -> None:
        self._historical_recovery_attempted = True
        try:
            async with AsyncSessionLocal() as session:
                latest_snapshot_at = await session.scalar(
                    text(
                        """
                        SELECT MAX(timezone('Asia/Kolkata', time))
                        FROM atm_option_watchlist_snapshots
                        WHERE timezone('Asia/Kolkata', time)::date < :today
                           OR timezone('Asia/Kolkata', time)::time >= TIME '15:15'
                        """
                    ).bindparams(today=_now_ist().date())
                )
                latest_position_at = await session.scalar(
                    text(
                        """
                        SELECT MAX(timezone('Asia/Kolkata', created_at))
                        FROM positions
                        WHERE qty > 0
                          AND symbol LIKE 'OPT:%'
                        """
                    )
                )

            reset_at = _parse_iso_timestamp(self._last_paper_reset_at)
            latest_snapshot_day = self._historical_recovery_day_after_reset(latest_snapshot_at, reset_at)
            latest_position_day = self._historical_recovery_day_after_reset(latest_position_at, reset_at)
            if (
                reset_at is not None
                and latest_snapshot_day is None
                and latest_position_day is None
                and not self._last_run_at
            ):
                return
            current_strategy1_day = _latest_runtime_day(
                [position.entered_at for position in self._strategy1.positions.values()]
            )
            current_strategy2_day = _latest_runtime_day(
                [
                    signal.get("spot_session_date") or signal.get("signal_date") or signal.get("as_of")
                    for signal in self._strategy2.signal_lane
                    if isinstance(signal, dict)
                ]
            )
            current_prepared_day = _latest_runtime_day(
                [
                    (runtime.meta or {}).get("updated_at")
                    for runtime in self._runtimes()
                    if (runtime.meta or {}).get("mode") == "prepared_market_closed"
                ]
            )
            if current_prepared_day is not None:
                latest_required_day = max(
                    [day for day in (latest_snapshot_day, latest_position_day) if day is not None],
                    default=None,
                )
                prepared_snapshot_is_useful = (
                    self._strategy1_saved_snapshot_is_useful()
                    or self._strategy2_saved_snapshot_is_useful()
                )
                if prepared_snapshot_is_useful and (
                    latest_required_day is None or current_prepared_day >= latest_required_day
                ):
                    return
            needs_strategy1 = latest_position_day is not None and (
                not self._strategy1.positions
                or current_strategy1_day is None
                or current_strategy1_day < latest_position_day
            )
            needs_strategy2 = latest_snapshot_day is not None and (
                len(self._strategy2.signal_lane) < len(STRATEGY2_UNDERLYINGS)
                or current_strategy2_day is None
                or current_strategy2_day < latest_snapshot_day
            )
            needs_recovery = (not self._last_run_at) or needs_strategy1 or needs_strategy2
            if not needs_recovery:
                return
            await self._restore_from_historical_state(
                latest_snapshot_day=latest_snapshot_day,
                latest_position_day=latest_position_day,
            )
        except Exception as exc:
            logger.warning(f"[Strategy] Historical recovery skipped: {exc}")

    @staticmethod
    def _historical_recovery_day_after_reset(value: Any, reset_at: Optional[datetime]) -> Optional[date]:
        if value is None:
            return None
        if isinstance(value, datetime):
            timestamp = _ensure_ist_datetime(value)
            if reset_at is not None and timestamp is not None and timestamp <= reset_at:
                return None
            return timestamp.date() if timestamp is not None else None
        if isinstance(value, date):
            if reset_at is not None and value <= reset_at.astimezone(IST).date():
                return None
            return value
        timestamp = _parse_iso_timestamp(str(value))
        if timestamp is None:
            return None
        if reset_at is not None and timestamp <= reset_at:
            return None
        return timestamp.date()

    def _strategy1_saved_snapshot_is_useful(self) -> bool:
        meta = self._strategy1.meta or {}
        prepared_watchlist = meta.get("prepared_watchlist") or []
        try:
            watchlist_rows = int(meta.get("watchlist_rows") or 0)
        except (TypeError, ValueError):
            watchlist_rows = 0
        return bool(self._strategy1.positions or prepared_watchlist or watchlist_rows > 0)

    def _strategy2_saved_snapshot_is_useful(self) -> bool:
        meta = self._strategy2.meta or {}
        pipeline = meta.get("pipeline") or []
        if any(int((item or {}).get("rows") or 0) > 0 for item in pipeline if isinstance(item, dict)):
            return True
        signals = [row for row in self._strategy2.signal_lane if isinstance(row, dict)]
        return any(str(row.get("freshness") or "").lower() != "missing" for row in signals)

    async def _restore_from_historical_state(
        self,
        *,
        latest_snapshot_day: Optional[date] = None,
        latest_position_day: Optional[date] = None,
    ) -> None:
        strategy1_day = latest_position_day or latest_snapshot_day
        strategy2_day = latest_snapshot_day or latest_position_day
        if strategy1_day is None and strategy2_day is None:
            return

        recovered_positions = 0
        strategy1_signal_count = 0
        strategy1_last_seen: Optional[datetime] = None
        if strategy1_day is not None:
            recovered_positions, strategy1_signal_count, strategy1_last_seen = await self._restore_strategy1_positions_from_db(
                strategy1_day
            )

        strategy2_summary: dict[str, Any] = {}
        if strategy2_day is not None:
            strategy2_summary = await self._replay_strategy2_session(strategy2_day)

        timestamps = [
            ts
            for ts in (
                strategy1_last_seen,
                strategy2_summary.get("last_seen_at"),
            )
            if ts is not None
        ]
        reference_day = max(day for day in (strategy1_day, strategy2_day) if day is not None)
        latest_seen = max(timestamps) if timestamps else datetime.combine(reference_day, time(15, 20), tzinfo=IST)
        latest_seen_iso = latest_seen.astimezone(IST).isoformat()

        self._last_run_at = latest_seen_iso
        self._last_message = f"Recovered last session state from {reference_day.isoformat()}."

        commentary: list[str] = []
        if recovered_positions:
            commentary.append(
                f"Recovered {recovered_positions} open Strategy 1 position{'s' if recovered_positions != 1 else ''} from {strategy1_day.isoformat()}"
            )
            commentary.append(f"{strategy1_signal_count} Strategy 1 raw signals")
        entry_ready = int(strategy2_summary.get("entry_ready_count") or 0)
        trend_aligned = int(strategy2_summary.get("trend_aligned_count") or 0)
        if entry_ready or trend_aligned or self._strategy2.signal_lane:
            commentary.append(
                f"Strategy 2 replay found {entry_ready} entry-ready and {trend_aligned} trend-aligned lanes from {strategy2_day.isoformat()}"
            )
        if commentary:
            self._append_commentary(
                "System",
                f"{'; '.join(commentary)}.",
                tone="info",
            )

    @staticmethod
    def _prepared_side_score(row: dict[str, Any]) -> tuple[Optional[str], str, float]:
        ce = row.get("ce") or {}
        pe = row.get("pe") or {}

        def _as_float(value: Any) -> Optional[float]:
            try:
                if value is None:
                    return None
                return float(value)
            except (TypeError, ValueError):
                return None

        ce_macd = _as_float(ce.get("macd"))
        pe_macd = _as_float(pe.get("macd"))
        if ce_macd is not None and pe_macd is not None:
            if ce_macd >= 0 > pe_macd:
                return "CE", "bullish ATM option MACD quadrant", abs(ce_macd) + abs(pe_macd)
            if ce_macd < 0 <= pe_macd:
                return "PE", "bearish ATM option MACD quadrant", abs(ce_macd) + abs(pe_macd)

        ce_change = _as_float(ce.get("change_pct"))
        pe_change = _as_float(pe.get("change_pct"))
        if ce_change is not None and pe_change is not None:
            gap = ce_change - pe_change
            if gap > 0.5:
                return "CE", "CE premium strength versus PE", abs(gap)
            if gap < -0.5:
                return "PE", "PE premium strength versus CE", abs(gap)

        return None, "no directional option-premium edge yet", 0.0

    def _build_prepared_strategy1_watchlist(
        self,
        rows: list[dict[str, Any]],
        *,
        started_at: datetime,
        limit: int = 80,
    ) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        for row in rows:
            underlying = str(row.get("underlying") or "").strip()
            if not underlying or underlying in EXCLUDED_UNDERLYINGS:
                continue
            direction, reason, score = self._prepared_side_score(row)
            if direction not in {"CE", "PE"}:
                continue
            side = row.get("ce") if direction == "CE" else row.get("pe")
            side = side or {}
            try:
                ltp = float(side.get("ltp") or 0.0)
            except (TypeError, ValueError):
                ltp = 0.0
            if ltp <= 0:
                continue
            iv_raw = side.get("iv")
            iv_pct = None
            if iv_raw is not None:
                try:
                    iv_val = float(iv_raw)
                    iv_pct = iv_val * 100.0 if iv_val < 1.0 else iv_val
                except (TypeError, ValueError):
                    iv_pct = None
            # Pre-market preparation: no hard IV reject and no premium
            # band — ATM-only universe is liquid by construction. We
            # apply the IV sanity scaler so broker reads above 90% IV
            # are dropped, and nudge the watchlist score down for
            # high-IV setups so they fall in priority without being
            # rejected.
            from agent.iv_size_policy import iv_size_scaler as _iv_size_scaler
            _iv_scaler, _iv_note = _iv_size_scaler(iv_pct, None)
            if _iv_scaler <= 0:
                continue
            if _iv_scaler < 0.6:
                score -= 25.0  # heavy IV → push down the watchlist rank

            prepared.append(
                {
                    "strategy": "Strategy 1",
                    "source": "prepared_watchlist",
                    "underlying": underlying,
                    "signal_date": started_at.date().isoformat(),
                    "trade_date": "pre-market preparation",
                    "as_of": started_at.isoformat(),
                    "direction": direction,
                    "reason": reason,
                    "strength": "monitoring",
                    "status": "watching",
                    "freshness": "prepared",
                    "instruction": (
                        f"{underlying}: {direction} is favoured by {reason}; "
                        "wait for the live MACD zero-cross and risk gates after market open."
                    ),
                    "spot_price": _round_or_none(row.get("spot_price"), 2),
                    "expiry": row.get("expiry"),
                    "atm_strike": row.get("atm_strike"),
                    "ltp": _round_or_none(ltp, 2),
                    "iv_pct": _round_or_none(iv_pct, 2),
                    "priority_score": _round_or_none(score, 4),
                    "option_last_bar_time": started_at.isoformat(),
                    "spot_last_time": started_at.isoformat(),
                    **classify_status_bucket(
                        has_position=self._has_underlying_position(self._strategy1, underlying),
                        status="watching",
                    ),
                }
            )

        prepared.sort(key=lambda item: float(item.get("priority_score") or 0.0), reverse=True)
        return prepared[:limit]

    async def _prepare_closed_market_state(
        self,
        started_at: datetime,
        *,
        market_intelligence_health: dict[str, Any],
        broker_snapshot: Optional[dict[str, Any]],
        local_only_mode: bool,
    ) -> None:
        rows: list[dict[str, Any]] = []
        watchlist_detail: Optional[str] = None
        broker_ready = bool((broker_snapshot or {}).get("broker_ready"))
        live_refresh = bool(broker_ready and not local_only_mode)

        try:
            expiry_scope = await atm_watchlist_service.get_expiries(
                self._last_expiry,
                live_refresh=live_refresh,
            )
            self._last_expiry = str(expiry_scope.get("default_expiry") or self._last_expiry or "")
            self._last_candidate_expiries = [
                str(item)
                for item in (expiry_scope.get("expiries") or [])
                if str(item or "").strip()
            ]
            watchlist = await atm_watchlist_service.get_watchlist(
                expiry=self._last_expiry or None,
                live_refresh=live_refresh,
            )
            rows = list(watchlist.get("rows") or [])
            strategy2_native_rows = await self._load_strategy2_native_watchlist_rows(
                expiry_scope,
                live_refresh=live_refresh,
            )
            if strategy2_native_rows:
                rows_by_underlying = {
                    str(row.get("underlying") or "").strip(): row
                    for row in rows
                    if str(row.get("underlying") or "").strip()
                }
                rows_by_underlying.update(strategy2_native_rows)
                rows = list(rows_by_underlying.values())
            watchlist_detail = str(watchlist.get("detail") or "").strip() or None
        except Exception as exc:
            watchlist_detail = f"Closed-market watchlist preparation failed: {exc}"
            logger.warning(f"[Strategy] closed-market preparation skipped: {exc}")

        try:
            self._active_windows = await get_all_active_windows(as_of=started_at.date())
            self._scan_windows = await get_all_strategy1_scan_windows(as_of=started_at.date())
        except Exception as exc:
            logger.debug(f"[Strategy] closed-market window preparation skipped: {exc}")

        if not rows:
            await self.ensure_recovered_state()
            last_live_scan = self._last_run_at or self._strategy2.last_scan_at or self._strategy1.last_scan_at
            self._last_message = (
                f"Market closed. Showing saved NSE strategy state from {last_live_scan}; "
                "closed-market watchlist returned 0 rows."
            )
            for lane in self._lane_agents():
                lane.on_market_closed(started_at, last_live_scan)
                if _looks_like_stale_blocking_message(lane.runtime.last_message):
                    lane.runtime.last_message = self._last_message
            self._append_commentary("System", self._last_message, tone="idle")
            return

        all_underlyings = [
            str(row.get("underlying") or "").strip()
            for row in rows
            if str(row.get("underlying") or "").strip()
        ]
        stock_underlyings = [
            str(row.get("underlying") or "").strip()
            for row in rows
            if str(row.get("kind") or "").upper() == "STOCK"
        ]
        index_underlyings = [
            str(row.get("underlying") or "").strip()
            for row in rows
            if str(row.get("kind") or "").upper() == "INDEX"
        ]
        prepared_watchlist = self._build_prepared_strategy1_watchlist(rows, started_at=started_at)
        latest_watchlist_time = (
            market_intelligence_health.get("latest_watchlist_time")
            or market_intelligence_health.get("latest_watchlist_session")
        )

        s1_message = (
            f"Prepared for next NSE session: {len(rows)} ATM rows "
            f"({len(stock_underlyings)} stocks, {len(index_underlyings)} indices), "
            f"{len(prepared_watchlist)} priority watch candidates. "
            "No entries are opened while market is closed."
        )
        if watchlist_detail:
            s1_message = f"{s1_message} {watchlist_detail}"

        self._last_run_at = started_at.isoformat()
        self._strategy1.last_scan_at = started_at.isoformat()
        self._strategy1.last_message = s1_message
        self._strategy1.meta = {
            **(self._strategy1.meta or {}),
            "mode": "prepared_market_closed",
            "market_state": "closed",
            "updated_at": started_at.isoformat(),
            "watchlist_rows": len(rows),
            "stock_underlyings": len(stock_underlyings),
            "index_underlyings": len(index_underlyings),
            "latest_watchlist_time": latest_watchlist_time,
            "prepared_watchlist": prepared_watchlist,
            "instrument_universe": all_underlyings,
            "active_windows": len(self._active_windows),
            "scan_windows": len(self._scan_windows),
        }
        for item in prepared_watchlist:
            await self._persist_agent_signal_observation(
                self._strategy1,
                item,
                status="watching",
            )

        index_rows = [row for row in rows if str(row.get("underlying") or "") in STRATEGY2_UNDERLYINGS]
        strategy2_contexts: dict[str, dict[str, Any]] = {}
        for row in index_rows:
            underlying = str(row.get("underlying") or "")
            try:
                context = await self._build_strategy2_signal_context(row, started_at)
            except Exception as exc:
                logger.warning(f"[Strategy2] closed-market context preparation failed for {underlying}: {exc}")
                context = self._build_strategy2_preparation_failure_context(row, started_at, exc)
            signal = self._normalize_strategy2_prepared_signal(
                dict(context.get("signal") or {}),
                row=row,
                started_at=started_at,
            )
            context["signal"] = signal
            context["can_enter"] = False
            strategy2_contexts[underlying] = context
        for underlying in STRATEGY2_UNDERLYINGS:
            if underlying in strategy2_contexts:
                continue
            row = {"underlying": underlying}
            context = self._build_strategy2_preparation_failure_context(
                row,
                started_at,
                RuntimeError("ATM watchlist row missing for configured Strategy 2 underlying"),
            )
            context["signal"] = self._normalize_strategy2_prepared_signal(
                dict(context.get("signal") or {}),
                row=row,
                started_at=started_at,
            )
            strategy2_contexts[underlying] = context

        strategy2_signals = [
            strategy2_contexts[underlying]["signal"]
            for underlying in STRATEGY2_UNDERLYINGS
            if underlying in strategy2_contexts
        ]
        strategy2_pipeline = [
            strategy2_contexts[underlying]["pipeline"]
            for underlying in STRATEGY2_UNDERLYINGS
            if underlying in strategy2_contexts and strategy2_contexts[underlying].get("pipeline")
        ]
        trend_aligned_count = sum(1 for item in strategy2_signals if item.get("status") == "trend-aligned")
        waiting_count = sum(1 for item in strategy2_signals if item.get("status") in {"waiting-cross", "waiting"})
        stale_count = sum(1 for item in strategy2_signals if item.get("freshness") == "stale")
        missing_count = sum(1 for item in strategy2_signals if item.get("freshness") == "missing")
        s2_message = (
            f"Prepared for next index session: {len(index_rows)} index lanes, "
            f"{trend_aligned_count} trend-aligned and {waiting_count} waiting. "
            "No Strategy 2 entries are opened while market is closed; first live 15-minute bar must re-confirm."
        )
        self._strategy2.last_scan_at = started_at.isoformat()
        self._strategy2.last_message = s2_message
        self._strategy2.signal_lane = strategy2_signals
        self._strategy2.meta = {
            **(self._strategy2.meta or {}),
            "mode": "prepared_market_closed",
            "market_state": "closed",
            "scan_interval": STRATEGY2_OPTION_TIMEFRAME,
            "watchlist_rows": len(index_rows),
            "updated_at": started_at.isoformat(),
            "instrument_universe": [symbol for symbol in STRATEGY2_UNDERLYINGS if symbol in set(index_underlyings or STRATEGY2_UNDERLYINGS)],
            "pipeline": strategy2_pipeline,
            "entry_ready_count": 0,
            "trend_aligned_count": trend_aligned_count,
            "waiting_count": waiting_count,
            "stale_count": stale_count,
            "missing_count": missing_count,
            "prepared_note": "Closed-market preparation never creates actionable Strategy 2 entries.",
        }
        row_map = {str(row.get("underlying") or ""): row for row in index_rows}
        for signal in strategy2_signals:
            await self._persist_agent_signal_observation(
                self._strategy2,
                signal,
                status="watching",
                row=row_map.get(str(signal.get("underlying") or "")),
            )

        await self._ensure_open_position_order_records(self._strategy1)
        await self._ensure_open_position_order_records(self._strategy2)

        self._last_message = (
            f"Market closed. Strategy preparation is ready: {len(rows)} ATM rows, "
            f"Fyers={'ready' if (broker_snapshot or {}).get('fyers_ready') else 'not ready'}, "
            f"Upstox={'ready' if (broker_snapshot or {}).get('upstox_ready') else 'not ready'}."
        )
        self._append_commentary("System", self._last_message, tone="info")

    async def _load_strategy2_native_watchlist_rows(
        self,
        expiry_scope: dict[str, Any],
        *,
        live_refresh: bool,
    ) -> dict[str, dict[str, Any]]:
        """Resolve the S2 watchlist on the per-underlying expiry-track matrix.

        NIFTY and SENSEX trade BOTH weekly and monthly ATM contracts;
        BANKNIFTY/FINNIFTY/MIDCPNIFTY trade monthly only. The routing
        comes from :data:`strategy2_mp_of.S2_EXPIRY_ROUTING`.

        Returned dict is keyed by ``f"{underlying}:{track}"`` so the
        caller can tell the rows apart even though they share an
        underlying. Each row is tagged with ``row["expiry_track"]``
        (``"weekly"`` or ``"monthly"``) so the downstream entry loop
        can size and book each leg independently.
        """
        from paper_engine.strategy2_mp_of import (
            load_s2_expiry_inputs,
            resolve_s2_expiry_targets,
            select_s2_expiry_targets,
        )

        today_iso = _now_ist().date().isoformat()

        # Build the (underlying, track, expiry) request matrix from the expiry
        # calendar catalog (fo_expiry_catalog monthlies + the underlying's own
        # listed expiries for weeklies), so SENSEX trades its BSE weeklies
        # rather than NIFTY's NSE ladder. Falls back to the legacy scope-based
        # resolver when the catalog has no rows for an underlying.
        requests: list[tuple[str, str, str]] = []
        for underlying in STRATEGY2_UNDERLYINGS:
            targets: list[tuple[str, str]] = []
            try:
                inputs = await load_s2_expiry_inputs(underlying)
                if inputs.get("monthlies"):
                    targets = select_s2_expiry_targets(
                        underlying,
                        monthlies=inputs.get("monthlies") or [],
                        listed_expiries=inputs.get("listed_expiries") or [],
                        today_iso=today_iso,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[Strategy2] catalog expiry resolve failed for {underlying}: {exc}")
            if not targets:
                targets = resolve_s2_expiry_targets(underlying, expiry_scope)
            for track, expiry in targets:
                if expiry:
                    requests.append((underlying, track, expiry))
        if not requests:
            return {}

        results = await asyncio.gather(
            *(
                atm_watchlist_service.get_watchlist(
                    expiry=expiry,
                    symbols=[underlying],
                    live_refresh=live_refresh,
                )
                for underlying, _track, expiry in requests
            ),
            return_exceptions=True,
        )

        rows_by_key: dict[str, dict[str, Any]] = {}
        for (underlying, track, _expiry), result in zip(requests, results):
            if not isinstance(result, dict):
                continue
            for row in result.get("rows") or []:
                row_underlying = str(row.get("underlying") or "").strip()
                if row_underlying != underlying:
                    continue
                # Don't mutate the watchlist service's cached dict.
                tagged = dict(row)
                tagged["expiry_track"] = track
                rows_by_key[f"{underlying}:{track}"] = tagged
                # First matching row per request is the ATM line we want;
                # if the watchlist returned multiple, we take just one.
                break
        return rows_by_key

    @staticmethod
    def _broker_failure_message(snapshot: dict[str, Any]) -> str:
        upstox_status = str((snapshot.get("upstox_token_health") or {}).get("status") or "disconnected")
        fyers_status = str((snapshot.get("fyers_token_health") or {}).get("status") or "disconnected")
        return (
            "No valid NSE broker session is available for the paper scan. "
            f"Upstox={upstox_status.replace('_', ' ')}, "
            f"Fyers={fyers_status.replace('_', ' ')}."
        )

    @staticmethod
    def _local_data_failure_message(health: dict[str, Any]) -> str:
        latest_watchlist_time = str(health.get("latest_watchlist_time") or "none")
        watchlist_rows = int(health.get("watchlist_rows_today") or 0)
        latest_rows = int(health.get("watchlist_rows_latest") or watchlist_rows)
        readiness_mode = str(health.get("readiness_mode") or "missing")
        execution_mode = str(health.get("execution_mode") or readiness_mode)
        age = health.get("watchlist_age_seconds")
        max_age = health.get("max_execution_age_seconds")
        age_detail = ""
        if age is not None:
            age_detail = f", age={int(float(age))}s"
            if max_age is not None:
                age_detail += f", max execution age={int(float(max_age))}s"
        if health.get("ready") and not health.get("execution_ready", health.get("ready")):
            return (
                "Shared market-intelligence data is stale for the paper scan. "
                f"Today rows={watchlist_rows}, latest-session rows={latest_rows}, "
                f"mode={readiness_mode}, execution mode={execution_mode}, "
                f"latest watchlist time={latest_watchlist_time}{age_detail}."
            )
        return (
            "Shared market-intelligence data is not ready for the paper scan. "
            f"Today rows={watchlist_rows}, latest-session rows={latest_rows}, "
            f"mode={readiness_mode}, execution mode={execution_mode}, "
            f"latest watchlist time={latest_watchlist_time}{age_detail}."
        )

    @staticmethod
    def _market_intelligence_execution_ready(health: dict[str, Any]) -> bool:
        return bool(health.get("execution_ready", health.get("ready")))

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
        return "Option history warnings detected: " + "; ".join(broker_parts) + "."

    async def _restore_strategy1_positions_from_db(self, trading_day: date) -> tuple[int, int, Optional[datetime]]:
        runtime = self._strategy1
        runtime.positions.clear()
        runtime.recent_events.clear()
        runtime.portfolio._positions.clear()
        runtime.portfolio._trade_history.clear()

        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT DISTINCT ON (symbol)
                               created_at,
                               symbol,
                               qty,
                               avg_price
                        FROM positions
                        WHERE qty > 0
                          AND symbol LIKE 'OPT:%'
                          AND created_at::date = :day
                        ORDER BY symbol, created_at ASC
                        """
                    ),
                    {"day": trading_day},
                )
            ).mappings().all()

        latest_seen: Optional[datetime] = None
        recovered = 0
        for row in rows:
            symbol = str(row.get("symbol") or "")
            parts = symbol.split(":")
            if len(parts) != 5 or parts[0] != "OPT":
                continue
            _, underlying, expiry, strike_raw, option_type = parts
            try:
                expiry_date = date.fromisoformat(expiry)
            except ValueError:
                continue
            created_at = row.get("created_at")
            if isinstance(created_at, datetime):
                created_at = created_at.astimezone(IST)
                latest_seen = max(latest_seen, created_at) if latest_seen else created_at
            try:
                strike = float(strike_raw)
                qty = int(row.get("qty") or 0)
                entry_price = float(row.get("avg_price") or 0.0)
            except (TypeError, ValueError):
                continue
            if qty <= 0 or entry_price <= 0:
                continue

            current_price = entry_price
            async with AsyncSessionLocal() as session:
                latest_ltp = await session.scalar(
                    text(
                        """
                        SELECT ltp::float8
                        FROM atm_option_watchlist_snapshots
                        WHERE time::date = :day
                          AND underlying = :underlying
                          AND expiry = :expiry
                          AND strike = :strike
                          AND option_type = :option_type
                        ORDER BY time DESC
                        LIMIT 1
                        """
                    ),
                    {
                        "day": trading_day,
                        "underlying": underlying,
                        "expiry": expiry_date,
                        "strike": strike,
                        "option_type": option_type,
                    },
                )
            if latest_ltp is not None:
                current_price = float(latest_ltp)

            entered_at = created_at.isoformat() if isinstance(created_at, datetime) else datetime.combine(
                trading_day, time(15, 20), tzinfo=IST
            ).isoformat()
            position = StrategyPosition(
                signal_id=str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{runtime.key}:{symbol}:{entered_at}")),
                symbol=symbol,
                underlying=underlying,
                expiry=expiry,
                strike=strike,
                option_type=option_type,
                instrument_key=None,
                trading_symbol=None,
                qty=qty,
                initial_qty=qty,
                entry_price=entry_price,
                current_price=current_price,
                peak_price=max(entry_price, current_price),
                entry_bar_time=entered_at,
                entered_at=entered_at,
                signal_reason="macd_zero_cross",
                phase=PHASE_1,
            )
            runtime.positions[symbol] = position
            runtime.portfolio._positions[symbol] = VirtualPosition(
                symbol=symbol,
                action="BUY",
                qty=qty,
                avg_price=entry_price,
                current_price=current_price,
                instrument_type=option_type,
                expiry=expiry,
                strike=strike,
                option_type=option_type,
                opened_at=_parse_iso_timestamp(entered_at) or datetime.combine(trading_day, time(15, 20), tzinfo=IST),
            )
            runtime.recent_events.append(
                StrategyEvent(
                    time=entered_at,
                    event="entry",
                    symbol=symbol,
                    underlying=underlying,
                    option_type=option_type,
                    strike=strike,
                    price=entry_price,
                    qty=qty,
                    reason="recovered_position",
                    phase=PHASE_1,
                )
            )
            recovered += 1

        if runtime.positions:
            runtime.portfolio.update_prices({symbol: pos.current_price for symbol, pos in runtime.positions.items()})
            runtime.entries = len(runtime.positions)
            runtime.last_scan_at = latest_seen.isoformat() if latest_seen else datetime.combine(
                trading_day, time(15, 20), tzinfo=IST
            ).isoformat()
            runtime.last_message = f"Recovered {len(runtime.positions)} open Strategy 1 positions from {trading_day.isoformat()}."

        signal_count = await self._count_strategy1_historical_signals(trading_day)
        runtime.meta = {
            **(runtime.meta or {}),
            "mode": "historical_recovery",
            "recovered_trading_day": trading_day.isoformat(),
            "historical_signal_count": signal_count,
        }
        return recovered, signal_count, latest_seen

    async def _count_strategy1_historical_signals(self, trading_day: date) -> int:
        windows = {w["underlying"]: w for w in await get_all_active_windows(as_of=trading_day)}
        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT timezone('Asia/Kolkata', time) AS local_time,
                               underlying,
                               expiry::text AS expiry,
                               strike::float8 AS strike,
                               option_type,
                               ltp::float8 AS ltp,
                               iv::float8 AS iv,
                               macd::float8 AS macd
                        FROM atm_option_watchlist_snapshots
                        WHERE time::date = :day
                        ORDER BY time ASC
                        """
                    ),
                    {"day": trading_day},
                )
            ).mappings().all()

        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        previous_macd: dict[tuple[str, str], float] = {}
        signal_keys: set[tuple[str, str, str]] = set()

        for row in rows:
            underlying = str(row.get("underlying") or "")
            if underlying in EXCLUDED_UNDERLYINGS:
                continue
            local_time = _ensure_ist_datetime(row.get("local_time"))
            if local_time is None:
                continue
            bucket = local_time.replace(second=0, microsecond=0)
            bucket_key = (underlying, bucket.isoformat())
            paired = grouped.setdefault(bucket_key, {"bucket": bucket, "underlying": underlying, "expiry": row.get("expiry")})
            paired[str(row.get("option_type") or "").lower()] = row

        for paired in sorted(grouped.values(), key=lambda item: (item["bucket"], item["underlying"])):
            underlying = paired["underlying"]
            window = windows.get(underlying)
            ce = paired.get("ce")
            pe = paired.get("pe")
            if not window or not ce or not pe:
                continue
            try:
                expiry = date.fromisoformat(str(paired.get("expiry") or ""))
            except ValueError:
                continue
            if days_remaining_in_window(window, as_of=trading_day) < MIN_TTE_DAYS:
                continue
            if (expiry - trading_day).days < 3:
                continue
            ce_macd = ce.get("macd")
            pe_macd = pe.get("macd")
            if ce_macd is None or pe_macd is None:
                continue
            regime_name = REGIME_DEAD if ce_macd is None or pe_macd is None else (
                REGIME_BULLISH if ce_macd >= 0 > pe_macd else REGIME_BEARISH if ce_macd < 0 <= pe_macd else REGIME_DEAD
            )
            prev_ce = previous_macd.get((underlying, "CE"))
            prev_pe = previous_macd.get((underlying, "PE"))
            ce_cross = prev_ce is not None and prev_ce <= 0 < ce_macd
            pe_cross = prev_pe is not None and prev_pe >= 0 > pe_macd
            previous_macd[(underlying, "CE")] = float(ce_macd)
            previous_macd[(underlying, "PE")] = float(pe_macd)

            side = None
            option_type = None
            if regime_name == REGIME_BULLISH and ce_cross:
                side = ce
                option_type = "CE"
            elif regime_name == REGIME_BEARISH and pe_cross:
                side = pe
                option_type = "PE"
            if side is None or option_type is None:
                continue
            ltp = float(side.get("ltp") or 0.0)
            # No premium price filter — ATM-only universe is liquid by
            # construction. We still apply the IV sanity scaler so
            # implausibly-high IV broker readings (>90%) are dropped.
            from agent.iv_size_policy import iv_size_scaler as _iv_size_scaler_replay
            iv_raw = side.get("iv")
            iv_pct = None
            if iv_raw is not None:
                iv_val = float(iv_raw)
                iv_pct = iv_val * 100.0 if iv_val < 1.0 else iv_val
            _iv_scaler, _ = _iv_size_scaler_replay(iv_pct, None)
            if _iv_scaler <= 0:
                continue
            signal_keys.add((underlying, option_type, paired["bucket"].isoformat()))

        return len(signal_keys)

    async def _replay_strategy2_session(self, trading_day: date) -> dict[str, Any]:
        runtime = self._strategy2
        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT time,
                               timezone('Asia/Kolkata', time) AS local_time,
                               underlying,
                               expiry::text AS expiry,
                               strike::float8 AS strike,
                               option_type,
                               instrument_key,
                               trading_symbol,
                               underlying_price::float8 AS underlying_price,
                               ltp::float8 AS ltp,
                               iv::float8 AS iv
                        FROM atm_option_watchlist_snapshots
                        WHERE time::date = :day
                          AND underlying = ANY(:underlyings)
                        ORDER BY time ASC
                        """
                    ),
                    {"day": trading_day, "underlyings": list(STRATEGY2_UNDERLYINGS)},
                )
            ).mappings().all()

        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            local_time = _ensure_ist_datetime(row.get("local_time"))
            if local_time is None:
                continue
            bucket = local_time.replace(
                minute=(local_time.minute // 5) * 5,
                second=0,
                microsecond=0,
            )
            key = (str(row.get("underlying") or ""), bucket.isoformat())
            paired = grouped.setdefault(key, {"bucket": bucket, "underlying": row.get("underlying"), "expiry": row.get("expiry")})
            option_key = str(row.get("option_type") or "").lower()
            current = paired.get(option_key)
            if current is None or row.get("time") > current.get("time"):
                paired[option_key] = dict(row)

        option_cache: dict[str, list[dict[str, Any]]] = {}
        spot_cache: dict[str, list[dict[str, Any]]] = {}
        latest_signals: dict[str, dict[str, Any]] = {}
        latest_pipeline: dict[str, dict[str, Any]] = {}
        entry_ready_count = 0
        trend_aligned_count = 0
        last_seen_at: Optional[datetime] = None

        async def option_minutes(instrument_key: str) -> list[dict[str, Any]]:
            if instrument_key not in option_cache:
                option_cache[instrument_key] = await option_history_service._fetch_broker_candles(
                    instrument_key=instrument_key,
                    from_date=trading_day,
                    to_date=trading_day,
                    interval="1minute",
                )
            return option_cache[instrument_key]

        async def spot_minutes(underlying: str, started_at: datetime) -> list[dict[str, Any]]:
            if underlying not in spot_cache:
                rows, _source = await self._load_strategy2_spot_rows(underlying, started_at)
                spot_cache[underlying] = rows
            return spot_cache[underlying]

        for paired in sorted(grouped.values(), key=lambda item: (item["bucket"], item["underlying"])):
            underlying = str(paired.get("underlying") or "")
            ce = paired.get("ce")
            pe = paired.get("pe")
            if not ce or not pe:
                continue
            started_at = _ensure_ist_datetime(paired["bucket"]) or paired["bucket"]
            spot_rows_all = await spot_minutes(underlying, started_at)
            spot_rows = [
                row
                for row in spot_rows_all
                if (_parse_iso_timestamp(row.get("time")) or datetime.min.replace(tzinfo=IST)) <= started_at
            ]
            session_rows, session_date = _latest_populated_session_rows(spot_rows)
            if len(session_rows) < 30:
                continue
            profile = market_profile_builder.build_profile_from_rows(underlying, session_rows, "day", "1minute")
            if not profile:
                continue
            current_spot = float(session_rows[-1].get("close") or ce.get("underlying_price") or 0.0)
            direction, day_type, gate_reason = self._classify_strategy2_market_profile(
                profile=profile,
                current_spot=current_spot,
                today_rows=session_rows,
            )
            if direction not in {"CE", "PE"}:
                continue

            ce_minute_rows = await option_minutes(str(ce.get("instrument_key") or ""))
            pe_minute_rows = await option_minutes(str(pe.get("instrument_key") or ""))
            ce_slice = [
                row
                for row in ce_minute_rows
                if (_parse_iso_timestamp(row.get("time")) or datetime.min.replace(tzinfo=IST)) <= started_at
            ]
            pe_slice = [
                row
                for row in pe_minute_rows
                if (_parse_iso_timestamp(row.get("time")) or datetime.min.replace(tzinfo=IST)) <= started_at
            ]
            ce_candles = option_history_service._aggregate_rows(ce_slice, STRATEGY2_OPTION_BAR_MINUTES)
            pe_candles = option_history_service._aggregate_rows(pe_slice, STRATEGY2_OPTION_BAR_MINUTES)
            ce_closes = [float(item["close"]) for item in ce_candles if item.get("close") is not None]
            pe_closes = [float(item["close"]) for item in pe_candles if item.get("close") is not None]
            ce_symbol = str(ce.get("instrument_key") or ce.get("trading_symbol") or f"{underlying}:CE")
            pe_symbol = str(pe.get("instrument_key") or pe.get("trading_symbol") or f"{underlying}:PE")
            ce_last_bar_time = str(ce_candles[-1].get("time") or "") if ce_candles else None
            pe_last_bar_time = str(pe_candles[-1].get("time") or "") if pe_candles else None
            if direction == "CE":
                fresh_cross, _, _ = detect_macd_zero_cross(
                    ce_closes,
                    "CE",
                    symbol=ce_symbol,
                    timeframe=STRATEGY2_OPTION_TIMEFRAME,
                    last_bar_time=ce_last_bar_time,
                )
                macd_line, _, _ = _strategy_macd(
                    ce_closes,
                    symbol=ce_symbol,
                    timeframe=STRATEGY2_OPTION_TIMEFRAME,
                    last_bar_time=ce_last_bar_time,
                ) if len(ce_closes) >= MACD_MIN_BARS else ([], [], [])
                macd_value = macd_line[-1] if macd_line else None
                aligned = macd_value is not None and macd_value > 0
                option_last_bar_time = ce_last_bar_time
            else:
                fresh_cross, _, _ = detect_macd_zero_cross(
                    pe_closes,
                    "PE",
                    symbol=pe_symbol,
                    timeframe=STRATEGY2_OPTION_TIMEFRAME,
                    last_bar_time=pe_last_bar_time,
                )
                macd_line, _, _ = _strategy_macd(
                    pe_closes,
                    symbol=pe_symbol,
                    timeframe=STRATEGY2_OPTION_TIMEFRAME,
                    last_bar_time=pe_last_bar_time,
                ) if len(pe_closes) >= MACD_MIN_BARS else ([], [], [])
                macd_value = macd_line[-1] if macd_line else None
                aligned = macd_value is not None and macd_value < 0
                option_last_bar_time = pe_last_bar_time
            if not option_last_bar_time:
                continue

            status = "waiting-cross"
            instruction = f"{underlying}: MP gate is {day_type}, waiting for MACD to align with zero."
            if fresh_cross:
                status = "entry-ready"
                entry_ready_count += 1
                instruction = f"{underlying}: {direction} zero-cross confirmed with MP {day_type} gate."
            elif aligned:
                status = "entry-ready"
                entry_ready_count += 1
                instruction = f"{underlying}: MP {day_type} gate is aligned and {direction} MACD is beyond zero."

            side_closes = ce_closes if direction == "CE" else pe_closes
            latest_hist_val: Optional[float] = None
            prev_hist_val: Optional[float] = None
            try:
                if len(side_closes) >= MACD_MIN_BARS:
                    side_symbol = ce_symbol if direction == "CE" else pe_symbol
                    _, _, side_hist = _strategy_macd(
                        side_closes,
                        symbol=side_symbol,
                        timeframe=STRATEGY2_OPTION_TIMEFRAME,
                        last_bar_time=option_last_bar_time,
                    )
                    if side_hist:
                        latest_hist_val = side_hist[-1]
                        if len(side_hist) >= 2:
                            prev_hist_val = side_hist[-2]
            except Exception:
                pass
            bucket_info = classify_signal_bucket(
                has_position=self._has_underlying_position(runtime, underlying),
                signal_validation="ready" if status == "entry-ready" else status,
                macd=macd_value,
                macd_histogram=latest_hist_val,
                prev_macd_histogram=prev_hist_val,
                recent_cross_signal=("BUY" if direction == "CE" and fresh_cross else "SELL" if direction == "PE" and fresh_cross else None),
                recent_cross_bars_ago=0 if fresh_cross else None,
            )

            signal = {
                "strategy": "Strategy 2",
                "source": "historical_replay",
                "underlying": underlying,
                "signal_date": trading_day.isoformat(),
                "trade_date": "historical replay",
                "as_of": started_at.isoformat(),
                "direction": direction,
                "reason": gate_reason,
                "strength": "strong" if status == "entry-ready" else "monitoring",
                "status": status,
                "freshness": "session-close",
                "instruction": instruction,
                "mp_day_type": day_type,
                "spot_price": _round_or_none(current_spot, 2),
                "poc": _round_or_none(profile.poc, 2),
                "vah": _round_or_none(profile.vah, 2),
                "val": _round_or_none(profile.val, 2),
                "option_last_bar_time": option_last_bar_time,
                "spot_last_time": session_rows[-1].get("time"),
                "spot_source": "historical",
                "spot_session_date": session_date.isoformat() if session_date else trading_day.isoformat(),
                **bucket_info,
            }
            latest_signals[underlying] = signal
            latest_pipeline[underlying] = {
                "name": f"Strategy 2 {underlying}",
                "status": "ok",
                "rows": len(session_rows),
                "last_date": str(option_last_bar_time),
                "detail": f"Historical replay · MP {day_type} · {status}",
                "freshness": "session-close",
            }
            last_seen = _parse_iso_timestamp(option_last_bar_time) or started_at
            last_seen_at = max(last_seen_at, last_seen) if last_seen_at else last_seen

        if latest_signals:
            runtime.signal_lane = [latest_signals[underlying] for underlying in STRATEGY2_UNDERLYINGS if underlying in latest_signals]
            runtime.meta = {
                "mode": "historical_recovery",
                "scan_interval": STRATEGY2_OPTION_TIMEFRAME,
                "watchlist_rows": len(runtime.signal_lane),
                "updated_at": (last_seen_at or datetime.combine(trading_day, time(15, 20), tzinfo=IST)).isoformat(),
                "pipeline": [latest_pipeline[underlying] for underlying in STRATEGY2_UNDERLYINGS if underlying in latest_pipeline],
                "recovered_trading_day": trading_day.isoformat(),
                "entry_ready_count": entry_ready_count,
                "trend_aligned_count": trend_aligned_count,
                "market_state": "closed",
            }
            runtime.last_scan_at = (last_seen_at or datetime.combine(trading_day, time(15, 20), tzinfo=IST)).isoformat()
            runtime.last_message = (
                f"Recovered Strategy 2 from {trading_day.isoformat()}: "
                f"{entry_ready_count} entry-ready, {trend_aligned_count} trend-aligned."
            )

        return {
            "entry_ready_count": entry_ready_count,
            "trend_aligned_count": trend_aligned_count,
            "last_seen_at": last_seen_at,
        }

    # ── Main Scan ────────────────────────────────────────────────────────────

    async def run_once(self, *, force: bool = False) -> dict[str, Any]:
        if self._lock.locked() and not force:
            return self.get_status()

        async with self._lock:
            self._running = True
            started_at = _now_ist()
            self._last_error = None
            option_history_service.reset_health()
            try:
                try:
                    from market_data.data_quality_agent import data_quality_agent

                    data_quality_snapshot = data_quality_agent.snapshot()
                except Exception as exc:
                    data_quality_snapshot = {"overall": "unknown", "error": str(exc)}

                if not _in_market_hours(started_at):
                    local_only_mode = settings.MARKET_INTELLIGENCE_STRATEGY_LOCAL_ONLY or settings.PAPER_TRADING_ONLY
                    try:
                        market_intelligence_health = await market_intelligence_runtime.get_strategy_health()
                    except Exception as exc:
                        market_intelligence_health = {"ready": False, "error": str(exc)}
                    try:
                        broker_snapshot = await get_broker_connection_snapshot(force_validate=False)
                    except Exception as exc:
                        broker_snapshot = {"broker_ready": False, "error": str(exc)}
                    self._last_data_health = {
                        **({"broker_snapshot": broker_snapshot} if broker_snapshot is not None else {}),
                        "market_intelligence": market_intelligence_health,
                        "data_quality": data_quality_snapshot,
                        "option_history": option_history_service.get_health_snapshot(),
                    }
                    if market_intelligence_health.get("ready") or (broker_snapshot or {}).get("broker_ready"):
                        await self._prepare_closed_market_state(
                            started_at,
                            market_intelligence_health=market_intelligence_health,
                            broker_snapshot=broker_snapshot,
                            local_only_mode=local_only_mode,
                        )
                    else:
                        last_live_scan = self._last_run_at or self._strategy2.last_scan_at or self._strategy1.last_scan_at
                        self._last_message = (
                            f"Market closed. Showing last live strategy state from {last_live_scan}."
                            if last_live_scan
                            else "Waiting for NSE market hours."
                        )
                        for lane in self._lane_agents():
                            lane.on_market_closed(started_at, last_live_scan)
                            if _looks_like_stale_blocking_message(lane.runtime.last_message):
                                lane.runtime.last_message = self._last_message
                        self._append_commentary("System", "Market closed. Agent idle.", tone="idle")
                    return await self._status_with_risk_snapshot()

                local_only_mode = settings.MARKET_INTELLIGENCE_STRATEGY_LOCAL_ONLY or settings.PAPER_TRADING_ONLY
                broker_snapshot: dict[str, Any] | None = None
                market_intelligence_health: dict[str, Any] | None = None
                if local_only_mode:
                    market_intelligence_health = await market_intelligence_runtime.get_strategy_health()
                    self._last_data_health = {
                        "market_intelligence": market_intelligence_health,
                        "data_quality": data_quality_snapshot,
                        "option_history": option_history_service.get_health_snapshot(),
                    }
                    # Gate only on hard "critical" (flagged data, broker outage).
                    # "degraded" is informational — happens routinely at boot,
                    # right after broker reconnect, on intraday slow ticks, and
                    # whenever NSE market is between sessions. Blocking on
                    # degraded turns benign warmups into desk-wide outages.
                    if (
                        settings.DATA_QUALITY_SCAN_GATE_ENABLED
                        and
                        int(data_quality_snapshot.get("symbol_count") or 0) > 0
                        and data_quality_snapshot.get("overall") == "critical"
                    ):
                        message = (
                            "Data quality gate blocked the NSE paper scan: "
                            f"{data_quality_snapshot.get('overall')} market-data freshness."
                        )
                        self._last_error = message
                        self._last_message = message
                        for lane in self._lane_agents():
                            lane.runtime.last_message = message
                            lane.runtime.meta = {
                                **(lane.runtime.meta or {}),
                                "mode": "data_quality_blocked",
                                "updated_at": started_at.isoformat(),
                                "data_quality": data_quality_snapshot,
                            }
                        self._append_commentary("System", message, tone="warning")
                        return await self._status_with_risk_snapshot()
                    if not self._market_intelligence_execution_ready(market_intelligence_health):
                        message = self._local_data_failure_message(market_intelligence_health)
                        self._last_error = message
                        self._last_message = message
                        data_mode = (
                            "local_data_stale"
                            if market_intelligence_health.get("ready")
                            else "local_data_unavailable"
                        )
                        for lane in self._lane_agents():
                            lane.runtime.last_message = message
                            lane.runtime.meta = {
                                **(lane.runtime.meta or {}),
                                "mode": data_mode,
                                "updated_at": started_at.isoformat(),
                                "market_intelligence": market_intelligence_health,
                            }
                        self._append_commentary("System", message, tone="warning")
                        return await self._status_with_risk_snapshot()
                else:
                    broker_snapshot = await get_broker_connection_snapshot(force_validate=False)
                    # Populate market_intelligence health in broker mode too —
                    # the dashboard's "NSE CE/PE MACD + MP" readiness card reads
                    # `data_health.market_intelligence.{watchlist_rows_latest,
                    # latest_ce_ready, latest_pe_ready, ready, readiness_mode}`.
                    # Skipping this branch leaves it perpetually "CHECKING" with
                    # 0/0/0 even while the strategy is actively scanning.
                    try:
                        market_intelligence_health = await market_intelligence_runtime.get_strategy_health()
                    except Exception as exc:
                        market_intelligence_health = {"ready": False, "error": str(exc)}
                    self._last_data_health = {
                        "broker_snapshot": broker_snapshot,
                        "market_intelligence": market_intelligence_health,
                        "data_quality": data_quality_snapshot,
                        "option_history": option_history_service.get_health_snapshot(),
                    }
                    # Gate only on hard "critical" (flagged data, broker outage).
                    # "degraded" is informational — happens routinely at boot,
                    # right after broker reconnect, on intraday slow ticks, and
                    # whenever NSE market is between sessions. Blocking on
                    # degraded turns benign warmups into desk-wide outages.
                    if (
                        settings.DATA_QUALITY_SCAN_GATE_ENABLED
                        and
                        int(data_quality_snapshot.get("symbol_count") or 0) > 0
                        and data_quality_snapshot.get("overall") == "critical"
                    ):
                        message = (
                            "Data quality gate blocked the NSE paper scan: "
                            f"{data_quality_snapshot.get('overall')} market-data freshness."
                        )
                        self._last_error = message
                        self._last_message = message
                        for lane in self._lane_agents():
                            lane.runtime.last_message = message
                            lane.runtime.meta = {
                                **(lane.runtime.meta or {}),
                                "mode": "data_quality_blocked",
                                "updated_at": started_at.isoformat(),
                                "data_quality": data_quality_snapshot,
                            }
                        self._append_commentary("System", message, tone="warning")
                        return await self._status_with_risk_snapshot()
                    if not broker_snapshot.get("broker_ready"):
                        message = self._broker_failure_message(broker_snapshot)
                        self._last_error = message
                        self._last_message = message
                        for lane in self._lane_agents():
                            lane.on_broker_unavailable(started_at, broker_snapshot, message)
                        self._append_commentary("System", message, tone="error")
                        return await self._status_with_risk_snapshot()

                for lane in self._lane_agents():
                    lane.mark_scan_started(started_at)

                universe_bootstrap = {"status": "ready", "counts_after": {}}
                if not local_only_mode:
                    universe_bootstrap = await ensure_fo_underlying_catalog()
                    if universe_bootstrap.get("status") in {"partial", "skipped_no_upstox"}:
                        self._append_commentary(
                            "Strategy 1",
                            (
                                "F&O universe bootstrap is incomplete. "
                                f"Catalog rows with keys: {((universe_bootstrap.get('counts_after') or {}).get('keyed_rows') or 0)}."
                            ),
                            tone="warning",
                        )

                # 1. Get trading windows
                self._active_windows = await get_all_active_windows(as_of=started_at.date())
                self._scan_windows = await get_all_strategy1_scan_windows(as_of=started_at.date())
                self._last_candidate_expiries = sorted({
                    get_monthly_expiry(w["expiry"].year, w["expiry"].month).isoformat()
                    for w in self._scan_windows
                })
                self._last_expiry = self._last_candidate_expiries[0] if self._last_candidate_expiries else None
                rolled_underlyings = sorted(
                    str(window.get("underlying") or "")
                    for window in self._scan_windows
                    if str(window.get("window_state") or "") == "future"
                )
                if rolled_underlyings:
                    preview = ", ".join(rolled_underlyings[:5])
                    suffix = "..." if len(rolled_underlyings) > 5 else ""
                    self._append_commentary(
                        "Strategy 1",
                        (
                            "Current monthly window is exhausted for "
                            f"{len(rolled_underlyings)} underlyings. "
                            f"Rolling scan to the next expiry: {preview}{suffix}"
                        ),
                        tone="warning",
                    )

                # 2. Get ATM watchlist rows for each active expiry.
                # Also include the broker's default (nearest weekly) expiry in
                # case the monthly expiry isn't directly available in the live chain.
                monthly_expiries = list(self._last_candidate_expiries)
                expiry_scope = await atm_watchlist_service.get_expiries(
                    self._last_expiry,
                    live_refresh=not local_only_mode,
                )
                native_index_expiries = sorted(
                    {
                        str(expiry)
                        for underlying, expiry in dict(expiry_scope.get("index_monthlies") or {}).items()
                        if underlying in STRATEGY2_UNDERLYINGS and str(expiry or "").strip()
                    }
                )
                watchlist_requests: list[tuple[Optional[str], Optional[list[str]]]] = []
                seen_requests: set[tuple[Optional[str], tuple[str, ...]]] = set()

                def _append_watchlist_request(
                    candidate_expiry: Optional[str],
                    candidate_symbols: Optional[list[str]] = None,
                ) -> None:
                    request_key = (
                        candidate_expiry,
                        tuple(sorted(str(symbol) for symbol in (candidate_symbols or []))),
                    )
                    if request_key in seen_requests:
                        return
                    seen_requests.add(request_key)
                    watchlist_requests.append((candidate_expiry, candidate_symbols))

                for candidate in monthly_expiries:
                    _append_watchlist_request(candidate)
                for candidate in native_index_expiries:
                    if candidate not in monthly_expiries:
                        _append_watchlist_request(candidate, list(STRATEGY2_UNDERLYINGS))
                _append_watchlist_request(None, list(STRATEGY2_UNDERLYINGS))
                watchlists = await asyncio.gather(
                    *(
                        atm_watchlist_service.get_watchlist(expiry=request_expiry, symbols=request_symbols)
                        if local_only_mode
                        else atm_watchlist_service.get_watchlist(
                            expiry=request_expiry,
                            symbols=request_symbols,
                            live_refresh=True,
                        )
                        for request_expiry, request_symbols in watchlist_requests
                    ),
                    return_exceptions=True,
                )
                # Merge rows: monthly-expiry rows first, broker-default rows as fallback.
                # Deduplicate by underlying only — one row per underlying.
                # Monthly expiry is preferred over broker-default nearest expiry
                # to avoid dual entries when both weekly and monthly chains exist.
                monthly_set = set(monthly_expiries)
                rows_by_underlying: dict[str, dict] = {}

                # First pass: add monthly-expiry rows (highest priority)
                for wl in watchlists:
                    if not isinstance(wl, dict):
                        continue
                    for r in (wl.get("rows") or []):
                        und = r.get("underlying", "")
                        row_expiry = r.get("expiry", "")
                        if row_expiry in monthly_set:
                            # Only overwrite if this underlying doesn't have a monthly row yet
                            if und not in rows_by_underlying or rows_by_underlying[und].get("expiry") not in monthly_set:
                                rows_by_underlying[und] = r

                # Second pass: broker-default rows fill gaps only (no monthly row found)
                for wl in watchlists:
                    if not isinstance(wl, dict):
                        continue
                    for r in (wl.get("rows") or []):
                        und = r.get("underlying", "")
                        row_expiry = r.get("expiry", "")
                        if row_expiry not in monthly_set and und not in rows_by_underlying:
                            rows_by_underlying[und] = r

                rows = list(rows_by_underlying.values())
                strategy2_native_rows = await self._load_strategy2_native_watchlist_rows(
                    expiry_scope,
                    live_refresh=not local_only_mode,
                )
                if strategy2_native_rows:
                    # The S2 loader returns composite-keyed rows
                    # ("NIFTY:weekly", "NIFTY:monthly", …). When we have
                    # composite entries for an underlying, drop the
                    # legacy single-key entry so the S2 lane doesn't see
                    # the same underlying both as a one-expiry and as a
                    # multi-expiry row matrix.
                    s2_underlyings_with_tracks: set[str] = set()
                    for key in strategy2_native_rows.keys():
                        if ":" in key:
                            s2_underlyings_with_tracks.add(key.split(":", 1)[0])
                    for underlying in s2_underlyings_with_tracks:
                        rows_by_underlying.pop(underlying, None)
                    rows_by_underlying.update(strategy2_native_rows)
                    rows = list(rows_by_underlying.values())
                expiries = sorted({r.get("expiry", "") for r in rows if r.get("expiry")})

                if not rows:
                    detail_messages = [
                        str(wl.get("detail") or "").strip()
                        for wl in watchlists
                        if isinstance(wl, dict) and str(wl.get("detail") or "").strip()
                    ]
                    health_warning = self._option_history_warning(option_history_service.get_health_snapshot())
                    self._last_message = "ATM watchlist empty for active windows."
                    if detail_messages:
                        self._last_message = f"{self._last_message} {' '.join(detail_messages[:2])}"
                    if health_warning:
                        self._last_message = f"{self._last_message} {health_warning}"
                    for lane in self._lane_agents():
                        lane.on_watchlist_unavailable(
                            started_at,
                            detail_messages=detail_messages,
                            history_warning=health_warning,
                        )
                    self._append_commentary("System", "No ATM watchlist data available.", tone="warning")
                    return await self._status_with_risk_snapshot()

                # 3. Build window lookup
                window_map = {w["underlying"]: w for w in self._scan_windows}

                for lane in self._lane_agents():
                    await lane.run_cycle(
                        rows,
                        started_at,
                        window_map=window_map,
                        expiries=expiries,
                    )
                await self._maybe_send_telegram_report()

                self._last_run_at = _now_ist().isoformat()
                n_pos = len(self._strategy1.positions)
                s2_pos = len(self._strategy2.positions)
                option_history_health = option_history_service.get_health_snapshot()
                self._last_data_health = {
                    **({"broker_snapshot": broker_snapshot} if broker_snapshot is not None else {}),
                    **({"market_intelligence": market_intelligence_health} if market_intelligence_health is not None else {}),
                    "option_history": option_history_health,
                }
                self._last_message = (
                    f"Scanned {len(rows)} instruments across {len(expiries)} expiries. "
                    f"S1={n_pos} open, S2={s2_pos} open."
                )
                history_warning = self._option_history_warning(option_history_health)
                if history_warning:
                    self._last_message = f"{self._last_message} {history_warning}"
                if history_warning:
                    self._strategy1.last_message = f"{self._strategy1.last_message or 'Strategy 1 scan complete.'} {history_warning}"
                    self._strategy2.last_message = f"{self._strategy2.last_message or 'Strategy 2 scan complete.'} {history_warning}"
                self._append_commentary(
                    "System",
                    f"Scan complete. {len(rows)} rows, S1={n_pos}, S2={s2_pos}.",
                    tone="warning" if history_warning else "success",
                )
                if history_warning:
                    self._append_commentary("System", history_warning, tone="warning")
                # Capture equity snapshot for equity curve chart + persist to Redis
                for item in self._runtimes():
                    item.portfolio.snapshot_equity()
                    await item.portfolio.persist_equity_to_redis()

                # Periodically sync spot candles to keep MA context fresh
                await self._maybe_sync_spot_candles()

                return await self._status_with_risk_snapshot()

            except Exception as exc:
                self._last_error = str(exc)
                self._last_message = f"Agent error: {exc}"
                for lane in self._lane_agents():
                    lane.last_message = self._last_message
                self._append_commentary("System", f"Error: {exc}", tone="error")
                raise
            finally:
                self._running = False
                await self._apersist_state()

    # ── Entry Scanning ───────────────────────────────────────────────────────

    async def _scan_entries(
        self,
        runtime: StrategyRuntime,
        rows: list[dict[str, Any]],
        window_map: dict[str, dict],
    ) -> None:
        return await StrategyEntryMixin._scan_entries(self, runtime, rows, window_map)

    # ── Position Entry ───────────────────────────────────────────────────────

    async def _open_position(self, runtime: StrategyRuntime, candidate: dict[str, Any]) -> None:
        return await StrategyEntryMixin._open_position(self, runtime, candidate)

    def _get_sizing_mode(self, candidate: dict) -> str:
        return StrategyEntryMixin._get_sizing_mode(self, candidate)

    # ── Exit Management ──────────────────────────────────────────────────────

    async def _manage_exits(
        self, runtime: StrategyRuntime, rows: Optional[list] = None
    ) -> None:
        return await StrategyExitMixin._manage_exits(self, runtime, rows)

    # ── Watchlist LTP Price Refresh ──────────────────────────────────────────

    def _refresh_prices_from_watchlist(
        self, runtime: StrategyRuntime, rows: list[dict[str, Any]]
    ) -> None:
        return StrategyExitMixin._refresh_prices_from_watchlist(self, runtime, rows)

    # ── Position Close ───────────────────────────────────────────────────────

    async def _close_position(
        self,
        runtime: StrategyRuntime,
        position: StrategyPosition,
        exit_price: float,
        reason: str,
        qty: Optional[int] = None,
        partial: bool = False,
    ) -> None:
        return await StrategyExitMixin._close_position(self, runtime, position, exit_price, reason, qty, partial)

    async def operator_close_position(
        self,
        *,
        strategy_key: str,
        symbol: str,
        reason: str = "operator_override",
    ) -> dict[str, Any]:
        """Close one agent-held paper position through an explicit operator override."""
        runtime = self.get_runtime(strategy_key)
        if runtime is None:
            raise ValueError(f"Unknown strategy lane: {strategy_key}")

        position = runtime.positions.get(symbol)
        if position is None:
            raise ValueError(f"No open position for {symbol} in {strategy_key}")

        exit_price = float(position.current_price or position.entry_price or 0.0)
        if exit_price <= 0:
            raise ValueError(f"No valid mark price available for {symbol}")

        close_reason = reason.strip()[:80] if reason and reason.strip() else "operator_override"
        await self._close_position(
            runtime,
            position,
            exit_price,
            close_reason,
            qty=position.qty,
            partial=False,
        )
        self._last_message = f"Operator closed {position.underlying} {position.option_type} {int(position.strike)} from {runtime.label}."
        self._append_commentary("Operator", self._last_message, tone="warning")
        self._persist_state()

        return {
            "closed": True,
            "strategy_key": runtime.key,
            "symbol": symbol,
            "exit_price": exit_price,
            "qty": position.qty,
            "reason": close_reason,
            "status": self.get_status(refresh=False),
        }

    # ── Helper Methods ───────────────────────────────────────────────────────

    async def _load_candles(
        self,
        row: dict,
        side: dict,
        *,
        interval: str = "30minute",
        limit: int = 80,
    ) -> list[dict]:
        candles = await option_history_service.load_candles(
            underlying=row["underlying"],
            expiry=date.fromisoformat(row["expiry"]),
            strike=float(side["strike"]),
            option_type=str(side["option_type"]),
            instrument_key=side.get("instrument_key"),
            interval=interval,
            limit=limit,
            allow_broker_refresh=not (settings.MARKET_INTELLIGENCE_STRATEGY_LOCAL_ONLY or settings.PAPER_TRADING_ONLY),
        )
        candles = list(candles or [])
        snapshot_time = _parse_iso_timestamp(str(side.get("as_of") or row.get("as_of") or ""))
        try:
            snapshot_close = float(side.get("ltp"))
        except (TypeError, ValueError):
            snapshot_close = 0.0
        latest_candle_time = _parse_iso_timestamp(candles[-1].get("time")) if candles else None
        if snapshot_time is not None and snapshot_close > 0 and (
            latest_candle_time is None or snapshot_time > latest_candle_time
        ):
            candles.append(
                {
                    "time": snapshot_time.isoformat(),
                    "open": snapshot_close,
                    "high": snapshot_close,
                    "low": snapshot_close,
                    "close": snapshot_close,
                    "volume": side.get("volume") or 0,
                    "source": "atm_watchlist_snapshot",
                }
            )
        return candles[-limit:]

    async def _run_strategy2(
        self,
        runtime: StrategyRuntime,
        rows: list[dict[str, Any]],
        started_at: datetime,
    ) -> None:
        # S2 wants weekly index contracts and is comfortable trading on
        # expiry day. The shared `rows` argument here is S1's monthly
        # watchlist build — wrong shape for S2. Ask MI for the S2
        # profile's contracts instead and use *that* as the row set.
        try:
            from agent.strategy_profiles import S2_CONTRACT_PROFILE
            s2_payload = await atm_watchlist_service.get_watchlist_for_strategy(
                S2_CONTRACT_PROFILE,
                symbols=list(STRATEGY2_UNDERLYINGS),
                live_refresh=False,
            )
            s2_rows = list((s2_payload or {}).get("rows") or [])
            if s2_rows:
                rows = s2_rows
                logger.debug(
                    f"[Strategy2] MI served {len(s2_rows)} weekly rows via "
                    f"profile={S2_CONTRACT_PROFILE.name}; "
                    f"expiries={s2_payload.get('expiries_requested')}"
                )
            else:
                logger.debug(
                    "[Strategy2] MI returned no weekly rows for profile "
                    f"{S2_CONTRACT_PROFILE.name}; falling back to shared S1 rows."
                )
        except Exception as exc:
            logger.debug(f"[Strategy2] profile-aware MI call failed: {exc}; using shared rows.")
        index_rows = [row for row in rows if row.get("underlying") in STRATEGY2_UNDERLYINGS]
        if not index_rows:
            contexts = {
                underlying: self._build_strategy2_preparation_failure_context(
                    {"underlying": underlying},
                    started_at,
                    RuntimeError("ATM watchlist row missing for configured Strategy 2 underlying"),
                )
                for underlying in STRATEGY2_UNDERLYINGS
            }
            runtime.signal_lane = [
                contexts[underlying]["signal"]
                for underlying in STRATEGY2_UNDERLYINGS
            ]
            runtime.meta = {
                **(runtime.meta or {}),
                "mode": "no_index_rows",
                "scan_interval": STRATEGY2_OPTION_TIMEFRAME,
                "watchlist_rows": 0,
                "updated_at": started_at.isoformat(),
                "pipeline": [contexts[underlying]["pipeline"] for underlying in STRATEGY2_UNDERLYINGS],
            }
            runtime.last_message = "No index ATM rows available for Strategy 2."
            return

        # Contexts are keyed by (underlying, expiry_track) so the weekly
        # and monthly NIFTY/SENSEX rows can carry their own option-side
        # candles, MACD lines, and CVD windows. For status-line display
        # we still want a single representative context per underlying;
        # _ctx_for picks the first one for an underlying (monthly comes
        # first thanks to resolve_s2_expiry_targets's ordering).
        contexts: dict[str, dict[str, Any]] = {}

        def _row_key(r: dict[str, Any]) -> str:
            return f"{r.get('underlying') or ''}:{r.get('expiry_track') or 'monthly'}"

        def _ctx_for(und: str) -> dict[str, Any]:
            for k, v in contexts.items():
                if k.startswith(f"{und}:"):
                    return v
            return {}

        for row in index_rows:
            contexts[_row_key(row)] = await self._build_strategy2_signal_context(row, started_at)
        for underlying in STRATEGY2_UNDERLYINGS:
            if any(k.startswith(f"{underlying}:") for k in contexts):
                continue
            contexts[f"{underlying}:monthly"] = self._build_strategy2_preparation_failure_context(
                {"underlying": underlying},
                started_at,
                RuntimeError("ATM watchlist row missing for configured Strategy 2 underlying"),
            )

        runtime.signal_lane = [
            _ctx_for(underlying)["signal"]
            for underlying in STRATEGY2_UNDERLYINGS
            if _ctx_for(underlying).get("signal")
        ]
        for row in index_rows:
            signal = dict(contexts.get(_row_key(row), {}).get("signal") or {})
            signal.setdefault("expiry", row.get("expiry"))
            signal.setdefault("expiry_track", row.get("expiry_track"))
            signal.setdefault("atm_strike", row.get("atm_strike"))
            direction = signal.get("direction")
            if direction == "CE" and row.get("ce"):
                signal.setdefault("ltp", (row.get("ce") or {}).get("ltp"))
                signal.setdefault("iv_pct", (row.get("ce") or {}).get("iv"))
            elif direction == "PE" and row.get("pe"):
                signal.setdefault("ltp", (row.get("pe") or {}).get("ltp"))
                signal.setdefault("iv_pct", (row.get("pe") or {}).get("iv"))
            await self._persist_agent_signal_observation(runtime, signal, row=row)
        runtime.meta = {
            "mode": "live_scan",
            "scan_interval": STRATEGY2_OPTION_TIMEFRAME,
            "watchlist_rows": len(index_rows),
            "updated_at": started_at.isoformat(),
            "pipeline": [
                _ctx_for(underlying)["pipeline"]
                for underlying in STRATEGY2_UNDERLYINGS
                if _ctx_for(underlying).get("pipeline")
            ],
        }

        await self._manage_strategy2_exits(runtime, index_rows, contexts, started_at)
        await self._scan_strategy2_entries(runtime, index_rows, contexts, started_at)
        self._refresh_prices_from_watchlist(runtime, index_rows)

        actionable = sum(
            1
            for item in runtime.signal_lane
            if item.get("status") in {"entry-ready", "trend-aligned", "active"}
        )
        runtime.last_message = (
            f"Scanned {len(index_rows)} indices. "
            f"{actionable} aligned lanes, {len(runtime.positions)} open positions."
        )

    def _build_strategy2_preparation_failure_context(
        self,
        row: dict[str, Any],
        started_at: datetime,
        exc: Exception,
    ) -> dict[str, Any]:
        underlying = str(row.get("underlying") or "").strip() or "UNKNOWN"
        detail = str(exc).strip()[:160] or exc.__class__.__name__
        signal = {
            "strategy": "Strategy 2",
            "source": "closed_market_preparation",
            "underlying": underlying,
            "signal_date": started_at.date().isoformat(),
            "trade_date": "pre-market preparation",
            "as_of": started_at.isoformat(),
            "direction": None,
            "reason": "preparation_error",
            "strength": "standby",
            "status": "waiting",
            "freshness": "missing",
            "instruction": f"{underlying}: Strategy 2 preparation failed: {detail}. Recheck after market open.",
            "expiry": row.get("expiry"),
            "atm_strike": row.get("atm_strike"),
            "spot_price": _round_or_none(row.get("spot_price"), 2),
            **classify_status_bucket(
                has_position=self._has_underlying_position(self._strategy2, underlying),
                status="waiting",
            ),
        }
        return {
            "direction": None,
            "signal": signal,
            "pipeline": {
                "name": f"Strategy 2 {underlying}",
                "status": "warning",
                "rows": 0,
                "last_date": "—",
                "detail": f"Closed-market preparation error: {detail}",
                "freshness": "missing",
            },
            "can_enter": False,
        }

    def _normalize_strategy2_prepared_signal(
        self,
        signal: dict[str, Any],
        *,
        row: dict[str, Any],
        started_at: datetime,
    ) -> dict[str, Any]:
        underlying = str(signal.get("underlying") or row.get("underlying") or "").strip()
        raw_status = str(signal.get("status") or "waiting").lower().strip()
        direction = str(signal.get("direction") or "").upper().strip()
        if direction not in {"CE", "PE"}:
            direction = None

        if raw_status in {"entry-ready", "conditions_met", "active", "trend-aligned"} and direction:
            status = "trend-aligned"
        elif raw_status in {"waiting-cross", "waiting"}:
            status = raw_status
        elif raw_status == "standby":
            status = "standby"
        elif raw_status in {"blocked", "avoid"}:
            status = "avoid"
        else:
            status = "waiting"

        side = (row.get("ce") if direction == "CE" else row.get("pe")) or {}
        ltp = side.get("ltp")
        iv_pct = None
        try:
            if side.get("iv") is not None:
                iv_value = float(side.get("iv"))
                iv_pct = iv_value * 100.0 if iv_value <= 1.0 else iv_value
        except (TypeError, ValueError):
            iv_pct = None

        priority_score = None
        if direction == "CE":
            priority_score = _round_or_none(abs(float(signal.get("ce_macd") or 0.0)), 4)
        elif direction == "PE":
            priority_score = _round_or_none(abs(float(signal.get("pe_macd") or 0.0)), 4)
        if priority_score in {None, 0}:
            _, _, score = self._prepared_side_score(row)
            priority_score = _round_or_none(score, 4)

        instruction = str(signal.get("instruction") or f"{underlying}: waiting for Strategy 2 live confirmation.")
        if raw_status in {"entry-ready", "conditions_met", "active"}:
            instruction = (
                f"{instruction} Closed-market zero-crosses are monitoring context only; "
                "re-confirm on the first live 15-minute bar before entry."
            )
        else:
            instruction = f"{instruction} Re-confirm on the first live 15-minute bar before entry."

        prepared = {
            **signal,
            "source": "closed_market_preparation",
            "raw_source": signal.get("source"),
            "prepared_status": raw_status,
            "underlying": underlying,
            "signal_date": started_at.date().isoformat(),
            "trade_date": "pre-market preparation",
            "as_of": started_at.isoformat(),
            "direction": direction,
            "status": status,
            "strength": "monitoring" if status in {"trend-aligned", "waiting-cross", "waiting"} else signal.get("strength", "standby"),
            "instruction": instruction,
            "expiry": signal.get("expiry") or row.get("expiry"),
            "atm_strike": signal.get("atm_strike") or row.get("atm_strike"),
            "strike": signal.get("strike") or row.get("atm_strike"),
            "ltp": signal.get("ltp") if signal.get("ltp") is not None else ltp,
            "iv_pct": signal.get("iv_pct") if signal.get("iv_pct") is not None else iv_pct,
            "priority_score": signal.get("priority_score") if signal.get("priority_score") is not None else priority_score,
            **classify_status_bucket(
                has_position=self._has_underlying_position(self._strategy2, underlying),
                status=status,
            ),
        }
        return prepared

    async def _scan_strategy2_entries(
        self,
        runtime: StrategyRuntime,
        rows: list[dict[str, Any]],
        contexts: dict[str, dict[str, Any]],
        started_at: datetime,
    ) -> None:
        capacity = STRATEGY2_MAX_POSITIONS - len(runtime.positions)
        if capacity <= 0:
            return
        if self._kill_switch_active:
            return

        opened = 0
        learning_scores = await strategy_learning_service.load_scores(runtime.key)
        for row in rows:
            if opened >= capacity:
                break
            # Context key: prefer (underlying, expiry_track) so weekly /
            # monthly rows for the same underlying carry their own
            # option-side candles + MACD lines. Falls back to the legacy
            # underlying-only lookup so old callers / pre-refactor states
            # still resolve cleanly.
            _ctx_key = f"{row.get('underlying') or ''}:{row.get('expiry_track') or 'monthly'}"
            context = contexts.get(_ctx_key) or contexts.get(row["underlying"]) or {}
            if not context.get("can_enter"):
                continue
            # Uniqueness gate: in the weekly+monthly routing world for
            # NIFTY/SENSEX we want BOTH legs open, so we check on
            # (underlying, expiry) rather than underlying alone. For the
            # monthly-only underlyings the result is identical because
            # there's only one row per underlying.
            if self._has_underlying_expiry_position(
                runtime, row["underlying"], str(row.get("expiry") or "")
            ):
                continue
            if started_at.time() > STRATEGY2_ENTRY_CUTOFF:
                continue

            direction = context.get("direction")
            side = row.get("ce") if direction == "CE" else row.get("pe")
            closes = context.get("ce_closes") if direction == "CE" else context.get("pe_closes")
            macd_line = context.get("ce_macd_line") if direction == "CE" else context.get("pe_macd_line")
            if not side or not closes:
                continue

            try:
                opt_expiry = date.fromisoformat(str(row.get("expiry")))
                tte_days = max((opt_expiry - started_at.date()).days, 0)
            except Exception:
                tte_days = 0

            latest_price = float(side.get("ltp") or (closes[-1] if closes else 0.0) or 0.0)
            if latest_price <= 0:
                continue
            data_quality_block = _data_quality_observation_block_reason(
                symbol=str(side.get("instrument_key") or side.get("trading_symbol") or f"{row['underlying']}:{direction}"),
                source="option_history_5m",
                observed_at=context.get("option_last_bar_time"),
                now=started_at,
            )
            if data_quality_block:
                self._append_commentary(
                    runtime.label,
                    f"{row['underlying']} {direction} skipped by data quality gate: {data_quality_block}",
                    tone="warning",
                )
                continue

            # ── CVD-agreement gate ──────────────────────────────────────
            # S2 buys premium directly. For the entry to make sense, the
            # chosen side's bar-CVD over the last 6 bars should be rising
            # (more premium accumulation, not distribution). When it
            # disagrees we wait for the next bar instead of paying up
            # into a fading move.
            cvd_window = (
                context.get("ce_cvd_window")
                if direction == "CE"
                else context.get("pe_cvd_window")
            ) or []
            if cvd_window and len(cvd_window) >= 2 and not cvd_agrees_with("BUY", cvd_window):
                self._append_commentary(
                    runtime.label,
                    f"{row['underlying']} {direction} signal valid but bar-CVD disagreeing "
                    f"({cvd_window[0]:.0f} → {cvd_window[-1]:.0f}); skipping entry.",
                    tone="warning",
                )
                continue

            candidate = {
                "row": row,
                "side": side,
                "closes": closes,
                "candles": context.get("ce_candles") if direction == "CE" else context.get("pe_candles"),
                "latest_close": latest_price,
                "latest_bar_time": context.get("option_last_bar_time") or started_at.isoformat(),
                "signal_key": f"strategy2:{row['underlying']}:{direction}",
                "strength": abs(float(context.get("ce_macd_value") or 0.0))
                if direction == "CE"
                else abs(float(context.get("pe_macd_value") or 0.0)),
                "reason": f"strategy2_{context.get('gate_reason')}_{context.get('entry_reason') or 'macd_above_zero'}",
                "rsi": latest_macd_rsi(closes).get("rsi"),
                "opt_type": direction,
                "iv_pct": _round_or_none(float(side.get("iv") or 0.0) * 100.0, 1) if side.get("iv") is not None else None,
                "iv_status": "preferred" if side.get("iv") is not None else "unknown",
                "spot_setup": f"mp_{context.get('day_type')}",
                "quadrant": None,
                "window": None,
                "tte_days": tte_days,
                "macd_line": macd_line,
                "mp_day_type": context.get("day_type"),
                "fraction_override": max(KELLY_FRACTION * STRATEGY2_KELLY_SCALE, 0.01),
            }
            score = strategy_learning_service.pick_score(
                learning_scores,
                strategy_key=runtime.key,
                underlying=str(row.get("underlying") or ""),
                option_type=str(direction or ""),
                signal_reason=str(candidate.get("reason") or ""),
            )
            strategy_learning_service.annotate_payload(candidate, score)
            candidate["fraction_override"] = max(
                0.005,
                float(candidate["fraction_override"]) * float(candidate.get("learning_size_multiplier") or 1.0),
            )
            if candidate.get("learning_blocked"):
                continue
            if runtime.processed_signals.get(candidate["signal_key"]) == candidate["latest_bar_time"]:
                continue
            await self._open_position(runtime, candidate)
            opened += 1

        if opened:
            self._append_commentary(
                runtime.label,
                f"Opened {opened} Strategy 2 position{'s' if opened != 1 else ''} from live 15-minute index signals.",
                tone="info",
            )

    async def _manage_strategy2_exits(
        self,
        runtime: StrategyRuntime,
        rows: list[dict[str, Any]],
        contexts: dict[str, dict[str, Any]],
        started_at: datetime,
    ) -> None:
        if not runtime.positions:
            return

        row_map = {row["underlying"]: row for row in rows}
        live_quotes = await self._latest_position_quote_map(list(runtime.positions.values()))
        for symbol, pos in list(runtime.positions.items()):
            row = row_map.get(pos.underlying)
            if not row:
                continue

            side = row.get("ce") if pos.option_type == "CE" else row.get("pe")
            candles = await self._load_candles(row, side or {}, interval=STRATEGY2_OPTION_TIMEFRAME, limit=96) if side else []
            closes = [float(c["close"]) for c in candles if c.get("close")] if candles else []
            live_observed_at: Optional[str] = None
            direct_quote = live_quotes.get(pos.symbol)
            if direct_quote:
                latest_close, live_observed_at = direct_quote
            else:
                live_ltp = float((side or {}).get("ltp") or 0.0)
                latest_close = closes[-1] if closes else live_ltp
            if latest_close <= 0:
                continue
            # No-arbitrage mark guard: a put can never be worth more than its
            # strike. Fyers serves zombie post-corporate-action strikes with
            # garbage LTPs (INDIANB 820 PE quoted 1298.8 on 2026-06-11 — the
            # active ladder had moved to ×××.75 adjusted strikes); one such
            # mark armed a phantom trailing stop and booked a ₹19L phantom
            # partial exit. The chain ingest now drops these rows at source;
            # this guard protects the book from any residual bad mark.
            if (
                pos.option_type == "PE"
                and float(pos.strike or 0) > 0
                and latest_close > float(pos.strike) * 1.02
            ):
                logger.warning(
                    f"[Strategy] rejecting no-arb PE mark {pos.symbol}: "
                    f"{latest_close} > strike {pos.strike}"
                )
                continue

            pos.current_price = latest_close
            pos.peak_price = max(pos.peak_price, latest_close)
            pos.price_updated_at = live_observed_at or started_at.isoformat()
            if len(closes) >= MACD_MIN_BARS:
                last_bar_time = str(candles[-1].get("time") or "") if candles else None
                macd_line, _, _ = _strategy_macd(
                    closes,
                    symbol=pos.instrument_key or pos.trading_symbol or pos.symbol,
                    timeframe=STRATEGY2_OPTION_TIMEFRAME,
                    last_bar_time=last_bar_time,
                )
                pos.macd_line = macd_line
                pos.latest_rsi = _round_or_none(latest_macd_rsi(closes).get("rsi"), 2)

            # Contexts are now composite-keyed (underlying:track) — pick
            # the row matching the position's expiry when possible, fall
            # back to any track for the underlying (MP+OF signal is
            # per-underlying so direction is identical across tracks).
            context = (
                contexts.get(pos.underlying)
                or next(
                    (v for k, v in contexts.items() if k.startswith(f"{pos.underlying}:")),
                    {},
                )
            )
            aligned_direction = context.get("direction")
            entered_at = _parse_iso_timestamp(pos.entered_at)
            return_pct = pos.return_pct

            if started_at.time() >= STRATEGY2_FORCE_EXIT or (entered_at and entered_at.date() < started_at.date()):
                await self._close_position(runtime, pos, latest_close, "intraday_close", qty=pos.qty)
                continue

            if return_pct <= -STRATEGY2_HARD_STOP_PCT:
                await self._close_position(runtime, pos, latest_close, "strategy2_hard_stop", qty=pos.qty)
                continue

            if return_pct >= STRATEGY2_TARGET_PCT:
                await self._close_position(runtime, pos, latest_close, "strategy2_target", qty=pos.qty)
                continue

            if aligned_direction and aligned_direction != pos.option_type:
                hold_seconds = None
                if entered_at is not None:
                    hold_seconds = (started_at - entered_at).total_seconds()
                if hold_seconds is None or hold_seconds >= STRATEGY2_MIN_HOLD_SECONDS:
                    await self._close_position(runtime, pos, latest_close, "mp_gate_flip", qty=pos.qty)
                    continue
                # Held too briefly — single-bar MP flips are noise. Skip the
                # close and re-check next cycle. Hard-stop / target / macd
                # reversal exits below still get evaluated.
                logger.debug(
                    "[S2] mp_gate_flip suppressed by min-hold: underlying={u} "
                    "option_type={ot} hold_s={hs:.0f} threshold_s={th}",
                    u=pos.underlying,
                    ot=pos.option_type,
                    hs=hold_seconds,
                    th=STRATEGY2_MIN_HOLD_SECONDS,
                )

            if pos.macd_line and len(pos.macd_line) >= 2:
                previous = pos.macd_line[-2]
                current = pos.macd_line[-1]
                if pos.option_type == "CE" and previous is not None and current is not None and previous >= 0 > current:
                    await self._close_position(runtime, pos, latest_close, "macd_reversal", qty=pos.qty)
                    continue
                if pos.option_type == "PE" and previous is not None and current is not None and previous <= 0 < current:
                    await self._close_position(runtime, pos, latest_close, "macd_reversal", qty=pos.qty)
                    continue

        latest_prices = {sym: pos.current_price for sym, pos in runtime.positions.items()}
        if latest_prices:
            runtime.portfolio.update_prices(latest_prices)

    async def _build_strategy2_signal_context(
        self,
        row: dict[str, Any],
        started_at: datetime,
    ) -> dict[str, Any]:
        underlying = row["underlying"]
        ce_side = row.get("ce")
        pe_side = row.get("pe")
        empty_signal = {
            "strategy": "Strategy 2",
            "source": "live_scan",
            "underlying": underlying,
            "signal_date": started_at.date().isoformat(),
            "trade_date": "live scan",
            "as_of": started_at.isoformat(),
            "direction": None,
            "reason": "data_pending",
            "strength": "standby",
            "status": "waiting",
            "freshness": "missing",
            "instruction": f"{underlying}: waiting for live 1-minute spot rows and 15-minute option candles.",
            **classify_status_bucket(
                has_position=self._has_underlying_position(self._strategy2, underlying),
                status="waiting",
            ),
        }
        empty_pipeline = {
            "name": f"Strategy 2 {underlying}",
            "status": "missing",
            "rows": 0,
            "last_date": "—",
            "detail": "No live data yet",
            "freshness": "missing",
        }
        if not ce_side or not pe_side:
            return {
                "direction": None,
                "signal": empty_signal,
                "pipeline": empty_pipeline,
                "can_enter": False,
            }

        ce_candles = await self._load_candles(row, ce_side, interval=STRATEGY2_OPTION_TIMEFRAME, limit=96)
        pe_candles = await self._load_candles(row, pe_side, interval=STRATEGY2_OPTION_TIMEFRAME, limit=96)
        ce_closes = [float(item["close"]) for item in ce_candles if item.get("close")] if ce_candles else []
        pe_closes = [float(item["close"]) for item in pe_candles if item.get("close")] if pe_candles else []
        option_last_bar_time = (
            (ce_candles[-1].get("time") if ce_candles else None)
            or (pe_candles[-1].get("time") if pe_candles else None)
        )
        ce_symbol = str(ce_side.get("instrument_key") or ce_side.get("trading_symbol") or f"{underlying}:CE")
        pe_symbol = str(pe_side.get("instrument_key") or pe_side.get("trading_symbol") or f"{underlying}:PE")
        ce_last_bar_time = str(ce_candles[-1].get("time") or "") if ce_candles else None
        pe_last_bar_time = str(pe_candles[-1].get("time") or "") if pe_candles else None

        ce_macd_line, _, _ = _strategy_macd(
            ce_closes,
            symbol=ce_symbol,
            timeframe=STRATEGY2_OPTION_TIMEFRAME,
            last_bar_time=ce_last_bar_time,
        ) if len(ce_closes) >= MACD_MIN_BARS else ([], [], [])
        pe_macd_line, _, _ = _strategy_macd(
            pe_closes,
            symbol=pe_symbol,
            timeframe=STRATEGY2_OPTION_TIMEFRAME,
            last_bar_time=pe_last_bar_time,
        ) if len(pe_closes) >= MACD_MIN_BARS else ([], [], [])
        ce_macd_value = ce_macd_line[-1] if ce_macd_line else None
        pe_macd_value = pe_macd_line[-1] if pe_macd_line else None
        fresh_ce, _, _ = detect_macd_zero_cross(
            ce_closes,
            "CE",
            symbol=ce_symbol,
            timeframe=STRATEGY2_OPTION_TIMEFRAME,
            last_bar_time=ce_last_bar_time,
        )
        fresh_pe, _, _ = detect_macd_zero_cross(
            pe_closes,
            "PE",
            symbol=pe_symbol,
            timeframe=STRATEGY2_OPTION_TIMEFRAME,
            last_bar_time=pe_last_bar_time,
        )
        ce_aligned = ce_macd_value is not None and ce_macd_value > 0
        pe_aligned = pe_macd_value is not None and pe_macd_value < 0

        # ── MP+OF directional override ──────────────────────────────────
        # Ported from the commodity desk: a 4-trigger Market-Profile +
        # Order-Flow engine running on 1-min index spot. When it emits a
        # BUY/SELL, we override the MACD-derived fresh_ce/fresh_pe so the
        # rest of the lane treats the MP+OF side as the entry trigger.
        # Falls through to MACD when the engine is silent (insufficient
        # 1m history, no prior session, etc.), so this is a soft promotion
        # not a wholesale rewire.
        mp_of_signal: dict[str, Any] = {}
        if settings.NSE_S2_USE_MP_OF_ENGINE:
            try:
                from paper_engine.strategy2_mp_of import evaluate_strategy2_mp_of

                mp_of_signal = await evaluate_strategy2_mp_of(
                    underlying=underlying,
                    started_at=started_at,
                )
                _mp_side = mp_of_signal.get("side")
                if _mp_side == "CE":
                    fresh_ce, fresh_pe = True, False
                elif _mp_side == "PE":
                    fresh_ce, fresh_pe = False, True
            except Exception as mp_exc:
                logger.debug(
                    f"[Strategy2] MP+OF evaluation failed for {underlying}: {mp_exc}; "
                    "falling back to MACD path."
                )
                mp_of_signal = {"signal": None, "reason": f"mp_of_error:{mp_exc}"}

        # ── Order-flow on option premium candles ────────────────────────
        # S2 trades option premium directly (long CE or long PE). CVD on
        # the chosen side's candles tells us whether premium accumulation
        # actually agrees with the MACD trigger. For a CE entry we want
        # CE CVD trending up; for a PE entry we want PE CVD trending up.
        # `orderflow_snapshot` returns recent values; `bar_cvd(...)[-6:]`
        # gives the 6-bar window used by the gate (about 90 minutes on 15m bars).
        ce_orderflow = orderflow_snapshot(ce_candles) if ce_candles else {}
        pe_orderflow = orderflow_snapshot(pe_candles) if pe_candles else {}
        ce_cvd_full = bar_cvd(ce_candles) if ce_candles else []
        pe_cvd_full = bar_cvd(pe_candles) if pe_candles else []
        ce_cvd_window = ce_cvd_full[-6:] if len(ce_cvd_full) >= 2 else []
        pe_cvd_window = pe_cvd_full[-6:] if len(pe_cvd_full) >= 2 else []

        # S2's MP gate. Defaults to False (gate enforced) — this lane is
        # transitioning to the MP+OF engine where the auction itself is
        # the signal, so the bypass path stays here only for emergency
        # rollback. Legacy global flag is honored as a fallback.
        if (
            settings.NSE_S2_BYPASS_MARKET_PROFILE_GATE
            or settings.NSE_STRATEGY_BYPASS_MARKET_PROFILE_GATE
        ):
            current_spot = float(row.get("spot_price") or 0.0)
            day_type = "bypassed"
            gate_reason = "market_profile_gate_bypassed"
            spot_source = "watchlist_live"
            spot_last_time = None
            session_date = started_at.date()

            def _stronger_direction() -> str:
                return "CE" if abs(float(ce_macd_value or 0.0)) >= abs(float(pe_macd_value or 0.0)) else "PE"

            direction = None
            entry_reason = None
            if fresh_ce and not fresh_pe:
                direction = "CE"
                status = "entry-ready"
                instruction = f"{underlying}: CE zero-cross confirmed while Market Profile gate is bypassed for test mode."
                can_enter = True
                entry_reason = "macd_zero_cross"
            elif fresh_pe and not fresh_ce:
                direction = "PE"
                status = "entry-ready"
                instruction = f"{underlying}: PE zero-cross confirmed while Market Profile gate is bypassed for test mode."
                can_enter = True
                entry_reason = "macd_zero_cross"
            elif fresh_ce and fresh_pe:
                direction = _stronger_direction()
                status = "entry-ready"
                instruction = (
                    f"{underlying}: Both option sides zero-crossed; using the stronger MACD while "
                    "Market Profile gate is bypassed for test mode."
                )
                can_enter = True
                entry_reason = "macd_zero_cross"
            elif ce_aligned and not pe_aligned:
                direction = "CE"
                status = "entry-ready"
                instruction = f"{underlying}: CE MACD stays above zero while Market Profile gate is bypassed for test mode."
                can_enter = True
                entry_reason = "macd_above_zero"
            elif pe_aligned and not ce_aligned:
                direction = "PE"
                status = "entry-ready"
                instruction = f"{underlying}: PE MACD stays aligned while Market Profile gate is bypassed for test mode."
                can_enter = True
                entry_reason = "macd_below_zero"
            elif ce_aligned and pe_aligned:
                direction = _stronger_direction()
                status = "entry-ready"
                instruction = (
                    f"{underlying}: Both option sides are aligned; using the stronger MACD while "
                    "Market Profile gate is bypassed for test mode."
                )
                can_enter = True
                entry_reason = "macd_above_zero" if direction == "CE" else "macd_below_zero"
            else:
                status = "waiting-cross"
                instruction = f"{underlying}: Market Profile gate is bypassed for test mode; waiting for MACD to align with zero."
                can_enter = False

            option_fresh = _parse_iso_timestamp(option_last_bar_time)
            freshness = "live"
            if not option_fresh:
                freshness = "missing"
            elif _strategy2_is_regular_session(started_at) and started_at - option_fresh > timedelta(minutes=20):
                freshness = "stale"
            can_enter = can_enter and freshness == "live"

            side_macd_val = ce_macd_value if direction == "CE" else pe_macd_value if direction == "PE" else None
            side_closes_live = ce_closes if direction == "CE" else pe_closes if direction == "PE" else []
            latest_hist_val: Optional[float] = None
            prev_hist_val: Optional[float] = None
            try:
                if len(side_closes_live) >= MACD_MIN_BARS:
                    side_symbol = ce_symbol if direction == "CE" else pe_symbol
                    side_last_bar_time = ce_last_bar_time if direction == "CE" else pe_last_bar_time
                    _, _, side_hist = _strategy_macd(
                        side_closes_live,
                        symbol=side_symbol,
                        timeframe=STRATEGY2_OPTION_TIMEFRAME,
                        last_bar_time=side_last_bar_time,
                    )
                    if side_hist:
                        latest_hist_val = side_hist[-1]
                        if len(side_hist) >= 2:
                            prev_hist_val = side_hist[-2]
            except Exception:
                pass
            bucket_info = classify_signal_bucket(
                has_position=self._has_underlying_position(self._strategy2, underlying),
                signal_validation="ready" if status == "entry-ready" else status,
                macd=side_macd_val,
                macd_histogram=latest_hist_val,
                prev_macd_histogram=prev_hist_val,
                recent_cross_signal=("BUY" if direction == "CE" and fresh_ce else "SELL" if direction == "PE" and fresh_pe else None),
                recent_cross_bars_ago=0 if (fresh_ce or fresh_pe) else None,
            )

            # Pick the active-side orderflow snapshot for surface fields.
            active_of = ce_orderflow if direction == "CE" else pe_orderflow if direction == "PE" else {}
            active_cvd_window = ce_cvd_window if direction == "CE" else pe_cvd_window if direction == "PE" else []
            cvd_agrees = (
                cvd_agrees_with("BUY", active_cvd_window) if direction in {"CE", "PE"} else None
            )

            # If MP+OF supplied the direction, replace the MACD-shaped audit
            # strings so logs, signal records, and the UI surface the real
            # trigger. Falls through transparently when MP+OF was silent
            # (the bypass branch's MACD reason stays in place).
            mp_of_active = bool(mp_of_signal.get("signal")) and direction in {"CE", "PE"}
            if mp_of_active:
                entry_reason = f"mp_of:{mp_of_signal.get('entry_style') or 'auto'}"
                gate_reason = mp_of_signal.get("reason") or gate_reason
                instruction = (
                    f"{underlying}: MP+OF {mp_of_signal.get('entry_style', 'signal')} "
                    f"→ {direction} (confidence "
                    f"{int(round(float(mp_of_signal.get('confidence') or 0.0) * 100))}%). "
                    f"{mp_of_signal.get('signal_validation_detail', '')}"
                ).strip()
            signal = {
                "strategy": "Strategy 2",
                "source": "live_scan",
                "underlying": underlying,
                "signal_date": started_at.date().isoformat(),
                "trade_date": "live scan",
                "as_of": started_at.isoformat(),
                "direction": direction,
                "reason": gate_reason,
                "entry_reason": entry_reason,
                "strength": "strong" if status == "entry-ready" else "monitoring",
                "status": status,
                "freshness": freshness,
                "instruction": instruction,
                "mp_day_type": day_type,
                "spot_price": _round_or_none(current_spot, 2),
                "poc": None,
                "vah": None,
                "val": None,
                "ce_macd": _round_or_none(ce_macd_value, 4),
                "pe_macd": _round_or_none(pe_macd_value, 4),
                "option_last_bar_time": option_last_bar_time,
                "spot_last_time": spot_last_time,
                "spot_source": spot_source,
                "spot_session_date": session_date.isoformat(),
                "ce_cvd_session": _round_or_none(ce_orderflow.get("cvd_anchored_latest"), 0),
                "pe_cvd_session": _round_or_none(pe_orderflow.get("cvd_anchored_latest"), 0),
                "ce_vwap": _round_or_none(ce_orderflow.get("vwap_latest"), 2),
                "pe_vwap": _round_or_none(pe_orderflow.get("vwap_latest"), 2),
                "cvd_window_delta": (
                    _round_or_none(active_cvd_window[-1] - active_cvd_window[0], 0)
                    if len(active_cvd_window) >= 2 else None
                ),
                "cvd_agrees": cvd_agrees,
                "cvd_divergence": active_of.get("divergence"),
                # MP+OF surface fields — populated only when the engine fired.
                # The dashboard reads these to render the entry style + the
                # auction context the trigger fired on.
                "mp_of_engine_signal": mp_of_signal.get("signal") if mp_of_signal else None,
                "mp_of_entry_style": mp_of_signal.get("entry_style") if mp_of_signal else None,
                "mp_of_confidence": _round_or_none(mp_of_signal.get("confidence"), 3) if mp_of_signal else None,
                "mp_of_stop_hint": _round_or_none(mp_of_signal.get("stop_hint"), 2) if mp_of_signal else None,
                "mp_of_validation": mp_of_signal.get("signal_validation_detail") if mp_of_signal else None,
                "mp_of_day_type": mp_of_signal.get("mp_day_type") if mp_of_signal else None,
                "mp_of_periods": mp_of_signal.get("mp_periods") if mp_of_signal else None,
                **bucket_info,
            }
            # Pipeline detail: when MP+OF actively shaped the decision we
            # surface its trigger style instead of the legacy "MP bypassed"
            # marker. Falls back to the test-mode string only when MP+OF
            # didn't fire (so the watcher sees the right state).
            if mp_of_active:
                pipeline_detail = (
                    f"mp+of {mp_of_signal.get('entry_style', 'signal')} → {direction} · {status}"
                )
            else:
                pipeline_detail = f"mp+of standby · {status}"
            pipeline = {
                "name": f"Strategy 2 {underlying}",
                "status": "ok" if freshness == "live" else ("warning" if freshness == "stale" else "missing"),
                "rows": max(len(ce_candles), len(pe_candles)),
                "last_date": str(option_last_bar_time or "—"),
                "detail": pipeline_detail,
                "freshness": freshness,
            }
            return {
                "direction": direction,
                "day_type": day_type,
                "gate_reason": gate_reason,
                "entry_reason": entry_reason,
                "signal": signal,
                "pipeline": pipeline,
                "can_enter": can_enter,
                "option_last_bar_time": option_last_bar_time,
                "spot_last_time": spot_last_time,
                "ce_closes": ce_closes,
                "pe_closes": pe_closes,
                "ce_candles": ce_candles,
                "ce_cvd_window": ce_cvd_window,
                "pe_cvd_window": pe_cvd_window,
                "ce_orderflow": ce_orderflow,
                "pe_orderflow": pe_orderflow,
                "pe_candles": pe_candles,
                "ce_macd_line": ce_macd_line,
                "pe_macd_line": pe_macd_line,
                "ce_macd_value": ce_macd_value,
                "pe_macd_value": pe_macd_value,
            }

        spot_rows, spot_source = await self._load_strategy2_spot_rows(underlying, started_at)
        session_rows, session_date = _latest_populated_session_rows(spot_rows)
        spot_last_time = session_rows[-1].get("time") if session_rows else None
        expected_session_date = _strategy2_expected_session_date(started_at)
        using_latest_session = session_date is not None and session_date != expected_session_date
        session_label = session_date.isoformat() if session_date else "latest available session"

        if len(session_rows) < 30:
            signal = {
                **empty_signal,
                "reason": "spot_warmup",
                "freshness": "stale" if session_rows else "missing",
                "instruction": (
                    f"{underlying}: {len(session_rows)} spot bars loaded for {session_label}. "
                    "Waiting for intraday Market Profile warm-up."
                ),
            }
            pipeline = {
                "name": f"Strategy 2 {underlying}",
                "status": "warning" if session_rows else "missing",
                "rows": len(session_rows),
                "last_date": str(spot_last_time or "—"),
                "detail": f"{spot_source} spot rows warming up ({session_label})",
                "freshness": "stale" if session_rows else "missing",
            }
            return {
                "direction": None,
                "signal": signal,
                "pipeline": pipeline,
                "can_enter": False,
            }

        profile = market_profile_builder.build_profile_from_rows(
            underlying,
            session_rows,
            "day",
            "1minute",
        )
        if not profile:
            return {
                "direction": None,
                "signal": empty_signal,
                "pipeline": empty_pipeline,
                "can_enter": False,
            }

        current_spot = float(session_rows[-1].get("close") or row.get("spot_price") or 0.0)
        direction, day_type, gate_reason = self._classify_strategy2_market_profile(
            profile=profile,
            current_spot=current_spot,
            today_rows=session_rows,
        )

        entry_reason = None
        if direction == "CE":
            if fresh_ce:
                status = "entry-ready"
                instruction = f"{underlying}: CE zero-cross confirmed with MP {day_type} gate above POC {profile.poc:.0f}."
                can_enter = True
                entry_reason = "macd_zero_cross"
            elif ce_aligned:
                status = "entry-ready"
                instruction = f"{underlying}: MP gate is bullish ({day_type}) and CE MACD is above zero."
                can_enter = True
                entry_reason = "macd_above_zero"
            else:
                status = "waiting-cross"
                instruction = f"{underlying}: MP gate is bullish ({day_type}) but CE MACD is not above zero yet."
                can_enter = False
        elif direction == "PE":
            if fresh_pe:
                status = "entry-ready"
                instruction = f"{underlying}: PE zero-cross confirmed with MP {day_type} gate below POC {profile.poc:.0f}."
                can_enter = True
                entry_reason = "macd_zero_cross"
            elif pe_aligned:
                status = "entry-ready"
                instruction = f"{underlying}: MP gate is bearish ({day_type}) and PE MACD is below zero."
                can_enter = True
                entry_reason = "macd_below_zero"
            else:
                status = "waiting-cross"
                instruction = f"{underlying}: MP gate is bearish ({day_type}) but PE MACD is not below zero yet."
                can_enter = False
        else:
            status = "standby"
            instruction = f"{underlying}: Market Profile is balanced around POC {profile.poc:.0f}. No directional gate yet."
            can_enter = False

        spot_fresh = _parse_iso_timestamp(spot_last_time)
        option_fresh = _parse_iso_timestamp(option_last_bar_time)
        freshness = "live"
        if not spot_fresh or not option_fresh:
            freshness = "missing"
        elif using_latest_session or (
            _strategy2_is_regular_session(started_at)
            and (
                started_at - spot_fresh > timedelta(minutes=10)
                or started_at - option_fresh > timedelta(minutes=20)
            )
        ):
            freshness = "stale"
        can_enter = can_enter and freshness == "live"

        side_macd_val = ce_macd_value if direction == "CE" else pe_macd_value if direction == "PE" else None
        side_closes_live = ce_closes if direction == "CE" else pe_closes if direction == "PE" else []
        latest_hist_val: Optional[float] = None
        prev_hist_val: Optional[float] = None
        try:
            if len(side_closes_live) >= MACD_MIN_BARS:
                side_symbol = ce_symbol if direction == "CE" else pe_symbol
                side_last_bar_time = ce_last_bar_time if direction == "CE" else pe_last_bar_time
                _, _, side_hist = _strategy_macd(
                    side_closes_live,
                    symbol=side_symbol,
                    timeframe=STRATEGY2_OPTION_TIMEFRAME,
                    last_bar_time=side_last_bar_time,
                )
                if side_hist:
                    latest_hist_val = side_hist[-1]
                    if len(side_hist) >= 2:
                        prev_hist_val = side_hist[-2]
        except Exception:
            pass
        bucket_info = classify_signal_bucket(
            has_position=self._has_underlying_position(self._strategy2, underlying),
            signal_validation="ready" if status == "entry-ready" else status,
            macd=side_macd_val,
            macd_histogram=latest_hist_val,
            prev_macd_histogram=prev_hist_val,
            recent_cross_signal=("BUY" if direction == "CE" and fresh_ce else "SELL" if direction == "PE" and fresh_pe else None),
            recent_cross_bars_ago=0 if (fresh_ce or fresh_pe) else None,
        )

        active_of_full = ce_orderflow if direction == "CE" else pe_orderflow if direction == "PE" else {}
        active_cvd_window_full = ce_cvd_window if direction == "CE" else pe_cvd_window if direction == "PE" else []
        cvd_agrees_full = (
            cvd_agrees_with("BUY", active_cvd_window_full) if direction in {"CE", "PE"} else None
        )
        signal = {
            "strategy": "Strategy 2",
            "source": "live_scan",
            "underlying": underlying,
            "signal_date": started_at.date().isoformat(),
            "trade_date": "live scan",
            "as_of": started_at.isoformat(),
            "direction": direction,
            "reason": gate_reason,
            "entry_reason": entry_reason,
            "strength": "strong" if status == "entry-ready" else "monitoring",
            "status": status,
            "freshness": freshness,
            "instruction": instruction,
            "mp_day_type": day_type,
            "spot_price": _round_or_none(current_spot, 2),
            "poc": _round_or_none(profile.poc, 2),
            "vah": _round_or_none(profile.vah, 2),
            "val": _round_or_none(profile.val, 2),
            "ce_macd": _round_or_none(ce_macd_value, 4),
            "pe_macd": _round_or_none(pe_macd_value, 4),
            "option_last_bar_time": option_last_bar_time,
            "spot_last_time": spot_last_time,
            "spot_source": spot_source,
            "spot_session_date": session_date.isoformat() if session_date else None,
            "ce_cvd_session": _round_or_none(ce_orderflow.get("cvd_anchored_latest"), 0),
            "pe_cvd_session": _round_or_none(pe_orderflow.get("cvd_anchored_latest"), 0),
            "ce_vwap": _round_or_none(ce_orderflow.get("vwap_latest"), 2),
            "pe_vwap": _round_or_none(pe_orderflow.get("vwap_latest"), 2),
            "cvd_window_delta": (
                _round_or_none(active_cvd_window_full[-1] - active_cvd_window_full[0], 0)
                if len(active_cvd_window_full) >= 2 else None
            ),
            "cvd_agrees": cvd_agrees_full,
            "cvd_divergence": active_of_full.get("divergence"),
            **bucket_info,
        }
        pipeline = {
            "name": f"Strategy 2 {underlying}",
            "status": "ok" if freshness == "live" else ("warning" if freshness == "stale" else "missing"),
            "rows": len(session_rows),
            "last_date": str(option_last_bar_time or spot_last_time or "—"),
            "detail": (
                f"{spot_source} spot · session {session_label} · MP {day_type} · {status}"
            ),
            "freshness": freshness,
        }
        return {
            "direction": direction,
            "day_type": day_type,
            "gate_reason": gate_reason,
            "entry_reason": entry_reason,
            "signal": signal,
            "pipeline": pipeline,
            "can_enter": can_enter,
            "option_last_bar_time": option_last_bar_time,
            "spot_last_time": spot_last_time,
            "ce_closes": ce_closes,
            "pe_closes": pe_closes,
            "ce_candles": ce_candles,
            "pe_candles": pe_candles,
            "ce_cvd_window": ce_cvd_window,
            "pe_cvd_window": pe_cvd_window,
            "ce_orderflow": ce_orderflow,
            "pe_orderflow": pe_orderflow,
            "ce_macd_line": ce_macd_line,
            "pe_macd_line": pe_macd_line,
            "ce_macd_value": ce_macd_value,
            "pe_macd_value": pe_macd_value,
        }

    async def _load_strategy2_spot_rows(
        self,
        underlying: str,
        started_at: datetime,
    ) -> tuple[list[dict[str, Any]], str]:
        cached = self._strategy2_spot_cache.get(underlying)
        if cached and (started_at - cached[0]).total_seconds() <= STRATEGY2_SPOT_CACHE_TTL_SECONDS:
            return cached[1], cached[2]

        from_date = started_at.date() - timedelta(days=5)
        rows: list[dict[str, Any]] = []
        source = "none"
        local_only_mode = settings.MARKET_INTELLIGENCE_STRATEGY_LOCAL_ONLY or settings.PAPER_TRADING_ONLY

        if local_only_mode:
            rows, source, _ = await market_intelligence_runtime.load_local_spot_rows(
                underlying,
                lookback_days=5,
            )
        else:
            to_date = started_at.date()
            fyers_adapter = get_active_adapter("fyers")
            if fyers_adapter is None and await ensure_fyers_session(force_validate=True):
                fyers_adapter = get_active_adapter("fyers")
            get_history = getattr(fyers_adapter, "get_historical_candles", None) if fyers_adapter else None
            fyers_symbol = STRATEGY2_FYERS_SYMBOLS.get(underlying)
            if fyers_symbol and callable(get_history):
                try:
                    rows = await get_history(
                        fyers_symbol,
                        "1",
                        from_date.isoformat(),
                        to_date.isoformat(),
                    )
                    if rows:
                        source = "fyers"
                except Exception as exc:
                    logger.debug(f"[Strategy2] Fyers spot history failed for {underlying}: {exc}")

            if not rows:
                spot_key = self._INDEX_SPOT_KEYS.get(underlying)
                if spot_key:
                    rows = await option_history_service._fetch_broker_candles(
                        instrument_key=spot_key,
                        from_date=from_date,
                        to_date=to_date,
                        interval="1minute",
                    )
                    if rows:
                        source = "upstox"

        if rows:
            self._strategy2_spot_cache[underlying] = (started_at, rows, source)
        return rows, source

    def _classify_strategy2_market_profile(
        self,
        *,
        profile: Any,
        current_spot: float,
        today_rows: list[dict[str, Any]],
    ) -> tuple[Optional[str], str, str]:
        recent_move = 0.0
        if len(today_rows) >= 15:
            try:
                recent_move = current_spot - float(today_rows[-15].get("close") or current_spot)
            except Exception:
                recent_move = 0.0

        if current_spot >= profile.vah and current_spot >= profile.ib_high and recent_move >= 0:
            return "CE", "trend_up", "mp_trend_up"
        if current_spot <= profile.val and current_spot <= profile.ib_low and recent_move <= 0:
            return "PE", "trend_down", "mp_trend_down"
        if profile.poor_low and current_spot >= profile.poc:
            return "CE", "failed_auction_low", "poor_low_recovery"
        if profile.poor_high and current_spot <= profile.poc:
            return "PE", "failed_auction_high", "poor_high_reversal"
        if current_spot > profile.poc and recent_move >= 0:
            return "CE", "balance_above_poc", "holding_above_poc"
        if current_spot < profile.poc and recent_move <= 0:
            return "PE", "balance_below_poc", "holding_below_poc"
        return None, "balance", "mp_balanced"

    # ── Spot Candle Sync ───────────────────────────────────────────────────

    SPOT_SYNC_INTERVAL_SCANS = 30   # every ~30 min at 60s scan interval
    SPOT_SYNC_LOOKBACK_DAYS = 10    # fetch 10 days to cover weekends + gaps

    # Upstox instrument keys for index spot prices
    _INDEX_SPOT_KEYS: dict[str, str] = {
        "NIFTY": "NSE_INDEX|Nifty 50",
        "BANKNIFTY": "NSE_INDEX|Nifty Bank",
        "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
        "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
        "NIFTYNXT50": "NSE_INDEX|Nifty Next 50",
        "SENSEX": "BSE_INDEX|SENSEX",
        "BANKEX": "BSE_INDEX|BSE-BANKEX",
    }

    async def _maybe_sync_spot_candles(self) -> None:
        """Periodically sync spot candles from broker to DB during market hours."""
        if settings.MARKET_INTELLIGENCE_STRATEGY_LOCAL_ONLY or settings.PAPER_TRADING_ONLY:
            return
        if not settings.STRATEGY_SPOT_SYNC_ENABLED:
            return
        self._scan_count += 1
        if self._scan_count % self.SPOT_SYNC_INTERVAL_SCANS != 1:
            return  # first scan + every 30th scan
        try:
            await self._sync_spot_candles()
            self._last_spot_sync = _now_ist()
            logger.info("[Strategy] Spot candle sync completed")
        except Exception as exc:
            logger.warning(f"[Strategy] Spot candle sync failed: {exc}")

    async def _sync_spot_candles(self) -> int:
        """Fetch recent 30-min spot candles from Upstox and upsert into DB."""
        from api.routers.auth import get_broker_token, ensure_upstox_session
        from urllib.parse import quote

        token = get_broker_token("upstox")
        if not token:
            await ensure_upstox_session()
            token = get_broker_token("upstox")
        if not token:
            logger.debug("[Strategy] No Upstox token for spot sync — skipped")
            return 0

        today = date.today()
        from_date = today - timedelta(days=self.SPOT_SYNC_LOOKBACK_DAYS)

        # Collect underlyings: all indices + any stocks with open positions
        targets: dict[str, str] = dict(self._INDEX_SPOT_KEYS)

        # Also sync spot for stocks with open strategy positions
        for runtime in self._runtimes():
            for pos in runtime.positions.values():
                if pos.underlying not in targets:
                    key = await self._resolve_spot_instrument_key(pos.underlying)
                    if key:
                        targets[pos.underlying] = key

        total_stored = 0
        for underlying, instrument_key in targets.items():
            try:
                candles = await self._fetch_spot_candles_upstox(
                    instrument_key, from_date, today, token
                )
                if not candles:
                    continue
                stored = await self._upsert_spot_candles(underlying, instrument_key, candles)
                total_stored += stored
            except Exception as exc:
                logger.debug(f"[Strategy] Spot sync failed for {underlying}: {exc}")
            # Small delay to respect rate limits
            await asyncio.sleep(0.5)

        return total_stored

    async def _fetch_spot_candles_upstox(
        self,
        instrument_key: str,
        from_date: date,
        to_date: date,
        token: str,
    ) -> list[dict]:
        """Fetch 30-min candles from Upstox historical API."""
        from urllib.parse import quote
        encoded_key = quote(instrument_key, safe="")
        url = (
            f"https://api.upstox.com/v2/historical-candle/"
            f"{encoded_key}/30minute/{to_date.isoformat()}/{from_date.isoformat()}"
        )
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )
        if resp.status_code != 200:
            logger.debug(f"[Strategy] Upstox spot API {resp.status_code} for {instrument_key}")
            return []

        rows: list[dict] = []
        for candle in reversed(resp.json().get("data", {}).get("candles", [])):
            if not candle or len(candle) < 6:
                continue
            rows.append({
                "time": str(candle[0]),
                "open": float(candle[1]),
                "high": float(candle[2]),
                "low": float(candle[3]),
                "close": float(candle[4]),
                "volume": int(candle[5] or 0),
                "oi": 0,
            })
        return rows

    async def _upsert_spot_candles(
        self,
        underlying: str,
        instrument_key: str,
        candles: list[dict],
    ) -> int:
        """Upsert spot candles into underlying_spot_candles hypertable."""
        from db.database import AsyncSessionLocal

        def _parse_ts(value: str) -> datetime:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

        payload = [
            {
                "time": _parse_ts(c["time"]),
                "instrument_key": instrument_key,
                "underlying": underlying,
                "interval": "30minute",
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": int(c.get("volume", 0)),
                "oi": int(c.get("oi", 0)),
                "source": "strategy_agent",
            }
            for c in candles
        ]
        if not payload:
            return 0

        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO underlying_spot_candles (
                        time, instrument_key, underlying, interval, open, high,
                        low, close, volume, oi, source, synced_at
                    )
                    VALUES (
                        :time, :instrument_key, :underlying, :interval, :open, :high,
                        :low, :close, :volume, :oi, :source, NOW()
                    )
                    ON CONFLICT (instrument_key, interval, time) DO UPDATE
                    SET open = EXCLUDED.open,
                        high = EXCLUDED.high,
                        low = EXCLUDED.low,
                        close = EXCLUDED.close,
                        volume = EXCLUDED.volume,
                        oi = EXCLUDED.oi,
                        source = EXCLUDED.source,
                        synced_at = NOW()
                """),
                payload,
            )
            await session.commit()

        logger.debug(f"[Strategy] Upserted {len(payload)} spot candles for {underlying}")
        return len(payload)

    async def _resolve_spot_instrument_key(self, underlying: str) -> Optional[str]:
        """Look up spot_instrument_key from fo_underlying_catalog for a stock."""
        try:
            from db.database import AsyncSessionLocal
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text("""
                        SELECT spot_instrument_key
                        FROM fo_underlying_catalog
                        WHERE symbol = :symbol AND spot_instrument_key IS NOT NULL
                        LIMIT 1
                    """),
                    {"symbol": underlying},
                )
                row = result.fetchone()
                return row.spot_instrument_key if row else None
        except Exception:
            return None

    async def _compute_spot_context(self, underlying: str, window: dict) -> dict:
        """Load spot candles and classify the MA setup."""
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text(
                        """
                        SELECT close FROM underlying_spot_candles
                        WHERE underlying = :underlying AND interval = '30minute'
                          AND time::date BETWEEN (CAST(:window_start AS date) - INTERVAL '60 days')::date AND CAST(:window_end AS date)
                        ORDER BY time
                        """
                    ),
                    {
                        "underlying": underlying,
                        "window_start": window["window_start"],
                        "window_end": window["window_end"],
                    },
                )
                rows = result.fetchall()

            if len(rows) < SPOT_MA_SLOW + 10:
                return {"setup": "unknown"}

            spot_closes = [float(row.close) for row in rows]
            return compute_spot_ma_context(spot_closes, SPOT_MA_FAST, SPOT_MA_SLOW)
        except Exception as exc:
            logger.debug(f"[Strategy] Spot context failed for {underlying}: {exc}")
            return {"setup": "unknown"}

    def _has_underlying_position(self, runtime: StrategyRuntime, underlying: str) -> bool:
        return any(p.underlying == underlying for p in runtime.positions.values())

    def _has_underlying_expiry_position(
        self,
        runtime: StrategyRuntime,
        underlying: str,
        expiry: str,
    ) -> bool:
        """Stronger uniqueness check used by S2 for the weekly/monthly matrix.

        Lets the lane open BOTH the weekly and monthly ATM legs on a single
        signal — `_has_underlying_position` would skip the second leg because
        it sees the first leg as "already in" for the underlying.
        """
        expiry_norm = str(expiry or "").strip()
        return any(
            p.underlying == underlying and str(p.expiry or "").strip() == expiry_norm
            for p in runtime.positions.values()
        )

    @staticmethod
    def _paper_session_uuid(runtime: StrategyRuntime) -> str:
        import uuid

        return str(uuid.uuid5(uuid.NAMESPACE_DNS, runtime.portfolio.session_id or runtime.key))

    async def _ensure_paper_session_record(self, session: Any, runtime: StrategyRuntime) -> str:
        session_id = self._paper_session_uuid(runtime)
        await session.execute(
            text(
                """
                INSERT INTO paper_sessions (
                    id, name, broker, initial_capital, current_capital, is_active
                ) VALUES (
                    :id, :name, 'paper', :initial_capital, :current_capital, true
                )
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    broker = EXCLUDED.broker,
                    current_capital = EXCLUDED.current_capital,
                    is_active = true
                """
            ),
            {
                "id": session_id,
                "name": runtime.label,
                "initial_capital": runtime.portfolio.initial_capital,
                "current_capital": runtime.portfolio.total_equity,
            },
        )
        return session_id

    # ── DB Persistence ────────────────────────────────────────────────────

    async def _persist_macd_signal(
        self,
        underlying: str,
        expiry: str,
        strike: float,
        option_type: str,
        macd_value: float,
        signal_value: Optional[float],
        histogram: Optional[float],
        signal_type: str,
        premium_at_signal: float,
    ) -> None:
        """Write a MACD signal to the macd_signals hypertable for historical analysis."""
        try:
            from db.database import AsyncSessionLocal
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("""
                        INSERT INTO macd_signals
                            (time, underlying, market, expiry, strike, option_type,
                             macd_value, signal_value, histogram, signal_type, premium_at_signal)
                        VALUES
                            (now(), :underlying, 'NSE', CAST(:expiry AS DATE), :strike, :option_type,
                             :macd_value, :signal_value, :histogram, :signal_type, :premium_at_signal)
                    """),
                    {
                        "underlying": underlying,
                        "expiry": _coerce_date(expiry),
                        "strike": strike,
                        "option_type": option_type,
                        "macd_value": macd_value,
                        "signal_value": signal_value,
                        "histogram": histogram,
                        "signal_type": signal_type,
                        "premium_at_signal": premium_at_signal,
                    },
                )
                await session.commit()
            logger.debug(f"[Strategy] Persisted MACD signal: {underlying} {option_type} {signal_type}")
        except Exception as exc:
            logger.warning(f"[Strategy] Failed to persist MACD signal: {exc}")

    async def _persist_order(
        self,
        runtime: StrategyRuntime,
        symbol: str,
        action: str,
        qty: int,
        price: float,
        expiry: str,
        strike: float,
        option_type: str,
        reason: str,
    ) -> None:
        """Write an order record to the orders table for audit trail."""
        try:
            from db.database import AsyncSessionLocal
            import uuid
            async with AsyncSessionLocal() as session:
                session_id = await self._ensure_paper_session_record(session, runtime)
                await session.execute(
                    text("""
                        INSERT INTO orders
                            (id, session_id, mode, broker, symbol, exchange,
                             instrument_type, strike, expiry, option_type,
                             action, order_type, qty, price, status,
                             fill_price, fill_time, created_at)
                        VALUES
                            (:id, :session_id, 'paper', 'paper', :symbol, 'NSE',
                             :instrument_type, :strike, :expiry, :option_type,
                             :action, 'MARKET', :qty, :price, 'FILLED',
                             :price, now(), now())
                    """),
                    {
                        "id": str(uuid.uuid4()),
                        "session_id": session_id,
                        "symbol": symbol,
                        "instrument_type": option_type if option_type in {"CE", "PE"} else "EQ",
                        "option_type": option_type,
                        "strike": strike,
                        "expiry": str(_coerce_date(expiry) or expiry or ""),
                        "action": action,
                        "qty": qty,
                        "price": price,
                    },
                )
                await session.commit()
        except Exception as exc:
            logger.warning(f"[Strategy] Failed to persist order: {exc}")

    async def _ensure_open_position_order_records(self, runtime: StrategyRuntime) -> None:
        """Backfill missing BUY order audit rows for restored/open positions."""
        if not runtime.positions:
            return
        try:
            from db.database import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                session_id = await self._ensure_paper_session_record(session, runtime)
                for position in runtime.positions.values():
                    result = await session.execute(
                        text(
                            """
                            SELECT id
                            FROM orders
                            WHERE session_id = :session_id
                              AND symbol = :symbol
                              AND action = 'BUY'
                            LIMIT 1
                            """
                        ),
                        {
                            "session_id": session_id,
                            "symbol": position.symbol,
                        },
                    )
                    if result.first():
                        continue
                    await session.execute(
                        text(
                            """
                            INSERT INTO orders
                                (id, session_id, mode, broker, symbol, exchange,
                                 instrument_type, strike, expiry, option_type,
                                 action, order_type, qty, price, status,
                                 fill_price, fill_time, created_at)
                            VALUES
                                (:id, :session_id, 'paper', 'paper', :symbol, 'NSE',
                                 :instrument_type, :strike, :expiry, :option_type,
                                 'BUY', 'MARKET', :qty, :price, 'FILLED',
                                 :price, :fill_time, :created_at)
                            ON CONFLICT (id) DO NOTHING
                            """
                        ),
                        {
                            "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{runtime.key}:entry-order:{position.symbol}")),
                            "session_id": session_id,
                            "symbol": position.symbol,
                            "instrument_type": position.option_type if position.option_type in {"CE", "PE"} else "EQ",
                            "strike": position.strike,
                            "expiry": str(_coerce_date(position.expiry) or position.expiry or ""),
                            "option_type": position.option_type,
                            "qty": position.initial_qty or position.qty,
                            "price": position.entry_price,
                            "fill_time": _parse_iso_timestamp(position.entered_at) or _now_ist(),
                            "created_at": _parse_iso_timestamp(position.entered_at) or _now_ist(),
                        },
                    )
                await session.commit()
        except Exception as exc:
            logger.warning(f"[Strategy] Failed to reconcile open position order records: {exc}")

    async def _persist_position(
        self,
        runtime: StrategyRuntime,
        pos: StrategyPosition,
        realized_pnl: float = 0.0,
    ) -> None:
        """Upsert a position record to the positions table."""
        try:
            from db.database import AsyncSessionLocal
            async with AsyncSessionLocal() as session:
                session_id = await self._ensure_paper_session_record(session, runtime)
                await session.execute(
                    text("""
                        INSERT INTO positions
                            (id, session_id, mode, broker, symbol, strike,
                             expiry, option_type, qty, avg_price, realized_pnl, created_at)
                        VALUES
                            (:id, :session_id, 'paper', 'paper', :symbol, :strike,
                             :expiry, :option_type, :qty, :avg_price, :realized_pnl, now())
                        ON CONFLICT (id) DO UPDATE SET
                            qty = EXCLUDED.qty,
                            realized_pnl = EXCLUDED.realized_pnl
                    """),
                    {
                        "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{runtime.key}:{pos.symbol}")),
                        "session_id": session_id,
                        "symbol": pos.symbol,
                        "strike": pos.strike,
                        "expiry": str(_coerce_date(pos.expiry) or pos.expiry or ""),
                        "option_type": pos.option_type,
                        "qty": pos.qty,
                        "avg_price": pos.entry_price,
                        "realized_pnl": realized_pnl,
                    },
                )
                await session.commit()
        except Exception as exc:
            logger.warning(f"[Strategy] Failed to persist position: {exc}")

    async def _persist_agent_signal_observation(
        self,
        runtime: StrategyRuntime,
        signal: dict[str, Any],
        *,
        status: Optional[str] = None,
        row: Optional[dict[str, Any]] = None,
    ) -> None:
        """Persist a strategy observation even when it does not become a position."""
        try:
            underlying = str(signal.get("underlying") or (row or {}).get("underlying") or "").upper().strip()
            if not underlying:
                return
            option_type = str(signal.get("direction") or signal.get("option_type") or "").upper().strip() or None
            if option_type not in {"CE", "PE"}:
                option_type = None
            expiry = signal.get("expiry") or (row or {}).get("expiry")
            expiry_date = _coerce_date(expiry)
            strike = signal.get("strike") or signal.get("atm_strike") or (row or {}).get("atm_strike")
            try:
                strike_value = float(strike) if strike not in {None, ""} else None
            except (TypeError, ValueError):
                strike_value = None

            signal_date = str(signal.get("signal_date") or _now_ist().date().isoformat())
            source = str(signal.get("source") or "observation")
            signal_key = str(
                signal.get("signal_key")
                or f"{runtime.key}:{source}:{underlying}:{option_type or 'NA'}:{expiry or 'NA'}:{signal_date}"
            )
            symbol = str(signal.get("symbol") or "").strip()
            if not symbol:
                symbol = (
                    self._contract_symbol(underlying, str(expiry), strike_value, option_type)
                    if expiry_date and strike_value is not None and option_type
                    else f"{runtime.key}:{underlying}:{option_type or 'NA'}"
                )

            raw_status = str(status or signal.get("status") or "observed").lower().strip()
            status_value = {
                "entry-ready": "candidate",
                "conditions_met": "candidate",
                "active": "candidate",
                "trend-aligned": "watching",
                "watching": "watching",
                "waiting-cross": "watching",
                "standby": "observed",
                "avoid": "blocked",
                "blocked": "blocked",
            }.get(raw_status, raw_status or "observed")

            def _flt(value: Any) -> Optional[float]:
                try:
                    if value is None or value == "":
                        return None
                    return float(value)
                except (TypeError, ValueError):
                    return None

            strength = _flt(signal.get("priority_score"))
            if strength is None:
                strength = _flt(signal.get("strength"))
            signal_bar_time = (
                _parse_iso_timestamp(str(signal.get("option_last_bar_time") or ""))
                or _parse_iso_timestamp(str(signal.get("spot_last_time") or ""))
                or _parse_iso_timestamp(str(signal.get("as_of") or ""))
            )
            iv_pct = _flt(signal.get("iv_pct") or signal.get("entry_iv_pct"))
            if iv_pct is not None and iv_pct <= 1.0:
                iv_pct *= 100.0

            async with AsyncSessionLocal() as session:
                session_id = await self._ensure_paper_session_record(session, runtime)
                await session.execute(
                    text(
                        """
                        INSERT INTO agent_signals (
                            id, session_id, market, strategy_key, strategy_label, signal_key,
                            symbol, underlying, expiry, strike, option_type, signal_reason,
                            signal_strength, spot_setup, regime, status, entry_price,
                            entry_iv_pct, tte_days, option_ma20, option_ma50,
                            above_option_ma20, above_option_ma50, signal_bar_time, entered_at,
                            closed_at, metadata, created_at, updated_at
                        ) VALUES (
                            :id, :session_id, 'NSE', :strategy_key, :strategy_label, :signal_key,
                            :symbol, :underlying, CAST(:expiry AS DATE), :strike, :option_type, :signal_reason,
                            :signal_strength, :spot_setup, :regime, :status, :entry_price,
                            :entry_iv_pct, :tte_days, NULL, NULL,
                            FALSE, FALSE, :signal_bar_time, NULL,
                            NULL, CAST(:metadata AS JSONB), NOW(), NOW()
                        )
                        ON CONFLICT (signal_key) DO UPDATE SET
                            status = EXCLUDED.status,
                            signal_reason = EXCLUDED.signal_reason,
                            signal_strength = EXCLUDED.signal_strength,
                            entry_price = EXCLUDED.entry_price,
                            entry_iv_pct = EXCLUDED.entry_iv_pct,
                            signal_bar_time = COALESCE(EXCLUDED.signal_bar_time, agent_signals.signal_bar_time),
                            metadata = agent_signals.metadata || EXCLUDED.metadata,
                            updated_at = NOW()
                        """
                    ),
                    {
                        "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, signal_key)),
                        "session_id": session_id,
                        "strategy_key": runtime.key,
                        "strategy_label": runtime.label,
                        "signal_key": signal_key,
                        "symbol": symbol,
                        "underlying": underlying,
                        "expiry": expiry_date,
                        "strike": strike_value,
                        "option_type": option_type,
                        "signal_reason": str(signal.get("reason") or source)[:120],
                        "signal_strength": strength,
                        "spot_setup": signal.get("mp_day_type") or signal.get("spot_setup"),
                        "regime": signal.get("regime") or signal.get("mp_day_type"),
                        "status": status_value[:40],
                        "entry_price": _flt(signal.get("ltp") or signal.get("entry_price")),
                        "entry_iv_pct": iv_pct,
                        "tte_days": signal.get("tte_days"),
                        "signal_bar_time": signal_bar_time,
                        "metadata": json.dumps(signal),
                    },
                )
                await session.commit()
        except Exception as exc:
            logger.warning(f"[Strategy] Failed to persist agent signal observation: {exc}")

    async def _persist_agent_signal(
        self,
        runtime: StrategyRuntime,
        pos: StrategyPosition,
        *,
        status: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        try:
            async with AsyncSessionLocal() as session:
                session_id = await self._ensure_paper_session_record(session, runtime)
                payload = metadata or {}
                signal_bar_time = _parse_iso_timestamp(str(payload.get("entry_bar_time") or pos.entry_bar_time))
                entered_at = _parse_iso_timestamp(pos.entered_at)
                closed_at = _now_ist() if status in {"closed", "cancelled"} else None
                await session.execute(
                    text(
                        """
                        INSERT INTO agent_signals (
                            id, session_id, market, strategy_key, strategy_label, signal_key,
                            symbol, underlying, expiry, strike, option_type, signal_reason,
                            signal_strength, spot_setup, regime, status, entry_price,
                            entry_iv_pct, tte_days, option_ma20, option_ma50,
                            above_option_ma20, above_option_ma50, signal_bar_time, entered_at,
                            closed_at, metadata, created_at, updated_at
                        ) VALUES (
                            :id, :session_id, 'NSE', :strategy_key, :strategy_label, :signal_key,
                            :symbol, :underlying, CAST(:expiry AS DATE), :strike, :option_type, :signal_reason,
                            :signal_strength, :spot_setup, :regime, :status, :entry_price,
                            :entry_iv_pct, :tte_days, :option_ma20, :option_ma50,
                            :above_option_ma20, :above_option_ma50, :signal_bar_time, :entered_at,
                            :closed_at, CAST(:metadata AS JSONB), NOW(), NOW()
                        )
                        ON CONFLICT (signal_key) DO UPDATE SET
                            status = EXCLUDED.status,
                            entry_price = EXCLUDED.entry_price,
                            entry_iv_pct = EXCLUDED.entry_iv_pct,
                            option_ma20 = EXCLUDED.option_ma20,
                            option_ma50 = EXCLUDED.option_ma50,
                            above_option_ma20 = EXCLUDED.above_option_ma20,
                            above_option_ma50 = EXCLUDED.above_option_ma50,
                            closed_at = COALESCE(EXCLUDED.closed_at, agent_signals.closed_at),
                            metadata = agent_signals.metadata || EXCLUDED.metadata,
                            updated_at = NOW()
                        """
                    ),
                    {
                        "id": pos.signal_id or str(uuid.uuid4()),
                        "session_id": session_id,
                        "strategy_key": runtime.key,
                        "strategy_label": runtime.label,
                        "signal_key": f"{runtime.key}:{pos.symbol}:{pos.entry_bar_time}",
                        "symbol": pos.symbol,
                        "underlying": pos.underlying,
                        "expiry": _coerce_date(pos.expiry),
                        "strike": pos.strike,
                        "option_type": pos.option_type,
                        "signal_reason": pos.signal_reason,
                        "signal_strength": pos.signal_strength,
                        "spot_setup": pos.spot_setup,
                        "regime": pos.regime,
                        "status": status,
                        "entry_price": pos.entry_price,
                        "entry_iv_pct": pos.entry_iv_pct,
                        "tte_days": payload.get("tte_days"),
                        "option_ma20": pos.option_ma20,
                        "option_ma50": pos.option_ma50,
                        "above_option_ma20": pos.above_option_ma20,
                        "above_option_ma50": pos.above_option_ma50,
                        "signal_bar_time": signal_bar_time,
                        "entered_at": entered_at,
                        "closed_at": closed_at,
                        "metadata": json.dumps(payload),
                    },
                )
                unrealized_pnl = _round_or_none(pos.unrealized_pnl, 2) or 0.0
                realized_pnl = float(payload.get("realized_pnl") or 0.0)
                position_status = "open" if pos.qty > 0 and status not in {"closed", "cancelled"} else "closed"
                await session.execute(
                    text(
                        """
                        INSERT INTO agent_positions (
                            id, signal_id, session_id, market, strategy_key, strategy_label,
                            symbol, underlying, expiry, strike, option_type, qty, initial_qty,
                            entry_price, current_price, peak_price, realized_pnl, unrealized_pnl,
                            entry_iv_pct, spot_setup, regime, signal_reason, phase, status,
                            option_ma20, option_ma50, above_option_ma20, above_option_ma50,
                            entered_at, closed_at, metadata, created_at, updated_at
                        ) VALUES (
                            :id, :signal_id, :session_id, 'NSE', :strategy_key, :strategy_label,
                            :symbol, :underlying, CAST(:expiry AS DATE), :strike, :option_type, :qty, :initial_qty,
                            :entry_price, :current_price, :peak_price, :realized_pnl, :unrealized_pnl,
                            :entry_iv_pct, :spot_setup, :regime, :signal_reason, :phase, :status,
                            :option_ma20, :option_ma50, :above_option_ma20, :above_option_ma50,
                            :entered_at, :closed_at, CAST(:metadata AS JSONB), NOW(), NOW()
                        )
                        ON CONFLICT (symbol) DO UPDATE SET
                            signal_id = EXCLUDED.signal_id,
                            qty = EXCLUDED.qty,
                            initial_qty = EXCLUDED.initial_qty,
                            current_price = EXCLUDED.current_price,
                            peak_price = EXCLUDED.peak_price,
                            realized_pnl = EXCLUDED.realized_pnl,
                            unrealized_pnl = EXCLUDED.unrealized_pnl,
                            phase = EXCLUDED.phase,
                            status = EXCLUDED.status,
                            closed_at = EXCLUDED.closed_at,
                            metadata = agent_positions.metadata || EXCLUDED.metadata,
                            updated_at = NOW()
                        """
                    ),
                    {
                        "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{runtime.key}:{pos.symbol}")),
                        "signal_id": pos.signal_id,
                        "session_id": session_id,
                        "strategy_key": runtime.key,
                        "strategy_label": runtime.label,
                        "symbol": pos.symbol,
                        "underlying": pos.underlying,
                        "expiry": _coerce_date(pos.expiry),
                        "strike": pos.strike,
                        "option_type": pos.option_type,
                        "qty": pos.qty,
                        "initial_qty": pos.initial_qty,
                        "entry_price": pos.entry_price,
                        "current_price": pos.current_price,
                        "peak_price": pos.peak_price,
                        "realized_pnl": realized_pnl,
                        "unrealized_pnl": unrealized_pnl,
                        "entry_iv_pct": pos.entry_iv_pct,
                        "spot_setup": pos.spot_setup,
                        "regime": pos.regime,
                        "signal_reason": pos.signal_reason,
                        "phase": pos.phase if pos.qty > 0 else PHASE_EXITED,
                        "status": position_status,
                        "option_ma20": pos.option_ma20,
                        "option_ma50": pos.option_ma50,
                        "above_option_ma20": pos.above_option_ma20,
                        "above_option_ma50": pos.above_option_ma50,
                        "entered_at": entered_at,
                        "closed_at": closed_at,
                        "metadata": json.dumps(payload),
                    },
                )
                await session.commit()
        except Exception as exc:
            logger.warning(f"[Strategy] Failed to persist agent audit rows: {exc}")

    async def _persist_agent_risk_state(self, status_payload: dict[str, Any]) -> None:
        try:
            broker_snapshot = (status_payload.get("data_health") or {}).get("broker_snapshot") or {}
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text(
                        """
                        INSERT INTO agent_risk_state (
                            id, market, strategy_key, strategy_label, trading_allowed,
                            kill_switch_active, auto_run_enabled, loop_active, running,
                            scan_interval_seconds, open_positions, active_windows, last_run_at,
                            broker_ready, connected_brokers, status_payload, created_at
                        ) VALUES (
                            :id, 'NSE', 'paper_strategy_agent', 'NSE Strategy Agent', :trading_allowed,
                            :kill_switch_active, :auto_run_enabled, :loop_active, :running,
                            :scan_interval_seconds, :open_positions, :active_windows, :last_run_at,
                            :broker_ready, CAST(:connected_brokers AS JSONB), CAST(:status_payload AS JSONB), NOW()
                        )
                        """
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "trading_allowed": not bool(status_payload.get("kill_switch_active")),
                        "kill_switch_active": bool(status_payload.get("kill_switch_active")),
                        "auto_run_enabled": bool(status_payload.get("auto_run_enabled")),
                        "loop_active": bool(status_payload.get("loop_active")),
                        "running": bool(status_payload.get("running")),
                        "scan_interval_seconds": int(status_payload.get("scan_interval_seconds") or 0),
                        "open_positions": sum(
                            int(item.get("summary", {}).get("open_positions") or 0)
                            for item in status_payload.get("strategies", [])
                        ),
                        "active_windows": int(status_payload.get("active_windows") or 0),
                        "last_run_at": _parse_iso_timestamp(status_payload.get("last_run_at")),
                        "broker_ready": bool(broker_snapshot.get("broker_ready")),
                        "connected_brokers": json.dumps(broker_snapshot.get("connected_brokers") or []),
                        "status_payload": json.dumps(status_payload),
                    },
                )
                # Rolling retention — the table is written every scan cycle
                # (60s) with the full status_payload as ~28 KB JSONB, so
                # without a cap the table grows ~40 MB/day. Keep the last
                # 720 rows per (market, strategy_key) ≈ 12 hours of scan
                # history. The transition log lives in agent_audit_events
                # (migration 017) which is the authoritative trail —
                # agent_risk_state is just a fast point-in-time snapshot.
                await session.execute(
                    text(
                        """
                        DELETE FROM agent_risk_state
                        WHERE id IN (
                            SELECT id FROM agent_risk_state
                            WHERE market = 'NSE' AND strategy_key = 'paper_strategy_agent'
                            ORDER BY created_at DESC
                            OFFSET 720
                        )
                        """
                    )
                )
                await session.commit()
        except Exception as exc:
            logger.warning(f"[Strategy] Failed to persist agent risk state: {exc}")

    async def _status_with_risk_snapshot(self) -> dict[str, Any]:
        status = self.get_status()
        await self._persist_agent_risk_state(status)
        return status

    def _append_event(self, runtime: StrategyRuntime, event: StrategyEvent) -> None:
        runtime.recent_events.insert(0, event)
        del runtime.recent_events[20:]

    def _append_commentary(self, scope: str, message: str, tone: str = "info") -> None:
        if not message:
            return
        prev = self._commentary[0] if self._commentary else None
        if prev and prev.scope == scope and prev.message == message:
            return
        self._commentary.insert(0, CommentaryEntry(
            time=_now_ist().isoformat(), scope=scope, tone=tone, message=message,
        ))
        del self._commentary[COMMENTARY_MAX:]

    # ── Telegram ─────────────────────────────────────────────────────────────

    async def _get_broker_status_summary(self) -> str | None:
        try:
            from api.routers.auth import (
                format_broker_status_summary,
                get_broker_connection_snapshot,
            )

            snapshot = await get_broker_connection_snapshot()
            return format_broker_status_summary(snapshot)
        except Exception as exc:
            logger.debug(f"[Strategy] broker status summary failed: {exc}")
            return None

    async def _send_telegram_text(self, message: str) -> None:
        from api.routers.auth import refresh_persistent_credentials_async

        await refresh_persistent_credentials_async()
        if not settings.TELEGRAM_REPORTS_ENABLED:
            return
        if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
            return
        text = message.strip()
        broker_status = await self._get_broker_status_summary()
        if broker_status:
            text = f"{text}\n{broker_status}"
        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.post(url, json={"chat_id": settings.TELEGRAM_CHAT_ID, "text": text})
        except Exception as exc:
            logger.warning(f"[Strategy] Telegram failed: {exc}")

    async def _maybe_send_telegram_report(self) -> None:
        from api.routers.auth import refresh_persistent_credentials_async

        await refresh_persistent_credentials_async()
        if not settings.TELEGRAM_REPORTS_ENABLED or not settings.TELEGRAM_BOT_TOKEN:
            return
        now = _now_ist()
        interval = _report_interval_seconds(settings.TELEGRAM_REPORT_INTERVAL)
        if self._telegram_last_sent_at and (now - self._telegram_last_sent_at).total_seconds() < interval:
            return

        # Pull cross-desk state at report time. Each section is optional —
        # if a desk isn't loaded or errors, that section is just empty.
        from notifications.telegram_agent import telegram_agent

        sections: list[tuple[str, list[str]]] = []

        # NSE strategies (this agent's own runtimes)
        nse_lines: list[str] = []
        nse_equity = 0.0
        nse_realized = 0.0
        nse_open = 0
        for runtime in self._runtimes():
            summary = runtime.portfolio.get_summary()
            equity = float(summary.get("total_equity") or 0.0)
            realized = float(summary.get("realized_pnl") or 0.0)
            open_count = len(runtime.positions)
            nse_equity += equity
            nse_realized += realized
            nse_open += open_count
            nse_lines.append(
                f"{runtime.label}: equity ₹{equity:,.0f}; realized ₹{realized:,.0f}; "
                f"open {open_count}; entries {runtime.entries}; exits {runtime.exits}"
            )
            for pos in list(runtime.positions.values())[:3]:
                strike_label = f" {int(pos.strike)}" if getattr(pos, "strike", None) else ""
                nse_lines.append(
                    f"  · {pos.underlying} {pos.option_type or ''}{strike_label} "
                    f"@{pos.entry_price:.2f} → {pos.current_price:.2f} ({pos.return_pct:.1f}%)"
                )
        nse_lines.insert(
            0,
            f"NSE total — equity ₹{nse_equity:,.0f}; realized ₹{nse_realized:,.0f}; open {nse_open}",
        )
        sections.append(("NSE Desk", nse_lines))

        # Commodity desk
        try:
            from paper_engine.commodity_strategy_agent import commodity_strategy_agent

            cstatus = commodity_strategy_agent.get_status(refresh=False)
            csum = cstatus.get("summary") or {}
            kill_label = "KILL" if cstatus.get("kill_switch_active") else "live"
            commodity_lines = [
                f"Commodity [{kill_label}] — equity ₹{float(csum.get('total_equity') or 0):,.0f}; "
                f"realized ₹{float(csum.get('realized_pnl') or 0):,.0f}; "
                f"day ₹{float(csum.get('day_pnl') or 0):,.0f}; "
                f"open {int(csum.get('open_positions') or 0)}; "
                f"win {float(csum.get('win_rate') or 0):.0%}",
            ]
            buckets = {"favourable": 0, "drifting": 0, "ready": 0, "active": 0, "neutral": 0}
            for row in (cstatus.get("watchlist") or []):
                b = str(row.get("bucket") or "")
                if b in buckets:
                    buckets[b] += 1
            commodity_lines.append(
                f"Buckets — ready {buckets['ready']}; active {buckets['active']}; "
                f"favourable {buckets['favourable']}; drifting {buckets['drifting']}"
            )
            sections.append(("Commodity Desk", commodity_lines))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[Telegram] commodity section skipped: {exc}")

        # Data quality
        try:
            from market_data.data_quality_agent import data_quality_agent

            dq = data_quality_agent.snapshot()
            dq_lines = [
                f"overall {dq.get('overall')}; market {dq.get('market_state')}; "
                f"{dq.get('symbol_count')} symbols; stale {dq.get('stale_count')}; "
                f"flagged {dq.get('flagged_count')}"
            ]
            sections.append(("Data Quality", dq_lines))
        except Exception:  # noqa: BLE001
            pass

        # Active windows + broker health
        broker_status = await self._get_broker_status_summary()
        header_extras: list[str] = [f"windows {len(self._active_windows)}"]
        if broker_status:
            header_extras.append(broker_status.replace("\n", " · "))
        title = (
            f"Nomad Curie · {now.strftime('%d %b %Y %I:%M %p IST')}\n"
            + " · ".join(header_extras)
        )
        try:
            sent = await telegram_agent.notify_summary(
                title=title,
                sections=sections,
                dedup_key=f"nse_periodic:{now.strftime('%Y%m%d%H%M')}",
            )
            if sent:
                self._telegram_last_sent_at = now
        except Exception:
            pass

    def set_kill_switch(self, active: bool) -> dict[str, Any]:
        self._kill_switch_active = bool(active)
        cancelled_orders = 0
        for runtime in self._runtimes():
            for order in list(runtime.order_book.get_open_orders(runtime.portfolio.session_id)):
                if runtime.order_book.cancel_order(order.order_id):
                    cancelled_orders += 1

        if self._kill_switch_active:
            self._manual_restart_required = True
            self._last_message = "NSE kill switch active. New entries are blocked until a manual restart."
            self._append_commentary("System", self._last_message, tone="warning")
        else:
            self._last_message = "NSE kill switch released. Use auto-run restart to resume scanning."
            self._append_commentary("System", self._last_message, tone="success")

        self._persist_state()
        return self.get_control_state(cancelled_orders=cancelled_orders)

    async def engage_manual_kill_switch(self) -> dict[str, Any]:
        """Operator kill switch: flatten paper strategy positions and stop auto scans."""
        self._refresh_state_from_store()
        self._kill_switch_active = True
        self._manual_restart_required = True
        self._auto_run_enabled = False
        cancelled_orders = 0
        closed_positions: list[dict[str, Any]] = []

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        for runtime in self._runtimes():
            for order in list(runtime.order_book.get_open_orders(runtime.portfolio.session_id)):
                if runtime.order_book.cancel_order(order.order_id):
                    cancelled_orders += 1
            for symbol, position in list(runtime.positions.items()):
                exit_price = float(position.current_price or position.entry_price or 0.0)
                if exit_price <= 0:
                    continue
                qty = int(position.qty or 0)
                await self._close_position(
                    runtime,
                    position,
                    exit_price,
                    "manual_kill_switch",
                    qty=qty,
                    partial=False,
                )
                closed_positions.append(
                    {
                        "strategy_key": runtime.key,
                        "symbol": symbol,
                        "qty": qty,
                        "exit_price": round(exit_price, 2),
                    }
                )

        self._last_message = (
            f"NSE kill switch active. Closed {len(closed_positions)} position(s), "
            f"cancelled {cancelled_orders} order(s), and stopped auto-run."
        )
        self._append_commentary("System", self._last_message, tone="warning")
        self._persist_state()
        return self.get_control_state(
            cancelled_orders=cancelled_orders,
            closed_positions=closed_positions,
        )

    async def set_auto_run(self, enabled: bool) -> dict[str, Any]:
        """Enable or disable the recurring background scan loop."""
        if enabled and self._kill_switch_active:
            self._last_message = "NSE kill switch is active. Release it before restarting auto-run."
            self._append_commentary("System", self._last_message, tone="warning")
            self._persist_state()
            return self.get_control_state()
        self._auto_run_enabled = bool(enabled)
        if self._auto_run_enabled:
            self._manual_restart_required = False
            # Start background loop if not already running
            if not self._task or self._task.done():
                self._task = asyncio.create_task(self._loop(), name="paper-strategy-agent")
            self._last_message = "Auto-run enabled. Agent will scan every 60 s during market hours."
            self._append_commentary("System", self._last_message, tone="success")
        else:
            # Cancel background loop but keep agent enabled for manual runs
            if self._task and not self._task.done():
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
                self._task = None
            self._last_message = "Auto-run disabled. Use run-once for manual scans."
            self._append_commentary("System", self._last_message, tone="warning")
        self._persist_state()
        return self.get_control_state()

    def get_control_state(
        self,
        *,
        cancelled_orders: int = 0,
        closed_positions: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        self._refresh_state_from_store()
        return {
            "market": "nse",
            "auto_run_enabled": self._auto_run_enabled,
            "kill_switch_active": self._kill_switch_active,
            "manual_restart_required": self._manual_restart_required,
            "loop_active": bool(self._task and not self._task.done()),
            "cancelled_orders": cancelled_orders,
            "closed_positions": closed_positions or [],
        }

    # ── Status API ───────────────────────────────────────────────────────────

    @staticmethod
    def _signal_audit_bucket(signal: dict[str, Any]) -> str:
        status = str(signal.get("status") or "").lower().replace("_", "-")
        strength = str(signal.get("strength") or "").lower()
        freshness = str(signal.get("freshness") or "").lower()
        blocked = bool(signal.get("learning_blocked"))

        if blocked:
            return "drifting_away"
        if status in {"active", "open", "entry-ready", "candidate", "filled"}:
            return "conditions_met_traded"
        if status in {"trend-aligned", "watching", "monitoring", "research-only"}:
            return "favourable_tracking"
        if strength in {"strong", "monitoring"} and freshness not in {"missing", "stale"}:
            return "favourable_tracking"
        return "drifting_away"

    @staticmethod
    def _serialize_signal_for_audit(runtime: StrategyRuntime, signal: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": "signal",
            "strategy_key": runtime.key,
            "strategy_label": runtime.label,
            "source": signal.get("source") or "strategy_signal_lane",
            "underlying": signal.get("underlying"),
            "symbol": signal.get("symbol") or signal.get("trading_symbol"),
            "direction": signal.get("direction"),
            "status": signal.get("status"),
            "reason": signal.get("reason"),
            "freshness": signal.get("freshness"),
            "instruction": signal.get("instruction"),
            "as_of": signal.get("as_of") or signal.get("option_last_bar_time") or signal.get("signal_date"),
            "expiry": signal.get("expiry"),
            "strike": signal.get("strike") or signal.get("atm_strike"),
            "ltp": signal.get("ltp"),
            "iv_pct": signal.get("iv_pct"),
            "macd": signal.get("macd"),
            "previous_macd": signal.get("previous_macd"),
            "macd_histogram": signal.get("macd_histogram"),
            "rsi": signal.get("rsi"),
            "priority_score": signal.get("priority_score") or signal.get("learning_score"),
        }

    def _build_strategy_audit_lanes(self, runtime: StrategyRuntime) -> dict[str, list[dict[str, Any]]]:
        lanes: dict[str, list[dict[str, Any]]] = {
            "conditions_met_traded": [],
            "favourable_tracking": [],
            "drifting_away": [],
        }

        for pos in runtime.positions.values():
            lanes["conditions_met_traded"].append(
                {
                    "kind": "position",
                    "strategy_key": runtime.key,
                    "strategy_label": runtime.label,
                    "source": "open_position",
                    "underlying": pos.underlying,
                    "symbol": pos.symbol,
                    "direction": pos.option_type,
                    "status": f"open:{pos.phase}",
                    "reason": pos.signal_reason,
                    "freshness": "live",
                    "instruction": (
                        f"{pos.underlying} {pos.option_type} {int(pos.strike)} "
                        f"entry {pos.entry_price:.2f}, last {pos.current_price:.2f}, "
                        f"return {pos.return_pct:+.2f}%"
                    ),
                    "as_of": pos.price_updated_at or pos.entered_at,
                    "expiry": pos.expiry,
                    "strike": pos.strike,
                    "ltp": _round_or_none(pos.current_price, 2),
                    "entry_price": _round_or_none(pos.entry_price, 2),
                    "qty": pos.qty,
                    "pnl": _round_or_none(pos.unrealized_pnl, 2),
                    "return_pct": _round_or_none(pos.return_pct, 2),
                    "iv_pct": pos.entry_iv_pct,
                    "rsi": pos.latest_rsi,
                    "priority_score": pos.signal_strength,
                }
            )

        for trade in reversed(runtime.portfolio._trade_history[-20:]):
            lanes["conditions_met_traded"].append(
                {
                    "kind": "trade",
                    "strategy_key": runtime.key,
                    "strategy_label": runtime.label,
                    "source": "closed_trade",
                    "underlying": trade.symbol.split(":")[1] if ":" in trade.symbol else trade.symbol,
                    "symbol": trade.symbol,
                    "direction": trade.option_type or trade.instrument_type,
                    "status": "closed",
                    "reason": trade.setup_type or trade.signal_id or "trade_closed",
                    "freshness": "ledger",
                    "instruction": (
                        f"Closed {trade.qty} @ {trade.exit_price:.2f}; "
                        f"P&L {trade.pnl:+.2f}"
                    ),
                    "as_of": trade.exit_time.isoformat(),
                    "expiry": trade.expiry,
                    "strike": trade.strike,
                    "entry_price": _round_or_none(trade.entry_price, 2),
                    "exit_price": _round_or_none(trade.exit_price, 2),
                    "qty": trade.qty,
                    "pnl": _round_or_none(trade.pnl, 2),
                    "iv_pct": trade.entry_iv_pct,
                }
            )

        signal_rows = list(runtime.signal_lane or [])
        if not signal_rows and runtime.key == "macd_strategy":
            signal_rows = [
                row for row in list((runtime.meta or {}).get("prepared_watchlist") or [])
                if isinstance(row, dict)
            ]

        for signal in signal_rows:
            if not isinstance(signal, dict):
                continue
            bucket = self._signal_audit_bucket(signal)
            lanes[bucket].append(self._serialize_signal_for_audit(runtime, signal))

        for key in lanes:
            lanes[key] = lanes[key][:30]
        return lanes

    def get_status(self, *, refresh: bool = True) -> dict[str, Any]:
        if refresh:
            self._refresh_state_from_store()
        next_scan_at = None
        if self._last_run_at and self._auto_run_enabled:
            try:
                next_scan_at = (
                    datetime.fromisoformat(self._last_run_at) + timedelta(seconds=self.scan_interval_seconds)
                ).isoformat()
            except ValueError:
                next_scan_at = None

        return {
            "enabled": self._enabled,
            "auto_run_enabled": self._auto_run_enabled,
            "kill_switch_active": self._kill_switch_active,
            "manual_restart_required": self._manual_restart_required,
            "loop_active": bool(self._task and not self._task.done()),
            "running": self._running,
            "scan_interval_seconds": self.scan_interval_seconds,
            "last_run_at": self._last_run_at,
            "next_scan_at": next_scan_at,
            "last_error": self._last_error,
            "last_message": self._last_message,
            "data_health": self._last_data_health,
            "trading_calendar": trading_calendar.exchange_status("NSE"),
            "target_expiry": self._last_expiry,
            "candidate_expiries": self._last_candidate_expiries,
            "active_windows": len(self._active_windows),
            "strategy1_scan_windows": len(self._scan_windows),
            "regime_summary": {
                und: q.regime for und, q in self._regime_cache.items()
            } if self._regime_cache else {},
            "telegram": {
                "enabled": settings.TELEGRAM_REPORTS_ENABLED,
                "report_interval": settings.TELEGRAM_REPORT_INTERVAL,
                "configured": bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID),
                "last_sent_at": self._telegram_last_sent_at.isoformat() if self._telegram_last_sent_at else None,
            },
            "strategy_agents": [lane.build_status_payload() for lane in self._lane_agents()],
            "commentary": [asdict(entry) for entry in self._commentary],
            "strategies": [
                {
                    **(
                        next(
                            (lane.build_status_payload() for lane in self._lane_agents() if lane.runtime is runtime),
                            {},
                        )
                    ),
                    "key": runtime.key,
                    "label": runtime.label,
                    "agent": next(
                        (lane.build_status_payload() for lane in self._lane_agents() if lane.runtime is runtime),
                        None,
                    ),
                    "summary": {
                        **runtime.portfolio.get_summary(),
                        "open_positions": len(runtime.positions),
                        "entries": runtime.entries,
                        "exits": runtime.exits,
                    },
                    "positions": [
                        {
                            **{k: v for k, v in asdict(pos).items() if k != "macd_line"},
                            "unrealized_pnl": _round_or_none(pos.unrealized_pnl, 2),
                            "return_pct": _round_or_none(pos.return_pct, 2),
                        }
                        for pos in runtime.positions.values()
                    ],
                    "recent_events": [asdict(event) for event in runtime.recent_events],
                    **(lambda all_trades: {
                        "trade_history": all_trades,
                        "today_trades": _split_today_history(all_trades)[0],
                        "historical_trades": _split_today_history(all_trades)[1],
                    })([
                        {
                            "symbol": trade.symbol,
                            "action": trade.action,
                            "qty": trade.qty,
                            "entry_price": _round_or_none(trade.entry_price, 2),
                            "exit_price": _round_or_none(trade.exit_price, 2),
                            "pnl": _round_or_none(trade.pnl, 2),
                            "entry_time": trade.entry_time.isoformat(),
                            "exit_time": trade.exit_time.isoformat(),
                            "instrument_type": trade.instrument_type,
                            "expiry": trade.expiry,
                            "strike": trade.strike,
                            "option_type": trade.option_type,
                        }
                        for trade in reversed(runtime.portfolio._trade_history)
                    ]),
                    "last_scan_at": runtime.last_scan_at,
                    "last_message": runtime.last_message,
                    "signals": runtime.signal_lane
                    or (
                        [
                            row for row in list((runtime.meta or {}).get("prepared_watchlist") or [])
                            if isinstance(row, dict)
                        ]
                        if runtime.key == "macd_strategy"
                        else []
                    ),
                    "audit_lanes": self._build_strategy_audit_lanes(runtime),
                    "meta": runtime.meta,
                    "instrument_universe": (runtime.meta or {}).get("instrument_universe") or [],
                }
                # S2 deleted (2026-06-02): the index_mp_strategy runtime is
                # kept in _runtimes() for state-file load/persist back-compat,
                # but it must NOT surface in the API `strategies[]` array —
                # otherwise the UI still receives a (dead) Strategy 2 lane.
                for runtime in self._runtimes()
                if runtime.key != "index_mp_strategy"
            ],
        }

    async def archive_and_reset_paper_account(self, *, actor: str = "manual") -> dict[str, Any]:
        """Archive current NSE paper state, then reset each strategy runtime
        to a fresh ₹10L paper account. Mirrors the commodity reset endpoint.
        """
        try:
            from agentic_rag.audit_agent import record_audit_event
        except Exception:  # noqa: BLE001
            record_audit_event = None  # type: ignore[assignment]

        archived_at = _now_ist()
        archive_dir = Path(__file__).resolve().parent.parent / "runtime" / "nse_archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        stamp = archived_at.strftime("%Y%m%dT%H%M%S%f%z")
        archive_path = archive_dir / f"{stamp}_pre_reset.json"

        prior_realized: dict[str, float] = {}
        prior_trades: dict[str, int] = {}
        snapshot: dict[str, Any] = {}
        try:
            snapshot = self.get_status(refresh=False)
        except Exception:  # noqa: BLE001
            snapshot = {}
        archive_payload = {
            "archived_at": archived_at.isoformat(),
            "reason": "manual_paper_reset",
            "snapshot": snapshot,
        }
        archive_path.write_text(json.dumps(archive_payload, indent=2, default=str))

        for runtime in self._runtimes():
            try:
                summary = runtime.portfolio.get_summary()
                prior_realized[runtime.key] = float(summary.get("realized_pnl") or 0.0)
                prior_trades[runtime.key] = int(summary.get("total_trades") or 0)
            except Exception:  # noqa: BLE001
                prior_realized[runtime.key] = 0.0
                prior_trades[runtime.key] = 0
            session_id = f"{runtime.key}-paper"
            runtime.portfolio = PaperPortfolio(initial_capital=1_000_000.0, session_id=session_id)
            # PaperPortfolio.__init__ starts with empty curves, but defensively
            # re-zero them here so any prior reference cached elsewhere still
            # surfaces a clean drawdown / sharpe series on the dashboard.
            runtime.portfolio._trade_history = []
            runtime.portfolio._equity_curve = []
            runtime.portfolio._daily_pnl = defaultdict(float)
            runtime.portfolio._peak_equity = runtime.portfolio.initial_capital
            runtime.portfolio._positions = {}
            # After clearing positions + history, available_capital MUST be
            # the full initial capital. Without this, any prior realized PnL
            # that had been refunded to cash before the reset would survive
            # as phantom equity.
            runtime.portfolio.available_capital = runtime.portfolio.initial_capital
            runtime.order_book = PaperOrderBook(on_fill=runtime.portfolio.on_fill)
            runtime.positions = {}
            runtime.signal_lane = []
            runtime.processed_signals = {}
            runtime.recent_events = []
            runtime.entries = 0
            runtime.exits = 0
            runtime.meta = {}
            runtime.last_scan_at = None
            runtime.last_message = (
                f"{runtime.label} reset to ₹1,000,000. Prior state archived."
            )

        self._last_run_at = None
        self._last_paper_reset_at = archived_at.isoformat()
        self._last_error = None
        self._last_message = (
            "NSE paper account reset to ₹1,000,000 across all strategies. "
            "Archived prior state."
        )
        try:
            self._persist_state()
        except Exception:  # noqa: BLE001
            pass

        if record_audit_event is not None:
            try:
                await record_audit_event(
                    market="nse",
                    event_type="paper_account_reset",
                    actor=actor,
                    severity="warning",
                    message=self._last_message,
                    previous_state="damaged",
                    new_state="fresh",
                    payload={
                        "archive_path": str(archive_path),
                        "prior_realized_pnl_by_strategy": prior_realized,
                        "prior_total_trades_by_strategy": prior_trades,
                        "new_initial_capital": 1_000_000.0,
                    },
                )
            except Exception:  # noqa: BLE001
                pass

        return {
            "archived": True,
            "archive_path": str(archive_path),
            "initial_capital": 1_000_000.0,
            "prior_realized_pnl_by_strategy": prior_realized,
            "prior_total_trades_by_strategy": prior_trades,
        }


paper_strategy_agent = PaperStrategyAgent()
