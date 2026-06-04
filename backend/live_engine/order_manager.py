"""Live order manager — routes orders to broker, manages state machine."""
from __future__ import annotations
import asyncio
import time
import uuid
from datetime import datetime, timedelta
from threading import RLock
from typing import Dict, List, Optional

from loguru import logger

from brokers.base import BrokerAdapter, OrderRequest, OrderResponse
from live_engine.risk_manager import RiskManager

try:  # WS-0.2 instrumentation — must never block order placement
    from core.metrics import observe_order_rtt as _observe_order_rtt
except Exception:  # pragma: no cover
    def _observe_order_rtt(*_a, **_k) -> None:  # type: ignore[misc]
        ...


# ── Duplicate guard ──────────────────────────────────────────────────────────

class DuplicateGuard:
    """Prevents same symbol+action within WINDOW_SECS."""
    WINDOW_SECS = 5

    def __init__(self):
        self._cache: Dict[str, datetime] = {}

    def is_duplicate(self, symbol: str, action: str) -> bool:
        key = f"{symbol}:{action}"
        last = self._cache.get(key)
        if last and (datetime.utcnow() - last).total_seconds() < self.WINDOW_SECS:
            return True
        self._cache[key] = datetime.utcnow()
        return False


# ── Order State ──────────────────────────────────────────────────────────────

class LiveOrder:
    def __init__(self, local_id: str, broker_id: Optional[str], order: OrderRequest):
        self.local_id = local_id
        self.broker_id = broker_id
        self.order = order
        self.client_order_id: Optional[str] = getattr(order, "client_order_id", None)
        self.status = "PENDING"    # PENDING → OPEN → FILLED / REJECTED / CANCELLED / SEND_FAILED
        self.fill_price: Optional[float] = None
        self.fill_time: Optional[datetime] = None
        self.created_at = datetime.utcnow()


# ── Live Order Manager ───────────────────────────────────────────────────────

class LiveOrderManager:
    """Routes live orders to broker with pre-trade checks and state tracking."""

    RECONCILE_INTERVAL = 30  # seconds

    def __init__(self, broker: BrokerAdapter, risk_manager: RiskManager):
        self.broker = broker
        self.risk = risk_manager
        self._orders: Dict[str, LiveOrder] = {}
        self._by_client_id: Dict[str, LiveOrder] = {}  # WS-0.4a idempotency claims
        self._dup_guard = DuplicateGuard()
        self._reconcile_task: Optional[asyncio.Task] = None
        self._kill_switch_active = False
        self._orders_lock = RLock()

    async def start_reconciliation(self):
        """Start background position reconciliation loop."""
        self._reconcile_task = asyncio.create_task(self._reconcile_loop())

    async def stop_reconciliation(self):
        if self._reconcile_task:
            self._reconcile_task.cancel()

    async def place_order(self, order_req: OrderRequest) -> LiveOrder:
        """Pre-check → place → track.

        Idempotent on ``client_order_id`` (WS-0.4a): a request carrying an id
        already seen this session returns the existing order WITHOUT re-sending —
        this neutralises double-clicks and automatic retries, including the
        dangerous 'send timed out but may have landed' case. The claim is reserved
        before the broker call and never released, so a failed send is not silently
        re-tried. To genuinely retry, mint a NEW client_order_id. Callers that omit
        an id fall back to the legacy best-effort 5s symbol+action guard.
        """
        if self._kill_switch_active:
            raise RuntimeError("Kill switch active — trading disabled")

        coid = getattr(order_req, "client_order_id", None)
        if coid:
            with self._orders_lock:
                existing = self._by_client_id.get(coid)
            if existing is not None:
                logger.info(
                    f"[LiveOM] Idempotent: client_order_id={coid} already "
                    f"{existing.status} — not re-sending."
                )
                return existing
        else:
            # Legacy best-effort guard for callers that don't supply an id.
            if self._dup_guard.is_duplicate(order_req.symbol, order_req.action):
                raise ValueError(
                    f"Duplicate order: {order_req.symbol} {order_req.action} within 5 seconds"
                )

        # Risk check
        price = order_req.price or 0
        allowed, reason = self.risk.check_order(
            symbol=order_req.symbol,
            action=order_req.action,
            qty=order_req.qty,
            price=price,
            sl=order_req.sl,
        )
        if not allowed:
            raise ValueError(f"Risk check failed: {reason}")

        local_id = str(uuid.uuid4())
        live_order = LiveOrder(local_id=local_id, broker_id=None, order=order_req)
        if coid:
            with self._orders_lock:
                # Re-check under lock (a concurrent call may have claimed it during
                # the risk gate), then reserve so concurrent retries collapse to one send.
                existing = self._by_client_id.get(coid)
                if existing is not None:
                    return existing
                self._by_client_id[coid] = live_order

        _broker_name = getattr(self.broker, "broker_name", None) or type(self.broker).__name__
        _t0 = time.perf_counter()
        try:
            response: OrderResponse = await self.broker.place_order(order_req)
            _observe_order_rtt(_broker_name, "ok", time.perf_counter() - _t0)
            live_order.broker_id = response.order_id
            live_order.status = response.status
            with self._orders_lock:
                self._orders[local_id] = live_order
            self.risk.on_position_opened(order_req.symbol, price * order_req.qty)
            logger.info(
                f"[LiveOM] Placed {order_req.action} {order_req.qty} {order_req.symbol} "
                f"→ broker_id={response.order_id}" + (f" coid={coid}" if coid else "")
            )
            return live_order
        except Exception as e:
            _observe_order_rtt(_broker_name, "error", time.perf_counter() - _t0)
            # Keep the coid claim (do NOT release): the send may have reached the
            # broker. A repeat with this id returns this record instead of risking a
            # second order; deliberate retries must use a new id.
            live_order.status = "SEND_FAILED"
            logger.error(f"[LiveOM] Place order failed (coid={coid}): {e}")
            raise

    async def cancel_order(self, local_id: str) -> bool:
        with self._orders_lock:
            order = self._orders.get(local_id)
        if not order:
            return False
        success = await self.broker.cancel_order(order.broker_id or "")
        if success:
            order.status = "CANCELLED"
        return success

    async def kill_switch(self) -> int:
        """Cancel all open orders. Returns count cancelled."""
        self._kill_switch_active = True
        cancelled = 0
        with self._orders_lock:
            open_orders = [o for o in self._orders.values() if o.status in ("PENDING", "OPEN")]
        tasks = [self.broker.cancel_order(o.broker_id or "") for o in open_orders if o.broker_id]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for o, result in zip(open_orders, results):
            if result is True:
                o.status = "CANCELLED"
                cancelled += 1
        logger.warning(f"[LiveOM] Kill switch: cancelled {cancelled} orders")
        return cancelled

    def reset_kill_switch(self):
        self._kill_switch_active = False
        logger.info("[LiveOM] Kill switch reset")

    def get_open_orders(self) -> List[LiveOrder]:
        with self._orders_lock:
            return [o for o in self._orders.values() if o.status in ("PENDING", "OPEN")]

    # ── Reconciliation ────────────────────────────────────────────────────

    async def _reconcile_loop(self):
        while True:
            try:
                await asyncio.sleep(self.RECONCILE_INTERVAL)
                await self._reconcile_positions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[LiveOM] Reconciliation error: {e}")

    async def _reconcile_positions(self):
        """Sync broker order book against internal state."""
        try:
            broker_orders = await self.broker.get_order_book()
            broker_map = {o.order_id: o for o in broker_orders}
            with self._orders_lock:
                live_orders = list(self._orders.values())
            for live_order in live_orders:
                if not live_order.broker_id:
                    continue
                broker_order = broker_map.get(live_order.broker_id)
                if broker_order:
                    if broker_order.status != live_order.status:
                        logger.info(
                            f"[LiveOM] Reconcile: {live_order.local_id[:8]} "
                            f"{live_order.status} → {broker_order.status}"
                        )
                        live_order.status = broker_order.status
                        live_order.fill_price = broker_order.fill_price
                        live_order.fill_time = broker_order.fill_time
        except Exception as e:
            logger.error(f"[LiveOM] Reconcile failed: {e}")
