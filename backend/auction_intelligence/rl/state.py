"""RL state extraction from Market Profile context.

State space: 6 day_types × 3 buyer_fail × 3 seller_fail × 3 ib_size × 2 direction = 324 states.
Each dimension is discretized from live MarketProfileSnapshot + RegimeAssessment data.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from auction_intelligence.schemas import MarketProfileSnapshot, RegimeAssessment


# --- Day-type mapping -------------------------------------------------------
# Regime labels → day type index (0-5)
_REGIME_TO_DAY_TYPE: dict[str, int] = {
    "trend_day": 0,            # TREND_UP / TREND_DN depends on direction
    "trend_continuation": 0,
    "breakout_acceptance": 0,
    "balance": 2,              # NORMAL
    "developing_balance": 2,
    "rotational_day": 4,       # NEUTRAL
    "neutral_extreme": 4,
    "failed_auction": 3,       # FAILED_AUCTION
    "breakout_rejection": 3,
    "reversal": 3,
    "no_trade": 5,             # NON_TREND
}

DAY_TYPE_LABELS = [
    "TREND_UP",      # 0
    "TREND_DN",      # 1
    "NORMAL",        # 2
    "FAILED_AUCTION",# 3
    "NEUTRAL",       # 4
    "NON_TREND",     # 5
]


def _day_type_idx(regime_label: str, direction: int) -> int:
    """Map regime label + direction to day_type index (0-5)."""
    base = _REGIME_TO_DAY_TYPE.get(regime_label, 5)
    # TREND_UP (0) vs TREND_DN (1) only when it's a trend-class regime
    if base == 0 and direction == 1:  # bearish trend
        return 1
    return base


def _tail_bin(tail: list[Any]) -> int:
    """Discretize tail length: 0=low(0-1), 1=mid(2-3), 2=high(4+)."""
    n = len(tail) if tail else 0
    if n <= 1:
        return 0
    if n <= 3:
        return 1
    return 2


def _ib_bin(ib_range: float, close_price: float) -> int:
    """Discretize IB range as % of price: 0=small(<1%), 1=normal(1-2%), 2=large(>2%)."""
    if close_price <= 0:
        return 1
    pct = (ib_range / close_price) * 100.0
    if pct < 1.0:
        return 0
    if pct <= 2.0:
        return 1
    return 2


@dataclass(frozen=True)
class MPState:
    """Discretized Market Profile state for Q-learning.

    Attributes:
        day_type_idx:   0=TREND_UP, 1=TREND_DN, 2=NORMAL, 3=FAILED_AUCTION,
                        4=NEUTRAL, 5=NON_TREND
        buyer_fail_bin: buying_tail length bucket (0=low, 1=mid, 2=high)
        seller_fail_bin:selling_tail length bucket (0=low, 1=mid, 2=high)
        ib_size_bin:    initial_balance_range / price bucket (0=small, 1=normal, 2=large)
        direction:      0=bullish/LONG, 1=bearish/SHORT
    """

    day_type_idx: int
    buyer_fail_bin: int
    seller_fail_bin: int
    ib_size_bin: int
    direction: int

    def to_key(self) -> str:
        return (
            f"{self.day_type_idx}_{self.buyer_fail_bin}_"
            f"{self.seller_fail_bin}_{self.ib_size_bin}_{self.direction}"
        )

    @property
    def label(self) -> str:
        day_label = DAY_TYPE_LABELS[self.day_type_idx]
        dir_label = "bullish" if self.direction == 0 else "bearish"
        return (
            f"{day_label} bf={self.buyer_fail_bin} sf={self.seller_fail_bin} "
            f"ib={self.ib_size_bin} {dir_label}"
        )


def extract_state(
    regime_label: str,
    profile: "MarketProfileSnapshot",
    direction: str,
) -> MPState:
    """Extract discretized MPState from live profile + regime data.

    Args:
        regime_label:  e.g. "trend_day", "balance", "failed_auction"
        profile:       current MarketProfileSnapshot
        direction:     "LONG" or "SHORT"
    """
    dir_idx = 1 if direction == "SHORT" else 0
    return MPState(
        day_type_idx=_day_type_idx(regime_label, dir_idx),
        buyer_fail_bin=_tail_bin(profile.buying_tail),
        seller_fail_bin=_tail_bin(profile.selling_tail),
        ib_size_bin=_ib_bin(profile.initial_balance_range, profile.close_price),
        direction=dir_idx,
    )
