from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from directional_options.config import clone_default_config
from directional_options.regime import RegimeClassifier
from directional_options.risk import DirectionalOptionsRiskEngine
from directional_options.schemas import ContractCandidate, DirectionalSignal, RegimeSnapshot
from directional_options.service import DirectionalOptionsService
from directional_options.signals import DirectionalSignalEngine


def test_regime_classifier_marks_chop_as_no_trade() -> None:
    classifier = RegimeClassifier()

    regime = classifier.classify(
        {
            "adx": 11.5,
            "breakout_up": 0.05,
            "breakout_down": 0.02,
            "rv_percentile": 0.34,
            "range_expansion": 0.9,
            "ema_spread_pct": 0.0002,
        }
    )

    assert regime.label == "chop"
    assert regime.trade_allowed is False


def test_signal_engine_generates_bullish_signal_in_trend_regime() -> None:
    engine = DirectionalSignalEngine(clone_default_config()["signal_engine"])
    regime = RegimeSnapshot(
        label="trend",
        trade_allowed=True,
        confidence=0.78,
        reasons=["trend confirmed"],
        preferred_expiry_kind="weekly",
        delta_target_min=0.35,
        delta_target_max=0.55,
        exit_profile="balanced",
    )

    signal = engine.predict(
        {
            "ema_spread_pct": 0.0032,
            "breakout_up": 0.4,
            "breakout_down": -0.2,
            "plus_di": 31.0,
            "minus_di": 16.0,
            "momentum_3": 0.004,
            "momentum_8": 0.009,
            "atr": 72.0,
            "close": 24850.0,
            "range_expansion": 1.3,
            "rv_percentile": 0.42,
        },
        regime,
        "5minute",
    )

    assert signal is not None
    assert signal.direction == "CE"
    assert signal.expected_move > 0
    assert signal.confidence >= clone_default_config()["signal_engine"]["min_confidence"]


def test_risk_engine_rejects_candidate_when_edge_is_too_small() -> None:
    engine = DirectionalOptionsRiskEngine(clone_default_config()["risk"])
    candidate = ContractCandidate(
        trading_symbol="NIFTY TEST CE",
        file_path="contracts/test.csv.gz",
        option_type="CE",
        expiry="2025-08-28",
        expiry_kind="weekly",
        strike=25000.0,
        lot_size=75,
        tick_size=5.0,
        option_price=120.0,
        volume=2_500.0,
        oi=25_000.0,
        days_to_expiry=3.0,
        moneyness_pct=0.002,
        implied_vol=0.22,
        delta=0.48,
        gamma=0.0005,
        theta=-18.0,
        vega=10.0,
        delta_bucket="core",
        liquidity_score=0.92,
        iv_value_score=0.64,
        theta_penalty=0.02,
        spread_pct=0.03,
        slippage_pct=0.01,
        spread_cost=3.6,
        slippage_cost=1.2,
        fees=0.9,
        expected_pnl=5.0,
        contract_score=37.0,
        selection_reason="synthetic candidate",
    )
    signal = DirectionalSignal(
        direction="CE",
        confidence=0.74,
        expected_move=65.0,
        expected_horizon_bars=8,
        expected_horizon_hours=0.67,
        direction_score=0.7,
        expected_iv_change=0.004,
        sleeve="intraday_breakout",
        thesis="bullish test signal",
        regime="breakout",
    )

    decision = engine.approve(candidate=candidate, signal=signal, equity=1_000_000.0)

    assert decision.approved is False
    assert any("hurdle" in reason.lower() for reason in decision.reasons)


def test_directional_options_service_returns_workspace_payload() -> None:
    service = DirectionalOptionsService()

    payload = service.workspace("NIFTY", "5minute", 4)

    assert payload["selection"]["underlying"] == "NIFTY"
    assert payload["module"]["key"] == "directional_long_options"
    assert payload["snapshot"]["as_of"] is not None
    assert "summary" in payload["backtest"]


def test_directional_options_service_handles_missing_runtime_dataset(tmp_path) -> None:
    config = clone_default_config()
    config["data_root"] = tmp_path / "missing-runtime-data"
    service = DirectionalOptionsService(config)

    summary = service.summary()
    payload = service.workspace("NIFTY", "5minute", 4)

    assert summary["underlyings"] == []
    assert payload["snapshot"]["as_of"] is None
    assert payload["backtest"]["summary"]["trade_count"] == 0
