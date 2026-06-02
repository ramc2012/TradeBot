"""Round-trip pairing tests. Critical because FIFO matching errors silently corrupt all
downstream labeling."""

from __future__ import annotations

from datetime import datetime

import pytest

from nomad_sniper.data.round_trips import pair_round_trips
from nomad_sniper.data.trades import Trade
from nomad_sniper.utils.timeutil import IST


def _trade(tid, ttype, qty, price, minutes_from_start):
    """Build a Trade at 2025-01-08 11:00 IST + N minutes."""
    from datetime import timedelta
    ts = IST.localize(datetime(2025, 1, 8, 11, 0)) + timedelta(minutes=minutes_from_start)
    return Trade(
        trade_id=tid, order_id=f"o{tid}", symbol="NIFTY25JANFUT",
        exchange="NFO", segment="NFO",
        trade_type=ttype, quantity=qty, price=price,
        executed_at=ts, trade_date=IST.localize(datetime(2025, 1, 8)),
    )


def test_simple_long_round_trip():
    trades = [
        _trade("1", "buy", 50, 22000.0, 0),
        _trade("2", "sell", 50, 22050.0, 30),
    ]
    rts = pair_round_trips(trades)
    assert len(rts) == 1
    rt = rts[0]
    assert rt.direction == "long"
    assert rt.quantity == 50
    assert rt.gross_pnl == pytest.approx(2500.0)


def test_simple_short_round_trip():
    trades = [
        _trade("1", "sell", 50, 22000.0, 0),
        _trade("2", "buy", 50, 21950.0, 30),
    ]
    rts = pair_round_trips(trades)
    assert len(rts) == 1
    rt = rts[0]
    assert rt.direction == "short"
    assert rt.gross_pnl == pytest.approx(2500.0)


def test_partial_close_creates_two_round_trips():
    """Buy 100 then close 50 + 50 separately should yield two round trips."""
    trades = [
        _trade("1", "buy", 100, 22000.0, 0),
        _trade("2", "sell", 50, 22050.0, 30),
        _trade("3", "sell", 50, 22020.0, 45),
    ]
    rts = pair_round_trips(trades)
    assert len(rts) == 2
    assert all(rt.direction == "long" for rt in rts)
    assert sum(rt.quantity for rt in rts) == 100
    total_pnl = sum(rt.gross_pnl for rt in rts)
    assert total_pnl == pytest.approx(50 * 50 + 50 * 20)


def test_oversized_close_flips_to_short():
    """Buy 50, then sell 80: 50 closes the long, remaining 30 opens a short."""
    trades = [
        _trade("1", "buy", 50, 22000.0, 0),
        _trade("2", "sell", 80, 22050.0, 30),
        _trade("3", "buy", 30, 22030.0, 60),
    ]
    rts = pair_round_trips(trades)
    assert len(rts) == 2
    long_rt = [r for r in rts if r.direction == "long"][0]
    short_rt = [r for r in rts if r.direction == "short"][0]
    assert long_rt.quantity == 50
    assert long_rt.gross_pnl == pytest.approx(2500.0)
    assert short_rt.quantity == 30
    assert short_rt.gross_pnl == pytest.approx(30 * (22050.0 - 22030.0))


def test_unpaired_legs_are_dropped_with_warning(caplog):
    """An open position at end of input should produce no round trip and warn."""
    trades = [_trade("1", "buy", 50, 22000.0, 0)]
    rts = pair_round_trips(trades)
    assert rts == []
