"""Stock option-chain instrument resolution (2026-07-18, defect 7c).

to_broker_symbol() maps only the 5 index app-symbols to Upstox instrument
keys; stocks passed through as bare names ("INFY"), so every Upstox
option-chain call for a stock sent an invalid instrument key -> guaranteed
400 "Invalid Instrument key" (the 2026-07-17 storm: 8120 400s, ~83% of the
Upstox 30-min budget). The resolver now sources the broker-canonical key
from fo_underlying_catalog.underlying_key with an in-process TTL cache, and
the chain refresh FAILS CLOSED (no broker call, counts toward eviction)
when a stock has no canonical key.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import market_data.symbols as symbols_module
from market_data.option_chain import OptionChainService
from market_data.symbols import (
    APP_TO_BROKER_SYMBOL,
    APP_TO_FYERS_SYMBOL,
    clear_underlying_key_cache,
    resolve_upstox_option_underlying_key,
    to_broker_symbol,
    to_fyers_option_symbol,
)


# Live-DB-shaped fixture row: exact column set of fo_underlying_catalog as
# verified against the running stack on 2026-07-18.
def _catalog_row(symbol: str, underlying_key: str) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        kind="STOCK",
        spot_instrument_key=underlying_key,
        underlying_key=underlying_key,
        lot_size=400,
    )


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeSession:
    """Mirrors the async-context AsyncSessionLocal shape used by the code."""

    def __init__(self, rows_by_symbol: dict[str, SimpleNamespace], calls: list[dict]):
        self._rows = rows_by_symbol
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, stmt, params=None):
        self._calls.append(dict(params or {}))
        return _FakeResult(self._rows.get((params or {}).get("symbol")))


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_underlying_key_cache()
    yield
    clear_underlying_key_cache()


def _install_fake_db(monkeypatch, rows_by_symbol: dict[str, SimpleNamespace]) -> list[dict]:
    calls: list[dict] = []
    import db.database as database_module

    monkeypatch.setattr(
        database_module,
        "AsyncSessionLocal",
        lambda: _FakeSession(rows_by_symbol, calls),
    )
    return calls


def _install_exploding_db(monkeypatch) -> None:
    import db.database as database_module

    def _boom():
        raise AssertionError("index resolution must never touch the DB")

    monkeypatch.setattr(database_module, "AsyncSessionLocal", _boom)


# ---------------------------------------------------------------------------
# 1. Index resolution unchanged — byte-identical to the static map, no DB.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_index_resolution_byte_identical_and_no_db(monkeypatch) -> None:
    _install_exploding_db(monkeypatch)
    assert len(APP_TO_BROKER_SYMBOL) == 5
    for app_symbol, broker_key in APP_TO_BROKER_SYMBOL.items():
        resolved = await resolve_upstox_option_underlying_key(app_symbol)
        assert resolved == broker_key
        assert resolved == to_broker_symbol(app_symbol)


@pytest.mark.asyncio
async def test_index_display_names_resolve_statically(monkeypatch) -> None:
    _install_exploding_db(monkeypatch)
    assert (
        await resolve_upstox_option_underlying_key("NIFTY")
        == APP_TO_BROKER_SYMBOL["NSE:NIFTY50-INDEX"]
    )
    assert (
        await resolve_upstox_option_underlying_key("SENSEX")
        == APP_TO_BROKER_SYMBOL["BSE:SENSEX-INDEX"]
    )


# ---------------------------------------------------------------------------
# 2. Stock resolution via fo_underlying_catalog.underlying_key.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stock_resolves_to_catalog_underlying_key(monkeypatch) -> None:
    calls = _install_fake_db(
        monkeypatch, {"INFY": _catalog_row("INFY", "NSE_EQ|INE009A01021")}
    )
    assert await resolve_upstox_option_underlying_key("INFY") == "NSE_EQ|INE009A01021"
    assert calls == [{"symbol": "INFY"}]


@pytest.mark.asyncio
async def test_stock_app_symbol_forms_normalize_to_catalog_name(monkeypatch) -> None:
    calls = _install_fake_db(
        monkeypatch,
        {
            "INFY": _catalog_row("INFY", "NSE_EQ|INE009A01021"),
            "BAJAJ-AUTO": _catalog_row("BAJAJ-AUTO", "NSE_EQ|INE917I01010"),
        },
    )
    # NSE:XXX-EQ app style strips to the catalog name.
    assert (
        await resolve_upstox_option_underlying_key("NSE:INFY-EQ")
        == "NSE_EQ|INE009A01021"
    )
    # A hyphenated NSE name is NOT mangled (only a trailing -EQ is stripped).
    assert (
        await resolve_upstox_option_underlying_key("BAJAJ-AUTO")
        == "NSE_EQ|INE917I01010"
    )
    assert {c["symbol"] for c in calls} == {"INFY", "BAJAJ-AUTO"}


# ---------------------------------------------------------------------------
# 3. TTL cache — repeat lookups don't touch the DB again.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ttl_cache_hit_avoids_repeat_db_lookup(monkeypatch) -> None:
    calls = _install_fake_db(
        monkeypatch, {"RELIANCE": _catalog_row("RELIANCE", "NSE_EQ|INE002A01018")}
    )
    for _ in range(3):
        assert (
            await resolve_upstox_option_underlying_key("RELIANCE")
            == "NSE_EQ|INE002A01018"
        )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_negative_cache_expires_so_catalog_backfill_is_picked_up(monkeypatch) -> None:
    calls = _install_fake_db(monkeypatch, {})
    assert await resolve_upstox_option_underlying_key("NEWNAME") is None
    assert await resolve_upstox_option_underlying_key("NEWNAME") is None
    assert len(calls) == 1  # negative result cached within its TTL

    # Age the negative entry past its TTL: the catalog is re-queried. The
    # clock stub is scoped to the symbols module only (never patch the global
    # time module — the event loop uses it).
    import time as _time

    aged = _time.monotonic() + symbols_module.UNDERLYING_KEY_NEGATIVE_TTL_SECONDS + 1.0
    monkeypatch.setattr(
        symbols_module, "time", SimpleNamespace(monotonic=lambda: aged)
    )
    await resolve_upstox_option_underlying_key("NEWNAME")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_db_error_fails_closed_without_negative_caching(monkeypatch) -> None:
    import db.database as database_module

    attempts = [0]

    def _flaky():
        attempts[0] += 1
        raise ConnectionError("DB unreachable")

    monkeypatch.setattr(database_module, "AsyncSessionLocal", _flaky)
    assert await resolve_upstox_option_underlying_key("INFY") is None
    assert await resolve_upstox_option_underlying_key("INFY") is None
    # A transient DB failure is NOT cached — each call retried the catalog.
    assert attempts[0] == 2


# ---------------------------------------------------------------------------
# 4. Fail-closed chain refresh: no broker call, eviction counter advances.
# ---------------------------------------------------------------------------

class _RecordingBroker:
    broker_name = "upstox"

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def get_option_chain(self, symbol: str, expiry: str):
        self.calls.append((symbol, expiry))
        raise AssertionError("broker must not be called for an unresolvable stock")


@pytest.mark.asyncio
async def test_missing_catalog_stock_fails_closed_and_counts_to_eviction(monkeypatch) -> None:
    _install_fake_db(monkeypatch, {})  # catalog has no row for the name
    service = OptionChainService()
    broker = _RecordingBroker()
    service.set_broker(broker)
    service.track("UNLISTEDCO", "2026-07-30")

    await service._refresh("UNLISTEDCO", "2026-07-30")
    assert broker.calls == []  # fail closed: known-invalid request never sent
    assert service._refresh_failures[("UNLISTEDCO", "2026-07-30")] == 1

    for _ in range(service.EVICT_AFTER_CONSECUTIVE_FAILURES - 1):
        await service._refresh("UNLISTEDCO", "2026-07-30")
    assert ("UNLISTEDCO", "2026-07-30") not in service._tracked
    assert broker.calls == []


@pytest.mark.asyncio
async def test_resolved_stock_sends_catalog_key_to_upstox(monkeypatch) -> None:
    _install_fake_db(
        monkeypatch, {"INFY": _catalog_row("INFY", "NSE_EQ|INE009A01021")}
    )

    sent: list[tuple[str, str]] = []

    class _CapturingBroker:
        broker_name = "upstox"

        async def get_option_chain(self, symbol: str, expiry: str):
            sent.append((symbol, expiry))
            raise RuntimeError("stop before Redis")  # halt after capture

    service = OptionChainService()
    service.set_broker(_CapturingBroker())
    await service._refresh("INFY", "2026-07-30")
    assert sent == [("NSE_EQ|INE009A01021", "2026-07-30")]


# ---------------------------------------------------------------------------
# 5. Fyers side — indices unchanged, bare stock names get NSE:XXX-EQ.
# ---------------------------------------------------------------------------

def test_fyers_option_symbol_indices_unchanged() -> None:
    for app_symbol, fyers_symbol in APP_TO_FYERS_SYMBOL.items():
        assert to_fyers_option_symbol(app_symbol) == fyers_symbol


def test_fyers_option_symbol_stock_forms() -> None:
    assert to_fyers_option_symbol("INFY") == "NSE:INFY-EQ"
    assert to_fyers_option_symbol("BAJAJ-AUTO") == "NSE:BAJAJ-AUTO-EQ"
    # Already-qualified symbols pass through untouched.
    assert to_fyers_option_symbol("NSE:INFY-EQ") == "NSE:INFY-EQ"
    assert to_fyers_option_symbol("MCX:CRUDEOIL25JULFUT") == "MCX:CRUDEOIL25JULFUT"
