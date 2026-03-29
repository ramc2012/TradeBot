"""Broker authentication routes."""
from __future__ import annotations
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import httpx
from loguru import logger

from brokers import get_broker, BROKER_MAP
from core.config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])

# In-memory broker registry (active connections this session)
_active_brokers: dict = {}

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
IST = timezone(timedelta(hours=5, minutes=30))


# ── Credential persistence ────────────────────────────────────────────────────

def _load_credentials() -> dict:
    """Load saved broker credentials from disk."""
    if _CREDS_FILE.exists():
        try:
            return json.loads(_CREDS_FILE.read_text())
        except Exception as e:
            logger.warning(f"Could not load credentials.json: {e}")
    return {}


def _save_credentials_to_disk(creds: dict) -> None:
    """Persist broker credentials to disk."""
    try:
        _CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CREDS_FILE.write_text(json.dumps(creds, indent=2))
        logger.debug(f"Credentials saved to {_CREDS_FILE}")
    except Exception as e:
        logger.error(f"Could not write credentials to {_CREDS_FILE}: {e}")


def _reset_upstox_token_health_cache() -> None:
    _upstox_token_health_cache.update(
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
    _save_credentials_to_disk(_broker_credentials)
    if broker == "upstox":
        _reset_upstox_token_health_cache()


def _apply_credentials_to_settings(broker: str, creds: dict) -> None:
    """Push saved credentials into the live settings object."""
    if broker == "fyers":
        if creds.get("app_id"):   settings.FYERS_APP_ID = creds["app_id"]
        if creds.get("secret"):   settings.FYERS_SECRET = creds["secret"]
        if creds.get("redirect_uri"): settings.FYERS_REDIRECT_URI = creds["redirect_uri"]
    elif broker == "upstox":
        if creds.get("api_key"):      settings.UPSTOX_API_KEY = creds["api_key"]
        if creds.get("secret"):       settings.UPSTOX_SECRET = creds["secret"]
        if creds.get("redirect_uri"): settings.UPSTOX_REDIRECT_URI = creds["redirect_uri"]
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


def _bootstrap_credentials() -> None:
    """
    Called at startup. Loads credentials.json into memory,
    then fills any gaps from environment variables / .env.
    """
    global _broker_credentials
    # Load persisted file first
    _broker_credentials = _load_credentials()

    # Also seed from .env / environment so manually set env vars always win
    env_seed = {
        "fyers": {
            "app_id": settings.FYERS_APP_ID,
            "secret": settings.FYERS_SECRET,
            "redirect_uri": settings.FYERS_REDIRECT_URI,
        },
        "upstox": {
            "api_key": settings.UPSTOX_API_KEY,
            "secret": settings.UPSTOX_SECRET,
            "redirect_uri": settings.UPSTOX_REDIRECT_URI,
        },
        "fivepaisa": {
            "app_name":       settings.FIVEPAISA_APP_NAME,
            "app_source":     settings.FIVEPAISA_APP_SOURCE,
            "user_id":        settings.FIVEPAISA_USER_ID,
            "email":          settings.FIVEPAISA_EMAIL,
            "password":       settings.FIVEPAISA_PASSWORD,
            "user_key":       settings.FIVEPAISA_USER_KEY,
            "encryption_key": settings.FIVEPAISA_ENCRYPTION_KEY,
        },
        "icici_breeze": {
            "api_key": settings.ICICI_BREEZE_API_KEY,
            "secret":  settings.ICICI_BREEZE_SECRET,
        },
    }
    for broker, env_creds in env_seed.items():
        filled = {k: v for k, v in env_creds.items() if v}
        if filled:
            existing = _broker_credentials.get(broker, {})
            # env overrides file for fields present in env
            merged = {**existing, **filled}
            _broker_credentials[broker] = merged

    # Apply everything to live settings
    for broker, creds in _broker_credentials.items():
        _apply_credentials_to_settings(broker, creds)

    saved = [b for b, c in _broker_credentials.items() if c]
    if saved:
        logger.info(f"Credentials loaded for: {', '.join(saved)}")


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


def _next_upstox_expiry_ist(now_utc: datetime) -> datetime:
    now_ist = now_utc.astimezone(IST)
    cutoff = now_ist.replace(hour=3, minute=30, second=0, microsecond=0)
    if now_ist >= cutoff:
        cutoff = cutoff + timedelta(days=1)
    return cutoff


async def get_upstox_token_health(force: bool = False) -> dict:
    upstox_creds = _broker_credentials.get("upstox", {})
    active_token = get_broker_token("upstox")
    saved_token = str(upstox_creds.get("access_token", "")).strip()
    token_to_check = active_token or saved_token
    connected = "upstox" in _active_brokers
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

    is_valid = await _validate_upstox_access_token(token_to_check)
    source = "active_session" if active_token else "saved_credentials"
    expires_at_ist = _next_upstox_expiry_ist(now).isoformat()

    if is_valid:
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
        "has_saved_token": bool(saved_token),
        "needs_reconnect": not is_valid,
        "message": message,
        "expires_at_ist": expires_at_ist,
    }
    _upstox_token_health_cache.update(
        {"token": token_to_check, "checked_at": now, "result": result}
    )
    return result


async def ensure_upstox_session(force_validate: bool = False) -> bool:
    """
    Restore the Upstox adapter from the saved JWT when the in-memory session is gone.

    This is needed because the backend commonly reloads in Docker dev mode, which
    clears `_active_brokers` even though credentials.json still contains a valid
    Upstox access token.
    """
    if "upstox" in _active_brokers:
        return True

    saved_token = str(_broker_credentials.get("upstox", {}).get("access_token", "")).strip()
    if not saved_token:
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

        _active_brokers["upstox"] = {
            "adapter": adapter,
            "token": token,
            "profile": profile,
            "connected_at": datetime.utcnow().isoformat(),
            "auto_restored": True,
        }
        await _sync_market_data_feed()
        logger.info(f"✓ Upstox restored from saved credentials (token ends …{saved_token[-8:]})")
        return True
    except Exception as exc:
        logger.warning(f"On-demand Upstox restore failed: {exc}")
        return False


async def ensure_fyers_session() -> bool:
    """
    Restore the Fyers adapter from a saved access token when available.

    Fyers access tokens are also session-scoped, but when the backend reloads
    during the same trading day we can usually reuse the saved token until it
    naturally expires.
    """
    if "fyers" in _active_brokers:
        return True

    saved_token = str(_broker_credentials.get("fyers", {}).get("access_token", "")).strip()
    if not saved_token:
        return False

    try:
        from brokers.fyers import FyersAdapter

        adapter = FyersAdapter()
        token = await adapter.authenticate({"access_token": saved_token})
        profile = await adapter.get_profile()
        _active_brokers["fyers"] = {
            "adapter": adapter,
            "token": token,
            "profile": profile,
            "connected_at": datetime.utcnow().isoformat(),
            "auto_restored": True,
        }
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
    user_id: Optional[str] = None
    name: Optional[str] = None
    connected_at: Optional[str] = None


class SaveCredentialsRequest(BaseModel):
    broker: str
    credentials: dict


# ── Credential Management ──────────────────────────────────────────────────────

@router.post("/save-credentials")
async def save_credentials(req: SaveCredentialsRequest):
    """
    Save broker API credentials.
    Persisted to credentials.json on disk — survives container restarts.
    Also pushed into live settings immediately.
    """
    if req.broker not in BROKER_MAP:
        raise HTTPException(400, f"Unknown broker: {req.broker}")

    # Merge with existing (partial saves allowed — don't wipe other fields)
    existing = _broker_credentials.get(req.broker, {})
    merged = {**existing, **{k: v for k, v in req.credentials.items() if v}}
    _broker_credentials[req.broker] = merged

    # Persist to disk immediately
    _save_credentials_to_disk(_broker_credentials)

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
    if broker not in BROKER_MAP:
        raise HTTPException(400, f"Unknown broker: {broker}")
    creds = _broker_credentials.get(broker, {})
    filled = {k: bool(v) for k, v in creds.items()}
    return {
        "broker": broker,
        "has_credentials": any(filled.values()),
        "fields": filled,   # {field_name: true/false}
    }


@router.get("/all-credentials-status")
async def all_credentials_status():
    """Return credential field presence for all brokers at once."""
    result = {}
    for broker in BROKER_MAP:
        creds = _broker_credentials.get(broker, {})
        result[broker] = {
            "has_credentials": any(bool(v) for v in creds.values()),
            "fields": {k: bool(v) for k, v in creds.items()},
        }
    return result


# ── Generic Connect/Disconnect ────────────────────────────────────────────────

@router.post("/connect-broker")
async def connect_broker(req: ConnectBrokerRequest):
    """Authenticate with a broker and store session."""
    if req.broker not in BROKER_MAP:
        raise HTTPException(400, f"Unknown broker: {req.broker}")

    adapter = get_broker(req.broker)
    try:
        token = await adapter.authenticate(req.credentials)
        profile = await adapter.get_profile()
        _active_brokers[req.broker] = {
            "adapter": adapter,
            "token": token,
            "profile": profile,
            "connected_at": datetime.utcnow().isoformat(),
        }
        if req.broker == "upstox":
            _persist_access_token("upstox", getattr(token, "access_token", None))
        if req.broker == "fyers":
            _persist_access_token("fyers", getattr(token, "access_token", None))
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
    if broker in _active_brokers:
        _active_brokers.pop(broker)
    await _sync_market_data_feed()
    return {"status": "disconnected", "broker": broker}


@router.get("/broker-status")
async def broker_status():
    await ensure_upstox_session(force_validate=False)
    await ensure_fyers_session()
    statuses = []
    for broker, info in _active_brokers.items():
        profile = info.get("profile")
        statuses.append(BrokerStatusResponse(
            broker=broker,
            connected=True,
            user_id=profile.user_id if profile else None,
            name=profile.name if profile else None,
            connected_at=info.get("connected_at"),
        ))
    for broker in BROKER_MAP:
        if broker not in _active_brokers:
            statuses.append(BrokerStatusResponse(broker=broker, connected=False))
    return statuses


# ── Fyers OAuth ───────────────────────────────────────────────────────────────

@router.get("/fyers/auth-url")
async def fyers_auth_url():
    if not settings.FYERS_APP_ID:
        raise HTTPException(400, "FYERS_APP_ID not configured. Save credentials first.")
    from brokers.fyers import FyersAdapter
    return {"auth_url": FyersAdapter().get_auth_url()}


@router.get("/fyers/callback")
async def fyers_callback(auth_code: str = None, code: str = None):
    """Fyers OAuth callback — accepts both auth_code= and code= query params."""
    from brokers.fyers import FyersAdapter
    from fastapi.responses import HTMLResponse
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
    _active_brokers["fyers"] = {
        "adapter": adapter, "token": token, "profile": profile,
        "connected_at": datetime.utcnow().isoformat(),
    }
    _persist_access_token("fyers", token.access_token)
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
    from brokers.upstox import UpstoxAdapter
    return {"auth_url": UpstoxAdapter().get_auth_url()}


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

    _active_brokers["upstox"] = {
        "adapter": adapter, "token": token, "profile": profile,
        "connected_at": datetime.utcnow().isoformat(),
    }

    # Persist the access token so it auto-restores until the daily Upstox expiry.
    _persist_access_token("upstox", token.access_token)
    logger.info(f"Upstox connected — token persisted for auto-restore (ends …{token.access_token[-8:]})")
    await _sync_market_data_feed()

    return {"status": "connected", "broker": "upstox",
            "user_id": profile.user_id, "name": profile.name}


@router.get("/upstox/callback")
async def upstox_callback(code: str):
    from brokers.upstox import UpstoxAdapter
    from brokers.base import UserProfile
    from fastapi.responses import HTMLResponse
    adapter = UpstoxAdapter()
    token = await adapter.authenticate({"code": code})
    try:
        profile = await adapter.get_profile()
    except Exception as e:
        logger.warning(f"Upstox callback profile fetch failed: {e}")
        profile = UserProfile(user_id="upstox_user", name="Upstox", email="", mobile="", broker="upstox")
    _active_brokers["upstox"] = {
        "adapter": adapter, "token": token, "profile": profile,
        "connected_at": datetime.utcnow().isoformat(),
    }
    _persist_access_token("upstox", token.access_token)
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
    if broker:
        info = _active_brokers.get(broker)
        return info["adapter"] if info else None
    for info in _active_brokers.values():
        return info["adapter"]
    return None


def get_broker_token(broker: str) -> Optional[str]:
    """Return the access token string for an active broker session or saved token."""
    info = _active_brokers.get(broker)
    if not info:
        saved = str(_broker_credentials.get(broker, {}).get("access_token", "")).strip()
        return saved or None
    token = info.get("token")
    if token is None:
        saved = str(_broker_credentials.get(broker, {}).get("access_token", "")).strip()
        return saved or None
    return getattr(token, "access_token", None) or str(
        _broker_credentials.get(broker, {}).get("access_token", "")
    ).strip() or None


def get_connected_brokers() -> list[str]:
    """Return list of currently connected broker names."""
    return list(_active_brokers.keys())


async def _sync_market_data_feed() -> None:
    """Keep the shared index tick feed aligned with the current active broker."""
    from market_data import data_router as market_data_router
    from market_data.symbols import LIVE_INDEX_APP_SYMBOLS

    adapter = get_active_adapter()
    if adapter:
        market_data_router.set_broker(adapter)
        await market_data_router.subscribe(list(LIVE_INDEX_APP_SYMBOLS))
    else:
        await market_data_router.unsubscribe()
        asyncio.create_task(
            market_data_router.start_mock_feed(list(LIVE_INDEX_APP_SYMBOLS), interval_secs=1.0)
        )


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
        if not await ensure_upstox_session(force_validate=True):
            logger.warning("Upstox auto-restore failed — manual connect required")
    if str(_broker_credentials.get("fyers", {}).get("access_token", "")).strip():
        logger.info("Auto-restoring Fyers session from saved access token…")
        if not await ensure_fyers_session():
            logger.warning("Fyers auto-restore failed — manual connect required")
