"""WS-0.4a — LiveOrderManager client_order_id idempotency (claim-once)."""
import asyncio

import pytest

from brokers.base import OrderRequest, OrderResponse
from live_engine.order_manager import LiveOrderManager


class _FakeBroker:
    broker_name = "fake"

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    async def place_order(self, req: OrderRequest) -> OrderResponse:
        self.calls += 1
        if self.fail:
            raise RuntimeError("simulated broker/network failure")
        return OrderResponse(order_id=f"B{self.calls}", status="OPEN")

    async def get_order_book(self):
        return []


class _FakeRisk:
    def check_order(self, **kwargs):
        return (True, "ok")

    def on_position_opened(self, *a, **k):
        pass


def _req(coid=None) -> OrderRequest:
    return OrderRequest(
        symbol="NSE:X", exchange="NSE", action="BUY",
        order_type="MARKET", qty=1, price=10.0, client_order_id=coid,
    )


def test_same_client_order_id_is_not_resent():
    async def run():
        broker = _FakeBroker()
        om = LiveOrderManager(broker, _FakeRisk())
        o1 = await om.place_order(_req("c1"))
        o2 = await om.place_order(_req("c1"))
        assert broker.calls == 1          # only one send
        assert o1 is o2                   # same record returned
    asyncio.run(run())


def test_distinct_client_order_ids_each_send():
    async def run():
        broker = _FakeBroker()
        om = LiveOrderManager(broker, _FakeRisk())
        await om.place_order(_req("c1"))
        await om.place_order(_req("c2"))
        assert broker.calls == 2
    asyncio.run(run())


def test_failed_send_keeps_claim_and_is_not_resent():
    async def run():
        broker = _FakeBroker()
        broker.fail = True
        om = LiveOrderManager(broker, _FakeRisk())
        with pytest.raises(RuntimeError):
            await om.place_order(_req("c3"))
        assert broker.calls == 1
        # A retry with the SAME id must not hit the broker again (it may have landed).
        order = await om.place_order(_req("c3"))
        assert broker.calls == 1
        assert order.status == "SEND_FAILED"
    asyncio.run(run())


def test_no_client_id_falls_back_to_legacy_dup_guard():
    async def run():
        broker = _FakeBroker()
        om = LiveOrderManager(broker, _FakeRisk())
        await om.place_order(_req())
        with pytest.raises(ValueError):
            await om.place_order(_req())   # same symbol+action within 5s
    asyncio.run(run())
