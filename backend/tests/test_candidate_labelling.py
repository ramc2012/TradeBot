"""Outcome labelling + cost model — pure computation, no DB.

The load-bearing assertions here are the honesty ones: that a mark which cannot
be made is REPORTED rather than substituted, that a realized lag is never
silently rounded to the horizon it was asked for, and that a measured spread is
never fused with an assumed one.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from candidate_capture.costs import (
    FALLBACK_HALF_SPREAD_PCT,
    NSE_OPTION_TICK,
    breakeven_move_pct,
    measured_half_spread_pct,
    round_to_tick,
    round_trip_cost,
    statutory_round_trip,
)
from candidate_capture.labelling import (
    NO_TRADE_ROW,
    OK,
    UNLABELLABLE_NO_FORWARD,
    UNLABELLABLE_OUT_OF_TOLERANCE,
    UNLABELLABLE_SOURCE_DARK,
    ForwardMark,
    build_outcome_row,
    build_spot_path,
    barrier_width_for_horizon,
    realized_vol_per_sqrt_second,
    select_forward_mark,
    tolerance_window,
)

UTC = timezone.utc
T0 = datetime(2026, 8, 25, 5, 0, 0, tzinfo=UTC)
LOT = 65


def _ticks(prices, *, start=T0, step_seconds=1):
    return [(start + timedelta(seconds=step_seconds * (i + 1)), p) for i, p in enumerate(prices)]


def _sample(offset_seconds, price, **kw):
    row = {
        "time": T0 + timedelta(seconds=offset_seconds),
        "price": price,
        "bid": None,
        "ask": None,
        "volume": None,
        "oi": None,
        "source": "option_chain_snapshots",
    }
    row.update(kw)
    return row


# ══════════════════════════════════════════════════════════════════════════
# Cost model
# ══════════════════════════════════════════════════════════════════════════
class TestCostModel:
    def test_tick_rounding_is_real(self):
        assert round_to_tick(402.33) == 402.35
        assert round_to_tick(402.31) == 402.30
        assert round_to_tick(100.0) == 100.0
        # A sub-tick adjustment must not survive as a distinct price — this is
        # why 5 bps of "slippage" on a 20 rupee option is fictional.
        assert round_to_tick(20.0 * 1.0005) == round_to_tick(20.0)

    def test_measured_half_spread_from_a_real_quote(self):
        half, notes = measured_half_spread_pct(bid=402.35, ask=403.95)
        assert half == pytest.approx(0.001984, abs=1e-5)
        assert notes == []

    def test_unusable_quotes_are_reported_not_defaulted(self):
        assert measured_half_spread_pct(bid=None, ask=10.0)[0] is None
        assert measured_half_spread_pct(bid=0.0, ask=10.0)[0] is None
        assert measured_half_spread_pct(bid=12.0, ask=10.0) == (None, ["crossed_quote"])

    def test_statutory_layer_matches_the_repo_implementation(self):
        from paper_engine.costs import round_trip_charges

        for premium in (20.0, 100.0, 250.0):
            mine = statutory_round_trip(
                entry_price=premium, exit_price=premium, quantity=LOT
            )
            theirs = round_trip_charges(
                symbol="NIFTY", instrument_type="CE",
                entry_price=premium, exit_price=premium,
                qty=LOT, entry_action="BUY",
            )
            assert mine == pytest.approx(theirs, abs=0.01)

    def test_statutory_is_not_silenced_by_the_paper_toggle(self, monkeypatch):
        """A label must never be zeroed by a paper-book display switch."""
        import paper_engine.costs as pec

        monkeypatch.setattr(pec, "PAPER_APPLY_COSTS", False, raising=False)
        assert pec.round_trip_charges(
            symbol="NIFTY", instrument_type="CE", entry_price=100.0,
            exit_price=100.0, qty=LOT, entry_action="BUY",
        ) == 0.0
        # Ours still charges.
        assert statutory_round_trip(entry_price=100.0, exit_price=100.0, quantity=LOT) > 50.0

    def test_measured_and_assumed_halves_stay_separable(self):
        cost = round_trip_cost(
            entry_mid=403.15, exit_mid=403.15, quantity=LOT, lot_size=LOT,
            entry_bid=402.35, entry_ask=403.95,
        )
        assert cost.entry_half_spread_measured is True
        # No forward quote was supplied, so the exit half is assumed — and says so.
        assert cost.exit_half_spread_measured is False
        assert "exit_half_spread_assumed_from_entry" in cost.notes

    def test_forward_quote_makes_the_exit_half_measured(self):
        cost = round_trip_cost(
            entry_mid=403.15, exit_mid=410.0, quantity=LOT, lot_size=LOT,
            entry_bid=402.35, entry_ask=403.95,
            exit_bid=409.0, exit_ask=411.0,
        )
        assert cost.entry_half_spread_measured is True
        assert cost.exit_half_spread_measured is True

    def test_missing_quote_falls_back_and_flags_it(self):
        cost = round_trip_cost(
            entry_mid=100.0, exit_mid=100.0, quantity=LOT, lot_size=LOT,
        )
        assert cost.entry_half_spread_measured is False
        assert cost.entry_half_spread_pct == FALLBACK_HALF_SPREAD_PCT
        assert "entry_half_spread_assumed" in cost.notes

    def test_liquid_contracts_cost_far_less_than_a_flat_model_assumes(self):
        """The reason the spread term is measured per row rather than assumed."""
        tight = round_trip_cost(
            entry_mid=403.15, exit_mid=403.15, quantity=LOT, lot_size=LOT,
            entry_bid=402.35, entry_ask=403.95,
        )
        wide = round_trip_cost(
            entry_mid=661.15, exit_mid=661.15, quantity=LOT, lot_size=LOT,
            entry_bid=653.00, entry_ask=669.30,
        )
        assert tight.total_pct_of_entry_notional < 0.01
        assert wide.total_pct_of_entry_notional > 0.02
        # A flat per-side model would charge these two the same; they differ ~3x.
        assert wide.total_pct_of_entry_notional > 2 * tight.total_pct_of_entry_notional

    def test_breakeven_is_the_move_that_exactly_pays_for_the_round_trip(self):
        be = breakeven_move_pct(
            entry_mid=403.15, quantity=LOT, lot_size=LOT,
            entry_bid=402.35, entry_ask=403.95,
        )
        assert 0.0 < be < 0.05
        # At exactly breakeven, net P&L is ~0; below it, negative.
        entry = 403.15
        at = round_trip_cost(
            entry_mid=entry, exit_mid=entry * (1 + be), quantity=LOT, lot_size=LOT,
            entry_bid=402.35, entry_ask=403.95,
        )
        gross_at = (entry * (1 + be) - entry) * LOT
        assert gross_at - at.total_rupees == pytest.approx(0.0, abs=entry * LOT * 0.002)

    def test_unreachable_breakeven_is_none_not_the_bracket(self):
        """A cheap contract whose flat brokerage dwarfs the premium has no
        breakeven inside any sane range — that must be NULL, not '500%'."""
        assert breakeven_move_pct(entry_mid=0.30, quantity=30, lot_size=30) is None
        assert breakeven_move_pct(entry_mid=0.05, quantity=75, lot_size=75) is None
        # A normal contract still converges.
        assert 0 < breakeven_move_pct(entry_mid=403.15, quantity=65, lot_size=65) < 0.05

    def test_uncostable_inputs_do_not_fabricate_a_zero(self):
        cost = round_trip_cost(entry_mid=None, exit_mid=10.0, quantity=LOT)
        assert "uncostable_missing_price_or_quantity" in cost.notes
        assert cost.total_pct_of_entry_notional is None
        assert breakeven_move_pct(entry_mid=None, quantity=LOT) is None


# ══════════════════════════════════════════════════════════════════════════
# Stage A — spot path
# ══════════════════════════════════════════════════════════════════════════
class TestSpotPath:
    def test_exact_return_mfe_and_mae(self):
        path = build_spot_path(
            anchor_price=100.0,
            forward_ticks=_ticks([101.0, 103.0, 99.0, 102.0]),
            anchor_time=T0,
            horizon_seconds=300,
            barrier_width_pct=None,
        )
        assert path.return_pct == pytest.approx(0.02)
        assert path.mfe_pct == pytest.approx(0.03)
        assert path.mae_pct == pytest.approx(-0.01)
        assert path.tick_count == 4

    def test_barrier_records_first_touch_and_its_time(self):
        path = build_spot_path(
            anchor_price=100.0,
            forward_ticks=_ticks([100.5, 102.5, 97.0]),
            anchor_time=T0,
            horizon_seconds=300,
            barrier_width_pct=0.02,
        )
        assert path.barrier_hit == "up"
        assert path.time_to_barrier_seconds == pytest.approx(2.0)

    def test_adverse_side_wins_a_simultaneous_touch(self):
        path = build_spot_path(
            anchor_price=100.0,
            forward_ticks=_ticks([97.0]),
            anchor_time=T0,
            horizon_seconds=300,
            barrier_width_pct=0.02,
        )
        assert path.barrier_hit == "down"

    def test_untouched_barrier_is_none_not_null(self):
        path = build_spot_path(
            anchor_price=100.0,
            forward_ticks=_ticks([100.1, 100.2]),
            anchor_time=T0,
            horizon_seconds=300,
            barrier_width_pct=0.05,
        )
        assert path.barrier_hit == "none"

    def test_no_ticks_yields_nulls_not_a_flat_return(self):
        path = build_spot_path(
            anchor_price=100.0, forward_ticks=[], anchor_time=T0,
            horizon_seconds=300, barrier_width_pct=0.01,
        )
        assert path.return_pct is None
        assert path.tick_count == 0

    def test_realized_vol_refuses_a_sample_too_small_to_be_one(self):
        # Too few ticks.
        assert realized_vol_per_sqrt_second(_ticks([100.0, 100.5, 101.0])) is None
        # Enough ticks but far too short a wall-clock span: at ~4 ticks/second a
        # count guard alone would wave this through during a feed stall.
        dense = _ticks([100.0 + (i % 5) * 0.1 for i in range(200)], step_seconds=0.25)
        assert realized_vol_per_sqrt_second(dense) is None
        # Enough ticks AND enough span.
        real = _ticks([100.0 + (i % 5) * 0.1 for i in range(200)], step_seconds=5)
        assert realized_vol_per_sqrt_second(real) > 0

    def test_barrier_width_scales_with_the_horizon(self):
        series = _ticks([100.0 + (i % 7) * 0.05 for i in range(400)], step_seconds=5)
        sigma = realized_vol_per_sqrt_second(series)
        w300 = barrier_width_for_horizon(sigma, 300)
        w3600 = barrier_width_for_horizon(sigma, 3600)
        # sqrt(3600/300) = sqrt(12) ~ 3.46x wider at the hour.
        assert w3600 == pytest.approx(w300 * (3600 / 300) ** 0.5, rel=1e-6)
        assert barrier_width_for_horizon(None, 300) is None

    def test_truncated_window_is_flagged_not_reported_as_complete(self):
        # 60-minute horizon but the tape stops after 10 minutes.
        path = build_spot_path(
            anchor_price=100.0,
            forward_ticks=_ticks([100.1, 100.2], step_seconds=300),
            anchor_time=T0,
            horizon_seconds=3600,
            barrier_width_pct=0.01,
        )
        assert path.window_complete is False
        assert path.forward_lag_seconds == pytest.approx(600.0)

    def test_complete_window_is_marked_complete(self):
        path = build_spot_path(
            anchor_price=100.0,
            forward_ticks=_ticks([100.1, 100.2], step_seconds=150),
            anchor_time=T0,
            horizon_seconds=300,
            barrier_width_pct=0.01,
        )
        assert path.window_complete is True
        assert path.forward_lag_seconds == pytest.approx(300.0)


# ══════════════════════════════════════════════════════════════════════════
# Stage B — forward mark selection
# ══════════════════════════════════════════════════════════════════════════
class TestForwardMark:
    def test_tolerance_band_is_asymmetric_and_scales(self):
        lo, hi = tolerance_window(300)
        assert lo == 240.0 and hi == 450.0
        lo_h, hi_h = tolerance_window(3600)
        assert hi_h - 3600 > 3600 - lo_h  # late tolerance is wider

    def test_picks_the_sample_nearest_the_horizon_not_the_first_past_it(self):
        mark = select_forward_mark(
            samples=[_sample(280, 105.0), _sample(600, 120.0)],
            anchor_time=T0, anchor_price=100.0, horizon_seconds=300,
        )
        assert mark.status == OK
        assert mark.price == 105.0
        assert mark.lag_seconds == pytest.approx(280.0)

    def test_realized_lag_is_reported_not_rounded_to_the_horizon(self):
        mark = select_forward_mark(
            samples=[_sample(407, 105.0)],
            anchor_time=T0, anchor_price=100.0, horizon_seconds=300,
        )
        assert mark.status == OK
        assert mark.lag_seconds == pytest.approx(407.0)  # NOT 300

    def test_out_of_tolerance_is_flagged_rather_than_accepted(self):
        mark = select_forward_mark(
            samples=[_sample(900, 105.0)],
            anchor_time=T0, anchor_price=100.0, horizon_seconds=300,
        )
        assert mark.status == UNLABELLABLE_OUT_OF_TOLERANCE
        assert "900" in mark.reason

    def test_an_in_band_mark_beats_a_closer_out_of_band_one(self):
        """A usable observation must not be discarded because a worse one
        sorted first on distance-to-horizon."""
        mark = select_forward_mark(
            samples=[_sample(230, 111.0), _sample(300, 105.0)],
            anchor_time=T0, anchor_price=100.0, horizon_seconds=300,
        )
        assert mark.status == OK
        assert mark.price == 105.0  # the in-band one, not the nearer 230s sample

    def test_no_samples_is_unlabellable(self):
        mark = select_forward_mark(
            samples=[], anchor_time=T0, anchor_price=100.0, horizon_seconds=300
        )
        assert mark.status == UNLABELLABLE_NO_FORWARD

    def test_samples_at_or_before_the_anchor_are_refused(self):
        mark = select_forward_mark(
            samples=[_sample(-60, 99.0), _sample(0, 100.0)],
            anchor_time=T0, anchor_price=100.0, horizon_seconds=300,
        )
        assert mark.status == UNLABELLABLE_NO_FORWARD

    def test_path_statistics_use_only_samples_up_to_the_mark(self):
        mark = select_forward_mark(
            samples=[_sample(100, 110.0), _sample(300, 105.0), _sample(420, 999.0)],
            anchor_time=T0, anchor_price=100.0, horizon_seconds=300,
        )
        assert mark.price == 105.0
        assert mark.mfe_pct == pytest.approx(0.10)  # the 999 is beyond the mark
        assert mark.sample_count == 2


# ══════════════════════════════════════════════════════════════════════════
# Row assembly
# ══════════════════════════════════════════════════════════════════════════
def _anchor(**kw):
    row = {
        "time": T0,
        "decision_id": "11111111-1111-1111-1111-111111111111",
        "underlying": "NIFTY",
        "expiry": date(2026, 9, 29),
        "strike": 24200.0,
        "option_type": "CE",
        "ltp": 402.30,
        "bid": 402.35,
        "ask": 403.95,
        "volume": 1000,
        "oi": 2039180,
        "spot": 24208.0,
    }
    row.update(kw)
    return row


_GOOD_SPOT = build_spot_path(
    anchor_price=24208.0,
    forward_ticks=_ticks([24210.0, 24250.0]),
    anchor_time=T0, horizon_seconds=300, barrier_width_pct=0.002,
)


class TestOutcomeRow:
    def test_full_row_is_net_of_costs_and_carries_provenance(self):
        mark = ForwardMark(
            price=430.0, lag_seconds=305.0, source="candidate_snapshots",
            sample_count=2, volume=1500, oi=2040000.0, bid=429.0, ask=431.0,
            mfe_pct=0.07, mae_pct=-0.01, status=OK, reason=None,
        )
        row = build_outcome_row(
            anchor=_anchor(), horizon_seconds=300, spot=_GOOD_SPOT,
            mark=mark, lot_size=LOT,
        )
        assert row["label_status"] == OK
        assert row["option_gross_return_pct"] > 0
        # Net must be strictly below gross — costs were actually charged.
        assert row["option_net_return_pct"] < row["option_gross_return_pct"]
        assert row["cost_total_rupees"] > 0
        assert row["forward_lag_seconds"] == 305.0
        assert row["forward_source"] == "candidate_snapshots"
        assert row["exit_half_spread_measured"] is True
        assert row["breakeven_move_pct"] > 0
        # Spot stage is populated independently of the option stage.
        assert row["spot_return_pct"] is not None
        assert row["spot_tick_count"] == 2

    def test_trade_arrival_separates_flat_from_untraded(self):
        traded = build_outcome_row(
            anchor=_anchor(volume=1000), horizon_seconds=300, spot=_GOOD_SPOT,
            mark=ForwardMark(402.30, 300.0, "x", 1, 1500, None, None, None, 0.0, 0.0, OK, None),
            lot_size=LOT,
        )
        assert traded["trade_arrived"] is True
        assert traded["volume_delta"] == 500

        untraded = build_outcome_row(
            anchor=_anchor(volume=1000), horizon_seconds=300, spot=_GOOD_SPOT,
            mark=ForwardMark(402.30, 300.0, "x", 1, 1000, None, None, None, 0.0, 0.0, OK, None),
            lot_size=LOT,
        )
        # Same zero return, but the reason is completely different.
        assert untraded["trade_arrived"] is False
        assert untraded["volume_delta"] == 0

    def test_no_trade_candidate_carries_the_spot_outcome_only(self):
        row = build_outcome_row(
            anchor=_anchor(option_type="NO_TRADE", strike=None, bid=None, ask=None, ltp=None),
            horizon_seconds=300, spot=_GOOD_SPOT,
            mark=ForwardMark(None, None, None, 0, None, None, None, None, None, None,
                             UNLABELLABLE_NO_FORWARD, "n/a"),
            lot_size=None,
        )
        assert row["label_status"] == NO_TRADE_ROW
        # Abstention's outcome is exactly the move you declined — measured.
        assert row["spot_return_pct"] is not None
        assert row["option_gross_return_pct"] is None

    def test_dark_source_is_distinguished_from_a_missing_contract(self):
        row = build_outcome_row(
            anchor=_anchor(), horizon_seconds=300, spot=_GOOD_SPOT,
            mark=ForwardMark(None, None, None, 0, None, None, None, None, None, None,
                             UNLABELLABLE_NO_FORWARD, "no forward sample"),
            lot_size=LOT, source_dark=True,
        )
        assert row["label_status"] == UNLABELLABLE_SOURCE_DARK
        assert "ZERO rows for this session" in row["label_reason"]
        # Spot still labelled — the tick tape was healthy on those days.
        assert row["spot_return_pct"] is not None

    def test_unmarkable_contract_is_stored_not_dropped(self):
        row = build_outcome_row(
            anchor=_anchor(), horizon_seconds=300, spot=_GOOD_SPOT,
            mark=ForwardMark(None, None, None, 0, None, None, None, None, None, None,
                             UNLABELLABLE_NO_FORWARD, "no forward sample"),
            lot_size=LOT,
        )
        assert row["label_status"] == UNLABELLABLE_NO_FORWARD
        assert row["option_net_return_pct"] is None
        assert row["label_version"]

    def test_economic_decidability_is_recorded_per_row(self):
        # A move far larger than breakeven → decidable.
        big = build_outcome_row(
            anchor=_anchor(), horizon_seconds=300, spot=_GOOD_SPOT,
            mark=ForwardMark(500.0, 300.0, "x", 3, 1500, None, None, None, 0.25, -0.01, OK, None),
            lot_size=LOT,
        )
        assert big["economically_decidable"] is True
        # A move smaller than breakeven → not decidable at this horizon.
        # A contract that only ever fell: its "best" excursion is a LOSS, so it
        # cannot have paid for a long round trip however large the magnitude.
        only_fell = build_outcome_row(
            anchor=_anchor(), horizon_seconds=300, spot=_GOOD_SPOT,
            mark=ForwardMark(300.0, 300.0, "x", 3, 1500, None, None, None, -0.25, -0.30, OK, None),
            lot_size=LOT,
        )
        assert only_fell["economically_decidable"] is False

        tiny = build_outcome_row(
            anchor=_anchor(), horizon_seconds=300, spot=_GOOD_SPOT,
            mark=ForwardMark(402.4, 300.0, "x", 3, 1500, None, None, None, 0.0002, -0.0001, OK, None),
            lot_size=LOT,
        )
        assert tiny["economically_decidable"] is False
