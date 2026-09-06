from datetime import datetime, timezone

import numpy as np

from model.nonlinear_selector import (
    FEATURE_NAMES, QuantileMLP, artifact_sha256, feature_row,
)


def _instrument():
    return {
        "premium": 100.0, "spot": 1000.0, "strike": 1000.0,
        "open": 98.0, "high": 104.0, "low": 96.0, "volume": 5000,
        "oi": 10000, "iv": 0.22, "delta": 0.51, "gamma": 0.001,
        "theta": -2.0, "vega": 8.0, "dte_days": 7,
        "straddle_to_spot": 0.04, "normalized_straddle": 0.18,
        "strangle_straddle_ratio": 0.35, "put_wing_iv_ratio": 1.2,
        "call_wing_iv_ratio": 1.05, "atm_put_call_premium_ratio": 1.1,
        "atm_call_put_extrinsic_ratio": 0.9, "premium_pcr": 1.3,
        "call_itm_atm_extrinsic_ratio": 0.8,
        "call_otm_atm_extrinsic_ratio": 0.4,
        "put_itm_atm_extrinsic_ratio": 0.75,
        "put_otm_atm_extrinsic_ratio": 0.5, "ratio_n_strikes": 9,
    }


def _inputs():
    return {
        "flow_score": 40, "flow_age_sessions": 1, "flow_n_ingredients": 4,
        "rs_z20": -0.5, "rs_age_sessions": 1, "regime": "NEG",
        "gex_percentile": 0.2, "regime_age_bars": 0,
        "timing_state": "COMPRESSION", "timing_score": 55, "rvol": 1.8,
        "va_position": 0.4, "best_lag": 1, "leadlag_corr": 0.5,
        "ce_state": "long_buildup", "pe_state": "short_buildup",
    }


def test_both_sides_are_features_even_when_old_rule_legs_disagree():
    ts = datetime(2026, 8, 28, 8, 15, tzinfo=timezone.utc)
    ce = feature_row(_inputs(), _instrument(), "CE", ts)
    pe = feature_row(_inputs(), _instrument(), "PE", ts)
    assert ce.shape == pe.shape == (len(FEATURE_NAMES),)
    assert ce[FEATURE_NAMES.index("side_sign")] == 1
    assert pe[FEATURE_NAMES.index("side_sign")] == -1
    assert ce[FEATURE_NAMES.index("flow_aligned")] == -pe[FEATURE_NAMES.index("flow_aligned")]


def test_artifact_round_trip_and_economic_abstention_are_deterministic():
    n = len(FEATURE_NAMES)
    model = QuantileMLP(
        median=np.zeros(n), scale=np.ones(n),
        weights=[np.zeros((n * 2, 3))], biases=[np.asarray([-0.02, 0.03, 0.08])],
        selection_threshold=0.01, cost_pct=0.01, width_penalty=0.25,
    )
    artifact = model.to_artifact()
    restored = QuantileMLP.from_artifact(artifact)
    q = restored.predict(np.zeros((1, n)))
    # 3% median - 2.5% uncertainty penalty - 1% cost = -0.5%: abstain.
    assert np.isclose(restored.conservative_edge(q)[0], -0.005)
    assert artifact_sha256(artifact) == artifact_sha256(restored.to_artifact())


def test_missing_inputs_are_imputed_and_exposed_as_missing_flags():
    n = len(FEATURE_NAMES)
    model = QuantileMLP(
        median=np.zeros(n), scale=np.ones(n),
        weights=[np.zeros((n * 2, 3))], biases=[np.zeros(3)],
        selection_threshold=0, cost_pct=0.01,
    )
    prepared = model._prepare(np.full((1, n), np.nan))
    assert prepared.shape == (1, n * 2)
    assert np.all(prepared[0, n:] == 1.0)


def test_artifact_versioned_ood_and_prediction_clips_are_enforced():
    n = len(FEATURE_NAMES)
    model = QuantileMLP(
        median=np.zeros(n), scale=np.ones(n),
        weights=[np.ones((n * 2, 3))], biases=[np.zeros(3)],
        selection_threshold=0, cost_pct=0,
        standardized_clip=5.0, prediction_clip=(-0.15, 0.15),
    )
    prepared = model._prepare(np.full((1, n), 100.0))
    assert np.all(prepared[0, :n] == 5.0)
    restored = QuantileMLP.from_artifact(model.to_artifact())
    assert np.all(restored.predict(np.full((1, n), 100.0)) == 0.15)
