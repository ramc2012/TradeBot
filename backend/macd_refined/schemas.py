"""Typed schemas shared by the MACD Refined engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class MacdSignal:
    """A premium-MACD zero-cross entry trigger on one ATM contract."""
    underlying: str
    expiry: str                 # ISO date
    option_type: str            # CE | PE
    strike: float
    signal_time: str            # ISO timestamp (bar that produced the cross)
    premium_at_signal: float
    macd: float
    macd_signal: float
    histogram: float
    spot_at_signal: float
    days_to_expiry: float
    # Gate context
    iv: float
    iv_rank: Optional[float]
    realized_vol: Optional[float]
    daily_turnover_rupees: float
    lot_size: int
    tick_size: float
    trading_symbol: str = ""
    instrument_key: str = ""
    # Early-warning / directional context (spec §4)
    signal_kind: str = "macd_confirmation"   # macd_confirmation | volume_starter
    volume_surge_ratio: float = 0.0           # turnover / trailing baseline
    direction_bias: str = "neutral"           # up | down | neutral
    direction_confidence: float = 0.0
    # Gate verdicts
    passed_iv_gate: bool = False
    passed_liquidity_gate: bool = False
    passed_window_gate: bool = False
    passed_trend_gate: bool = True
    accepted: bool = False
    reasons: list[str] = field(default_factory=list)


@dataclass
class MacdTrade:
    """One simulated/booked long-premium trade, held to the window end or
    stopped at -50% (spec §8)."""
    underlying: str
    trading_symbol: str
    instrument_key: str
    option_type: str
    expiry: str
    strike: float
    lot_size: int
    signal_time: str
    entry_time: str
    entry_premium: float          # gross
    entry_fill_premium: float     # after slippage
    qty_lots: int
    qty_units: int
    notional_rupees: float
    book: str                     # CE | PE
    # Exit
    exit_time: Optional[str] = None
    exit_premium: Optional[float] = None
    exit_fill_premium: Optional[float] = None
    exit_reason: str = ""         # window_end | catastrophe_stop | expiry | starter_invalidation | profit_scale
    holding_bars: int = 0
    # P&L (net of slippage)
    pnl_rupees: float = 0.0
    return_pct: float = 0.0
    max_adverse_pct: float = 0.0
    max_favorable_pct: float = 0.0
    # Context
    entry_iv: float = 0.0
    iv_rank: Optional[float] = None
    direction_bias: str = "neutral"
    signal_kind: str = "macd_confirmation"

    @property
    def is_winner(self) -> bool:
        return self.pnl_rupees > 0


@dataclass(frozen=True)
class BookMetrics:
    """Per-book (CE or PE) or aggregate performance summary (spec §11C)."""
    book: str
    trades: int
    wins: int
    win_rate: float
    median_return_pct: float
    mean_return_pct: float
    total_pnl_rupees: float
    profit_factor: float
    pct_below_minus_50: float     # tail — % trades worse than -50%
    avg_hold_bars: float
    max_drawdown_pct: float


@dataclass(frozen=True)
class ContractSnapshot:
    """A live per-contract snapshot persisted for volume/turnover tracking."""
    captured_at: str
    underlying: str
    expiry: str
    expiry_kind: str              # current | next
    option_type: str
    strike: float
    moneyness: str                # ATM | ITM | OTM
    ltp: float
    volume: float                 # shares (spec §2)
    oi: float
    iv: float
    turnover_rupees: float        # volume × premium (spec §2)
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    lot_size: int = 1
    trading_symbol: str = ""
    instrument_key: str = ""
    spot_price: float = 0.0
