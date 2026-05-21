from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class FMPOptionSelection:
    underlying: str
    option_type: str
    strike: float
    expiry: str
    premium: float
    previous_premium: Optional[float]
    trading_symbol: Optional[str]
    instrument_key: Optional[str]
    lot_size: int
    oi: Optional[float] = None
    oi_change: Optional[float] = None
    volume: Optional[float] = None
    pcr_oi: Optional[float] = None
    iv_rank: Optional[float] = None
    selection_reason: str = ""
    moneyness: str = "ATM"
    horizon: str = "swing"
    days_to_expiry: int = 0


@dataclass
class FMPSignal:
    underlying: str
    signal_time: str
    setup_name: str
    action: str
    confidence: float
    horizon: str
    actionable: bool
    latest_close: float
    entry_trigger: float
    stop_level: float
    target_level: float
    hourly_shape: str
    daily_shape: str
    hourly_number: int
    value_migration_score: int
    daily_context: str
    rationale: list[str] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)
    order_flow_bias: dict[str, float | str] = field(default_factory=dict)
    options: Optional[FMPOptionSelection] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class FMPReplayTrade:
    trade_id: str
    underlying: str
    setup_name: str
    action: str
    horizon: str
    entry_time: str
    exit_time: str
    entry_underlying: float
    exit_underlying: float
    entry_premium: float
    exit_premium: float
    strike: float
    expiry: str
    option_type: str
    quantity: int
    pnl: float
    return_pct: float
    max_adverse_pct: float
    max_favorable_pct: float
    stop_level: float
    target_level: float
    exit_reason: str
    confidence: float
    daily_shape: str
    hourly_shape: str


@dataclass
class FMPPaperPositionRecord:
    position_id: str
    status: str
    opened_at: str
    updated_at: str
    closed_at: Optional[str]
    underlying: str
    setup_name: str
    action: str
    horizon: str
    trading_symbol: Optional[str]
    instrument_key: Optional[str]
    instrument_type: Optional[str]
    option_type: Optional[str]
    strike: Optional[float]
    expiry: Optional[str]
    quantity: int
    lot_size: int
    entry_premium: float
    latest_premium: float
    exit_premium: Optional[float]
    realized_pnl: float
    unrealized_pnl: float
    stop_level: float
    target_level: float
    confidence: float
    daily_shape: str
    hourly_shape: str
