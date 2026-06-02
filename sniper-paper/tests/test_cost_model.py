from __future__ import annotations

from sniper_paper.common.settings import Settings
from sniper_paper.execution.cost_model import round_trip_costs, slippage_inr_one_side


def _settings():
    return Settings.load("configs/paper.yaml")


def test_round_trip_costs_breakdown_positive():
    s = _settings()
    out = round_trip_costs(s.costs, "NSE", qty=50, entry_price=24000, exit_price=24050)
    for k in ("brokerage", "exchange_fee", "stt", "stamp", "slippage", "total"):
        assert out[k] >= 0
    assert out["total"] > 0


def test_event_day_slippage_is_higher():
    s = _settings()
    base = round_trip_costs(s.costs, "NSE", 50, 24000, 24050, event_day=False)
    ev = round_trip_costs(s.costs, "NSE", 50, 24000, 24050, event_day=True)
    assert ev["slippage"] > base["slippage"]


def test_slippage_one_side_proportional_to_notional():
    s = _settings()
    a = slippage_inr_one_side(s.costs, 24000, 50)
    b = slippage_inr_one_side(s.costs, 48000, 50)
    assert b > 1.9 * a   # ~2x notional → ~2x slippage
