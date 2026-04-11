"""Paper trading order book — simulates fills on incoming ticks."""
from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
from typing import Callable, Dict, List, Optional
from loguru import logger
from brokers.base import Tick


# ─── Internal Order Representation ──────────────────────────────────────────

@dataclass
class PaperOrder:
    order_id: str
    symbol: str
    action: str          # BUY / SELL
    order_type: str      # MARKET / LIMIT / SL / SL_M
    qty: int
    price: Optional[float]   # limit price
    sl: Optional[float]      # stop price
    target: Optional[float]
    status: str = "OPEN"     # OPEN / FILLED / CANCELLED
    fill_price: Optional[float] = None
    fill_time: Optional[datetime] = None
    instrument_type: str = "CE"
    expiry: Optional[str] = None
    strike: Optional[float] = None
    option_type: Optional[str] = None
    session_id: Optional[str] = None
    signal_id: Optional[str] = None
    setup_type: Optional[str] = None
    entry_iv_pct: Optional[float] = None
    regime: Optional[str] = None
    # Bracket legs
    sl_order_id: Optional[str] = None
    target_order_id: Optional[str] = None
    parent_order_id: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)


# ─── Callbacks ───────────────────────────────────────────────────────────────

FillCallback = Callable[[PaperOrder], None]


# ─── Paper Order Book ────────────────────────────────────────────────────────

class PaperOrderBook:
    """Maintains virtual orders and simulates fills on tick updates."""

    SLIPPAGE_BPS = 5  # 5 basis points slippage on market orders

    def __init__(self, on_fill: Optional[FillCallback] = None):
        self._orders: Dict[str, PaperOrder] = {}
        self._on_fill = on_fill
        self._lock = RLock()

    def place_order(
        self,
        symbol: str,
        action: str,
        order_type: str,
        qty: int,
        price: Optional[float] = None,
        sl: Optional[float] = None,
        target: Optional[float] = None,
        instrument_type: str = "CE",
        expiry: Optional[str] = None,
        strike: Optional[float] = None,
        option_type: Optional[str] = None,
        session_id: Optional[str] = None,
        signal_id: Optional[str] = None,
        setup_type: Optional[str] = None,
        entry_iv_pct: Optional[float] = None,
        regime: Optional[str] = None,
        ltp: Optional[float] = None,
    ) -> PaperOrder:
        order_id = str(uuid.uuid4())
        order = PaperOrder(
            order_id=order_id,
            symbol=symbol,
            action=action,
            order_type=order_type,
            qty=qty,
            price=price,
            sl=sl,
            target=target,
            instrument_type=instrument_type,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            session_id=session_id,
            signal_id=signal_id,
            setup_type=setup_type,
            entry_iv_pct=entry_iv_pct,
            regime=regime,
        )

        # Market orders fill immediately if LTP provided
        if order_type == "MARKET" and ltp is not None:
            self._fill_market(order, ltp)
        else:
            with self._lock:
                self._orders[order_id] = order
            logger.debug(f"[PaperOB] Queued order {order_id[:8]} {action} {qty} {symbol}")

        return order

    def place_bracket_order(
        self,
        symbol: str,
        action: str,
        qty: int,
        ltp: float,
        sl: float,
        target: float,
        instrument_type: str = "CE",
        session_id: Optional[str] = None,
        **kwargs,
    ) -> tuple[PaperOrder, PaperOrder, PaperOrder]:
        """Place entry + SL + target as linked bracket orders."""
        entry = self.place_order(
            symbol=symbol, action=action, order_type="MARKET",
            qty=qty, sl=sl, target=target,
            instrument_type=instrument_type, session_id=session_id,
            ltp=ltp, **kwargs,
        )
        exit_action = "SELL" if action == "BUY" else "BUY"
        sl_order = self.place_order(
            symbol=symbol, action=exit_action, order_type="SL_M",
            qty=qty, sl=sl, instrument_type=instrument_type,
            session_id=session_id, **kwargs,
        )
        target_order = self.place_order(
            symbol=symbol, action=exit_action, order_type="LIMIT",
            qty=qty, price=target, instrument_type=instrument_type,
            session_id=session_id, **kwargs,
        )
        entry.sl_order_id = sl_order.order_id
        entry.target_order_id = target_order.order_id
        sl_order.parent_order_id = entry.order_id
        target_order.parent_order_id = entry.order_id
        return entry, sl_order, target_order

    def cancel_order(self, order_id: str) -> bool:
        with self._lock:
            order = self._orders.get(order_id)
            if not order or order.status != "OPEN":
                return False
            order.status = "CANCELLED"
            self._orders.pop(order_id, None)
            return True

    def get_order(self, order_id: str) -> Optional[PaperOrder]:
        with self._lock:
            return self._orders.get(order_id)

    def get_open_orders(self, session_id: Optional[str] = None) -> List[PaperOrder]:
        with self._lock:
            orders = [o for o in self._orders.values() if o.status == "OPEN"]
        if session_id:
            orders = [o for o in orders if o.session_id == session_id]
        return orders

    def try_fill(self, tick: Tick) -> List[PaperOrder]:
        """Called on each market tick. Returns list of newly filled orders."""
        filled = []
        with self._lock:
            orders = list(self._orders.values())
        for order in orders:
            if order.symbol != tick.symbol or order.status != "OPEN":
                continue
            ltp = tick.ltp
            fill_price = self._check_fill(order, ltp)
            if fill_price is not None:
                with self._lock:
                    current = self._orders.get(order.order_id)
                    if not current or current.status != "OPEN":
                        continue
                    current.fill_price = fill_price
                    current.fill_time = tick.timestamp or datetime.utcnow()
                    current.status = "FILLED"
                    del self._orders[current.order_id]
                filled.append(current)
                logger.info(
                    f"[PaperOB] FILLED {current.order_id[:8]} "
                    f"{current.action} {current.qty} {current.symbol} @ {fill_price:.2f}"
                )
                if self._on_fill:
                    self._on_fill(current)
                # Cancel opposing bracket leg
                self._cancel_bracket_sibling(current)
        return filled

    def _check_fill(self, order: PaperOrder, ltp: float) -> Optional[float]:
        otype = order.order_type
        action = order.action

        if otype == "MARKET":
            return self._apply_slippage(ltp, action)

        if otype == "LIMIT":
            price = order.price or 0
            if action == "BUY" and ltp <= price:
                return price
            if action == "SELL" and ltp >= price:
                return price

        if otype in ("SL", "SL_M"):
            sl_price = order.sl or 0
            if action == "BUY" and ltp >= sl_price:
                return self._apply_slippage(ltp, action) if otype == "SL_M" else sl_price
            if action == "SELL" and ltp <= sl_price:
                return self._apply_slippage(ltp, action) if otype == "SL_M" else sl_price

        return None

    def _apply_slippage(self, ltp: float, action: str) -> float:
        slip = ltp * (self.SLIPPAGE_BPS / 10_000)
        return ltp + slip if action == "BUY" else ltp - slip

    def _fill_market(self, order: PaperOrder, ltp: float):
        order.fill_price = self._apply_slippage(ltp, order.action)
        order.fill_time = datetime.utcnow()
        order.status = "FILLED"
        logger.info(
            f"[PaperOB] INSTANT FILL {order.order_id[:8]} "
            f"{order.action} {order.qty} {order.symbol} @ {order.fill_price:.2f}"
        )
        if self._on_fill:
            self._on_fill(order)

    def _cancel_bracket_sibling(self, filled_order: PaperOrder):
        """When one bracket leg fills, cancel the other."""
        parent_id = filled_order.parent_order_id
        if not parent_id:
            return
        parent = None
        with self._lock:
            orders = list(self._orders.values())
        for o in orders:
            if o.order_id == parent_id or o.parent_order_id == parent_id:
                if o.order_id != filled_order.order_id:
                    self.cancel_order(o.order_id)
