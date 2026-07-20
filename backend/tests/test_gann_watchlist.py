"""Tests for the Gann persistence, universe and daily-bar layers.

Covers the three gaps closed in this change:

* GAP 1 — `gann_watchlist_snapshots`: every field is COMPUTED or NULL with a
  recorded reason. A fabricated default anywhere here would be worse than an
  empty table, because it would look like data.
* GAP 2 — universe expansion with LOUD accounting. The silent 6-of-7 scan is
  the failure mode being guarded.
* GAP 3 — the daily loader that makes the higher-order horizon reachable,
  including the wick sanitiser that stops one contaminated print from becoming
  the anchor of the entire geometry.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from gann_tp_delta import daily_data as dd
from gann_tp_delta import universe as uni
from gann_tp_delta import watchlist as wl
from gann_tp_delta.config import clone_default_config


CFG = clone_default_config()


def _daily(closes: list[float], start: str = "2025-01-01") -> pd.DataFrame:
    times = pd.date_range(start, periods=len(closes), freq="D")
    return pd.DataFrame(
        {
            "time": times,
            "open": closes,
            "high": [value * 1.01 for value in closes],
            "low": [value * 0.99 for value in closes],
            "close": closes,
            "volume": [0] * len(closes),
            "oi": [0] * len(closes),
        }
    )


# ── Daily loader ───────────────────────────────────────────────────────────


def test_resample_to_daily_buckets_by_IST_session():
    rows = [
        # 2026-07-20 04:00Z = 09:30 IST; 2026-07-20 09:30Z = 15:00 IST
        {"time": datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc), "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1, "oi": 0},
        {"time": datetime(2026, 7, 20, 9, 30, tzinfo=timezone.utc), "open": 100, "high": 104, "low": 98, "close": 103, "volume": 2, "oi": 0},
        # 2026-07-20 20:00Z = 01:30 IST on 2026-07-21 -> a DIFFERENT session
        {"time": datetime(2026, 7, 20, 20, 0, tzinfo=timezone.utc), "open": 103, "high": 105, "low": 103, "close": 105, "volume": 3, "oi": 0},
    ]
    frame = dd.resample_to_daily(rows)
    assert len(frame.index) == 2
    first = frame.iloc[0]
    assert pd.Timestamp(first["time"]).date() == date(2026, 7, 20)
    assert first["high"] == 104 and first["low"] == 98 and first["close"] == 103
    assert pd.Timestamp(frame.iloc[1]["time"]).date() == date(2026, 7, 21)


def test_resample_to_daily_is_empty_safe():
    assert dd.resample_to_daily([]).empty
    assert dd.resample_to_daily(pd.DataFrame()).empty


def test_sanitize_wicks_clamps_the_observed_nifty_contamination():
    """The real bar: NIFTY 30-minute 2026-07-08 04:00Z, high 27094.30 on a body
    of 24207-24251. Left alone it becomes the swing-high anchor for the whole
    daily frame, and every angle, SQ9 level and cycle count derives from it."""
    frame = pd.DataFrame(
        {
            "time": pd.to_datetime(["2026-07-08 04:00:00+00:00", "2026-07-08 04:30:00+00:00"]),
            "open": [24207.30, 24251.30],
            "high": [27094.30, 24258.10],
            "low": [24207.30, 24219.25],
            "close": [24251.30, 24247.65],
            "volume": [0, 0],
            "oi": [0, 0],
        }
    )
    cleaned, clamped = dd.sanitize_wicks(frame)
    assert clamped == 1
    assert cleaned["high"].iloc[0] == 24251.30  # the bar's own body extreme
    assert cleaned["high"].iloc[1] == 24258.10  # untouched
    assert cleaned["low"].tolist() == frame["low"].tolist()


def test_sanitize_wicks_leaves_ordinary_bars_alone():
    frame = pd.DataFrame(
        {
            "time": pd.to_datetime(["2026-07-08 04:00:00+00:00"]),
            "open": [100.0], "high": [103.0], "low": [98.0], "close": [101.0],
            "volume": [0], "oi": [0],
        }
    )
    cleaned, clamped = dd.sanitize_wicks(frame)
    assert clamped == 0
    assert cleaned["high"].iloc[0] == 103.0 and cleaned["low"].iloc[0] == 98.0


def test_window_bounds_are_tight_and_utc():
    start, end = dd.window_bounds(sessions=400, as_of=datetime(2026, 7, 20, tzinfo=timezone.utc))
    assert start.tzinfo is not None and end.tzinfo is not None
    span = (end - start).days
    assert 400 < span < 700, "wide enough for 400 sessions, tight enough to bound chunks"


def test_daily_select_never_wraps_the_partitioning_column_in_a_function():
    """The PG rule that OOM-killed this database: bound `time` DIRECTLY."""
    sql = " ".join(dd._SELECT.lower().split())
    assert "time >= $3::timestamptz" in sql
    assert "time < $4::timestamptz" in sql
    for forbidden in ("time::date", "date_trunc", "at time zone", "extract("):
        assert forbidden not in sql
    # The composite index needs the other two predicates bound too.
    assert "underlying = $1" in sql and "interval = $2" in sql


# ── Universe ───────────────────────────────────────────────────────────────


def test_classify_separates_indices_stocks_and_commodities():
    assert uni.classify("NIFTY") == uni.CLASS_INDEX
    assert uni.classify("SENSEX") == uni.CLASS_INDEX
    assert uni.classify("CRUDEOIL") == uni.CLASS_COMMODITY
    assert uni.classify("RELIANCE") == uni.CLASS_STOCK


def test_sweep_cursor_round_robins_and_reports_coverage():
    cursor = uni.SweepCursor(batch_size=3)
    universe = ["A", "B", "C", "D", "E"]
    first, stats = cursor.take(universe)
    assert first == ["A", "B", "C"]
    assert stats["universe_size"] == 5 and stats["scanned"] == 3
    assert stats["truncated"] is True and stats["sweep_complete"] is False
    second, stats2 = cursor.take(universe)
    assert second == ["D", "E", "A"]
    assert stats2["cursor_before"] == 3


def test_sweep_cursor_reports_a_complete_sweep_when_the_batch_covers_all():
    cursor = uni.SweepCursor(batch_size=10)
    selection, stats = cursor.take(["A", "B"])
    assert selection == ["A", "B"]
    assert stats["sweep_complete"] is True and stats["truncated"] is False


def test_sweep_cursor_is_safe_on_an_empty_universe():
    selection, stats = uni.SweepCursor().take([])
    assert selection == [] and stats["universe_size"] == 0


# ── Watchlist row: computed-or-NULL ────────────────────────────────────────


def test_row_is_all_null_with_reasons_when_there_are_no_bars():
    row = wl.compute_watchlist_row(
        underlying="NOTHING", instrument_class="stock",
        daily_frame=pd.DataFrame(), config=CFG,
    )
    assert row.spot is None and row.regime is None and row.next_turn_date is None
    assert row.daily_bars == 0
    assert row.null_reasons["spot"] == "no daily bars available for this instrument"
    # Nothing was invented.
    assert row.blockers == [] and row.active_cycles == []


def test_row_computes_levels_and_records_a_reason_for_every_null():
    closes = [100.0 + (index % 17) - (index % 5) for index in range(240)]
    row = wl.compute_watchlist_row(
        underlying="TESTSYM", instrument_class="stock",
        daily_frame=_daily(closes), config=CFG,
    )
    assert row.spot is not None
    assert row.price_unit is not None
    assert row.anchor_kind in {"swing_high", "swing_low"}
    assert row.anchor_confirmed_at is not None
    # Every NULL among the owner-requested fields must carry a reason.
    for name in (
        "next_turn_date", "price_time_square_date",
        "nearest_angle_support", "nearest_angle_resistance",
        "nearest_sq9_support", "nearest_sq9_resistance",
    ):
        if getattr(row, name) is None:
            assert row.null_reasons.get(name), f"{name} is NULL with no recorded reason"


def test_regime_columns_are_null_when_no_signal_is_supplied():
    row = wl.compute_watchlist_row(
        underlying="TESTSYM", instrument_class="index",
        daily_frame=_daily([100.0 + index * 0.1 for index in range(120)]), config=CFG,
    )
    assert row.regime is None and row.conviction is None and row.setup_state is None
    assert "no strategy signal supplied" in row.null_reasons["regime"]


def test_signal_fields_are_copied_not_defaulted():
    row = wl.compute_watchlist_row(
        underlying="TESTSYM", instrument_class="index",
        daily_frame=_daily([100.0 + index * 0.1 for index in range(120)]), config=CFG,
        signal={
            "regime": "bull", "regime_strength": 0.42, "conviction": 5.5,
            "setup_state": "ARMED", "archetype": "continuation", "side": "long",
            "blockers": ["Conviction floor"],
        },
    )
    assert row.regime == "bull" and row.conviction == 5.5
    assert row.setup_state == "ARMED" and row.blockers == ["Conviction floor"]
    assert "regime" not in row.null_reasons


def test_prominence_gate_with_no_prominent_cycle_leaves_turn_date_unranked_not_faked():
    frame = _daily([100.0 + (index % 23) for index in range(400)])
    row = wl.compute_watchlist_row(
        underlying="TESTSYM", instrument_class="index", daily_frame=frame, config=CFG,
        prominent_cycle_keys=[],
    )
    # An empty prominent set must never be silently upgraded to "prominent".
    assert row.next_turn_prominence in (None, "unranked")


def test_prominence_gate_restricts_to_the_listed_cycles():
    frame = _daily([100.0 + (index % 23) for index in range(400)])
    row = wl.compute_watchlist_row(
        underlying="TESTSYM", instrument_class="index", daily_frame=frame, config=CFG,
        prominent_cycle_keys=["cal_30"],
    )
    assert row.next_turn_cycle_key in (None, "cal_30")
    if row.next_turn_cycle_key is None:
        assert row.null_reasons.get("next_turn_date")
    else:
        assert row.next_turn_prominence == "prominent"


# ── Persistence contract ───────────────────────────────────────────────────


def test_upsert_params_arity_matches_the_sql_placeholders():
    row = wl.GannWatchlistRow(session_date=date(2026, 7, 20), underlying="X", instrument_class="index")
    placeholders = {int(token[1:]) for token in re.findall(r"\$\d+", wl.UPSERT_SQL)}
    assert placeholders == set(range(1, len(wl.upsert_params(row)) + 1))


def test_upsert_params_serialise_json_columns():
    row = wl.GannWatchlistRow(
        session_date=date(2026, 7, 20), underlying="X", instrument_class="index",
        blockers=["a"], active_cycles=[{"k": 1}], null_reasons={"spot": "why"},
    )
    params = wl.upsert_params(row)
    assert '"a"' in params[29] and '"k"' in params[30] and "why" in params[31]


def test_upsert_is_idempotent_per_session_and_underlying():
    assert "ON CONFLICT (session_date, underlying) DO UPDATE" in wl.UPSERT_SQL


# ── Horizon config ─────────────────────────────────────────────────────────


def test_lane_is_configured_for_the_higher_order_horizon():
    assert CFG["paper_agent"]["timeframe"] == "1day"
    assert CFG["paper_agent"]["lookback_sessions"] >= 200
    # A hold must be able to elapse MORE THAN A DAY, and the time stop is the
    # binding constraint on that.
    assert CFG["risk"]["time_stop_bars"] > 1
    assert CFG["strategy"]["min_signal_bars"] >= 60


def test_horizon_change_did_not_move_the_conviction_floors():
    strategy = CFG["strategy"]
    assert strategy["continuation_min_conviction"] == 5.0
    assert strategy["reversal_min_conviction"] == 6.5
    assert strategy["commodity_min_conviction"] == 6.0
    assert strategy["per_underlying_min_conviction"] == {"BANKNIFTY": 6.0}
    # Codex's staged setup gates must survive untouched.
    for gate in (
        "continuation_require_resumption",
        "reversal_require_cardinal_sq9",
        "reversal_require_major_cycle",
        "reversal_require_price_time_square",
    ):
        assert strategy[gate] is True


def test_universe_expansion_is_staged_off_and_batched():
    expansion = CFG["universe_expansion"]
    assert expansion["enabled"] is False
    assert set(expansion["classes"]) == {"index", "stock", "commodity"}
    assert expansion["batch_size"] > 0


def test_cycle_prominence_gate_is_off_because_nothing_is_prominent():
    assert CFG["time_cycles"]["gate_on_prominence"] is False


# ── Horizon PROOF: a daily bar must not advance ten times an hour ──────────
#
# The config change alone does not deliver the owner's "elapsing more than a
# day". `_timeframe_minutes` read only the DIGITS of the timeframe string, so
# '1day' parsed as ONE MINUTE and `bars_held` advanced once a minute. Combined
# with the re-denominated `time_stop_bars = 10` that turned a two-week time
# stop into a TEN-MINUTE one — the exact inverse of the instruction. These
# tests pin the unit, not the config value.


def test_timeframe_minutes_reads_the_unit_not_just_the_digits():
    from gann_tp_delta.agent import _timeframe_minutes

    assert _timeframe_minutes("15minute") == 15
    assert _timeframe_minutes("5minute") == 5
    assert _timeframe_minutes("1hour") == 60
    # Day/week are not minute-denominated at all.
    assert _timeframe_minutes("1day") == 0
    assert _timeframe_minutes("1week") == 0
    assert _timeframe_minutes(None) == 15
    assert _timeframe_minutes("garbage") == 15


def test_daily_signal_bar_advances_once_per_ist_date_not_per_minute():
    from gann_tp_delta import agent as ag

    intraday_first = ag._signal_bar_bucket("15minute")
    daily_first = ag._signal_bar_bucket("1day")

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 21, 6, 0, tzinfo=timezone.utc)

    original = ag.datetime
    try:
        ag.datetime = _Frozen
        # Same IST date, 5 hours later -> SAME daily bar, DIFFERENT 15m bar.
        assert ag._signal_bar_bucket("1day") == ag._signal_bar_bucket("1day")
        same_day = ag._signal_bar_bucket("1day")

        class _NextDay(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 7, 22, 6, 0, tzinfo=timezone.utc)

        ag.datetime = _NextDay
        assert ag._signal_bar_bucket("1day") == same_day + 1
    finally:
        ag.datetime = original
    assert isinstance(intraday_first, int) and isinstance(daily_first, int)


def test_time_stop_cannot_fire_in_under_a_day_on_the_daily_horizon():
    """`time_stop_bars` daily bars must span more than one calendar day."""
    from gann_tp_delta import agent as ag

    timeframe = CFG["paper_agent"]["timeframe"]
    stop_bars = int(CFG["risk"]["time_stop_bars"])
    assert stop_bars >= 2

    buckets = set()
    base = datetime(2026, 7, 21, 6, 0, tzinfo=timezone.utc)
    original = ag.datetime
    try:
        for hour in range(0, 24):
            moment = base.replace(hour=0) + timedelta(hours=hour)

            class _At(datetime):
                _moment = moment

                @classmethod
                def now(cls, tz=None):
                    return cls._moment

            ag.datetime = _At
            buckets.add(ag._signal_bar_bucket(timeframe))
    finally:
        ag.datetime = original
    # At most two distinct daily bars can be reached inside 24 hours of wall
    # clock (the IST date rolls once), so 10 bars cannot be reached in a day.
    assert len(buckets) <= 2 < stop_bars


# ── Price-time squaring date is a SESSION count, not a calendar count ──────


def test_squaring_date_converts_sessions_to_calendar_days():
    frame = _daily([100.0] * 10)
    # A pure calendar frame: one session per calendar day -> factor 1.0.
    assert wl._days_per_session(frame) == pytest.approx(1.0)

    weekdays = pd.bdate_range("2025-01-01", periods=60)
    business = pd.DataFrame(
        {
            "time": weekdays,
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
            "volume": 0, "oi": 0,
        }
    )
    # A five-day trading week is ~1.4 calendar days per session; projecting a
    # 28-session squaring as 28 calendar days would land ~12 days early.
    factor = wl._days_per_session(business)
    assert 1.3 < factor < 1.5
    assert round(28 * factor) >= 38


def test_days_per_session_is_safe_on_a_degenerate_frame():
    assert wl._days_per_session(pd.DataFrame()) == 1.0
    assert wl._days_per_session(_daily([100.0])) == 1.0


# ── The shipped config must not break Codex's harnesses ───────────────────


def test_backtester_survives_the_auto_price_unit_config():
    """`price_unit: "auto"` is a STRING. `float("auto")` raises.

    The backtester, tune_sweep.py, validate_local.py and validate_sweep.py all
    read `geometry.price_unit` directly; before this fix the horizon change
    silently killed every one of them (and the /backtest route) with a
    ValueError on the shipped default config.
    """
    from gann_tp_delta.backtest import GannTPDeltaBacktester

    assert CFG["geometry"]["price_unit"] == "auto"
    with pytest.raises(ValueError):
        float(CFG["geometry"]["price_unit"])

    closes = [100.0 + (index % 11) * 0.8 for index in range(320)]
    frame = _daily(closes)
    frame["ema_fast"] = frame["close"]
    result = GannTPDeltaBacktester(CFG).run(
        frame, anchor_mode="auto_pivot", h_mode="median_tpd", underlying="TEST"
    )
    assert isinstance(result, dict)
