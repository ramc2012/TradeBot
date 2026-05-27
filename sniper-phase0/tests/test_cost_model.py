from __future__ import annotations

from sniper_phase0.labels.cost_model import compute_costs, net_pnl
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


def test_costs_are_positive_and_finite() -> None:
    tc = compute_costs(entry_price=25000.0, exit_price=25100.0, qty=25, costs=COSTS)
    assert tc.total > 0
    assert tc.brokerage == 40.0  # 20 x 2
    assert tc.stt > 0
    assert tc.gst > 0


def test_long_pnl_sign() -> None:
    gross, net, _ = net_pnl(25000, 25100, 25, "long", COSTS)
    assert gross == 100 * 25
    assert net < gross  # costs eat into pnl


def test_short_pnl_sign() -> None:
    gross, net, _ = net_pnl(25000, 24900, 25, "short", COSTS)
    assert gross == 100 * 25
    assert net < gross


def test_event_day_slippage_higher() -> None:
    _g1, n1, _ = net_pnl(25000, 25100, 25, "long", COSTS, is_event_day=False)
    _g2, n2, _ = net_pnl(25000, 25100, 25, "long", COSTS, is_event_day=True)
    assert n2 < n1


def test_slippage_multiplier_scales() -> None:
    _g1, n1, _ = net_pnl(25000, 25100, 25, "long", COSTS, slippage_multiplier=1.0)
    _g2, n2, _ = net_pnl(25000, 25100, 25, "long", COSTS, slippage_multiplier=2.0)
    assert n2 < n1
