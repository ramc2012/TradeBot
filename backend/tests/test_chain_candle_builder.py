"""Unit tests for the chain-candle-builder accumulator — pure, no broker/DB.

Focus: the phase-P2 dual-bucket bridge. The 3m accumulator must keep emitting 3m
bars unchanged, and a parallel 30m accumulator must roll the SAME snapshots into
the interval='30minute' series that S1's entry MACD actually reads.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from market_data.chain_candle_builder import (
    BUCKET_PHASE_30M,
    BUCKET_SECONDS,
    BUCKET_SECONDS_30M,
    ChainBarAccumulator,
    _bucket_start,
)

UTC = timezone.utc
KEY = ("NIFTY", "2026-07-30", 24500.0, "CE")


def _t(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 7, 7, hh, mm, ss, tzinfo=UTC)


def test_bucket_start_parameterized_by_size() -> None:
    ts = _t(9, 47, 12)
    assert _bucket_start(ts, BUCKET_SECONDS) == _t(9, 45, 0)
    assert _bucket_start(ts, BUCKET_SECONDS_30M) == _t(9, 30, 0)  # phase 0


def test_30m_phase_anchors_to_nse_ist_session_grid() -> None:
    # NSE 30m option bars are on the :15/:45 IST grid = :45/:15 UTC.
    # 09:15 IST == 03:45 UTC (the session-open anchor).
    p = BUCKET_PHASE_30M
    assert _bucket_start(_t(3, 45, 0), BUCKET_SECONDS_30M, p) == _t(3, 45, 0)    # 09:15 IST anchor
    assert _bucket_start(_t(4, 14, 38), BUCKET_SECONDS_30M, p) == _t(3, 45, 0)   # 09:44:38 IST → 09:15 bar
    assert _bucket_start(_t(4, 15, 0), BUCKET_SECONDS_30M, p) == _t(4, 15, 0)    # 09:45 IST anchor
    assert _bucket_start(_t(4, 35, 0), BUCKET_SECONDS_30M, p) == _t(4, 15, 0)    # 10:05 IST → 09:45 bar
    # The 3m grid (phase 0) is unaffected — 09:15 IST is already a 180s boundary.
    assert _bucket_start(_t(3, 45, 10), BUCKET_SECONDS) == _t(3, 45, 0)


def test_3m_accumulator_closes_on_bucket_cross() -> None:
    acc = ChainBarAccumulator(bucket_seconds=BUCKET_SECONDS)
    # Two samples in the 09:45 bucket, then one in 09:48 → first bar closes.
    assert acc.update(KEY, _t(9, 45, 10), 100.0, volume=10, instrument_key="ik") is None
    assert acc.update(KEY, _t(9, 46, 30), 110.0, volume=15) is None
    closed = acc.update(KEY, _t(9, 48, 5), 120.0, volume=20)
    assert closed is not None
    _key, bar = closed
    assert bar.open == 100.0 and bar.high == 110.0 and bar.close == 110.0
    assert bar.bucket_start == _t(9, 45, 0)
    assert bar.to_row()["volume"] == 5  # 15 - 10 within the closed bucket


def test_30m_accumulator_rolls_many_3m_snapshots_into_one_bar() -> None:
    acc30 = ChainBarAccumulator(bucket_seconds=BUCKET_SECONDS_30M)
    # Snapshots every 3 min across the 09:30 half-hour: 09:31..09:58 all land in
    # the SAME 30m bucket; the first cross into 10:00 closes one 30m bar.
    price = 100.0
    for i in range(10):  # 09:31, 09:34, ... 09:58
        ts = _t(9, 31) + timedelta(minutes=3 * i)
        assert acc30.update(KEY, ts, price + i, volume=10 + i, instrument_key="ik") is None
    closed = acc30.update(KEY, _t(10, 0, 30), 200.0, volume=99)
    assert closed is not None
    _key, bar = closed
    assert bar.bucket_start == _t(9, 30, 0)
    assert bar.open == 100.0           # first snapshot
    assert bar.high == 109.0           # 100..109 across the window
    assert bar.close == 109.0          # last snapshot in the bucket
    assert bar.to_row()["volume"] == 9  # 19 - 10


def test_poll_once_full_cycle_no_nameerror(monkeypatch) -> None:
    """Regression (2026-07-08): an edit dropped TIER_INTERVAL_SECONDS /
    DEFAULT_INTERVAL_SECONDS and poll_once NameError'd EVERY cycle — zero
    fyers_chain rows all session. Exercise a complete poll_once cycle with a
    stubbed adapter/universe so any missing module-level constant fails HERE."""
    import asyncio
    from types import SimpleNamespace

    from market_data.chain_candle_builder import ChainCandleBuilder

    builder = ChainCandleBuilder()

    entry = SimpleNamespace(
        option_type="CE", strike=24500.0, ltp=101.5, volume=10, oi=1000,
        iv=0.12, delta=0.5, gamma=0.001, theta=-5.0, vega=8.0,
        instrument_key="NSE_FO|12345",
    )
    chain = SimpleNamespace(expiry="2026-07-30", spot_price=24510.0, entries=[entry])

    class _Adapter:
        async def get_option_chain(self, symbol, expiry):
            return chain

    async def fake_universe(self):
        return [SimpleNamespace(symbol="NIFTY", kind="INDEX")]

    async def fake_adapter(self):
        return _Adapter()

    async def fake_persist(self, closed, *, interval, acc):
        return len(closed)

    monkeypatch.setattr(ChainCandleBuilder, "_universe", fake_universe)
    monkeypatch.setattr(ChainCandleBuilder, "_fyers_adapter", fake_adapter)
    monkeypatch.setattr(ChainCandleBuilder, "_fyers_symbol", staticmethod(lambda meta: "NSE:NIFTY50-INDEX"))
    monkeypatch.setattr(ChainCandleBuilder, "_persist", fake_persist)

    stats = asyncio.run(builder.poll_once())
    assert stats["polled"] == 1
    assert stats["universe"] == 1
    # Second immediate poll: the per-symbol cadence gate must SKIP (proves the
    # TIER_INTERVAL_SECONDS lookup path executed, not just defaulted).
    stats2 = asyncio.run(builder.poll_once())
    assert stats2["skipped"] == 1 and stats2["polled"] == 0


def test_flush_closes_open_bars_before_cutoff() -> None:
    acc30 = ChainBarAccumulator(bucket_seconds=BUCKET_SECONDS_30M)
    acc30.update(KEY, _t(9, 45), 100.0, volume=5, instrument_key="ik")
    # Nothing crossed yet → still open. Flush at 10:31 closes the 09:30-10:00 bar.
    out = acc30.flush(_t(10, 31))
    assert len(out) == 1 and out[0][1].bucket_start == _t(9, 30, 0)
    # Idempotent: nothing left to flush.
    assert acc30.flush(_t(10, 31)) == []
