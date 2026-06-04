"""WS-1.2 — active reconciliation (alert on breaks, adopt orphans, partial fills)."""
import asyncio

from brokers.base import Order, OrderRequest
from live_engine.order_manager import LiveOrder, LiveOrderManager


class _FakeBroker:
    broker_name = "fake"

    def __init__(self, book):
        self._book = book

    async def get_order_book(self):
        return self._book


def _bo(order_id, status, *, qty=75, filled=0, symbol="NSE:X", action="BUY"):
    o = Order(
        order_id=order_id, symbol=symbol, exchange="NSE", action=action,
        order_type="MARKET", qty=qty, price=100.0, status=status,
    )
    o.filled_qty = filled  # dataclass instance accepts the forward-compat attr
    return o


def _tracked(om, local_id, broker_id, status="OPEN", qty=75):
    lo = LiveOrder(local_id, broker_id, OrderRequest(
        symbol="NSE:X", exchange="NSE", action="BUY", order_type="MARKET", qty=qty,
    ))
    lo.status = status
    om._orders[local_id] = lo
    return lo


def test_reconcile_updates_status_and_alerts_on_break(monkeypatch):
    async def run():
        alerts = []

        async def fake_audit(**kw):
            alerts.append(kw)

        monkeypatch.setattr("agentic_rag.audit_agent.record_audit_event", fake_audit)
        om = LiveOrderManager(_FakeBroker([_bo("B1", "REJECTED")]), object())
        lo = _tracked(om, "local1", "B1", status="OPEN")
        await om._reconcile_positions()
        assert lo.status == "REJECTED"
        assert any(
            a.get("event_type") == "order_reconcile" and a.get("severity") == "warning"
            for a in alerts
        )

    asyncio.run(run())


def test_reconcile_adopts_untracked_active_broker_order(monkeypatch):
    async def run():
        monkeypatch.setattr("agentic_rag.audit_agent.record_audit_event", _noop_audit)
        om = LiveOrderManager(_FakeBroker([_bo("B2", "OPEN")]), object())
        await om._reconcile_positions()
        assert "adopted-B2" in om._orders
        assert om._orders["adopted-B2"].broker_id == "B2"
        # second cycle must NOT duplicate the adoption
        await om._reconcile_positions()
        assert sum(1 for k in om._orders if k.startswith("adopted-")) == 1

    asyncio.run(run())


def test_reconcile_tracks_partial_fill(monkeypatch):
    async def run():
        monkeypatch.setattr("agentic_rag.audit_agent.record_audit_event", _noop_audit)
        om = LiveOrderManager(_FakeBroker([_bo("B3", "OPEN", qty=75, filled=30)]), object())
        lo = _tracked(om, "local3", "B3", status="OPEN", qty=75)
        await om._reconcile_positions()
        assert lo.filled_qty == 30

    asyncio.run(run())


def test_terminal_untracked_orders_are_not_adopted(monkeypatch):
    async def run():
        monkeypatch.setattr("agentic_rag.audit_agent.record_audit_event", _noop_audit)
        om = LiveOrderManager(_FakeBroker([_bo("B4", "CANCELLED"), _bo("B5", "REJECTED")]), object())
        await om._reconcile_positions()
        assert not any(k.startswith("adopted-") for k in om._orders)

    asyncio.run(run())


async def _noop_audit(**kw):
    return None
