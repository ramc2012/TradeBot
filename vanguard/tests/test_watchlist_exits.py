from datetime import datetime, timedelta, timezone
import pytest

from model.session_clock import available_at, completed, timely_decision
from model.watchlist_exits import HARD_STOP_POLICY, analyse_path, policy_card

START = datetime(2026, 8, 31, 3, 45, tzinfo=timezone.utc)
EOD = START.replace(hour=10, minute=0)


def bar(n, open_=100, high=110, low=95, close=100):
    return dict(time=START + timedelta(minutes=30*n), open=open_, high=high, low=low, close=close)


def test_open_stamped_bar_not_usable_until_completed():
    assert not completed(START, START + timedelta(minutes=29))
    assert completed(START, START + timedelta(minutes=30))
    assert available_at(START+timedelta(hours=6)) == EOD
    with pytest.raises(ValueError):
        available_at(START.replace(tzinfo=None))


def test_retrospective_prediction_is_not_classified_as_prospective():
    assert not timely_decision(START,START+timedelta(minutes=29))
    assert timely_decision(START,START+timedelta(minutes=35))
    assert not timely_decision(START,START+timedelta(minutes=60))


def test_future_partial_bar_is_excluded():
    result = analyse_path([bar(0), bar(1, high=180)], START+timedelta(minutes=40))
    assert result["bars"] == 1
    assert result["max_return_pct"] == 0
    assert result["runner"]["status"] == "tracking"


def test_entry_candle_extremes_cannot_be_profit_or_stop():
    result = analyse_path([bar(0, high=200, low=30), bar(1, high=105)], EOD)
    assert result["max_return_pct"] == pytest.approx(.05)
    assert result["min_return_pct"] == pytest.approx(-.05)
    assert result["runner"]["status"] == "tracking"


def test_initial_stop_precedes_any_same_bar_activation():
    result = analyse_path([bar(0), bar(1, high=160, low=80)], EOD)
    assert result["runner"]["reason"] == "initial_stop"
    assert result["runner"]["exit_mark"] == 85
    assert result["runner"]["net_return_pct"] == pytest.approx(-.16)


def test_gap_through_stop_fills_worse_open():
    result = analyse_path([bar(0), bar(1, open_=70, high=80, low=60, close=75)], EOD)
    assert result["runner"]["exit_mark"] == 70


def test_ratchet_only_applies_next_candle_not_the_earlier_low():
    path = [bar(0), bar(1, high=140, low=90, close=135)]
    first = analyse_path(path, EOD)
    assert first["runner"]["status"] == "tracking"
    assert first["runner"]["stop_for_next_bar"] == 120
    next_ = analyse_path(path+[bar(2, open_=130, high=135, low=115, close=125)], EOD)
    assert next_["runner"]["exit_mark"] == 120
    assert next_["runner"]["net_return_pct"] == pytest.approx(.19)


def test_break_even_protects_assumed_cost_not_guaranteed_fill():
    path = [bar(0), bar(1, high=120, close=115), bar(2, open_=110, high=115, low=100, close=105)]
    result = analyse_path(path, EOD)
    assert result["runner"]["exit_mark"] == 101
    assert result["runner"]["net_return_pct"] == pytest.approx(0)


def test_no_fixed_profit_cap_and_own_final_close_required():
    path = [bar(0), bar(1, high=193, close=185)]
    path += [bar(i, open_=180, high=185, low=170, close=180) for i in range(2,11)]
    partial = analyse_path(path, EOD)
    assert partial["runner"]["status"] == "tracking"
    result = analyse_path(path+[bar(11, open_=180, high=185, low=170, close=180)], EOD)
    assert result["max_return_pct"] == pytest.approx(.93)
    assert result["runner"]["reason"] == "session_close"
    assert result["runner"]["net_return_pct"] == pytest.approx(.79)


def test_short_final_candle_after_scheduled_exit_never_changes_results():
    path = [bar(0)] + [bar(i) for i in range(1,12)]
    assert analyse_path(path,EOD) == analyse_path(path+[bar(12,high=900)],EOD)


def test_missing_candle_before_exit_invalidates_not_invents_fill():
    result = analyse_path([bar(0), bar(2, low=60)], EOD)
    assert result["runner"]["status"] == "insufficient_data"
    assert result["runner"].get("net_return_pct") is None


def test_future_path_cannot_change_an_already_resolved_exit():
    path = [bar(0), bar(1, low=80)]
    before = analyse_path(path, EOD)["runner"]
    after = analyse_path(path+[bar(5, high=1000)], EOD)["runner"]
    assert before == after


def test_missing_opening_bar_never_shifts_entry_to_favourable_later_mark():
    result = analyse_path([bar(2), bar(3, high=160)], EOD)
    assert not result["entry_on_time"]
    assert result["runner"]["status"] == "insufficient_data"


@pytest.mark.parametrize("path,status", [([],"missing_contract"),
    ([bar(0),bar(0)],"duplicate_candles"), ([bar(0,close=0)],"invalid_candle"),
    ([bar(0,low=110)],"invalid_candle")])
def test_bad_data_is_explicit(path,status):
    assert analyse_path(path,EOD)["status"] == status


def test_policy_is_versioned_shadow_and_has_no_broker_or_profit_cap():
    card = policy_card()
    assert card["mode"] == "shadow_only"
    assert card["profit_cap"] is None
    assert card["cost_pct"] == .01


def test_stop_only_control_does_not_ratchet_with_the_runner():
    path = [bar(0),bar(1,high=140,close=135),bar(2,open_=130,high=135,low=115,close=125)]
    assert analyse_path(path,EOD)["runner"]["status"] == "exited"
    assert analyse_path(path,EOD,policy=HARD_STOP_POLICY)["runner"]["status"] == "tracking"
