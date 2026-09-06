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
    FLOW_MAX_AGE_SESSIONS,
    FLOW_MIN_INGREDIENTS,
    LEG_ORDER,
    REGIME_MAX_AGE_BARS,
    RS_MAX_AGE_SESSIONS,
    TOP_N_PER_BAR,
    Candidate,
    Evaluation,
    _age_in_sessions,
    build_tickets,
    config_hash,
    evaluate_symbol,
    funnel_counts,
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


def _inputs(flow_score=75.0, flow_age=1, ingredients=3, rs_z20=1.5, rs_age=1,
            regime="NEG", regime_age=0, timing_state="IGNITION", timing_score=80.0,
            best_lag=None, corr=None, va_position=0.8, rvol=2.5, gex_percentile=0.1,
            ce_state='long_buildup', pe_state='long_buildup'):
    """One symbol's joined inputs at one bar, all legs passing by default.

    Every test below changes exactly one of these, so a failure names the leg
    it broke rather than a whole scenario.
    """
    return {
        "flow_score": flow_score, "flow_ts": datetime(2026, 8, 25, 15, 15),
        "flow_age_sessions": flow_age, "flow_n_ingredients": ingredients,
        "rs_z20": rs_z20, "rs_ts": datetime(2026, 8, 25, 15, 15), "rs_age_sessions": rs_age,
        "regime": regime, "gex_percentile": gex_percentile,
        "regime_ts": AS_OF, "regime_age_bars": regime_age,
        "timing_state": timing_state, "timing_score": timing_score,
        "rvol": rvol, "va_position": va_position,
        "ce_state": ce_state, "pe_state": pe_state,
        "best_lag": best_lag, "leadlag_corr": corr,
    }


def _evaluate(**kwargs):
    return evaluate_symbol("TCS", AS_OF, "IT", _inputs(**kwargs))


# ---------------------------------------------------------------------------
# evaluate_symbol -- the six-leg filter, as a pure function
# ---------------------------------------------------------------------------

def test_a_candidate_confirmed_on_every_axis_survives_with_hand_computed_conviction():
    e = _evaluate()
    assert e.survived is True
    assert e.first_failed_leg is None
    assert all(e.legs[leg] is True for leg in LEG_ORDER)
    assert e.direction == "bullish"
    # flow=(75+100)/2=87.5, side_momentum(long_buildup)=100,
    # sector_rs=(1.5+3)/6*100=75, timing(IGNITION, break agrees)=80, regime(NEG)=75.
    # leadlag is ABSENT (no best_lag) so it is omitted and the rest renormalise
    # over 0.95 -- it is not scored 50 as if it had been measured.
    # (.30*87.5 + .20*100 + .15*75 + .20*80 + .10*75) / .95 = 85.263...
    assert round(e.conviction, 4) == round(
        (0.30 * 87.5 + 0.20 * 100 + 0.15 * 75 + 0.20 * 80 + 0.10 * 75) / 0.95, 4)
    assert "leadlag" not in e.components


def test_bearish_candidate_with_positive_lag_gets_the_leadlag_bonus():
    # va_position NEGATIVE: a PE case needs IGNITION that broke BELOW value.
    # This fixture said +0.1 while the rest of it was bearish, which the timing
    # leg now rejects -- it only passed before because the leg tested that
    # IGNITION fired without asking which way.
    e = evaluate_symbol("INFY", AS_OF, "PHARMA", _inputs(
        flow_score=-70.0, rs_z20=-2.0, regime="STRONG_NEG",
        timing_score=90.0, best_lag=3, corr=0.6, va_position=-0.1))
    assert e.direction == "bearish"
    assert e.survived is True
    # flow=(70+100)/2=85, side_momentum=100, aligned_rs=(-1)*(-2.0)=2.0 ->
    # sector_rs=(2+3)/6*100=83.333, timing=90, regime(STRONG_NEG)=100,
    # leadlag=50+min(50,0.6*100)=100. Every axis present, so no renormalisation.
    assert round(e.conviction, 4) == round(
        0.30 * 85 + 0.20 * 100 + 0.15 * (5 / 6 * 100)
        + 0.20 * 90 + 0.10 * 100 + 0.05 * 100, 4)


def test_nonpositive_lag_gets_only_the_neutral_leadlag_baseline_not_a_bonus():
    for lag in (0, -1):
        e = _evaluate(best_lag=lag, corr=0.9)
        assert e.components["leadlag"] == 50.0


# ── the freshness leg (the defect this whole rewrite exists for) ────────────

def test_a_month_old_flow_score_fails_the_freshness_leg():
    """THE BUG. The LATERAL joins had no lower time bound, so with
    features_flow frozen at 2026-07-28 and the daemon evaluating every 30
    minutes, live bars were joining a month-old flow score and treating it as
    yesterday's EOD reading. Nothing in the code objected."""
    e = _evaluate(flow_age=22)
    assert e.survived is False
    assert e.first_failed_leg == "flow_fresh"
    assert e.legs["flow_present"] is True


def test_flow_exactly_at_the_age_limit_still_passes():
    assert _evaluate(flow_age=FLOW_MAX_AGE_SESSIONS).legs["flow_fresh"] is True
    assert _evaluate(flow_age=FLOW_MAX_AGE_SESSIONS + 1).first_failed_leg == "flow_fresh"


def test_an_unknowable_age_fails_closed_rather_than_being_assumed_fresh():
    assert _evaluate(flow_age=None).first_failed_leg == "flow_fresh"


def test_a_single_ingredient_flow_score_fails_even_when_saturated_and_fresh():
    """~42% of the historical candidate pool was a saturated +/-100 built from
    ONE ingredient -- and the ingredient most often surviving alone (O/S) is
    an unsigned activity measure carrying no direction at all."""
    e = _evaluate(flow_score=100.0, ingredients=1)
    assert e.first_failed_leg == "flow_fresh"


def test_a_missing_ingredient_count_is_treated_as_unknown_not_as_adequate():
    """Pre-006 rows did not record the count. 'We did not record it' is not
    evidence that the score was corroborated."""
    assert _evaluate(ingredients=None).first_failed_leg == "flow_fresh"
    assert _evaluate(ingredients=FLOW_MIN_INGREDIENTS).legs["flow_fresh"] is True


def test_a_stale_sector_rs_row_fails_its_own_leg():
    assert _evaluate(rs_age=RS_MAX_AGE_SESSIONS + 1).first_failed_leg == "sector_rs"


def test_a_stale_regime_bucket_fails_even_when_the_bucket_itself_permits():
    """A GEX regime describes dealer positioning now, not an hour ago."""
    e = _evaluate(regime="NEG", regime_age=REGIME_MAX_AGE_BARS + 1)
    assert e.first_failed_leg == "regime"


# ── the original filter legs, unchanged in meaning ─────────────────────────

def test_absent_flow_fails_the_first_leg_and_asks_nothing_further():
    e = _evaluate(flow_score=None)
    assert e.legs["flow_present"] is False
    assert e.first_failed_leg == "flow_present"
    assert e.direction is None
    assert e.conviction is None


def test_weak_flow_fails_the_strength_leg_not_the_freshness_one():
    e = _evaluate(flow_score=40.0)
    assert e.first_failed_leg == "flow_strength"
    assert e.legs["flow_fresh"] is True


def test_sector_rs_direction_mismatch_excludes_the_candidate():
    assert _evaluate(flow_score=65.0, rs_z20=-1.5).first_failed_leg == "sector_rs"


def test_sector_rs_below_min_abs_z_excludes_the_candidate():
    assert _evaluate(rs_z20=0.5).first_failed_leg == "sector_rs"


def test_regime_outside_permits_excludes_the_candidate_even_with_everything_else_confirming():
    for regime in ("POS", "STRONG_POS"):
        assert _evaluate(regime=regime).first_failed_leg == "regime"


def test_non_ignition_timing_state_excludes_the_candidate():
    assert _evaluate(timing_state="COMPRESSION").first_failed_leg == "timing"


def test_timing_score_below_threshold_excludes_the_candidate_even_if_ignition():
    assert _evaluate(timing_score=65.0).first_failed_leg == "timing"


# ── short-circuit semantics: NULL is not FALSE ─────────────────────────────

def test_legs_after_the_first_failure_are_null_not_false():
    """A NULL means 'never asked'. Recording it as FALSE would let the funnel
    blame six gates for one death and make every downstream count wrong."""
    e = _evaluate(flow_score=None)
    assert e.legs["flow_present"] is False
    assert all(e.legs[leg] is None for leg in LEG_ORDER if leg != "flow_present")


def test_exactly_one_leg_is_ever_false():
    for kwargs in ({"flow_score": None}, {"flow_age": 99}, {"flow_score": 10.0},
                   {"rs_z20": 0.1}, {"regime": "POS"}, {"timing_score": 1.0}):
        e = _evaluate(**kwargs)
        assert sum(1 for v in e.legs.values() if v is False) == 1


# ── conviction is computed for the whole cross-section, not just survivors ──

def test_conviction_is_computed_for_symbols_that_fail_the_filter():
    """The cross-sectional IC study needs a score for every name, not only for
    the handful that already passed. Correlating a component inside its own
    acceptance region measures nothing."""
    e = _evaluate(regime="STRONG_POS")
    # A hostile regime no longer ELIMINATES the candidate -- it scores 0 on that
    # axis and drags the conviction down, which is the whole point of weighing
    # rather than gating. The leg still records its dissent for the journal.
    assert e.legs["regime"] is False
    assert e.conviction is not None
    assert e.components["regime"] == 0.0
    assert e.conviction < _evaluate().conviction


def test_signed_readings_are_raw_and_unaligned():
    """Direction-aligned magnitudes correlate with the alignment, not the
    edge, so the journal keeps the signed originals too."""
    e = _evaluate(flow_score=-70.0, rs_z20=-2.0, va_position=0.2, gex_percentile=0.9)
    assert e.signed["flow"] == -70.0
    assert e.signed["rs"] == -2.0
    assert e.signed["timing"] < 0          # price below the developing value area
    assert round(e.signed["regime"], 4) == 0.8


# ── funnel counts come from the journal, not a second copy of the filter ───

def test_funnel_counts_attribute_each_death_to_exactly_one_leg():
    evaluations = [
        _evaluate(),                                # survives
        _evaluate(flow_score=None),                 # dies at flow_present
        _evaluate(flow_age=99),                     # dies at flow_fresh
        _evaluate(regime="POS"),                    # dies at regime
    ]
    stages = {s["leg"]: s for s in funnel_counts(evaluations)}
    assert stages["timing_bar"]["surviving"] == 4
    # The two VALIDITY gates still remove candidates outright...
    assert stages["flow_present"]["lost_here"] == 1
    assert stages["flow_fresh"]["lost_here"] == 1
    # ...but a hostile regime no longer does. It is dissent on one axis, and
    # the candidate is still judged on the other five, so it survives to be
    # ranked rather than being discarded here.
    assert stages["regime"]["surviving"] == 1
    assert stages["timing"]["surviving"] == 2


# ── age arithmetic ─────────────────────────────────────────────────────────

def test_age_in_sessions_counts_observed_sessions_not_calendar_days():
    """A Monday bar joining the previous Friday's EOD row is ONE session old,
    not three days old. Calendar days would make the same join mean different
    things midweek and over a weekend."""
    calendar = [date(2026, 8, 20), date(2026, 8, 21), date(2026, 8, 24), date(2026, 8, 25)]
    assert _age_in_sessions(calendar, date(2026, 8, 21), date(2026, 8, 24)) == 1
    assert _age_in_sessions(calendar, date(2026, 8, 24), date(2026, 8, 24)) == 0
    assert _age_in_sessions(calendar, date(2026, 8, 20), date(2026, 8, 25)) == 3


def test_a_row_older_than_the_calendar_window_reads_as_very_old_not_unknown():
    calendar = [date(2026, 8, 24), date(2026, 8, 25)]
    assert _age_in_sessions(calendar, date(2026, 1, 5), date(2026, 8, 25)) == len(calendar)


def test_age_is_none_when_there_is_no_row_at_all():
    assert _age_in_sessions([date(2026, 8, 25)], None, date(2026, 8, 25)) is None


# ── config hash ────────────────────────────────────────────────────────────

def test_config_hash_is_stable_and_travels_with_every_journaled_row():
    assert config_hash() == config_hash()
    assert len(config_hash()) == 16


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


def _surviving_evaluation(symbol="A", conviction=90.0, direction="bullish", sector20="IT"):
    """An Evaluation that build_tickets will project back into a Candidate.

    build_tickets now consumes evaluations rather than pre-filtered
    candidates, so the tests feed it what the real path feeds it.
    """
    candidate = _candidate(symbol, conviction, direction, sector20)
    return Evaluation(
        symbol=symbol, ts=AS_OF, sector20=sector20,
        inputs={"flow_score": candidate.flow_score, "rs_z20": candidate.rs_z20,
                "regime": candidate.regime, "timing_score": candidate.timing_score,
                "timing_state": candidate.timing_state, "best_lag": None,
                "leadlag_corr": None},
        legs={leg: True for leg in LEG_ORDER},
        first_failed_leg=None, survived=True, direction=direction,
        conviction=conviction, components=candidate.components,
        signed={"flow": candidate.flow_score, "rs": candidate.rs_z20,
                "timing": candidate.timing_score, "regime": 0.0},
    )


def _risk_state():
    return RiskState(capital=1_000_000.0, daily_pnl_pct=0.0, weekly_pnl_pct=0.0,
                     open_positions=[], kelly_edge=None)


def test_candidate_below_conviction_min_is_gated_before_touching_instrument_or_risk(monkeypatch):
    monkeypatch.setattr(m6_select, "evaluate_bar", lambda c, ts: [_surviving_evaluation(conviction=CONVICTION_MIN - 0.1)])
    monkeypatch.setattr(m6_select, "load_risk_state", lambda c, ts, cap: _risk_state())
    monkeypatch.setattr(m6_select, "resolve_instrument", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")))
    rows = build_tickets(object(), AS_OF, 1_000_000.0)
    assert len(rows) == 1
    assert rows[0]["emitted"] is False
    assert "conviction" in rows[0]["gated_reason"]


def test_no_model_state_can_suppress_the_candidate_universe(monkeypatch):
    """The 2026-09-04 doctrine change, as a regression test.

    build_tickets used to hand the whole bar to the intraday neural model the
    moment any compatible artifact loaded, and abstain entirely when one was
    registered but incompatible. A `shadow` artifact then refused every row it
    had taken over: 9,030 gated rows and zero emissions across 2026-09-02..04,
    with the fusion evidence that identified names like KEI bearish left with
    no path of its own. A research model must never be able to delete the
    signal it exists to rank, in any of its states.
    """
    monkeypatch.setattr(m6_select, "load_risk_state", lambda c, ts, cap: _risk_state())
    monkeypatch.setattr(m6_select, "resolve_instrument", lambda *a, **k: {
        "instrument": "X", "premium": 50.0, "strike": 100,
        "expiry": date(2026, 8, 28), "option_type": "CE", "lot_size": 150,
    })
    monkeypatch.setattr(m6_select, "risk_check", lambda *a, **k: SizingResult(
        allowed=True, lots=1, notional=7500, risk_rupees=7500, method="fixed_fractional"))

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("the intraday model must not sit on the ticket path")

    monkeypatch.setattr(m6_select, "load_selector_model", _must_not_be_called)

    rows = build_tickets(
        object(), AS_OF, 1_000_000.0,
        evaluations=[_surviving_evaluation(symbol="KEI")],
    )

    assert [row["symbol"] for row in rows] == ["KEI"]
    assert rows[0]["instrument"] == "X"
    assert "shadow" not in (rows[0]["gated_reason"] or "")


def test_candidate_past_top_n_rank_is_gated_by_rank(monkeypatch):
    evaluations = [_surviving_evaluation(symbol=f"S{i}", conviction=99.0 - i)
                   for i in range(TOP_N_PER_BAR + 1)]
    monkeypatch.setattr(m6_select, "evaluate_bar", lambda c, ts: evaluations)
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
    monkeypatch.setattr(m6_select, "evaluate_bar", lambda c, ts: [_surviving_evaluation()])
    monkeypatch.setattr(m6_select, "load_risk_state", lambda c, ts, cap: _risk_state())
    monkeypatch.setattr(m6_select, "resolve_instrument", lambda *a, **k: None)
    rows = build_tickets(object(), AS_OF, 1_000_000.0)
    assert rows[0]["emitted"] is False
    assert rows[0]["gated_reason"] == "no tradable ATM contract resolved"


def test_m7_refusal_floors_the_size_instead_of_skipping_the_trade(monkeypatch):
    """M7 GATE OFF (2026-08-28): M7 still SIZES, it no longer VETOES.

    This asserted the opposite -- that an M7 refusal produced emitted=False.
    The lane is paper-only, and with the gate on its top-ranked names were all
    rejected for rounding to 0 lots under a premium cap M7's own docstring
    shows is mis-set, so it produced no fills to learn from. A refusal now
    takes MIN_PAPER_LOTS and RECORDS that it was floored, so nothing later
    mistakes a floor-sized paper position for one M7 sanctioned.
    """
    monkeypatch.setattr(m6_select, "evaluate_bar", lambda c, ts: [_surviving_evaluation()])
    monkeypatch.setattr(m6_select, "load_risk_state", lambda c, ts, cap: _risk_state())
    monkeypatch.setattr(m6_select, "resolve_instrument", lambda *a, **k: {
        "instrument": "X", "premium": 50.0, "strike": 100, "expiry": date(2026, 8, 28), "option_type": "CE", "lot_size": 150,
    })
    monkeypatch.setattr(m6_select, "risk_check", lambda *a, **k: SizingResult(allowed=False, reason="STAND-DOWN"))
    rows = build_tickets(object(), AS_OF, 1_000_000.0)
    assert rows[0]["sizing_lots"] == m6_select.MIN_PAPER_LOTS
    # the refusal is preserved, not swallowed
    assert rows[0]["sizing_method"] == "floored:STAND-DOWN"
    # ...and the row is journaled, not emitted: sizing is M7's answer, the
    # actionable list is the pre-close swing lane's (2026-09-04 plan).
    assert rows[0]["emitted"] is False
    assert rows[0]["gated_reason"] == m6_select.CANDIDATE_UNIVERSE_REASON


def test_a_candidate_clearing_every_gate_is_journaled_with_sizing_fields_populated(monkeypatch):
    monkeypatch.setattr(m6_select, "evaluate_bar", lambda c, ts: [_surviving_evaluation()])
    monkeypatch.setattr(m6_select, "load_risk_state", lambda c, ts, cap: _risk_state())
    monkeypatch.setattr(m6_select, "resolve_instrument", lambda *a, **k: {
        "instrument": "TCS26AUG3500CE", "premium": 50.0, "strike": 3500, "expiry": date(2026, 8, 28), "option_type": "CE", "lot_size": 150,
    })
    monkeypatch.setattr(m6_select, "risk_check", lambda *a, **k: SizingResult(
        allowed=True, lots=2, notional=15000.0, risk_rupees=15000.0, method="kelly_0.25x",
    ))
    rows = build_tickets(object(), AS_OF, 1_000_000.0)
    row = rows[0]
    assert row["emitted"] is False
    assert row["gated_reason"] == m6_select.CANDIDATE_UNIVERSE_REASON
    assert row["sizing_lots"] == 2
    assert row["sizing_method"] == "kelly_0.25x"
    assert row["stop"] == round(50.0 * 0.85, 4)
    assert row["target1"] == round(50.0 * 1.20, 4)
    assert row["target2"] == round(50.0 * 1.50, 4)


def test_every_filtered_candidate_appears_in_the_audit_trail_not_just_winners(monkeypatch):
    """doctrine #5 ('everything measurable') -- gated-out near-misses must
    still be recorded, not silently dropped."""
    monkeypatch.setattr(m6_select, "evaluate_bar", lambda c, ts: [
        _surviving_evaluation(symbol="WINNER", conviction=95.0),
        _surviving_evaluation(symbol="LOSER", conviction=20.0),
    ])
    monkeypatch.setattr(m6_select, "load_risk_state", lambda c, ts, cap: _risk_state())
    monkeypatch.setattr(m6_select, "resolve_instrument", lambda *a, **k: {
        "instrument": "X", "premium": 50.0, "strike": 100, "expiry": date(2026, 8, 28), "option_type": "CE", "lot_size": 150,
    })
    monkeypatch.setattr(m6_select, "risk_check", lambda *a, **k: SizingResult(allowed=True, lots=1, notional=7500, risk_rupees=7500, method="fixed_fractional"))
    rows = build_tickets(object(), AS_OF, 1_000_000.0)
    assert {r["symbol"] for r in rows} == {"WINNER", "LOSER"}
    # Both journaled; the near-miss keeps its OWN reason rather than being
    # flattened into the universe reason the cleared candidate carries.
    assert [r["emitted"] for r in rows] == [False, False]
    assert rows[0]["gated_reason"] == m6_select.CANDIDATE_UNIVERSE_REASON
    assert "conviction" in rows[1]["gated_reason"]


def test_a_sized_candidate_consumes_risk_budget_for_the_rest_of_the_bar(monkeypatch):
    """The second candidate's risk_check must see the first candidate's
    position already reflected in open_positions -- otherwise two rows
    at the same bar would quote the same headroom twice."""
    monkeypatch.setattr(m6_select, "evaluate_bar", lambda c, ts: [
        _surviving_evaluation(symbol="FIRST", conviction=95.0),
        _surviving_evaluation(symbol="SECOND", conviction=90.0),
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


# ---------------------------------------------------------------------------
# the CE/PE split (2026-08-28)
# ---------------------------------------------------------------------------

def test_a_bullish_case_is_not_confirmed_by_a_downside_break():
    """The defect the split exists to fix.

    M5 fires IGNITION on "price beyond value area" and records WHICH SIDE in
    va_position. M6 tested only that IGNITION fired, so a CE candidate could be
    confirmed by a breakdown. Not hypothetical: on 2026-08-28 three of the four
    IGNITION bars were below value, so the unchecked leg had a 3-in-4 chance of
    confirming a call with a break the wrong way.
    """
    e = _evaluate(flow_score=75.0, rs_z20=1.5, va_position=-0.4)
    assert e.first_failed_leg == "timing"
    assert e.legs["timing"] is False
    # every earlier leg still passed -- it is specifically the break direction
    assert e.legs["sector_rs"] is True and e.legs["regime"] is True


def test_a_bearish_case_is_not_confirmed_by_an_upside_break():
    """The mirror, so the check cannot be satisfied by hard-coding one sign."""
    e = _evaluate(flow_score=-75.0, rs_z20=-1.5, regime="STRONG_NEG", va_position=0.4)
    assert e.first_failed_leg == "timing"


def test_an_unrecorded_break_direction_fails_rather_than_passes():
    """The direction of the break IS the leg. "Not recorded" is not evidence
    that it agreed, so it fails like any other unmeasured input."""
    e = _evaluate(flow_score=75.0, rs_z20=1.5, va_position=None)
    assert e.first_failed_leg == "timing"


def test_each_side_is_selected_on_its_own_signed_evidence():
    """A strongly bearish reading is found as the PE case, with both branches
    evaluated rather than the side being committed from flow's sign first."""
    e = _evaluate(flow_score=-75.0, rs_z20=-1.5, regime="STRONG_NEG", va_position=-0.4)
    assert e.direction == "bearish" and e.survived is True

    # and the bullish mirror is found as the CE case
    e = _evaluate(flow_score=75.0, rs_z20=1.5, va_position=0.4)
    assert e.direction == "bullish" and e.survived is True


def test_flow_strength_is_signed_so_a_bearish_score_cannot_arm_the_bullish_case():
    """`abs(flow) >= FLOW_MIN_ABS` accepted a strongly BEARISH score as
    evidence for the bullish case and leaned on sector_rs to veto it later. A
    veto is not the same as never proposing it: with RS absent or weak the
    wrong-signed score would reach further than it should."""
    e = _evaluate(flow_score=-75.0, rs_z20=None, va_position=0.4)
    # PE is the only side its flow can arm, and PE dies at sector_rs (no RS row)
    assert e.direction == "bearish"
    assert e.first_failed_leg == "sector_rs"


def test_the_deeper_of_the_two_sides_is_journaled_when_neither_survives():
    """"The CE case died at sector_rs" is a more useful record than "something
    died", so the side that got furthest is the one kept."""
    # Bullish flow clears strength; PE cannot even arm on this score, so the
    # CE evaluation is the deeper one and is what gets journaled.
    e = _evaluate(flow_score=75.0, rs_z20=None, va_position=0.4)
    assert e.direction == "bullish"
    assert e.first_failed_leg == "sector_rs"


# ---------------------------------------------------------------------------
# side_momentum: be in the contract that is being accumulated
# ---------------------------------------------------------------------------

def test_a_bullish_view_does_not_buy_a_call_nobody_is_accumulating():
    """The instrument is not a direction token. `CE if bullish else PE` inferred
    the contract from a view about the UNDERLYING, so a call could be bought
    while it bled -- theta, an IV crush, or simply nobody paying up for it.

    Measured 2026-08-28: 139 of 153 tickets would have gone into a contract
    with no buildup on its own side.
    """
    e = _evaluate(flow_score=75.0, rs_z20=1.5, ce_state="long_unwind")
    assert e.first_failed_leg == "side_momentum"
    assert e.legs["flow_strength"] is True     # the view was fine
    assert e.legs["side_momentum"] is False
    # The later axes ARE still evaluated -- dissent on one no longer stops the
    # rest being asked, because they all feed the weighted judgement.
    assert e.legs["sector_rs"] is True
    # ...and the missing momentum shows up as a LOWER score, not a veto.
    assert e.conviction < _evaluate().conviction


def test_the_put_side_is_judged_on_its_own_state_not_the_calls():
    """The two sides are not mirror images: on 2026-08-28, 14 names had BOTH
    sides short_covering and 11 had BOTH long_unwind. A PE case must therefore
    read pe_state, and must not be rescued by a healthy call side."""
    e = _evaluate(flow_score=-75.0, rs_z20=-1.5, regime="STRONG_NEG",
                  va_position=-0.4, ce_state="long_buildup", pe_state="long_unwind")
    assert e.first_failed_leg == "side_momentum"

    # ...and it passes when the PUT side itself is being accumulated, even
    # though the call side is now the dead one.
    e = _evaluate(flow_score=-75.0, rs_z20=-1.5, regime="STRONG_NEG",
                  va_position=-0.4, ce_state="long_unwind", pe_state="long_buildup")
    assert e.survived is True and e.direction == "bearish"


def test_short_covering_is_not_accumulation():
    """OI falling while premium rises is a squeeze being closed out, not money
    coming in -- it ends when the shorts are done. Only long_buildup (OI up AND
    premium up) counts as being in the momentum."""
    for state in ("short_covering", "short_buildup", "long_unwind", None):
        e = _evaluate(flow_score=75.0, rs_z20=1.5, ce_state=state)
        assert e.first_failed_leg == "side_momentum", state
