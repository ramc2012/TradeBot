"""Typed schemas shared by the directional options engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ContractMeta:
    underlying: str
    expiry: str
    expiry_kind: str
    option_type: str
    strike: float
    trading_symbol: str
    lot_size: int
    tick_size: float
    file_path: str
    earliest_candle: str
    latest_candle: str
    candle_count: int


@dataclass(frozen=True)
class FeatureSnapshot:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    ema_fast: float
    ema_slow: float
    ema_spread_pct: float
    adx: float
    plus_di: float
    minus_di: float
    atr: float
    range_pct: float
    breakout_up: float
    breakout_down: float
    rv_annualized: float
    rv_percentile: float
    range_expansion: float
    session_progress: float
    momentum_3: float
    momentum_8: float
    atr_pct: float = 0.0
    ema_fast_slope_pct: float = 0.0
    macd: float = 0.0
    macd_signal: float = 0.0
    macd_hist: float = 0.0
    macd_hist_pct: float = 0.0
    rsi_14: float = 50.0
    vwap: float = 0.0
    vwap_deviation_pct: float = 0.0
    volume_zscore: float = 0.0
    body_pct: float = 0.0
    close_location: float = 0.0
    opening_range_position: float = 0.5
    trend_quality: float = 0.0


@dataclass(frozen=True)
class RegimeSnapshot:
    label: str
    trade_allowed: bool
    confidence: float
    reasons: list[str] = field(default_factory=list)
    preferred_expiry_kind: str = "weekly"
    delta_target_min: float = 0.35
    delta_target_max: float = 0.55
    exit_profile: str = "balanced"


@dataclass(frozen=True)
class DirectionalSignal:
    direction: str
    confidence: float
    expected_move: float
    expected_horizon_bars: int
    expected_horizon_hours: float
    direction_score: float
    expected_iv_change: float
    sleeve: str
    thesis: str
    regime: str
    expected_move_pct: float = 0.0
    p_up: float = 0.5
    p_move_gt_1sigma: float = 0.0
    p_move_gt_2sigma: float = 0.0
    jump_score: float = 0.0
    timing_precision: float = 0.0
    tail_probability: float = 0.0
    model_uncertainty: float = 0.0
    # True when this signal came from the POSITIONAL (positioning-confirmed)
    # view — the selector must then pick the MONTHLY contract in the validated
    # DTE window, not the regime's intraday weekly preference.
    positional: bool = False
    # IV-conditioned position-size factor in [IV_SIZING_FLOOR, 1.0]
    # (2026-07-17 owner directive: "position has to be sized as per IV it
    # cannot prevent a trade"). 1.0 = favorable IV (low level, falling/flat
    # trend) = full base risk budget; adverse IV shrinks the BASE risk budget
    # monotonically toward the floor instead of vetoing the entry. Default 1.0
    # so stock / legacy-momentum signals and older callers are unchanged.
    iv_sizing_factor: float = 1.0


@dataclass(frozen=True)
class ContractCandidate:
    trading_symbol: str
    file_path: str
    option_type: str
    expiry: str
    expiry_kind: str
    strike: float
    lot_size: int
    tick_size: float
    option_price: float
    volume: float
    oi: float
    days_to_expiry: float
    moneyness_pct: float
    implied_vol: float
    delta: float
    gamma: float
    theta: float
    vega: float
    delta_bucket: str
    liquidity_score: float
    iv_value_score: float
    theta_penalty: float
    spread_pct: float
    slippage_pct: float
    spread_cost: float
    slippage_cost: float
    fees: float
    expected_pnl: float
    contract_score: float
    selection_reason: str
    q_price: float = 0.0
    p_terminal_edge: float = 0.0
    p_trading_edge: float = 0.0
    p_tail: float = 0.0
    q_tail: float = 0.0
    p_minus_q_tail: float = 0.0
    expected_return_on_premium: float = 0.0
    probability_of_profit: float = 0.0
    probability_of_50pct_loss: float = 0.0
    probability_of_total_loss: float = 0.0
    timing_fit: float = 0.0
    skew_tax: float = 0.0
    model_confidence: float = 0.0
    model_error_buffer: float = 0.0
    theta_cost: float = 0.0
    iv_tail_edge_bonus: float = 0.0
    expiry_score: float = 0.0
    utility: float = 0.0
    rejection_reasons: list[str] = field(default_factory=list)
    selected: bool = False
    instrument_key: Optional[str] = None
    price_source: str = "runtime_dataset"
    # spread_pct is NOT a quoted bid/ask spread — the watchlist snapshots
    # carry LTP only, so it is estimated from volume/OI/moneyness in the
    # selector. Labeled here so every payload (journal, UI, policy
    # features) is honest about the lineage.
    spread_source: str = "synthetic_volume_oi_moneyness"


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    quantity_lots: int
    quantity_units: int
    premium_at_risk: float
    max_loss: float
    risk_budget: float
    # premium_cap is None when the engine is configured with no premium
    # cap (RL-managed sizing). Kept as float for backwards-compatible
    # consumers when the cap IS set.
    premium_cap: Optional[float]
    reasons: list[str] = field(default_factory=list)


@dataclass
class PositionState:
    underlying: str
    contract: ContractCandidate
    entry_time: str
    entry_spot: float
    entry_mark_price: float
    entry_fill_price: float
    stop_price: float
    target_price: float
    stop_underlying: float
    quantity_lots: int
    quantity_units: int
    max_horizon_bars: int
    expected_move: float
    expected_pnl: float
    confidence: float
    regime: str
    peak_mark_price: float
    held_bars: int = 0


@dataclass(frozen=True)
class TradeRecord:
    underlying: str
    trading_symbol: str
    option_type: str
    expiry: str
    expiry_kind: str
    strike: float
    qty_lots: int
    qty_units: int
    entry_time: str
    exit_time: str
    entry_spot: float
    exit_spot: float
    entry_price: float
    exit_price: float
    pnl: float
    return_pct: float
    premium_paid: float
    expected_pnl: float
    expected_move: float
    realized_move: float
    confidence: float
    regime: str
    delta_bucket: str
    exit_reason: str
    spread_cost: float
    slippage_cost: float
    theta_cost: float


@dataclass(frozen=True)
class DashboardMountState:
    mounted: bool
    url: Optional[str]
    reason: str
