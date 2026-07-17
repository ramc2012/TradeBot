"""Anti-churn execution discipline + signal-uncap regression (2026-07-17).

OWNER DIRECTIVE (~13:40 IST): "reverse those codex fixes- uncap signals, no
hard gate. but see that the lane has sane strategy instead of just opening
and closing posiitons."

Two halves, tested together because they are one design:

* SIGNALS STAY UNCAPPED — no allowed_regimes barrier, no min_confidence
  cutoff, no premium cap, no portfolio position caps. Regimes are FEATURES
  the RL policy sees as a one-hot; the policy learns act/skip from realised
  R-multiples.
* DISCIPLINE LIVES IN THE EXECUTION LAYER — per-underlying re-entry
  cooldowns (reason-differentiated: stop_loss waits longer than flat/flip/
  target closes), 2-cycle flip confirmation, min-hold on flat/flip closes
  only. Protective exits (stop/target/DTE/expiry) are NEVER blocked, and a
  cooldown-blocked proposal is journaled (cooldown_skip) — never a silent
  drop, never a policy act.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.config import settings
from directional_options.config import clone_default_config
from directional_options.paper import DirectionalOptionsPaperStore
from directional_options.schemas import RegimeSnapshot
from directional_options.signals import DirectionalSignalEngine


# ── helpers ──────────────────────────────────────────────────────────────────


def _regime(label: str, *, trade_allowed: bool, confidence: float = 0.78) -> RegimeSnapshot:
    return RegimeSnapshot(
        label=label,
        trade_allowed=trade_allowed,
        confidence=confidence,
        reasons=[],
        preferred_expiry_kind="weekly",
        delta_target_min=0.35,
        delta_target_max=0.55,
        exit_profile="balanced",
    )


def _strong_bar() -> dict[str, float]:
    return {
        "ema_spread_pct": 0.0032,
        "breakout_up": 0.4,
        "breakout_down": 0.0,
        "plus_di": 31.0,
        "minus_di": 16.0,
        "momentum_3": 0.004,
        "momentum_8": 0.009,
        "atr": 72.0,
        "close": 24850.0,
        "range_expansion": 1.3,
        "rv_percentile": 0.42,
    }


def _weak_bar() -> dict[str, float]:
    """Barely-directional tape → confidence lands well below 0.60."""
    return {
        "ema_spread_pct": 0.0004,
        "breakout_up": 0.0,
        "breakout_down": 0.0,
        "plus_di": 21.0,
        "minus_di": 19.0,
        "momentum_3": 0.0005,
        "momentum_8": 0.0003,
        "atr": 18.0,
        "close": 24850.0,
        "range_expansion": 1.0,
        "rv_percentile": 0.4,
    }


class _RecordingPolicy:
    def __init__(self) -> None:
        self.register_open_calls: list[dict] = []
        self.record_close_calls: list[dict] = []

    def register_open(self, **kwargs) -> None:
        self.register_open_calls.append(kwargs)

    def record_close(self, **kwargs) -> float:
        self.record_close_calls.append(kwargs)
        return 1.0


def _isolate_store(
    store: DirectionalOptionsPaperStore,
    monkeypatch: pytest.MonkeyPatch,
    *,
    initial_state: dict[str, list] | None = None,
) -> tuple[dict[str, list], list[dict]]:
    """DB-free store: in-memory position state + captured journal records.

    Keeps the REAL _summary (its DB read already falls back to the in-memory
    lists on failure) so the churn telemetry keys are exercised.
    """
    state: dict[str, list] = {
        "open_positions": [dict(row) for row in (initial_state or {}).get("open_positions", [])],
        "closed_positions": [dict(row) for row in (initial_state or {}).get("closed_positions", [])],
    }
    journal: list[dict] = []

    async def _load_positions() -> dict[str, list]:
        return {
            "open_positions": [dict(row) for row in state["open_positions"]],
            "closed_positions": [dict(row) for row in state["closed_positions"]],
        }

    async def _save_positions(payload: dict) -> None:
        state["open_positions"] = [dict(row) for row in payload.get("open_positions", [])]
        state["closed_positions"] = [dict(row) for row in payload.get("closed_positions", [])]

    async def _load_journal() -> list[dict]:
        return list(journal)

    async def _append_journal(payload: dict) -> None:
        journal.append(dict(payload))

    async def _noop(*_args, **_kwargs):
        return None

    def _no_db():
        raise RuntimeError("DB disabled in test")

    monkeypatch.setattr(store, "_load_positions", _load_positions)
    monkeypatch.setattr(store, "_save_positions", _save_positions)
    monkeypatch.setattr(store, "_load_journal", _load_journal)
    monkeypatch.setattr(store, "_append_journal", _append_journal)
    monkeypatch.setattr("directional_options.paper.AsyncSessionLocal", _no_db)
    monkeypatch.setattr("directional_options.paper.paper_trade_recorder.record_event", _noop)
    return state, journal


def _open_row(
    *,
    underlying: str = "NIFTY",
    direction: str = "CE",
    opened_seconds_ago: float = 3600.0,
    entry_premium: float = 132.0,
    expiry: str = "2026-08-27",
    position_id: str = "pos-1",
) -> dict:
    now = datetime.now(timezone.utc)
    opened = (now - timedelta(seconds=opened_seconds_ago)).isoformat()
    return {
        "position_id": position_id,
        "status": "open",
        "opened_at": opened,
        "updated_at": opened,
        "last_actionable_at": opened,
        "underlying": underlying,
        "positional": False,
        "timeframe": "3minute",
        "direction": direction,
        "trading_symbol": f"{underlying} 22500 {direction}",
        "instrument_key": f"NSE:{underlying}22500{direction}",
        "option_type": direction,
        "expiry": expiry,
        "strike": 22500.0,
        "quantity_lots": 1,
        "quantity_units": 75,
        "entry_premium": entry_premium,
        "latest_premium": entry_premium,
        "entry_spot": 22500.0,
        "latest_spot": 22500.0,
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
    }


def _closed_row(
    *,
    underlying: str = "NIFTY",
    close_reason: str = "flat_signal",
    closed_seconds_ago: float = 60.0,
    position_id: str = "closed-1",
) -> dict:
    now = datetime.now(timezone.utc)
    closed = (now - timedelta(seconds=closed_seconds_ago)).isoformat()
    return {
        "position_id": position_id,
        "status": "closed",
        # Opened well before today so the opens_today telemetry counter only
        # reflects positions opened during the test run itself.
        "opened_at": (now - timedelta(days=2)).isoformat(),
        "updated_at": closed,
        "closed_at": closed,
        "close_reason": close_reason,
        "underlying": underlying,
        "direction": "CE",
        "entry_premium": 130.0,
        "exit_premium": 131.0,
        "quantity_units": 75,
        "realized_pnl": 75.0,
    }


def _actionable_payload(
    *,
    underlying: str = "NIFTY",
    direction: str = "CE",
    strike: float = 22500.0,
    option_price: float = 132.0,
) -> dict:
    return {
        "selection": {"underlying": underlying, "timeframe": "3minute", "lookback_sessions": 16},
        "snapshot": {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "underlying": underlying,
            "timeframe": "3minute",
            "spot_price": 22512.5,
            "positional": False,
            "signal": {
                "direction": direction,
                "confidence": 0.71,
                "expected_move": 118.0,
                "expected_horizon_bars": 8,
            },
            "regime": {"label": "trend"},
            "selected_contract": {
                "trading_symbol": f"{underlying} {int(strike)} {direction}",
                "instrument_key": f"NSE:{underlying}{int(strike)}{direction}",
                "option_type": direction,
                "expiry": "2026-08-27",
                "strike": strike,
                "option_price": option_price,
                "expected_pnl": 1800.0,
            },
            "risk": {"approved": True, "quantity_lots": 1, "quantity_units": 75, "risk_budget": 15000.0},
            "policy": {"size_multiplier": 1.0},
            "selection_reason": "test entry",
            "data_status": {"execution_ready": True},
        },
    }


def _flat_payload(*, underlying: str = "NIFTY", execution_ready: bool = True) -> dict:
    return {
        "selection": {"underlying": underlying, "timeframe": "3minute", "lookback_sessions": 16},
        "snapshot": {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "underlying": underlying,
            "timeframe": "3minute",
            "spot_price": 22512.5,
            "signal": None,
            "regime": {"label": "chop"},
            "selected_contract": None,
            "risk": {"approved": False},
            "selection_reason": "flat",
            "data_status": {"execution_ready": execution_ready},
        },
    }


def _mark(position_id: str, premium: float) -> dict[str, dict]:
    return {
        position_id: {
            "premium": premium,
            "spot": 22512.5,
            "mark_time": datetime.now(timezone.utc).isoformat(),
            "price_source": "local_watchlist",
        }
    }


# ── signal uncap regression (the reversal itself) ───────────────────────────


def test_chop_and_exploration_regimes_still_produce_signals(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DIRECTIONAL_POSITIONAL_OPTIONS_ENABLED", False)
    engine = DirectionalSignalEngine(clone_default_config()["signal_engine"])

    chop = engine.predict(_strong_bar(), _regime("chop", trade_allowed=False), "3minute", underlying="RELIANCE")
    exploration = engine.predict(
        _strong_bar(), _regime("exploration", trade_allowed=True), "3minute", underlying="RELIANCE"
    )
    assert chop is not None, "chop regime must reach the policy as a FEATURE, not be vetoed"
    assert exploration is not None
    assert chop.sleeve == "no_trade"  # labelled honestly — but still surfaced


def test_low_confidence_still_produces_a_signal(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DIRECTIONAL_POSITIONAL_OPTIONS_ENABLED", False)
    engine = DirectionalSignalEngine(clone_default_config()["signal_engine"])

    signal = engine.predict(
        _weak_bar(), _regime("trend", trade_allowed=True, confidence=0.2), "3minute", underlying="RELIANCE"
    )
    assert signal is not None, "no min_confidence cutoff — the policy decides act/skip"
    assert signal.confidence < 0.60


def test_gate_configuration_reversed() -> None:
    config = clone_default_config()
    assert config["risk"]["premium_cap_pct"] is None, "owner: no limit on size"
    assert "allowed_regimes" not in config["signal_engine"]
    assert "min_confidence" not in config["signal_engine"]
    assert config["signal_engine"]["min_direction_score_floor"] == pytest.approx(0.001)
    assert "max_open_positions" not in config["paper_trading"]
    assert "max_reserved_premium_pct" not in config["paper_trading"]


# ── re-entry cooldown ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cooldown_blocks_reentry_journals_and_skips_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DIRECTIONAL_REENTRY_COOLDOWN_FLAT_SECONDS", 900.0)
    policy = _RecordingPolicy()
    store = DirectionalOptionsPaperStore(tmp_path / "paper", policy=policy)
    state, journal = _isolate_store(
        store,
        monkeypatch,
        initial_state={"closed_positions": [_closed_row(close_reason="flat_signal", closed_seconds_ago=60.0)]},
    )

    summary = await store.sync_snapshot(_actionable_payload())

    assert state["open_positions"] == []
    assert policy.register_open_calls == [], "a cooldown skip is NOT a policy act"
    skips = [row for row in journal if row.get("status") == "cooldown_skip"]
    assert len(skips) == 1, "blocked proposal must be journaled, not silently dropped"
    assert 0.0 < float(skips[0]["cooldown_seconds_remaining"]) <= 900.0
    assert skips[0]["cooldown_close_reason"] == "flat_signal"
    assert summary["cooldown_skips_today"] == 1
    assert summary["opens_today"] == 0


@pytest.mark.asyncio
async def test_cooldown_allows_entry_after_window_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DIRECTIONAL_REENTRY_COOLDOWN_FLAT_SECONDS", 900.0)
    policy = _RecordingPolicy()
    store = DirectionalOptionsPaperStore(tmp_path / "paper", policy=policy)
    state, journal = _isolate_store(
        store,
        monkeypatch,
        initial_state={"closed_positions": [_closed_row(close_reason="flat_signal", closed_seconds_ago=1200.0)]},
    )

    summary = await store.sync_snapshot(_actionable_payload())

    assert len(state["open_positions"]) == 1
    assert len(policy.register_open_calls) == 1
    assert [row for row in journal if row.get("status") == "cooldown_skip"] == []
    assert summary["opens_today"] == 1


@pytest.mark.asyncio
async def test_stop_loss_cooldown_is_longer_than_flat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1200s after the close: a flat close has cleared its 900s window, but a
    stop-out (falsified thesis) still sits inside its 1800s window."""
    monkeypatch.setattr(settings, "DIRECTIONAL_REENTRY_COOLDOWN_FLAT_SECONDS", 900.0)
    monkeypatch.setattr(settings, "DIRECTIONAL_REENTRY_COOLDOWN_STOP_SECONDS", 1800.0)

    stop_store = DirectionalOptionsPaperStore(tmp_path / "paper-stop", policy=_RecordingPolicy())
    stop_state, stop_journal = _isolate_store(
        stop_store,
        monkeypatch,
        initial_state={"closed_positions": [_closed_row(close_reason="stop_loss", closed_seconds_ago=1200.0)]},
    )
    await stop_store.sync_snapshot(_actionable_payload())
    assert stop_state["open_positions"] == []
    assert [row for row in stop_journal if row.get("status") == "cooldown_skip"]

    flat_store = DirectionalOptionsPaperStore(tmp_path / "paper-flat", policy=_RecordingPolicy())
    flat_state, _ = _isolate_store(
        flat_store,
        monkeypatch,
        initial_state={"closed_positions": [_closed_row(close_reason="flat_signal", closed_seconds_ago=1200.0)]},
    )
    await flat_store.sync_snapshot(_actionable_payload())
    assert len(flat_state["open_positions"]) == 1


# ── flip confirmation ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_flip_needs_two_consecutive_cycles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = _RecordingPolicy()
    store = DirectionalOptionsPaperStore(tmp_path / "paper", policy=policy)
    held = _open_row(direction="CE", opened_seconds_ago=3600.0)
    state, journal = _isolate_store(store, monkeypatch, initial_state={"open_positions": [held]})

    flip_payload = _actionable_payload(direction="PE", strike=22400.0, option_price=118.0)

    # Cycle 1: flip observed once → NO round trip. Position stays open, flip
    # is pending, no new position opened.
    await store.sync_snapshot(flip_payload, position_marks=_mark("pos-1", 131.0))
    assert len(state["open_positions"]) == 1
    assert state["open_positions"][0]["position_id"] == "pos-1"
    assert state["open_positions"][0]["direction"] == "CE"
    assert state["open_positions"][0]["pending_flip_direction"] == "PE"
    assert state["closed_positions"] == []
    assert policy.register_open_calls == []

    # Cycle 2: flip persists → close-and-reverse executes (the confirmed
    # reversal is atomic — its own close does not cooldown-block the reverse
    # leg).
    await store.sync_snapshot(flip_payload, position_marks=_mark("pos-1", 131.0))
    assert len(state["closed_positions"]) == 1
    assert state["closed_positions"][0]["close_reason"] == "signal_flip"
    assert len(state["open_positions"]) == 1
    assert state["open_positions"][0]["direction"] == "PE"
    assert state["open_positions"][0]["position_id"] != "pos-1"
    assert len(policy.register_open_calls) == 1


@pytest.mark.asyncio
async def test_flip_pending_cleared_by_reaffirming_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CE → (one PE cycle) → CE again: the whipsaw never round-trips and the
    pending flip is dropped when the held direction is re-affirmed."""
    store = DirectionalOptionsPaperStore(tmp_path / "paper", policy=_RecordingPolicy())
    held = _open_row(direction="CE", opened_seconds_ago=3600.0)
    state, _ = _isolate_store(store, monkeypatch, initial_state={"open_positions": [held]})

    await store.sync_snapshot(
        _actionable_payload(direction="PE", strike=22400.0), position_marks=_mark("pos-1", 131.0)
    )
    assert state["open_positions"][0]["pending_flip_direction"] == "PE"

    # Same contract + direction as held → refresh path clears the pending flip.
    await store.sync_snapshot(_actionable_payload(direction="CE"), position_marks=_mark("pos-1", 131.0))
    assert len(state["open_positions"]) == 1
    assert state["open_positions"][0]["position_id"] == "pos-1"
    assert "pending_flip_direction" not in state["open_positions"][0]
    assert state["closed_positions"] == []


# ── protective exits are never blocked ───────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_loss_ignores_min_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A -35% mark 10 seconds after open fires the stop immediately — min_hold
    only delays flat/flip closes, never protective exits."""
    store = DirectionalOptionsPaperStore(tmp_path / "paper", policy=_RecordingPolicy(), min_hold_bars=3)
    held = _open_row(direction="CE", opened_seconds_ago=10.0, entry_premium=132.0)
    state, _ = _isolate_store(store, monkeypatch, initial_state={"open_positions": [held]})

    await store.sync_snapshot(_flat_payload(), position_marks=_mark("pos-1", 132.0 * 0.6))

    assert state["open_positions"] == []
    assert len(state["closed_positions"]) == 1
    assert state["closed_positions"][0]["close_reason"] == "stop_loss"


@pytest.mark.asyncio
async def test_expiry_exit_fires_even_without_a_fresh_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DTE guard must close the book even on a cycle with no mark for the held
    leg — a fresh-mark precondition on protective exits made near-expiry
    positions immortal whenever their contract fell off the feed."""
    store = DirectionalOptionsPaperStore(tmp_path / "paper", policy=_RecordingPolicy())
    expiry = (datetime.now(timezone.utc) + timedelta(days=0)).date().isoformat()
    held = _open_row(direction="CE", opened_seconds_ago=30.0, expiry=expiry)
    state, _ = _isolate_store(store, monkeypatch, initial_state={"open_positions": [held]})

    # No position_marks at all — and the cycle isn't even execution_ready.
    await store.sync_snapshot(_flat_payload(execution_ready=False))

    assert state["open_positions"] == []
    assert len(state["closed_positions"]) == 1
    assert state["closed_positions"][0]["close_reason"] == "expiry_roll"


@pytest.mark.asyncio
async def test_cooldown_never_delays_protective_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An active cooldown on the underlying (recent stop-out) must not stop
    the OTHER open position's stop from firing — cooldowns gate entries only."""
    monkeypatch.setattr(settings, "DIRECTIONAL_REENTRY_COOLDOWN_STOP_SECONDS", 1800.0)
    store = DirectionalOptionsPaperStore(tmp_path / "paper", policy=_RecordingPolicy())
    held = _open_row(direction="CE", opened_seconds_ago=20.0, entry_premium=132.0, position_id="pos-2")
    state, _ = _isolate_store(
        store,
        monkeypatch,
        initial_state={
            "open_positions": [held],
            "closed_positions": [_closed_row(close_reason="stop_loss", closed_seconds_ago=30.0)],
        },
    )

    await store.sync_snapshot(_flat_payload(), position_marks=_mark("pos-2", 132.0 * 0.5))

    assert state["open_positions"] == []
    closed_ids = {row["position_id"]: row for row in state["closed_positions"]}
    assert closed_ids["pos-2"]["close_reason"] == "stop_loss"
