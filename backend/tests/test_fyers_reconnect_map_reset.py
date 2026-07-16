"""Routing fix — Fyers WS reconnect clears the SDK's topic_id→symbol maps.

The cross-symbol contamination root cause: the Fyers SDK builds index_sym /
scrips_sym / dp_sym (topic_id → symbol) and the resp accumulator once and never
clears them across a reconnect (its __on_close resets only symbol_token). A
reused topic_id then misresolves a lightweight update frame to the previous
session's symbol, emitting one instrument's OHLC under another's symbol. The
adapter now clears those maps on every reconnect, before re-subscription.
"""
from __future__ import annotations

import pytest

from brokers.fyers import FyersAdapter


class _FakeSDK:
    def __init__(self, **kwargs):
        self.on_connect = kwargs.get("on_connect")
        # Stale maps left over from a prior session.
        self.index_sym = {1: "NSE:NIFTYBANK-INDEX", 2: "NSE:NIFTYFMCG-INDEX"}
        self.scrips_sym = {5: "NSE:RELIANCE-EQ"}
        self.dp_sym = {7: "NSE:NIFTY50-INDEX"}
        self.resp = {"NSE:NIFTYBANK-INDEX": {"ltp": 57000.0}}
        self.symbol_token = {"NSE:NIFTYBANK-INDEX": 1}
        self.subscribed: list = []

    def connect(self):
        pass

    def subscribe(self, symbols=None, data_type=None):
        self.subscribed.append((symbols, data_type))


def test_reset_sdk_topic_maps_clears_resolution_state():
    client = _FakeSDK()
    FyersAdapter._reset_sdk_topic_maps(client)
    assert client.index_sym == {}
    assert client.scrips_sym == {}
    assert client.dp_sym == {}
    assert client.resp == {}


def test_reset_sdk_topic_maps_is_safe_when_attrs_missing():
    class _Bare:
        pass

    # Must not raise even if the SDK shape changes.
    FyersAdapter._reset_sdk_topic_maps(_Bare())


@pytest.mark.asyncio
async def test_reconnect_clears_maps_and_fires_callback(monkeypatch: pytest.MonkeyPatch):
    from fyers_apiv3.FyersWebsocket import data_ws

    monkeypatch.setattr(data_ws, "FyersDataSocket", _FakeSDK)

    adapter = FyersAdapter()
    adapter._access_token = "tok"

    reconnects: list[int] = []
    client = await adapter.subscribe_websocket(
        ["NSE:NIFTYBANK-INDEX"],
        lambda tick: None,
        on_reconnect_callback=lambda: reconnects.append(1),
    )

    on_connect = client.on_connect
    assert on_connect is not None

    # First connect: NOT a reconnect — maps must be left intact, no callback.
    on_connect()
    assert client.index_sym != {}
    assert reconnects == []

    # Second connect = reconnect: stale maps cleared AND router notified.
    on_connect()
    assert client.index_sym == {}
    assert client.scrips_sym == {}
    assert client.dp_sym == {}
    assert client.resp == {}
    assert reconnects == [1]
