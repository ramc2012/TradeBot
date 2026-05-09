from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from api.routers import trading
from core import paper_bootstrap


class _FakeLiveManager:
    def __init__(self) -> None:
        self.stopped = False

    async def stop_reconciliation(self) -> None:
        self.stopped = True


class _FakeNSEAgent:
    def __init__(self) -> None:
        self.control = {
            "kill_switch_active": True,
            "auto_run_enabled": False,
            "loop_active": False,
        }
        self.recovered = False

    async def ensure_recovered_state(self) -> None:
        self.recovered = True

    def get_control_state(self, *, cancelled_orders: int = 0) -> dict:
        return dict(self.control)

    def set_kill_switch(self, active: bool) -> dict:
        self.control["kill_switch_active"] = bool(active)
        return self.get_control_state()

    async def set_auto_run(self, enabled: bool) -> dict:
        self.control["auto_run_enabled"] = bool(enabled)
        self.control["loop_active"] = bool(enabled)
        return self.get_control_state()

    def get_status(self) -> dict:
        return {
            "kill_switch_active": self.control["kill_switch_active"],
            "auto_run_enabled": self.control["auto_run_enabled"],
            "loop_active": self.control["loop_active"],
        }


class _FakeCommodityAgent:
    def __init__(self) -> None:
        self.control = {
            "kill_switch_active": True,
            "auto_run_enabled": True,
            "loop_active": False,
            "start_required": True,
        }
        self.started_with_force = False

    def get_control_state(self, *, cancelled_orders: int = 0) -> dict:
        return dict(self.control)

    async def set_kill_switch(self, active: bool) -> dict:
        self.control["kill_switch_active"] = bool(active)
        return self.get_control_state()

    async def start(self, *, force: bool = False) -> None:
        self.started_with_force = force
        self.control["start_required"] = False
        self.control["loop_active"] = True

    def get_status(self) -> dict:
        return {
            "kill_switch_active": self.control["kill_switch_active"],
            "auto_run_enabled": self.control["auto_run_enabled"],
            "loop_active": self.control["loop_active"],
            "start_required": self.control["start_required"],
            "summary": {"tracked_symbols": 4},
        }

    def get_symbols(self) -> list[str]:
        return ["MCX:GOLD26JUNFUT"]


def test_set_mode_blocks_live_when_paper_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trading.settings, "PAPER_TRADING_ONLY", True)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(trading.set_mode(trading.SetModeRequest(mode="live", broker="fyers")))

    assert excinfo.value.status_code == 403
    assert "Paper mode only" in str(excinfo.value.detail)


def test_ensure_paper_trading_mode_clears_live_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_manager = _FakeLiveManager()
    monkeypatch.setattr(trading, "_mode", "live")
    monkeypatch.setattr(trading, "_active_broker", "upstox")
    monkeypatch.setattr(trading, "_live_manager", fake_manager)
    monkeypatch.setattr(trading.settings, "PAPER_TRADING_ONLY", True)

    payload = asyncio.run(trading.ensure_paper_trading_mode(preferred_broker="fyers"))

    assert payload == {
        "mode": "paper",
        "broker": "fyers",
        "paper_only": True,
        "live_manager_active": False,
    }
    assert fake_manager.stopped is True
    assert trading._mode == "paper"
    assert trading._active_broker == "fyers"
    assert trading._live_manager is None


def test_bootstrap_paper_trading_runtime_normalizes_supervisors(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.routers import auth as auth_module
    from api.routers import trading as trading_module
    from paper_engine import commodity_strategy_agent as commodity_module
    from paper_engine import strategy_agent as strategy_module

    fake_nse = _FakeNSEAgent()
    fake_commodity = _FakeCommodityAgent()

    monkeypatch.setattr(paper_bootstrap.settings, "PAPER_TRADING_ONLY", True)
    monkeypatch.setattr(paper_bootstrap.settings, "PAPER_RUNTIME_PREWARM_ENABLED", False)
    monkeypatch.setattr(
        auth_module,
        "get_broker_connection_snapshot",
        lambda force_validate=False: asyncio.sleep(0, result={"fyers_ready": True, "upstox_ready": False}),
    )
    monkeypatch.setattr(
        trading_module,
        "ensure_paper_trading_mode",
        lambda preferred_broker=None: asyncio.sleep(
            0,
            result={
                "mode": "paper",
                "broker": preferred_broker or "fyers",
                "paper_only": True,
                "live_manager_active": False,
            },
        ),
    )
    monkeypatch.setattr(strategy_module, "paper_strategy_agent", fake_nse)
    monkeypatch.setattr(commodity_module, "commodity_strategy_agent", fake_commodity)

    payload = asyncio.run(paper_bootstrap.bootstrap_paper_trading_runtime())

    assert payload["enabled"] is True
    assert payload["paper_only"] is True
    assert payload["trading_mode"]["mode"] == "paper"
    assert payload["nse"]["kill_switch_active"] is False
    assert payload["nse"]["loop_active"] is True
    assert payload["commodity"]["kill_switch_active"] is False
    assert payload["commodity"]["loop_active"] is True
    assert payload["commodity"]["start_required"] is False
    assert fake_nse.recovered is True
    assert fake_commodity.started_with_force is True
