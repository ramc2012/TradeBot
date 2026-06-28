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
    # Market-profile TPO bucket size. The exchange tick (`mp_tick_size`) is far
    # too fine for a value-area profile on high-priced contracts (GOLD ~143000
    # at tick 1.0 → thousands of one-rupee TPO levels → POC never concentrates,
    # value area = half the day). `mp_value_tick` is the coarser bucket used to
    # BUILD the profile so POC/VAH/VAL are meaningful. Defaults to 0.0 → fall
    # back to mp_tick_size (see mp_profile_tick()).
    mp_value_tick: float = 0.0
    # Session-to-session POC gap fraction above which a daily profile is treated
    # as belonging to a DIFFERENT (front-month) contract. The MCX continuous
    # series is NOT back-adjusted — it re-levels at every roll (e.g. NATURALGAS
    # ~-42%). Weekly/monthly aggregates must never blend two contract regimes, so
    # the profile store clips each aggregate at the most recent gap exceeding
    # this fraction. 0.0 → use a sane default (see roll_gap_threshold()).
    roll_gap_frac: float = 0.0

    def mp_profile_tick(self) -> float:
        """Coarse TPO bucket for building the value-area profile."""
        return self.mp_value_tick if self.mp_value_tick and self.mp_value_tick > 0 else self.mp_tick_size

    def roll_gap_threshold(self) -> float:
        """Fractional POC gap that marks a contract roll boundary."""
        return self.roll_gap_frac if self.roll_gap_frac and self.roll_gap_frac > 0 else 0.06


COMMODITY_CONTRACT_SPECS: dict[str, CommodityContractSpec] = {
    "GOLD": CommodityContractSpec(
        root="GOLD",
        display_name="Gold",
        futures_lot_size=10,
        mp_tick_size=1.0,
        contract_unit_label="100 gm contract",
        quote_unit_label="Rs / 10 gm",
        options_label="Strategy 1 · Options",
        futures_label="MP+OF Futures",
        notes="Gold options are monitored off the saved expiry ladder; futures execution uses one 100 gm lot with prices quoted per 10 gm.",
        mp_value_tick=20.0,
        roll_gap_frac=0.06,
    ),
    "SILVERM": CommodityContractSpec(
        root="SILVERM",
        display_name="Silver Mini",
        futures_lot_size=5,
        mp_tick_size=1.0,
        contract_unit_label="5 kg contract",
        quote_unit_label="Rs / kg",
        options_label="Strategy 1 · Options",
        futures_label="MP+OF Futures",
        notes="Silver Mini options and futures share the same saved root; one futures lot is 5 kg.",
        mp_value_tick=100.0,
        roll_gap_frac=0.05,
    ),
    "CRUDEOIL": CommodityContractSpec(
        root="CRUDEOIL",
        display_name="Crude Oil",
        futures_lot_size=100,
        mp_tick_size=1.0,
        contract_unit_label="100 barrel contract",
        quote_unit_label="Rs / barrel",
        options_label="Strategy 1 · Options",
        futures_label="MP+OF Futures",
        notes="Crude Oil options align with the saved expiry ladder; one futures lot is 100 barrels.",
        mp_value_tick=5.0,
        roll_gap_frac=0.06,
    ),
    "NATURALGAS": CommodityContractSpec(
        root="NATURALGAS",
        display_name="Natural Gas",
        futures_lot_size=1250,
        mp_tick_size=0.1,
        contract_unit_label="1250 MMBtu contract",
        quote_unit_label="Rs / MMBtu",
        options_label="Strategy 1 · Options",
        futures_label="MP+OF Futures",
        notes="Natural Gas futures use the MP+OF entry engine; one lot is 1250 MMBtu.",
        mp_value_tick=0.2,
        roll_gap_frac=0.10,
    ),
    "COPPER": CommodityContractSpec(
        root="COPPER",
        display_name="Copper",
        futures_lot_size=2500,
        mp_tick_size=0.05,
        contract_unit_label="2500 kg contract",
        quote_unit_label="Rs / kg",
        options_label="Strategy 1 · Options",
        futures_label="MP+OF Futures",
        notes="Most liquid MCX base metal; one futures lot is 2500 kg quoted per kg.",
        mp_value_tick=0.25,
        roll_gap_frac=0.05,
    ),
    "ALUMINI": CommodityContractSpec(
        root="ALUMINI",
        display_name="Aluminium Mini",
        futures_lot_size=1000,
        mp_tick_size=0.05,
        contract_unit_label="1 MT mini contract",
        quote_unit_label="Rs / kg",
        options_label="Strategy 1 · Options",
        futures_label="MP+OF Futures",
        notes="1-ton mini contract for capital-efficient aluminium exposure; day session preferred.",
        mp_value_tick=0.1,
        roll_gap_frac=0.05,
    ),
    "ZINCMINI": CommodityContractSpec(
        root="ZINCMINI",
        display_name="Zinc Mini",
        futures_lot_size=1000,
        mp_tick_size=0.05,
        contract_unit_label="1 MT mini contract",
        quote_unit_label="Rs / kg",
        options_label="Strategy 1 · Options",
        futures_label="MP+OF Futures",
        notes="1-ton zinc mini; cleaner intraday flow than full ZINC (5 MT).",
        mp_value_tick=0.1,
        roll_gap_frac=0.05,
    ),
    "NICKEL": CommodityContractSpec(
        root="NICKEL",
        display_name="Nickel",
        futures_lot_size=1500,
        mp_tick_size=0.10,
        contract_unit_label="1500 kg contract",
        quote_unit_label="Rs / kg",
        options_label="Strategy 1 · Options",
        futures_label="MP+OF Futures",
        notes="LME-driven; expect wider spreads and occasional circuit halts.",
        mp_value_tick=0.5,
        roll_gap_frac=0.05,
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
    futures_label="MP+OF Futures",
    notes="Commodity contract metadata is unavailable for this symbol; falling back to a 1-lot placeholder.",
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
