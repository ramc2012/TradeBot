"""RL state extraction from Market Profile + order-flow context.

State space:
    6 day_types × 3 buyer_fail × 3 seller_fail × 3 ib_size × 2 direction
    × 3 trade_imbalance × 3 book_pressure × 3 toxicity × 3 timing = 26,244 states.

This is intentionally still tabular. The state stays compact, interpretable, and
learnable from the paper/shadow dataset this repo already collects.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from auction_intelligence.schemas import MarketProfileSnapshot, OrderFlowSnapshot


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

SIGNED_FLOW_LABELS = ["bearish", "neutral", "bullish"]
QUALITY_LABELS = ["low", "mid", "high"]
STATE_VERSION = "v2"


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


def _signed_flow_bin(value: float, *, lower: float = -0.2, upper: float = 0.2) -> int:
    """Discretize signed order-flow metrics into bearish / neutral / bullish bins."""
    if value <= lower:
        return 0
    if value >= upper:
        return 2
    return 1


def _quality_bin(value: float, *, low: float, high: float) -> int:
    """Discretize a normalized [0, 1] quality metric into low / mid / high buckets."""
    if value < low:
        return 0
    if value < high:
        return 1
    return 2


def _coerce_bin(raw: object, *, default: int, low: int = 0, high: int = 2) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _legacy_state(parts: Iterable[str]) -> "MPState":
    """Parse the pre-order-flow five-part state key into the v2 shape."""
    values = [int(part) for part in parts]
    return MPState(
        day_type_idx=values[0],
        buyer_fail_bin=values[1],
        seller_fail_bin=values[2],
        ib_size_bin=values[3],
        direction=values[4],
        trade_imbalance_bin=1,
        book_pressure_bin=1,
        toxicity_bin=1,
        timing_bin=1,
    )


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
        trade_imbalance_bin: signed trade pressure bucket
        book_pressure_bin:   aggregated best-book pressure bucket
        toxicity_bin:        adverse microstructure bucket
        timing_bin:          execution timing confidence bucket
    """

    day_type_idx: int
    buyer_fail_bin: int
    seller_fail_bin: int
    ib_size_bin: int
    direction: int
    trade_imbalance_bin: int = 1
    book_pressure_bin: int = 1
    toxicity_bin: int = 1
    timing_bin: int = 1

    def to_key(self) -> str:
        return (
            f"{STATE_VERSION}_{self.day_type_idx}_{self.buyer_fail_bin}_"
            f"{self.seller_fail_bin}_{self.ib_size_bin}_{self.direction}_"
            f"{self.trade_imbalance_bin}_{self.book_pressure_bin}_"
            f"{self.toxicity_bin}_{self.timing_bin}"
        )

    @property
    def label(self) -> str:
        day_label = DAY_TYPE_LABELS[self.day_type_idx]
        dir_label = "bullish" if self.direction == 0 else "bearish"
        return (
            f"{day_label} bf={self.buyer_fail_bin} sf={self.seller_fail_bin} "
            f"ib={self.ib_size_bin} {dir_label} "
            f"trade={SIGNED_FLOW_LABELS[self.trade_imbalance_bin]} "
            f"book={SIGNED_FLOW_LABELS[self.book_pressure_bin]} "
            f"tox={QUALITY_LABELS[self.toxicity_bin]} "
            f"timing={QUALITY_LABELS[self.timing_bin]}"
        )

    @classmethod
    def from_key(cls, state_key: str) -> "MPState | None":
        """Parse both legacy and v2 state keys for policy introspection."""
        if not state_key:
            return None

        parts = state_key.split("_")
        try:
            if parts[0] == STATE_VERSION and len(parts) == 10:
                return cls(
                    day_type_idx=_coerce_bin(parts[1], default=5, high=5),
                    buyer_fail_bin=_coerce_bin(parts[2], default=0),
                    seller_fail_bin=_coerce_bin(parts[3], default=0),
                    ib_size_bin=_coerce_bin(parts[4], default=1),
                    direction=_coerce_bin(parts[5], default=0, high=1),
                    trade_imbalance_bin=_coerce_bin(parts[6], default=1),
                    book_pressure_bin=_coerce_bin(parts[7], default=1),
                    toxicity_bin=_coerce_bin(parts[8], default=1),
                    timing_bin=_coerce_bin(parts[9], default=1),
                )
            if len(parts) == 5:
                return _legacy_state(parts)
        except (TypeError, ValueError):
            return None
        return None


def describe_state_key(state_key: str) -> str:
    state = MPState.from_key(state_key)
    return state.label if state else state_key


def extract_state_from_bins(
    *,
    regime_label: str,
    direction: str,
    buyer_fail_bin: int,
    seller_fail_bin: int,
    ib_size_bin: int,
    trade_imbalance: float = 0.0,
    book_pressure: float = 0.0,
    toxicity_score: float = 0.5,
    timing_confidence: float = 0.5,
) -> MPState:
    """Build a discretized state from persisted bins and order-flow diagnostics."""
    dir_idx = 1 if direction == "SHORT" else 0
    return MPState(
        day_type_idx=_day_type_idx(regime_label, dir_idx),
        buyer_fail_bin=_coerce_bin(buyer_fail_bin, default=0),
        seller_fail_bin=_coerce_bin(seller_fail_bin, default=0),
        ib_size_bin=_coerce_bin(ib_size_bin, default=1),
        direction=dir_idx,
        trade_imbalance_bin=_signed_flow_bin(trade_imbalance),
        book_pressure_bin=_signed_flow_bin(book_pressure),
        toxicity_bin=_quality_bin(toxicity_score, low=0.35, high=0.65),
        timing_bin=_quality_bin(timing_confidence, low=0.45, high=0.7),
    )


def extract_state(
    regime_label: str,
    profile: "MarketProfileSnapshot",
    direction: str,
    order_flow: "OrderFlowSnapshot | None" = None,
) -> MPState:
    """Extract discretized MPState from live profile + regime data.

    Args:
        regime_label:  e.g. "trend_day", "balance", "failed_auction"
        profile:       current MarketProfileSnapshot
        direction:     "LONG" or "SHORT"
        order_flow:    optional live order-flow metrics used for RL timing/sizing
    """
    return extract_state_from_bins(
        regime_label=regime_label,
        direction=direction,
        buyer_fail_bin=_tail_bin(profile.buying_tail),
        seller_fail_bin=_tail_bin(profile.selling_tail),
        ib_size_bin=_ib_bin(profile.initial_balance_range, profile.close_price),
        trade_imbalance=float(getattr(order_flow, "trade_imbalance", 0.0) or 0.0),
        book_pressure=float(getattr(order_flow, "book_pressure", 0.0) or 0.0),
        toxicity_score=float(getattr(order_flow, "toxicity_score", 0.5) or 0.0),
        timing_confidence=float(getattr(order_flow, "timing_confidence", 0.5) or 0.0),
    )
