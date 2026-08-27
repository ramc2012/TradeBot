"""Offline tests for M6's pure candidate-filtering/conviction/gating logic.

Mirrors test_m7_risk.py's style: fake psycopg2-shaped connections for the
two functions that run their own SQL (load_candidates_at, resolve_instrument),
and monkeypatched collaborators for build_tickets so its own orchestration
logic (conviction gate, rank gate, instrument-resolution gate, M7 risk gate,
audit trail, per-bar cumulative risk-budget consumption) is tested in
isolation from the DB layer and from M7's own already-tested internals.
"""
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from fusion import m6_select  # noqa: E402
from fusion.m6_select import (  # noqa: E402
    CONVICTION_MIN,
    TOP_N_PER_BAR,
    Candidate,
    build_tickets,
    load_candidates_at,
    resolve_instrument,
)
from fusion.m7_risk import RiskState, SizingResult  # noqa: E402


class _ScriptedCursor:
    """Pops one canned result off a shared queue per execute() call. Each
    entry is whatever fetchall()/fetchone() should return for that call."""
    def __init__(self, queue):
        self._queue = queue
        self._current = None

    def execute(self, *a, **k):
        self._current = self._queue.pop(0)

    def fetchall(self):
        return self._current

    def fetchone(self):
        return self._current

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _ScriptedConnection:
    """cursor() may be called more than once (resolve_instrument's single
    `with` block issues 3 execute()s on one cursor; other functions open a
    fresh `with` block per query) -- share one queue across every cursor()
    call so results are consumed in call order regardless of which cursor
    object issues them."""
    def __init__(self, results):
        self._queue = list(results)

    def cursor(self):
        return _ScriptedCursor(self._queue)


AS_OF = datetime(2026, 8, 26, 10, 30)


def _row(symbol="TCS", timing_score=80.0, timing_state="IGNITION",
         flow_score=75.0, rs_z20=1.5, sector20="IT", regime="NEG",
         best_lag=None, corr=None):
    return (symbol, timing_score, timing_state, flow_score, rs_z20, sector20,
            regime, best_lag, corr)


# ---------------------------------------------------------------------------
# load_candidates_at: post-SQL filtering + conviction math
# ---------------------------------------------------------------------------

def test_bullish_candidate_confirmed_on_every_axis_is_included_with_hand_computed_conviction():
    conn = _ScriptedConnection([[_row()]])
    candidates = load_candidates_at(conn, AS_OF)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.direction == "bullish"
    # flow=75, sector_rs=(1.5+3)/6*100=75, timing=80, regime(NEG)=75, leadlag=50
    # conviction = .35*75 + .20*75 + .20*80 + .15*75 + .10*50 = 73.5
    assert c.conviction == 73.5


def test_bearish_candidate_with_positive_lag_gets_the_leadlag_bonus():
    conn = _ScriptedConnection([[_row(
        symbol="INFY", flow_score=-70.0, rs_z20=-2.0, regime="STRONG_NEG",
        timing_score=90.0, best_lag=3, corr=0.6,
    )]])
    c = load_candidates_at(conn, AS_OF)[0]
    assert c.direction == "bearish"
    # aligned_rs = (-1)*(-2.0) = 2.0 -> sector_rs=(2+3)/6*100=83.3333
    # leadlag = 50 + min(50, 0.6*100) = 100
    # conviction = .35*70 + .20*83.33333 + .20*90 + .15*100 + .10*100
    assert round(c.conviction, 4) == round(0.35 * 70 + 0.20 * (5 / 6 * 100) + 0.20 * 90 + 0.15 * 100 + 0.10 * 100, 4)


def test_nonpositive_lag_gets_only_the_neutral_leadlag_baseline_not_a_bonus():
    conn = _ScriptedConnection([[_row(best_lag=0, corr=0.9), _row(symbol="X", best_lag=-1, corr=0.9)]])
    candidates = load_candidates_at(conn, AS_OF)
    assert all(c.components["leadlag"] == 50.0 for c in candidates)


def test_sector_rs_direction_mismatch_excludes_the_candidate():
    """Bullish flow but rs_z20 negative -- sector RS must confirm direction."""
    conn = _ScriptedConnection([[_row(flow_score=65.0, rs_z20=-1.5)]])
    assert load_candidates_at(conn, AS_OF) == []


def test_sector_rs_below_min_abs_z_excludes_the_candidate():
    conn = _ScriptedConnection([[_row(rs_z20=0.5)]])
    assert load_candidates_at(conn, AS_OF) == []


def test_regime_outside_permits_excludes_the_candidate_even_with_everything_else_confirming():
    conn = _ScriptedConnection([[_row(regime="POS")]])
    assert load_candidates_at(conn, AS_OF) == []
    conn2 = _ScriptedConnection([[_row(regime="STRONG_POS")]])
    assert load_candidates_at(conn2, AS_OF) == []


def test_non_ignition_timing_state_excludes_the_candidate():
    conn = _ScriptedConnection([[_row(timing_state="COILED")]])
    assert load_candidates_at(conn, AS_OF) == []


def test_timing_score_below_threshold_excludes_the_candidate_even_if_ignition():
    conn = _ScriptedConnection([[_row(timing_score=65.0, timing_state="IGNITION")]])
    assert load_candidates_at(conn, AS_OF) == []


def test_results_are_sorted_by_conviction_descending():
    conn = _ScriptedConnection([[
        _row(symbol="LOW", timing_score=70.0),
        _row(symbol="HIGH", timing_score=100.0),
        _row(symbol="MID", timing_score=85.0),
    ]])
    symbols = [c.symbol for c in load_candidates_at(conn, AS_OF)]
    assert symbols == ["HIGH", "MID", "LOW"]


def test_null_flow_score_row_is_skipped_defensively():
    """The SQL WHERE already excludes NULL flow_score, but the Python guard
    must not crash / must not fabricate a direction if one ever slips
    through (e.g. a future query change)."""
    conn = _ScriptedConnection([[_row(flow_score=None)]])
    assert load_candidates_at(conn, AS_OF) == []


# ---------------------------------------------------------------------------
# resolve_instrument
# ---------------------------------------------------------------------------

def test_resolve_instrument_builds_ce_for_bullish_and_computes_entry_zone_fields():
    conn = _ScriptedConnection([
        ("TCS", 3500.0),                              # spot query
        (3500, 45.5, date(2026, 8, 28)),               # chain query: strike, close, expiry
        (150,),                                        # lot_size query
    ])
    result = resolve_instrument(conn, "TCS", "bullish", AS_OF)
    assert result["instrument"] == "TCS26AUG3500CE"
    assert result["premium"] == 45.5
    assert result["lot_size"] == 150


def test_resolve_instrument_builds_pe_for_bearish():
    conn = _ScriptedConnection([
        ("TCS", 3500.0),
        (3500, 40.0, date(2026, 8, 28)),
        (150,),
    ])
    result = resolve_instrument(conn, "TCS", "bearish", AS_OF)
    assert result["instrument"].endswith("PE")


def test_resolve_instrument_returns_none_when_no_spot_row():
    conn = _ScriptedConnection([None])
    assert resolve_instrument(conn, "TCS", "bullish", AS_OF) is None


def test_resolve_instrument_returns_none_when_no_chain_row():
    conn = _ScriptedConnection([("TCS", 3500.0), None])
    assert resolve_instrument(conn, "TCS", "bullish", AS_OF) is None


def test_resolve_instrument_returns_none_when_no_lot_size():
    conn = _ScriptedConnection([
        ("TCS", 3500.0), (3500, 45.5, date(2026, 8, 28)), None,
    ])
    assert resolve_instrument(conn, "TCS", "bullish", AS_OF) is None


def test_resolve_instrument_rejects_premium_below_min():
    conn = _ScriptedConnection([
        ("TCS", 3500.0), (3500, 2.0, date(2026, 8, 28)), (150,),
    ])
    assert resolve_instrument(conn, "TCS", "bullish", AS_OF) is None


# ---------------------------------------------------------------------------
# build_tickets: gating order, audit trail, per-bar risk-budget consumption
# ---------------------------------------------------------------------------

def _candidate(symbol="A", conviction=90.0, direction="bullish", sector20="IT"):
    return Candidate(
        symbol=symbol, ts=AS_OF, direction=direction, flow_score=70.0, rs_z20=1.5,
        sector20=sector20, regime="NEG", timing_score=80.0, timing_state="IGNITION",
        best_lag=None, corr=None, conviction=conviction,
        components={"flow": 70.0, "sector_rs": 75.0, "timing": 80.0, "regime": 75.0, "leadlag": 50.0},
    )


def _risk_state():
    return RiskState(capital=1_000_000.0, daily_pnl_pct=0.0, weekly_pnl_pct=0.0,
                     open_positions=[], kelly_edge=None)


def test_candidate_below_conviction_min_is_gated_before_touching_instrument_or_risk(monkeypatch):
    monkeypatch.setattr(m6_select, "load_candidates_at", lambda c, ts: [_candidate(conviction=CONVICTION_MIN - 0.1)])
    monkeypatch.setattr(m6_select, "load_risk_state", lambda c, ts, cap: _risk_state())
    monkeypatch.setattr(m6_select, "resolve_instrument", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")))
    rows = build_tickets(object(), AS_OF, 1_000_000.0)
    assert len(rows) == 1
    assert rows[0]["emitted"] is False
    assert "conviction" in rows[0]["gated_reason"]


def test_candidate_past_top_n_rank_is_gated_by_rank(monkeypatch):
    candidates = [_candidate(symbol=f"S{i}", conviction=99.0 - i) for i in range(TOP_N_PER_BAR + 1)]
    monkeypatch.setattr(m6_select, "load_candidates_at", lambda c, ts: candidates)
    monkeypatch.setattr(m6_select, "load_risk_state", lambda c, ts, cap: _risk_state())
    monkeypatch.setattr(m6_select, "resolve_instrument", lambda *a, **k: {
        "instrument": "X", "premium": 50.0, "strike": 100, "expiry": date(2026, 8, 28), "option_type": "CE", "lot_size": 150,
    })
    monkeypatch.setattr(m6_select, "risk_check", lambda *a, **k: SizingResult(allowed=True, lots=1, notional=7500, risk_rupees=7500, method="fixed_fractional"))
    rows = build_tickets(object(), AS_OF, 1_000_000.0)
    last = rows[-1]
    assert last["rank_in_session"] == TOP_N_PER_BAR + 1
    assert last["emitted"] is False
    assert "rank" in last["gated_reason"]


def test_unresolvable_instrument_is_gated_with_its_own_reason(monkeypatch):
    monkeypatch.setattr(m6_select, "load_candidates_at", lambda c, ts: [_candidate()])
    monkeypatch.setattr(m6_select, "load_risk_state", lambda c, ts, cap: _risk_state())
    monkeypatch.setattr(m6_select, "resolve_instrument", lambda *a, **k: None)
    rows = build_tickets(object(), AS_OF, 1_000_000.0)
    assert rows[0]["emitted"] is False
    assert rows[0]["gated_reason"] == "no tradable ATM contract resolved"


def test_m7_risk_veto_is_gated_with_an_m7_prefixed_reason(monkeypatch):
    monkeypatch.setattr(m6_select, "load_candidates_at", lambda c, ts: [_candidate()])
    monkeypatch.setattr(m6_select, "load_risk_state", lambda c, ts, cap: _risk_state())
    monkeypatch.setattr(m6_select, "resolve_instrument", lambda *a, **k: {
        "instrument": "X", "premium": 50.0, "strike": 100, "expiry": date(2026, 8, 28), "option_type": "CE", "lot_size": 150,
    })
    monkeypatch.setattr(m6_select, "risk_check", lambda *a, **k: SizingResult(allowed=False, reason="STAND-DOWN"))
    rows = build_tickets(object(), AS_OF, 1_000_000.0)
    assert rows[0]["emitted"] is False
    assert rows[0]["gated_reason"] == "M7: STAND-DOWN"


def test_a_candidate_clearing_every_gate_is_emitted_with_sizing_fields_populated(monkeypatch):
    monkeypatch.setattr(m6_select, "load_candidates_at", lambda c, ts: [_candidate()])
    monkeypatch.setattr(m6_select, "load_risk_state", lambda c, ts, cap: _risk_state())
    monkeypatch.setattr(m6_select, "resolve_instrument", lambda *a, **k: {
        "instrument": "TCS26AUG3500CE", "premium": 50.0, "strike": 3500, "expiry": date(2026, 8, 28), "option_type": "CE", "lot_size": 150,
    })
    monkeypatch.setattr(m6_select, "risk_check", lambda *a, **k: SizingResult(
        allowed=True, lots=2, notional=15000.0, risk_rupees=15000.0, method="kelly_0.25x",
    ))
    rows = build_tickets(object(), AS_OF, 1_000_000.0)
    row = rows[0]
    assert row["emitted"] is True
    assert row["gated_reason"] is None
    assert row["sizing_lots"] == 2
    assert row["sizing_method"] == "kelly_0.25x"
    assert row["stop"] == round(50.0 * 0.85, 4)
    assert row["target1"] == round(50.0 * 1.20, 4)
    assert row["target2"] == round(50.0 * 1.50, 4)


def test_every_filtered_candidate_appears_in_the_audit_trail_not_just_winners(monkeypatch):
    """doctrine #5 ('everything measurable') -- gated-out near-misses must
    still be recorded, not silently dropped."""
    monkeypatch.setattr(m6_select, "load_candidates_at", lambda c, ts: [
        _candidate(symbol="WINNER", conviction=95.0),
        _candidate(symbol="LOSER", conviction=50.0),
    ])
    monkeypatch.setattr(m6_select, "load_risk_state", lambda c, ts, cap: _risk_state())
    monkeypatch.setattr(m6_select, "resolve_instrument", lambda *a, **k: {
        "instrument": "X", "premium": 50.0, "strike": 100, "expiry": date(2026, 8, 28), "option_type": "CE", "lot_size": 150,
    })
    monkeypatch.setattr(m6_select, "risk_check", lambda *a, **k: SizingResult(allowed=True, lots=1, notional=7500, risk_rupees=7500, method="fixed_fractional"))
    rows = build_tickets(object(), AS_OF, 1_000_000.0)
    assert {r["symbol"] for r in rows} == {"WINNER", "LOSER"}
    assert [r["emitted"] for r in rows] == [True, False]


def test_an_emitted_ticket_consumes_risk_budget_for_the_rest_of_the_bar(monkeypatch):
    """The second candidate's risk_check must see the first candidate's
    position already reflected in open_positions -- otherwise two tickets
    at the same bar could double-count the same headroom."""
    monkeypatch.setattr(m6_select, "load_candidates_at", lambda c, ts: [
        _candidate(symbol="FIRST", conviction=95.0),
        _candidate(symbol="SECOND", conviction=90.0),
    ])
    monkeypatch.setattr(m6_select, "load_risk_state", lambda c, ts, cap: _risk_state())
    monkeypatch.setattr(m6_select, "resolve_instrument", lambda *a, **k: {
        "instrument": "X", "premium": 50.0, "strike": 100, "expiry": date(2026, 8, 28), "option_type": "CE", "lot_size": 150,
    })

    seen_open_position_counts = []

    def fake_risk_check(state, connection, **kwargs):
        seen_open_position_counts.append(len(state.open_positions))
        return SizingResult(allowed=True, lots=1, notional=7500, risk_rupees=7500, method="fixed_fractional")

    monkeypatch.setattr(m6_select, "risk_check", fake_risk_check)
    build_tickets(object(), AS_OF, 1_000_000.0)
    assert seen_open_position_counts == [0, 1]
