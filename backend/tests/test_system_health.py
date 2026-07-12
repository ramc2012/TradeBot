from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from api.routers import system


@pytest.fixture(autouse=True)
def _reset_system_route_caches() -> None:
    system._health_cache["payload"] = None
    system._health_cache["expires_at"] = 0.0
    system._overview_cache["payload"] = None
    system._overview_cache["expires_at"] = 0.0


@pytest.fixture(autouse=True)
def _stub_notifications_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """These are hermetic aggregation tests — every service builder is stubbed.
    The notifications builder reads the process-global Telegram singleton,
    whose health can be mutated mid-test by background sender loops leaked
    from earlier test modules (real 401s against the currently-revoked bot
    token). Stub it like every other builder."""
    monkeypatch.setattr(
        system,
        "_notifications_service",
        lambda: {
            "key": "notifications",
            "label": "Telegram Notifications",
            "status": "idle",
            "detail": "stubbed in tests",
            "meta": {},
        },
    )


class _FakeResult:
    def scalar_one_or_none(self) -> int:
        return 1


class _FakeSession:
    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def execute(self, _query):
        return _FakeResult()


class _FakeRedis:
    async def ping(self) -> bool:
        return True


class _FakeStrategySupervisor:
    def __init__(self, payload: dict):
        self._payload = payload

    def get_status(self) -> dict:
        return self._payload


@pytest.mark.asyncio
async def test_system_health_reports_healthy_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(timezone.utc)

    monkeypatch.setattr(system, "AsyncSessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(system, "get_redis", lambda: _async_value(_FakeRedis()))
    monkeypatch.setattr(
        system,
        "_load_research_sync_runtime_state",
        lambda: {
            "state": "waiting",
            "run_completed_at": (now - timedelta(minutes=15)).isoformat(),
            "next_run_at": (now + timedelta(minutes=15)).isoformat(),
        },
    )
    monkeypatch.setattr(
        system,
        "get_broker_connection_snapshot",
        lambda force_validate=False: _async_result(
            {
                "connected_brokers": ["fyers"],
                "broker_ready": True,
                "upstox_ready": False,
                "fyers_ready": True,
                "upstox_token_health": {"status": "missing", "valid": False},
                "fyers_token_health": {"status": "valid_session", "valid": True},
            }
        ),
    )
    monkeypatch.setattr(
        system.data_router,
        "get_status",
        lambda: {
            "mode": "broker",
            "broker": "fyers",
            "subscribed_symbols": ["NIFTY", "BANKNIFTY"],
            "subscribed_symbol_count": 2,
            "tick_buffer_size": 2,
            "callback_count": 4,
            "ws_connected": True,
            "mock_running": False,
            "last_tick_at": now.isoformat(),
        },
    )
    monkeypatch.setattr(
        system,
        "paper_strategy_agent",
        _FakeStrategySupervisor(
            {
                "enabled": True,
                "running": True,
                "auto_run_enabled": True,
                "kill_switch_active": False,
                "scan_interval_seconds": 60,
                "last_run_at": now.isoformat(),
                "next_scan_at": (now + timedelta(minutes=1)).isoformat(),
                "last_message": "Scan loop healthy.",
                "strategy_agents": [
                    {"key": "macd_strategy", "label": "ATM MACD", "timeframe": "30minute", "scope": "ATM options"}
                ],
                "strategies": [
                    {
                        "key": "macd_strategy",
                        "label": "ATM MACD",
                        "summary": {"open_positions": 1},
                        "last_scan_at": now.isoformat(),
                    }
                ],
                "data_health": {"broker_snapshot": {"broker_ready": True}},
            }
        ),
    )
    monkeypatch.setattr(
        system,
        "commodity_strategy_agent",
        _FakeStrategySupervisor(
            {
                "enabled": True,
                "running": True,
                "auto_run_enabled": True,
                "kill_switch_active": False,
                "scan_interval_seconds": 30,
                "last_run_at": now.isoformat(),
                "last_message": "Commodity loop healthy.",
                "strategy_agents": [
                    {"key": "commodity_futures", "label": "Commodity Futures", "timeframe": "15minute", "scope": "MCX futures"}
                ],
                "strategies": [
                    {
                        "key": "commodity_futures",
                        "label": "Commodity Futures",
                        "summary": {"open_positions": 0},
                        "last_scan_at": now.isoformat(),
                    }
                ],
                "data_health": {"fyers_token_health": {"valid": True}},
            }
        ),
    )
    monkeypatch.setattr(
        system,
        "auction_intelligence_summary",
        lambda: _async_result(
            {
                "connected_brokers": ["fyers"],
                "live_ready": True,
                "deployable_first_sleeve": "swing",
                "validation_gates": [{"id": "gate_a", "status": "available"}],
            }
        ),
    )
    monkeypatch.setattr(
        system,
        "_fractal_market_profile_service",
        lambda: _async_result(
            {
                "key": "fractal_market_profile",
                "label": "Fractal Market Profile",
                "status": "healthy",
                "detail": "Recent minute history is available.",
                "meta": {"symbol_code": "NIFTY", "session_count": 2},
            }
        ),
    )

    payload = await system.system_health()

    assert payload["summary"]["status"] == "healthy"
    assert payload["summary"]["critical_services"] == 0
    assert len(payload["services"]) == 11  # + notifications (telegram delivery health)
    assert any(service["key"] == "notifications" for service in payload["services"])
    assert any(service["key"] == "brokers" and service["status"] == "healthy" for service in payload["services"])
    assert any(service["key"] == "fractal_market_profile" and service["status"] == "healthy" for service in payload["services"])
    assert any(lane["key"] == "nse_strategy:macd_strategy" for lane in payload["strategy_lanes"])


@pytest.mark.asyncio
async def test_system_health_marks_missing_brokers_critical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system, "AsyncSessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(system, "get_redis", lambda: _async_value(_FakeRedis()))
    monkeypatch.setattr(system, "_load_research_sync_runtime_state", lambda: {})
    monkeypatch.setattr(
        system,
        "get_broker_connection_snapshot",
        lambda force_validate=False: _async_result(
            {
                "connected_brokers": [],
                "broker_ready": False,
                "upstox_ready": False,
                "fyers_ready": False,
                "upstox_token_health": {"status": "missing", "valid": False},
                "fyers_token_health": {"status": "missing", "valid": False},
            }
        ),
    )
    monkeypatch.setattr(
        system.data_router,
        "get_status",
        lambda: {
            "mode": "idle",
            "broker": None,
            "subscribed_symbols": [],
            "subscribed_symbol_count": 0,
            "tick_buffer_size": 0,
            "callback_count": 0,
            "ws_connected": False,
            "mock_running": False,
            "last_tick_at": None,
        },
    )
    monkeypatch.setattr(system, "paper_strategy_agent", _FakeStrategySupervisor({"enabled": False, "running": False, "strategies": [], "strategy_agents": []}))
    monkeypatch.setattr(system, "commodity_strategy_agent", _FakeStrategySupervisor({"enabled": False, "running": False, "strategies": [], "strategy_agents": []}))
    monkeypatch.setattr(
        system,
        "auction_intelligence_summary",
        lambda: _async_result(
            {
                "connected_brokers": [],
                "live_ready": False,
                "deployable_first_sleeve": "swing",
                "validation_gates": [{"id": "gate_a", "status": "available"}],
            }
        ),
    )
    monkeypatch.setattr(
        system,
        "_fractal_market_profile_service",
        lambda: _async_result(
            {
                "key": "fractal_market_profile",
                "label": "Fractal Market Profile",
                "status": "degraded",
                "detail": "Live snapshot unavailable.",
                "meta": {"symbol_code": "NIFTY"},
            }
        ),
    )

    payload = await system.system_health()

    assert payload["summary"]["status"] == "critical"
    assert any(service["key"] == "brokers" and service["status"] == "critical" for service in payload["services"])


@pytest.mark.asyncio
async def test_system_health_reuses_short_lived_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"postgres": 0}
    system._health_cache["payload"] = None
    system._health_cache["expires_at"] = 0.0

    async def _postgres():
        calls["postgres"] += 1
        return {"key": "postgres", "label": "Postgres", "status": "healthy", "detail": "ok", "meta": {}}

    monkeypatch.setattr(system, "_postgres_service", _postgres)
    monkeypatch.setattr(system, "_redis_service", lambda: _async_result({"key": "redis", "label": "Redis", "status": "healthy", "detail": "ok", "meta": {}}))
    monkeypatch.setattr(system, "_research_sync_service", lambda now_utc: {"key": "research_sync", "label": "Research Sync", "status": "healthy", "detail": "ok", "meta": {}})
    monkeypatch.setattr(system, "_broker_service", lambda now_utc: _async_result({"key": "brokers", "label": "Brokers", "status": "healthy", "detail": "ok", "meta": {}}))
    monkeypatch.setattr(system, "_market_data_service", lambda: {"key": "market_data", "label": "Market Data", "status": "healthy", "detail": "ok", "meta": {}})
    monkeypatch.setattr(system, "_strategy_service", lambda key, label, status: ({"key": key, "label": label, "status": "healthy", "detail": "ok", "meta": {}}, []))
    monkeypatch.setattr(system, "_auction_service", lambda: _async_result({"key": "auction_intelligence", "label": "Auction Intelligence", "status": "healthy", "detail": "ok", "meta": {}}))
    monkeypatch.setattr(system, "_fractal_market_profile_service", lambda: _async_result({"key": "fractal_market_profile", "label": "Fractal Market Profile", "status": "healthy", "detail": "ok", "meta": {}}))
    monkeypatch.setattr(system.paper_strategy_agent, "get_status", lambda: {})
    monkeypatch.setattr(system.commodity_strategy_agent, "get_status", lambda: {})

    first = await system.system_health()
    second = await system.system_health()

    assert first["summary"]["status"] == "healthy"
    assert second["summary"]["status"] == "healthy"
    assert calls["postgres"] == 1


@pytest.mark.asyncio
async def test_system_health_marks_stale_supervisor_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(timezone.utc)

    monkeypatch.setattr(system, "AsyncSessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(system, "get_redis", lambda: _async_value(_FakeRedis()))
    monkeypatch.setattr(
        system,
        "_load_research_sync_runtime_state",
        lambda: {
            "state": "waiting",
            "run_completed_at": (now - timedelta(minutes=10)).isoformat(),
            "next_run_at": (now + timedelta(minutes=20)).isoformat(),
        },
    )
    monkeypatch.setattr(system, "_in_market_hours", lambda _now: True)
    monkeypatch.setattr(system, "_in_commodity_hours", lambda _now: True)
    monkeypatch.setattr(
        system,
        "get_broker_connection_snapshot",
        lambda force_validate=False: _async_result(
            {
                "connected_brokers": ["fyers", "upstox"],
                "broker_ready": True,
                "upstox_ready": True,
                "fyers_ready": True,
                "upstox_token_health": {"status": "valid", "valid": True},
                "fyers_token_health": {"status": "valid_session", "valid": True},
            }
        ),
    )
    monkeypatch.setattr(
        system.data_router,
        "get_status",
        lambda: {
            "mode": "broker",
            "broker": "fyers",
            "subscribed_symbols": ["NIFTY"],
            "subscribed_symbol_count": 1,
            "tick_buffer_size": 1,
            "callback_count": 1,
            "ws_connected": True,
            "mock_running": False,
            "last_tick_at": now.isoformat(),
        },
    )
    stale_scan = now - timedelta(minutes=12)
    monkeypatch.setattr(
        system,
        "paper_strategy_agent",
        _FakeStrategySupervisor(
            {
                "enabled": True,
                "running": True,
                "auto_run_enabled": True,
                "kill_switch_active": False,
                "scan_interval_seconds": 60,
                "last_run_at": stale_scan.isoformat(),
                "next_scan_at": (stale_scan + timedelta(minutes=1)).isoformat(),
                "last_message": "Scan loop healthy.",
                "strategy_agents": [
                    {"key": "macd_strategy", "label": "ATM MACD", "timeframe": "30minute", "scope": "ATM options"}
                ],
                "strategies": [
                    {
                        "key": "macd_strategy",
                        "label": "ATM MACD",
                        "summary": {"open_positions": 1},
                        "last_scan_at": stale_scan.isoformat(),
                    }
                ],
                "data_health": {"broker_snapshot": {"broker_ready": True}},
            }
        ),
    )
    monkeypatch.setattr(
        system,
        "commodity_strategy_agent",
        _FakeStrategySupervisor(
            {
                "enabled": True,
                "running": True,
                "auto_run_enabled": True,
                "kill_switch_active": False,
                "scan_interval_seconds": 30,
                "last_run_at": now.isoformat(),
                "last_message": "Commodity loop healthy.",
                "strategy_agents": [],
                "strategies": [],
                "data_health": {},
            }
        ),
    )
    monkeypatch.setattr(
        system,
        "auction_intelligence_summary",
        lambda: _async_result(
            {
                "connected_brokers": ["fyers"],
                "live_ready": True,
                "deployable_first_sleeve": "swing",
                "validation_gates": [{"id": "gate_a", "status": "available"}],
            }
        ),
    )
    monkeypatch.setattr(
        system,
        "_fractal_market_profile_service",
        lambda: _async_result(
            {
                "key": "fractal_market_profile",
                "label": "Fractal Market Profile",
                "status": "healthy",
                "detail": "Recent minute history is available.",
                "meta": {"symbol_code": "NIFTY", "session_count": 2},
            }
        ),
    )

    payload = await system.system_health()

    nse_service = next(service for service in payload["services"] if service["key"] == "nse_strategy")
    nse_lane = next(lane for lane in payload["strategy_lanes"] if lane["key"] == "nse_strategy:macd_strategy")

    assert payload["summary"]["status"] == "degraded"
    assert nse_service["status"] == "degraded"
    assert nse_service["meta"]["mode"] == "stale"
    assert nse_service["meta"]["last_run_age_seconds"] > 300
    assert nse_lane["status"] == "stale"


@pytest.mark.asyncio
async def test_system_overview_excludes_idle_services_from_blockers(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(timezone.utc)

    monkeypatch.setattr(system, "AsyncSessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(system, "get_redis", lambda: _async_value(_FakeRedis()))
    monkeypatch.setattr(
        system,
        "_load_research_sync_runtime_state",
        lambda: {
            "state": "waiting",
            "run_completed_at": (now - timedelta(minutes=15)).isoformat(),
            "next_run_at": (now + timedelta(minutes=15)).isoformat(),
        },
    )
    monkeypatch.setattr(
        system,
        "get_broker_connection_snapshot",
        lambda force_validate=False: _async_result(
            {
                "connected_brokers": ["fyers"],
                "broker_ready": True,
                "upstox_ready": False,
                "fyers_ready": True,
                "upstox_token_health": {"status": "missing", "valid": False},
                "fyers_token_health": {"status": "valid_session", "valid": True},
            }
        ),
    )
    monkeypatch.setattr(
        system.data_router,
        "get_status",
        lambda: {
            "mode": "broker",
            "broker": "fyers",
            "subscribed_symbols": ["NIFTY"],
            "subscribed_symbol_count": 1,
            "tick_buffer_size": 1,
            "callback_count": 1,
            "ws_connected": True,
            "mock_running": False,
            "last_tick_at": now.isoformat(),
        },
    )
    monkeypatch.setattr(system, "_in_market_hours", lambda _dt: False)
    monkeypatch.setattr(system, "_in_commodity_hours", lambda _dt: False)
    monkeypatch.setattr(
        system,
        "paper_strategy_agent",
        _FakeStrategySupervisor(
            {
                "enabled": True,
                "running": False,
                "loop_active": False,
                "auto_run_enabled": True,
                "kill_switch_active": False,
                "scan_interval_seconds": 60,
                "last_run_at": now.isoformat(),
                "strategies": [],
                "strategy_agents": [],
            }
        ),
    )
    monkeypatch.setattr(
        system,
        "commodity_strategy_agent",
        _FakeStrategySupervisor(
            {
                "enabled": True,
                "running": False,
                "loop_active": False,
                "auto_run_enabled": True,
                "kill_switch_active": False,
                "scan_interval_seconds": 30,
                "last_run_at": now.isoformat(),
                "strategies": [],
                "strategy_agents": [],
            }
        ),
    )
    monkeypatch.setattr(
        system,
        "auction_intelligence_summary",
        lambda: _async_result(
            {
                "connected_brokers": ["fyers"],
                "live_ready": True,
                "deployable_first_sleeve": "swing",
                "validation_gates": [],
            }
        ),
    )
    monkeypatch.setattr(
        system,
        "_fractal_market_profile_service",
        lambda: _async_result(
            {
                "key": "fractal_market_profile",
                "label": "Fractal Market Profile",
                "status": "healthy",
                "detail": "Recent minute history is available.",
                "meta": {"symbol_code": "NIFTY", "session_count": 2},
            }
        ),
    )
    async def _fake_manual_book_summary() -> dict:
        return {"total_pnl": 0.0, "total_trades": 0, "open_positions": 0, "open_pnl": 0.0}

    monkeypatch.setattr(system, "_manual_book_summary", _fake_manual_book_summary)

    class _FakeRiskManager:
        def get_status(self) -> dict:
            return {"trading_allowed": True, "open_positions": 0, "max_positions": 6}

    monkeypatch.setattr(system, "_risk_manager", _FakeRiskManager())

    payload = await system.system_overview()

    assert payload["health"]["summary"]["service_counts"]["idle"] >= 2
    assert payload["blockers"] == []


@pytest.mark.asyncio
async def test_system_overview_counts_top_level_strategy_positions(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(timezone.utc)

    monkeypatch.setattr(system, "AsyncSessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(system, "get_redis", lambda: _async_value(_FakeRedis()))
    monkeypatch.setattr(system, "_load_research_sync_runtime_state", lambda: {})
    monkeypatch.setattr(
        system,
        "get_broker_connection_snapshot",
        lambda force_validate=False: _async_result(
            {
                "connected_brokers": ["fyers"],
                "broker_ready": True,
                "upstox_ready": False,
                "fyers_ready": True,
                "upstox_token_health": {"status": "missing", "valid": False},
                "fyers_token_health": {"status": "valid_session", "valid": True},
            }
        ),
    )
    monkeypatch.setattr(
        system.data_router,
        "get_status",
        lambda: {
            "mode": "broker",
            "broker": "fyers",
            "subscribed_symbols": ["NIFTY"],
            "subscribed_symbol_count": 1,
            "tick_buffer_size": 1,
            "callback_count": 1,
            "ws_connected": True,
            "mock_running": False,
            "last_tick_at": now.isoformat(),
        },
    )
    monkeypatch.setattr(system, "_in_market_hours", lambda _dt: True)
    monkeypatch.setattr(system, "_in_commodity_hours", lambda _dt: True)
    monkeypatch.setattr(
        system,
        "paper_strategy_agent",
        _FakeStrategySupervisor(
            {
                "enabled": True,
                "running": True,
                "loop_active": True,
                "auto_run_enabled": True,
                "kill_switch_active": False,
                "scan_interval_seconds": 60,
                "last_run_at": now.isoformat(),
                "strategies": [
                    {
                        "key": "macd_strategy",
                        "summary": {"open_positions": 2},
                    }
                ],
                "strategy_agents": [
                    {"key": "macd_strategy", "label": "Strategy 1", "timeframe": "30minute"}
                ],
            }
        ),
    )
    monkeypatch.setattr(
        system,
        "commodity_strategy_agent",
        _FakeStrategySupervisor(
            {
                "enabled": True,
                "running": True,
                "loop_active": True,
                "auto_run_enabled": True,
                "kill_switch_active": False,
                "scan_interval_seconds": 30,
                "last_run_at": now.isoformat(),
                "strategies": [
                    {
                        "key": "commodity_futures",
                        "title": "Strategy 2 · Futures",
                        "open_positions": 1,
                    }
                ],
                "strategy_agents": [
                    {"key": "commodity_futures", "title": "Strategy 2 · Futures", "timeframe": "15minute"}
                ],
            }
        ),
    )
    monkeypatch.setattr(
        system,
        "auction_intelligence_summary",
        lambda: _async_result(
            {
                "connected_brokers": ["fyers"],
                "live_ready": True,
                "deployable_first_sleeve": "swing",
                "validation_gates": [],
            }
        ),
    )
    monkeypatch.setattr(
        system,
        "_fractal_market_profile_service",
        lambda: _async_result(
            {
                "key": "fractal_market_profile",
                "label": "Fractal Market Profile",
                "status": "degraded",
                "detail": "Live snapshot unavailable.",
                "meta": {"symbol_code": "NIFTY"},
            }
        ),
    )

    async def _fake_manual_book_summary() -> dict:
        return {"total_pnl": 0.0, "total_trades": 0, "open_positions": 0, "open_pnl": 0.0}

    monkeypatch.setattr(system, "_manual_book_summary", _fake_manual_book_summary)

    class _FakeRiskManager:
        def get_status(self) -> dict:
            return {"trading_allowed": True, "open_positions": 0, "max_positions": 6}

    monkeypatch.setattr(system, "_risk_manager", _FakeRiskManager())

    payload = await system.system_overview()
    commodity_service = next(item for item in payload["health"]["services"] if item["key"] == "commodity_strategy")

    assert payload["books"]["combined"]["open_positions"] == 3
    assert payload["books"]["commodity_strategy"]["status"]["running"] is True
    assert commodity_service["meta"]["open_positions"] == 1
    assert any(item["key"] == "fractal_market_profile" for item in payload["blockers"])


async def _async_result(payload: dict) -> dict:
    return payload


async def _async_value(payload):
    return payload
