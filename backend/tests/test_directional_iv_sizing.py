"""IV-sizing regression tests for the directional lane (2026-07-17).

OWNER DIRECTIVE: "position has to be sized as per IV it cannot prevent a
trade." The former positional vol gate (vol_ok = d_atm_iv >= 0, a hard veto
that blocked falling-IV and NULL-IV days) is replaced by a monotone sizing
factor in [IV_SIZING_FLOOR, 1.0] applied to the BASE risk budget.

Covers:
  * predict() positional path: rising IV ENTERS with factor < 1, NULL
    d_atm_iv ENTERS at the conservative neutral (both previously vetoed
    outcomes are now sized entries), falling IV enters at full size;
  * gates that must KEEP blocking: positioning confirmation, stale feed,
    missing index feed row (data-honesty, not IV);
  * compute_iv_sizing_factor curve: monotone in both inputs, bounded,
    level-percentile (primary) and rising-trend (secondary) components;
  * risk.approve(): BASE budget scales by the factor, loss caps stay
    denominated in the UN-conditioned unit budget, IV shrink alone can
    never produce a 0-lot de-facto veto (1-lot minimum), while a genuinely
    too-small budget still rejects;
  * paper store: register_open receives base x iv_factor (de-scaled by the
    policy multiplier ONLY) and the journal surfaces iv_sizing_factor.
"""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from directional_options.config import clone_default_config
from directional_options.risk import DirectionalOptionsRiskEngine
from directional_options.schemas import ContractCandidate, DirectionalSignal, RegimeSnapshot
from directional_options.signals import (
    IV_SIZING_FLOOR,
    IV_SIZING_NEUTRAL,
    DirectionalSignalEngine,
    compute_iv_sizing_factor,
)


# ── shared fixtures ──────────────────────────────────────────────────────────

TREND_ROW = {
    "ema_spread_pct": 0.0032,
    "breakout_up": 0.4,
    "breakout_down": -0.2,
    "plus_di": 31.0,
    "minus_di": 16.0,
    "momentum_3": 0.004,
    "momentum_8": 0.009,
    "atr": 22.0,
    "close": 1502.0,
    "range_expansion": 1.3,
    "rv_percentile": 0.42,
}

TREND_REGIME = RegimeSnapshot(
    label="trend",
    trade_allowed=True,
    confidence=0.72,
    reasons=["trend confirmed"],
    preferred_expiry_kind="weekly",
    delta_target_min=0.35,
    delta_target_max=0.55,
    exit_profile="balanced",
)


def _positioning(**overrides) -> dict:
    base = {
        "is_stale": False,
        "htf_up": True,
        "oi_build_bias": 0.5,
        "pcr_oi": 0.8,
        "d_atm_iv": 0.0,
        "atm_iv_pctile": None,
    }
    base.update(overrides)
    return base


def _engine() -> DirectionalSignalEngine:
    return DirectionalSignalEngine(clone_default_config()["signal_engine"])


def _predict(engine, positioning, underlying="BANKNIFTY"):
    return engine.predict(
        TREND_ROW, TREND_REGIME, "3minute", positioning=positioning, underlying=underlying
    )


def _signal(iv_factor: float = 1.0) -> DirectionalSignal:
    return DirectionalSignal(
        direction="CE",
        confidence=0.6,
        expected_move=120.0,
        expected_horizon_bars=6,
        expected_horizon_hours=3.0,
        direction_score=0.5,
        expected_iv_change=0.0,
        sleeve="swing_trend",
        thesis="test",
        regime="trend",
        positional=True,
        iv_sizing_factor=iv_factor,
    )


def _candidate(option_price: float = 100.0, lot_size: int = 50) -> ContractCandidate:
    return ContractCandidate(
        trading_symbol="NIFTY 22500 CE",
        file_path="",
        option_type="CE",
        expiry="2026-08-27",
        expiry_kind="monthly",
        strike=22500.0,
        lot_size=lot_size,
        tick_size=0.05,
        option_price=option_price,
        volume=10_000.0,
        oi=500_000.0,
        days_to_expiry=30.0,
        moneyness_pct=0.0,
        implied_vol=0.12,
        delta=0.5,
        gamma=0.001,
        theta=-4.0,
        vega=12.0,
        delta_bucket="core",
        liquidity_score=0.8,
        iv_value_score=0.5,
        theta_penalty=0.02,
        spread_pct=0.05,
        slippage_pct=0.014,
        spread_cost=5.0,
        slippage_cost=1.4,
        fees=0.9,
        expected_pnl=25.0,
        contract_score=0.7,
        selection_reason="test",
    )


def _risk_engine(premium_cap_pct=None) -> DirectionalOptionsRiskEngine:
    # Explicit config so these tests pin the IV-sizing CONTRACT independently
    # of default-config churn.
    return DirectionalOptionsRiskEngine(
        {
            "risk_pct": 0.005,
            "premium_cap_pct": premium_cap_pct,
            "planned_stop_pct": 0.30,
            "daily_loss_cap_r": 4.0,
            "weekly_loss_cap_r": 10.0,
        }
    )


# With option_price 100, lot_size 50, planned_stop_pct 0.30, fee 0.45:
# lot_risk = (30.0 + 0.45) * 50 = 1522.5. Equity 1_000_000 at risk_pct 0.005
# gives an UN-conditioned unit budget of 5_000 -> 3 lots at 1.0x.
_EQUITY = 1_000_000.0
_LOT_RISK = 1_522.5


# ── signals.predict: IV never vetoes the positional entry ────────────────────


class TestPositionalIvNeverVetoes:
    @pytest.fixture(autouse=True)
    def _enable_positional(self, monkeypatch: pytest.MonkeyPatch):
        from core.config import settings as _settings

        monkeypatch.setattr(_settings, "DIRECTIONAL_POSITIONAL_OPTIONS_ENABLED", True, raising=False)

    def test_rising_iv_enters_with_reduced_factor(self) -> None:
        # +1.5 IV pts/day: linear shrink toward the floor, saturating at +3.
        signal = _predict(_engine(), _positioning(d_atm_iv=1.5))
        assert signal is not None, "IV state must never veto the entry"
        assert signal.positional is True
        assert IV_SIZING_FLOOR <= signal.iv_sizing_factor < 1.0
        assert signal.iv_sizing_factor == pytest.approx(
            1.0 - (1.0 - IV_SIZING_FLOOR) * 0.5, abs=1e-4
        )

    def test_null_d_atm_iv_enters_at_neutral(self) -> None:
        # Pre-directive this was a hard veto (vol_ok failed on NULL). Now the
        # feed's inability to compute an ATM IV trend means: trade, but small.
        signal = _predict(_engine(), _positioning(d_atm_iv=None))
        assert signal is not None
        assert signal.iv_sizing_factor == pytest.approx(IV_SIZING_NEUTRAL)

    def test_falling_iv_enters_at_full_size(self) -> None:
        # Pre-directive falling IV was ALSO vetoed (vol_ok required
        # d_atm_iv >= 0) despite research measuring falling/low IV as the
        # FAVORABLE long-premium state. Now it is the full-size case.
        signal = _predict(_engine(), _positioning(d_atm_iv=-1.2))
        assert signal is not None
        assert signal.iv_sizing_factor == pytest.approx(1.0)

    def test_high_iv_level_percentile_shrinks_size(self) -> None:
        top = _predict(_engine(), _positioning(d_atm_iv=0.0, atm_iv_pctile=1.0))
        assert top is not None
        assert top.iv_sizing_factor == pytest.approx(IV_SIZING_FLOOR)
        median = _predict(_engine(), _positioning(d_atm_iv=0.0, atm_iv_pctile=0.5))
        assert median is not None
        assert median.iv_sizing_factor == pytest.approx(1.0)

    def test_confirmation_gate_still_blocks(self) -> None:
        # Positioning CONFIRMATION is the researched edge, NOT an IV gate —
        # it must keep vetoing: uptrend with put-side OI build + mid PCR.
        unconfirmed = _positioning(oi_build_bias=-0.4, pcr_oi=1.05, d_atm_iv=-1.0)
        assert _predict(_engine(), unconfirmed) is None

    def test_stale_feed_still_blocks(self) -> None:
        assert _predict(_engine(), _positioning(is_stale=True, d_atm_iv=-1.0)) is None

    def test_missing_index_feed_row_still_blocks(self) -> None:
        assert _predict(_engine(), None, underlying="NIFTY") is None

    def test_stock_path_keeps_default_factor(self) -> None:
        signal = _predict(_engine(), None, underlying="RELIANCE")
        assert signal is not None
        assert signal.positional is False
        assert signal.iv_sizing_factor == pytest.approx(1.0)


# ── curve shape ──────────────────────────────────────────────────────────────


class TestIvSizingCurve:
    def test_monotone_non_increasing_in_d_atm_iv(self) -> None:
        factors = [
            compute_iv_sizing_factor(d, 0.8)
            for d in (-2.0, -0.5, 0.0, 0.3, 0.8, 1.5, 2.4, 3.0, 5.0)
        ]
        assert all(a >= b for a, b in zip(factors, factors[1:]))
        assert all(IV_SIZING_FLOOR <= f <= 1.0 for f in factors)

    def test_monotone_non_increasing_in_level_percentile(self) -> None:
        factors = [
            compute_iv_sizing_factor(0.3, p)
            for p in (None, 0.0, 0.25, 0.5, 0.6, 0.8, 1.0)
        ]
        assert all(a >= b for a, b in zip(factors, factors[1:]))
        assert all(IV_SIZING_FLOOR <= f <= 1.0 for f in factors)

    def test_floor_clamps_combined_components(self) -> None:
        # Worst trend x worst level would multiply to floor^2 — the clamp
        # keeps the contract's [floor, 1.0] range.
        assert compute_iv_sizing_factor(10.0, 1.0) == pytest.approx(IV_SIZING_FLOOR)

    def test_favorable_state_is_exactly_full(self) -> None:
        assert compute_iv_sizing_factor(-0.7, 0.2) == pytest.approx(1.0)
        assert compute_iv_sizing_factor(0.0, None) == pytest.approx(1.0)

    def test_null_trend_is_neutral(self) -> None:
        assert compute_iv_sizing_factor(None) == pytest.approx(IV_SIZING_NEUTRAL)
        assert compute_iv_sizing_factor(None, 0.9) == pytest.approx(IV_SIZING_NEUTRAL)


# ── risk.approve: factor scales the BASE budget, never rejects ───────────────


class TestRiskBudgetIvScaling:
    def test_risk_budget_scales_by_iv_factor(self) -> None:
        engine = _risk_engine()
        full = engine.approve(candidate=_candidate(), signal=_signal(1.0), equity=_EQUITY)
        half = engine.approve(candidate=_candidate(), signal=_signal(0.5), equity=_EQUITY)
        assert full.approved and half.approved
        assert full.risk_budget == pytest.approx(5_000.0)
        assert half.risk_budget == pytest.approx(2_500.0)
        assert full.quantity_lots == 3  # floor(5000 / 1522.5)
        assert half.quantity_lots == 1  # floor(2500 / 1522.5)

    def test_reported_budget_is_base_times_iv_times_multiplier(self) -> None:
        decision = _risk_engine().approve(
            candidate=_candidate(), signal=_signal(0.5), equity=_EQUITY, size_multiplier=2.0
        )
        # unit 5_000 x iv 0.5 x mult 2.0 — de-scaling by the multiplier ONLY
        # (paper.register_open) must recover base x iv = 2_500.
        assert decision.risk_budget == pytest.approx(5_000.0)
        assert decision.risk_budget / 2.0 == pytest.approx(2_500.0)

    def test_iv_shrink_alone_cannot_zero_the_lots(self) -> None:
        # Floor factor 0.25 -> budget 1_250 -> floor(1250/1522.5) = 0, but the
        # UN-conditioned budget affords 3 lots: the 1-lot minimum must fire
        # (the sizing floor must never become a de-facto veto).
        decision = _risk_engine(premium_cap_pct=0.05).approve(
            candidate=_candidate(), signal=_signal(IV_SIZING_FLOOR), equity=_EQUITY
        )
        assert decision.approved
        assert decision.quantity_lots == 1

    def test_genuinely_unaffordable_contract_still_rejects(self) -> None:
        # Unit budget 1_000 (< one lot_risk 1522.5) — a 0-lot outcome that
        # exists even at iv_factor 1.0 keeps rejecting; that is a capital
        # constraint, not an IV veto.
        decision = _risk_engine().approve(
            candidate=_candidate(), signal=_signal(0.5), equity=200_000.0
        )
        assert not decision.approved
        assert decision.quantity_lots == 0
        assert any("0 lots" in reason for reason in decision.reasons)

    def test_loss_caps_stay_denominated_in_unconditioned_unit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.config import settings as _settings

        monkeypatch.setattr(_settings, "SIGNAL_VALIDATION_UNCAPPED", False, raising=False)
        engine = _risk_engine()
        # Daily cap = 4R of the UNIT budget (5_000) = 20_000, for every IV
        # state — the IV factor must not tighten (nor loosen) the cap.
        for factor in (1.0, 0.5, IV_SIZING_FLOOR):
            blocked = engine.approve(
                candidate=_candidate(), signal=_signal(factor), equity=_EQUITY,
                daily_realized=-20_000.0,
            )
            assert not blocked.approved
            assert any("Daily loss cap" in r for r in blocked.reasons)
            open_ok = engine.approve(
                candidate=_candidate(), signal=_signal(factor), equity=_EQUITY,
                daily_realized=-19_999.0,
            )
            assert open_ok.approved


# ── paper store: R denominator + journal surfacing ───────────────────────────


class TestPaperStoreIvFlow:
    @pytest.mark.asyncio
    async def test_register_open_gets_base_times_iv_and_journal_surfaces_factor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from directional_options.paper import DirectionalOptionsPaperStore

        captured: dict[str, float] = {}
        journal: list[dict] = []

        class _CapturePolicy:
            def register_open(self, **kwargs):
                captured["risk_budget"] = float(kwargs.get("risk_budget"))
                captured["size_multiplier"] = float(kwargs.get("size_multiplier"))

            def record_close(self, **kwargs):
                return None

        store = DirectionalOptionsPaperStore(
            tmp_path / "paper", policy=_CapturePolicy()  # type: ignore[arg-type]
        )

        state = {"open_positions": [], "closed_positions": []}

        async def _load_positions():
            return {
                "open_positions": list(state["open_positions"]),
                "closed_positions": list(state["closed_positions"]),
            }

        async def _save_positions(payload):
            state["open_positions"] = list(payload.get("open_positions", []))
            state["closed_positions"] = list(payload.get("closed_positions", []))

        async def _append_journal(payload):
            journal.append(dict(payload))

        async def _summary(open_positions, closed_positions):
            return {"open_positions": len(open_positions), "closed_positions": len(closed_positions)}

        async def _noop(*_args, **_kwargs):
            return None

        monkeypatch.setattr(store, "_load_positions", _load_positions)
        monkeypatch.setattr(store, "_save_positions", _save_positions)
        monkeypatch.setattr(store, "_append_journal", _append_journal)
        monkeypatch.setattr(store, "_summary", _summary)
        monkeypatch.setattr("directional_options.paper.paper_trade_recorder.record_event", _noop)

        payload = {
            "selection": {"underlying": "BANKNIFTY", "timeframe": "30minute", "lookback_sessions": 16},
            "snapshot": {
                "as_of": "2026-07-17T04:45:00+00:00",
                "underlying": "BANKNIFTY",
                "timeframe": "30minute",
                "spot_price": 51_200.0,
                "signal": {
                    "direction": "CE",
                    "confidence": 0.60,
                    "expected_move": 320.0,
                    "expected_horizon_bars": 6,
                    "positional": True,
                    # Adverse IV day: half-size entry instead of the old veto.
                    "iv_sizing_factor": 0.5,
                },
                "regime": {"label": "trend"},
                "selected_contract": {
                    "trading_symbol": "BANKNIFTY 51200 CE",
                    "instrument_key": "NSE_FO|BANKNIFTY51200CE",
                    "option_type": "CE",
                    "expiry": "2026-08-27",
                    "expiry_kind": "monthly",
                    "strike": 51_200.0,
                    "option_price": 620.0,
                    "expected_pnl": 40.0,
                    "price_source": "local_watchlist",
                },
                # risk.approve() reports unit 5_000 x iv 0.5 x mult 2.0.
                "risk": {"approved": True, "quantity_lots": 1, "quantity_units": 35, "risk_budget": 5_000.0},
                "policy": {"size_multiplier": 2.0, "act": True},
                "selection_reason": "iv-sized positional entry",
                "data_status": {"execution_ready": True},
            },
        }

        await store.sync_snapshot(payload)

        # De-scaled by the policy multiplier ONLY: base x iv = 2_500 stays the
        # R denominator, so rewards remain R-per-intended-risk INCLUDING the
        # IV conditioning.
        assert captured["risk_budget"] == pytest.approx(2_500.0)
        assert captured["size_multiplier"] == pytest.approx(2.0)

        # Journal surfaces WHY the position was small.
        assert journal, "sync_snapshot must journal the proposal"
        assert journal[0]["iv_sizing_factor"] == pytest.approx(0.5)
