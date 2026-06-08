"""Anti-whipsaw gate for directional_options paper store.

Two guards, both config-driven and off-by-default in the constructor:
  - wall-clock floor on min-hold (so 1m positions can't flatten in ~3-6 min)
  - per-symbol re-entry cooldown after a flat_signal / signal_flip close
    (stops the open->fade->close->reopen churn: 16 closes / 3 symbols, all
    whipsaw, on 2026-06-08).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from directional_options.paper import DirectionalOptionsPaperStore, _has_satisfied_min_hold


def _ago_iso(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _store(tmp_path, **kw) -> DirectionalOptionsPaperStore:
    return DirectionalOptionsPaperStore(tmp_path, **kw)


def test_min_hold_wall_clock_floor():
    pos = {"opened_at": _ago_iso(6), "timeframe": "1minute"}
    # 1m * 3 bars = 3 min required; with no floor a 6-min hold satisfies (the
    # old behaviour that let a 6-min flat_signal slip through).
    assert _has_satisfied_min_hold(pos, min_hold_bars=3, timeframe="1minute", floor_minutes=0.0)
    # With an 8-min floor the same 6-min position is NOT yet eligible to close.
    assert not _has_satisfied_min_hold(pos, min_hold_bars=3, timeframe="1minute", floor_minutes=8.0)
    # A 10-min-old position clears the 8-min floor.
    older = {"opened_at": _ago_iso(10), "timeframe": "1minute"}
    assert _has_satisfied_min_hold(older, min_hold_bars=3, timeframe="1minute", floor_minutes=8.0)


def test_reentry_cooldown_blocks_recent_whipsaw(tmp_path):
    store = _store(tmp_path, reentry_cooldown_bars=3, reentry_cooldown_floor_seconds=600.0)
    closed = [{"underlying": "NIFTY", "close_reason": "flat_signal", "closed_at": _ago_iso(2)}]
    # 5m * 3 bars = 15 min cooldown; a 2-min-old whipsaw close blocks re-entry.
    assert store._in_reentry_cooldown("NIFTY", closed, timeframe="5minute") is True
    # A different symbol is unaffected.
    assert store._in_reentry_cooldown("BANKNIFTY", closed, timeframe="5minute") is False


def test_reentry_cooldown_expires(tmp_path):
    store = _store(tmp_path, reentry_cooldown_bars=3, reentry_cooldown_floor_seconds=600.0)
    closed = [{"underlying": "NIFTY", "close_reason": "signal_flip", "closed_at": _ago_iso(20)}]
    # 20 min > 15 min cooldown -> re-entry allowed again.
    assert store._in_reentry_cooldown("NIFTY", closed, timeframe="5minute") is False


def test_natural_close_does_not_arm_cooldown(tmp_path):
    store = _store(tmp_path, reentry_cooldown_bars=3, reentry_cooldown_floor_seconds=600.0)
    closed = [
        {"underlying": "NIFTY", "close_reason": "stop_loss", "closed_at": _ago_iso(1)},
        {"underlying": "NIFTY", "close_reason": "target", "closed_at": _ago_iso(1)},
    ]
    assert store._in_reentry_cooldown("NIFTY", closed, timeframe="5minute") is False


def test_cooldown_disabled_by_default(tmp_path):
    store = _store(tmp_path)  # constructor defaults: cooldown off
    closed = [{"underlying": "NIFTY", "close_reason": "flat_signal", "closed_at": _ago_iso(1)}]
    assert store._in_reentry_cooldown("NIFTY", closed, timeframe="5minute") is False


def test_floor_seconds_applies_when_bars_zero(tmp_path):
    # bars=0 but a 600s floor still enforces a cooldown.
    store = _store(tmp_path, reentry_cooldown_bars=0, reentry_cooldown_floor_seconds=600.0)
    closed = [{"underlying": "NIFTY", "close_reason": "flat_signal", "closed_at": _ago_iso(5)}]
    assert store._in_reentry_cooldown("NIFTY", closed, timeframe="5minute") is True
    closed_old = [{"underlying": "NIFTY", "close_reason": "flat_signal", "closed_at": _ago_iso(11)}]
    assert store._in_reentry_cooldown("NIFTY", closed_old, timeframe="5minute") is False
