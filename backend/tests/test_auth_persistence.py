from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from api.routers import auth


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
    monkeypatch.setattr(auth, "_save_credentials_to_disk", lambda creds: saved_payloads.append(dict(creds)))

    auth._broker_credentials = {"fyers": {"app_id": "APP"}}
    auth._active_brokers = {
        "fyers": {
            "token": SimpleNamespace(access_token="LIVE_FYERS_TOKEN"),
        }
    }

    auth._persist_active_session_tokens()

    assert auth._broker_credentials["fyers"]["access_token"] == "LIVE_FYERS_TOKEN"
    assert saved_payloads


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


def test_websocket_token_is_required_and_verifiable() -> None:
    payload = asyncio.run(auth.websocket_token())

    class DummySocket:
        query_params = {"auth": payload["token"]}
        client = None

    claims = auth.authenticate_websocket_client(DummySocket())

    assert claims["scope"] == "websocket"
    assert claims["sub"] == "browser-client"
