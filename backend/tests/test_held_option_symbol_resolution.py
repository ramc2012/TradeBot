"""Tests for held-position option symbol resolution.

Covers the deterministic monthly Fyers symbol builder used for NSE stock
options (the index option-chain endpoint can't resolve those). The format
is validated against the broker-resolved index leg observed on prod:
``NSE:MIDCPNIFTY26JUN14400PE``.
"""
from __future__ import annotations

from market_data import option_subscription_manager as osm


def test_builder_stock_call_monthly() -> None:
    assert (
        osm._build_fyers_monthly_option_symbol("DIXON", "2026-06-30", 11700, "CE")
        == "NSE:DIXON26JUN11700CE"
    )


def test_builder_stock_put_monthly() -> None:
    assert (
        osm._build_fyers_monthly_option_symbol("KPITTECH", "2026-06-30", 770, "PE")
        == "NSE:KPITTECH26JUN770PE"
    )


def test_builder_matches_known_index_format() -> None:
    # Mirrors the broker-resolved leg seen in the prod tick feed.
    assert (
        osm._build_fyers_monthly_option_symbol("MIDCPNIFTY", "2026-06-30", 14400, "PE")
        == "NSE:MIDCPNIFTY26JUN14400PE"
    )


def test_builder_uppercases_symbol() -> None:
    assert (
        osm._build_fyers_monthly_option_symbol("phoenixltd", "2026-06-30", 1740, "PE")
        == "NSE:PHOENIXLTD26JUN1740PE"
    )


def test_builder_renders_whole_strike_without_decimal() -> None:
    assert (
        osm._build_fyers_monthly_option_symbol("WAAREEENER", "2026-06-30", 3100.0, "CE")
        == "NSE:WAAREEENER26JUN3100CE"
    )


def test_builder_preserves_fractional_strike() -> None:
    """Half-rung strikes must NOT be rounded (2026-08-04).

    The builder used to do ``int(round(float(strike)))``, so a held ITC 287.5
    PE asked the WS for ``NSE:ITC26AUG288PE`` — a contract that does not exist
    — and the leg never ticked. 19 NSE underlyings list x.50 strikes. Fyers
    itself uses decimals: ``NSE:ONGC26JUL247.5CE`` is broker-fed.
    """
    assert (
        osm._build_fyers_monthly_option_symbol("ITC", "2026-08-25", 287.5, "PE")
        == "NSE:ITC26AUG287.5PE"
    )


def test_builder_rejects_bad_option_type() -> None:
    assert osm._build_fyers_monthly_option_symbol("DIXON", "2026-06-30", 11700, "XX") is None


def test_builder_rejects_bad_expiry() -> None:
    assert osm._build_fyers_monthly_option_symbol("DIXON", "not-a-date", 11700, "CE") is None


def test_builder_rejects_zero_strike() -> None:
    assert osm._build_fyers_monthly_option_symbol("DIXON", "2026-06-30", 0, "CE") is None


def test_month_codes_across_year() -> None:
    # Spot-check a few months render as 3-letter uppercase.
    assert osm._build_fyers_monthly_option_symbol("X", "2026-01-27", 100, "CE") == "NSE:X26JAN100CE"
    assert osm._build_fyers_monthly_option_symbol("X", "2026-12-29", 100, "CE") == "NSE:X26DEC100CE"
