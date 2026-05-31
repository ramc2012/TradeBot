"""Smoke tests for the base-metal additions to COMMODITY_CONTRACT_SPECS.

These confirm the new symbols resolve and carry the expected lot/tick
metadata so the MP+OF futures lane can size and place stops correctly.
"""
from __future__ import annotations

import pytest

from market_data.commodity_contract_specs import (
    COMMODITY_CONTRACT_SPECS,
    extract_commodity_root,
    get_commodity_contract_spec,
)


@pytest.mark.parametrize(
    "root, lot_size, tick_size",
    [
        ("COPPER", 2500, 0.05),
        ("ALUMINI", 1000, 0.05),
        ("ZINCMINI", 1000, 0.05),
        ("NICKEL", 1500, 0.10),
    ],
)
def test_base_metal_spec_registered(root: str, lot_size: int, tick_size: float) -> None:
    assert root in COMMODITY_CONTRACT_SPECS, f"{root} missing from spec table"
    spec = COMMODITY_CONTRACT_SPECS[root]
    assert spec.root == root
    assert spec.futures_lot_size == lot_size
    assert spec.mp_tick_size == pytest.approx(tick_size)
    assert spec.futures_label == "MP+OF Futures"
    # Display label sanity — non-empty and not a placeholder.
    assert spec.display_name and "contract" not in spec.display_name.lower()


@pytest.mark.parametrize(
    "raw_symbol, expected_root",
    [
        ("MCX:COPPER26JUNFUT", "COPPER"),
        ("MCX:ALUMINI26JUNFUT", "ALUMINI"),
        ("MCX:ZINCMINI26JUNFUT", "ZINCMINI"),
        ("MCX:NICKEL26JUNFUT", "NICKEL"),
    ],
)
def test_extract_commodity_root_resolves_metals(raw_symbol: str, expected_root: str) -> None:
    assert extract_commodity_root(raw_symbol) == expected_root


def test_get_commodity_contract_spec_for_metals() -> None:
    spec = get_commodity_contract_spec("MCX:COPPER26JUNFUT")
    assert spec.root == "COPPER"
    assert spec.futures_lot_size == 2500


def test_legacy_symbols_still_resolve() -> None:
    # Ensure the new entries didn't disturb the existing four.
    for legacy_root in ("GOLD", "SILVERM", "CRUDEOIL", "NATURALGAS"):
        assert legacy_root in COMMODITY_CONTRACT_SPECS
        assert COMMODITY_CONTRACT_SPECS[legacy_root].futures_label == "MP+OF Futures"
