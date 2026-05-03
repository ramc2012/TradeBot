from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from api.routers import auth
from core.config import FYERS_FIXED_REDIRECT_URI, UPSTOX_SANDBOX_REDIRECT_URI


def test_bootstrap_credentials_keeps_saved_redirect_uri_when_env_is_absent(monkeypatch) -> None:
    monkeypatch.setattr(
        auth,
        "_load_credentials",
        lambda: {
            "fyers": {
                "app_id": "APP",
                "secret": "SECRET",
                "redirect_uri": "https://persisted.example/callback",
            }
        },
    )
    monkeypatch.setattr(auth, "_apply_credentials_to_settings", lambda broker, creds: None)
    for name in (
        "FYERS_APP_ID",
        "FYERS_SECRET",
        "FYERS_REDIRECT_URI",
        "UPSTOX_API_KEY",
        "UPSTOX_SECRET",
        "UPSTOX_REDIRECT_URI",
        "FIVEPAISA_APP_NAME",
        "FIVEPAISA_APP_SOURCE",
        "FIVEPAISA_USER_ID",
        "FIVEPAISA_EMAIL",
        "FIVEPAISA_PASSWORD",
        "FIVEPAISA_USER_KEY",
        "FIVEPAISA_ENCRYPTION_KEY",
        "ICICI_BREEZE_API_KEY",
        "ICICI_BREEZE_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)

    auth._broker_credentials = {}
    auth._bootstrap_credentials()

    assert auth._broker_credentials["fyers"]["redirect_uri"] == "https://persisted.example/callback"


def test_persist_active_session_tokens_backfills_saved_token(monkeypatch) -> None:
    saved_payloads: list[dict] = []
    monkeypatch.setattr(auth, "_persist_credentials", lambda creds: saved_payloads.append(dict(creds)))

    auth._broker_credentials = {"fyers": {"app_id": "APP"}}
    auth._active_brokers = {
        "fyers": {
            "token": SimpleNamespace(access_token="LIVE_FYERS_TOKEN"),
        }
    }

    auth._persist_active_session_tokens()

    assert auth._broker_credentials["fyers"]["access_token"] == "LIVE_FYERS_TOKEN"
    assert saved_payloads


def test_load_persistent_credentials_prefers_explicit_env(monkeypatch) -> None:
    auth._broker_credentials = {
        "fyers": {
            "app_id": "FILE_APP",
            "redirect_uri": "https://file.example/callback",
        }
    }
    monkeypatch.setattr(
        auth,
        "_load_credentials_payload_from_database",
        lambda: (
            {
                "fyers": {
                    "app_id": "DB_APP",
                    "secret": "DB_SECRET",
                    "redirect_uri": "https://db.example/callback",
                }
            },
            datetime.now(timezone.utc),
        ),
    )
    applied: list[tuple[str, dict]] = []
    monkeypatch.setattr(auth, "_apply_credentials_to_settings", lambda broker, creds: applied.append((broker, dict(creds))))
    monkeypatch.setenv("FYERS_REDIRECT_URI", "https://env.example/callback")

    auth.load_persistent_credentials()

    assert auth._broker_credentials["fyers"]["app_id"] == "DB_APP"
    assert auth._broker_credentials["fyers"]["secret"] == "DB_SECRET"
    assert auth._broker_credentials["fyers"]["redirect_uri"] == "https://env.example/callback"
    assert applied[-1][1]["redirect_uri"] == "https://env.example/callback"


def test_refresh_persistent_credentials_updates_telegram_toggle(monkeypatch) -> None:
    updated_at = datetime.now(timezone.utc)
    auth._broker_credentials = {
        "telegram": {
            "bot_token": "123:abc",
            "chat_id": "-10042",
            "enabled": False,
            "report_interval": "30m",
        }
    }
    auth.settings.TELEGRAM_REPORTS_ENABLED = False
    auth._credentials_db_checked_at_monotonic = 0.0
    auth._credentials_db_updated_at = None

    monkeypatch.setattr(
        auth,
        "_load_credentials_payload_from_database",
        lambda: (
            {
                "telegram": {
                    "bot_token": "123:abc",
                    "chat_id": "-10042",
                    "enabled": True,
                    "report_interval": "30m",
                }
            },
            updated_at,
        ),
    )

    auth.refresh_persistent_credentials(force=True)

    assert auth._broker_credentials["telegram"]["enabled"] is True
    assert auth.settings.TELEGRAM_REPORTS_ENABLED is True
    assert auth._credentials_db_updated_at == updated_at


def test_bootstrap_credentials_normalizes_legacy_fyers_callback(monkeypatch) -> None:
    monkeypatch.setattr(
        auth,
        "_load_credentials",
        lambda: {
            "fyers": {
                "app_id": "APP",
                "secret": "SECRET",
                "redirect_uri": "https://legacy.example/api/auth/fyers/callback",
            }
        },
    )
    monkeypatch.setattr(auth, "_persist_credentials", lambda creds: None)
    monkeypatch.setattr(auth, "_apply_credentials_to_settings", lambda broker, creds: None)
    monkeypatch.delenv("FYERS_REDIRECT_URI", raising=False)

    auth._broker_credentials = {}
    auth._bootstrap_credentials()

    assert auth._broker_credentials["fyers"]["redirect_uri"] == FYERS_FIXED_REDIRECT_URI


def test_bootstrap_credentials_normalizes_legacy_upstox_localhost_redirect(monkeypatch) -> None:
    monkeypatch.setattr(
        auth,
        "_load_credentials",
        lambda: {
            "upstox": {
                "api_key": "APP",
                "secret": "SECRET",
                "redirect_uri": "http://localhost:8000/api/auth/upstox/callback",
            }
        },
    )
    monkeypatch.setattr(auth, "_persist_credentials", lambda creds: None)
    monkeypatch.setattr(auth, "_apply_credentials_to_settings", lambda broker, creds: None)
    monkeypatch.delenv("UPSTOX_REDIRECT_URI", raising=False)

    auth._broker_credentials = {}
    auth._bootstrap_credentials()

    assert auth._broker_credentials["upstox"]["redirect_uri"] == UPSTOX_SANDBOX_REDIRECT_URI


def test_save_fyers_credentials_persists_fixed_redirect_uri(monkeypatch) -> None:
    saved_payloads: list[dict] = []
    applied: list[tuple[str, dict]] = []
    monkeypatch.setattr(auth, "_persist_credentials", lambda creds: saved_payloads.append(dict(creds)))
    monkeypatch.setattr(auth, "_apply_credentials_to_settings", lambda broker, creds: applied.append((broker, dict(creds))))

    auth._broker_credentials = {
        "fyers": {
            "app_id": "APP",
            "secret": "SECRET",
            "redirect_uri": "https://legacy.example/api/auth/fyers/callback",
        }
    }

    payload = auth.SaveCredentialsRequest(
        broker="fyers",
        credentials={"app_id": "APP", "redirect_uri": "http://localhost:8000/api/auth/fyers/callback"},
    )

    result = asyncio.run(auth.save_credentials(payload))

    assert result["status"] == "saved"
    assert auth._broker_credentials["fyers"]["redirect_uri"] == FYERS_FIXED_REDIRECT_URI
    assert saved_payloads[-1]["fyers"]["redirect_uri"] == FYERS_FIXED_REDIRECT_URI
    assert applied[-1][1]["redirect_uri"] == FYERS_FIXED_REDIRECT_URI


def test_save_credentials_to_disk_encrypts_sensitive_values(monkeypatch, tmp_path) -> None:
    creds_file = tmp_path / "credentials.json"
    monkeypatch.setattr(auth, "_CREDS_FILE", creds_file)

    auth._save_credentials_to_disk(
        {
            "upstox": {
                "api_key": "client-key",
                "redirect_uri": "https://callback.example/upstox",
                "access_token": "eyJ.live.token",
            }
        }
    )

    payload = json.loads(creds_file.read_text())
    stored = payload["data"]["upstox"]

    assert payload["_format"] == "fernet-v1"
    assert stored["api_key"].startswith("fernet::")
    assert stored["redirect_uri"].startswith("fernet::")
    assert stored["access_token"].startswith("fernet::")
    assert "eyJ.live.token" not in creds_file.read_text()

    auth._save_credentials_to_disk({"fyers": {"pin": "1234", "refresh_token": "REFRESH"}})
    fyers_payload = json.loads(creds_file.read_text())["data"]["fyers"]
    assert fyers_payload["pin"].startswith("fernet::")
    assert fyers_payload["refresh_token"].startswith("fernet::")
    assert "1234" not in creds_file.read_text()


def test_load_credentials_decrypts_encrypted_payload(monkeypatch, tmp_path) -> None:
    creds_file = tmp_path / "credentials.json"
    monkeypatch.setattr(auth, "_CREDS_FILE", creds_file)

    auth._save_credentials_to_disk(
        {
            "telegram": {
                "bot_token": "123:abc",
                "chat_id": "-10042",
                "enabled": True,
                "report_interval": "1h",
            }
        }
    )

    loaded = auth._load_credentials()

    assert loaded["telegram"]["bot_token"] == "123:abc"
    assert loaded["telegram"]["chat_id"] == "-10042"
    assert loaded["telegram"]["enabled"] is True


def test_persist_broker_session_stores_refresh_metadata(monkeypatch) -> None:
    saved_payloads: list[dict] = []
    monkeypatch.setattr(auth, "_persist_credentials", lambda creds: saved_payloads.append(json.loads(json.dumps(creds))))

    auth._broker_credentials = {"upstox": {"api_key": "client-key"}}
    expires_at = datetime(2026, 4, 16, 3, 30, tzinfo=timezone.utc)

    auth._persist_broker_session(
        "upstox",
        SimpleNamespace(
            access_token="ACCESS_TOKEN",
            refresh_token="REFRESH_TOKEN",
            expires_at=expires_at,
        ),
    )

    stored = auth._broker_credentials["upstox"]
    assert stored["access_token"] == "ACCESS_TOKEN"
    assert stored["refresh_token"] == "REFRESH_TOKEN"
    assert stored["expires_at"] == expires_at.isoformat()
    assert stored["token_saved_at"]
    assert saved_payloads[-1]["upstox"]["refresh_token"] == "REFRESH_TOKEN"


def test_ensure_fyers_session_refreshes_durable_credentials_before_restore(monkeypatch) -> None:
    auth._active_brokers = {}
    auth._broker_credentials = {
        "fyers": {
            "app_id": "APP",
            "secret": "SECRET",
            "access_token": "STALE_TOKEN",
        }
    }

    def fake_refresh(*, force: bool = False) -> None:
        auth._broker_credentials["fyers"]["access_token"] = "LIVE_TOKEN"

    async def fake_validate(token: str) -> bool:
        return token == "LIVE_TOKEN"

    class _FakeFyersAdapter:
        async def authenticate(self, credentials: dict):
            assert credentials["access_token"] == "LIVE_TOKEN"
            return SimpleNamespace(access_token="LIVE_TOKEN", refresh_token=None, expires_at=None)

        async def get_profile(self):
            return SimpleNamespace(user_id="FY123", name="Fyers User")

    monkeypatch.setattr(auth, "refresh_persistent_credentials", fake_refresh)
    monkeypatch.setattr(auth, "_validate_fyers_access_token", fake_validate)
    monkeypatch.setattr(auth, "_persist_broker_session", lambda broker, token, connected_at=None: None)
    monkeypatch.setattr(auth, "_sync_market_data_feed", lambda: asyncio.sleep(0))
    monkeypatch.setattr("brokers.fyers.FyersAdapter", _FakeFyersAdapter)

    restored = asyncio.run(auth.ensure_fyers_session(force_validate=True))

    assert restored is True
    assert auth._active_brokers["fyers"]["token"].access_token == "LIVE_TOKEN"


def test_ensure_fyers_session_uses_saved_refresh_token_and_pin(monkeypatch) -> None:
    auth._active_brokers = {}
    auth._broker_credentials = {
        "fyers": {
            "app_id": "APP",
            "secret": "SECRET",
            "access_token": "STALE_TOKEN",
            "refresh_token": "REFRESH_TOKEN",
            "pin": "1234",
        }
    }

    async def fake_validate(token: str) -> bool:
        return False

    class _FakeFyersAdapter:
        async def authenticate(self, credentials: dict):
            assert credentials == {"refresh_token": "REFRESH_TOKEN", "pin": "1234"}
            return SimpleNamespace(
                access_token="REFRESHED_TOKEN",
                refresh_token="REFRESH_TOKEN",
                expires_at=None,
            )

        async def get_profile(self):
            return SimpleNamespace(user_id="FY123", name="Fyers User")

    persisted: list[tuple[str, object]] = []
    monkeypatch.setattr(auth, "refresh_persistent_credentials", lambda force=False: None)
    monkeypatch.setattr(auth, "_validate_fyers_access_token", fake_validate)
    monkeypatch.setattr(auth, "_persist_broker_session", lambda broker, token, connected_at=None: persisted.append((broker, token)))
    monkeypatch.setattr(auth, "_sync_market_data_feed", lambda: asyncio.sleep(0))
    monkeypatch.setattr("brokers.fyers.FyersAdapter", _FakeFyersAdapter)

    restored = asyncio.run(auth.ensure_fyers_session(force_validate=True))

    assert restored is True
    assert auth._active_brokers["fyers"]["token"].access_token == "REFRESHED_TOKEN"
    assert persisted[-1][0] == "fyers"


def test_broker_status_avoids_forcing_credential_refresh_on_normal_poll(monkeypatch) -> None:
    forced_flags: list[bool] = []

    def fake_refresh(*, force: bool = False) -> None:
        forced_flags.append(force)

    async def fake_snapshot(force_validate: bool = False) -> dict:
        return {
            "connected_brokers": [],
            "session_brokers": [],
            "upstox_ready": False,
            "fyers_ready": False,
            "upstox_token_health": {},
            "fyers_token_health": {},
        }

    monkeypatch.setattr(auth, "refresh_persistent_credentials", fake_refresh)
    monkeypatch.setattr(auth, "get_broker_connection_snapshot", fake_snapshot)
    monkeypatch.setattr(auth, "_persist_active_session_tokens", lambda: None)
    auth._active_brokers = {}

    payload = asyncio.run(auth.broker_status())

    assert len(payload) == len(auth.BROKER_MAP)
    assert forced_flags == [False]


def test_websocket_token_is_required_and_verifiable() -> None:
    payload = asyncio.run(auth.websocket_token())

    class DummySocket:
        query_params = {"auth": payload["token"]}
        client = None

    claims = auth.authenticate_websocket_client(DummySocket())

    assert claims["scope"] == "websocket"
    assert claims["sub"] == "browser-client"
