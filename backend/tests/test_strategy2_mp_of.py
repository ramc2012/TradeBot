"""Contract tests for :mod:`paper_engine.strategy2_mp_of`.

These verify the small pure-function surface (signal→side mapping and
expiry routing) plus the assembly of an S2-shaped result dict. We
intentionally don't go through the SQL-loading or the MarketProfileEngine
build path here — those are exercised by the commodity tests already
and by the integration smoke against the deployed service.
"""
from __future__ import annotations

from freezegun import freeze_time

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


def test_s2_symbol_supported_only_for_routed_symbols() -> None:
    # Capability gate: the five routed indices are supported; anything else
    # (a typo, a de-scoped symbol, a future addition) fails closed instead of
    # trading a defaulted monthly expiry.
    for symbol in ("NIFTY", "SENSEX", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
        assert s2.s2_symbol_supported(symbol)
    assert not s2.s2_symbol_supported("BANKEX")
    assert not s2.s2_symbol_supported("ASDF")
    assert not s2.s2_symbol_supported("")
    assert not s2.s2_symbol_supported(None)


def test_s2_symbol_supported_is_case_insensitive() -> None:
    assert s2.s2_symbol_supported("nifty")
    assert not s2.s2_symbol_supported("bankex")


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


# ─── resolve_s2_expiry_targets ────────────────────────────────────────────


def _scope(monthlies: dict[str, str], expiries: list[str]) -> dict:
    """Mimic the shape of atm_watchlist_service.get_expiries()."""
    return {"index_monthlies": monthlies, "expiries": expiries}


# resolve_s2_expiry_targets picks the "earliest non-monthly expiry after today" as the
# weekly. Freeze "now" inside the fixture's June ladder (the test's own comment assumes
# a run date >= 2026-05-31) so 2026-06-04 is the nearest forward weekly regardless of
# when the suite actually runs.
@freeze_time("2026-06-01")
def test_resolve_nifty_picks_weekly_and_monthly() -> None:
    scope = _scope(
        {"NIFTY": "2026-06-30"},
        ["2026-06-04", "2026-06-11", "2026-06-25", "2026-06-30", "2026-07-30"],
    )
    targets = s2.resolve_s2_expiry_targets("NIFTY", scope)
    # Monthly first (preserved ordering), then weekly.
    assert ("monthly", "2026-06-30") in targets
    # Weekly = earliest non-monthly expiry after today. Test runs at
    # date >= 2026-05-31 so 2026-06-04 is the nearest forward weekly.
    weekly = [(t, e) for t, e in targets if t == "weekly"]
    assert len(weekly) == 1
    assert weekly[0][1] < "2026-06-30"


def test_resolve_banknifty_monthly_only_even_when_weekly_exists() -> None:
    # BANKNIFTY policy says monthly-only, regardless of what the broker
    # chain lists. We deliberately drop weekly contracts here.
    scope = _scope(
        {"BANKNIFTY": "2026-06-30"},
        ["2026-06-25", "2026-06-30"],
    )
    targets = s2.resolve_s2_expiry_targets("BANKNIFTY", scope)
    tracks = [t for t, _ in targets]
    assert "monthly" in tracks
    assert "weekly" not in tracks


def test_resolve_sensex_with_only_one_listed_expiry() -> None:
    # SENSEX currently shows just the monthly contract in the live chain
    # (the BSE weekly may be a separate symbol). Resolver should still
    # surface monthly without throwing.
    scope = _scope(
        {"SENSEX": "2026-06-25"},
        ["2026-06-25"],
    )
    targets = s2.resolve_s2_expiry_targets("SENSEX", scope)
    assert targets == [("monthly", "2026-06-25")]


def test_resolve_sensex_ignores_nifty_weekly_ladder() -> None:
    # The global `expiries` ladder is NIFTY's NSE board; it must NOT be used
    # to pick a SENSEX weekly (that would be a NIFTY date with no SENSEX
    # contract, blocking the underlying). SENSEX degrades to monthly-only.
    scope = _scope(
        {"SENSEX": "2026-06-30"},
        ["2026-06-04", "2026-06-11", "2026-06-25", "2026-06-30"],  # NIFTY dates
    )
    targets = s2.resolve_s2_expiry_targets("SENSEX", scope)
    assert targets == [("monthly", "2026-06-30")]
    assert all(t != "weekly" for t, _ in targets)


# ─── select_s2_expiry_targets (catalog-backed) ────────────────────────────


def test_select_nifty_weekly_and_monthly_from_catalog() -> None:
    # NIFTY: catalog monthlies 06-30/07-28; listed has its own weeklies.
    targets = s2.select_s2_expiry_targets(
        "NIFTY",
        monthlies=["2026-06-30", "2026-07-28"],
        listed_expiries=["2026-06-09", "2026-06-16", "2026-06-23", "2026-06-30"],
        today_iso="2026-06-05",
    )
    assert ("monthly", "2026-06-30") in targets
    weekly = [(t, e) for t, e in targets if t == "weekly"]
    assert len(weekly) == 1
    assert weekly[0][1] == "2026-06-09"  # nearest weekly before the monthly


def test_select_sensex_uses_its_own_bse_weeklies() -> None:
    # SENSEX: its OWN listed expiries (BSE), not NIFTY's. Now it gets a weekly.
    targets = s2.select_s2_expiry_targets(
        "SENSEX",
        monthlies=["2026-06-25", "2026-07-30"],
        listed_expiries=["2026-06-09", "2026-06-16", "2026-06-25"],
        today_iso="2026-06-05",
    )
    assert ("monthly", "2026-06-25") in targets
    assert ("weekly", "2026-06-09") in targets


def test_select_banknifty_monthly_only() -> None:
    targets = s2.select_s2_expiry_targets(
        "BANKNIFTY",
        monthlies=["2026-06-30", "2026-07-28"],
        listed_expiries=["2026-06-09", "2026-06-30"],
        today_iso="2026-06-05",
    )
    assert targets == [("monthly", "2026-06-30")]


def test_select_rolls_monthly_when_about_to_expire() -> None:
    # Today is the day before the monthly → roll to next month's.
    targets = s2.select_s2_expiry_targets(
        "BANKNIFTY",
        monthlies=["2026-06-30", "2026-07-28"],
        listed_expiries=["2026-06-30", "2026-07-28"],
        today_iso="2026-06-30",  # 0 days to 06-30 → roll
    )
    assert targets == [("monthly", "2026-07-28")]


def test_select_rolls_weekly_when_about_to_expire() -> None:
    # Nearest weekly expires today → roll to the next weekly.
    targets = s2.select_s2_expiry_targets(
        "NIFTY",
        monthlies=["2026-06-30"],
        listed_expiries=["2026-06-09", "2026-06-16", "2026-06-30"],
        today_iso="2026-06-09",  # 06-09 is today → roll to 06-16
    )
    weekly = [e for t, e in targets if t == "weekly"]
    assert weekly == ["2026-06-16"]


def test_select_no_weekly_when_only_monthly_listed() -> None:
    # SENSEX month with no distinct weekly listed → monthly only.
    targets = s2.select_s2_expiry_targets(
        "SENSEX",
        monthlies=["2026-06-25"],
        listed_expiries=["2026-06-25"],
        today_iso="2026-06-05",
    )
    assert targets == [("monthly", "2026-06-25")]


def test_resolve_empty_scope_drops_all() -> None:
    assert s2.resolve_s2_expiry_targets("NIFTY", {}) == []
    assert s2.resolve_s2_expiry_targets("BANKNIFTY", {"index_monthlies": {}}) == []


def test_resolve_skips_today_and_past_for_weekly() -> None:
    # Weekly resolver must not pick a same-day expiry — the contract is
    # essentially dead after the morning open.
    from datetime import date as _date

    today_iso = _date.today().isoformat()
    scope = _scope(
        {"NIFTY": "2026-06-30"},
        [today_iso, "2026-06-25", "2026-06-30"],
    )
    targets = s2.resolve_s2_expiry_targets("NIFTY", scope)
    weekly_expiries = [e for t, e in targets if t == "weekly"]
    assert today_iso not in weekly_expiries


# ─── F3 (2026-07-20): read-boundary contamination guard ───────────────────
#
# ``load_index_1m_spot`` was the one unfiltered consumer of index 1m spot.
# These cover the second (per-session) stage, which is the one that catches the
# same-decade contamination the write-boundary band cannot.

from datetime import datetime, timedelta, timezone  # noqa: E402


def _session_bars(n: int, level: float, start_hour_utc: int = 4) -> list[dict]:
    base = datetime(2026, 7, 17, start_hour_utc, 0, tzinfo=timezone.utc)
    return [
        {
            "time": base + timedelta(minutes=i),
            "open": level,
            "high": level + 5.0,
            "low": level - 5.0,
            "close": level,
            "volume": 100,
        }
        for i in range(n)
    ]


def test_session_band_drops_the_observed_nifty_contamination() -> None:
    bars = _session_bars(200, 24150.0)
    # The 2026-07-17 leak: a 26,923 print ~11.5% off the session median, which
    # sits INSIDE the write-boundary multi-day band.
    bars[50]["high"] = 26923.0
    bars[120]["close"] = 27005.7
    kept, dropped = s2._drop_off_session_bars(bars)
    assert dropped == 2
    assert len(kept) == 198
    assert all(bar["high"] < 26000.0 for bar in kept)
    assert all(bar["close"] < 26000.0 for bar in kept)


def test_session_band_drops_a_low_side_frame() -> None:
    bars = _session_bars(200, 14771.0)
    bars[10]["low"] = 12485.0          # observed MIDCPNIFTY low, -15%
    kept, dropped = s2._drop_off_session_bars(bars)
    assert dropped == 1
    assert len(kept) == 199


def test_session_band_preserves_order_and_never_alters_values() -> None:
    bars = _session_bars(200, 24150.0)
    bars[7]["high"] = 26923.0
    kept, dropped = s2._drop_off_session_bars(bars)
    assert dropped == 1
    times = [bar["time"] for bar in kept]
    assert times == sorted(times)
    # Surviving bars are the same objects, untouched — nothing is clamped,
    # interpolated or fabricated.
    assert kept[0] is bars[0]
    assert all(bar["close"] == 24150.0 for bar in kept)


def test_session_band_leaves_a_clean_session_completely_alone() -> None:
    bars = _session_bars(200, 24150.0)
    kept, dropped = s2._drop_off_session_bars(bars)
    assert dropped == 0
    assert kept is bars


def test_session_band_survives_a_genuine_trending_session() -> None:
    """A real 10% circuit-halt move must NOT be rejected.

    The reference is the session's own median, so it tracks the day — the whole
    reason this stage can be tight where a fixed multi-day anchor cannot.
    """
    base = datetime(2026, 7, 17, 4, 0, tzinfo=timezone.utc)
    bars = []
    for i in range(200):
        level = 24150.0 * (1.0 - 0.10 * (i / 199.0))  # -10% over the session
        bars.append(
            {
                "time": base + timedelta(minutes=i),
                "open": level,
                "high": level,
                "low": level,
                "close": level,
                "volume": 100,
            }
        )
    _kept, dropped = s2._drop_off_session_bars(bars)
    assert dropped == 0


def test_session_band_skips_sessions_with_too_few_bars_to_judge() -> None:
    bars = _session_bars(5, 24150.0)
    bars[0]["close"] = 26923.0
    kept, dropped = s2._drop_off_session_bars(bars)
    assert dropped == 0
    assert len(kept) == 5


def test_session_band_scopes_each_session_independently() -> None:
    day_one = _session_bars(200, 24150.0)
    day_two = [
        {**bar, "time": bar["time"] + timedelta(days=1)} for bar in _session_bars(200, 22000.0)
    ]
    bars = day_one + day_two
    kept, dropped = s2._drop_off_session_bars(bars)
    # Two sessions 9% apart: neither is judged against the other's level.
    assert dropped == 0
    assert len(kept) == 400
