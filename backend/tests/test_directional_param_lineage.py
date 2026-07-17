"""Decision-parameter lineage regression tests for the directional lane.

Covers the 2026-07-17 lineage-audit fixes:
  * session_progress clock-basis heuristic (naive-UTC live frames vs
    naive-IST research frames) and the late-session blocker it unlocks.
  * Mixed-unit broker IV normalization in the live-snapshot selector.
  * RL reward denominator (base risk budget, multiplier counted once).
  * Fast-tape regime match no longer catching 30minute.
"""
from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from directional_options.ai_model import HybridDirectionalOptionsModel
from directional_options.config import clone_default_config
from directional_options.features import FeatureEngine, _times_on_ist_basis
from directional_options.policy import DirectionalPolicy
from directional_options.regime import RegimeClassifier
from directional_options.schemas import DirectionalSignal, RegimeSnapshot
from directional_options.selector import OptionSelectionEngine, _iv_as_fraction


def _minute_frame(start: str, end: str) -> pd.DataFrame:
    """Synthetic 1-minute OHLCV spot frame between two timestamps (inclusive)."""
    times = pd.date_range(start=start, end=end, freq="1min")
    n = len(times)
    rng = np.random.default_rng(7)
    closes = 22_500.0 + np.cumsum(rng.normal(0.0, 4.0, size=n))
    return pd.DataFrame(
        {
            "time": times,
            "open": closes - 1.0,
            "high": closes + 3.0,
            "low": closes - 3.0,
            "close": closes,
            "volume": np.full(n, 1_000.0),
            "oi": np.zeros(n),
        }
    )


def _session_minutes(day: str, *, basis: str) -> tuple[str, str]:
    if basis == "utc":
        return f"{day} 03:46:00", f"{day} 10:00:00"
    return f"{day} 09:16:00", f"{day} 15:30:00"


def _build_two_session_frame(basis: str) -> pd.DataFrame:
    parts = []
    for day in ("2026-07-15", "2026-07-16"):
        start, end = _session_minutes(day, basis=basis)
        parts.append(_minute_frame(start, end))
    return pd.concat(parts, ignore_index=True)


class TestSessionProgressClockBasis:
    def test_naive_utc_frame_gets_real_session_progress(self) -> None:
        engine = FeatureEngine(clone_default_config()["feature_engine"])
        frame = engine.build_frame(_build_two_session_frame("utc"), "3minute")
        # Last bar of the session (10:00 UTC == 15:30 IST) must read as
        # END of session, not 0 (the pre-fix behaviour).
        assert float(frame.iloc[-1]["session_progress"]) > 0.9
        # A mid-session bar (06:45 UTC == 12:15 IST => (735-555)/375 = 0.48)
        mid = frame.loc[frame["time"].dt.strftime("%H:%M") == "06:45"]
        assert not mid.empty
        assert float(mid.iloc[-1]["session_progress"]) == pytest.approx(0.48, abs=0.02)

    def test_ist_and_utc_bases_produce_identical_progress(self) -> None:
        engine = FeatureEngine(clone_default_config()["feature_engine"])
        utc_frame = engine.build_frame(_build_two_session_frame("utc"), "3minute")
        ist_frame = engine.build_frame(_build_two_session_frame("ist"), "3minute")
        assert len(utc_frame) == len(ist_frame)
        assert np.allclose(
            utc_frame["session_progress"].to_numpy(),
            ist_frame["session_progress"].to_numpy(),
        )
        # IST research frames keep their historical values (no shift).
        assert float(ist_frame.iloc[-1]["session_progress"]) > 0.9

    def test_basis_helper_detects_each_clock(self) -> None:
        utc_times = pd.Series(pd.to_datetime(["2026-07-16 03:46:00", "2026-07-16 09:59:00"]))
        ist_times = pd.Series(pd.to_datetime(["2026-07-16 09:16:00", "2026-07-16 15:29:00"]))
        shifted = _times_on_ist_basis(utc_times)
        assert shifted.iloc[0].hour == 9 and shifted.iloc[0].minute == 16
        unshifted = _times_on_ist_basis(ist_times)
        assert unshifted.iloc[-1].hour == 15


class TestLateSessionBlocker:
    def _evaluate(self, session_progress: float, days_to_expiry: float):
        model = HybridDirectionalOptionsModel(clone_default_config()["ai_model"])
        row = {
            "session_progress": session_progress,
            "ema_spread_pct": 0.002,
            "plus_di": 24.0,
            "minus_di": 12.0,
            "momentum_3": 0.001,
            "momentum_8": 0.002,
            "rv_percentile": 0.5,
            "atr_pct": 0.004,
            "range_expansion": 1.1,
        }
        candidate = {
            "option_type": "CE",
            "option_price": 130.0,
            "spread_pct": 0.06,
            "liquidity_score": 0.6,
            "delta": 0.52,
            "days_to_expiry": days_to_expiry,
            "theta_penalty": 0.01,
            "timing_fit": 0.6,
            "probability_of_profit": 0.5,
            "p_trading_edge": 10.0,
            "p_terminal_edge": 8.0,
            "p_minus_q_tail": 0.05,
            "expected_return_on_premium": 0.08,
        }
        signal = {"direction": "CE"}
        regime = {"label": "trend"}
        return model.evaluate(row=row, signal=signal, regime=regime, candidate=candidate)

    def test_expiry_day_late_session_is_blocked(self) -> None:
        # 15:10 IST (progress 0.947) holding a same-day weekly (DTE 0.5):
        # the blocker the naive-UTC session_progress silently disabled.
        evaluation = self._evaluate(0.947, 0.5)
        assert "late_session_expiry_risk" in evaluation.blockers

    def test_late_session_with_time_cushion_not_blocked(self) -> None:
        evaluation = self._evaluate(0.947, 5.0)
        assert "late_session_expiry_risk" not in evaluation.blockers

    def test_midday_expiry_day_not_blocked(self) -> None:
        evaluation = self._evaluate(0.45, 0.5)
        assert "late_session_expiry_risk" not in evaluation.blockers


class TestSnapshotIvNormalization:
    def test_iv_as_fraction_split_point(self) -> None:
        assert _iv_as_fraction(12.23) == pytest.approx(0.1223)
        assert _iv_as_fraction(0.144) == pytest.approx(0.144)
        assert _iv_as_fraction(0.62) == pytest.approx(0.62)

    def _select(self, iv_value: float):
        config = clone_default_config()
        selector = OptionSelectionEngine(store=None, config=config["selector"])
        regime = RegimeSnapshot(
            label="trend",
            trade_allowed=True,
            confidence=0.8,
            reasons=[],
            preferred_expiry_kind="weekly",
            delta_target_min=0.35,
            delta_target_max=0.55,
            exit_profile="balanced",
        )
        signal = DirectionalSignal(
            direction="CE",
            confidence=0.74,
            expected_move=82.0,
            expected_horizon_bars=6,
            expected_horizon_hours=0.3,
            direction_score=0.74,
            expected_iv_change=0.003,
            sleeve="intraday_breakout",
            thesis="test",
            regime="trend",
        )
        return selector.select_from_live_snapshots(
            underlying="NIFTY",
            timestamp=pd.Timestamp("2026-07-17T04:45:00Z"),
            spot_price=22500.0,
            row={"rv_annualized": 0.12},
            signal=signal,
            regime=regime,
            timeframe="3minute",
            snapshot_rows=[
                {
                    "time": "2026-07-17T04:45:00Z",
                    "underlying": "NIFTY",
                    "expiry": "2026-07-21",
                    "expiry_kind": "weekly",
                    "strike": 22500.0,
                    "option_type": "CE",
                    "instrument_key": "NSE_FO|NIFTY22500CE",
                    "trading_symbol": "NIFTY 22500 CE",
                    "underlying_price": 22500.0,
                    "ltp": 132.0,
                    "volume": 6400.0,
                    "oi": 245000.0,
                    "iv": iv_value,
                    "lot_size": 75,
                    "tick_size": 0.05,
                }
            ],
        )

    def test_percent_unit_broker_iv_is_normalized(self) -> None:
        # Upstox writes iv as PERCENT (12.5) — pre-fix this pegged sigma at
        # the 0.62 ceiling and priced every greek at 62% vol.
        selection = self._select(12.5)
        assert selection["best"] is not None
        assert selection["best"].implied_vol == pytest.approx(0.125, abs=1e-4)

    def test_fraction_unit_broker_iv_unchanged(self) -> None:
        selection = self._select(0.144)
        assert selection["best"] is not None
        assert selection["best"].implied_vol == pytest.approx(0.144, abs=1e-4)


class TestPolicyRewardDenominator:
    def test_record_close_divides_by_base_budget_times_multiplier_once(self, tmp_path: Path) -> None:
        policy = DirectionalPolicy(tmp_path / "policy_state.json", seed=3)
        signal = {"direction": "CE", "confidence": 0.6}
        candidate = {"option_price": 130.0, "delta": 0.5, "delta_bucket": "core", "expiry_kind": "weekly"}
        regime = {"label": "trend", "confidence": 0.6}
        # BASE budget 15_000 at 2.0x => position risk 30_000. A +30_000
        # realized PnL is exactly R = +1.0.
        policy.register_open(
            position_id="pos-1",
            signal=signal,
            candidate=candidate,
            regime=regime,
            size_multiplier=2.0,
            risk_budget=15_000.0,
        )
        r = policy.record_close(position_id="pos-1", realized_pnl=30_000.0)
        assert r == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_paper_store_descales_risk_budget_before_register_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from directional_options.paper import DirectionalOptionsPaperStore

        captured: dict[str, float] = {}

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

        async def _append_journal(_payload):
            return None

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
            "selection": {"underlying": "NIFTY", "timeframe": "3minute", "lookback_sessions": 16},
            "snapshot": {
                "as_of": "2026-07-17T04:45:00+00:00",
                "underlying": "NIFTY",
                "timeframe": "3minute",
                "spot_price": 22512.5,
                "signal": {"direction": "CE", "confidence": 0.71, "expected_move": 118.0, "expected_horizon_bars": 6},
                "regime": {"label": "trend"},
                "selected_contract": {
                    "trading_symbol": "NIFTY 22500 CE",
                    "instrument_key": "NSE_FO|NIFTY22500CE",
                    "option_type": "CE",
                    "expiry": "2026-07-21",
                    "expiry_kind": "weekly",
                    "strike": 22500.0,
                    "option_price": 132.0,
                    "expected_pnl": 18.0,
                    "price_source": "local_watchlist",
                },
                # risk.approve() reports the budget ALREADY scaled by the
                # policy multiplier (base 15_000 x 2.0 = 30_000).
                "risk": {"approved": True, "quantity_lots": 1, "quantity_units": 75, "risk_budget": 30_000.0},
                "policy": {"size_multiplier": 2.0, "act": True},
                "selection_reason": "test open",
                "data_status": {"execution_ready": True},
            },
        }

        await store.sync_snapshot(payload)
        # register_open must receive the BASE budget (30_000 / 2.0).
        assert captured["risk_budget"] == pytest.approx(15_000.0)
        assert captured["size_multiplier"] == pytest.approx(2.0)


class TestRegimeFastTapeMatch:
    def _micro_trend_row(self) -> dict:
        return {
            "adx": 13.0,
            "breakout_up": 0.0,
            "breakout_down": 0.0,
            "rv_percentile": 0.4,
            "range_expansion": 1.0,
            "ema_spread_pct": 0.0004,
            "plus_di": 10.0,
            "minus_di": 9.0,
        }

    def test_3minute_is_fast_tape(self) -> None:
        snapshot = RegimeClassifier().classify(self._micro_trend_row(), timeframe="3minute")
        assert snapshot.label == "micro_trend"

    def test_30minute_is_not_fast_tape(self) -> None:
        snapshot = RegimeClassifier().classify(self._micro_trend_row(), timeframe="30minute")
        assert snapshot.label != "micro_trend"
