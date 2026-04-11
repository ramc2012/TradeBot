"""Token encryption/decryption at rest using Fernet symmetric encryption."""
from __future__ import annotations
import base64
import json
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from cryptography.fernet import Fernet
from core.config import settings


def _derive_key() -> bytes:
    """Derive a 32-byte Fernet key from SECRET_KEY."""
    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return base64.urlsafe_b64encode(digest)


_fernet = Fernet(_derive_key())


def encrypt_token(token: str) -> str:
    return _fernet.encrypt(token.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    return _fernet.decrypt(encrypted.encode()).decode()


def issue_ephemeral_token(
    *,
    scope: str,
    subject: str,
    ttl_seconds: int = 300,
    extra: Optional[dict[str, Any]] = None,
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "scope": scope,
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=max(ttl_seconds, 1))).timestamp()),
    }
    if extra:
        payload.update(extra)
    return encrypt_token(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def verify_ephemeral_token(token: str, *, expected_scope: Optional[str] = None) -> dict[str, Any]:
    payload = json.loads(decrypt_token(token))
    if not isinstance(payload, dict):
        raise ValueError("invalid token payload")
    exp = int(payload.get("exp") or 0)
    if datetime.now(timezone.utc).timestamp() >= exp:
        raise ValueError("token expired")
    scope = str(payload.get("scope") or "")
    if expected_scope and scope != expected_scope:
        raise ValueError("token scope mismatch")
    return payload
