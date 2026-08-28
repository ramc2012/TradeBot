"""Ranker, evaluation gates, and capital policies — pure computation.

The load-bearing assertions are the refusals: that a fit on too little data is
declined rather than returned weak, that a gate which cannot be computed FAILS
rather than being skipped, and that abstaining beats a bad trade.
"""
from __future__ import annotations

import math

import pytest

from candidate_capture.evaluation import (
    KELLY_TEST_FRACTION,
    MIN_EVAL_ROWS,
    MIN_EVAL_SESSIONS,
    calibration_error,
    clustered_mean,
    evaluate_gates,
    expected_log_growth,
    gates_passed,
    max_drawdown,
    refusal_summary,
    stress_returns,
)
from candidate_capture.features import (
    TARGET_BEATS_BREAKEVEN,
    TARGET_NET_POSITIVE,
    build_features,
    build_target,
    feature_names,
)
from candidate_capture.model import (
    MIN_MINORITY_ROWS,
    MIN_TRAIN_ROWS,
    apply_calibrator,
    fit_calibrator,
    fit_logistic,
    predict_raw,
    score_rows,
    serialize_artifact,
)
from candidate_capture.ranking import (
    NO_TRADE,
    PORTFOLIO_DOUBLING,
    PORTFOLIO_PRACTICAL,
    PORTFOLIO_RESEARCH,
    candidate_utility,
    rank_decision_set,
    run_all_portfolios,
    run_portfolio,
    select,
)


def _snap(**kw):
    row = {
        "option_type": "CE",
        "strike": 24200.0,
        "moneyness": "ATM",
        "moneyness_steps": 0.2,
        "liquidity_bucket": "TOP",
        "liquidity_percentile": 0.9,
        "expiry_class": "MONTHLY",
        "days_to_expiry": 33,
        "hours_to_expiry": 790.0,
        "expiry_day_flag": False,
        "monthly_expiry_week_flag": False,
        "ltp": 402.3,
        "bid": 402.35,
        "ask": 403.95,
        "spread_pct": 0.004,
        "volume": 50_000,
        "oi": 2_039_180,
        "iv": 13.5,
        "delta": 0.5,
        "gamma": 0.001,
        "theta": -8.0,
        "vega": 12.0,
        "spot": 24208.0,
        "features": {"pcr_oi": 0.92, "max_pain": 24000.0, "atm_iv": 13.4, "india_vix": 13.2},
    }
    row.update(kw)
    return row


# ══════════════════════════════════════════════════════════════════════════
class TestFeatures:
    def test_vector_matches_declared_names(self):
        assert len(build_features(_snap())) == len(feature_names())

    def test_vector_is_stable_when_fields_are_missing(self):
        sparse = {"option_type": "PE", "moneyness": "UNKNOWN", "features": {}}
        assert len(build_features(sparse)) == len(feature_names())

    def test_unknown_category_gets_no_column_of_its_own(self):
        names = feature_names()
        idx = [i for i, n in enumerate(names) if n.startswith("moneyness__")]
        vec = build_features(_snap(moneyness="UNKNOWN"))
        # All zeros across the block: UNKNOWN is a data-quality event, not a class.
        assert sum(vec[i] for i in idx) == 0.0

    def test_one_hot_is_set_for_a_known_class(self):
        names = feature_names()
        idx = names.index("moneyness__ATM")
        assert build_features(_snap(moneyness="ATM"))[idx] == 1.0

    def test_missing_indicators_fire(self):
        names = feature_names()
        vec = build_features(_snap(iv=None, oi=None))
        assert vec[names.index("iv_missing")] == 1.0
        assert vec[names.index("oi_missing")] == 1.0

    def test_features_cannot_see_an_outcome(self):
        """The leak guard is the signature: outcome fields are simply ignored."""
        planted = _snap()
        planted["option_net_return_pct"] = 999.0
        planted["spot_return_pct"] = 999.0
        assert build_features(planted) == build_features(_snap())


class TestTargets:
    def test_net_positive(self):
        ok = {"label_status": "ok", "trade_arrived": True, "option_net_return_pct": 0.05}
        bad = {"label_status": "ok", "trade_arrived": True, "option_net_return_pct": -0.05}
        assert build_target(ok, TARGET_NET_POSITIVE) == 1
        assert build_target(bad, TARGET_NET_POSITIVE) == 0

    def test_unlabellable_is_none_not_zero(self):
        row = {"label_status": "unlabellable_no_forward", "option_net_return_pct": None}
        assert build_target(row, TARGET_NET_POSITIVE) is None

    def test_untraded_mark_is_not_evidence(self):
        row = {"label_status": "ok", "trade_arrived": False, "option_net_return_pct": 0.0}
        assert build_target(row, TARGET_NET_POSITIVE) is None

    def test_breakeven_target_uses_favourable_excursion_only(self):
        fell = {
            "label_status": "ok", "trade_arrived": True,
            "option_mfe_pct": -0.25, "breakeven_move_pct": 0.03,
        }
        assert build_target(fell, TARGET_BEATS_BREAKEVEN) == 0
        rose = {**fell, "option_mfe_pct": 0.25}
        assert build_target(rose, TARGET_BEATS_BREAKEVEN) == 1

    def test_unknown_target_raises(self):
        with pytest.raises(ValueError):
            build_target({"label_status": "ok"}, "not_a_target")


# ══════════════════════════════════════════════════════════════════════════
def _separable(n=400):
    X, y = [], []
    for i in range(n):
        signal = 1.0 if i % 2 == 0 else -1.0
        X.append([signal, (i % 7) * 0.1])
        y.append(1 if signal > 0 else 0)
    return X, y


class TestLogistic:
    def test_learns_a_separable_signal(self):
        X, y = _separable()
        fit = fit_logistic(X, y)
        assert fit.ok and fit.converged
        probs = predict_raw(X, fit.coefficients, fit.intercept)
        assert probs[0] > 0.9 and probs[1] < 0.1

    def test_refuses_too_few_rows(self):
        X, y = _separable(50)
        fit = fit_logistic(X, y)
        assert fit.ok is False
        assert str(MIN_TRAIN_ROWS) in fit.reason

    def test_refuses_a_near_single_class_target(self):
        X = [[float(i % 5), 1.0] for i in range(400)]
        y = [1] * 395 + [0] * 5
        fit = fit_logistic(X, y)
        assert fit.ok is False
        assert str(MIN_MINORITY_ROWS) in fit.reason
        # And it says WHY, so an untrainable stratum is not read as weak signal.
        assert "decidable" in fit.reason

    def test_is_deterministic(self):
        X, y = _separable()
        assert fit_logistic(X, y).coefficients == fit_logistic(X, y).coefficients

    def test_scoring_refuses_a_changed_feature_layout(self):
        X, y = _separable()
        fit = fit_logistic(X, y)
        artifact = serialize_artifact(fit=fit, feature_names=["a", "b"], calibrator=None)
        with pytest.raises(ValueError, match="feature layout"):
            score_rows(artifact, X, ["b", "a"])

    def test_scoring_works_through_the_artifact(self):
        X, y = _separable()
        fit = fit_logistic(X, y)
        artifact = serialize_artifact(fit=fit, feature_names=["a", "b"], calibrator=None)
        assert len(score_rows(artifact, X, ["a", "b"])) == len(X)


class TestCalibration:
    def test_isotonic_is_reusable_from_the_leaf_loader(self):
        probs = [i / 100 for i in range(100)]
        outcomes = [1 if p > 0.5 else 0 for p in probs]
        cal = fit_calibrator(probs, outcomes)
        assert cal is not None
        out = apply_calibrator(cal, [0.1, 0.9])
        assert all(0.0 <= v <= 1.0 for v in out)

    def test_absent_calibrator_passes_probabilities_through(self):
        assert apply_calibrator(None, [0.3, 0.7]) == [0.3, 0.7]


# ══════════════════════════════════════════════════════════════════════════
class TestClusteredInference:
    def test_clustering_widens_the_interval_versus_nominal(self):
        """The whole reason SEs are clustered here."""
        # 10 sessions; every row inside a session is identical, so the session
        # is the real observation and the naive n=200 is a fiction.
        values, clusters = [], []
        for s in range(10):
            per_session = 0.02 if s % 2 == 0 else -0.01
            for _ in range(20):
                values.append(per_session)
                clusters.append(f"2026-08-{s + 1:02d}")
        clustered = clustered_mean(values, clusters)
        nominal_se = (
            math.sqrt(sum((v - clustered["mean"]) ** 2 for v in values) / (len(values) - 1))
            / math.sqrt(len(values))
        )
        assert clustered["clusters"] == 10
        assert clustered["se"] > nominal_se

    def test_single_cluster_cannot_produce_a_standard_error(self):
        out = clustered_mean([0.01] * 50, ["2026-08-25"] * 50)
        assert out["se"] is None and out["usable"] is False

    def test_too_few_clusters_is_flagged_unusable(self):
        values, clusters = [], []
        for s in range(3):
            values += [0.01, 0.02]
            clusters += [f"d{s}"] * 2
        assert clustered_mean(values, clusters)["usable"] is False


class TestGrowthMetrics:
    def test_expected_log_growth_positive_and_negative(self):
        assert expected_log_growth([0.5, 0.5, 0.5]) > 0
        assert expected_log_growth([-0.5, -0.5]) < 0

    def test_total_wipeout_is_negative_infinity_not_a_number(self):
        assert expected_log_growth([-100.0], fraction=1.0) == float("-inf")

    def test_max_drawdown_measures_peak_to_trough(self):
        dd = max_drawdown([1.0, -1.0, -1.0], fraction=0.5)
        assert 0.0 < dd < 1.0
        assert max_drawdown([0.1, 0.1]) == 0.0

    def test_stress_makes_returns_worse(self):
        rows = [{"option_net_return_pct": 0.02}, {"option_net_return_pct": 0.03}]
        assert all(s < 0.02 + 1e-9 for s in stress_returns(rows))


class TestGates:
    def _inputs(self, mean_return: float, sessions: int = 12, per: int = 30):
        """Realistic synthetic evaluation data.

        Deliberately NOT constant per session: identical sessions give zero
        between-cluster variance (so the clustered SE is degenerate) and a
        single-valued target (so Brier skill has no base rate to beat). Both
        are correctly refused by the gates, which makes constant data useless
        for testing that a good model CAN pass.
        """
        rows, probs, outcomes, rets, dates = [], [], [], [], []
        for s in range(sessions):
            session_effect = ((s % 5) - 2) * 0.01          # sessions differ
            for i in range(per):
                trade_noise = ((i % 7) - 3) * 0.02          # wins and losses both occur
                r = mean_return + session_effect + trade_noise
                rows.append({"option_net_return_pct": r})
                rets.append(r)
                # Informative but imperfect: correlated with the outcome, not
                # identical to it, so calibration has something to measure.
                probs.append(0.72 if r > 0 else 0.34)
                outcomes.append(1 if r > 0 else 0)
                dates.append(f"2026-08-{s + 1:02d}")
        return rows, probs, outcomes, rets, dates

    def test_a_strong_model_can_pass(self):
        rows, probs, outcomes, rets, dates = self._inputs(0.05)
        gates, metrics = evaluate_gates(
            rows=rows, probs=probs, outcomes=outcomes,
            selected_returns=rets, session_dates=dates,
        )
        assert gates_passed(gates), refusal_summary(gates)
        assert metrics["eval_sessions"] == 12

    def test_a_losing_model_is_refused(self):
        rows, probs, outcomes, rets, dates = self._inputs(-0.05)
        gates, _ = evaluate_gates(
            rows=rows, probs=probs, outcomes=outcomes,
            selected_returns=rets, session_dates=dates,
        )
        assert not gates_passed(gates)
        assert "positive_net_return" in refusal_summary(gates)

    def test_too_few_sessions_is_refused_however_good_the_returns(self):
        rows, probs, outcomes, rets, dates = self._inputs(0.20, sessions=3, per=100)
        gates, _ = evaluate_gates(
            rows=rows, probs=probs, outcomes=outcomes,
            selected_returns=rets, session_dates=dates,
        )
        failed = {g.name for g in gates if not g.passed}
        assert "sample_size_sessions" in failed

    def test_one_exceptional_trade_cannot_carry_a_model(self):
        rows, probs, outcomes, rets, dates = self._inputs(-0.002)
        rets[0] = 50.0  # one enormous winner
        rows[0] = {"option_net_return_pct": 50.0}
        gates, _ = evaluate_gates(
            rows=rows, probs=probs, outcomes=outcomes,
            selected_returns=rets, session_dates=dates,
        )
        failed = {g.name for g in gates if not g.passed}
        assert "not_one_exceptional_trade" in failed

    def test_a_thin_edge_dies_under_slippage_stress(self):
        rows, probs, outcomes, rets, dates = self._inputs(0.002)
        gates, _ = evaluate_gates(
            rows=rows, probs=probs, outcomes=outcomes,
            selected_returns=rets, session_dates=dates,
        )
        failed = {g.name for g in gates if not g.passed}
        assert "robust_to_worse_slippage" in failed

    def test_must_beat_the_incumbent_champion(self):
        rows, probs, outcomes, rets, dates = self._inputs(0.05)
        gates, _ = evaluate_gates(
            rows=rows, probs=probs, outcomes=outcomes,
            selected_returns=rets, session_dates=dates,
            champion_mean_return=0.50,
        )
        failed = {g.name for g in gates if not g.passed}
        assert "beats_champion" in failed

    def test_an_uncomputable_gate_fails_rather_than_being_skipped(self):
        gates, _ = evaluate_gates(
            rows=[], probs=[], outcomes=[], selected_returns=[], session_dates=[],
        )
        assert gates and not gates_passed(gates)
        assert all(g.passed is False for g in gates)


# ══════════════════════════════════════════════════════════════════════════
class TestRanking:
    def test_no_trade_is_always_present_and_sits_at_zero(self):
        ranked = rank_decision_set([_snap()], [0.5])
        abstain = [c for c in ranked if c.is_no_trade]
        assert len(abstain) == 1 and abstain[0].utility == 0.0

    def test_a_bad_candidate_loses_to_abstaining(self):
        ranked = rank_decision_set([_snap()], [0.01])
        assert select(ranked).is_no_trade is True

    def test_a_good_candidate_beats_abstaining(self):
        ranked = rank_decision_set([_snap(spread_pct=0.002)], [0.97])
        assert select(ranked).is_no_trade is False

    def test_a_wide_spread_is_penalised_against_an_identical_contract(self):
        tight, _ = candidate_utility(probability=0.8, snapshot=_snap(spread_pct=0.002))
        wide, _ = candidate_utility(probability=0.8, snapshot=_snap(spread_pct=0.30))
        assert tight > wide

    def test_illiquidity_is_penalised(self):
        liquid, _ = candidate_utility(probability=0.8, snapshot=_snap(liquidity_percentile=0.95))
        thin, _ = candidate_utility(probability=0.8, snapshot=_snap(liquidity_percentile=0.05))
        assert liquid > thin

    def test_concentration_is_penalised(self):
        fresh, _ = candidate_utility(probability=0.8, snapshot=_snap(), exposure_share=0.0)
        loaded, _ = candidate_utility(probability=0.8, snapshot=_snap(), exposure_share=1.0)
        assert fresh > loaded

    def test_components_are_exposed_so_a_ranking_can_be_explained(self):
        _, comps = candidate_utility(probability=0.8, snapshot=_snap())
        assert set(comps) == {
            "expected_log_growth", "uneconomic_penalty", "slippage_penalty",
            "downside_tail_penalty", "uncertainty_penalty", "liquidity_penalty",
            "concentration_penalty",
        }

    def test_an_input_no_trade_row_is_not_scored_as_a_contract(self):
        ranked = rank_decision_set([_snap(option_type=NO_TRADE, strike=None)], [0.99])
        assert len([c for c in ranked if c.is_no_trade]) == 1
        assert len(ranked) == 1


class TestRankingMonotonicity:
    """The invariant an audit found violated: utility must RISE with probability.

    The uncertainty and tail penalties both vary with p. When their combined
    slope exceeded the expected-log slope, total utility FELL as probability
    rose for every p < 0.5 — and since the measured base rate of a profitable
    long is 0.15-0.31, essentially every real candidate sat in that region. The
    ranker was systematically preferring the contract least likely to profit.
    """

    def test_weights_satisfy_the_slope_budget(self):
        from candidate_capture.ranking import (
            assert_monotone_in_probability, probability_slope_budget,
        )

        signal, penalty = probability_slope_budget()
        assert penalty < signal, (
            f"probability-varying penalties ({penalty:.6f}) must stay under the "
            f"expected-log slope ({signal:.6f}) or the ranking inverts"
        )
        assert_monotone_in_probability()

    def test_utility_rises_with_probability_everywhere(self):
        snap = _snap(spread_pct=0.015, moneyness_steps=2.5,
                     liquidity_percentile=0.9, breakeven_move_pct=0.03)
        last = None
        for i in range(5, 100, 5):
            p = i / 100
            u, _ = candidate_utility(probability=p, snapshot=snap, breakeven_move_pct=0.03)
            if last is not None:
                assert u > last, f"utility fell between p={p - 0.05:.2f} and p={p:.2f}"
            last = u

    def test_utility_rises_with_probability_at_the_moneyness_cap(self):
        """The tail penalty is largest at the cap, which is where the slope
        budget is tightest."""
        snap = _snap(spread_pct=0.015, moneyness_steps=8.0,
                     liquidity_percentile=0.5, breakeven_move_pct=0.03)
        last = None
        for i in range(5, 100, 5):
            u, _ = candidate_utility(probability=i / 100, snapshot=snap, breakeven_move_pct=0.03)
            if last is not None:
                assert u > last
            last = u

    def test_the_abstain_floor_is_actually_reachable(self):
        """It previously required p > 0.795, which a calibrated model on a ~28%
        base rate cannot emit — so 92% abstention was arithmetic, not judgement."""
        snap = _snap(spread_pct=0.015, moneyness_steps=2.5,
                     liquidity_percentile=0.9, breakeven_move_pct=0.03)
        crossing = next(
            (i / 100 for i in range(5, 100)
             if candidate_utility(probability=i / 100, snapshot=snap,
                                  breakeven_move_pct=0.03)[0] > 0),
            None,
        )
        assert crossing is not None and crossing <= 0.70, (
            f"abstain floor needs p>{crossing}, which no calibrated model on this "
            f"base rate can reach"
        )


class TestPayoffGeometry:
    """Regressions for the two ranking defects adversarial review confirmed."""

    def test_a_liquid_contract_beats_abstaining_at_a_plausible_probability(self):
        """The old model needed p~0.97, so nothing could ever be selected."""
        liquid = _snap(spread_pct=0.002, liquidity_percentile=0.95,
                       breakeven_move_pct=0.0102, moneyness_steps=0.2)
        chosen = select(rank_decision_set([liquid], [0.80]))
        assert chosen.is_no_trade is False

    def test_an_uneconomic_wing_is_refused_at_any_probability(self):
        """A contract whose breakeven exceeds the target cannot be rescued."""
        wing = _snap(strike=25000.0, spread_pct=0.25, liquidity_percentile=0.05,
                     breakeven_move_pct=0.45, moneyness_steps=8.0)
        for p in (0.5, 0.9, 0.99):
            assert select(rank_decision_set([wing], [p])).is_no_trade is True

    def test_a_liquid_contract_outranks_a_cheap_wing(self):
        """The old breakeven-scaled upside INVERTED this: the wing always won."""
        liquid = _snap(spread_pct=0.002, liquidity_percentile=0.95,
                       breakeven_move_pct=0.0102, moneyness_steps=0.2)
        wing = _snap(strike=25000.0, spread_pct=0.25, liquidity_percentile=0.05,
                     breakeven_move_pct=0.45, moneyness_steps=8.0)
        ranked = rank_decision_set([wing, liquid], [0.85, 0.85])
        assert ranked[0].strike == liquid["strike"]

    def test_a_missing_spread_is_charged_the_worst_case_not_zero(self):
        """assess_quote returns None for the WORST quote states, so None must
        never score better than a measured wide spread."""
        quoted, _ = candidate_utility(probability=0.8, snapshot=_snap(spread_pct=0.05))
        absent, _ = candidate_utility(probability=0.8, snapshot=_snap(spread_pct=None))
        assert absent < quoted


class TestGateShape:
    def test_champion_gate_is_always_present(self):
        """A conditional gate meant two models faced different bars."""
        rows = [{"option_net_return_pct": 0.05}] * 10
        gates, _ = evaluate_gates(
            rows=rows, probs=[0.8] * 10, outcomes=[1] * 10,
            selected_returns=[0.05] * 10, session_dates=[f"d{i}" for i in range(10)],
        )
        assert "beats_champion" in {g.name for g in gates}

    def test_a_model_that_mostly_picks_unmarkable_contracts_is_refused(self):
        rows = [{"option_net_return_pct": 0.05}] * 100
        gates, metrics = evaluate_gates(
            rows=rows, probs=[0.8] * 100, outcomes=[1] * 100,
            selected_returns=[0.05] * 10,
            session_dates=[f"d{i}" for i in range(10)],
            unresolved_selections=90,
        )
        failed = {g.name for g in gates if not g.passed}
        assert "selections_are_resolvable" in failed
        assert metrics["unresolved_selections"] == 90

    def test_too_few_actual_trades_is_refused_however_good_they_were(self):
        gates, _ = evaluate_gates(
            rows=[{"option_net_return_pct": 0.9}] * 500, probs=[0.9] * 500,
            outcomes=[1] * 500, selected_returns=[0.9] * 10,
            session_dates=[f"d{i}" for i in range(10)],
        )
        failed = {g.name for g in gates if not g.passed}
        assert "sample_size_trades" in failed


class TestPortfolios:
    def test_all_three_run_over_the_same_decisions(self):
        out = run_all_portfolios([0.1, -0.05, 0.2, -0.1])
        assert set(out) == {PORTFOLIO_RESEARCH, PORTFOLIO_PRACTICAL, PORTFOLIO_DOUBLING}

    def test_research_book_sizes_identically_regardless_of_conviction(self):
        rets = [0.1, -0.05, 0.2]
        a = run_portfolio(rets, policy=PORTFOLIO_RESEARCH, probabilities=[0.9, 0.9, 0.9])
        b = run_portfolio(rets, policy=PORTFOLIO_RESEARCH, probabilities=[0.1, 0.1, 0.1])
        assert a.equity == b.equity

    def test_practical_book_does_size_with_conviction(self):
        rets = [0.1, 0.1, 0.1]
        high = run_portfolio(rets, policy=PORTFOLIO_PRACTICAL, probabilities=[0.95] * 3)
        low = run_portfolio(rets, policy=PORTFOLIO_PRACTICAL, probabilities=[0.5] * 3)
        assert high.equity > low.equity

    def test_practical_book_never_exceeds_its_risk_cap(self):
        # Even at certainty, one loss cannot cost more than the cap.
        out = run_portfolio([-1.0], policy=PORTFOLIO_PRACTICAL, probabilities=[1.0])
        assert out.equity >= 0.97

    def test_doubling_book_restarts_its_own_episode_without_ruining_the_record(self):
        out = run_portfolio([-0.9, -0.9, 1.0], policy=PORTFOLIO_DOUBLING)
        assert out.episodes_restarted >= 1
        # The shadow episode restarting is not the same as the book vanishing.
        assert out.trades == 3

    def test_doubling_book_counts_completed_steps(self):
        out = run_portfolio([3.0] * 6, policy=PORTFOLIO_DOUBLING)
        assert out.doubling_steps_completed >= 1

    def test_unknown_policy_raises(self):
        with pytest.raises(ValueError):
            run_portfolio([0.1], policy="martingale")
