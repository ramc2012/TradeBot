"""State models and persistence helpers for the NSE paper strategy runtime."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from paper_engine.base_strategy_agent import IST
from paper_engine.order_book import PaperOrderBook
from paper_engine.portfolio import PaperPortfolio


@dataclass
class StrategyPosition:
    signal_id: Optional[str] = field(default=None, kw_only=True)
    symbol: str
    underlying: str
    expiry: str
    strike: float
    option_type: str
    instrument_key: Optional[str]
    trading_symbol: Optional[str]
    qty: int
    initial_qty: int
    entry_price: float
    current_price: float
    peak_price: float
    entry_bar_time: str
    entered_at: str
    signal_reason: str
    signal_strength: Optional[float] = None
    latest_rsi: Optional[float] = None
    phase: str = "phase1"
    trailing_stop: Optional[float] = None
    entry_iv_pct: Optional[float] = None
    spot_setup: Optional[str] = None
    regime: Optional[str] = None
    option_ma20: Optional[float] = None
    option_ma50: Optional[float] = None
    above_option_ma20: bool = False
    above_option_ma50: bool = False
    first_pullback_ignored_at: Optional[str] = None
    window_end: Optional[str] = None
    lot_size: Optional[int] = None
    price_updated_at: Optional[str] = None
    macd_line: Optional[list] = field(default=None, repr=False)

    @property
    def unrealized_pnl(self) -> float:
        return (self.current_price - self.entry_price) * self.qty

    @property
    def return_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return ((self.current_price - self.entry_price) / self.entry_price) * 100.0


@dataclass
class StrategyEvent:
    time: str
    event: str
    symbol: str
    underlying: str
    option_type: str
    strike: float
    price: float
    qty: int
    reason: str
    signal_strength: Optional[float] = None
    pnl: Optional[float] = None
    phase: Optional[str] = None


@dataclass
class CommentaryEntry:
    time: str
    scope: str
    tone: str
    message: str


@dataclass
class StrategyRuntime:
    key: str
    label: str
    portfolio: PaperPortfolio
    order_book: PaperOrderBook
    positions: dict[str, StrategyPosition] = field(default_factory=dict)
    processed_signals: dict[str, str] = field(default_factory=dict)
    recent_events: list[StrategyEvent] = field(default_factory=list)
    entries: int = 0
    exits: int = 0
    last_scan_at: Optional[str] = None
    last_message: Optional[str] = None
    signal_lane: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


def _resolve_strategy_state_file() -> Path:
    env_path = os.environ.get("NSE_STRATEGY_STATE_FILE", "").strip()
    if env_path:
        return Path(env_path)
    docker_path = Path("/app/nse_strategy_state.json")
    if docker_path.parent.is_dir():
        return docker_path
    return Path(__file__).resolve().parent.parent / "nse_strategy_state.json"


_NSE_STRATEGY_STATE_FILE = _resolve_strategy_state_file()


def _load_saved_strategy_state() -> dict[str, Any]:
    if not _NSE_STRATEGY_STATE_FILE.exists():
        return {}
    try:
        payload = json.loads(_NSE_STRATEGY_STATE_FILE.read_text())
    except Exception as exc:
        logger.warning(f"[Strategy] Failed to load {_NSE_STRATEGY_STATE_FILE}: {exc}")
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_strategy_state(payload: dict[str, Any]) -> None:
    try:
        _NSE_STRATEGY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _NSE_STRATEGY_STATE_FILE.write_text(json.dumps(payload, indent=2))
    except Exception as exc:
        logger.warning(f"[Strategy] Failed to persist {_NSE_STRATEGY_STATE_FILE}: {exc}")


__all__ = [
    "IST",
    "CommentaryEntry",
    "StrategyEvent",
    "StrategyPosition",
    "StrategyRuntime",
    "_NSE_STRATEGY_STATE_FILE",
    "_load_saved_strategy_state",
    "_save_strategy_state",
]
