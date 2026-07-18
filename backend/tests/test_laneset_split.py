"""Phase-1 process split (LANESET) — gating, partition, and no-op proofs.

The deploy-tonight guarantee: LANESET=all (the default) must be byte-identical
to the pre-split single-process boot. These tests pin that flag-off proof and
the split-mode behaviors (WS gate, runner partition, control seam, heartbeat
loop_active, foreign-plane status merge, 409 guards, REST budget fraction).
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta

import pytest

from core import laneset
from core.config import settings
from core.laneset import (
    boots_core,
    boots_strategies,
    is_core_only,
    is_split,
    normalized_laneset,
    planned_subsystems,
)
from core.market_hours_paper_supervisor import (
    MarketHoursPaperSupervisor,
    RunnerConfig,
    catchup_state_path_for,
)
from paper_engine.base_strategy_agent import IST


# ── 1. Gating truth table ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expect_norm,expect_core,expect_strategies",
    [
        ("all", "all", True, True),
        ("core", "core", True, False),
        ("strategies", "strategies", False, True),
        ("  CORE  ", "core", True, False),
        ("garbage", "all", True, True),  # fail-safe: unknown → full boot
        ("", "all", True, True),
        (None, "all", True, True),
    ],
)
def test_laneset_truth_table(monkeypatch, value, expect_norm, expect_core, expect_strategies):
    monkeypatch.setattr(settings, "LANESET", value, raising=False)
    assert normalized_laneset() == expect_norm
    assert boots_core() is expect_core
    assert boots_strategies() is expect_strategies
    assert is_split() is (expect_norm != "all")
    assert is_core_only() is (expect_norm == "core")


# ── 2. planned_subsystems inventory (the flag-off proof) ─────────────────────


def test_planned_subsystems_partition_properties():
    full = planned_subsystems("all")
    core = planned_subsystems("core")
    strategies = planned_subsystems("strategies")
    # Union: nothing is lost by splitting.
    assert full == core | strategies
    # The overlap is EXACTLY the deliberately-shared subsystems.
    assert core & strategies == laneset.SHARED_SUBSYSTEMS
    # Plane-exclusive sets never overlap.
    assert laneset.CORE_SUBSYSTEMS.isdisjoint(laneset.STRATEGY_SUBSYSTEMS)
    # Unknown laneset plans the full single-process boot (fail-safe).
    assert planned_subsystems("bogus") == full


def test_planned_runner_inventory_matches_supervisor(monkeypatch):
    monkeypatch.setattr(settings, "LANESET", "all", raising=False)
    supervisor = MarketHoursPaperSupervisor(enabled=False)
    actual = {f"runner:{key}" for key in supervisor._runners}
    planned = {item for item in planned_subsystems("all") if item.startswith("runner:")}
    assert actual == planned
    core_tagged = {
        key for key, runtime in supervisor._runners.items() if runtime.config.plane == "core"
    }
    assert core_tagged == {"option_flow_watchdog", "token_readiness", "market_intelligence"}


# ── 3. Supervisor runner partition + per-plane catch-up path ─────────────────


def test_supervisor_runner_partition_by_laneset(monkeypatch):
    monkeypatch.setattr(settings, "LANESET", "all", raising=False)
    all_keys = set(MarketHoursPaperSupervisor(enabled=False)._runners)
    assert len(all_keys) == 17

    monkeypatch.setattr(settings, "LANESET", "core", raising=False)
    core_keys = set(MarketHoursPaperSupervisor(enabled=False)._runners)
    assert core_keys == {"option_flow_watchdog", "token_readiness", "market_intelligence"}

    monkeypatch.setattr(settings, "LANESET", "strategies", raising=False)
    strategy_keys = set(MarketHoursPaperSupervisor(enabled=False)._runners)
    assert strategy_keys == all_keys - core_keys
    assert len(strategy_keys) == 14


def test_supervisor_status_shape_and_catchup_paths(monkeypatch):
    # LANESET=all: payload has NO laneset key (byte-identical shape) and the
    # legacy catch-up filename is preserved.
    monkeypatch.setattr(settings, "LANESET", "all", raising=False)
    supervisor = MarketHoursPaperSupervisor(enabled=False)
    status = supervisor.get_status()
    assert "laneset" not in status
    assert catchup_state_path_for("all").name == "post_close_catchup.json"
    assert catchup_state_path_for("bogus").name == "post_close_catchup.json"
    # Split planes get their OWN marker files (bind-mounted runtime/ is shared).
    assert catchup_state_path_for("core").name == "post_close_catchup.core.json"
    assert catchup_state_path_for("strategies").name == "post_close_catchup.strategies.json"


# ── 4. data_router broker-WS gate ────────────────────────────────────────────


class _RecordingBroker:
    broker_name = "fyers"

    def __init__(self) -> None:
        self.calls: list = []

    async def subscribe_websocket(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return object()


def test_data_router_ws_gate_blocks_strategy_plane(monkeypatch):
    from market_data.data_router import DataRouter

    router = DataRouter()
    broker = _RecordingBroker()
    router.set_broker(broker)

    monkeypatch.setattr(settings, "LANESET", "strategies", raising=False)
    asyncio.run(router.subscribe(["NSE:NIFTY50-INDEX"]))
    assert router._ws_client is None
    assert broker.calls == []
    assert router._desired_primary_symbols == []


def test_data_router_ws_gate_open_for_all_and_core(monkeypatch):
    from market_data.data_router import DataRouter

    for mode in ("all", "core"):
        router = DataRouter()
        router.set_broker(_RecordingBroker())
        monkeypatch.setattr(settings, "LANESET", mode, raising=False)
        asyncio.run(router.subscribe(["NSE:NIFTY50-INDEX"]))
        # The gate let the call through to _subscribe_unlocked (which records
        # the desired set regardless of whether the stream window is open).
        assert router._desired_primary_symbols == ["NSE:NIFTY50-INDEX"]


# ── 5. Weld ride-along: the S2 MP+OF engine has no WS/data-plane dependency ──


def test_s2_mpof_engine_has_no_ws_dependency():
    import paper_engine.strategy2_mp_of as s2

    source = inspect.getsource(s2)
    assert "data_router" not in source
    assert "subscribe_websocket" not in source
    assert "quote_bus" not in source


# ── 6. S1 cross-process control seam + heartbeat loop_active ─────────────────


def _fresh_s1_agent(monkeypatch):
    from paper_engine.strategy_agent import PaperStrategyAgent

    agent = PaperStrategyAgent()
    # Isolate from the real store for these unit tests.
    monkeypatch.setattr(agent, "_refresh_state_from_store", lambda *a, **k: False)
    monkeypatch.setattr(agent, "_persist_state", lambda: None)
    return agent


def test_s1_run_once_honors_store_kill_switch_in_split(monkeypatch):
    from paper_engine.strategy_agent import PaperStrategyAgent

    agent = PaperStrategyAgent()
    monkeypatch.setattr(settings, "LANESET", "strategies", raising=False)

    def _store_refresh(*, force: bool = False) -> bool:
        agent._kill_switch_active = True
        agent._manual_restart_required = True
        agent._auto_run_enabled = False
        return True

    monkeypatch.setattr(agent, "_refresh_state_from_store", _store_refresh)
    status = asyncio.run(agent.run_once(force=False))
    # The cycle declined to scan: control flags honored from the shared store.
    assert status["kill_switch_active"] is True
    assert agent._running is False


def test_s1_loop_active_heartbeat_only_in_split(monkeypatch):
    agent = _fresh_s1_agent(monkeypatch)
    agent._task = None
    agent._loop_heartbeat_at = datetime.now(IST).isoformat()

    # LANESET=all: legacy local-task semantics — heartbeat NEVER consulted.
    monkeypatch.setattr(settings, "LANESET", "all", raising=False)
    assert agent.get_control_state()["loop_active"] is False

    # Split core plane: fresh heartbeat ⇒ the strategy plane's loop is alive.
    monkeypatch.setattr(settings, "LANESET", "core", raising=False)
    assert agent.get_control_state()["loop_active"] is True

    # Stale heartbeat (>2× scan interval) ⇒ honestly dead.
    agent._loop_heartbeat_at = (
        datetime.now(IST) - timedelta(seconds=10 * agent.scan_interval_seconds)
    ).isoformat()
    assert agent.get_control_state()["loop_active"] is False


def test_s1_set_auto_run_never_spawns_loop_on_core(monkeypatch):
    agent = _fresh_s1_agent(monkeypatch)
    agent._kill_switch_active = False
    agent._task = None
    # Keep the loop inert if it ever gets created (belt and braces).
    monkeypatch.setattr(settings, "SLOW_LANES_ENABLED", False, raising=False)

    async def _scenario() -> None:
        monkeypatch.setattr(settings, "LANESET", "core", raising=False)
        await agent.set_auto_run(True)
        assert agent._task is None  # flag persisted, no local loop on core
        monkeypatch.setattr(settings, "LANESET", "all", raising=False)
        await agent.set_auto_run(True)
        assert agent._task is not None  # default behavior unchanged
        await agent.set_auto_run(False)
        assert agent._task is None

    asyncio.run(_scenario())


def test_commodity_heartbeat_survives_state_normalization():
    from paper_engine.commodity_strategy_agent import _normalize_saved_state

    stamp = datetime.now(IST).isoformat()
    normalized = _normalize_saved_state({"control": {"loop_heartbeat_at": stamp}})
    assert normalized["control"]["loop_heartbeat_at"] == stamp


# ── 7. Supervisor foreign-plane status merge ─────────────────────────────────


def test_supervisor_merges_foreign_plane_status(monkeypatch):
    monkeypatch.setattr(settings, "LANESET", "core", raising=False)
    now = datetime(2026, 7, 18, 12, 0, tzinfo=IST)

    async def _noop() -> dict:
        return {"result_count": 0}

    supervisor = MarketHoursPaperSupervisor(
        enabled=False,
        runners=[
            RunnerConfig(
                key="market_intelligence",
                label="MI",
                interval_seconds=60,
                callback=_noop,
                plane="core",
            )
        ],
        now_fn=lambda: now,
        market_hours_fn=lambda current: False,
        next_open_fn=lambda current: current + timedelta(days=1),
    )
    supervisor._foreign_plane_status = {
        "plane": "strategies",
        "published_at": (now - timedelta(seconds=30)).isoformat(),
        "runners": {
            "macd_refined": {
                "key": "macd_refined",
                "enabled": True,
                "running": False,
                "last_error": None,
            }
        },
    }

    status = supervisor.get_status()
    assert status["laneset"] == "core"
    assert "market_intelligence" in status["runners"]
    foreign = status["runners"]["macd_refined"]
    assert foreign["foreign"] is True
    assert foreign["plane"] == "strategies"
    assert foreign["snapshot_age_seconds"] == pytest.approx(30.0, abs=1.0)
    assert foreign["snapshot_stale"] is False

    # get_runner_status resolves foreign runners too.
    assert supervisor.get_runner_status("macd_refined")["foreign"] is True
    # Own runners always win over a foreign duplicate.
    supervisor._foreign_plane_status["runners"]["market_intelligence"] = {"key": "x"}
    assert supervisor.get_status()["runners"]["market_intelligence"].get("foreign") is None

    # Publishing path is a hard no-op for LANESET=all instances.
    monkeypatch.setattr(settings, "LANESET", "all", raising=False)
    plain = MarketHoursPaperSupervisor(
        enabled=False,
        runners=[
            RunnerConfig(key="k", label="K", interval_seconds=60, callback=_noop)
        ],
        now_fn=lambda: now,
        market_hours_fn=lambda current: False,
        next_open_fn=lambda current: current + timedelta(days=1),
    )
    asyncio.run(plain._publish_plane_status())  # returns before any Redis I/O
    assert plain._foreign_plane_status is None
    assert "laneset" not in plain.get_status()


# ── 8. 409 guards on in-process strategy endpoints (core plane only) ─────────


def test_strategy_mutating_endpoints_409_on_core(monkeypatch):
    from fastapi import HTTPException

    from api.routers import commodity as commodity_router
    from api.routers import trading as trading_router

    monkeypatch.setattr(settings, "LANESET", "core", raising=False)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(trading_router.run_strategy_agent_once())
    assert excinfo.value.status_code == 409

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(commodity_router.start_commodity_strategy_agent())
    assert excinfo.value.status_code == 409

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(commodity_router.run_commodity_strategy_once())
    assert excinfo.value.status_code == 409


def test_run_once_endpoint_passes_through_when_all(monkeypatch):
    from api.routers import trading as trading_router

    monkeypatch.setattr(settings, "LANESET", "all", raising=False)
    calls: list[bool] = []

    async def _fake_run_once(*, force: bool = True):
        calls.append(force)
        return {"ok": True}

    monkeypatch.setattr(trading_router.paper_strategy_agent, "run_once", _fake_run_once)
    result = asyncio.run(trading_router.run_strategy_agent_once())
    assert result == {"ok": True}
    assert calls == [True]


# ── 9. BROKER_REST_BUDGET_FRACTION scaling ───────────────────────────────────


def test_budget_scaled_fraction(monkeypatch):
    from brokers.rate_limiter import _budget_scaled

    monkeypatch.setattr(settings, "BROKER_REST_BUDGET_FRACTION", 1.0, raising=False)
    assert _budget_scaled(190) == 190  # default = provable no-op
    assert _budget_scaled(None) is None

    monkeypatch.setattr(settings, "BROKER_REST_BUDGET_FRACTION", 0.4, raising=False)
    assert _budget_scaled(190) == 76
    assert _budget_scaled(1800) == 720
    assert _budget_scaled(1) == 1  # floor at 1, never 0

    # Out-of-range values fail safe to the unscaled cap.
    monkeypatch.setattr(settings, "BROKER_REST_BUDGET_FRACTION", 0.0, raising=False)
    assert _budget_scaled(190) == 190
    monkeypatch.setattr(settings, "BROKER_REST_BUDGET_FRACTION", 2.0, raising=False)
    assert _budget_scaled(190) == 190
