"""Catalog instrument-key uniqueness (2026-07-19, M&M/MARUTI ISIN collision).

``fo_underlying_catalog`` mapped BOTH ``M&M`` and ``MARUTI`` to
``NSE_EQ|INE585B01010`` (MARUTI's ISIN; M&M's real ISIN is ``INE101A01026``).
``underlying_spot_candles`` is keyed ``(instrument_key, interval, time)`` and
``underlying`` is only a label, so the two names silently OVERWROTE each other
bar-for-bar — on 2026-07-17 MARUTI/1minute had 313 of 351 rows out of band
(23.50 … 24,180 against a ~13,800 underlying), and stock 30m coverage was stuck
at 210 of 211 because only one of the pair could own a clean grid per day.

Three independent defences are covered here:
  1. the resolver refuses a fuzzy-only instrument-search match (fail closed),
  2. catalog upserts never assign a key another symbol already owns,
  3. an explicit uniqueness assertion surfaces any collision that slips in.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from market_data.catalog_integrity import (
    CatalogKeyCollision,
    assert_unique_spot_keys,
    filter_key_collisions,
    find_spot_key_collisions,
)

MARUTI_KEY = "NSE_EQ|INE585B01010"
MM_KEY = "NSE_EQ|INE101A01026"


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeSession:
    """Returns a canned row set for whatever query the code issues."""

    def __init__(self, rows):
        self._rows = rows
        self.queries: list[str] = []

    async def execute(self, statement, params=None):
        self.queries.append(str(statement))
        return _FakeResult(self._rows)


# ── 1. resolver fails closed on a fuzzy-only match ───────────────────────────

@pytest.mark.asyncio
async def test_resolver_refuses_fuzzy_only_match():
    """A search with no exact symbol/name match must resolve to None, not results[0].

    Upstox /instruments/search is fuzzy: querying "M&M" really does return
    MRPL / MCX / MOTHERSON / M&MFIN. Taking the top row blindly is how a FOREIGN
    ISIN gets adopted under a symbol.
    """
    from analysis.backtest import MACDBacktester

    bt = MACDBacktester(access_token="test-token")

    fuzzy_rows = [
        {"trading_symbol": "MRPL", "name": "MRPL", "short_name": "MRPL",
         "instrument_key": "NSE_EQ|INE103A01014", "segment": "NSE_EQ"},
        {"trading_symbol": "MARUTI", "name": "MARUTI SUZUKI INDIA LTD.",
         "short_name": "Maruti Suzuki", "instrument_key": MARUTI_KEY, "segment": "NSE_EQ"},
    ]

    async def _fake_search(**_kwargs):
        return fuzzy_rows

    bt._search_instruments = _fake_search  # type: ignore[assignment]

    meta = await bt._resolve_underlying_metadata("M&M")
    assert meta is None, "a fuzzy-only match must NOT be adopted"


@pytest.mark.asyncio
async def test_resolver_accepts_exact_match():
    """The fail-closed guard must not break the normal exact-match path."""
    from analysis.backtest import MACDBacktester

    bt = MACDBacktester(access_token="test-token")

    async def _fake_search(**_kwargs):
        return [
            {"trading_symbol": "MRPL", "name": "MRPL", "short_name": "MRPL",
             "instrument_key": "NSE_EQ|INE103A01014", "segment": "NSE_EQ"},
            {"trading_symbol": "M&M", "name": "MAHINDRA & MAHINDRA LTD",
             "short_name": "Mahindra & Mahindra", "instrument_key": MM_KEY,
             "segment": "NSE_EQ"},
        ]

    bt._search_instruments = _fake_search  # type: ignore[assignment]

    meta = await bt._resolve_underlying_metadata("M&M")
    assert meta is not None
    assert meta["spot_instrument_key"] == MM_KEY


# ── 2. write-time guard ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_filter_key_collisions_drops_foreign_key_claim():
    """A resolve that maps M&M onto MARUTI's key must be dropped, not written."""
    session = _FakeSession([SimpleNamespace(symbol="MARUTI", spot_instrument_key=MARUTI_KEY)])

    safe = await filter_key_collisions(session, [
        {"symbol": "M&M", "spot_instrument_key": MARUTI_KEY, "underlying_key": MARUTI_KEY},
        {"symbol": "INFY", "spot_instrument_key": "NSE_EQ|INE009A01021",
         "underlying_key": "NSE_EQ|INE009A01021"},
    ])

    symbols = {row["symbol"] for row in safe}
    assert "M&M" not in symbols, "M&M must not be allowed to claim MARUTI's key"
    assert "INFY" in symbols, "an uncontested mapping must still be written"


@pytest.mark.asyncio
async def test_filter_key_collisions_allows_symbol_to_keep_own_key():
    """Re-asserting a symbol's OWN existing key is not a collision (idempotent)."""
    session = _FakeSession([SimpleNamespace(symbol="MARUTI", spot_instrument_key=MARUTI_KEY)])

    safe = await filter_key_collisions(session, [
        {"symbol": "MARUTI", "spot_instrument_key": MARUTI_KEY, "underlying_key": MARUTI_KEY},
    ])
    assert len(safe) == 1


@pytest.mark.asyncio
async def test_filter_key_collisions_dedupes_within_batch():
    """Two symbols in ONE batch resolving to the same key cannot both be written."""
    session = _FakeSession([])

    safe = await filter_key_collisions(session, [
        {"symbol": "M&M", "spot_instrument_key": MARUTI_KEY, "underlying_key": MARUTI_KEY},
        {"symbol": "MARUTI", "spot_instrument_key": MARUTI_KEY, "underlying_key": MARUTI_KEY},
    ])
    assert len(safe) == 1, "a within-batch duplicate key must be rejected too"


# ── 3. explicit invariant assertion ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_find_and_assert_detect_collision():
    session = _FakeSession([SimpleNamespace(key=MARUTI_KEY, symbols=["M&M", "MARUTI"])])

    collisions = await find_spot_key_collisions(session)
    assert collisions == [{"instrument_key": MARUTI_KEY, "symbols": ["M&M", "MARUTI"]}]

    session = _FakeSession([SimpleNamespace(key=MARUTI_KEY, symbols=["M&M", "MARUTI"])])
    with pytest.raises(CatalogKeyCollision):
        await assert_unique_spot_keys(session, raise_on_collision=True)


@pytest.mark.asyncio
async def test_assert_unique_spot_keys_clean_catalog():
    session = _FakeSession([])
    assert await assert_unique_spot_keys(session) == []
    # Non-raising by default, so one bad row can never take the backend dark.
    assert await assert_unique_spot_keys(session, raise_on_collision=True) == []
