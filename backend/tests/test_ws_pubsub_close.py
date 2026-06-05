"""Live-data fix: WS pub/sub handlers must RELEASE their Redis connection on
disconnect. unsubscribe() alone leaks the connection; a market day of UI
reconnects then exhausts Redis maxclients ('max number of clients reached'),
which broke tick pub/sub on 2026-06-05.
"""
import asyncio

from api.websockets.ticks import _close_pubsub


def test_close_pubsub_calls_aclose():
    class _PS:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    ps = _PS()
    asyncio.run(_close_pubsub(ps))
    assert ps.closed


def test_close_pubsub_falls_back_to_sync_close():
    class _PS:
        def __init__(self):
            self.closed = False

        def close(self):  # no aclose attribute
            self.closed = True

    ps = _PS()
    asyncio.run(_close_pubsub(ps))
    assert ps.closed


def test_close_pubsub_never_raises_without_closer():
    asyncio.run(_close_pubsub(object()))  # must degrade silently, not raise
