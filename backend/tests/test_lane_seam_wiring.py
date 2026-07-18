"""Lane-seam wiring for the self-owned agent loops (audit 2026-07-18).

S1 (paper_engine/strategy_agent.py) and the commodity agent
(paper_engine/commodity_strategy_agent.py) run their OWN asyncio tasks outside
supervisor dispatch, so they historically never entered the lane_broker_profile
contextvar (set only at market_hours_paper_supervisor._run_runner) nor the
SLOW/FAST_LANES_ENABLED cadence kill switches consulted by RunnerRuntime.is_due.

These tests pin the wired seam:
  * S1's loop runs run_once under LANE_PROFILE_SLOW and honors
    SLOW_LANES_ENABLED=False by skipping cycles.
  * The commodity loop tags run_once with COMMODITY_AGENT_BROKER_PROFILE
    (default "default") and is killed by SLOW_LANES_ENABLED only when tagged
    "slow", FAST_LANES_ENABLED only when tagged "fast" — the default profile is
    unaffected by either switch.

All of this is latent (no-op routing) while LANE_BROKER_ROUTING_ENABLED=False;
the seam must exist before that flag flips.
"""
from __future__ import annotations

import asyncio

import pytest

import core.config as config
from brokers.rate_limiter import (
    LANE_PROFILE_DEFAULT,
    LANE_PROFILE_SLOW,
    current_lane_profile,
)


# ── Stubs: drive the REAL unbound _loop with a minimal self ───────────────────


class _S1Stub:
    scan_interval_seconds = 0
    _last_error = None
    _last_message = None

    def __init__(self) -> None:
        self.profiles_seen: list[str] = []
        self.run_calls = 0

    async def run_once(self, *, force: bool = False):
        self.run_calls += 1
        self.profiles_seen.append(current_lane_profile())
        raise asyncio.CancelledError  # break out of the infinite loop


class _CommodityStub:
    scan_interval_seconds = 0

    def __init__(self) -> None:
        self._enabled = True
        self._kill_switch_active = False
        self._start_required = False
        self._task = None
        self._last_error = None
        self._last_message = None
        self.profiles_seen: list[str] = []
        self.run_calls = 0

    def _scan_timeout_seconds(self) -> int:
        return 5

    def _append_commentary(self, *_args, **_kwargs) -> None:
        pass

    async def _apersist_state(self) -> None:
        pass

    async def run_once(self, *, force: bool = False):
        self.run_calls += 1
        self.profiles_seen.append(current_lane_profile())
        self._enabled = False  # exit the loop after one cycle


def _cancel_after(monkeypatch, module_path: str, stub, *, attr: str | None = None):
    """Replace asyncio.sleep so the skip-path can't spin forever."""

    async def fake_sleep(_seconds):
        if attr is not None:
            setattr(stub, attr, False)  # flip the loop condition off (commodity)
        else:
            raise asyncio.CancelledError

    monkeypatch.setattr(f"{module_path}.asyncio.sleep", fake_sleep)


# ── S1 seam ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_s1_loop_runs_under_slow_profile(monkeypatch) -> None:
    from paper_engine.strategy_agent import PaperStrategyAgent

    monkeypatch.setattr(config.settings, "SLOW_LANES_ENABLED", True)
    stub = _S1Stub()
    with pytest.raises(asyncio.CancelledError):
        await PaperStrategyAgent._loop(stub)
    assert stub.run_calls == 1
    assert stub.profiles_seen == [LANE_PROFILE_SLOW]
    # The contextvar is restored after the cycle.
    assert current_lane_profile() == LANE_PROFILE_DEFAULT


@pytest.mark.asyncio
async def test_s1_loop_skips_cycles_when_slow_lanes_disabled(monkeypatch) -> None:
    import paper_engine.strategy_agent as s1_module

    monkeypatch.setattr(config.settings, "SLOW_LANES_ENABLED", False)
    stub = _S1Stub()

    sleep_calls = {"n": 0}

    async def fake_sleep(_seconds):
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(s1_module.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await s1_module.PaperStrategyAgent._loop(stub)
    assert stub.run_calls == 0  # kill switch skipped every cycle


# ── Commodity seam ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_commodity_default_profile_ignores_both_kill_switches(monkeypatch) -> None:
    from paper_engine.commodity_strategy_agent import CommodityStrategyAgent

    monkeypatch.setattr(config.settings, "COMMODITY_AGENT_BROKER_PROFILE", "default", raising=False)
    # Both switches OFF — the default profile must still run.
    monkeypatch.setattr(config.settings, "SLOW_LANES_ENABLED", False)
    monkeypatch.setattr(config.settings, "FAST_LANES_ENABLED", False)
    stub = _CommodityStub()
    await CommodityStrategyAgent._loop(stub)
    assert stub.run_calls == 1
    assert stub.profiles_seen == [LANE_PROFILE_DEFAULT]
    assert current_lane_profile() == LANE_PROFILE_DEFAULT


@pytest.mark.asyncio
async def test_commodity_slow_profile_killed_by_slow_switch(monkeypatch) -> None:
    import paper_engine.commodity_strategy_agent as commodity_module

    monkeypatch.setattr(config.settings, "COMMODITY_AGENT_BROKER_PROFILE", "slow", raising=False)
    monkeypatch.setattr(config.settings, "SLOW_LANES_ENABLED", False)
    monkeypatch.setattr(config.settings, "FAST_LANES_ENABLED", True)
    stub = _CommodityStub()

    async def fake_sleep(_seconds):
        stub._enabled = False  # exit after the first skipped cycle

    monkeypatch.setattr(commodity_module.asyncio, "sleep", fake_sleep)
    await commodity_module.CommodityStrategyAgent._loop(stub)
    assert stub.run_calls == 0


@pytest.mark.asyncio
async def test_commodity_slow_profile_runs_when_slow_enabled(monkeypatch) -> None:
    from paper_engine.commodity_strategy_agent import CommodityStrategyAgent

    monkeypatch.setattr(config.settings, "COMMODITY_AGENT_BROKER_PROFILE", "slow", raising=False)
    monkeypatch.setattr(config.settings, "SLOW_LANES_ENABLED", True)
    # FAST switch must NOT kill a slow-tagged lane.
    monkeypatch.setattr(config.settings, "FAST_LANES_ENABLED", False)
    stub = _CommodityStub()
    await CommodityStrategyAgent._loop(stub)
    assert stub.run_calls == 1
    assert stub.profiles_seen == [LANE_PROFILE_SLOW]


@pytest.mark.asyncio
async def test_commodity_fast_profile_killed_by_fast_switch(monkeypatch) -> None:
    import paper_engine.commodity_strategy_agent as commodity_module

    monkeypatch.setattr(config.settings, "COMMODITY_AGENT_BROKER_PROFILE", "fast", raising=False)
    monkeypatch.setattr(config.settings, "SLOW_LANES_ENABLED", True)
    monkeypatch.setattr(config.settings, "FAST_LANES_ENABLED", False)
    stub = _CommodityStub()

    async def fake_sleep(_seconds):
        stub._enabled = False

    monkeypatch.setattr(commodity_module.asyncio, "sleep", fake_sleep)
    await commodity_module.CommodityStrategyAgent._loop(stub)
    assert stub.run_calls == 0
