"""Taxonomy labelling — pure computation, no DB / broker / clock."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from candidate_capture.taxonomy import (
    EXPIRY_LONG_DATED,
    EXPIRY_MONTHLY,
    EXPIRY_QUARTERLY,
    EXPIRY_UNKNOWN,
    EXPIRY_WEEKLY,
    LIQUIDITY_LOW,
    LIQUIDITY_TOP,
    LIQUIDITY_UNKNOWN,
    MONEYNESS_ATM,
    MONEYNESS_DEEP_ITM,
    MONEYNESS_DEEP_OTM,
    MONEYNESS_NEAR_ITM,
    MONEYNESS_NEAR_OTM,
    MONEYNESS_OTM,
    MONEYNESS_UNKNOWN,
    chain_liquidity_percentiles,
    classify_contract,
    classify_expiry,
    classify_liquidity,
    classify_moneyness,
    classify_underlying,
    expiry_horizon,
    monthly_expiries,
    monthly_expiry_week,
    moneyness_steps,
)

IST = timezone(timedelta(hours=5, minutes=30))
INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]

# A realistic NSE listing: four Tuesday weeklies in August (the last of which
# IS August's monthly), then monthlies out to March.
AUG_LISTED = [
    date(2026, 8, 4), date(2026, 8, 11), date(2026, 8, 18), date(2026, 8, 25),
    date(2026, 9, 29), date(2026, 10, 27), date(2026, 11, 24),
    date(2026, 12, 29), date(2027, 3, 30),
]


class TestUnderlyingClass:
    def test_index_and_stock_split(self):
        assert classify_underlying("NIFTY", INDICES) == "INDEX"
        assert classify_underlying("banknifty", INDICES) == "INDEX"
        assert classify_underlying("RELIANCE", INDICES) == "STOCK"
        assert classify_underlying("ITC", INDICES) == "STOCK"


class TestExpiryClass:
    def test_last_listed_expiry_of_a_month_is_the_monthly(self):
        assert monthly_expiries(AUG_LISTED) == [
            date(2026, 8, 25), date(2026, 9, 29), date(2026, 10, 27),
            date(2026, 11, 24), date(2026, 12, 29), date(2027, 3, 30),
        ]

    def test_earlier_expiries_in_a_month_are_weeklies(self):
        today = date(2026, 8, 3)
        for weekly in (date(2026, 8, 4), date(2026, 8, 11), date(2026, 8, 18)):
            assert classify_expiry(weekly, AUG_LISTED, today=today) == (EXPIRY_WEEKLY, None)

    def test_near_three_monthlies_are_monthly(self):
        today = date(2026, 8, 3)
        for monthly in (date(2026, 8, 25), date(2026, 9, 29), date(2026, 10, 27)):
            assert classify_expiry(monthly, AUG_LISTED, today=today) == (EXPIRY_MONTHLY, None)

    def test_far_monthly_splits_into_quarterly_and_long_dated(self):
        today = date(2026, 8, 3)
        # 4th forward monthly, November — not a quarter-end month.
        assert classify_expiry(date(2026, 11, 24), AUG_LISTED, today=today)[0] == EXPIRY_LONG_DATED
        # December and March are quarter ends.
        assert classify_expiry(date(2026, 12, 29), AUG_LISTED, today=today)[0] == EXPIRY_QUARTERLY
        assert classify_expiry(date(2027, 3, 30), AUG_LISTED, today=today)[0] == EXPIRY_QUARTERLY

    def test_class_shifts_as_the_near_monthly_rolls_off(self):
        # Once August has expired, November becomes the 3rd forward monthly and
        # is a plain MONTHLY — the label tracks the listing, not a fixed date.
        after_august = date(2026, 8, 26)
        assert classify_expiry(date(2026, 11, 24), AUG_LISTED, today=after_august)[0] == EXPIRY_MONTHLY

    def test_unknown_is_returned_with_a_reason_never_guessed(self):
        assert classify_expiry(date(2026, 8, 25), [], today=date(2026, 8, 3)) == (
            EXPIRY_UNKNOWN, "no_listed_expiries_for_underlying"
        )
        assert classify_expiry(date(2026, 8, 25), None, today=date(2026, 8, 3))[0] == EXPIRY_UNKNOWN
        # A date absent from the listing is refused rather than assumed.
        assert classify_expiry(date(2026, 8, 26), AUG_LISTED, today=date(2026, 8, 3)) == (
            EXPIRY_UNKNOWN, "expiry_not_in_listed_set"
        )
        assert classify_expiry("not-a-date", AUG_LISTED)[0] == EXPIRY_UNKNOWN


class TestExpiryHorizon:
    def test_expiry_day_flag_and_hours_run_to_the_close(self):
        now = datetime(2026, 8, 25, 13, 30, tzinfo=IST)
        days, hours, is_expiry_day = expiry_horizon(date(2026, 8, 25), now=now)
        assert days == 0
        assert is_expiry_day is True
        assert hours == pytest.approx(2.0)  # 13:30 → 15:30 IST

    def test_hours_go_negative_after_the_close_on_expiry_day(self):
        now = datetime(2026, 8, 25, 16, 30, tzinfo=IST)
        _, hours, is_expiry_day = expiry_horizon(date(2026, 8, 25), now=now)
        assert is_expiry_day is True
        assert hours == pytest.approx(-1.0)

    def test_future_expiry(self):
        now = datetime(2026, 8, 20, 10, 0, tzinfo=IST)
        days, hours, is_expiry_day = expiry_horizon(date(2026, 8, 25), now=now)
        assert days == 5
        assert is_expiry_day is False
        assert hours > 100

    def test_missing_expiry_yields_nulls_not_zeros(self):
        assert expiry_horizon(None) == (None, None, False)


class TestMonthlyExpiryWeek:
    def test_true_inside_the_monthly_week(self):
        # 2026-08-25 is a Tuesday; the Monday of that ISO week is 08-24.
        assert monthly_expiry_week(date(2026, 8, 4), AUG_LISTED, today=date(2026, 8, 24)) is True

    def test_false_a_week_earlier(self):
        assert monthly_expiry_week(date(2026, 8, 4), AUG_LISTED, today=date(2026, 8, 17)) is False

    def test_false_without_a_listing(self):
        assert monthly_expiry_week(date(2026, 8, 4), [], today=date(2026, 8, 24)) is False


class TestMoneyness:
    def test_call_is_itm_below_spot_and_put_above(self):
        # NIFTY at 24,000 on a 50-wide ladder.
        assert moneyness_steps(spot=24000, strike=23900, option_type="CE", step=50) == 2.0
        assert moneyness_steps(spot=24000, strike=24100, option_type="CE", step=50) == -2.0
        assert moneyness_steps(spot=24000, strike=24100, option_type="PE", step=50) == 2.0
        assert moneyness_steps(spot=24000, strike=23900, option_type="PE", step=50) == -2.0

    def test_half_rung_ladder_is_handled(self):
        # ITC-style 2.5-wide ladder, the strike class that caused the lossy
        # strike-token incident.
        assert moneyness_steps(spot=290.0, strike=287.5, option_type="CE", step=2.5) == 1.0

    def test_bands(self):
        assert classify_moneyness(0.0) == MONEYNESS_ATM
        assert classify_moneyness(0.5) == MONEYNESS_ATM
        assert classify_moneyness(-0.5) == MONEYNESS_ATM
        assert classify_moneyness(1.0) == MONEYNESS_NEAR_ITM
        assert classify_moneyness(-1.0) == MONEYNESS_NEAR_OTM
        assert classify_moneyness(-3.0) == MONEYNESS_OTM
        assert classify_moneyness(9.0) == MONEYNESS_DEEP_ITM
        assert classify_moneyness(-9.0) == MONEYNESS_DEEP_OTM

    def test_missing_inputs_give_unknown_not_a_default_band(self):
        assert moneyness_steps(spot=None, strike=24000, option_type="CE", step=50) is None
        assert moneyness_steps(spot=24000, strike=24000, option_type="CE", step=0) is None
        assert moneyness_steps(spot=24000, strike=24000, option_type="FUT", step=50) is None
        assert classify_moneyness(None) == MONEYNESS_UNKNOWN


class TestLiquidity:
    def test_percentile_blends_oi_and_volume_ranks(self):
        rows = [
            {"oi": 10, "volume": 10},
            {"oi": 20, "volume": 20},
            {"oi": 30, "volume": 30},
            {"oi": 40, "volume": 40},
            {"oi": 50, "volume": 50},
        ]
        ranks = chain_liquidity_percentiles(rows)
        assert ranks == [0.0, 0.25, 0.5, 0.75, 1.0]
        assert classify_liquidity(ranks[-1]) == LIQUIDITY_TOP
        assert classify_liquidity(ranks[0]) == LIQUIDITY_LOW

    def test_ties_share_a_rank_regardless_of_chain_order(self):
        rows = [{"oi": 5, "volume": 5}, {"oi": 5, "volume": 5}, {"oi": 9, "volume": 9}]
        ranks = chain_liquidity_percentiles(rows)
        assert ranks[0] == ranks[1]
        assert ranks[2] > ranks[0]

    def test_one_sided_data_still_ranks(self):
        rows = [{"oi": 10, "volume": None}, {"oi": 90, "volume": None}]
        assert chain_liquidity_percentiles(rows) == [0.0, 1.0]

    def test_no_data_is_unknown_never_low(self):
        rows = [{"oi": None, "volume": None}, {"oi": None, "volume": None}]
        assert chain_liquidity_percentiles(rows) == [None, None]
        assert classify_liquidity(None) == LIQUIDITY_UNKNOWN


class TestClassifyContract:
    def test_index_weekly_atm_call(self):
        now = datetime(2026, 8, 3, 11, 0, tzinfo=IST)
        taxonomy = classify_contract(
            exchange="NSE",
            underlying="NIFTY",
            index_symbols=INDICES,
            expiry=date(2026, 8, 4),
            listed_expiries=AUG_LISTED,
            option_type="CE",
            strike=24000,
            spot=24010,
            ladder_step=50,
            liquidity_percentile=0.95,
            now=now,
        )
        assert taxonomy.underlying_class == "INDEX"
        assert taxonomy.expiry_class == EXPIRY_WEEKLY
        assert taxonomy.days_to_expiry == 1
        assert taxonomy.expiry_day_flag is False
        assert taxonomy.moneyness == MONEYNESS_ATM
        assert taxonomy.liquidity_bucket == LIQUIDITY_TOP
        assert taxonomy.expiry_class_reason is None

    def test_stock_monthly_deep_otm_put(self):
        now = datetime(2026, 8, 3, 11, 0, tzinfo=IST)
        taxonomy = classify_contract(
            exchange="NSE",
            underlying="ITC",
            index_symbols=INDICES,
            expiry=date(2026, 8, 25),
            listed_expiries=[date(2026, 8, 25), date(2026, 9, 29)],
            option_type="PE",
            strike=270.0,
            spot=290.0,
            ladder_step=2.5,
            liquidity_percentile=0.1,
            now=now,
        )
        assert taxonomy.underlying_class == "STOCK"
        assert taxonomy.expiry_class == EXPIRY_MONTHLY
        assert taxonomy.moneyness == MONEYNESS_DEEP_OTM
        assert taxonomy.liquidity_bucket == LIQUIDITY_LOW

    def test_degraded_inputs_are_labelled_unknown_and_serialise(self):
        taxonomy = classify_contract(
            exchange="NSE",
            underlying="NIFTY",
            index_symbols=INDICES,
            expiry=date(2026, 8, 4),
            listed_expiries=None,
            option_type="CE",
            strike=24000,
            spot=None,
            ladder_step=None,
            now=datetime(2026, 8, 3, 11, 0, tzinfo=IST),
        )
        assert taxonomy.expiry_class == EXPIRY_UNKNOWN
        assert taxonomy.expiry_class_reason == "no_listed_expiries_for_underlying"
        assert taxonomy.moneyness == MONEYNESS_UNKNOWN
        assert taxonomy.liquidity_bucket == LIQUIDITY_UNKNOWN
        assert taxonomy.as_dict()["expiry"] == "2026-08-04"
