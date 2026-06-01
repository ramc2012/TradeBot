"""Tests for the live-mark cross-wiring sanity guard.

The Fyers WS occasionally attributes one instrument's value to another option
symbol (observed: KPITTECH 770 PE reading 702 = NIFTY's value). overlay_live_marks
must reject a live mark that diverges absurdly from the agent's reference price
and keep the scan-cadence price instead.
"""
from __future__ import annotations

import asyncio

import pytest

from market_data import live_marks


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _patch_live(monkeypatch, value):
    from market_data.data_router import data_router

    async def fake_get_live_mark(symbol, *, max_age_seconds=30.0):
        return value

    monkeypatch.setattr(data_router, "get_live_mark", fake_get_live_mark)


def test_guard_rejects_crosswired_value(monkeypatch):
    # KPITTECH 770 PE: agent ref 31.3, WS returns NIFTY's 702.2 (22x).
    _patch_live(monkeypatch, 702.2)
    pos = [{"symbol": "OPT:KPITTECH:2026-06-30:770:PE", "current_price": 31.3, "entry_price": 25.0, "qty": 2125}]
    out = _run(live_marks.overlay_live_marks(pos, force_long=True))
    assert out[0]["current_price"] == 31.3  # kept the reference
    assert out[0]["mark_source"] == "scan_guarded"


def test_guard_accepts_normal_live_move(monkeypatch):
    # NIFTY 24000 PE: agent ref 692.3, WS returns 702.2 (1.4% move).
    _patch_live(monkeypatch, 702.2)
    pos = [{"symbol": "OPT:NIFTY:2026-06-30:24000:PE", "current_price": 692.3, "entry_price": 376.9, "qty": 163}]
    out = _run(live_marks.overlay_live_marks(pos, force_long=True))
    assert out[0]["current_price"] == 702.2  # applied the live tick
    assert out[0]["mark_source"] == "live_tick"


def test_guard_accepts_when_no_reference(monkeypatch):
    # No agent reference (current_price 0) → can't compare, accept the tick.
    _patch_live(monkeypatch, 50.0)
    pos = [{"symbol": "OPT:X:2026-06-30:100:CE", "current_price": 0.0, "entry_price": 40.0, "qty": 50}]
    out = _run(live_marks.overlay_live_marks(pos, force_long=True))
    assert out[0]["current_price"] == 50.0
    assert out[0]["mark_source"] == "live_tick"


def test_guard_rejects_low_outlier(monkeypatch):
    # Live mark far BELOW reference (e.g. a 0 / stale-zero leak) is also rejected.
    _patch_live(monkeypatch, 5.0)
    pos = [{"symbol": "OPT:NIFTY:2026-06-30:24000:PE", "current_price": 692.3, "entry_price": 376.9, "qty": 163}]
    out = _run(live_marks.overlay_live_marks(pos, force_long=True))
    assert out[0]["current_price"] == 692.3
    assert out[0]["mark_source"] == "scan_guarded"


def test_guard_pnl_uses_live_when_accepted(monkeypatch):
    _patch_live(monkeypatch, 100.0)
    pos = [{"symbol": "OPT:X:2026-06-30:100:CE", "current_price": 90.0, "entry_price": 80.0, "qty": 50}]
    out = _run(live_marks.overlay_live_marks(pos, force_long=True))
    # long premium: pnl = (100 - 80) * 50 = 1000
    assert out[0]["unrealized_pnl"] == 1000.0
