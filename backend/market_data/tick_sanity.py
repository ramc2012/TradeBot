from __future__ import annotations

from brokers.base import Tick


BOOK_DEVIATION_THRESHOLD = 0.25


def _float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def validate_structural_tick(tick: Tick) -> str | None:
    """Return a reject reason for clearly corrupt ticks, else None."""
    ltp = _float(getattr(tick, "ltp", None))
    if ltp <= 0:
        return "nonpositive_price"

    volume = _float(getattr(tick, "volume", None))
    if volume < 0:
        return "negative_volume"

    bid = _float(getattr(tick, "bid", None))
    ask = _float(getattr(tick, "ask", None))
    if bid > 0 and ask > 0:
        if ask < bid:
            return "crossed_market"
        if ltp < (bid * (1.0 - BOOK_DEVIATION_THRESHOLD)) or ltp > (ask * (1.0 + BOOK_DEVIATION_THRESHOLD)):
            return "book_magnitude"

    return None
