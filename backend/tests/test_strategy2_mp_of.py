"""Contract tests for :mod:`paper_engine.strategy2_mp_of`.

These verify the small pure-function surface (signal→side mapping and
expiry routing) plus the assembly of an S2-shaped result dict. We
intentionally don't go through the SQL-loading or the MarketProfileEngine
build path here — those are exercised by the commodity tests already
and by the integration smoke against the deployed service.
"""
from __future__ import annotations

from paper_engine import strategy2_mp_of as s2


def test_expiry_routing_nifty_sensex_have_weekly_and_monthly() -> None:
    assert s2.expiry_tracks_for("NIFTY") == ("weekly", "monthly")
    assert s2.expiry_tracks_for("SENSEX") == ("weekly", "monthly")


def test_expiry_routing_others_are_monthly_only() -> None:
    # NSE discontinued the BANKNIFTY/FINNIFTY/MIDCPNIFTY weeklies in 2024.
    assert s2.expiry_tracks_for("BANKNIFTY") == ("monthly",)
    assert s2.expiry_tracks_for("FINNIFTY") == ("monthly",)
    assert s2.expiry_tracks_for("MIDCPNIFTY") == ("monthly",)


def test_expiry_routing_unknown_underlying_falls_back_to_monthly() -> None:
    # Unknown root → safe fallback so a typo or new symbol doesn't drop
    # the signal silently.
    assert s2.expiry_tracks_for("ASDF") == ("monthly",)


def test_expiry_routing_is_case_insensitive() -> None:
    assert s2.expiry_tracks_for("nifty") == ("weekly", "monthly")


def test_map_signal_buy_to_ce() -> None:
    assert s2.map_signal_to_option_side("BUY") == "CE"


def test_map_signal_sell_to_pe() -> None:
    assert s2.map_signal_to_option_side("SELL") == "PE"


def test_map_signal_none_is_none() -> None:
    assert s2.map_signal_to_option_side(None) is None
    assert s2.map_signal_to_option_side("FLAT") is None


def test_shape_result_tags_side_and_routing() -> None:
    raw = {"signal": "BUY", "entry_style": "open_drive", "confidence": 0.85}
    shaped = s2.shape_result_for_s2(raw, underlying="NIFTY")

    assert shaped["signal"] == "BUY"
    assert shaped["entry_style"] == "open_drive"  # passed through untouched
    assert shaped["side"] == "CE"
    assert shaped["expiry_tracks"] == ("weekly", "monthly")
    assert shaped["underlying"] == "NIFTY"


def test_shape_result_for_silent_signal_drops_side() -> None:
    shaped = s2.shape_result_for_s2(
        {"signal": None, "reason": "no_trigger"},
        underlying="BANKNIFTY",
    )
    assert shaped["side"] is None
    assert shaped["expiry_tracks"] == ("monthly",)


def test_shape_result_does_not_mutate_input() -> None:
    raw = {"signal": "SELL"}
    s2.shape_result_for_s2(raw, underlying="SENSEX")
    # We rely on this for idempotent re-evaluation per bar.
    assert "side" not in raw
    assert "expiry_tracks" not in raw


def test_tick_size_table_covers_full_universe() -> None:
    for underlying in s2.S2_EXPIRY_ROUTING:
        assert underlying in s2.S2_TICK_SIZE, f"missing tick size for {underlying}"
        assert s2.S2_TICK_SIZE[underlying] > 0
