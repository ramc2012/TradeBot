"""Live order manager — routes orders to broker, manages state machine."""
from __future__ import annotations
import asyncio
import time
import uuid
from datetime import datetime, timedelta
from threading import RLock
from typing import Dict, List, Optional

from loguru import logger

from brokers.base import BrokerAdapter, Order, OrderRequest, OrderResponse
from live_engine.risk_manager import RiskManager

try:  # WS-0.2 / WS-1.2 instrumentation — must never block order placement
    from core.metrics import (
        observe_fill_confirm as _observe_fill_confirm,
        observe_order_rtt as _observe_order_rtt,
    )
except Exception:  # pragma: no cover
    def _observe_order_rtt(*_a, **_k) -> None:  # type: ignore[misc]
        ...

    def _observe_fill_confirm(*_a, **_k) -> None:  # type: ignore[misc]
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
        self.filled_qty: int = 0   # WS-1.2 partial-fill tracking (broker-reported filled units)
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
        """WS-1.2 active reconciliation: sync the broker order book against internal
        state, track partial fills, alert on breaks, and adopt untracked broker
        orders so a crash-after-place can't leave an orphan position."""
        try:
            broker_orders = await self.broker.get_order_book()
        except Exception as e:
            logger.error(f"[LiveOM] Reconcile: get_order_book failed: {e}")
            return

        broker_map = {o.order_id: o for o in broker_orders}
        with self._orders_lock:
            live_orders = list(self._orders.values())
            tracked_broker_ids = {o.broker_id for o in live_orders if o.broker_id}

        # 1) Update tracked orders; track partial fills; alert on breaks.
        for live_order in live_orders:
            if not live_order.broker_id:
                continue
            bo = broker_map.get(live_order.broker_id)
            if bo is None:
                continue
            prev_status = live_order.status
            bo_filled = int(getattr(bo, "filled_qty", 0) or 0)
            if bo.status == prev_status and bo_filled == live_order.filled_qty:
                continue
            live_order.status = bo.status
            live_order.fill_price = bo.fill_price
            live_order.fill_time = bo.fill_time
            if bo_filled:
                live_order.filled_qty = bo_filled
            logger.info(
                f"[LiveOM] Reconcile {live_order.local_id[:8]}: {prev_status}→{bo.status} "
                f"filled={live_order.filled_qty}/{getattr(live_order.order, 'qty', 0)}"
            )
            if str(bo.status).upper() == "FILLED" and str(prev_status).upper() != "FILLED":
                self._record_fill_latency(live_order)
            await self._alert_reconcile_break(live_order, prev_status, bo)

        # 2) Adopt untracked, still-active broker orders (orphan-position guard).
        for bo in broker_orders:
            if bo.order_id in tracked_broker_ids:
                continue
            if str(bo.status).upper() not in ("OPEN", "PENDING", "TRIGGER_PENDING", "FILLED"):
                continue
            await self._adopt_broker_order(bo)

    def _record_fill_latency(self, live_order: "LiveOrder") -> None:
        """WS-1.2 — observe decision→fill latency (nomad_fill_confirm_seconds)."""
        try:
            ft = live_order.fill_time
            ca = live_order.created_at
            if ft is None or ca is None:
                return
            if ft.tzinfo is not None and ca.tzinfo is None:
                ca = ca.replace(tzinfo=ft.tzinfo)
            elif ft.tzinfo is None and ca.tzinfo is not None:
                ft = ft.replace(tzinfo=ca.tzinfo)
            secs = (ft - ca).total_seconds()
            if secs >= 0:
                _observe_fill_confirm("live", secs)
        except Exception:
            pass

    async def _alert_reconcile_break(self, live_order: "LiveOrder", prev_status: str, bo: Order) -> None:
        """Emit an audit event on a reconcile transition. REJECTED/CANCELLED/EXPIRED
        are 'breaks' (warning → paged via the audit→Telegram bridge); FILLED is normal
        (info). Never raises into the reconcile loop."""
        new_status = str(bo.status).upper()
        severity = "warning" if new_status in ("REJECTED", "CANCELLED", "EXPIRED") else "info"
        try:
            from agentic_rag.audit_agent import record_audit_event

            await record_audit_event(
                market="live",
                strategy_key="live_order_manager",
                event_type="order_reconcile",
                actor="live_order_manager",
                severity=severity,
                symbol=getattr(live_order.order, "symbol", None),
                previous_state=prev_status,
                new_state=bo.status,
                message=(
                    f"order {live_order.broker_id} {prev_status}->{bo.status} "
                    f"filled={live_order.filled_qty}/{getattr(live_order.order, 'qty', 0)}"
                ),
            )
        except Exception:
            pass

    async def _adopt_broker_order(self, bo: Order) -> None:
        """WS-1.2 — adopt an untracked broker order into local state (so kill-switch /
        position views see it) and alert: an order at the broker we don't track is a
        crash-after-place orphan (or out-of-band order) that represents live risk."""
        local_id = f"adopted-{bo.order_id}"
        try:
            with self._orders_lock:
                if local_id in self._orders:
                    return
            synth = OrderRequest(
                symbol=bo.symbol,
                exchange=getattr(bo, "exchange", "NSE"),
                action=bo.action,
                order_type=getattr(bo, "order_type", "MARKET"),
                qty=int(getattr(bo, "qty", 0) or 0),
                price=getattr(bo, "price", None),
                instrument_type=getattr(bo, "instrument_type", "CE"),
            )
            lo = LiveOrder(local_id=local_id, broker_id=bo.order_id, order=synth)
            lo.status = bo.status
            lo.fill_price = bo.fill_price
            lo.fill_time = bo.fill_time
            lo.filled_qty = int(getattr(bo, "filled_qty", 0) or 0)
            with self._orders_lock:
                self._orders[local_id] = lo
            logger.warning(
                f"[LiveOM] Adopted untracked broker order {bo.order_id} "
                f"({bo.symbol} {bo.action} {bo.status})"
            )
            from agentic_rag.audit_agent import record_audit_event

            await record_audit_event(
                market="live",
                strategy_key="live_order_manager",
                event_type="orphan_order_adopted",
                actor="live_order_manager",
                severity="warning",
                symbol=bo.symbol,
                new_state=bo.status,
                message=(
                    f"adopted untracked broker order {bo.order_id} "
                    f"{bo.action} {bo.qty} {bo.symbol} status={bo.status}"
                ),
            )
        except Exception as e:
            logger.error(f"[LiveOM] Adopt failed for {getattr(bo, 'order_id', '?')}: {e}")
