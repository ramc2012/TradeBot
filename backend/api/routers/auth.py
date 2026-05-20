"""Broker authentication routes."""
from __future__ import annotations
import asyncio
import base64
import json
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketException, status
from pydantic import BaseModel
import httpx
from loguru import logger
import psycopg2
from psycopg2.extras import Json

from brokers import get_broker, BROKER_MAP
from core.config import (
    FYERS_FIXED_REDIRECT_URI,
    UPSTOX_SANDBOX_REDIRECT_URI,
    normalize_fyers_redirect_uri,
    normalize_upstox_redirect_uri,
    settings,
)
from core.security import decrypt_token, encrypt_token, issue_ephemeral_token, verify_ephemeral_token

router = APIRouter(prefix="/api/auth", tags=["auth"])

# In-memory broker registry (active connections this session)
_active_brokers: dict = {}
_active_brokers_lock = RLock()

# Persistent credential store — survives restarts via credentials.json
# Use /app/credentials.json in Docker; fall back to backend dir in local dev
def _resolve_creds_file() -> Path:
    # 1. Explicit override via env var
    env_path = os.environ.get("CREDENTIALS_FILE", "").strip()
    if env_path:
        return Path(env_path)
    # 2. Docker path (standard deployment)
    docker_path = Path("/app/credentials.json")
    if docker_path.parent.is_dir():
        return docker_path
    # 3. Local dev: store in the backend directory alongside this file
    local_path = Path(__file__).resolve().parent.parent.parent / "credentials.json"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    return local_path


_CREDS_FILE = _resolve_creds_file()
_broker_credentials: dict = {}
_upstox_token_health_cache: dict = {
    "token": None,
    "checked_at": None,
    "result": None,
}
_fyers_token_health_cache: dict = {
    "token": None,
    "checked_at": None,
    "result": None,
}
IST = timezone(timedelta(hours=5, minutes=30))
BROKER_STATUS_ORDER = ("fyers", "upstox", "icici_breeze", "fivepaisa")
BROKER_STATUS_LABELS = {
    "fyers": "FYERS",
    "upstox": "UPSTOX",
    "icici_breeze": "BREEZE",
    "fivepaisa": "5PAISA",
}
AUTO_RESTORE_TIMEOUT_SECONDS = 10
WS_TOKEN_TTL_SECONDS = 300
OAUTH_STATE_TTL_SECONDS = 900
_ENCRYPTED_VALUE_PREFIX = "fernet::"
_ENCRYPTED_CREDENTIALS_VERSION = "fernet-v1"
_credentials_require_migration = False
_CREDENTIAL_STORE_DB_KEY = "broker_credentials"
_CREDENTIAL_REFRESH_TTL_SECONDS = 15.0
_credentials_db_checked_at_monotonic = 0.0
_credentials_db_updated_at: datetime | None = None
_BROKER_SNAPSHOT_CACHE_TTL_SECONDS = 5.0
_BROKER_SNAPSHOT_SESSION_TIMEOUT_SECONDS = 1.5
_BROKER_SNAPSHOT_HEALTH_TIMEOUT_SECONDS = 2.0
_BROKER_SNAPSHOT_FORCE_TIMEOUT_SECONDS = 5.0
_broker_snapshot_cache: dict[str, Any] = {
    "expires_at": 0.0,
    "result": None,
}
_broker_snapshot_lock = asyncio.Lock()
_SENSITIVE_CREDENTIAL_FIELDS = {
    "fyers": {"app_id", "secret", "redirect_uri", "access_token", "refresh_token", "pin"},
    "upstox": {"api_key", "secret", "redirect_uri", "access_token", "refresh_token", "analytics_token"},
    "fivepaisa": {"app_name", "app_source", "user_id", "email", "password", "user_key", "encryption_key"},
    "icici_breeze": {"api_key", "secret"},
    "telegram": {"bot_token", "chat_id"},
}


# ── Credential persistence ────────────────────────────────────────────────────

def _is_sensitive_credential(broker: str, field: str) -> bool:
    return field in _SENSITIVE_CREDENTIAL_FIELDS.get(broker, set())


def _looks_like_placeholder_credential(value: Any) -> bool:
    raw = str(value or "").strip().lower()
    return raw in {"x", "xx", "xxx", "test", "dummy", "placeholder", "your_api_key", "your_secret"}


def _encrypt_credential_value(value: Any) -> Any:
    text_value = str(value or "").strip()
    if not text_value:
        return value
    if text_value.startswith(_ENCRYPTED_VALUE_PREFIX):
        return text_value
    return f"{_ENCRYPTED_VALUE_PREFIX}{encrypt_token(text_value)}"


def _decrypt_credential_value(value: Any) -> Any:
    text_value = str(value or "").strip()
    if not text_value.startswith(_ENCRYPTED_VALUE_PREFIX):
        return value
    return decrypt_token(text_value[len(_ENCRYPTED_VALUE_PREFIX):])


def _decode_credentials_payload(payload: Any) -> dict:
    global _credentials_require_migration

    raw = payload
    if isinstance(raw, dict) and raw.get("_format") == _ENCRYPTED_CREDENTIALS_VERSION:
        raw = raw.get("data", {})

    decoded: dict[str, dict[str, Any]] = {}
    for broker, values in dict(raw or {}).items():
        if not isinstance(values, dict):
            continue
        broker_values: dict[str, Any] = {}
        for key, value in values.items():
            if _is_sensitive_credential(broker, key):
                try:
                    broker_values[key] = _decrypt_credential_value(value)
                    if not str(value or "").startswith(_ENCRYPTED_VALUE_PREFIX):
                        _credentials_require_migration = True
                except Exception as exc:
                    logger.warning(f"Could not decrypt credentials.json field {broker}.{key}: {exc}")
                    broker_values[key] = value
            else:
                broker_values[key] = value
        decoded[broker] = broker_values
    return decoded


def _encode_credentials_payload(creds: dict) -> dict:
    data: dict[str, dict[str, Any]] = {}
    for broker, values in dict(creds or {}).items():
        if not isinstance(values, dict):
            continue
        broker_values: dict[str, Any] = {}
        for key, value in values.items():
            broker_values[key] = (
                _encrypt_credential_value(value)
                if _is_sensitive_credential(broker, key)
                else value
            )
        data[broker] = broker_values
    return {"_format": _ENCRYPTED_CREDENTIALS_VERSION, "data": data}

def _load_credentials() -> dict:
    """Load saved broker credentials from disk."""
    global _credentials_require_migration
    if _CREDS_FILE.exists():
        try:
            payload = json.loads(_CREDS_FILE.read_text())
            return _decode_credentials_payload(payload)
        except Exception as e:
            logger.warning(f"Could not load credentials.json: {e}")
            _credentials_require_migration = False
    return {}


def _database_url_for_credentials() -> Optional[str]:
    raw = str(settings.DATABASE_URL or "").strip()
    if not raw:
        return None
    return (
        raw.replace("postgresql+asyncpg://", "postgresql://")
        .replace("postgresql+psycopg2://", "postgresql://")
    )


def _connect_credentials_db(db_url: str) -> psycopg2.extensions.connection:
    return psycopg2.connect(
        db_url,
        connect_timeout=3,
        options="-c statement_timeout=3000",
    )


def _save_credentials_to_database(creds: dict) -> None:
    """Persist broker credentials to Postgres for durable cloud storage."""
    db_url = _database_url_for_credentials()
    if not db_url:
        return
    conn = None
    try:
        conn = _connect_credentials_db(db_url)
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS app_runtime_state (
                    state_key TEXT PRIMARY KEY,
                    payload JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                INSERT INTO app_runtime_state (state_key, payload, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (state_key) DO UPDATE SET
                    payload = EXCLUDED.payload,
                    updated_at = NOW()
                """,
                (_CREDENTIAL_STORE_DB_KEY, Json(_encode_credentials_payload(creds))),
            )
        conn.commit()
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        logger.warning(f"Could not persist broker credentials to database: {exc}")
    finally:
        if conn is not None:
            conn.close()


def _load_credentials_from_database() -> dict:
    """Load broker credentials from Postgres when available."""
    payload, _ = _load_credentials_payload_from_database()
    return payload


def _load_credentials_payload_from_database() -> tuple[dict, datetime | None]:
    """Load broker credentials and the row timestamp from Postgres when available."""
    db_url = _database_url_for_credentials()
    if not db_url:
        return {}, None
    conn = None
    try:
        conn = _connect_credentials_db(db_url)
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS app_runtime_state (
                    state_key TEXT PRIMARY KEY,
                    payload JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                "SELECT payload, updated_at FROM app_runtime_state WHERE state_key = %s",
                (_CREDENTIAL_STORE_DB_KEY,),
            )
            row = cur.fetchone()
        conn.commit()
        if not row:
            return {}, None
        return _decode_credentials_payload(row[0]), row[1]
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        logger.warning(f"Could not load broker credentials from database: {exc}")
        return {}, None
    finally:
        if conn is not None:
            conn.close()


def _save_credentials_to_disk(creds: dict) -> None:
    """Persist broker credentials to disk."""
    global _credentials_require_migration
    try:
        _CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CREDS_FILE.write_text(json.dumps(_encode_credentials_payload(creds), indent=2))
        _credentials_require_migration = False
        logger.debug(f"Credentials saved to {_CREDS_FILE}")
    except Exception as e:
        logger.error(f"Could not write credentials to {_CREDS_FILE}: {e}")


def _persist_credentials(creds: dict) -> None:
    """Persist credentials to all configured stores."""
    _save_credentials_to_disk(creds)
    _save_credentials_to_database(creds)


def _reset_upstox_token_health_cache() -> None:
    _upstox_token_health_cache.update(
        {
            "token": None,
            "checked_at": None,
            "result": None,
        }
    )


def _reset_fyers_token_health_cache() -> None:
    _fyers_token_health_cache.update(
        {
            "token": None,
            "checked_at": None,
            "result": None,
        }
    )


def _persist_access_token(broker: str, access_token: Optional[str]) -> None:
    """Persist a broker session token when the broker supports restore."""
    token_value = str(access_token or "").strip()
    if not token_value:
        return

    entry = _broker_credentials.setdefault(broker, {})
    if entry.get("access_token") == token_value:
        return

    entry["access_token"] = token_value
    _persist_credentials(_broker_credentials)
    if broker == "upstox":
        _reset_upstox_token_health_cache()
    if broker == "fyers":
        _reset_fyers_token_health_cache()


def _serialize_token_expiry(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_token_expiry(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _looks_like_jwt(token: str) -> bool:
    return token.count(".") >= 2 and token.startswith("eyJ")


def _token_is_expired(access_token: str, *, expires_at: Any = None) -> bool:
    expiry = _parse_token_expiry(expires_at)
    if expiry is not None:
        return datetime.now(timezone.utc) >= expiry
    if _looks_like_jwt(access_token):
        return _jwt_expired(access_token)
    return False


def _persist_broker_session(
    broker: str,
    token: Any,
    *,
    connected_at: Optional[datetime] = None,
) -> None:
    """Persist the full broker session payload for cross-instance same-day restore."""
    access_token = str(getattr(token, "access_token", None) or "").strip()
    if not access_token:
        return

    refresh_token = str(getattr(token, "refresh_token", None) or "").strip()
    expires_at = _serialize_token_expiry(getattr(token, "expires_at", None))
    saved_at = (connected_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()

    entry = _broker_credentials.setdefault(broker, {})
    changed = False
    updates = {
        "access_token": access_token,
        "refresh_token": refresh_token or None,
        "expires_at": expires_at,
        "token_saved_at": saved_at,
    }
    for key, value in updates.items():
        if value in (None, ""):
            if key in entry and key in {"expires_at"}:
                entry.pop(key, None)
                changed = True
            continue
        if entry.get(key) != value:
            entry[key] = value
            changed = True

    if not changed:
        return

    _persist_credentials(_broker_credentials)
    if broker == "upstox":
        _reset_upstox_token_health_cache()
    if broker == "fyers":
        _reset_fyers_token_health_cache()


def _jwt_expired(token: str) -> bool:
    """Decode JWT payload (without verification) and check exp claim."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return True
        padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.b64decode(padded))
        exp = payload.get("exp", 0)
        return datetime.utcnow().timestamp() >= exp
    except Exception:
        return True  # undecodable → treat as expired


def _explicit_env(name: str) -> Optional[str]:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _merge_explicit_env_credentials(creds: dict) -> dict:
    merged_creds = dict(creds or {})
    fyers_app_id = _explicit_env("FYERS_APP_ID")
    fyers_secret = _explicit_env("FYERS_SECRET")
    fyers_redirect = _explicit_env("FYERS_REDIRECT_URI")
    fyers_pin = _explicit_env("FYERS_PIN")
    fyers_existing = dict(merged_creds.get("fyers") or {})
    fyers_seed = {
        "app_id": fyers_app_id,
        "secret": fyers_secret,
    }
    if fyers_redirect and (fyers_existing or fyers_app_id or fyers_secret):
        fyers_seed["redirect_uri"] = normalize_fyers_redirect_uri(fyers_redirect)
    if fyers_pin:
        fyers_seed["pin"] = fyers_pin
    env_seed = {
        "fyers": fyers_seed,
        "upstox": {
            "api_key": _explicit_env("UPSTOX_API_KEY"),
            "secret": _explicit_env("UPSTOX_SECRET"),
            "analytics_token": _explicit_env("UPSTOX_ANALYTICS_TOKEN"),
            "redirect_uri": (
                normalize_upstox_redirect_uri(_explicit_env("UPSTOX_REDIRECT_URI"))
                if _explicit_env("UPSTOX_REDIRECT_URI")
                else None
            ),
        },
        "fivepaisa": {
            "app_name": _explicit_env("FIVEPAISA_APP_NAME"),
            "app_source": _explicit_env("FIVEPAISA_APP_SOURCE"),
            "user_id": _explicit_env("FIVEPAISA_USER_ID"),
            "email": _explicit_env("FIVEPAISA_EMAIL"),
            "password": _explicit_env("FIVEPAISA_PASSWORD"),
            "user_key": _explicit_env("FIVEPAISA_USER_KEY"),
            "encryption_key": _explicit_env("FIVEPAISA_ENCRYPTION_KEY"),
        },
        "icici_breeze": {
            "api_key": _explicit_env("ICICI_BREEZE_API_KEY"),
            "secret": _explicit_env("ICICI_BREEZE_SECRET"),
        },
        "telegram": {
            "bot_token": _explicit_env("TELEGRAM_BOT_TOKEN"),
            "chat_id": _explicit_env("TELEGRAM_CHAT_ID"),
            "report_interval": _explicit_env("TELEGRAM_REPORT_INTERVAL"),
        },
    }
    for broker, env_creds in env_seed.items():
        filled = {k: v for k, v in env_creds.items() if v}
        if filled:
            existing = merged_creds.get(broker, {})
            merged_creds[broker] = {**existing, **filled}
    return merged_creds


def _normalize_broker_credentials(creds: dict) -> tuple[dict, bool]:
    normalized: dict[str, dict[str, Any]] = {}
    changed = False
    for broker, values in dict(creds or {}).items():
        if not isinstance(values, dict):
            continue
        broker_values = dict(values)
        if broker == "fyers" and broker_values:
            redirect_uri = normalize_fyers_redirect_uri(broker_values.get("redirect_uri"))
            if broker_values.get("redirect_uri") != redirect_uri:
                broker_values["redirect_uri"] = redirect_uri
                changed = True
        if broker == "upstox" and broker_values:
            redirect_uri = normalize_upstox_redirect_uri(broker_values.get("redirect_uri"))
            if broker_values.get("redirect_uri") != redirect_uri:
                broker_values["redirect_uri"] = redirect_uri
                changed = True
        normalized[broker] = broker_values
    return normalized, changed


def _apply_credentials_to_settings(broker: str, creds: dict) -> None:
    """Push saved credentials into the live settings object."""
    if broker == "fyers":
        if creds.get("app_id"):   settings.FYERS_APP_ID = creds["app_id"]
        if creds.get("secret"):   settings.FYERS_SECRET = creds["secret"]
        if creds.get("pin"):      settings.FYERS_PIN = creds["pin"]
        settings.FYERS_REDIRECT_URI = normalize_fyers_redirect_uri(
            creds.get("redirect_uri") or settings.FYERS_REDIRECT_URI
        )
    elif broker == "upstox":
        if creds.get("api_key"):      settings.UPSTOX_API_KEY = creds["api_key"]
        if creds.get("secret"):       settings.UPSTOX_SECRET = creds["secret"]
        if creds.get("analytics_token"): settings.UPSTOX_ANALYTICS_TOKEN = creds["analytics_token"]
        if creds.get("redirect_uri"):
            settings.UPSTOX_REDIRECT_URI = normalize_upstox_redirect_uri(creds["redirect_uri"])
        # access_token is stored for auto-restore but not pushed to settings
    elif broker == "fivepaisa":
        if creds.get("app_name"):       settings.FIVEPAISA_APP_NAME = creds["app_name"]
        if creds.get("app_source"):     settings.FIVEPAISA_APP_SOURCE = creds["app_source"]
        if creds.get("user_id"):        settings.FIVEPAISA_USER_ID = creds["user_id"]
        if creds.get("email"):          settings.FIVEPAISA_EMAIL = creds["email"]
        if creds.get("password"):       settings.FIVEPAISA_PASSWORD = creds["password"]
        if creds.get("user_key"):       settings.FIVEPAISA_USER_KEY = creds["user_key"]
        if creds.get("encryption_key"): settings.FIVEPAISA_ENCRYPTION_KEY = creds["encryption_key"]
    elif broker == "icici_breeze":
        if creds.get("api_key"): settings.ICICI_BREEZE_API_KEY = creds["api_key"]
        if creds.get("secret"):  settings.ICICI_BREEZE_SECRET = creds["secret"]
    elif broker == "telegram":
        if creds.get("bot_token"): settings.TELEGRAM_BOT_TOKEN = creds["bot_token"]
        if creds.get("chat_id"): settings.TELEGRAM_CHAT_ID = creds["chat_id"]
        if "enabled" in creds: settings.TELEGRAM_REPORTS_ENABLED = bool(creds["enabled"])
        if creds.get("report_interval"): settings.TELEGRAM_REPORT_INTERVAL = str(creds["report_interval"])


def _bootstrap_credentials() -> None:
    """
    Called at startup. Loads credentials.json into memory,
    then fills any gaps from environment variables / .env.
    """
    global _broker_credentials
    # Load persisted file first
    loaded_creds, normalized_loaded = _normalize_broker_credentials(_load_credentials())
    _broker_credentials = _merge_explicit_env_credentials(loaded_creds)
    _broker_credentials, normalized_merged = _normalize_broker_credentials(_broker_credentials)

    # Apply everything to live settings
    for broker, creds in _broker_credentials.items():
        _apply_credentials_to_settings(broker, creds)

    if _broker_credentials and (_credentials_require_migration or normalized_loaded or normalized_merged):
        _persist_credentials(_broker_credentials)

    saved = [b for b, c in _broker_credentials.items() if c]
    if saved:
        logger.info(f"Credentials loaded for: {', '.join(saved)}")


def load_persistent_credentials() -> None:
    """Merge durable database credentials into memory, then re-apply explicit env overrides."""
    global _broker_credentials, _credentials_db_updated_at, _credentials_db_checked_at_monotonic
    db_creds, updated_at = _load_credentials_payload_from_database()
    if not db_creds:
        _credentials_db_checked_at_monotonic = monotonic()
        return
    merged_store: dict[str, dict[str, Any]] = dict(_broker_credentials)
    for broker, values in db_creds.items():
        existing = merged_store.get(broker, {})
        merged_store[broker] = {**existing, **dict(values or {})}
    _broker_credentials = _merge_explicit_env_credentials(merged_store)
    _broker_credentials, _ = _normalize_broker_credentials(_broker_credentials)
    for broker, creds in _broker_credentials.items():
        _apply_credentials_to_settings(broker, creds)
    _credentials_db_updated_at = updated_at
    _credentials_db_checked_at_monotonic = monotonic()
    logger.info(f"Loaded durable credentials from database for: {', '.join(sorted(db_creds.keys()))}")


def refresh_persistent_credentials(force: bool = False) -> None:
    """Refresh in-memory credentials from the durable store when another instance updated them."""
    global _credentials_db_checked_at_monotonic, _credentials_db_updated_at, _broker_credentials

    now = monotonic()
    if not force and (now - _credentials_db_checked_at_monotonic) < _CREDENTIAL_REFRESH_TTL_SECONDS:
        return

    db_creds, updated_at = _load_credentials_payload_from_database()
    _credentials_db_checked_at_monotonic = now
    if not db_creds:
        return
    if not force and updated_at is not None and _credentials_db_updated_at is not None and updated_at <= _credentials_db_updated_at:
        return

    merged_store: dict[str, dict[str, Any]] = dict(_broker_credentials)
    for broker, values in db_creds.items():
        existing = merged_store.get(broker, {})
        merged_store[broker] = {**existing, **dict(values or {})}
    _broker_credentials = _merge_explicit_env_credentials(merged_store)
    _broker_credentials, _ = _normalize_broker_credentials(_broker_credentials)
    for broker, creds in _broker_credentials.items():
        _apply_credentials_to_settings(broker, creds)
    _credentials_db_updated_at = updated_at


def _persist_active_session_tokens() -> None:
    for broker in ("upstox", "fyers"):
        with _active_brokers_lock:
            info = _active_brokers.get(broker)
        if not info:
            continue
        token = info.get("token")
        connected_at_raw = info.get("connected_at")
        connected_at: Optional[datetime] = None
        if isinstance(connected_at_raw, str):
            try:
                connected_at = datetime.fromisoformat(connected_at_raw)
            except Exception:
                connected_at = None
        _persist_broker_session(broker, token, connected_at=connected_at)


def _get_active_broker_info(broker: str) -> dict[str, Any] | None:
    with _active_brokers_lock:
        info = _active_brokers.get(broker)
        return dict(info) if info else None


def _get_active_session_access_token(broker: str) -> str | None:
    info = _get_active_broker_info(broker)
    if not info:
        return None
    token = info.get("token")
    if token is None:
        return None
    return getattr(token, "access_token", None) or None


async def _validate_upstox_access_token(access_token: str) -> bool:
    if not access_token:
        return False
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get("https://api.upstox.com/v2/user/profile", headers=headers)
        return resp.status_code == 200
    except Exception as exc:
        logger.debug(f"Upstox token validation failed: {exc}")
        return False


async def _validate_fyers_access_token(access_token: str) -> bool:
    if not access_token:
        return False
    headers = {
        "Authorization": f"{settings.FYERS_APP_ID}:{access_token}",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get("https://api-t1.fyers.in/api/v3/profile", headers=headers)
        return resp.status_code == 200
    except Exception as exc:
        logger.debug(f"Fyers token validation failed: {exc}")
        return False


def _has_saved_fyers_refresh_material() -> bool:
    fyers_creds = _broker_credentials.get("fyers", {})
    return bool(
        str(fyers_creds.get("refresh_token") or "").strip()
        and str(fyers_creds.get("pin") or settings.FYERS_PIN or "").strip()
        and str(fyers_creds.get("app_id") or settings.FYERS_APP_ID or "").strip()
        and str(fyers_creds.get("secret") or settings.FYERS_SECRET or "").strip()
    )


async def _refresh_fyers_session_from_saved_credentials() -> bool:
    refresh_persistent_credentials(force=True)
    fyers_creds = _broker_credentials.get("fyers", {})
    refresh_token = str(fyers_creds.get("refresh_token") or "").strip()
    pin = str(fyers_creds.get("pin") or settings.FYERS_PIN or "").strip()
    if not _has_saved_fyers_refresh_material():
        return False
    try:
        from brokers.fyers import FyersAdapter

        adapter = FyersAdapter()
        token = await adapter.authenticate({"refresh_token": refresh_token, "pin": pin})
        profile = await adapter.get_profile()
        with _active_brokers_lock:
            _active_brokers["fyers"] = {
                "adapter": adapter,
                "token": token,
                "profile": profile,
                "connected_at": datetime.utcnow().isoformat(),
                "auto_refreshed": True,
            }
        _persist_broker_session("fyers", token)
        await _sync_market_data_feed()
        logger.info("✓ Fyers refreshed from saved refresh token")
        return True
    except Exception as exc:
        logger.warning(f"Fyers refresh-token restore failed: {exc}")
        return False


def _next_upstox_expiry_ist(now_utc: datetime) -> datetime:
    now_ist = now_utc.astimezone(IST)
    cutoff = now_ist.replace(hour=3, minute=30, second=0, microsecond=0)
    if now_ist >= cutoff:
        cutoff = cutoff + timedelta(days=1)
    return cutoff


async def get_upstox_token_health(force: bool = False) -> dict:
    refresh_persistent_credentials(force=force)
    upstox_creds = _broker_credentials.get("upstox", {})
    active_token = _get_active_session_access_token("upstox")
    analytics_token = str(upstox_creds.get("analytics_token", "")).strip()
    saved_token = str(upstox_creds.get("access_token", "")).strip()
    token_to_check = active_token or analytics_token or saved_token
    with _active_brokers_lock:
        connected = "upstox" in _active_brokers
    now = datetime.now(timezone.utc)

    if not token_to_check:
        return {
            "connected": connected,
            "source": "none",
            "valid": False,
            "status": "missing",
            "checked_at": None,
            "has_saved_token": bool(saved_token or analytics_token),
            "analytics_token_saved": bool(analytics_token),
            "needs_reconnect": True,
            "message": "No saved Upstox token is available. Connect Upstox in Settings.",
            "expires_at_ist": None,
        }

    checked_at = _upstox_token_health_cache.get("checked_at")
    cached_token = _upstox_token_health_cache.get("token")
    if (
        not force
        and cached_token == token_to_check
        and isinstance(checked_at, datetime)
        and now - checked_at < timedelta(seconds=45)
        and _upstox_token_health_cache.get("result") is not None
    ):
        cached = dict(_upstox_token_health_cache["result"])
        cached["connected"] = connected
        cached["source"] = "active_session" if active_token else "saved_credentials"
        return cached

    source = (
        "active_session"
        if active_token
        else ("analytics_token" if analytics_token and token_to_check == analytics_token else "saved_credentials")
    )
    expires_at_ist = _next_upstox_expiry_ist(now).isoformat()
    is_valid = (
        not _token_is_expired(token_to_check)
        if source == "analytics_token"
        else await _validate_upstox_access_token(token_to_check)
    )

    if is_valid and source == "analytics_token":
        status = "valid_analytics_token"
        message = "Upstox analytics token is saved and usable for read-only historical data/backfill."
    elif is_valid:
        status = "valid_no_refresh"
        expiry_label = _next_upstox_expiry_ist(now).strftime("%B %d, %Y %I:%M %p IST")
        message = (
            f"Upstox token is valid. Upstox access tokens expire daily at 3:30 AM IST. "
            f"This token should expire around {expiry_label}."
        )
    else:
        status = "expired_reconnect_required"
        message = (
            "Saved Upstox access token is invalid. Upstox does not provide refresh tokens "
            "for this flow, so reconnect Upstox in Settings."
        )

    result = {
        "connected": connected,
        "source": source,
        "valid": is_valid,
        "status": status,
        "checked_at": now.isoformat(),
        "has_saved_token": bool(saved_token or analytics_token),
        "analytics_token_saved": bool(analytics_token),
        "needs_reconnect": not is_valid,
        "message": message,
        "expires_at_ist": expires_at_ist,
    }
    _upstox_token_health_cache.update(
        {"token": token_to_check, "checked_at": now, "result": result}
    )
    return result


async def get_fyers_token_health(force: bool = False) -> dict:
    refresh_persistent_credentials(force=force)
    fyers_creds = _broker_credentials.get("fyers", {})
    active_token = _get_active_session_access_token("fyers")
    saved_token = str(fyers_creds.get("access_token", "")).strip()
    token_to_check = active_token or saved_token
    with _active_brokers_lock:
        connected = "fyers" in _active_brokers
    now = datetime.now(timezone.utc)

    if not token_to_check:
        return {
            "connected": connected,
            "source": "none",
            "valid": False,
            "status": "missing",
            "checked_at": None,
            "has_saved_token": bool(saved_token),
            "needs_reconnect": True,
            "message": "No saved Fyers token is available. Connect Fyers in Settings.",
        }

    checked_at = _fyers_token_health_cache.get("checked_at")
    cached_token = _fyers_token_health_cache.get("token")
    if (
        not force
        and cached_token == token_to_check
        and isinstance(checked_at, datetime)
        and now - checked_at < timedelta(seconds=45)
        and _fyers_token_health_cache.get("result") is not None
    ):
        cached = dict(_fyers_token_health_cache["result"])
        cached["connected"] = connected
        cached["source"] = "active_session" if active_token else "saved_credentials"
        return cached

    is_valid = await _validate_fyers_access_token(token_to_check)
    source = "active_session" if active_token else "saved_credentials"
    refresh_available = _has_saved_fyers_refresh_material()
    result = {
        "connected": connected,
        "source": source,
        "valid": is_valid,
        "status": "valid_session" if is_valid else "expired_reconnect_required",
        "checked_at": now.isoformat(),
        "has_saved_token": bool(saved_token),
        "refresh_available": refresh_available,
        "needs_reconnect": not is_valid,
        "message": (
            "Fyers token is valid for the current session."
            if is_valid
            else (
                "Saved Fyers access token is invalid, but encrypted refresh material is available."
                if refresh_available
                else "Saved Fyers access token is invalid. Reconnect Fyers in Settings."
            )
        ),
    }
    _fyers_token_health_cache.update(
        {"token": token_to_check, "checked_at": now, "result": result}
    )
    return result


async def _await_broker_snapshot_step(label: str, operation, timeout: float, fallback):
    try:
        return await asyncio.wait_for(operation, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(f"Broker snapshot step {label} exceeded {timeout:.1f}s; using cached/session fallback")
        return fallback
    except Exception as exc:
        logger.warning(f"Broker snapshot step {label} failed: {exc}")
        return fallback


def _broker_health_timeout_payload(broker: str, *, connected: bool, force: bool = False) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    label = BROKER_STATUS_LABELS.get(broker, broker.upper())
    source = "active_session" if connected else "none"
    return {
        "connected": connected,
        "source": source,
        "valid": connected,
        "status": "validation_timeout" if connected else "validation_unavailable",
        "checked_at": now,
        "has_saved_token": True,
        "needs_reconnect": False if connected else bool(force),
        "message": (
            f"{label} validation timed out; using the active session state for this status poll."
            if connected
            else f"{label} validation timed out and no active session is available."
        ),
    }


async def get_broker_connection_snapshot(force_validate: bool = False) -> dict[str, Any]:
    """Return a compact broker connection snapshot for UI and Telegram usage."""
    if not force_validate:
        cached = _broker_snapshot_cache.get("result")
        if isinstance(cached, dict) and float(_broker_snapshot_cache.get("expires_at") or 0.0) > monotonic():
            return dict(cached)

    async with _broker_snapshot_lock:
        if not force_validate:
            cached = _broker_snapshot_cache.get("result")
            if isinstance(cached, dict) and float(_broker_snapshot_cache.get("expires_at") or 0.0) > monotonic():
                return dict(cached)

        refresh_persistent_credentials(force=force_validate)
        step_timeout = (
            _BROKER_SNAPSHOT_FORCE_TIMEOUT_SECONDS
            if force_validate
            else _BROKER_SNAPSHOT_SESSION_TIMEOUT_SECONDS
        )
        await asyncio.gather(
            _await_broker_snapshot_step(
                "ensure_upstox_session",
                ensure_upstox_session(force_validate=force_validate),
                step_timeout,
                False,
            ),
            _await_broker_snapshot_step(
                "ensure_fyers_session",
                ensure_fyers_session(force_validate=force_validate),
                step_timeout,
                False,
            ),
        )

        session_brokers = get_connected_brokers()
        health_timeout = (
            _BROKER_SNAPSHOT_FORCE_TIMEOUT_SECONDS
            if force_validate
            else _BROKER_SNAPSHOT_HEALTH_TIMEOUT_SECONDS
        )
        upstox_health, fyers_health = await asyncio.gather(
            _await_broker_snapshot_step(
                "upstox_token_health",
                get_upstox_token_health(force=force_validate),
                health_timeout,
                _broker_health_timeout_payload(
                    "upstox",
                    connected="upstox" in session_brokers,
                    force=force_validate,
                ),
            ),
            _await_broker_snapshot_step(
                "fyers_token_health",
                get_fyers_token_health(force=force_validate),
                health_timeout,
                _broker_health_timeout_payload(
                    "fyers",
                    connected="fyers" in session_brokers,
                    force=force_validate,
                ),
            ),
        )
        upstox_ready = "upstox" in session_brokers and bool(upstox_health.get("valid"))
        fyers_ready = "fyers" in session_brokers and bool(fyers_health.get("valid"))

        connected: list[str] = []
        if fyers_ready:
            connected.append("fyers")
        if upstox_ready:
            connected.append("upstox")
        for broker in session_brokers:
            if broker not in {"fyers", "upstox"} and broker not in connected:
                connected.append(broker)

        result = {
            "connected_brokers": connected,
            "session_brokers": session_brokers,
            "upstox_ready": upstox_ready,
            "fyers_ready": fyers_ready,
            "broker_ready": upstox_ready or fyers_ready,
            "upstox_token_health": upstox_health,
            "fyers_token_health": fyers_health,
        }
        if not force_validate:
            _broker_snapshot_cache["result"] = dict(result)
            _broker_snapshot_cache["expires_at"] = monotonic() + _BROKER_SNAPSHOT_CACHE_TTL_SECONDS
        return result


def format_broker_status_summary(snapshot: dict[str, Any]) -> str:
    """Render broker connectivity as a short Telegram-safe summary line."""
    connected = set(snapshot.get("connected_brokers") or [])
    upstox_health = snapshot.get("upstox_token_health") or {}
    fyers_health = snapshot.get("fyers_token_health") or {}
    upstox_ready = bool(snapshot.get("upstox_ready"))
    fyers_ready = bool(snapshot.get("fyers_ready"))

    parts: list[str] = []
    for broker in BROKER_STATUS_ORDER:
        label = BROKER_STATUS_LABELS.get(broker, broker.upper())
        if broker == "fyers":
            if broker in connected and fyers_ready:
                state = "connected"
            else:
                state = str(fyers_health.get("status") or ("connected" if broker in connected else "disconnected"))
        elif broker == "upstox":
            if broker in connected and upstox_ready:
                state = "connected"
            else:
                state = str(upstox_health.get("status") or ("connected" if broker in connected else "disconnected"))
        else:
            state = "connected" if broker in connected else "disconnected"
        parts.append(f"{label} {state.replace('_', ' ')}")

    return "Broker Status: " + " | ".join(parts)


async def ensure_upstox_session(force_validate: bool = False) -> bool:
    """
    Restore the Upstox adapter from the saved JWT when the in-memory session is gone.

    This is needed because the backend commonly reloads in Docker dev mode, which
    clears `_active_brokers` even though credentials.json still contains a valid
    Upstox access token.
    """
    refresh_persistent_credentials(force=force_validate)
    with _active_brokers_lock:
        info = _active_brokers.get("upstox")
    if info:
        # Validate existing in-memory session — evict if token is expired
        token = info.get("token")
        access_token = getattr(token, "access_token", None) if token else None
        if access_token and not _token_is_expired(
            access_token,
            expires_at=getattr(token, "expires_at", None),
        ):
            if not force_validate or await _validate_upstox_access_token(access_token):
                # Mirror the Fyers branch: ensure the data router is wired
                # even when we short-circuit on a still-valid in-memory
                # session. Without this, a container that boots with a
                # cached adapter but no data-router init never subscribes
                # to the tick feed.
                await _ensure_feed_sync_for("upstox")
                return True
            logger.info("[Upstox] In-memory session token failed validation — evicting")
        else:
            logger.info("[Upstox] In-memory session token expired — evicting")
        with _active_brokers_lock:
            _active_brokers.pop("upstox", None)

    saved_token = str(_broker_credentials.get("upstox", {}).get("access_token", "")).strip()
    if not saved_token:
        return False

    # Don't try to restore an already-expired saved token
    if _token_is_expired(saved_token, expires_at=_broker_credentials.get("upstox", {}).get("expires_at")):
        logger.info("[Upstox] Saved token is expired — re-authentication required")
        return False

    if force_validate and not await _validate_upstox_access_token(saved_token):
        logger.warning("Saved Upstox token is invalid during on-demand restore")
        return False

    try:
        from brokers.upstox import UpstoxAdapter
        from brokers.base import UserProfile

        adapter = UpstoxAdapter()
        token = await adapter.authenticate({"access_token": saved_token})
        try:
            profile = await adapter.get_profile()
        except Exception as exc:
            logger.debug(f"Upstox profile fetch during on-demand restore failed: {exc}")
            profile = UserProfile(
                user_id="upstox_user",
                name="Upstox",
                email="",
                mobile="",
                broker="upstox",
            )

        with _active_brokers_lock:
            _active_brokers["upstox"] = {
                "adapter": adapter,
                "token": token,
                "profile": profile,
                "connected_at": datetime.utcnow().isoformat(),
                "auto_restored": True,
            }
        _persist_broker_session("upstox", token)
        await _sync_market_data_feed()
        logger.info(f"✓ Upstox restored from saved credentials (token ends …{saved_token[-8:]})")
        return True
    except Exception as exc:
        logger.warning(f"On-demand Upstox restore failed: {exc}")
        return False


async def ensure_fyers_session(force_validate: bool = False) -> bool:
    """
    Restore the Fyers adapter from a saved access token when available.

    Fyers access tokens are also session-scoped, but when the backend reloads
    during the same trading day we can usually reuse the saved token until it
    naturally expires.
    """
    refresh_persistent_credentials(force=force_validate)
    with _active_brokers_lock:
        info = _active_brokers.get("fyers")
    if info:
        # Validate existing in-memory session — evict if token is expired
        token = info.get("token")
        access_token = getattr(token, "access_token", None) if token else None
        if access_token and not _token_is_expired(
            access_token,
            expires_at=getattr(token, "expires_at", None),
        ):
            if not force_validate or await _validate_fyers_access_token(access_token):
                # The adapter is in _active_brokers but the data router
                # may not yet be wired to it (this happens at startup when
                # auto_restore returns True before the data router is fully
                # initialized, and again after every container restart that
                # finds a valid in-memory cache via persistence). Ensure
                # set_broker + subscribe runs whenever we report success.
                await _ensure_feed_sync_for("fyers")
                return True
            logger.info("[Fyers] In-memory session token failed validation — evicting")
        else:
            logger.info("[Fyers] In-memory session token expired — evicting")
        with _active_brokers_lock:
            _active_brokers.pop("fyers", None)

    saved_token = str(_broker_credentials.get("fyers", {}).get("access_token", "")).strip()
    if not saved_token:
        return await _refresh_fyers_session_from_saved_credentials()

    # Don't try to restore an already-expired saved token
    if _token_is_expired(saved_token, expires_at=_broker_credentials.get("fyers", {}).get("expires_at")):
        logger.info("[Fyers] Saved token is expired — re-authentication required")
        return await _refresh_fyers_session_from_saved_credentials()

    if force_validate and not await _validate_fyers_access_token(saved_token):
        logger.warning("Saved Fyers token is invalid during on-demand restore")
        return await _refresh_fyers_session_from_saved_credentials()

    try:
        from brokers.fyers import FyersAdapter

        adapter = FyersAdapter()
        token = await adapter.authenticate({"access_token": saved_token})
        profile = await adapter.get_profile()
        with _active_brokers_lock:
            _active_brokers["fyers"] = {
                "adapter": adapter,
                "token": token,
                "profile": profile,
                "connected_at": datetime.utcnow().isoformat(),
                "auto_restored": True,
            }
        _persist_broker_session("fyers", token)
        # Wire the newly-restored adapter into the data router so the
        # websocket subscribes to the live index feed. Without this the
        # data router stays mode=idle with subscribed_count=0, no ticks
        # flow, live_candle_store has nothing to flush, and
        # option_premium_candles goes stale within the trading day.
        try:
            await _sync_market_data_feed()
        except Exception as feed_exc:
            logger.warning(f"[Fyers] feed sync after restore failed: {feed_exc}")
        logger.info("✓ Fyers restored from saved credentials")
        return True
    except Exception as exc:
        logger.warning(f"On-demand Fyers restore failed: {exc}")
        return False


# Bootstrap immediately on module import
_bootstrap_credentials()


# ── Pydantic Models ───────────────────────────────────────────────────────────

class ConnectBrokerRequest(BaseModel):
    broker: str
    credentials: dict


class BrokerStatusResponse(BaseModel):
    broker: str
    connected: bool
    ready: bool = False
    session_active: bool = False
    state: str = "disconnected"
    detail: Optional[str] = None
    source: Optional[str] = None
    checked_at: Optional[str] = None
    needs_reconnect: bool = False
    user_id: Optional[str] = None
    name: Optional[str] = None
    connected_at: Optional[str] = None


class SaveCredentialsRequest(BaseModel):
    broker: str
    credentials: dict


class TelegramSettingsRequest(BaseModel):
    bot_token: str = ""
    chat_id: str = ""
    enabled: bool = False
    report_interval: str = "1h"


class TelegramChatLookupRequest(BaseModel):
    bot_token: str = ""


class TelegramTestRequest(BaseModel):
    message: str = ""


def _websocket_subject(websocket: WebSocket) -> str:
    client = websocket.client
    return f"{client.host}:{client.port}" if client else "unknown"


def _mint_websocket_token(subject: str) -> tuple[str, str]:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=WS_TOKEN_TTL_SECONDS)
    token = issue_ephemeral_token(
        scope="websocket",
        subject=subject,
        ttl_seconds=WS_TOKEN_TTL_SECONDS,
    )
    return token, expires_at.isoformat()


def _append_query_param(url: str, key: str, value: str) -> str:
    parts = urlsplit(url)
    query = [(item_key, item_value) for item_key, item_value in parse_qsl(parts.query, keep_blank_values=True) if item_key != key]
    query.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _mint_oauth_state(broker: str) -> str:
    return issue_ephemeral_token(
        scope=f"oauth:{broker}",
        subject="browser-client",
        ttl_seconds=OAUTH_STATE_TTL_SECONDS,
    )


def _verify_oauth_state(broker: str, state: str | None) -> None:
    token = str(state or "").strip()
    if not token:
        raise HTTPException(status_code=403, detail="Missing OAuth state.")
    try:
        verify_ephemeral_token(token, expected_scope=f"oauth:{broker}")
    except Exception as exc:
        raise HTTPException(status_code=403, detail=f"Invalid OAuth state: {exc}")


def authenticate_websocket_client(websocket: WebSocket) -> dict[str, Any]:
    token = str(
        websocket.query_params.get("auth")
        or websocket.query_params.get("token")
        or ""
    ).strip()
    if not token:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="missing websocket token")
    try:
        claims = verify_ephemeral_token(token, expected_scope="websocket")
    except Exception as exc:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason=f"invalid websocket token: {exc}")
    return claims


# ── Credential Management ──────────────────────────────────────────────────────

@router.post("/save-credentials")
async def save_credentials(req: SaveCredentialsRequest):
    """
    Save broker API credentials.
    Persisted to the durable store and local dev file fallback.
    Also pushed into live settings immediately.
    """
    refresh_persistent_credentials()
    if req.broker not in BROKER_MAP:
        raise HTTPException(400, f"Unknown broker: {req.broker}")

    # Merge with existing (partial saves allowed — don't wipe other fields)
    existing = _broker_credentials.get(req.broker, {})
    merged = {**existing, **{k: v for k, v in req.credentials.items() if v}}
    if req.broker == "upstox":
        api_key_candidate = req.credentials.get("api_key")
        secret_candidate = req.credentials.get("secret")
        if api_key_candidate is not None and _looks_like_placeholder_credential(api_key_candidate):
            raise HTTPException(
                400,
                "Upstox API Key looks like a placeholder. Save the real API key from account.upstox.com → Developer Apps.",
            )
        if secret_candidate is not None and _looks_like_placeholder_credential(secret_candidate):
            raise HTTPException(
                400,
                "Upstox Secret looks like a placeholder. Save the real secret from account.upstox.com → Developer Apps.",
            )
    if req.broker == "fyers":
        merged["redirect_uri"] = normalize_fyers_redirect_uri(
            req.credentials.get("redirect_uri")
            or existing.get("redirect_uri")
            or FYERS_FIXED_REDIRECT_URI
        )
    elif req.broker == "upstox":
        merged["redirect_uri"] = normalize_upstox_redirect_uri(
            req.credentials.get("redirect_uri")
            or existing.get("redirect_uri")
            or UPSTOX_SANDBOX_REDIRECT_URI
        )
    _broker_credentials[req.broker] = merged

    # Persist immediately so cloud revisions can restore later.
    _persist_credentials(_broker_credentials)

    # Push into live settings
    _apply_credentials_to_settings(req.broker, merged)

    logger.info(f"Credentials saved for {req.broker}: {list(req.credentials.keys())}")
    return {
        "status": "saved",
        "broker": req.broker,
        "fields": list(merged.keys()),
    }


@router.get("/credentials/{broker}")
async def get_credentials_status(broker: str):
    """
    Return which credential fields are saved for a broker.
    Values are never returned — only field names and presence.
    """
    refresh_persistent_credentials()
    if broker not in BROKER_MAP:
        raise HTTPException(400, f"Unknown broker: {broker}")
    creds = _broker_credentials.get(broker, {})
    filled = {k: bool(v) for k, v in creds.items()}
    display: dict[str, str] = {}
    if broker == "fyers":
        if creds.get("app_id"):
            display["app_id"] = str(creds["app_id"])
        display["redirect_uri"] = normalize_fyers_redirect_uri(
            str(creds.get("redirect_uri") or settings.FYERS_REDIRECT_URI or "")
        )
    elif broker == "upstox":
        if creds.get("api_key"):
            display["api_key"] = str(creds["api_key"])
        display["redirect_uri"] = normalize_upstox_redirect_uri(
            str(creds.get("redirect_uri") or settings.UPSTOX_REDIRECT_URI or "")
        )
    return {
        "broker": broker,
        "has_credentials": any(filled.values()),
        "fields": filled,   # {field_name: true/false}
        "display": display,
    }


@router.get("/all-credentials-status")
async def all_credentials_status():
    """Return credential field presence for all brokers at once."""
    refresh_persistent_credentials()
    result = {}
    for broker in BROKER_MAP:
        creds = _broker_credentials.get(broker, {})
        result[broker] = {
            "has_credentials": any(bool(v) for v in creds.values()),
            "fields": {k: bool(v) for k, v in creds.items()},
        }
    telegram = _broker_credentials.get("telegram", {})
    result["telegram"] = {
        "has_credentials": bool(telegram.get("bot_token")) and bool(telegram.get("chat_id")),
        "fields": {k: bool(v) for k, v in telegram.items() if k in {"bot_token", "chat_id", "enabled", "report_interval"}},
    }
    return result


@router.get("/ws-token")
async def websocket_token():
    token, expires_at = _mint_websocket_token("browser-client")
    return {
        "token": token,
        "expires_at": expires_at,
    }


@router.get("/telegram-settings")
async def get_telegram_settings():
    refresh_persistent_credentials(force=True)
    saved = _broker_credentials.get("telegram", {})
    return {
        "bot_token_saved": bool(saved.get("bot_token")),
        "chat_id_saved": bool(saved.get("chat_id")),
        "enabled": bool(saved.get("enabled", settings.TELEGRAM_REPORTS_ENABLED)),
        "report_interval": str(saved.get("report_interval") or settings.TELEGRAM_REPORT_INTERVAL or "1h"),
        "has_destination": bool(saved.get("bot_token")) and bool(saved.get("chat_id")),
    }


@router.post("/telegram-discover-chats")
async def telegram_discover_chats(req: TelegramChatLookupRequest):
    refresh_persistent_credentials()
    saved = _broker_credentials.get("telegram", {})
    bot_token = (req.bot_token or saved.get("bot_token") or "").strip()
    if not bot_token:
        raise HTTPException(400, "Provide a bot token first.")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"https://api.telegram.org/bot{bot_token}/getUpdates",
                params={"limit": 100},
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        raise HTTPException(400, f"Telegram lookup failed: {exc}")

    if not payload.get("ok"):
        raise HTTPException(400, payload.get("description") or "Telegram rejected the token.")

    chats: dict[str, dict] = {}
    for update in payload.get("result") or []:
        for key in ("message", "edited_message", "channel_post", "edited_channel_post", "my_chat_member", "chat_member"):
            block = update.get(key)
            if not isinstance(block, dict):
                continue
            chat = block.get("chat")
            if not isinstance(chat, dict):
                continue
            chat_id = chat.get("id")
            if chat_id is None:
                continue
            title = chat.get("title") or " ".join(part for part in [chat.get("first_name"), chat.get("last_name")] if part) or chat.get("username") or str(chat_id)
            chats[str(chat_id)] = {
                "chat_id": str(chat_id),
                "title": title,
                "type": chat.get("type") or "unknown",
                "username": chat.get("username"),
            }

    return {
        "count": len(chats),
        "chats": sorted(chats.values(), key=lambda item: (item["type"], item["title"].lower())),
        "hint": (
            "If no chats appear, send /start to the bot in a private chat or add the bot to the target group/channel and create one update there, then refresh."
        ),
    }


@router.post("/telegram-settings")
async def save_telegram_settings(req: TelegramSettingsRequest):
    refresh_persistent_credentials()
    saved = _broker_credentials.get("telegram", {})
    merged = {
        **saved,
        **({
            "bot_token": req.bot_token.strip(),
        } if req.bot_token.strip() else {}),
        **({
            "chat_id": req.chat_id.strip(),
        } if req.chat_id.strip() else {}),
        "enabled": bool(req.enabled),
        "report_interval": req.report_interval,
    }
    _broker_credentials["telegram"] = merged
    _persist_credentials(_broker_credentials)
    _apply_credentials_to_settings("telegram", merged)
    return {
        "status": "saved",
        "enabled": bool(merged.get("enabled")),
        "report_interval": str(merged.get("report_interval") or "1h"),
        "bot_token_saved": bool(merged.get("bot_token")),
        "chat_id_saved": bool(merged.get("chat_id")),
    }


@router.post("/telegram-test")
async def send_telegram_test(req: TelegramTestRequest):
    refresh_persistent_credentials(force=True)
    saved = _broker_credentials.get("telegram", {})
    bot_token = str(saved.get("bot_token") or "").strip()
    chat_id = str(saved.get("chat_id") or "").strip()
    if not bot_token or not chat_id:
        raise HTTPException(400, "Telegram bot token and chat ID must be saved first.")

    text = (req.message or "").strip() or (
        f"Nomad Curie test message\n"
        f"Time: {datetime.now(IST).strftime('%d %b %Y %I:%M:%S %p IST')}\n"
        f"Status: Telegram delivery is configured correctly."
    )
    snapshot = await get_broker_connection_snapshot()
    text = f"{text}\n{format_broker_status_summary(snapshot)}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        raise HTTPException(400, f"Telegram test failed: {exc}")

    if not payload.get("ok"):
        raise HTTPException(400, payload.get("description") or "Telegram test failed.")

    return {
        "status": "sent",
        "message": "Telegram test message sent.",
    }


# ── Generic Connect/Disconnect ────────────────────────────────────────────────

@router.post("/connect-broker")
async def connect_broker(req: ConnectBrokerRequest):
    """Authenticate with a broker and store session."""
    refresh_persistent_credentials(force=True)
    if req.broker not in BROKER_MAP:
        raise HTTPException(400, f"Unknown broker: {req.broker}")

    adapter = get_broker(req.broker)
    try:
        token = await adapter.authenticate(req.credentials)
        profile = await adapter.get_profile()
        with _active_brokers_lock:
            _active_brokers[req.broker] = {
                "adapter": adapter,
                "token": token,
                "profile": profile,
                "connected_at": datetime.utcnow().isoformat(),
            }
        if req.broker in {"upstox", "fyers"}:
            _persist_broker_session(req.broker, token)
        await _sync_market_data_feed()
        return {
            "status": "connected",
            "broker": req.broker,
            "user_id": profile.user_id,
            "name": profile.name,
        }
    except Exception as e:
        raise HTTPException(400, f"Authentication failed: {str(e)}")


@router.post("/disconnect-broker")
async def disconnect_broker(broker: str):
    with _active_brokers_lock:
        _active_brokers.pop(broker, None)
    await _sync_market_data_feed()
    return {"status": "disconnected", "broker": broker}


@router.get("/broker-status")
async def broker_status(force_validate: bool = Query(False)):
    refresh_persistent_credentials(force=False)
    snapshot = await get_broker_connection_snapshot(force_validate=force_validate)
    ready_brokers = set(snapshot.get("connected_brokers") or [])
    session_brokers = set(snapshot.get("session_brokers") or [])
    upstox_health = snapshot.get("upstox_token_health") or {}
    fyers_health = snapshot.get("fyers_token_health") or {}
    statuses = []
    with _active_brokers_lock:
        active_snapshot = list(_active_brokers.items())
        active_names = set(_active_brokers.keys())
    info_by_broker = {broker: info for broker, info in active_snapshot}
    for broker in BROKER_MAP:
        info = info_by_broker.get(broker, {})
        profile = info.get("profile")
        session_active = broker in session_brokers or broker in active_names
        connected = broker in ready_brokers
        ready = connected
        state = "connected" if connected else "disconnected"
        detail = None
        source = None
        checked_at = None
        needs_reconnect = False

        if broker == "upstox":
            ready = bool(snapshot.get("upstox_ready"))
            connected = ready
            state = "connected" if ready else str(upstox_health.get("status") or "disconnected")
            detail = str(upstox_health.get("message") or "").strip() or None
            source = str(upstox_health.get("source") or "").strip() or None
            checked_at = str(upstox_health.get("checked_at") or "").strip() or None
            needs_reconnect = bool(upstox_health.get("needs_reconnect"))
        elif broker == "fyers":
            ready = bool(snapshot.get("fyers_ready"))
            connected = ready
            state = "connected" if ready else str(fyers_health.get("status") or "disconnected")
            detail = str(fyers_health.get("message") or "").strip() or None
            source = str(fyers_health.get("source") or "").strip() or None
            checked_at = str(fyers_health.get("checked_at") or "").strip() or None
            needs_reconnect = bool(fyers_health.get("needs_reconnect"))

        statuses.append(
            BrokerStatusResponse(
                broker=broker,
                connected=connected,
                ready=ready,
                session_active=session_active,
                state=state,
                detail=detail,
                source=source,
                checked_at=checked_at,
                needs_reconnect=needs_reconnect,
                user_id=profile.user_id if profile else None,
                name=profile.name if profile else None,
                connected_at=info.get("connected_at"),
            )
        )
    return statuses


# ── Fyers OAuth ───────────────────────────────────────────────────────────────

@router.get("/fyers/auth-url")
async def fyers_auth_url():
    if not settings.FYERS_APP_ID:
        raise HTTPException(400, "FYERS_APP_ID not configured. Save credentials first.")
    from brokers.fyers import FyersAdapter
    auth_url = _append_query_param(FyersAdapter().get_auth_url(), "state", _mint_oauth_state("fyers"))
    return {"auth_url": auth_url}


@router.get("/fyers/callback")
async def fyers_callback(auth_code: str = None, code: str = None, state: str = None):
    """Fyers OAuth callback — accepts both auth_code= and code= query params."""
    from brokers.fyers import FyersAdapter
    from fastapi.responses import HTMLResponse
    _verify_oauth_state("fyers", state)
    actual_code = auth_code or code
    if not actual_code:
        from fastapi.responses import HTMLResponse
        return HTMLResponse(
            content="<html><body style='background:#080b18;color:#ff4444'>Missing auth_code parameter.</body></html>",
            status_code=400,
        )
    adapter = FyersAdapter()
    token = await adapter.authenticate({"auth_code": actual_code})
    profile = await adapter.get_profile()
    with _active_brokers_lock:
        _active_brokers["fyers"] = {
            "adapter": adapter, "token": token, "profile": profile,
            "connected_at": datetime.utcnow().isoformat(),
        }
    _persist_broker_session("fyers", token)
    await _sync_market_data_feed()
    return HTMLResponse(content="""
    <html><body style="background:#080b18;color:#00ff88;font-family:monospace;padding:2rem">
    <h2>✓ Fyers connected successfully!</h2>
    <p>You can close this tab and return to Nomad Curie.</p>
    <script>
      window.opener && window.opener.postMessage({broker:'fyers',status:'connected'}, '*');
      setTimeout(() => window.close(), 2000);
    </script></body></html>
    """)


# ── Upstox OAuth ──────────────────────────────────────────────────────────────

@router.get("/upstox/auth-url")
async def upstox_auth_url():
    if not settings.UPSTOX_API_KEY:
        raise HTTPException(400, "UPSTOX_API_KEY not configured. Save credentials first.")
    if _looks_like_placeholder_credential(settings.UPSTOX_API_KEY):
        raise HTTPException(
            400,
            "Saved Upstox API Key is a placeholder. Replace it with the real API key from account.upstox.com → Developer Apps.",
        )
    from brokers.upstox import UpstoxAdapter
    auth_url = _append_query_param(UpstoxAdapter().get_auth_url(), "state", _mint_oauth_state("upstox"))
    return {"auth_url": auth_url}


@router.post("/upstox/connect")
async def upstox_connect_manual(body: dict):
    """
    Manual Upstox connect — for sandbox apps where the redirect URL is google.com.
    Accepts either:
      - A short auth code from the Google URL (code= param) → exchanges for token
      - A JWT access token starting with 'eyJ' → used directly (no exchange)
    """
    from brokers.upstox import UpstoxAdapter
    from brokers.base import UserProfile
    code = body.get("code", "").strip()
    if not code:
        raise HTTPException(400, "code is required — paste the auth code or access token")
    adapter = UpstoxAdapter()
    try:
        token = await adapter.authenticate({"code": code})
    except Exception as e:
        raise HTTPException(400, f"Upstox token exchange failed: {str(e)}")

    # get_profile() may fail for sandbox tokens that lack profile scope — non-fatal
    try:
        profile = await adapter.get_profile()
    except Exception as e:
        logger.warning(f"Upstox profile fetch failed (non-fatal, continuing): {e}")
        profile = UserProfile(
            user_id="upstox_user", name="Upstox", email="", mobile="", broker="upstox"
        )

    with _active_brokers_lock:
        _active_brokers["upstox"] = {
            "adapter": adapter, "token": token, "profile": profile,
            "connected_at": datetime.utcnow().isoformat(),
        }

    # Persist the access token so it auto-restores until the daily Upstox expiry.
    _persist_broker_session("upstox", token)
    logger.info(f"Upstox connected — token persisted for auto-restore (ends …{token.access_token[-8:]})")
    await _sync_market_data_feed()

    return {"status": "connected", "broker": "upstox",
            "user_id": profile.user_id, "name": profile.name}


@router.get("/upstox/callback")
async def upstox_callback(code: str, state: str = None):
    from brokers.upstox import UpstoxAdapter
    from brokers.base import UserProfile
    from fastapi.responses import HTMLResponse
    _verify_oauth_state("upstox", state)
    adapter = UpstoxAdapter()
    token = await adapter.authenticate({"code": code})
    try:
        profile = await adapter.get_profile()
    except Exception as e:
        logger.warning(f"Upstox callback profile fetch failed: {e}")
        profile = UserProfile(user_id="upstox_user", name="Upstox", email="", mobile="", broker="upstox")
    with _active_brokers_lock:
        _active_brokers["upstox"] = {
            "adapter": adapter, "token": token, "profile": profile,
            "connected_at": datetime.utcnow().isoformat(),
        }
    _persist_broker_session("upstox", token)
    await _sync_market_data_feed()
    return HTMLResponse(content="""
    <html><body style="background:#080b18;color:#00ff88;font-family:monospace;padding:2rem">
    <h2>✓ Upstox connected successfully!</h2>
    <p>You can close this tab and return to Nomad Curie.</p>
    <script>
      window.opener && window.opener.postMessage({broker:'upstox',status:'connected'}, '*');
      setTimeout(() => window.close(), 2000);
    </script></body></html>
    """)


# ── ICICI Breeze ──────────────────────────────────────────────────────────────

@router.get("/icici-breeze/login-url")
async def icici_breeze_login_url():
    if not settings.ICICI_BREEZE_API_KEY:
        raise HTTPException(400, "ICICI_BREEZE_API_KEY not configured. Save credentials first.")
    from brokers.icici_breeze import ICICIBreezeAdapter
    adapter = ICICIBreezeAdapter()
    return {
        "login_url": adapter.get_login_url(),
        "instructions": (
            "1. Click the login URL to open ICICI Direct login page. "
            "2. Log in with your ICICI Direct credentials. "
            "3. After login, you will be redirected to a URL containing '?apisession=TOKEN'. "
            "4. Copy that TOKEN and paste it in the Session Token field below. "
            "5. Click Connect."
        ),
    }


@router.post("/icici-breeze/connect")
async def icici_breeze_connect(body: dict):
    from brokers.icici_breeze import ICICIBreezeAdapter
    adapter = ICICIBreezeAdapter()
    session_token = body.get("session_token", "")
    if not session_token:
        raise HTTPException(400, "session_token is required")
    credentials = {
        "session_token": session_token,
        "api_secret": body.get("api_secret", settings.ICICI_BREEZE_SECRET),
    }
    try:
        token = await adapter.authenticate(credentials)
        profile = await adapter.get_profile()
        with _active_brokers_lock:
            _active_brokers["icici_breeze"] = {
                "adapter": adapter, "token": token, "profile": profile,
                "connected_at": datetime.utcnow().isoformat(),
            }
        await _sync_market_data_feed()
        return {"status": "connected", "broker": "icici_breeze",
                "user_id": profile.user_id, "name": profile.name}
    except Exception as e:
        raise HTTPException(400, f"ICICI Breeze authentication failed: {str(e)}")


# ── 5Paisa Direct Auth ────────────────────────────────────────────────────────

@router.post("/fivepaisa/connect")
async def fivepaisa_connect(body: dict):
    from brokers.fivepaisa import FivePaisaAdapter
    adapter = FivePaisaAdapter()
    totp = body.get("totp", "")
    if not totp:
        raise HTTPException(400, "totp is required")
    try:
        token = await adapter.authenticate({"totp": totp})
        profile = await adapter.get_profile()
        with _active_brokers_lock:
            _active_brokers["fivepaisa"] = {
                "adapter": adapter, "token": token, "profile": profile,
                "connected_at": datetime.utcnow().isoformat(),
            }
        await _sync_market_data_feed()
        return {"status": "connected", "broker": "fivepaisa",
                "user_id": profile.user_id, "name": profile.name}
    except Exception as e:
        raise HTTPException(400, f"5Paisa authentication failed: {str(e)}")


# ── Dependency ────────────────────────────────────────────────────────────────

def get_active_adapter(broker: Optional[str] = None):
    with _active_brokers_lock:
        if broker:
            info = _active_brokers.get(broker)
            return info["adapter"] if info else None
        for info in _active_brokers.values():
            return info["adapter"]
    return None


def get_broker_token(broker: str) -> Optional[str]:
    """Return the access token string for an active broker session or saved token."""
    with _active_brokers_lock:
        info = _active_brokers.get(broker)
    def _saved_token() -> Optional[str]:
        if broker == "upstox":
            analytics_token = str(_broker_credentials.get("upstox", {}).get("analytics_token", "")).strip()
            if analytics_token:
                return analytics_token
        saved = str(_broker_credentials.get(broker, {}).get("access_token", "")).strip()
        return saved or None

    if not info:
        return _saved_token()
    token = info.get("token")
    if token is None:
        return _saved_token()
    active_token = str(getattr(token, "access_token", None) or "").strip()
    if active_token and not _token_is_expired(
        active_token,
        expires_at=getattr(token, "expires_at", None),
    ):
        return active_token
    return _saved_token()


def get_connected_brokers() -> list[str]:
    """Return list of currently connected broker names."""
    with _active_brokers_lock:
        return list(_active_brokers.keys())


async def _ensure_feed_sync_for(broker: str) -> None:
    """Sync the data router when an ensure_* path reports success.

    Called from the early-return branches of ensure_fyers_session and
    ensure_upstox_session — i.e. paths where the in-memory adapter was
    already valid and we exit before running the saved-token restore
    path. Without this hook the data router can stay mode=idle for the
    entire session even though the broker is connected.

    Wrapped in try/except so a feed-sync error does not flip a valid
    session restore into a failure return.
    """
    try:
        await _sync_market_data_feed()
    except Exception as exc:
        logger.warning(
            f"[{broker.capitalize()}] feed sync after in-memory session "
            f"validation failed: {exc}"
        )


async def _sync_market_data_feed() -> None:
    """Keep the shared index tick feed aligned with the current active broker."""
    from market_data import data_router as market_data_router
    from market_data.source_policy import choose_active_adapter, source_policy_snapshot
    from market_data.symbols import LIVE_INDEX_APP_SYMBOLS

    active_adapters = {
        "fyers": get_active_adapter("fyers"),
        "upstox": get_active_adapter("upstox"),
    }
    active_brokers = [name for name, adapter in active_adapters.items() if adapter is not None]
    adapter, source, decisions = choose_active_adapter("live_ticks", active_adapters)
    market_data_router.set_source_policy(
        source_policy_snapshot(
            active_brokers=active_brokers,
            selected_live_source=source,
            route_decisions=decisions,
        )
    )
    if adapter:
        market_data_router.set_broker(adapter)
        await market_data_router.subscribe(list(LIVE_INDEX_APP_SYMBOLS))
    else:
        await market_data_router.stop_mock_feed()
        await market_data_router.unsubscribe()


async def auto_restore_sessions() -> None:
    """
    Called at server startup. Attempts to restore broker sessions from saved
    credentials without requiring the user to re-authenticate.

    Currently restores:
      - Upstox: if a saved access_token exists in credentials.json and still validates,
        uses it directly until the daily Upstox expiry.
    """
    if str(_broker_credentials.get("upstox", {}).get("access_token", "")).strip():
        logger.info("Auto-restoring Upstox session from saved access token…")
        try:
            restored = await asyncio.wait_for(
                ensure_upstox_session(force_validate=True),
                timeout=AUTO_RESTORE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            restored = False
            logger.warning("Upstox auto-restore timed out after 10 seconds")
        if not restored:
            logger.warning("Upstox auto-restore failed — manual connect required")
    if str(_broker_credentials.get("fyers", {}).get("access_token", "")).strip():
        logger.info("Auto-restoring Fyers session from saved access token…")
        try:
            restored = await asyncio.wait_for(
                ensure_fyers_session(force_validate=True),
                timeout=AUTO_RESTORE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            restored = False
            logger.warning("Fyers auto-restore timed out after 10 seconds")
        if not restored:
            logger.warning("Fyers auto-restore failed — manual connect required")
    elif _has_saved_fyers_refresh_material():
        logger.info("Auto-refreshing Fyers session from saved refresh token…")
        try:
            restored = await asyncio.wait_for(
                _refresh_fyers_session_from_saved_credentials(),
                timeout=AUTO_RESTORE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            restored = False
            logger.warning("Fyers refresh-token restore timed out after 10 seconds")
        if not restored:
            logger.warning("Fyers refresh-token restore failed — manual connect required")
