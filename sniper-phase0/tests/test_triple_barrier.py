from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from sniper_phase0.labels.triple_barrier import label_one
from sniper_phase0.utils.settings import Costs

COSTS = Costs(
    brokerage_per_order_inr=20.0,
    exchange_txn_charge_bps=0.345,
    sebi_charge_bps=0.001,
    stt_bps_sell_side=1.25,
    stamp_duty_bps_buy_side=0.2,
    gst_on_brokerage_pct=18.0,
    gst_on_exchange_pct=18.0,
    slippage_bps_default=1.5,
    slippage_bps_event_day=3.0,
)


def _ticks(start: str, prices: list[float], step_seconds: int = 1) -> pd.DataFrame:
    base = pd.Timestamp(start)
    ts = [base + pd.Timedelta(seconds=i * step_seconds) for i in range(len(prices))]
    return pd.DataFrame({"ts": ts, "ltp": prices, "last_qty": np.ones(len(prices), dtype=int)})


def test_long_target_hit() -> None:
    entry_ts = pd.Timestamp("2024-04-15 10:00:00")
    ticks = _ticks("2024-04-15 10:00:01", [25050, 25080, 25110])
    res = label_one(
        trade_id=1, entry_ts=entry_ts, entry_price=25000.0, side="long", qty=25,
        stop_price=24900.0, target_price=25100.0, max_hold_minutes=60,
        forward_ticks=ticks, costs=COSTS,
    )
    assert res.outcome == "target"
    assert res.gross_R > 0
    assert res.net_R < res.gross_R


def test_long_stop_hit() -> None:
    entry_ts = pd.Timestamp("2024-04-15 10:00:00")
    ticks = _ticks("2024-04-15 10:00:01", [24950, 24920, 24890])
    res = label_one(
        trade_id=1, entry_ts=entry_ts, entry_price=25000.0, side="long", qty=25,
        stop_price=24900.0, target_price=25100.0, max_hold_minutes=60,
        forward_ticks=ticks, costs=COSTS,
    )
    assert res.outcome == "stop"
    assert res.gross_R < 0
    assert res.net_R < res.gross_R


def test_short_target_hit() -> None:
    entry_ts = pd.Timestamp("2024-04-15 10:00:00")
    ticks = _ticks("2024-04-15 10:00:01", [24950, 24920, 24890])
    res = label_one(
        trade_id=1, entry_ts=entry_ts, entry_price=25000.0, side="short", qty=25,
        stop_price=25100.0, target_price=24900.0, max_hold_minutes=60,
        forward_ticks=ticks, costs=COSTS,
    )
    assert res.outcome == "target"
    assert res.gross_R > 0


def test_timeout_when_neither_hit() -> None:
    entry_ts = pd.Timestamp("2024-04-15 10:00:00")
    ticks = _ticks("2024-04-15 10:00:01", [25010, 25020, 25030])
    res = label_one(
        trade_id=1, entry_ts=entry_ts, entry_price=25000.0, side="long", qty=25,
        stop_price=24900.0, target_price=25100.0, max_hold_minutes=60,
        forward_ticks=ticks, costs=COSTS,
    )
    assert res.outcome == "timeout"


def test_invalid_barriers_raise() -> None:
    with pytest.raises(ValueError):
        label_one(
            trade_id=1, entry_ts=pd.Timestamp("2024-04-15 10:00"), entry_price=25000.0,
            side="long", qty=25, stop_price=25100.0, target_price=24900.0,  # swapped
            max_hold_minutes=60, forward_ticks=_ticks("2024-04-15 10:00:01", [25000]),
            costs=COSTS,
        )


def test_ticks_at_or_before_entry_are_ignored() -> None:
    entry_ts = pd.Timestamp("2024-04-15 10:00:00")
    # Stop-hitting price exactly at entry_ts — must be ignored.
    bad = pd.DataFrame({
        "ts": [entry_ts, entry_ts + pd.Timedelta(seconds=1)],
        "ltp": [24800.0, 25050.0],
        "last_qty": [1, 1],
    })
    res = label_one(
        trade_id=1, entry_ts=entry_ts, entry_price=25000.0, side="long", qty=25,
        stop_price=24900.0, target_price=25100.0, max_hold_minutes=60,
        forward_ticks=bad, costs=COSTS,
    )
    assert res.outcome != "stop"
