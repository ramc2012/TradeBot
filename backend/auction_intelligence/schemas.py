from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Optional


Direction = Literal["LONG", "SHORT", "FLAT"]
ExecutionStyle = Literal["PASSIVE", "AGGRESSIVE", "WAIT"]
RegimeLabel = Literal[
    "balance",
    "developing_balance",
    "breakout_acceptance",
    "breakout_rejection",
    "failed_auction",
    "trend_continuation",
    "reversal",
    "rotational_day",
    "trend_day",
    "neutral_extreme",
    "no_trade",
]


@dataclass
class MarketBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class TradePrint:
    timestamp: datetime
    price: float
    quantity: float
    aggressor_side: Literal["buy", "sell", "unknown"] = "unknown"


@dataclass
class QuoteSnapshot:
    timestamp: datetime
    bid: float
    ask: float
    bid_size: float = 0.0
    ask_size: float = 0.0


@dataclass
class DepthLevel:
    price: float
    quantity: float


@dataclass
class DepthSnapshot:
    timestamp: datetime
    bids: list[DepthLevel] = field(default_factory=list)
    asks: list[DepthLevel] = field(default_factory=list)


@dataclass
class SessionContext:
    symbol: str
    session_date: date
    last_price: float
    stale_data_seconds: float = 0.0
    minutes_to_close: int = 120
    broker_connected: bool = True


@dataclass
class PortfolioSnapshot:
    net_liquidation: float = 1_000_000.0
    daily_realized_pnl: float = 0.0
    open_positions: int = 0
    symbol_exposure: dict[str, float] = field(default_factory=dict)
    agent_drawdowns: dict[str, float] = field(default_factory=dict)
    correlated_exposure: float = 0.0


@dataclass
class MarketProfileSnapshot:
    symbol: str
    session_date: str
    period_minutes: int
    tick_size: float
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    total_volume: float
    tpo_counts: dict[float, int]
    tpo_letters: dict[float, str]
    poc: float
    vah: float
    val: float
    initial_balance_high: float
    initial_balance_low: float
    initial_balance_range: float
    day_range: float
    range_extension_up: float
    range_extension_down: float
    single_prints: list[float]
    buying_tail: list[float]
    selling_tail: list[float]
    poor_high: bool
    poor_low: bool
    excess_high: float
    excess_low: float
    spike_direction: str
    spike_price: Optional[float]
    period_count: int
    sample_count: int
    value_area_overlap: Optional[float] = None
    poc_shift: Optional[float] = None
    value_migration: Optional[float] = None
    prior_poc_untouched: Optional[bool] = None
    bracket_state: Optional[str] = None


@dataclass
class OrderFlowSnapshot:
    spread: float
    mid_price: float
    micro_price: float
    top_imbalance: float
    depth_imbalance: float
    aggressive_buy_volume: float
    aggressive_sell_volume: float
    delta: float
    cumulative_delta: float
    vwap: float
    vwap_drift: float
    queue_pressure: float
    volatility_burst: float
    passive_fill_probability: float
    aggressive_fill_probability: float
    adverse_selection_risk: float
    timing_confidence: float
    execution_aggression: ExecutionStyle
    micro_stop_distance: float


@dataclass
class RegimeAssessment:
    label: RegimeLabel
    confidence: float
    allowed_directions: list[Direction]
    reasons: list[str]
    scorecard: dict[str, float] = field(default_factory=dict)


@dataclass
class AgentDecision:
    agent_name: str
    action: Direction
    confidence: float
    entry_price: Optional[float]
    stop_price: Optional[float]
    target_price: Optional[float]
    quantity: int
    sleeve_fraction: float
    rationale: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RiskDecision:
    allowed: bool
    kill_switch: bool
    max_size_multiplier: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class ExecutionInstruction:
    agent_name: str
    symbol: str
    action: Direction
    style: ExecutionStyle
    order_type: str
    limit_price: Optional[float]
    slices: int
    cancel_after_seconds: int
    rationale: list[str]


@dataclass
class AgentContext:
    session: SessionContext
    portfolio: PortfolioSnapshot
    current_profile: MarketProfileSnapshot
    prior_profile: Optional[MarketProfileSnapshot]
    order_flow: OrderFlowSnapshot
    regime: RegimeAssessment
    config: dict[str, Any]


@dataclass
class AnalysisBundle:
    config_scope: dict[str, Any]
    market_profile: MarketProfileSnapshot
    prior_market_profile: Optional[MarketProfileSnapshot]
    order_flow: OrderFlowSnapshot
    regime: RegimeAssessment
    agent_decisions: list[AgentDecision]
    risk: RiskDecision
    execution_plan: list[ExecutionInstruction]


@dataclass
class PaperTradeRecord:
    recorded_at: str
    symbol: str
    regime: str
    agent_name: str
    action: Direction
    confidence: float
    entry_price: Optional[float]
    stop_price: Optional[float]
    target_price: Optional[float]
    execution_style: str
    notes: list[str] = field(default_factory=list)
