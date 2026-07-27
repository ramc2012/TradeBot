"""Tests for the pre-open spot snapshot + activeness flag (owner spec 2026-07-27).

The contracts under test are the honesty ones:
  * a name with no pre-open data is `unknown`, NEVER `quiet`
  * nothing is ever fabricated — a field that cannot be derived is None and the
    reason is recorded
  * the activeness verdict is COMPUTED from the module's own constants; moving
    a constant moves the verdict, so the definition cannot be hardcoded
  * an out-of-band print (WS cross-symbol contamination) voids the whole frame,
    including the volume, so it cannot poison a relative-volume baseline
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from market_data import preopen_spot as ps

UTC = timezone.utc
SESSION = date(2026, 7, 24)


def _tick(minute: int, *, ltp, volume=0, close=100.0, buy=None, sell=None, bid=None, ask=None):
    return ps.PreopenTick(
        time=datetime(2026, 7, 24, 3, 30, tzinfo=UTC) + timedelta(minutes=minute),
        ltp=ltp,
        volume=volume,
        close=close,
        bid=bid,
        ask=ask,
        total_buy_qty=buy,
        total_sell_qty=sell,
    )


def _daily(n: int, *, close=100.0, rng=2.0, last_session=date(2026, 7, 23)):
    """n consecutive sessions of clean OHLC with a constant absolute range.

    `last_session` is the newest session in the sample, so a test can make the
    sample deliberately stale.
    """
    return [
        {
            "session": last_session - timedelta(days=n - 1 - i),
            "high": close + rng / 2,
            "low": close - rng / 2,
            "close": close,
        }
        for i in range(n)
    ]


def _row(**kw):
    base = dict(
        session_date=SESSION,
        underlying="TESTCO",
        kind="STOCK",
        tick_symbol="NSE:TESTCO-EQ",
        ticks=[],
        spot_prev_close=None,
        daily_ohlc=[],
        baseline_volumes=[],
        universe_source="session_catalog",
    )
    base.update(kw)
    return ps.build_row(**base)


# ── window / symbol plumbing ──────────────────────────────────────────────
def test_preopen_window_is_the_nse_call_auction_band():
    start, end = ps.preopen_window_utc(SESSION)
    assert start.isoformat() == "2026-07-24T03:30:00+00:00"  # 09:00 IST
    assert end.isoformat() == "2026-07-24T03:45:00+00:00"    # 09:15 IST


def test_tick_symbol_mapping():
    assert ps.tick_symbol_for("RELIANCE", "STOCK") == "NSE:RELIANCE-EQ"
    assert ps.tick_symbol_for("NIFTY", "INDEX") == "NSE:NIFTY50-INDEX"
    assert ps.tick_symbol_for("SENSEX", "INDEX") == "BSE:SENSEX-INDEX"
    # In the F&O catalog but NOT in the tick-capture set: must resolve to None
    # so the row records `no_preopen_ticks` rather than silently vanishing.
    assert ps.tick_symbol_for("NIFTYNXT50", "INDEX") is None


# ── THE headline contract: no data ⇒ unknown, never inactive ─────────────
def test_name_with_no_preopen_data_is_unknown_not_quiet():
    row = _row(ticks=[], daily_ohlc=_daily(14))
    assert row["data_status"] == ps.STATUS_NO_TICKS
    assert row["activeness_state"] == ps.STATE_UNKNOWN
    assert row["activeness_state"] != ps.STATE_QUIET
    assert row["preopen_price"] is None
    assert row["preopen_volume"] is None
    assert row["gap_pct"] is None


def test_session_dark_is_recorded_as_absence_not_as_zeros():
    row = _row(ticks=[], session_dark=True, daily_ohlc=_daily(14))
    assert row["data_status"] == ps.STATUS_SESSION_DARK
    assert row["activeness_state"] == ps.STATE_UNKNOWN
    assert row["tick_count"] == 0
    for numeric in ("preopen_price", "preopen_volume", "gap_pct", "activeness_score"):
        assert row[numeric] is None, f"{numeric} must be NULL on a dark session, not 0"


def test_order_collection_frames_without_a_match_are_not_a_price():
    # 09:00 indicative frames only: volume never becomes non-zero.
    row = _row(
        ticks=[_tick(0, ltp=101.0, volume=0), _tick(2, ltp=101.5, volume=0)],
        daily_ohlc=_daily(14),
    )
    assert row["data_status"] == ps.STATUS_NO_MATCH
    assert row["preopen_price"] is None
    assert row["activeness_state"] == ps.STATE_UNKNOWN


def test_matched_auction_print_is_the_last_frame_carrying_volume():
    row = _row(
        ticks=[
            _tick(0, ltp=101.0, volume=0, close=100.0),
            _tick(7, ltp=97.0, volume=5_000, close=100.0, buy=10_000, sell=20_000),
            _tick(8, ltp=97.0, volume=5_000, close=100.0, buy=10_000, sell=20_000),
        ],
        daily_ohlc=_daily(14),
    )
    assert row["data_status"] == ps.STATUS_OK
    assert row["preopen_price"] == 97.0
    assert row["preopen_volume"] == 5_000
    assert row["prev_close"] == 100.0
    assert row["prev_close_source"] == "tick_close_field"
    assert row["gap_pct"] == pytest.approx(-3.0)


# ── indices ───────────────────────────────────────────────────────────────
def test_index_volume_is_null_with_a_recorded_reason_never_zero():
    row = _row(
        underlying="NIFTY",
        kind="INDEX",
        tick_symbol="NSE:NIFTY50-INDEX",
        ticks=[_tick(0, ltp=23900.0, volume=0, close=23869.6),
               _tick(14, ltp=23811.05, volume=0, close=23869.6)],
        daily_ohlc=_daily(14, close=23869.6, rng=200.0),
    )
    assert row["data_status"] == ps.STATUS_OK
    assert row["preopen_volume"] is None
    assert row["data_status_reason"] == "index_no_traded_volume"
    assert row["preopen_price"] == 23811.05


def test_index_stream_that_never_moved_off_prior_close_is_stale_carry():
    row = _row(
        underlying="NIFTYIT",
        kind="INDEX",
        tick_symbol="NSE:NIFTYIT-INDEX",
        ticks=[_tick(0, ltp=28533.55, close=28533.55), _tick(7, ltp=28533.55, close=28533.55)],
        daily_ohlc=_daily(14, close=28533.55, rng=200.0),
    )
    assert row["data_status"] == ps.STATUS_STALE_CARRY
    assert row["preopen_price"] is None
    assert row["activeness_state"] == ps.STATE_UNKNOWN


# ── contamination guard ───────────────────────────────────────────────────
def test_out_of_band_print_voids_price_AND_volume_AND_book():
    # The 2026-07-17 failure mode: a whole coherent frame from another symbol.
    row = _row(
        ticks=[_tick(7, ltp=10_366.0, volume=595, close=2663.5, buy=9_000, sell=1_000)],
        daily_ohlc=_daily(14, close=2663.5, rng=40.0),
    )
    assert row["data_status"] == ps.STATUS_BAND_REJECT
    assert row["preopen_price"] is None
    assert row["preopen_volume"] is None, "a contaminated volume must never reach a baseline"
    assert row["total_buy_qty"] is None and row["total_sell_qty"] is None
    assert row["gap_pct"] is None
    assert row["activeness_state"] == ps.STATE_UNKNOWN
    # Provenance survives: we still know frames arrived.
    assert row["tick_count"] == 1


def test_prev_close_prefers_the_external_anchor_when_the_tick_field_disagrees():
    value, source = ps.resolve_prev_close(tick_prev_close=2663.5, spot_prev_close=1826.7)
    assert value == 1826.7
    assert source == "spot_30m_prior_session_anchor_mismatch"
    value, source = ps.resolve_prev_close(tick_prev_close=1830.0, spot_prev_close=1826.7)
    assert value == 1830.0 and source == "tick_close_field"
    value, source = ps.resolve_prev_close(tick_prev_close=None, spot_prev_close=None)
    assert value is None and source == "unavailable"


# ── ATR ───────────────────────────────────────────────────────────────────
def test_atr_is_none_below_the_minimum_sample_and_the_count_is_reported():
    atr, n = ps.compute_atr_pct(_daily(4))
    assert atr is None
    assert n == 3  # TRs, not sessions — reported so the reason is recordable


def test_atr_drops_a_structurally_impossible_session():
    clean = _daily(14, close=100.0, rng=2.0)
    poisoned = list(clean) + [{"session": date(2026, 7, 17), "high": 57_582.0, "low": 235.0, "close": 249.0}]
    atr_clean, _ = ps.compute_atr_pct(clean)
    atr_poisoned, _ = ps.compute_atr_pct(poisoned)
    assert atr_clean == pytest.approx(2.0)
    assert atr_poisoned == pytest.approx(atr_clean), "a 57,582-high bar must not enter the ATR"


def test_atr_trusts_the_index_broker_history_labels_and_never_the_futures_series():
    """Regression, 2026-07-27.

    The first version of ATR_TRUSTED_SOURCES held only the stock-side labels.
    Index broker history is written by auction_intelligence/live.py under
    `upstox_spot_index` / `fyers_spot_index`, so excluding them silently drove
    atr_sessions_n to 4 for every index and made EVERY index `unknown` on every
    session from 2026-07-15 on. This test fails if those labels are dropped
    again — and equally if a tick-derived or FUTURES series is ever admitted.
    """
    trusted = set(ps.ATR_TRUSTED_SOURCES)
    assert {"upstox_spot_index", "fyers_spot_index"} <= trusted
    # A futures series stored under the index underlying is NOT the spot range.
    assert "fyers_continuous_futures" not in trusted
    # Tick-derived / derived-from-derived series carry the WS cross-symbol
    # contamination this filter exists to keep out.
    for derived in (
        "live_tick",
        "timescaledb_spot_1minute",
        "source_1minute_aggregate",
        "readiness_backfill_aggregate",
        "strategy_agent",
        "institutional_convergence_aggregate",
    ):
        assert derived not in trusted, derived


def test_a_stale_atr_sample_is_refused_and_the_reason_says_stale():
    """A real ATR built from bars that stopped weeks ago is not today's range."""
    # Sample ends 2026-05-01; the session is 2026-07-24 (84 days later).
    stale = _daily(14, close=100.0, rng=2.0, last_session=date(2026, 5, 1))
    row = _row(
        ticks=[_tick(7, ltp=97.0, volume=5_000, close=100.0, buy=1_000, sell=2_000)],
        daily_ohlc=stale,
    )
    assert row["data_status"] == ps.STATUS_OK
    assert row["atr_last_session"] == date(2026, 5, 1)
    assert row["atr_pct_14"] is None, "a stale ATR must not be presented as current"
    assert row["gap_atr_ratio"] is None
    # ...and the recorded reason must say STALE, not imply too small a sample.
    verdict = ps.apply_session_activeness([row])[0]
    reason = verdict["components_unknown"][ps.COMPONENT_GAP_ATR]
    assert reason.startswith("atr_sample_stale_last_session=2026-05-01")
    assert "<10" not in reason

    # The SAME sample, fresh, is used — proving the refusal is the staleness
    # gate and not some unrelated rejection.
    fresh = _daily(14, close=100.0, rng=2.0, last_session=date(2026, 7, 23))
    fresh_row = _row(
        ticks=[_tick(7, ltp=97.0, volume=5_000, close=100.0, buy=1_000, sell=2_000)],
        daily_ohlc=fresh,
    )
    assert fresh_row["atr_pct_14"] == pytest.approx(2.0)
    assert fresh_row["gap_atr_ratio"] == pytest.approx(1.5)


def test_private_carriers_never_reach_the_persisted_column_set():
    """`_atr_unavailable_reason` is plumbing; a stray key would break the bind."""
    row = _row(daily_ohlc=_daily(14))
    assert "_atr_unavailable_reason" in row
    persisted = ps.apply_session_activeness([row])[0]
    assert not [k for k in persisted if k.startswith("_")]


def test_dark_session_reason_wins_over_the_subscription_set_message():
    """On a dark session nothing can be concluded about ANY instrument.

    NIFTYNXT50 has no tick symbol at all, so both facts are true — but the
    dominant one (the whole tape was dark) must be the recorded reason.
    """
    row = _row(
        underlying="NIFTYNXT50",
        kind="INDEX",
        tick_symbol=None,
        ticks=[],
        session_dark=True,
        daily_ohlc=_daily(14),
    )
    assert row["data_status"] == ps.STATUS_SESSION_DARK
    assert "ZERO pre-open frames" in row["data_status_reason"]
    assert row["activeness_state"] == ps.STATE_UNKNOWN


# ── relative volume ───────────────────────────────────────────────────────
def test_rel_volume_is_unknown_below_the_minimum_baseline():
    ratio, median, n = ps.compute_rel_volume(10_000, [1_000, 1_000])
    assert ratio is None
    assert n == 2 < ps.REL_VOLUME_MIN_BASELINE


def test_rel_volume_is_self_relative():
    ratio, median, n = ps.compute_rel_volume(10_000, [1_000, 2_000, 3_000, 1_500])
    assert n == 4
    assert median == pytest.approx(1_750.0)
    assert ratio == pytest.approx(10_000 / 1_750.0)


# ── cross-sectional book imbalance ────────────────────────────────────────
def test_book_imbalance_z_needs_a_cross_section():
    median, sigma, n = ps.peer_imbalance_stats([-0.4, -0.5, -0.3])
    assert n == 3 < ps.BOOK_IMBALANCE_MIN_PEERS
    assert sigma is None
    assert ps.compute_book_imbalance_z(-0.9, median, sigma) is None


def test_book_imbalance_z_removes_the_market_wide_bias():
    # A whole session skewed to the sell side; one name skewed the other way.
    peers = [-0.40, -0.42, -0.38, -0.45, -0.41, -0.39, -0.43, -0.44, 0.30]
    median, sigma, n = ps.peer_imbalance_stats(peers)
    assert n == 9 and sigma is not None
    # A typical sell-skewed name is unremarkable...
    assert abs(ps.compute_book_imbalance_z(-0.41, median, sigma)) < ps.BOOK_IMBALANCE_Z_THRESHOLD
    # ...the odd one out is not.
    assert abs(ps.compute_book_imbalance_z(0.30, median, sigma)) >= ps.BOOK_IMBALANCE_Z_THRESHOLD


# ── the verdict itself is COMPUTED, not hardcoded ─────────────────────────
def test_activeness_tracks_the_module_thresholds_not_a_hardcoded_list(monkeypatch):
    kwargs = dict(
        kind="STOCK",
        data_status=ps.STATUS_OK,
        rel_volume=1.5,
        rel_volume_baseline_n=5,
        gap_atr_ratio=0.4,
        atr_sessions_n=14,
        book_imbalance_z=0.5,
        peer_n=40,
    )
    assert ps.compute_activeness(**kwargs).state == ps.STATE_QUIET
    # Move ONLY the constant: the same inputs must now be active. If the verdict
    # were hardcoded per-name or per-value this could not change.
    monkeypatch.setattr(ps, "REL_VOLUME_THRESHOLD", 1.0)
    verdict = ps.compute_activeness(**kwargs)
    assert verdict.state == ps.STATE_ACTIVE
    assert verdict.reasons == [ps.COMPONENT_REL_VOLUME]


def test_reasons_name_the_component_that_triggered():
    verdict = ps.compute_activeness(
        kind="STOCK",
        data_status=ps.STATUS_OK,
        rel_volume=5.0,
        rel_volume_baseline_n=5,
        gap_atr_ratio=0.1,
        atr_sessions_n=14,
        book_imbalance_z=9.0,
        peer_n=40,
    )
    assert verdict.state == ps.STATE_ACTIVE
    assert set(verdict.reasons) == {ps.COMPONENT_REL_VOLUME, ps.COMPONENT_BOOK_IMBALANCE}
    assert ps.COMPONENT_GAP_ATR in verdict.available
    assert 0.0 <= verdict.score <= 1.0


def test_quiet_requires_enough_evidence_to_be_a_claim():
    # Only one of three applicable stock components computable, none triggered:
    # that is not enough to call the name quiet.
    verdict = ps.compute_activeness(
        kind="STOCK",
        data_status=ps.STATUS_OK,
        rel_volume=None,
        rel_volume_baseline_n=0,
        gap_atr_ratio=0.2,
        atr_sessions_n=14,
        book_imbalance_z=None,
        peer_n=2,
    )
    assert verdict.available == [ps.COMPONENT_GAP_ATR]
    assert verdict.state == ps.STATE_UNKNOWN
    assert ps.COMPONENT_REL_VOLUME in verdict.unknown
    assert ps.COMPONENT_BOOK_IMBALANCE in verdict.unknown


def test_index_is_judged_only_on_its_applicable_component():
    verdict = ps.compute_activeness(
        kind="INDEX",
        data_status=ps.STATUS_OK,
        rel_volume=None,
        rel_volume_baseline_n=0,
        gap_atr_ratio=0.3,
        atr_sessions_n=14,
        book_imbalance_z=None,
        peer_n=0,
    )
    assert verdict.state == ps.STATE_QUIET
    assert verdict.unknown[ps.COMPONENT_REL_VOLUME].startswith("not_applicable")
    assert verdict.unknown[ps.COMPONENT_BOOK_IMBALANCE].startswith("not_applicable")


def test_a_triggered_component_beats_missing_evidence():
    verdict = ps.compute_activeness(
        kind="STOCK",
        data_status=ps.STATUS_OK,
        rel_volume=None,
        rel_volume_baseline_n=0,
        gap_atr_ratio=4.0,
        atr_sessions_n=14,
        book_imbalance_z=None,
        peer_n=0,
    )
    assert verdict.state == ps.STATE_ACTIVE
    assert verdict.reasons == [ps.COMPONENT_GAP_ATR]


def test_bad_data_status_never_yields_a_verdict():
    for status in (ps.STATUS_NO_TICKS, ps.STATUS_NO_MATCH, ps.STATUS_BAND_REJECT,
                   ps.STATUS_STALE_CARRY, ps.STATUS_SESSION_DARK):
        verdict = ps.compute_activeness(
            kind="STOCK", data_status=status,
            rel_volume=99.0, rel_volume_baseline_n=50,
            gap_atr_ratio=99.0, atr_sessions_n=14,
            book_imbalance_z=99.0, peer_n=50,
        )
        assert verdict.state == ps.STATE_UNKNOWN, status
        assert verdict.score is None


# ── session-level phase ───────────────────────────────────────────────────
def test_apply_session_activeness_uses_the_session_cross_section():
    rows = []
    for i in range(12):
        # A realistically dispersed but uniformly sell-skewed cross-section.
        rows.append(_row(
            underlying=f"NAME{i}",
            ticks=[_tick(7, ltp=99.0, volume=1_000, close=100.0,
                         buy=3_000 + 40 * i, sell=7_000 - 40 * i)],
            daily_ohlc=_daily(14),
        ))
    # One outlier with an inverted book.
    rows.append(_row(
        underlying="ODDONE",
        ticks=[_tick(7, ltp=99.0, volume=1_000, close=100.0, buy=9_000, sell=1_000)],
        daily_ohlc=_daily(14),
    ))
    out = ps.apply_session_activeness(rows)
    by_name = {r["underlying"]: r for r in out}
    assert by_name["NAME0"]["peer_n"] == 13
    # A typical member of the sell-skewed pack is unremarkable...
    assert abs(by_name["NAME6"]["book_imbalance_z"]) < ps.BOOK_IMBALANCE_Z_THRESHOLD
    assert by_name["ODDONE"]["book_imbalance_z"] is not None
    assert abs(by_name["ODDONE"]["book_imbalance_z"]) >= ps.BOOK_IMBALANCE_Z_THRESHOLD
    assert ps.COMPONENT_BOOK_IMBALANCE in by_name["ODDONE"]["activeness_reasons"]


def test_definition_version_is_stamped_on_every_row():
    row = _row(daily_ohlc=_daily(14))
    assert row["definition_version"] == ps.DEFINITION_VERSION


# ── runner gating ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_runner_is_a_no_op_while_the_flag_is_off(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "PREOPEN_SPOT_SNAPSHOT_ENABLED", False, raising=False)
    result = await ps.run_preopen_spot_snapshot()
    assert result == {"status": "disabled", "flag": "PREOPEN_SPOT_SNAPSHOT_ENABLED"}


def test_runner_is_registered_and_defaults_off():
    from core.lane_registry import get_registry

    spec = next(s for s in get_registry() if s.key == "preopen_spot_snapshot")
    assert spec.enabled_flag_name == "PREOPEN_SPOT_SNAPSHOT_ENABLED"
    assert spec.runner_keys == ("preopen_spot_snapshot",)
    # No broker REST at all — this reader must never be charged budget.
    assert spec.broker_profile is None

    from core.config import Settings

    assert Settings().PREOPEN_SPOT_SNAPSHOT_ENABLED is False


def test_runner_window_is_after_the_auction_and_clears_the_ladder_build():
    from core.market_hours_paper_supervisor import _in_preopen_snapshot_window

    IST = timezone(timedelta(hours=5, minutes=30))

    def at(h, m):
        return datetime(2026, 7, 27, h, m, tzinfo=IST)

    # 09:04-09:14 is the MACD ladder build; this runner must not start inside
    # the part of it that precedes the auction match.
    assert not _in_preopen_snapshot_window(at(9, 4))
    assert not _in_preopen_snapshot_window(at(9, 11))
    assert _in_preopen_snapshot_window(at(9, 12))
    assert _in_preopen_snapshot_window(at(9, 30))
    assert not _in_preopen_snapshot_window(at(9, 31))
    # Saturday is not an NSE session.
    assert not _in_preopen_snapshot_window(datetime(2026, 7, 25, 9, 20, tzinfo=IST))


def test_mcx_is_excluded_with_a_stated_reason_not_zero_filled():
    assert "no call auction" in ps.MCX_EXCLUSION_REASON
    # MCX roots have no tick-symbol mapping into the auction reader at all.
    assert ps.tick_symbol_for("GOLD", "COMMODITY") == "NSE:GOLD-EQ"  # never queried
    assert "COMMODITY" not in ps.APPLICABLE_COMPONENTS
