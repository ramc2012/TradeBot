"""Contract metadata for the NIFTY/BANKNIFTY index-futures MP+OF sleeve.

Reuses ``CommodityContractSpec`` verbatim so the index specs expose the EXACT
field names the agent reads (``futures_lot_size``, ``mp_tick_size``) — building a
parallel dataclass with a different field name (e.g. ``lot_size``) would make
``getattr(spec, "futures_lot_size", 0)`` return 0 and silently defeat the
equal-notional sizing. The agent sizes index orders off ``futures_lot_size``;
keep these honest.

Lot sizes (NIFTY 65 / BANKNIFTY 30) are the SEBI-revised values as of 2026-06;
they change periodically, so the agent prefers the live ``fo_underlying_catalog``
value at runtime and only falls back to these. Confirm before enabling the flag.
"""
from __future__ import annotations

import re
from typing import Optional

from market_data.commodity_contract_specs import CommodityContractSpec, canonicalize_commodity_root

# Roots this sleeve trades. SENSEX is intentionally excluded (BSE, different
# expiry convention) until separately validated.
INDEX_FUTURES_ROOTS: frozenset[str] = frozenset({"NIFTY", "BANKNIFTY"})


INDEX_CONTRACT_SPECS: dict[str, CommodityContractSpec] = {
    "NIFTY": CommodityContractSpec(
        root="NIFTY",
        display_name="Nifty 50",
        futures_lot_size=65,
        mp_tick_size=5.0,
        contract_unit_label="1 lot (65 qty)",
        quote_unit_label="index points",
        options_label="-",
        futures_label="MP+OF Index Futures",
        notes="NIFTY monthly future, NSE. Lot 65 (SEBI 2026-06) — prefer fo_underlying_catalog at runtime.",
    ),
    "BANKNIFTY": CommodityContractSpec(
        root="BANKNIFTY",
        display_name="Bank Nifty",
        futures_lot_size=30,
        mp_tick_size=10.0,
        contract_unit_label="1 lot (30 qty)",
        quote_unit_label="index points",
        options_label="-",
        futures_label="MP+OF Index Futures",
        notes="BANKNIFTY monthly future, NSE. Lot 30 (SEBI 2026-06) — prefer fo_underlying_catalog at runtime.",
    ),
}

# NIFTY index futures SPAN+exposure margin runs roughly 12–18% of notional and
# moves with volatility. These are conservative paper defaults (no live SPAN
# call wired); revisit before any real-money step.
INDEX_MARGIN_PCT: dict[str, float] = {
    "NIFTY": 0.18,
    "BANKNIFTY": 0.20,
}


def extract_index_root(symbol_or_root: str) -> str:
    """NSE index-futures root from a symbol / instrument_key / plain root.

    Handles ``NSE:NIFTY26JUNFUT``, ``NSE_FO|NIFTY26JUNFUT`` and ``NIFTY``.
    Extracts the FULL leading alpha token before the ``YYMMMFUT`` suffix so
    ``BANKNIFTY26JUNFUT`` → ``BANKNIFTY`` (never ``NIFTY``).
    """
    raw = str(symbol_or_root or "").strip().upper()
    token = re.split(r"[:|]", raw)[-1]
    token = re.sub(r"\d{2}[A-Z]{3}FUT$", "", token)
    return canonicalize_commodity_root(token)


def is_index_futures_symbol(symbol_or_root: str) -> bool:
    return extract_index_root(symbol_or_root) in INDEX_FUTURES_ROOTS


def get_index_contract_spec(symbol_or_root: str) -> Optional[CommodityContractSpec]:
    """Return the index spec, or None if the symbol isn't a tracked index future."""
    return INDEX_CONTRACT_SPECS.get(extract_index_root(symbol_or_root))


def index_margin_pct(symbol_or_root: str) -> Optional[float]:
    return INDEX_MARGIN_PCT.get(extract_index_root(symbol_or_root))
