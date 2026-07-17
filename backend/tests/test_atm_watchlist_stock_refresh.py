"""Tests for ATMWatchlistService.refresh_stock_snapshot_rows.

(2026-07-17 directional NIFTY-50 expansion) The directional runner refreshes
its ~25-name stock batch's watchlist rows just-in-time because the background
universe build rotates all ~217 F&O names over hours — day-one telemetry
showed every stock skipped as option_quotes_stale at 2.6-3.4h while its spot
stream was live. These tests pin the method's status contract: refreshed /
unknown_symbol / budget / error:no_active_broker, and that INDEX metas are
never refreshed through this path.
"""
from __future__ import annotations

import asyncio

import pytest

from market_data.atm_watchlist import ATMWatchlistService, UnderlyingMeta


def _stock_meta(symbol: str) -> UnderlyingMeta:
    return UnderlyingMeta(
        symbol=symbol,
        kind="STOCK",
        spot_instrument_key=f"NSE_EQ|{symbol}",
        underlying_key=f"NSE_FO|{symbol}",
    )


def _index_meta(symbol: str) -> UnderlyingMeta:
    return UnderlyingMeta(
        symbol=symbol,
        kind="INDEX",
        spot_instrument_key=f"NSE_INDEX|{symbol}",
        underlying_key=f"NSE_INDEX|{symbol}",
    )


@pytest.mark.asyncio
async def test_refresh_reports_refreshed_and_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    service = ATMWatchlistService()
    built: list[str] = []

    async def fake_load_underlyings():
        return [_index_meta("NIFTY"), _stock_meta("RELIANCE"), _stock_meta("TCS")]

    async def fake_build_row(meta, expiry, expiry_date, upstox_adapter, fyers_adapter):
        built.append(meta.symbol)
        return {"symbol": meta.symbol}

    async def fake_upstox():
        return object()

    monkeypatch.setattr(service, "_load_underlyings", fake_load_underlyings)
    monkeypatch.setattr(service, "_build_row", fake_build_row)
    monkeypatch.setattr(service, "_get_upstox_adapter", fake_upstox)
    monkeypatch.setattr(
        "market_data.atm_watchlist.get_active_adapter", lambda name: None
    )

    report = await service.refresh_stock_snapshot_rows(
        ["RELIANCE", "TCS", "NOTLISTED", "NIFTY"], budget_seconds=10.0, concurrency=2
    )

    assert report["RELIANCE"] == "refreshed"
    assert report["TCS"] == "refreshed"
    assert report["NOTLISTED"] == "unknown_symbol"
    # INDEX metas never route through the stock refresh path.
    assert report["NIFTY"] == "unknown_symbol"
    assert sorted(built) == ["RELIANCE", "TCS"]


@pytest.mark.asyncio
async def test_refresh_budget_exhaustion_reports_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    service = ATMWatchlistService()
    never = asyncio.Event()

    async def fake_load_underlyings():
        return [_stock_meta("RELIANCE")]

    async def hanging_build_row(meta, expiry, expiry_date, upstox_adapter, fyers_adapter):
        await never.wait()

    async def fake_upstox():
        return object()

    monkeypatch.setattr(service, "_load_underlyings", fake_load_underlyings)
    monkeypatch.setattr(service, "_build_row", hanging_build_row)
    monkeypatch.setattr(service, "_get_upstox_adapter", fake_upstox)
    monkeypatch.setattr(
        "market_data.atm_watchlist.get_active_adapter", lambda name: None
    )

    report = await service.refresh_stock_snapshot_rows(
        ["RELIANCE"], budget_seconds=1.0, concurrency=1
    )

    # Row never admitted/finished inside the budget — caller's post-refresh
    # honesty gate keeps the symbol skipped; nothing fails open.
    assert report["RELIANCE"] == "budget"


@pytest.mark.asyncio
async def test_refresh_without_brokers_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    service = ATMWatchlistService()

    async def fake_load_underlyings():
        return [_stock_meta("RELIANCE"), _stock_meta("TCS")]

    async def no_upstox():
        return None

    monkeypatch.setattr(service, "_load_underlyings", fake_load_underlyings)
    monkeypatch.setattr(service, "_get_upstox_adapter", no_upstox)
    monkeypatch.setattr(
        "market_data.atm_watchlist.get_active_adapter", lambda name: None
    )

    report = await service.refresh_stock_snapshot_rows(["RELIANCE", "TCS"])

    assert report == {
        "RELIANCE": "error:no_active_broker",
        "TCS": "error:no_active_broker",
    }


@pytest.mark.asyncio
async def test_refresh_isolates_per_symbol_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    service = ATMWatchlistService()

    async def fake_load_underlyings():
        return [_stock_meta("RELIANCE"), _stock_meta("TCS")]

    async def flaky_build_row(meta, expiry, expiry_date, upstox_adapter, fyers_adapter):
        if meta.symbol == "TCS":
            raise RuntimeError("chain fetch exploded")
        return {"symbol": meta.symbol}

    async def fake_upstox():
        return object()

    monkeypatch.setattr(service, "_load_underlyings", fake_load_underlyings)
    monkeypatch.setattr(service, "_build_row", flaky_build_row)
    monkeypatch.setattr(service, "_get_upstox_adapter", fake_upstox)
    monkeypatch.setattr(
        "market_data.atm_watchlist.get_active_adapter", lambda name: None
    )

    report = await service.refresh_stock_snapshot_rows(["RELIANCE", "TCS"], budget_seconds=10.0)

    assert report["RELIANCE"] == "refreshed"
    assert report["TCS"].startswith("error:")
    assert "chain fetch exploded" in report["TCS"]
