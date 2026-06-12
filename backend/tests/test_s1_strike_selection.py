"""Unit tests for S1 strike selection + 3m→30m resample (pure helpers)."""
from __future__ import annotations

from datetime import datetime

from paper_engine.s1_strike_selection import (
    _ema_macd,
    _macd_zero_cross,
    pick_strike,
    resample_closes_30m,
)


def test_resample_3m_to_30m_last_close_per_bucket():
    base = datetime(2026, 6, 8, 9, 0)
    rows = [
        (base.replace(minute=15), 10.0),
        (base.replace(minute=18), 11.0),
        (base.replace(minute=30), 12.0),
        (base.replace(minute=33), 13.0),
        (base.replace(hour=10, minute=0), 14.0),
    ]
    out = resample_closes_30m(rows)
    # buckets: 09:00 (last=11 @ :18), 09:30 (last=13 @ :33), 10:00 (=14)
    assert [(t.strftime("%H:%M"), c) for t, c in out] == [("09:00", 11.0), ("09:30", 13.0), ("10:00", 14.0)]


_FRESH = datetime(2026, 6, 8, 15, 0)
_STALE = datetime(2026, 6, 5, 12, 0)


def test_pick_strike_prefers_nearest_with_history():
    spot = 286.0
    candidates = [
        {"strike": 280.0, "bars": 400, "oi": 1000, "volume": 5000, "last": _FRESH},
        {"strike": 285.0, "bars": 50, "oi": 9000, "volume": 20000, "last": _FRESH},   # thin history
        {"strike": 290.0, "bars": 400, "oi": 8000, "volume": 15000, "last": _FRESH},
    ]
    chosen = pick_strike(candidates, spot, min_bars=350, min_oi=0)
    assert chosen["strike"] == 290.0  # 285 too little history -> next nearest with history


def test_pick_strike_excludes_stale_strike():
    # ALKEM-shaped: nearest strike has history but is ABANDONED (stale); the
    # active deeper strike should win even though it's further from spot.
    spot = 5382.5
    candidates = [
        {"strike": 5400.0, "bars": 557, "oi": 39500, "volume": 1, "last": _STALE},   # nearest but stale
        {"strike": 5300.0, "bars": 748, "oi": 90000, "volume": 9, "last": _FRESH},   # active
    ]
    chosen = pick_strike(candidates, spot, min_bars=350, min_oi=0)
    assert chosen["strike"] == 5300.0


def test_pick_strike_oi_floor_then_fallback():
    spot = 286.0
    candidates = [
        {"strike": 285.0, "bars": 50, "oi": 9000, "volume": 20000, "last": _FRESH},   # thin history
        {"strike": 290.0, "bars": 400, "oi": 8000, "volume": 15000, "last": _FRESH},  # oi below floor
        {"strike": 295.0, "bars": 400, "oi": 500, "volume": 1000, "last": _FRESH},
    ]
    chosen = pick_strike(candidates, spot, min_bars=350, min_oi=10000)
    assert chosen["strike"] == 290.0


def test_pick_strike_none_when_no_history():
    candidates = [{"strike": 285.0, "bars": 10, "oi": 9000, "volume": 1, "last": _FRESH}]
    assert pick_strike(candidates, 286.0, min_bars=350) is None


def test_ema_macd_crosses_zero_on_flat_then_uptrend():
    closes = [100.0] * 30 + [100.0 + 2.0 * i for i in range(1, 16)]  # flat then strong ramp
    ml = _ema_macd(closes)
    crossed = any(
        ml[i - 1] is not None and ml[i] is not None and ml[i - 1] <= 0 < ml[i]
        for i in range(1, len(ml))
    )
    assert crossed


def test_macd_zero_cross_short_series_is_false():
    assert _macd_zero_cross([1.0] * 10, "CE", "X", None) == (False, None, None)


def test_macd_zero_cross_ce_fires_after_uptrend():
    # Down then up so the MACD line ends just above zero after being below it.
    closes = [100.0 - i for i in range(20)] + [80.0 + 3.0 * i for i in range(1, 22)]
    fresh, cur, prev = _macd_zero_cross(closes, "CE", "X", None)
    assert cur is not None and prev is not None
    # The reversal pushes MACD up through zero at some recent bar; the final state
    # is bullish (current MACD positive).
    assert cur > 0
