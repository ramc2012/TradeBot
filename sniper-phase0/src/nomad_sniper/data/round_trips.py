"""Pair buy and sell legs into round-trip trades (the unit of P&L analysis).

NSE F&O retail accounts use FIFO accounting by default. We replicate that: when a sell
arrives for symbol S, it closes the oldest open buy at the recorded buy price.

Round-trip = (entry_leg, exit_leg). gross_pnl = qty * (exit_price - entry_price) for longs;
flipped sign for shorts. This module produces gross only — costs are added by the cost model
in `nomad_sniper.labels.cost_model`.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from nomad_sniper.data.trades import Trade
from nomad_sniper.utils.logging import get_logger

log = get_logger()


@dataclass
class RoundTrip:
    symbol: str
    exchange: str
    direction: Literal["long", "short"]
    entry_at: datetime
    exit_at: datetime
    entry_price: float
    exit_price: float
    quantity: int
    entry_trade_id: str
    exit_trade_id: str

    @property
    def gross_pnl(self) -> float:
        sign = 1.0 if self.direction == "long" else -1.0
        return sign * self.quantity * (self.exit_price - self.entry_price)

    @property
    def holding_seconds(self) -> int:
        return int((self.exit_at - self.entry_at).total_seconds())

    @property
    def notional(self) -> float:
        return self.quantity * (self.entry_price + self.exit_price) / 2.0


def pair_round_trips(trades: list[Trade]) -> list[RoundTrip]:
    """FIFO match buys and sells per symbol into round-trip trades.

    Strict rules:
        - Same symbol matches only same symbol (no cross-strike).
        - We process strictly in execution-time order.
        - If a sell arrives with no open buy for that symbol, we open a SHORT and wait
          for a buy to close it. (F&O is freely shortable on NSE.)
        - At end of input, any unpaired legs are logged and discarded.
    """
    trades_sorted = sorted(trades, key=lambda t: t.executed_at)
    open_long: dict[str, deque[Trade]] = defaultdict(deque)
    open_short: dict[str, deque[Trade]] = defaultdict(deque)
    round_trips: list[RoundTrip] = []

    for t in trades_sorted:
        if t.trade_type == "buy":
            # Closes a short if one is open, else opens a long
            if open_short[t.symbol]:
                opener = _pop_with_quantity(open_short[t.symbol], t.quantity)
                round_trips.extend(_build_pairs(opener, t, "short"))
                # Push back any remaining buy quantity as a new long
                leftover = t.quantity - sum(o.quantity for o in opener)
                if leftover > 0:
                    open_long[t.symbol].append(_clone_with_qty(t, leftover))
            else:
                open_long[t.symbol].append(t)
        else:  # sell
            if open_long[t.symbol]:
                opener = _pop_with_quantity(open_long[t.symbol], t.quantity)
                round_trips.extend(_build_pairs(opener, t, "long"))
                leftover = t.quantity - sum(o.quantity for o in opener)
                if leftover > 0:
                    open_short[t.symbol].append(_clone_with_qty(t, leftover))
            else:
                open_short[t.symbol].append(t)

    # Report unpaired
    unpaired_long = sum(len(q) for q in open_long.values())
    unpaired_short = sum(len(q) for q in open_short.values())
    if unpaired_long or unpaired_short:
        log.warning(
            f"Unpaired legs at end: {unpaired_long} open longs, {unpaired_short} open shorts. "
            "These are dropped from Phase 0 analysis."
        )

    log.info(f"Paired {len(round_trips)} round trips from {len(trades_sorted)} legs")
    return round_trips


def _pop_with_quantity(q: deque[Trade], need: int) -> list[Trade]:
    """Pop trades from the front of `q` until cumulative quantity >= need.

    May split the last popped trade. Returns the list of (possibly split) openers actually
    consumed by this closing leg.
    """
    consumed: list[Trade] = []
    remaining = need
    while remaining > 0 and q:
        head = q[0]
        if head.quantity <= remaining:
            consumed.append(head)
            remaining -= head.quantity
            q.popleft()
        else:
            # Partial fill: split the head
            consumed.append(_clone_with_qty(head, remaining))
            q[0] = _clone_with_qty(head, head.quantity - remaining)
            remaining = 0
    return consumed


def _clone_with_qty(t: Trade, qty: int) -> Trade:
    return Trade(
        trade_id=t.trade_id,
        order_id=t.order_id,
        symbol=t.symbol,
        exchange=t.exchange,
        segment=t.segment,
        trade_type=t.trade_type,
        quantity=qty,
        price=t.price,
        executed_at=t.executed_at,
        trade_date=t.trade_date,
    )


def _build_pairs(openers: list[Trade], closer: Trade, direction: str) -> list[RoundTrip]:
    """Build one or more RoundTrips from a closing leg and the openers it matched."""
    pairs: list[RoundTrip] = []
    remaining = closer.quantity
    for op in openers:
        qty = min(op.quantity, remaining)
        pairs.append(
            RoundTrip(
                symbol=closer.symbol,
                exchange=closer.exchange,
                direction=direction,  # type: ignore[arg-type]
                entry_at=op.executed_at,
                exit_at=closer.executed_at,
                entry_price=op.price,
                exit_price=closer.price,
                quantity=qty,
                entry_trade_id=op.trade_id,
                exit_trade_id=closer.trade_id,
            )
        )
        remaining -= qty
    return pairs
