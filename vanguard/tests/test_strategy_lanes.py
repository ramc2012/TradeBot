"""Contract guards for the three-lane journal.

These assert the SQL itself because the SQL *is* the logic here, and both
defects they cover were invisible to any test that mocked the database away.
"""
import inspect
import re

from journal import strategy_lanes


def _sql(function) -> str:
    return inspect.getsource(function)


def test_mp_returns_are_converted_from_percent_to_fraction():
    """mp_paper_trades stores PERCENT; the journal column is a FRACTION.

    Mirroring net_ret_pct raw made the desk render a 388.80 -> 397.05 overnight
    trade as +207.19% instead of +2.07% -- every MP-lane return inflated 100x.
    """
    assert "net_ret_pct/100.0" in _sql(strategy_lanes.sync_mp_journals)


def test_running_marks_and_swing_returns_are_fractions_too():
    """One column, one unit. Both writers must agree with the /100 above."""
    marks = _sql(strategy_lanes.mark_open_mp_journals)
    assert "(m.close/j.entry_mark-1.0)" in marks
    # cost_bp is basis points on a fraction, so 10000 -- not mp_edges' 100,
    # which applies to its percent-unit numbers.
    assert "/10000.0" in marks
    assert "summarize_path" in _sql(strategy_lanes.track_swing)


def test_a_running_mark_is_never_presented_as_a_settled_exit():
    assert "running_30m_spot_close" in _sql(strategy_lanes.mark_open_mp_journals)


def test_the_authoritative_mp_book_is_never_written_by_the_journal():
    """mp_paper_trades settles on its own schedule (next open / 4th close).

    The journal is a mirror + running mark. A journal that writes back into the
    book it mirrors is the zombie-journal failure this lane already survived.
    """
    for function in (strategy_lanes.sync_mp_journals,
                     strategy_lanes.mark_open_mp_journals,
                     strategy_lanes.track_swing):
        source = _sql(function)
        assert not re.search(r"(UPDATE|INSERT INTO|DELETE FROM)\s+mp_paper_trades", source)


def test_every_hypertable_read_bounds_time_directly():
    """underlying_spot_candles carries >1300 one-day chunks.

    Two forms defeat plan-time chunk exclusion and cost SECONDS of planner time
    per call on a once-a-minute loop: wrapping `time` in a function, and the
    STABLE `now() - interval` bound. Measured 3.1s and 6.8s of planning
    respectively, against ~0.02s of real work.
    """
    for function in (strategy_lanes._future_sessions,
                     strategy_lanes.mark_open_mp_journals,
                     strategy_lanes.track_swing):
        source = _sql(function)
        where = source[source.find("WHERE"):] if "WHERE" in source else source
        assert "now()-interval" not in where.replace(" ", "")
        assert not re.search(
            r"\(\s*\w*\.?time AT TIME ZONE '[^']+'\s*\)::date\s*[<>]", where), (
            f"{function.__name__} filters on a function of `time`")
