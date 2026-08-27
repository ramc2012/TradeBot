"""Futures symbols must subscribe by Upstox instrument KEY, both directions.

Upstox's MarketDataStreamerV3 subscribes by instrument key (NSE_FO|58072), never
by a display symbol. `to_broker_symbol` knew only the five index app-symbols, so
every futures subscription was handed a raw string like NSE:NIFTY26AUGFUT and was
silently INERT — no error, no ticks. `market_ticks` has therefore carried only 5
symbols since 2026-08-07, starving tick_fresh / real_tick_cvd /
confirmation_2_of_3, which are ANDed into every direction's gate set — so BOTH
convergence lanes could not emit an actionable row for 12+ days.
"""
from __future__ import annotations

import market_data.symbols as symbols_module
from market_data.symbols import (
    APP_TO_BROKER_SYMBOL,
    register_broker_symbol,
    to_app_symbol,
    to_broker_symbol,
)


def _clear_dynamic() -> None:
    symbols_module._DYNAMIC_APP_TO_BROKER.clear()
    symbols_module._DYNAMIC_BROKER_TO_APP.clear()


def test_unregistered_futures_symbol_passes_through_unchanged() -> None:
    """Baseline: this passthrough IS the bug — the raw symbol reaches Upstox."""
    _clear_dynamic()
    assert to_broker_symbol("NSE:NIFTY26AUGFUT") == "NSE:NIFTY26AUGFUT"


def test_registered_futures_symbol_translates_both_ways() -> None:
    """Both directions are load-bearing. Subscribing by key is only half: `_on_tick`
    normalises every inbound tick with `to_app_symbol`, so without the REVERSE
    entry ticks persist under NSE_FO|58072 and the lanes — which query
    market_ticks by app symbol — still find nothing."""
    _clear_dynamic()
    register_broker_symbol("NSE:NIFTY26AUGFUT", "NSE_FO|58072")

    assert to_broker_symbol("NSE:NIFTY26AUGFUT") == "NSE_FO|58072"
    assert to_app_symbol("NSE_FO|58072") == "NSE:NIFTY26AUGFUT"
    # Round trip is stable — a tick normalised once must not re-translate.
    assert to_app_symbol(to_broker_symbol("NSE:NIFTY26AUGFUT")) == "NSE:NIFTY26AUGFUT"


def test_dynamic_registry_can_never_shadow_the_static_index_map() -> None:
    """The five index symbols are the ONLY feed that currently works. A bad
    resolution must not be able to break them."""
    _clear_dynamic()
    register_broker_symbol("NSE:NIFTY50-INDEX", "NSE_FO|999999")

    assert to_broker_symbol("NSE:NIFTY50-INDEX") == APP_TO_BROKER_SYMBOL["NSE:NIFTY50-INDEX"]
    assert "NSE:NIFTY50-INDEX" not in symbols_module._DYNAMIC_APP_TO_BROKER


def test_registration_ignores_blank_input() -> None:
    _clear_dynamic()
    register_broker_symbol("", "NSE_FO|1")
    register_broker_symbol("NSE:NIFTY26AUGFUT", "")
    assert symbols_module._DYNAMIC_APP_TO_BROKER == {}


def test_mcx_futures_translate_by_key_too() -> None:
    """MCX has the identical failure: MCX:GOLD26OCTFUT is Fyers notation and is
    inert on Upstox, which needs MCX_FO|483079."""
    _clear_dynamic()
    register_broker_symbol("MCX:GOLD26OCTFUT", "MCX_FO|483079")
    assert to_broker_symbol("MCX:GOLD26OCTFUT") == "MCX_FO|483079"
    assert to_app_symbol("MCX_FO|483079") == "MCX:GOLD26OCTFUT"
