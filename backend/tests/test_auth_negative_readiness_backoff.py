"""Credential retry-storm fix (2026-07-18).

Under the SEBI daily-OAuth flow Fyers no longer issues durable refresh material,
so once the saved session is expired the "refresh from saved credentials" path is
a hard dead end. Before the negative-readiness backoff, every readiness check
(observed ~45x/10min, doubling under the LANESET process split) re-ran a forced
credential reload + emitted a warning line for nothing.

These tests pin the fix:
  * the FIRST expired-and-unrefreshable check attempts exactly one refresh (one
    forced credential reload), then opens the backoff window;
  * subsequent checks short-circuit — no credential reload, no adapter build —
    and the log is throttled to a single line per window;
  * persisting a NEW access token clears the backoff so the next check retries;
  * a failed refresh EXCHANGE (material present) also opens the backoff;
  * setting the backoff to 0 disables suppression entirely (each check retries).
"""
from __future__ import annotations

import asyncio

import pytest

from api.routers import auth
from core.config import settings


@pytest.fixture()
def clean_backoff(monkeypatch):
    """Reset caches + neutralize side-effecting persistence around each test."""
    auth._reset_negative_readiness(auth._fyers_negative_readiness)
    auth._reset_negative_readiness(auth._upstox_negative_readiness)
    auth._fyers_token_health_cache.update({"token": None, "checked_at": None, "result": None})
    saved_creds = auth._broker_credentials
    auth._broker_credentials = {"fyers": {"app_id": "APP"}}  # NO refresh material
    monkeypatch.setattr(auth, "_persist_credentials", lambda creds: None)
    yield
    auth._broker_credentials = saved_creds
    auth._reset_negative_readiness(auth._fyers_negative_readiness)


def _run(coro):
    return asyncio.run(coro)


def test_repeated_expired_checks_attempt_refresh_once_then_backoff(clean_backoff, monkeypatch):
    reload_calls = []

    async def _fake_reload(force: bool = False):
        reload_calls.append(force)

    monkeypatch.setattr(auth, "refresh_persistent_credentials_async", _fake_reload)
    # If the adapter is ever built we have a bug — no refresh material exists.
    monkeypatch.setattr(
        auth, "_has_saved_fyers_refresh_material", lambda: False
    )
    assert settings.BROKER_NEGATIVE_READINESS_BACKOFF_SECONDS > 0

    # Capture the loguru stream the module actually logs through.
    from loguru import logger as _loguru

    captured: list[str] = []
    sink_id = _loguru.add(lambda m: captured.append(str(m)), level="INFO")
    try:
        # First check: one real attempt (one forced credential reload), False.
        assert _run(auth._refresh_fyers_session_from_saved_credentials()) is False
        assert reload_calls == [True]
        assert auth._negative_readiness_active(auth._fyers_negative_readiness)

        # Next 20 checks short-circuit — no further credential reloads at all.
        for _ in range(20):
            assert _run(auth._refresh_fyers_session_from_saved_credentials()) is False
        assert reload_calls == [True]  # still exactly one reload across 21 checks
    finally:
        _loguru.remove(sink_id)

    # Logging is throttled to a single line for the whole window.
    suppress_lines = [m for m in captured if "suppress" in m.lower()]
    assert len(suppress_lines) == 1


def test_new_token_clears_backoff_and_next_check_retries(clean_backoff, monkeypatch):
    reload_calls = []

    async def _fake_reload(force: bool = False):
        reload_calls.append(force)

    monkeypatch.setattr(auth, "refresh_persistent_credentials_async", _fake_reload)
    monkeypatch.setattr(auth, "_has_saved_fyers_refresh_material", lambda: False)

    # Open the backoff.
    _run(auth._refresh_fyers_session_from_saved_credentials())
    _run(auth._refresh_fyers_session_from_saved_credentials())
    assert reload_calls == [True]
    assert auth._negative_readiness_active(auth._fyers_negative_readiness)

    # Owner supplies a fresh daily token → persistence clears the backoff.
    auth._persist_access_token("fyers", "FRESH_DAILY_TOKEN")
    assert not auth._negative_readiness_active(auth._fyers_negative_readiness)

    # The next readiness check retries (a second forced reload happens).
    _run(auth._refresh_fyers_session_from_saved_credentials())
    assert reload_calls == [True, True]


def test_failed_refresh_exchange_also_opens_backoff(clean_backoff, monkeypatch):
    reload_calls = []

    async def _fake_reload(force: bool = False):
        reload_calls.append(force)

    monkeypatch.setattr(auth, "refresh_persistent_credentials_async", _fake_reload)
    # Material IS present, but the exchange fails.
    auth._broker_credentials["fyers"].update({"refresh_token": "RT", "pin": "1234"})
    monkeypatch.setattr(auth, "_has_saved_fyers_refresh_material", lambda: True)

    class _BoomAdapter:
        async def authenticate(self, *_a, **_k):
            raise RuntimeError("refresh exchange rejected")

        async def get_profile(self):  # pragma: no cover - not reached
            raise AssertionError

    import brokers.fyers as fyers_mod

    monkeypatch.setattr(fyers_mod, "FyersAdapter", lambda: _BoomAdapter())

    assert _run(auth._refresh_fyers_session_from_saved_credentials()) is False
    assert auth._negative_readiness_active(auth._fyers_negative_readiness)

    # Subsequent check short-circuits before touching the adapter or reloading.
    before = list(reload_calls)
    assert _run(auth._refresh_fyers_session_from_saved_credentials()) is False
    assert reload_calls == before  # no additional reload


def test_backoff_seconds_zero_disables_suppression(clean_backoff, monkeypatch):
    reload_calls = []

    async def _fake_reload(force: bool = False):
        reload_calls.append(force)

    monkeypatch.setattr(auth, "refresh_persistent_credentials_async", _fake_reload)
    monkeypatch.setattr(auth, "_has_saved_fyers_refresh_material", lambda: False)
    monkeypatch.setattr(settings, "BROKER_NEGATIVE_READINESS_BACKOFF_SECONDS", 0)

    for _ in range(3):
        assert _run(auth._refresh_fyers_session_from_saved_credentials()) is False
    # With backoff disabled, every check retries (no suppression).
    assert reload_calls == [True, True, True]
    assert not auth._negative_readiness_active(auth._fyers_negative_readiness)
