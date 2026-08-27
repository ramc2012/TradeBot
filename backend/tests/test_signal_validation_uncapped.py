"""SIGNAL_VALIDATION_UNCAPPED — owner directive 2026-07-17 (paper lanes only).

"We are currently validating signals, hence no limit on loss/capital — allow
lanes to trade fully as per strategy."

These tests prove, per paper lane, that the CAPITAL / LOSS / DRAWDOWN /
CIRCUIT-BREAKER **entry** blocks are bypassed when the flag is True and
ENFORCED when it is False, and that the protective EXITS still fire under the
flag (validation needs honest exits). The live_engine / risk_manager path is
untouched by the flag.

Enforced-when-False coverage also lives in the lanes' pre-existing tests
(pinned to False):
  * tests/test_macd_refined_paper.py::test_macd_refined_rejects_entry_that_exceeds_available_capital
  * tests/test_commodity_strategy_agent.py::test_entry_risk_block_triggers_on_cumulative_drawdown
  * tests/test_auction_intelligence.py::test_risk_governor_daily_loss_halts_entries_in_paper_mode
  * tests/test_directional_options.py::test_risk_engine_caps_size_on_daily_loss_breach
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from core.config import settings
from paper_engine.base_strategy_agent import IST


# ── Flag + S1 precedent ────────────────────────────────────────────────────

def test_signal_validation_flag_defaults_on_and_s1_stays_uncapped() -> None:
    """Owner directive 2026-07-17: validation mode ships ON; S1's own
    uncapped-capital pin (the mirrored precedent) stays ON too."""
    assert settings.SIGNAL_VALIDATION_UNCAPPED is True
    assert settings.MACD_STRATEGY_UNCAPPED_CAPITAL is True
    assert settings.MACD_STRATEGY_MAX_POSITIONS >= 1000


# ── macd_refined ───────────────────────────────────────────────────────────

def _macd_store(tmp_path: Path):
    from macd_refined.config import clone_default_config
    from macd_refined.paper import MacdRefinedPaperStore

    config = clone_default_config()
    config["paper_trading"]["journal_root"] = str(tmp_path / "paper")
    return MacdRefinedPaperStore(config["paper_trading"]["journal_root"], config=config)


def _macd_proposal(**overrides) -> dict:
    row = {
        "underlying": "BAJFINANCE",
        "option_type": "CE",
        "trading_symbol": "BAJFINANCE 1050 CE",
        "instrument_key": "NSE:BAJFINANCE26JUL1050CE",
        "expiry": "2026-07-28",
        "expiry_window_end": "2026-07-21",
        "strike": 1050.0,
        "spot": 1054.0,
        "lot_size": 750,
        "quantity_lots": 20,
        "quantity_units": 15_000,
        "entry_premium": 27.30,
        "iv": 0.2302,
        "selection_reason": "test",
    }
    row.update(overrides)
    return row


def test_macd_refined_cash_gate_bypassed_when_flag_true(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "SIGNAL_VALIDATION_UNCAPPED", True)
    store = _macd_store(tmp_path)
    # ₹27.30 × 300k units ≈ ₹8.2M required vs ₹5M book — blocked when capped.
    proposal = _macd_proposal(quantity_units=300_000, quantity_lots=400)

    payload = store.sync_cycle(proposals=[proposal], marks={}, now="2026-07-13T04:00:00+00:00")

    assert payload["admitted_this_cycle"] == 1
    assert payload["capital_blocked_this_cycle"] == 0
    assert len(store.list_positions(status="open")["open_positions"]) == 1


def test_macd_refined_kill_switch_reports_but_does_not_pause_when_flag_true(tmp_path: Path, monkeypatch) -> None:
    import macd_refined.paper as paper_module

    monkeypatch.setattr(paper_module, "kill_switch_state", lambda *a, **k: (True, "forced by test"))

    # Flag ON: the paused kill switch is REPORTED but entries still admit.
    monkeypatch.setattr(settings, "SIGNAL_VALIDATION_UNCAPPED", True)
    store = _macd_store(tmp_path)
    payload = store.sync_cycle(proposals=[_macd_proposal()], marks={}, now="2026-07-13T04:00:00+00:00")
    assert payload["kill_switch_paused"] is True
    assert payload["kill_switch_reason"] == "forced by test"
    assert payload["admitted_this_cycle"] == 1

    # Flag OFF: the same paused kill switch blocks the entry.
    monkeypatch.setattr(settings, "SIGNAL_VALIDATION_UNCAPPED", False)
    store2 = _macd_store(tmp_path / "capped")
    payload2 = store2.sync_cycle(proposals=[_macd_proposal()], marks={}, now="2026-07-13T04:00:00+00:00")
    assert payload2["kill_switch_paused"] is True
    assert payload2["admitted_this_cycle"] == 0


def test_macd_refined_stop_loss_exit_still_fires_when_flag_true(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "SIGNAL_VALIDATION_UNCAPPED", True)
    store = _macd_store(tmp_path)
    store.sync_cycle(proposals=[_macd_proposal()], marks={}, now="2026-07-13T04:00:00+00:00")
    position_id = store.list_positions(status="open")["open_positions"][0]["position_id"]

    # 27.30 → 18.00 is a ~34% collapse — beyond the 30% hard stop.
    store.sync_cycle(
        proposals=[],
        marks={position_id: {"premium": 18.00, "spot": 1030.00}},
        now="2026-07-13T05:00:00+00:00",
        allow_entries=False,
    )

    closed = store.list_positions(status="closed")["closed_positions"]
    assert len(closed) == 1
    assert closed[0]["realized_pnl"] == pytest.approx((18.00 - 27.30) * 15_000)
    journal = store.list_journal(limit=5)["records"]
    assert any(r.get("event") == "close" and r.get("reason") == "stop_loss" for r in journal)


class _MacdStoreStub:
    def lot_size_for(self, _underlying: str) -> int:
        return 50


class _BrickedPaperStub:
    """The live 2026-07-17 state: book −33.7%, ₹23.5k available."""

    def capital_status(self) -> dict[str, float]:
        return {"total_equity_net": 23_500.0, "available_capital_net": 23_500.0}


def _macd_live_engine(tmp_path: Path):
    from macd_refined.config import clone_default_config
    from macd_refined.live import MacdRefinedLiveEngine

    config = clone_default_config()
    config["live_universe_mode"] = "list"
    config["live_universe"] = ["NIFTY"]
    config["live"]["volume_store_root"] = str(tmp_path / "volume_tracking")
    return MacdRefinedLiveEngine(_MacdStoreStub(), _BrickedPaperStub(), config)


def _write_cross_tracking(engine, expiry_iso: str) -> None:
    """Persist a synthetic 30-min premium history whose MACD crosses zero on
    the last completed bar (decline → rally), with rich turnover."""
    from macd_refined.indicators import compute_macd, zero_cross_up

    idx = pd.date_range("2026-07-01 04:00:00+00:00", periods=80, freq="30min")
    prices = list(np.linspace(120, 90, 50)) + list(np.linspace(92, 140, 30))
    macd, _sig, _hist = compute_macd(pd.Series(prices, index=idx), 12, 26, 9)
    cross_positions = [i for i, c in enumerate(zero_cross_up(macd).to_numpy()) if c]
    last_cross = cross_positions[-1]
    assert last_cross + 1 >= 37, "need ≥ macd_slow+macd_signal+2 completed bars"
    keep = last_cross + 2  # completed bars 0..last_cross + one forming bar

    rows = [
        {
            "captured_at": ts.isoformat(),
            "underlying": "NIFTY",
            "expiry": expiry_iso,
            "option_type": "CE",
            "strike": 25_000.0,
            "spot_price": 25_000.0,
            "ltp": float(price),
            "iv": 15.0,
            "lot_size": 50,
            "instrument_key": "NSE:NIFTY-TEST-25000-CE",
            "turnover_rupees": 1_000_000_000.0,
        }
        for ts, price in zip(idx[:keep], prices[:keep])
    ]
    engine._persist_snapshots("NIFTY", rows)


def test_macd_refined_live_sizing_base_pinned_when_flag_true(tmp_path: Path, monkeypatch) -> None:
    """live.py sizing base: bricked equity (₹23.5k) puts the 10%-equity cap at
    ₹2.35k ≪ the ₹50k min ticket → every signal skipped when capped. Under the
    flag the base pins to max(starting_equity, total_equity_net) = ₹5M and the
    same signal sizes to a full ticket."""
    expiry = date.today() + timedelta(days=20)

    monkeypatch.setattr(settings, "SIGNAL_VALIDATION_UNCAPPED", True)
    engine = _macd_live_engine(tmp_path / "uncapped")
    _write_cross_tracking(engine, expiry.isoformat())
    proposals = asyncio.run(engine._evaluate(None, "NIFTY", [expiry]))
    assert len(proposals) == 1
    assert proposals[0]["quantity_lots"] >= 1

    monkeypatch.setattr(settings, "SIGNAL_VALIDATION_UNCAPPED", False)
    engine2 = _macd_live_engine(tmp_path / "capped")
    _write_cross_tracking(engine2, expiry.isoformat())
    proposals2 = asyncio.run(engine2._evaluate(None, "NIFTY", [expiry]))
    assert proposals2 == []
    journal = engine2.recent_signals(limit=5)["signals"]
    assert journal and journal[0]["accepted"] is False
    assert "min" in journal[0]["skip_reason"]


# ── directional_options ────────────────────────────────────────────────────

def _directional_engine():
    from directional_options.config import clone_default_config
    from directional_options.risk import DirectionalOptionsRiskEngine

    return DirectionalOptionsRiskEngine(clone_default_config()["risk"]), clone_default_config()["risk"]


def _directional_candidate():
    from directional_options.schemas import ContractCandidate

    return ContractCandidate(
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


def _directional_signal():
    from directional_options.schemas import DirectionalSignal

    return DirectionalSignal(
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


def test_directional_loss_caps_bypassed_when_flag_true(monkeypatch) -> None:
    engine, risk_cfg = _directional_engine()
    equity = 1_000_000.0
    daily_breach = -(equity * float(risk_cfg["risk_pct"]) * float(risk_cfg["daily_loss_cap_r"])) - 1.0
    weekly_breach = -(equity * float(risk_cfg["risk_pct"]) * float(risk_cfg["weekly_loss_cap_r"])) - 1.0

    monkeypatch.setattr(settings, "SIGNAL_VALIDATION_UNCAPPED", True)
    decision = engine.approve(
        candidate=_directional_candidate(),
        signal=_directional_signal(),
        equity=equity,
        size_multiplier=1.0,
        daily_realized=daily_breach,
        weekly_realized=weekly_breach,
    )
    assert decision.approved is True
    assert decision.quantity_lots >= 1
    assert decision.reasons == []

    monkeypatch.setattr(settings, "SIGNAL_VALIDATION_UNCAPPED", False)
    blocked = engine.approve(
        candidate=_directional_candidate(),
        signal=_directional_signal(),
        equity=equity,
        size_multiplier=1.0,
        daily_realized=daily_breach,
        weekly_realized=weekly_breach,
    )
    assert blocked.approved is False
    assert any("daily loss cap" in reason.lower() for reason in blocked.reasons)
    assert any("weekly loss cap" in reason.lower() for reason in blocked.reasons)


# ── commodity strategy agent ───────────────────────────────────────────────

def test_commodity_drawdown_entry_block_bypassed_when_flag_true(tmp_path: Path, monkeypatch) -> None:
    import paper_engine.commodity_strategy_agent as commodity_module
    from paper_engine.commodity_strategy_agent import CommodityStrategyAgent

    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", tmp_path / "commodity_strategy.json")

    monkeypatch.setattr(settings, "SIGNAL_VALIDATION_UNCAPPED", True)
    agent = CommodityStrategyAgent()
    # The constructor loads LIVE state from the DB, so any open production
    # position inflates total_equity and shrinks the drawdown below the 15%
    # this test needs — it failed at 13.38% once the live book held 2 open
    # positions. Clear the loaded book so the fixture is hermetic.
    agent._runtime.positions.clear()  # type: ignore[attr-defined]
    agent._runtime.portfolio._positions.clear()  # type: ignore[attr-defined]
    # Deep drawdown vs peak — the 15% block would fire when capped.
    agent._runtime.portfolio.available_capital = 418_501.60  # type: ignore[attr-defined]
    agent._runtime.portfolio._peak_equity = 1_000_000.0  # type: ignore[attr-defined]
    assert agent._current_drawdown_pct() >= 15.0

    assert agent._entry_risk_block("SILVERM") is None

    # Enforced again the moment the flag is off (same agent state).
    monkeypatch.setattr(settings, "SIGNAL_VALIDATION_UNCAPPED", False)
    block = agent._entry_risk_block("SILVERM")
    assert block is not None and block["code"] == "max_drawdown_limit"


# ── institutional_convergence (NSE + MCX book class) ───────────────────────

def _ic_book(tmp_path: Path):
    from institutional_convergence.paper import ConvergencePaperBook

    return ConvergencePaperBook(tmp_path / "paper.json")


def _ic_locked_state(today: str, extra: dict | None = None) -> dict:
    state = {
        "initial_capital": 1_000_000,
        "open_positions": [],
        "closed_positions": [
            {"session_date": today, "realized_pnl": -1000},
            {"session_date": today, "realized_pnl": -1000},
        ],
    }
    state.update(extra or {})
    return state


def _ic_signal() -> dict:
    return {
        "symbol": "NIFTY", "status": "actionable_paper", "action": "LONG", "spot": 100.0,
        "risk": {"entry": 100.0, "stop": 90.0, "target1": 110.0, "target2_long": 120.0, "lot_size": 50, "risk_fraction": 0.01},
        "cvd": {"series": [{"cvd": 1}, {"cvd": 2}]},
    }


def test_ic_circuit_reports_but_does_not_lock_entries_when_flag_true(tmp_path: Path, monkeypatch) -> None:
    now = datetime(2026, 7, 13, 10, 30, tzinfo=IST)
    today = "2026-07-13"

    monkeypatch.setattr(settings, "SIGNAL_VALIDATION_UNCAPPED", True)
    book = _ic_book(tmp_path / "uncapped")
    book._save(_ic_locked_state(today))
    summary = book.sync([_ic_signal()], now)
    assert summary["circuit_breaker"]["locked"] is True  # still REPORTED
    assert summary["open_count"] == 1  # but entries are not locked

    monkeypatch.setattr(settings, "SIGNAL_VALIDATION_UNCAPPED", False)
    book2 = _ic_book(tmp_path / "capped")
    book2._save(_ic_locked_state(today))
    summary2 = book2.sync([_ic_signal()], now)
    assert summary2["circuit_breaker"]["locked"] is True
    assert summary2["open_count"] == 0  # enforced when the flag is off


def test_ic_hard_stop_exit_still_fires_when_flag_true(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "SIGNAL_VALIDATION_UNCAPPED", True)
    now = datetime(2026, 7, 13, 10, 30, tzinfo=IST)
    book = _ic_book(tmp_path)
    opened = book.sync([_ic_signal()], now)
    assert opened["open_count"] == 1

    # Price through the stop — the protective exit must fire even in
    # validation mode (and even while the circuit reports locked).
    stopped_signal = _ic_signal()
    stopped_signal["spot"] = 89.0
    closed = book.sync([stopped_signal], now.replace(minute=45))
    assert closed["open_count"] == 0
    assert closed["closed_positions"][-1]["exit_reason"] == "hard_stop"
    assert closed["closed_positions"][-1]["realized_pnl"] < 0


# ── auction_intelligence risk governor ─────────────────────────────────────

def _auction_governor(paper_mode: bool):
    from auction_intelligence.config import clone_default_config
    from auction_intelligence.risk import RiskGovernor

    config = clone_default_config()
    # Pin the loss cap so the tests verify blocking LOGIC, decoupled from the
    # prod default.
    return RiskGovernor({**config["risk"], "max_daily_loss": 75_000.0, "paper_mode": paper_mode})


def _auction_decision():
    from auction_intelligence.schemas import AgentDecision

    return AgentDecision(
        agent_name="positional",
        action="LONG",
        confidence=0.9,
        entry_price=100.0,
        stop_price=95.0,
        target_price=115.0,
        quantity=25,
        sleeve_fraction=0.04,
        rationale=["test"],
    )


def _auction_breached_portfolio():
    from auction_intelligence.schemas import PortfolioSnapshot

    return PortfolioSnapshot(
        daily_realized_pnl=-75_001.0,          # breaches the daily loss cap
        net_liquidation=5_000_000.0,
        agent_drawdowns={"positional": 0.50},  # breaches the 8% drawdown cap
        symbol_exposure={"NIFTY": 0.90},       # breaches the 0.35 symbol cap
        correlated_exposure=0.90,              # breaches the 0.55 correlated cap
    )


def _auction_session(minutes_to_close: int = 180):
    from auction_intelligence.schemas import SessionContext

    return SessionContext(
        symbol="NIFTY",
        session_date=date(2026, 7, 17),
        last_price=23_000.0,
        stale_data_seconds=0.0,
        minutes_to_close=minutes_to_close,
    )


def test_auction_governor_caps_bypassed_in_paper_when_flag_true(monkeypatch) -> None:
    monkeypatch.setattr(settings, "SIGNAL_VALIDATION_UNCAPPED", True)
    governor = _auction_governor(paper_mode=True)

    allowed = governor.evaluate(
        session=_auction_session(),
        portfolio=_auction_breached_portfolio(),
        decisions=[_auction_decision()],
    )
    assert allowed.allowed is True
    assert allowed.kill_switch is False

    # Same breached book, flag off → every cap fires again.
    monkeypatch.setattr(settings, "SIGNAL_VALIDATION_UNCAPPED", False)
    blocked = governor.evaluate(
        session=_auction_session(),
        portfolio=_auction_breached_portfolio(),
        decisions=[_auction_decision()],
    )
    assert blocked.allowed is False
    assert "Daily loss limit breached." in blocked.reasons
    assert "positional drawdown cap reached." in blocked.reasons
    assert "NIFTY exposure cap reached." in blocked.reasons
    assert "Correlated exposure cap reached." in blocked.reasons


def test_auction_governor_close_buffer_kept_under_flag(monkeypatch) -> None:
    """The 15-min session-close buffer is a strategy gate — KEPT even in
    validation mode."""
    monkeypatch.setattr(settings, "SIGNAL_VALIDATION_UNCAPPED", True)
    governor = _auction_governor(paper_mode=True)

    blocked = governor.evaluate(
        session=_auction_session(minutes_to_close=10),
        portfolio=_auction_breached_portfolio(),
        decisions=[_auction_decision()],
    )
    assert blocked.allowed is False
    assert "Too close to session close for new entries." in blocked.reasons


def test_auction_governor_live_mode_unaffected_by_flag(monkeypatch) -> None:
    """paper-only directive: with paper_mode=False the caps enforce even when
    the validation flag is True."""
    monkeypatch.setattr(settings, "SIGNAL_VALIDATION_UNCAPPED", True)
    governor = _auction_governor(paper_mode=False)

    blocked = governor.evaluate(
        session=_auction_session(),
        portfolio=_auction_breached_portfolio(),
        decisions=[_auction_decision()],
    )
    assert blocked.allowed is False
    assert "Daily loss limit breached." in blocked.reasons
