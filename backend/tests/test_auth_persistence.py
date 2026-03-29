from __future__ import annotations

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
