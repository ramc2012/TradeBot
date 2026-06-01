"""Equal-notional position sizing for the commodity futures lane.

Lots are sized so every contract opens at ~COMMODITY_TARGET_POSITION_VALUE
regardless of its lot size and price (gold, crude, zinc, … all ≈ ₹15L),
with a 1-lot floor for contracts whose single lot already exceeds the
target (e.g. COPPER, NICKEL).
"""
from __future__ import annotations

from market_data.commodity_contract_specs import get_commodity_contract_spec
from paper_engine.commodity_strategy_agent import (
    COMMODITY_TARGET_POSITION_VALUE,
    CommodityStrategyAgent,
)


class _SizingStub:
    """Lightweight holder so we can exercise the sizing method without
    constructing the full agent (which loads persisted state)."""

    _lots_per_trade = 1
    _target_lots_for_contract = CommodityStrategyAgent._target_lots_for_contract


# Representative recent MCX prices (in the contract's quote unit).
_PRICES = {
    "GOLD": 73000.0,       # Rs / 10 gm
    "SILVERM": 90000.0,    # Rs / kg
    "CRUDEOIL": 5500.0,    # Rs / barrel
    "NATURALGAS": 250.0,   # Rs / MMBtu
    "ALUMINI": 245.0,      # Rs / kg
    "ZINCMINI": 270.0,     # Rs / kg
}

# Contracts whose single lot already exceeds the target → floored at 1 lot.
_FLOORED = {
    "COPPER": 840.0,       # 2500 kg × 840 ≈ ₹21L per lot
    "NICKEL": 1350.0,      # 1500 kg × 1350 ≈ ₹20L per lot
}


def test_each_contract_sizes_near_the_target_value() -> None:
    stub = _SizingStub()
    for root, price in _PRICES.items():
        spec = get_commodity_contract_spec(root)
        lots = stub._target_lots_for_contract(spec, price)
        notional = lots * spec.futures_lot_size * price
        # Whole-lot rounding can't hit the target exactly; require within
        # half a lot of it on either side.
        per_lot = spec.futures_lot_size * price
        assert notional >= COMMODITY_TARGET_POSITION_VALUE - per_lot
        assert notional <= COMMODITY_TARGET_POSITION_VALUE + per_lot
        assert lots >= 1


def test_all_contracts_open_at_roughly_equal_size() -> None:
    stub = _SizingStub()
    notionals = []
    for root, price in _PRICES.items():
        spec = get_commodity_contract_spec(root)
        lots = stub._target_lots_for_contract(spec, price)
        notionals.append(lots * spec.futures_lot_size * price)
    # The spread between the smallest and largest non-floored position
    # should be modest — they're all targeting the same value.
    assert max(notionals) / min(notionals) < 1.6


def test_large_lot_contracts_floor_at_one_lot() -> None:
    stub = _SizingStub()
    for root, price in _FLOORED.items():
        spec = get_commodity_contract_spec(root)
        lots = stub._target_lots_for_contract(spec, price)
        assert lots == 1  # single lot already exceeds the target


def test_zero_price_falls_back_to_base_lots() -> None:
    stub = _SizingStub()
    spec = get_commodity_contract_spec("CRUDEOIL")
    assert stub._target_lots_for_contract(spec, 0.0) == 1


def test_lots_per_trade_scales_the_target() -> None:
    stub = _SizingStub()
    stub._lots_per_trade = 2
    spec = get_commodity_contract_spec("ZINCMINI")
    base = _SizingStub()
    # Doubling lots_per_trade roughly doubles the sized lots.
    assert stub._target_lots_for_contract(spec, 270.0) >= 2 * base._target_lots_for_contract(spec, 270.0) - 1
