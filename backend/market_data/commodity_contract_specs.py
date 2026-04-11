"""Shared commodity contract metadata used by the MCX strategy surfaces."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


_MCX_FUTURE_PARTS_RE = re.compile(
    r"^(?P<exchange>MCX):(?P<root>[A-Z0-9]+?)(?P<year>\d{2})(?P<month>[A-Z]{3})FUT$"
)
_ROOT_ALIASES = {
    "SILVERMIC": "SILVERM",
}


@dataclass(frozen=True)
class CommodityContractSpec:
    root: str
    display_name: str
    futures_lot_size: int
    mp_tick_size: float
    contract_unit_label: str
    quote_unit_label: str
    options_label: str
    futures_label: str
    notes: str


COMMODITY_CONTRACT_SPECS: dict[str, CommodityContractSpec] = {
    "GOLD": CommodityContractSpec(
        root="GOLD",
        display_name="Gold",
        futures_lot_size=10,
        mp_tick_size=1.0,
        contract_unit_label="100 gm contract",
        quote_unit_label="Rs / 10 gm",
        options_label="Strategy 1 · Options",
        futures_label="Strategy 2 · Futures",
        notes="Gold options are monitored off the saved expiry ladder; futures execution uses one 100 gm lot with prices quoted per 10 gm.",
    ),
    "SILVERM": CommodityContractSpec(
        root="SILVERM",
        display_name="Silver Mini",
        futures_lot_size=5,
        mp_tick_size=1.0,
        contract_unit_label="5 kg contract",
        quote_unit_label="Rs / kg",
        options_label="Strategy 1 · Options",
        futures_label="Strategy 2 · Futures",
        notes="Silver Mini options and futures share the same saved root; one futures lot is 5 kg.",
    ),
    "CRUDEOIL": CommodityContractSpec(
        root="CRUDEOIL",
        display_name="Crude Oil",
        futures_lot_size=100,
        mp_tick_size=1.0,
        contract_unit_label="100 barrel contract",
        quote_unit_label="Rs / barrel",
        options_label="Strategy 1 · Options",
        futures_label="Strategy 2 · Futures",
        notes="Crude Oil options align with the saved expiry ladder; one futures lot is 100 barrels.",
    ),
    "NATURALGAS": CommodityContractSpec(
        root="NATURALGAS",
        display_name="Natural Gas",
        futures_lot_size=1250,
        mp_tick_size=0.1,
        contract_unit_label="1250 MMBtu contract",
        quote_unit_label="Rs / MMBtu",
        options_label="Strategy 1 · Options",
        futures_label="Strategy 2 · Futures",
        notes="Natural Gas options and futures use the same root; one futures lot is 1250 MMBtu.",
    ),
}

DEFAULT_COMMODITY_CONTRACT_SPEC = CommodityContractSpec(
    root="UNKNOWN",
    display_name="Commodity",
    futures_lot_size=1,
    mp_tick_size=0.5,
    contract_unit_label="1 contract",
    quote_unit_label="quoted units",
    options_label="Strategy 1 · Options",
    futures_label="Strategy 2 · Futures",
    notes="Commodity contract metadata is unavailable for this symbol, so the runtime falls back to a 1-lot placeholder.",
)


def canonicalize_commodity_root(raw_root: str) -> str:
    return _ROOT_ALIASES.get(str(raw_root or "").strip().upper(), str(raw_root or "").strip().upper())


def extract_commodity_root(symbol: str) -> str:
    raw_symbol = str(symbol or "").strip().upper()
    match = _MCX_FUTURE_PARTS_RE.match(raw_symbol)
    if match:
        return canonicalize_commodity_root(str(match.group("root")))
    token = raw_symbol.split(":")[-1]
    token = re.sub(r"\d{2}[A-Z]{3}FUT$", "", token)
    return canonicalize_commodity_root(token)


def get_commodity_contract_spec(symbol_or_root: str) -> CommodityContractSpec:
    raw = str(symbol_or_root or "").strip().upper()
    root = extract_commodity_root(raw) if ":" in raw else canonicalize_commodity_root(raw)
    return COMMODITY_CONTRACT_SPECS.get(root, DEFAULT_COMMODITY_CONTRACT_SPEC)


def get_commodity_display_name(symbol_or_root: str) -> str:
    return get_commodity_contract_spec(symbol_or_root).display_name
