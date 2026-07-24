"""WS-1.4 (2026-07-24) — Fyers WS recovery: MCX-evening loss hook + de-storm gate.

Two root-cause fixes, each with a discriminating test:

  A. **Socket-loss recovery in ANY session.** reconnect=False means the router
     owns reconnection. Before this change the SDK's on_close/on_error only
     logged, so a broker-side drop during MCX-EVENING hours — outside the
     required-feed watchdog's NSE-only (09:15-15:30 IST) force-reconnect branch
     — had NO recovery trigger. Tue 2026-07-21: WS lost 16:22 IST, feed silent
     7h14m. The socket now fires an ``on_ws_lost`` hook that schedules a
     router-owned reconnect independent of the NSE gate.

  B. **De-storm the reconnect.** A reconnect clears _tick_buffer and re-subscribes
     ~169 symbols; that can't warm inside the 30s watchdog interval, so the
     staleness check reads the brand-new socket as (N/N) stale and fires again —
     the self-perpetuating storm (Thu 2026-07-23, 231 reconnects). A
     post-resubscribe warm-up gate paces reconnects to at most one per warm-up
     window so the fresh socket can warm its buffer.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from market_data.data_router import DataRouter


# ── A. fyers layer: on_close/on_error must fire the loss hook ────────────────

@pytest.mark.asyncio
async def test_fyers_close_and_error_fire_ws_lost_hook(monkeypatch):
    """Without the fix on_close/on_error only log; the loss hook is never called."""
    from fyers_apiv3.FyersWebsocket import data_ws
    from brokers.fyers import FyersAdapter

    captured: dict = {}

    class _FakeSDK:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def connect(self):
            pass

        def subscribe(self, symbols=None, data_type=None):
            pass

    monkeypatch.setattr(data_ws, "FyersDataSocket", _FakeSDK)

    adapter = FyersAdapter()
    adapter._access_token = "tok"

    losses: list[str] = []
    await adapter.subscribe_websocket(
        ["NSE:NIFTY50-INDEX"],
        lambda tick: None,
        on_ws_lost=lambda: losses.append("lost"),
    )

    on_close = captured["on_close"]
    on_error = captured["on_error"]

    on_close()
    assert losses == ["lost"]          # a close drives recovery
    on_error("boom")
    assert losses == ["lost", "lost"]  # an error drives recovery too


@pytest.mark.asyncio
async def test_fyers_ws_lost_hook_failure_never_kills_ws_thread(monkeypatch):
    """A raising hook must be swallowed — the WS thread must never die."""
    from fyers_apiv3.FyersWebsocket import data_ws
    from brokers.fyers import FyersAdapter

    captured: dict = {}

    class _FakeSDK:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def connect(self):
            pass

        def subscribe(self, symbols=None, data_type=None):
            pass

    monkeypatch.setattr(data_ws, "FyersDataSocket", _FakeSDK)

    adapter = FyersAdapter()
    adapter._access_token = "tok"

    def _boom():
        raise RuntimeError("hook exploded")

    await adapter.subscribe_websocket(
        ["NSE:NIFTY50-INDEX"], lambda tick: None, on_ws_lost=_boom
    )
    # Must not raise.
    captured["on_close"]()
    captured["on_error"]("x")


# ── A. router layer: loss recovers OUTSIDE NSE hours; fenced by generation ───

class _CaptureBroker:
    broker_name = "fyers"

    def __init__(self):
        self.captured: dict = {}

    async def subscribe_websocket(
        self,
        symbols,
        on_tick_callback,
        on_depth_callback=None,
        on_reconnect_callback=None,
        on_ws_lost=None,
    ):
        self.captured["on_ws_lost"] = on_ws_lost
        return object()  # opaque fake ws client


async def _subscribe_once(monkeypatch, dr: DataRouter, broker: _CaptureBroker):
    # Force the stream window open so subscribe actually builds the socket,
    # regardless of the wall clock the suite runs at.
    monkeypatch.setattr(DataRouter, "_stream_window_open", staticmethod(lambda now=None: True))
    dr.set_broker(broker)
    await dr.subscribe(["NSE:NIFTY50-INDEX"])


@pytest.mark.asyncio
async def test_ws_loss_schedules_reconnect_outside_nse_hours(monkeypatch):
    """The Tuesday fix: a socket loss during MCX-evening (NSE CLOSED) must still
    schedule a reconnect. The required-feed watchdog can't do this — it's gated
    to NSE 09:15-15:30 — so the loss hook is the only recovery path there."""
    dr = DataRouter()
    broker = _CaptureBroker()
    await _subscribe_once(monkeypatch, dr, broker)

    loss = broker.captured.get("on_ws_lost")
    assert loss is not None, "router did not wire a socket-loss hook"

    # Simulate MCX-evening: NSE index session is CLOSED.
    monkeypatch.setattr(DataRouter, "_is_index_market_open", staticmethod(lambda now=None: False))

    scheduled: list[int] = []
    monkeypatch.setattr(dr, "_schedule_reconnect", lambda: scheduled.append(1))

    loss()                    # fires the loop-thread hop
    await asyncio.sleep(0)    # let call_soon_threadsafe run
    assert scheduled == [1], "loss during MCX-evening did not schedule a reconnect"


@pytest.mark.asyncio
async def test_ws_loss_is_generation_fenced(monkeypatch):
    """A retired socket's late close must NOT reconnect the live socket."""
    dr = DataRouter()
    broker = _CaptureBroker()
    await _subscribe_once(monkeypatch, dr, broker)

    loss = broker.captured.get("on_ws_lost")
    assert loss is not None

    scheduled: list[int] = []
    monkeypatch.setattr(dr, "_schedule_reconnect", lambda: scheduled.append(1))

    dr._ws_generation += 1    # this socket has been retired
    loss()
    await asyncio.sleep(0)
    assert scheduled == [], "a retired socket's loss must not schedule a reconnect"


# ── B. de-storm: post-resubscribe warm-up gate ──────────────────────────────

@pytest.mark.asyncio
async def test_warmup_gate_blocks_then_allows(monkeypatch):
    """Within the warm-up window _schedule_reconnect is a no-op (lets the fresh
    socket warm its buffer); past it, a reconnect fires. Without the gate the
    first call fires immediately and the storm perpetuates."""
    dr = DataRouter()
    dr._loop = asyncio.get_running_loop()

    fired: list[int] = []

    async def _fake_reconnect():
        fired.append(1)

    monkeypatch.setattr(dr, "_reconnect_if_stale", _fake_reconnect)

    # A resubscribe just completed → inside warm-up → blocked.
    dr._last_resubscribe_at = datetime.now(timezone.utc)
    dr._schedule_reconnect()
    await asyncio.sleep(0)
    assert fired == [], "reconnect fired inside the warm-up window (storm not de-stormed)"

    # Warm-up has elapsed → allowed.
    dr._last_resubscribe_at = datetime.now(timezone.utc) - timedelta(
        seconds=dr._post_resubscribe_warmup_seconds + 1
    )
    dr._schedule_reconnect()
    await asyncio.sleep(0)
    assert fired == [1], "reconnect did not fire after the warm-up window elapsed"


def test_warmup_window_exceeds_watchdog_interval():
    """The gate only breaks the storm if it outlasts one watchdog cycle —
    otherwise a still-cold socket would be re-torn-down on the next tick."""
    dr = DataRouter()
    assert dr._post_resubscribe_warmup_seconds > dr._watchdog_interval_seconds
