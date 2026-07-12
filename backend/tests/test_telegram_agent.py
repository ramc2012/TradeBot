"""Telegram delivery health — the 401-invisibility fix.

A rotated/revoked bot token must be a visible fact (health, metrics, one audit
event per day), never a silent zero-notification day.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import settings
from notifications.telegram_agent import TelegramAgent

# Opt out of the suite-wide class-level send stub (tests/conftest.py) — this
# module tests the real send logic against mocked httpx.
pytestmark = pytest.mark.telegram_singleton_live


def _agent_with_creds(monkeypatch) -> TelegramAgent:
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "123:abc", raising=False)
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "-10042", raising=False)
    monkeypatch.setattr(settings, "TELEGRAM_RATE_LIMIT_PER_MINUTE", 12, raising=False)
    return TelegramAgent()


def _mock_response(status_code: int, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    return resp


def _client_returning(resp) -> MagicMock:
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=resp)
    return client


@pytest.mark.asyncio
async def test_401_sets_auth_health_and_emits_one_audit_event(monkeypatch) -> None:
    agent = _agent_with_creds(monkeypatch)
    audit = AsyncMock()
    with patch("httpx.AsyncClient", return_value=_client_returning(_mock_response(401, "Unauthorized"))), patch(
        "agentic_rag.audit_agent.record_audit_event", audit
    ):
        assert await agent.send("hello", parse_mode=None) is False
        assert await agent.send("hello again", parse_mode=None) is False

    health = agent.get_health()
    assert health["auth_failed"] is True
    assert health["last_error_status"] == 401
    assert health["failed_auth"] == 2
    assert health["consecutive_failures"] == 2
    assert health["last_failure_at"] is not None
    # One audit event per day, not per failed send.
    assert audit.await_count == 1
    kwargs = audit.await_args.kwargs
    assert kwargs["event_type"] == "telegram_auth_failed"
    assert kwargs["severity"] == "error"


@pytest.mark.asyncio
async def test_success_resets_consecutive_failures(monkeypatch) -> None:
    agent = _agent_with_creds(monkeypatch)
    with patch("httpx.AsyncClient", return_value=_client_returning(_mock_response(500, "boom"))):
        await agent.send("first", parse_mode=None)
    assert agent.get_health()["consecutive_failures"] == 1

    with patch("httpx.AsyncClient", return_value=_client_returning(_mock_response(200))):
        assert await agent.send("second", parse_mode=None) is True

    health = agent.get_health()
    assert health["sent_ok"] == 1
    assert health["consecutive_failures"] == 0
    assert health["auth_failed"] is False
    assert health["last_success_at"] is not None


@pytest.mark.asyncio
async def test_auth_alert_reentrancy_guard_set_before_emit(monkeypatch) -> None:
    """The audit bridge fans events back into Telegram — the day stamp must be
    set before record_audit_event is awaited so the re-entrant send can't loop."""
    agent = _agent_with_creds(monkeypatch)
    seen: list[date | None] = []

    async def _audit(**_kwargs):
        seen.append(agent._auth_alert_date)

    with patch("httpx.AsyncClient", return_value=_client_returning(_mock_response(403, "Forbidden"))), patch(
        "agentic_rag.audit_agent.record_audit_event", side_effect=_audit
    ):
        await agent.send("x", parse_mode=None)

    assert seen and seen[0] is not None


@pytest.mark.asyncio
async def test_missing_creds_counts_suppression(monkeypatch) -> None:
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "", raising=False)
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "", raising=False)
    agent = TelegramAgent()
    assert await agent.send("nobody home") is False
    assert agent.get_health()["suppressed_no_creds"] == 1


@pytest.mark.asyncio
async def test_priority_sends_get_larger_rate_allowance(monkeypatch) -> None:
    agent = _agent_with_creds(monkeypatch)
    monkeypatch.setattr(settings, "TELEGRAM_RATE_LIMIT_PER_MINUTE", 2, raising=False)
    with patch("httpx.AsyncClient", return_value=_client_returning(_mock_response(200))):
        assert await agent.send("a", parse_mode=None) is True
        assert await agent.send("b", parse_mode=None) is True
        # Normal send is over the 2/min cap...
        assert await agent.send("c", parse_mode=None) is False
        # ...but a priority (trade-alert) send still goes out.
        assert await agent.send("d", parse_mode=None, priority=True) is True
    assert agent.get_health()["suppressed_rate_limit"] == 1


@pytest.mark.asyncio
async def test_s1_trade_alert_not_gated_by_reports_flag(monkeypatch) -> None:
    """_send_telegram_text must deliver even when periodic reports are muted."""
    from paper_engine.strategy_agent import paper_strategy_agent

    monkeypatch.setattr(settings, "TELEGRAM_REPORTS_ENABLED", False, raising=False)
    sent = AsyncMock(return_value=True)
    with patch("api.routers.auth.refresh_persistent_credentials_async", AsyncMock()), patch(
        "notifications.telegram_agent.telegram_agent.send", sent
    ), patch.object(paper_strategy_agent, "_get_broker_status_summary", AsyncMock(return_value=None)):
        await paper_strategy_agent._send_telegram_text("ENTRY test")

    assert sent.await_count == 1
    assert sent.await_args.kwargs.get("priority") is True
