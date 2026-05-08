from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from api.routers import auth
from api.routers.auth import format_broker_status_summary


@pytest.fixture(autouse=True)
def _reset_broker_snapshot_cache() -> None:
    auth._broker_snapshot_cache["result"] = None
    auth._broker_snapshot_cache["expires_at"] = 0.0


def test_format_broker_status_summary_humanizes_upstox_status() -> None:
    summary = format_broker_status_summary(
        {
            "connected_brokers": ["fyers"],
            "upstox_ready": False,
            "upstox_token_health": {"status": "expired_reconnect_required"},
        }
    )

    assert summary == (
        "Broker Status: FYERS connected | UPSTOX expired reconnect required | "
        "BREEZE disconnected | 5PAISA disconnected"
    )


def test_format_broker_status_summary_marks_upstox_connected_when_ready() -> None:
    summary = format_broker_status_summary(
        {
            "connected_brokers": ["fyers", "upstox"],
            "upstox_ready": True,
            "upstox_token_health": {"status": "valid_no_refresh"},
        }
    )

    assert "UPSTOX connected" in summary


def test_broker_connection_snapshot_only_marks_valid_active_brokers_connected(monkeypatch) -> None:
    async def _noop(*args, **kwargs) -> bool:
        return False

    async def _invalid_upstox(force: bool = False) -> dict:
        return {
            "connected": True,
            "source": "active_session",
            "valid": False,
            "status": "expired_reconnect_required",
            "needs_reconnect": True,
            "message": "Reconnect Upstox.",
        }

    async def _valid_fyers(force: bool = False) -> dict:
        return {
            "connected": True,
            "source": "active_session",
            "valid": True,
            "status": "valid_session",
            "needs_reconnect": False,
            "message": "Fyers token is valid.",
        }

    monkeypatch.setattr(auth, "refresh_persistent_credentials", lambda force=False: None)
    monkeypatch.setattr(auth, "ensure_upstox_session", _noop)
    monkeypatch.setattr(auth, "ensure_fyers_session", _noop)
    monkeypatch.setattr(auth, "get_connected_brokers", lambda: ["fyers", "upstox", "icici_breeze"])
    monkeypatch.setattr(auth, "get_upstox_token_health", _invalid_upstox)
    monkeypatch.setattr(auth, "get_fyers_token_health", _valid_fyers)

    snapshot = asyncio.run(auth.get_broker_connection_snapshot())

    assert snapshot["connected_brokers"] == ["fyers", "icici_breeze"]
    assert snapshot["session_brokers"] == ["fyers", "upstox", "icici_breeze"]
    assert snapshot["fyers_ready"] is True
    assert snapshot["upstox_ready"] is False


def test_broker_status_surfaces_reconnect_state_even_if_session_object_exists(monkeypatch) -> None:
    monkeypatch.setattr(auth, "refresh_persistent_credentials", lambda force=False: None)
    monkeypatch.setattr(auth, "_persist_active_session_tokens", lambda: None)
    monkeypatch.setattr(
        auth,
        "get_broker_connection_snapshot",
        lambda force_validate=False: asyncio.sleep(
            0,
            result={
                "connected_brokers": [],
                "session_brokers": ["fyers", "upstox"],
                "upstox_ready": False,
                "fyers_ready": False,
                "broker_ready": False,
                "upstox_token_health": {
                    "status": "expired_reconnect_required",
                    "needs_reconnect": True,
                    "message": "Reconnect Upstox in Settings.",
                    "source": "active_session",
                    "checked_at": "2026-04-17T04:10:00+00:00",
                },
                "fyers_token_health": {
                    "status": "expired_reconnect_required",
                    "needs_reconnect": True,
                    "message": "Reconnect Fyers in Settings.",
                    "source": "active_session",
                    "checked_at": "2026-04-17T04:09:00+00:00",
                },
            },
        ),
    )
    monkeypatch.setattr(
        auth,
        "_active_brokers",
        {
            "fyers": {
                "profile": SimpleNamespace(user_id="FY123", name="Fyers User"),
                "connected_at": "2026-04-17T03:00:00+00:00",
            },
            "upstox": {
                "profile": SimpleNamespace(user_id="UP123", name="Upstox User"),
                "connected_at": "2026-04-17T03:05:00+00:00",
            },
        },
    )

    statuses = asyncio.run(auth.broker_status())
    status_by_broker = {item.broker: item for item in statuses}

    assert status_by_broker["fyers"].connected is False
    assert status_by_broker["fyers"].ready is False
    assert status_by_broker["fyers"].session_active is True
    assert status_by_broker["fyers"].state == "expired_reconnect_required"
    assert status_by_broker["fyers"].needs_reconnect is True
    assert status_by_broker["fyers"].checked_at == "2026-04-17T04:09:00+00:00"
    assert status_by_broker["upstox"].connected is False
    assert status_by_broker["upstox"].session_active is True
    assert status_by_broker["upstox"].detail == "Reconnect Upstox in Settings."
    assert status_by_broker["upstox"].checked_at == "2026-04-17T04:10:00+00:00"


def test_broker_status_can_force_validation(monkeypatch) -> None:
    calls: list[bool] = []

    monkeypatch.setattr(auth, "refresh_persistent_credentials", lambda force=False: None)
    monkeypatch.setattr(auth, "_persist_active_session_tokens", lambda: None)

    async def fake_snapshot(*, force_validate: bool = False):
        calls.append(force_validate)
        return {
            "connected_brokers": [],
            "session_brokers": [],
            "upstox_ready": False,
            "fyers_ready": False,
            "upstox_token_health": {},
            "fyers_token_health": {},
        }

    monkeypatch.setattr(auth, "get_broker_connection_snapshot", fake_snapshot)
    monkeypatch.setattr(auth, "_active_brokers", {})

    asyncio.run(auth.broker_status(force_validate=True))

    assert calls == [True]


def test_broker_status_read_path_does_not_persist_active_tokens(monkeypatch) -> None:
    monkeypatch.setattr(auth, "refresh_persistent_credentials", lambda force=False: None)

    async def fake_snapshot(*, force_validate: bool = False):
        return {
            "connected_brokers": ["fyers"],
            "session_brokers": ["fyers"],
            "upstox_ready": False,
            "fyers_ready": True,
            "upstox_token_health": {},
            "fyers_token_health": {"status": "valid_session"},
        }

    def fail_persist() -> None:
        raise AssertionError("broker_status should not persist tokens on a read")

    monkeypatch.setattr(auth, "get_broker_connection_snapshot", fake_snapshot)
    monkeypatch.setattr(auth, "_persist_active_session_tokens", fail_persist)
    monkeypatch.setattr(auth, "_active_brokers", {})

    statuses = asyncio.run(auth.broker_status())

    assert any(item.broker == "fyers" and item.connected for item in statuses)


def test_broker_connection_snapshot_reuses_short_lived_cache(monkeypatch) -> None:
    calls = {"upstox": 0, "fyers": 0}
    auth._broker_snapshot_cache["result"] = None
    auth._broker_snapshot_cache["expires_at"] = 0.0

    monkeypatch.setattr(auth, "refresh_persistent_credentials", lambda force=False: None)
    monkeypatch.setattr(auth, "ensure_upstox_session", lambda force_validate=False: asyncio.sleep(0, result=False))
    monkeypatch.setattr(auth, "ensure_fyers_session", lambda force_validate=False: asyncio.sleep(0, result=False))
    monkeypatch.setattr(auth, "get_connected_brokers", lambda: ["fyers"])

    async def _upstox_health(force: bool = False) -> dict:
        calls["upstox"] += 1
        return {"valid": False}

    async def _fyers_health(force: bool = False) -> dict:
        calls["fyers"] += 1
        return {"valid": True}

    monkeypatch.setattr(auth, "get_upstox_token_health", _upstox_health)
    monkeypatch.setattr(auth, "get_fyers_token_health", _fyers_health)

    first = asyncio.run(auth.get_broker_connection_snapshot())
    second = asyncio.run(auth.get_broker_connection_snapshot())

    assert first["connected_brokers"] == ["fyers"]
    assert second["connected_brokers"] == ["fyers"]
    assert calls == {"upstox": 1, "fyers": 1}


def test_broker_connection_snapshot_times_out_slow_health_checks(monkeypatch) -> None:
    async def _noop(*args, **kwargs) -> bool:
        return False

    async def _slow_health(force: bool = False) -> dict:
        await asyncio.sleep(10)
        return {"valid": False}

    monkeypatch.setattr(auth, "_BROKER_SNAPSHOT_SESSION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(auth, "_BROKER_SNAPSHOT_HEALTH_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(auth, "refresh_persistent_credentials", lambda force=False: None)
    monkeypatch.setattr(auth, "ensure_upstox_session", _noop)
    monkeypatch.setattr(auth, "ensure_fyers_session", _noop)
    monkeypatch.setattr(auth, "get_connected_brokers", lambda: ["fyers", "upstox"])
    monkeypatch.setattr(auth, "get_upstox_token_health", _slow_health)
    monkeypatch.setattr(auth, "get_fyers_token_health", _slow_health)

    snapshot = asyncio.run(auth.get_broker_connection_snapshot())

    assert snapshot["connected_brokers"] == ["fyers", "upstox"]
    assert snapshot["broker_ready"] is True
    assert snapshot["upstox_token_health"]["status"] == "validation_timeout"
    assert snapshot["fyers_token_health"]["status"] == "validation_timeout"
