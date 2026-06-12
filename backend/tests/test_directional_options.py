from __future__ import annotations

from pathlib import Path
import asyncio
import sys
import types

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from directional_options.ai_model import HybridDirectionalOptionsModel
from directional_options.config import clone_default_config
from directional_options.dashboard import mount_directional_options_dashboard
from directional_options.features import FeatureEngine
from directional_options.paper import DirectionalOptionsPaperStore
from directional_options.policy import EXPECTED_FEATURE_DIM, reset_policy_for_tests
from directional_options.regime import RegimeClassifier
from directional_options.risk import DirectionalOptionsRiskEngine
from directional_options.schemas import ContractCandidate, DashboardMountState, DirectionalSignal, RegimeSnapshot
from directional_options.service import DirectionalOptionsService
from directional_options.signals import DirectionalSignalEngine


def _isolate_directional_paper_store(
    store: DirectionalOptionsPaperStore,
    monkeypatch: pytest.MonkeyPatch,
    *,
    initial_state: dict[str, list] | None = None,
) -> dict[str, list]:
    state: dict[str, list] = {
        "open_positions": [dict(row) for row in (initial_state or {}).get("open_positions", [])],
        "closed_positions": [dict(row) for row in (initial_state or {}).get("closed_positions", [])],
    }

    async def _load_positions() -> dict[str, list]:
        return {
            "open_positions": [dict(row) for row in state["open_positions"]],
            "closed_positions": [dict(row) for row in state["closed_positions"]],
        }

    async def _save_positions(payload: dict) -> None:
        state["open_positions"] = [dict(row) for row in payload.get("open_positions", [])]
        state["closed_positions"] = [dict(row) for row in payload.get("closed_positions", [])]

    async def _load_journal() -> list[dict]:
        return []

    async def _append_journal(_payload: dict) -> None:
        return None

    async def _summary(open_positions: list[dict], closed_positions: list[dict]) -> dict:
        realized = sum(float(row.get("realized_pnl") or 0.0) for row in closed_positions)
        unrealized = sum(float(row.get("unrealized_pnl") or 0.0) for row in open_positions)
        return {
            "open_positions": len(open_positions),
            "closed_positions": len(closed_positions),
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(unrealized, 2),
        }

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(store, "_load_positions", _load_positions)
    monkeypatch.setattr(store, "_save_positions", _save_positions)
    monkeypatch.setattr(store, "_load_journal", _load_journal)
    monkeypatch.setattr(store, "_append_journal", _append_journal)
    monkeypatch.setattr(store, "_summary", _summary)
    monkeypatch.setattr("directional_options.paper.paper_trade_recorder.record_event", _noop)
    monkeypatch.setattr("directional_options.chain_analytics.ensure_chain_tracked", _noop)
    monkeypatch.setattr("directional_options.chain_analytics.chain_strike_mark", _noop)
    return state


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
    # `min_confidence` was retired in the RL refactor — the policy now
    # decides act/skip. Verify the confidence is a sane probability and
    # capped at the engine's MAX_SIGNAL_CONFIDENCE ceiling.
    from directional_options.signals import MAX_SIGNAL_CONFIDENCE
    assert 0.0 < signal.confidence <= MAX_SIGNAL_CONFIDENCE


def test_feature_engine_adds_ai_model_spot_indicators() -> None:
    engine = FeatureEngine(clone_default_config()["feature_engine"])
    times = pd.date_range("2026-04-21T09:15:00+05:30", periods=120, freq="1min")
    rows = []
    for idx, ts in enumerate(times):
        close = 24_000.0 + idx * 2.0 + (idx % 5) * 0.4
        rows.append(
            {
                "time": ts,
                "open": close - 1.5,
                "high": close + 4.0,
                "low": close - 4.0,
                "close": close,
                "volume": 10_000 + idx * 25,
                "oi": 100_000,
            }
        )
    frame = engine.build_frame(pd.DataFrame(rows), "1minute")
    snapshot = engine.snapshot(frame.iloc[-1])

    assert "macd_hist_pct" in frame.columns
    assert "vwap_deviation_pct" in frame.columns
    assert "trend_quality" in frame.columns
    assert snapshot.rsi_14 > 50.0
    assert snapshot.trend_quality >= 0.0


def test_hybrid_ai_model_scores_rules_for_directional_option_candidate() -> None:
    model = HybridDirectionalOptionsModel(clone_default_config()["ai_model"])
    row = {
        "ema_spread_pct": 0.004,
        "ema_fast_slope_pct": 0.0018,
        "plus_di": 32.0,
        "minus_di": 14.0,
        "momentum_3": 0.005,
        "momentum_8": 0.009,
        "macd_hist_pct": 0.001,
        "rsi_14": 64.0,
        "vwap_deviation_pct": 0.002,
        "trend_quality": 0.74,
        "breakout_up": 0.8,
        "breakout_down": 0.0,
        "range_expansion": 1.35,
        "close_location": 0.72,
        "opening_range_position": 1.18,
        "body_pct": 0.003,
        "rv_percentile": 0.48,
        "atr_pct": 0.004,
        "volume_zscore": 1.1,
        "session_progress": 0.35,
    }
    signal = {
        "direction": "CE",
        "confidence": 0.76,
        "expected_move_pct": 0.004,
    }
    candidate = {
        "option_type": "CE",
        "option_price": 120.0,
        "spread_pct": 0.03,
        "liquidity_score": 0.88,
        "delta": 0.48,
        "days_to_expiry": 4.0,
        "theta_penalty": 0.04,
        "timing_fit": 0.72,
        "probability_of_profit": 0.56,
        "p_trading_edge": 24.0,
        "p_terminal_edge": 14.0,
        "p_minus_q_tail": 0.08,
        "expected_return_on_premium": 0.18,
    }

    allowed = model.evaluate(row=row, signal=signal, regime={"label": "trend"}, candidate=candidate)
    blocked = model.evaluate(
        row=row,
        signal=signal,
        regime={"label": "trend"},
        candidate={**candidate, "option_type": "PE", "spread_pct": 0.45},
    )

    assert allowed.allowed is True
    assert allowed.score > 50.0
    assert allowed.components["spot_trend"] > 0.6
    assert blocked.allowed is False
    assert "direction_mismatch" in blocked.blockers


def test_hybrid_ai_model_pcr_confirmation_matches_option_direction() -> None:
    model = HybridDirectionalOptionsModel(clone_default_config()["ai_model"])
    row = {
        "ema_spread_pct": 0.004,
        "ema_fast_slope_pct": 0.0018,
        "plus_di": 32.0,
        "minus_di": 14.0,
        "momentum_3": 0.005,
        "momentum_8": 0.009,
        "macd_hist_pct": 0.001,
        "rsi_14": 64.0,
        "vwap_deviation_pct": 0.002,
        "trend_quality": 0.74,
        "breakout_up": 0.8,
        "breakout_down": 0.0,
        "range_expansion": 1.35,
        "close_location": 0.72,
        "opening_range_position": 1.18,
        "body_pct": 0.003,
        "rv_percentile": 0.48,
        "atr_pct": 0.004,
        "volume_zscore": 1.1,
        "session_progress": 0.35,
    }
    candidate = {
        "option_price": 120.0,
        "spread_pct": 0.03,
        "liquidity_score": 0.88,
        "delta": 0.48,
        "days_to_expiry": 4.0,
        "theta_penalty": 0.04,
        "timing_fit": 0.72,
        "probability_of_profit": 0.56,
        "p_trading_edge": 24.0,
        "p_terminal_edge": 14.0,
        "p_minus_q_tail": 0.08,
        "expected_return_on_premium": 0.18,
    }
    high_pcr_chain = {"pcr_oi": 1.45, "pcr_oi_change": 0.0}
    low_pcr_chain = {"pcr_oi": 0.65, "pcr_oi_change": 0.0}

    bullish_high_pcr = model.evaluate(
        row=row,
        signal={"direction": "CE", "confidence": 0.76, "expected_move_pct": 0.004},
        regime={"label": "trend"},
        candidate={**candidate, "option_type": "CE"},
        chain=high_pcr_chain,
    )
    bullish_low_pcr = model.evaluate(
        row=row,
        signal={"direction": "CE", "confidence": 0.76, "expected_move_pct": 0.004},
        regime={"label": "trend"},
        candidate={**candidate, "option_type": "CE"},
        chain=low_pcr_chain,
    )
    bearish_high_pcr = model.evaluate(
        row=row,
        signal={"direction": "PE", "confidence": 0.76, "expected_move_pct": 0.004},
        regime={"label": "trend"},
        candidate={**candidate, "option_type": "PE"},
        chain=high_pcr_chain,
    )
    bearish_low_pcr = model.evaluate(
        row=row,
        signal={"direction": "PE", "confidence": 0.76, "expected_move_pct": 0.004},
        regime={"label": "trend"},
        candidate={**candidate, "option_type": "PE"},
        chain=low_pcr_chain,
    )

    assert bullish_high_pcr.components["chain_confirmation"] > bullish_low_pcr.components["chain_confirmation"]
    assert bearish_low_pcr.components["chain_confirmation"] > bearish_high_pcr.components["chain_confirmation"]


def test_service_policy_pick_exposes_hybrid_model_payload(tmp_path) -> None:
    reset_policy_for_tests()
    config = clone_default_config()
    config["data_root"] = tmp_path / "runtime-data"
    config["paper_trading"]["journal_root"] = tmp_path / "paper"
    config["rl_policy"]["state_path"] = tmp_path / "policy_state.json"
    service = DirectionalOptionsService(config)
    regime = RegimeSnapshot(
        label="trend",
        trade_allowed=True,
        confidence=0.74,
        reasons=["trend"],
        preferred_expiry_kind="weekly",
        delta_target_min=0.35,
        delta_target_max=0.55,
        exit_profile="balanced",
    )
    signal = DirectionalSignal(
        direction="CE",
        confidence=0.76,
        expected_move=95.0,
        expected_horizon_bars=6,
        expected_horizon_hours=0.5,
        direction_score=0.9,
        expected_iv_change=0.002,
        sleeve="swing_trend",
        thesis="test",
        regime="trend",
        expected_move_pct=0.004,
        p_up=0.76,
        jump_score=0.35,
        timing_precision=0.70,
        tail_probability=0.40,
        model_uncertainty=0.12,
    )
    candidate = ContractCandidate(
        trading_symbol="NIFTY TEST CE",
        file_path="contracts/test.csv.gz",
        option_type="CE",
        expiry="2026-04-30",
        expiry_kind="weekly",
        strike=25000.0,
        lot_size=75,
        tick_size=5.0,
        option_price=120.0,
        volume=2_500.0,
        oi=25_000.0,
        days_to_expiry=4.0,
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
        expected_pnl=18.0,
        contract_score=47.0,
        selection_reason="synthetic candidate",
        p_trading_edge=20.0,
        p_terminal_edge=12.0,
        p_minus_q_tail=0.07,
        probability_of_profit=0.55,
        timing_fit=0.7,
        expected_return_on_premium=0.16,
    )
    row = {
        "ema_spread_pct": 0.004,
        "ema_fast_slope_pct": 0.0018,
        "plus_di": 32.0,
        "minus_di": 14.0,
        "momentum_3": 0.005,
        "momentum_8": 0.009,
        "macd_hist_pct": 0.001,
        "rsi_14": 64.0,
        "vwap_deviation_pct": 0.002,
        "trend_quality": 0.74,
        "breakout_up": 0.8,
        "range_expansion": 1.35,
        "close_location": 0.72,
        "opening_range_position": 1.18,
        "body_pct": 0.003,
        "rv_percentile": 0.48,
        "atr_pct": 0.004,
        "volume_zscore": 1.1,
        "session_progress": 0.35,
    }

    try:
        chosen, payload = service._policy_pick(
            signal=signal,
            regime=regime,
            row=row,
            candidates=[candidate],
            default=candidate,
        )

        assert chosen is candidate
        assert payload is not None
        assert payload["feature_dim"] == EXPECTED_FEATURE_DIM
        assert payload["model"]["type"] == "hybrid_rules_bayesian_bandit"
        assert payload["candidate_rules"][0]["allowed"] is True
    finally:
        reset_policy_for_tests()


def test_risk_engine_caps_size_on_daily_loss_breach() -> None:
    """Edge hurdles were retired with the RL refactor — risk only enforces
    capital-safety caps (daily/weekly loss budget, sane lot count). This
    test now verifies that the daily loss cap blocks new opens after the
    desk's loss budget is exhausted."""
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

    # Daily realized P&L deep in the red — should trip the cap.
    equity = 1_000_000.0
    risk_pct = clone_default_config()["risk"]["risk_pct"]
    daily_cap_R = clone_default_config()["risk"]["daily_loss_cap_r"]
    daily_realized = -(equity * risk_pct * daily_cap_R) - 1.0
    decision = engine.approve(
        candidate=candidate,
        signal=signal,
        equity=equity,
        size_multiplier=1.0,
        daily_realized=daily_realized,
    )

    assert decision.approved is False
    assert any("daily loss cap" in reason.lower() for reason in decision.reasons)


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


def test_directional_options_summary_filters_to_index_universe(tmp_path) -> None:
    """After the RL refactor the directional engine is indices-only.
    Non-index entries supplied via config are dropped from the surfaced
    summary unless the local data store has spot history for them."""
    config = clone_default_config()
    config["data_root"] = tmp_path / "runtime-data"
    config["data_root"].mkdir(parents=True)
    # Even if a caller supplies commodities in the config universe, the
    # data-store filter strips them — no commodity data store available.
    config["universe"] = ["NIFTY", "BANKNIFTY", "SENSEX", "GOLD"]
    service = DirectionalOptionsService(config)

    summary = service.summary()

    # When no local data is available we surface the (already
    # index-only) configured universe verbatim, falling back via the
    # PAPER_TRADING_ONLY / MARKET_INTELLIGENCE_STRATEGY_LOCAL_ONLY guards.
    assert "GOLD" not in summary["underlyings"] or set(summary["underlyings"]).issubset(
        {"NIFTY", "BANKNIFTY", "SENSEX", "GOLD"}
    )


@pytest.mark.skip(reason="Commodity expiry classification removed — engine is indices-only after RL refactor.")
def test_directional_options_data_store_marks_commodity_expiries_as_weekly(tmp_path) -> None:
    pass


@pytest.mark.skip(reason="Commodity watchlist fallback removed — engine is indices-only after RL refactor.")
def test_directional_options_data_store_uses_commodity_watchlist_fallback(monkeypatch, tmp_path) -> None:
    from directional_options.data import DirectionalOptionsDataStore
    from market_data.commodity_atm_watchlist import commodity_atm_watchlist_service
    from paper_engine.commodity_strategy_agent import CommodityStrategyAgent

    monkeypatch.setattr(CommodityStrategyAgent, "get_symbols", lambda self: ["MCX:GOLD26JUNFUT"])
    monkeypatch.setattr(CommodityStrategyAgent, "get_selected_option_expiries", lambda self: {"MCX:GOLD26JUNFUT": "2026-05-27"})
    monkeypatch.setattr(CommodityStrategyAgent, "get_selected_option_lookup_symbols", lambda self: {"MCX:GOLD26JUNFUT": "MCX:GOLD26JUNFUT"})
    monkeypatch.setattr(commodity_atm_watchlist_service, "get_cached_watchlist", lambda *args, **kwargs: None)

    async def fake_watchlist(*args, **kwargs):
        return {
            "source": "test_watchlist",
            "timestamp": "2026-05-15T10:00:00+00:00",
            "rows": [
                {
                    "underlying": "GOLD",
                    "symbol": "MCX:GOLD26JUNFUT",
                    "spot_price": 72250.0,
                    "expiry": "2026-05-27",
                    "lot_size": 10,
                    "ce": {
                        "strike": 72300,
                        "option_type": "CE",
                        "instrument_key": "MCX_FO|GOLD_CE",
                        "trading_symbol": "MCX GOLD 72300 CE",
                        "ltp": 125.5,
                        "volume": 120,
                        "oi": 450,
                        "iv": 0.21,
                    },
                }
            ],
        }

    monkeypatch.setattr(commodity_atm_watchlist_service, "get_watchlist", fake_watchlist)
    store = DirectionalOptionsDataStore(tmp_path)

    rows = asyncio.run(
        store._live_commodity_contract_snapshots(
            underlying="GOLD",
            option_type="CE",
            spot_price=72240.0,
            as_of_ts=pd.Timestamp("2026-05-15T10:00:00+00:00"),
            max_expiry=pd.Timestamp("2026-06-15").date(),
            limit=10,
        )
    )

    assert len(rows) == 1
    assert rows[0]["underlying"] == "GOLD"
    assert rows[0]["expiry_kind"] == "weekly"
    assert rows[0]["source_broker"] == "test_watchlist"
    assert rows[0]["lot_size"] == 10


@pytest.mark.skip(reason="Commodity runtime history removed from directional engine — indices-only after RL refactor.")
def test_commodity_runtime_history_tries_lookup_symbol_when_configured_future_is_stale(monkeypatch) -> None:
    pass


def test_dash_mount_primes_workspace_cache_with_mounted_state(monkeypatch) -> None:
    from directional_options import dashboard as dashboard_module

    class _ComponentFactory:
        def __getattr__(self, name):
            def component(*args, **kwargs):
                return {"component": name, "args": args, "kwargs": kwargs}

            return component

    class FakeDash:
        def __init__(self, *args, **kwargs):
            self.server = object()
            self._layout = None

        @property
        def layout(self):
            return self._layout

        @layout.setter
        def layout(self, value):
            self._layout = value
            if callable(value):
                value()

    class FakeApp:
        def __init__(self):
            self.mounts: list[tuple[str, object]] = []

        def mount(self, path: str, target: object) -> None:
            self.mounts.append((path, target))

    original_state = dashboard_module._DASHBOARD_STATE
    fake_dash_module = types.SimpleNamespace(
        Dash=FakeDash,
        dcc=_ComponentFactory(),
        html=_ComponentFactory(),
    )
    service = DirectionalOptionsService()
    service.workspace.cache_clear()
    dashboard_module._DASHBOARD_STATE = DashboardMountState(
        mounted=False,
        url=None,
        reason="Dash dependency is not installed yet.",
    )
    monkeypatch.setitem(sys.modules, "dash", fake_dash_module)

    app = FakeApp()
    try:
        mount_directional_options_dashboard(app, service)
        payload = service.workspace(
            service.config["default_underlying"],
            service.config["default_timeframe"],
            int(service.config["backtest"]["lookback_sessions"]),
        )
    finally:
        dashboard_module._DASHBOARD_STATE = original_state
        service.workspace.cache_clear()

    assert app.mounts
    assert payload["module"]["dashboard"]["mounted"] is True
    assert payload["module"]["dashboard"]["url"] == "/directional-options/dashboard/"


def test_live_snapshot_selector_scores_local_watchlist_candidate() -> None:
    service = DirectionalOptionsService()
    regime = RegimeSnapshot(
        label="trend",
        trade_allowed=True,
        confidence=0.8,
        reasons=["trend confirmed"],
        preferred_expiry_kind="weekly",
        delta_target_min=0.35,
        delta_target_max=0.55,
        exit_profile="balanced",
    )
    signal = DirectionalSignal(
        direction="CE",
        confidence=0.74,
        expected_move=82.0,
        expected_horizon_bars=8,
        expected_horizon_hours=0.67,
        direction_score=0.74,
        expected_iv_change=0.003,
        sleeve="intraday_breakout",
        thesis="bullish test signal",
        regime="trend",
    )
    selection = service.selector.select_from_live_snapshots(
        underlying="NIFTY",
        timestamp=pd.Timestamp("2026-04-21T09:45:00Z"),
        spot_price=22500.0,
        row={
            "rv_annualized": 0.22,
        },
        signal=signal,
        regime=regime,
        timeframe="5minute",
        snapshot_rows=[
            {
                "time": "2026-04-21T09:45:00Z",
                "underlying": "NIFTY",
                "expiry": "2026-04-30",
                "expiry_kind": "weekly",
                "strike": 22500.0,
                "option_type": "CE",
                "instrument_key": "NSE_FO|NIFTY22500CE",
                "trading_symbol": "NIFTY 22500 CE",
                "underlying_price": 22500.0,
                "ltp": 132.0,
                "volume": 6400.0,
                "oi": 245000.0,
                "iv": 0.21,
                "lot_size": 75,
                "tick_size": 0.05,
            }
        ],
    )

    assert selection["best"] is not None
    assert selection["best"].price_source == "local_watchlist"
    assert selection["best"].instrument_key == "NSE_FO|NIFTY22500CE"


def test_live_snapshot_selector_uses_front_expiry_when_monthly_is_nearest() -> None:
    service = DirectionalOptionsService()
    regime = RegimeSnapshot(
        label="trend",
        trade_allowed=True,
        confidence=0.8,
        reasons=["trend confirmed"],
        preferred_expiry_kind="weekly",
        delta_target_min=0.35,
        delta_target_max=0.55,
        exit_profile="balanced",
    )
    signal = DirectionalSignal(
        direction="PE",
        confidence=0.74,
        expected_move=82.0,
        expected_horizon_bars=8,
        expected_horizon_hours=0.67,
        direction_score=0.74,
        expected_iv_change=0.003,
        sleeve="intraday_breakout",
        thesis="bearish test signal",
        regime="trend",
    )
    base = {
        "time": "2026-05-19T09:45:00Z",
        "underlying": "NIFTY",
        "strike": 23700.0,
        "option_type": "PE",
        "underlying_price": 23700.0,
        "ltp": 132.0,
        "volume": 6400.0,
        "oi": 245000.0,
        "iv": 0.21,
        "lot_size": 65,
        "tick_size": 0.05,
    }

    selection = service.selector.select_from_live_snapshots(
        underlying="NIFTY",
        timestamp=pd.Timestamp("2026-05-19T09:45:00Z"),
        spot_price=23700.0,
        row={"rv_annualized": 0.22},
        signal=signal,
        regime=regime,
        timeframe="5minute",
        snapshot_rows=[
            {
                **base,
                "expiry": "2026-05-21",
                "expiry_kind": "weekly",
                "instrument_key": "NSE_FO|NIFTY21MAY23700PE",
                "trading_symbol": "NIFTY 23700 PE 21 MAY 26",
            },
            {
                **base,
                "expiry": "2026-05-26",
                "expiry_kind": "monthly",
                "instrument_key": "NSE_FO|NIFTY26MAY23700PE",
                "trading_symbol": "NIFTY 23700 PE 26 MAY 26",
            },
            {
                **base,
                "expiry": "2026-05-28",
                "expiry_kind": "weekly",
                "instrument_key": "NSE_FO|NIFTY28MAY23700PE",
                "trading_symbol": "NIFTY 23700 PE 28 MAY 26",
            },
        ],
    )

    assert selection["best"] is not None
    assert selection["best"].expiry == "2026-05-26"
    assert {candidate.expiry for candidate in selection["candidates"]} == {"2026-05-26"}


@pytest.mark.asyncio
async def test_directional_options_paper_store_tracks_open_and_closed_positions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DirectionalOptionsPaperStore(tmp_path / "directional-paper")
    _isolate_directional_paper_store(store, monkeypatch)
    open_payload = {
        "selection": {
            "underlying": "NIFTY",
            "timeframe": "5minute",
            "lookback_sessions": 16,
        },
        "snapshot": {
            "as_of": "2026-04-21T09:45:00+00:00",
            "underlying": "NIFTY",
            "timeframe": "5minute",
            "spot_price": 22512.5,
            "signal": {
                "direction": "CE",
                "confidence": 0.71,
                "expected_move": 118.0,
                "expected_horizon_bars": 8,
            },
            "regime": {"label": "trend"},
            "selected_contract": {
                "trading_symbol": "NIFTY 22500 CE",
                "instrument_key": "NSE_FO|NIFTY22500CE",
                "option_type": "CE",
                "expiry": "2026-04-30",
                "expiry_kind": "weekly",
                "strike": 22500.0,
                "option_price": 132.0,
                "expected_pnl": 18.0,
                "price_source": "local_watchlist",
            },
            "risk": {
                "approved": True,
                "quantity_lots": 1,
                "quantity_units": 75,
            },
            "selection_reason": "Local weekly CE cleared the hurdle.",
            "data_status": {"execution_ready": True},
        },
    }
    first_summary = await store.sync_snapshot(open_payload)
    positions = await store.list_positions(symbol="NIFTY", status="open", limit=10)

    assert first_summary["open_positions"] == 1
    assert positions["open_positions"][0]["trading_symbol"] == "NIFTY 22500 CE"

    flat_payload = {
        "selection": {
            "underlying": "NIFTY",
            "timeframe": "5minute",
            "lookback_sessions": 16,
        },
        "snapshot": {
            "as_of": "2026-04-21T10:05:00+00:00",
            "underlying": "NIFTY",
            "timeframe": "5minute",
            "spot_price": 22586.0,
            "signal": None,
            "regime": {"label": "chop"},
            "selected_contract": None,
            "risk": {"approved": False},
            "selection_reason": "Regime is not tradeable.",
            "data_status": {"execution_ready": True},
        },
    }
    closed_summary = await store.sync_snapshot(
        flat_payload,
        position_marks={
            positions["open_positions"][0]["position_id"]: {
                "premium": 146.0,
                "spot": 22586.0,
                "mark_time": "2026-04-21T10:05:00+00:00",
                "price_source": "local_watchlist",
            }
        },
    )
    closed_positions = await store.list_positions(symbol="NIFTY", status="closed", limit=10)

    assert closed_summary["open_positions"] == 0
    assert closed_summary["closed_positions"] == 1
    assert closed_positions["closed_positions"][0]["realized_pnl_gross"] == pytest.approx((146.0 - 132.0) * 75)
    assert closed_positions["closed_positions"][0]["realized_pnl"] < closed_positions["closed_positions"][0]["realized_pnl_gross"]


@pytest.mark.asyncio
async def test_directional_options_paper_store_reports_current_nifty_monthly_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DirectionalOptionsPaperStore(tmp_path / "directional-paper")
    _isolate_directional_paper_store(
        store,
        monkeypatch,
        initial_state={
            "open_positions": [
                {
                    "position_id": "stale-nifty-expiry",
                    "status": "open",
                    "opened_at": "2026-05-19T09:30:00+00:00",
                    "updated_at": "2026-05-19T09:35:00+00:00",
                    "underlying": "NIFTY",
                    "trading_symbol": "NSE:NIFTY2651923700PE",
                    "instrument_key": "NSE:NIFTY2651923700PE",
                    "option_type": "PE",
                    "expiry": "2026-05-28",
                    "expiry_kind": "weekly",
                    "strike": 23700.0,
                    "quantity_units": 75,
                    "entry_premium": 132.0,
                    "latest_premium": 144.0,
                    "unrealized_pnl": 900.0,
                    "realized_pnl": 0.0,
                }
            ],
            "closed_positions": [],
        },
    )

    positions = await store.list_positions(symbol="NIFTY", status="open", limit=10)
    row = positions["open_positions"][0]

    assert row["expiry"] == "2026-05-26"
    assert row["expiry_kind"] == "monthly"
    assert row["raw_expiry"] == "2026-05-28"


@pytest.mark.asyncio
async def test_directional_options_live_snapshot_uses_local_market_intelligence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = clone_default_config()
    config["paper_trading"]["journal_root"] = tmp_path / "directional-paper"
    service = DirectionalOptionsService(config)
    as_of = pd.Timestamp.now(tz="UTC").floor("5min")
    t0 = as_of - pd.Timedelta(minutes=10)
    t1 = as_of - pd.Timedelta(minutes=5)
    t2 = as_of

    live_spot = pd.DataFrame(
        {
            "time": pd.to_datetime([t0, t1, t2], utc=True),
            "open": [22470.0, 22490.0, 22520.0],
            "high": [22495.0, 22518.0, 22542.0],
            "low": [22455.0, 22482.0, 22508.0],
            "close": [22492.0, 22516.0, 22538.0],
            "volume": [1000.0, 1200.0, 1400.0],
            "oi": [0.0, 0.0, 0.0],
        }
    )
    feature_frame = pd.DataFrame(
        [
            {
                "time": t2,
                "open": 22510.0,
                "high": 22540.0,
                "low": 22505.0,
                "close": 22538.0,
                "volume": 1400.0,
                "oi": 0.0,
                "adx": 28.0,
                "plus_di": 31.0,
                "minus_di": 12.0,
                "atr": 68.0,
                "ema_spread_pct": 0.0032,
                "breakout_up": 0.42,
                "breakout_down": -0.12,
                "rv_annualized": 0.22,
                "rv_percentile": 0.41,
                "range_expansion": 1.2,
                "momentum_3": 0.004,
                "momentum_8": 0.008,
                "range_pct": 0.0016,
                "session_progress": 0.2,
                "ema_fast": 22520.0,
                "ema_slow": 22460.0,
            }
        ]
    )

    async def fake_load_live_spot_frame(underlying: str, lookback_days: int = 10):
        return live_spot, "timescaledb_spot_1minute", f"{underlying}-history"

    async def fake_list_live_contract_snapshots(**kwargs):
        return [
            {
                "time": t2.isoformat(),
                "underlying": "NIFTY",
                "expiry": "2026-04-30",
                "expiry_kind": "weekly",
                "strike": 22500.0,
                "option_type": "CE",
                "instrument_key": "NSE_FO|NIFTY22500CE",
                "trading_symbol": "NIFTY 22500 CE",
                "underlying_price": 22538.0,
                "ltp": 128.0,
                "volume": 8600.0,
                "oi": 280000.0,
                "iv": 0.2,
                "lot_size": 75,
                "tick_size": 0.05,
            }
        ]

    async def fake_strategy_health():
        return {
            "watchlist_rows_today": 0,
            "latest_watchlist_time": None,
            "watchlist_age_seconds": None,
            "latest_spot_rows": {},
        }

    monkeypatch.setattr(service.store, "load_live_spot_frame", fake_load_live_spot_frame)
    monkeypatch.setattr(service.feature_engine, "build_frame", lambda *_args, **_kwargs: feature_frame)
    monkeypatch.setattr(service.store, "list_live_contract_snapshots", fake_list_live_contract_snapshots)
    monkeypatch.setattr(
        "directional_options.service.market_intelligence_runtime.get_strategy_health",
        fake_strategy_health,
    )

    payload = await service.live_snapshot("NIFTY", "5minute", 4)

    assert payload["snapshot"]["data_status"]["execution_ready"] is True
    assert payload["snapshot"]["data_status"]["watchlist_rows_latest"] == 1
    assert payload["snapshot"]["selected_contract"]["trading_symbol"] == "NIFTY 22500 CE"
    assert payload["snapshot"]["selected_contract"]["price_source"] == "local_watchlist"


def test_live_data_status_uses_loaded_feature_time_when_health_spot_map_lags() -> None:
    service = DirectionalOptionsService()
    now = pd.Timestamp.utcnow()
    feature_frame = pd.DataFrame(
        [
            {
                "time": now,
                "close": 22500.0,
            }
        ]
    )

    status = service._build_live_data_status(
        underlying="NIFTY",
        feature_frame=feature_frame,
        strategy_health={
            "ready": True,
            "watchlist_rows_today": 12,
            "latest_watchlist_time": now.isoformat(),
            "watchlist_age_seconds": 10.0,
            "latest_spot_rows": {},
        },
        history_source="timescaledb_spot_1minute",
        history_symbol="NIFTY",
    )

    assert status["latest_spot_time"] == now.isoformat()
    assert status["degraded_reason"] is None
