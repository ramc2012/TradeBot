"""Read live broker credentials from the main app's persisted store.

The nomad-curie backend stores broker credentials in the `app_runtime_state`
table under `state_key='broker_credentials'`. The payload is JSONB shaped like:

    {
      "_format": "fernet-v1",
      "data": {
        "fyers":  {"app_id": "fernet::...", "secret": "fernet::...",
                   "access_token": "fernet::...", "refresh_token": "fernet::...",
                   "redirect_uri": "fernet::...", "token_saved_at": "ISO"},
        "upstox": {...},
        ...
      }
    }

Sensitive fields are prefixed `fernet::` and encrypted with
`Fernet(sha256(SECRET_KEY).digest())` (same scheme as backend/core/security.py).

This module mirrors that scheme. The main app remains the single source of
truth for OAuth flow and daily token refresh — we just read.
"""
from __future__ import annotations

import base64
import hashlib
import os
import time
from dataclasses import dataclass
from typing import Any

import asyncpg
from cryptography.fernet import Fernet, InvalidToken

from sniper_paper.common.logging import get_logger

log = get_logger(__name__)

ENCRYPTED_VALUE_PREFIX = "fernet::"
CREDENTIAL_STORE_DB_KEY = "broker_credentials"
DEFAULT_REFRESH_TTL_SEC = 15.0


@dataclass
class FyersCreds:
    app_id: str
    secret: str
    access_token: str
    refresh_token: str | None
    redirect_uri: str | None
    token_saved_at: str | None

    def is_usable(self) -> bool:
        return bool(self.app_id and self.access_token)


def _derive_fernet(secret_key: str) -> Fernet:
    """Reproduce backend/core/security.py:_derive_key()."""
    digest = hashlib.sha256(secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _decrypt_value(value: Any, fernet: Fernet) -> str:
    text = str(value or "").strip()
    if not text.startswith(ENCRYPTED_VALUE_PREFIX):
        return text  # plain (rare; usually only token_saved_at)
    try:
        return fernet.decrypt(text[len(ENCRYPTED_VALUE_PREFIX):].encode()).decode()
    except InvalidToken as e:
        raise ValueError(
            "SECRET_KEY mismatch — cannot decrypt main-app credentials. "
            "Ensure the SECRET_KEY env var on sniper-paper matches nomadcurie_backend."
        ) from e


class BrokerCredsStore:
    """Caches main-app credentials, refreshes from DB on TTL miss."""

    def __init__(self, pool: asyncpg.Pool, secret_key: str | None = None,
                 refresh_ttl_sec: float = DEFAULT_REFRESH_TTL_SEC):
        self.pool = pool
        secret_key = secret_key or os.environ.get("SECRET_KEY", "")
        if not secret_key:
            raise RuntimeError(
                "SECRET_KEY env var is required to decrypt main-app broker credentials"
            )
        self._fernet = _derive_fernet(secret_key)
        self._refresh_ttl = refresh_ttl_sec
        self._cache: dict[str, dict[str, Any]] = {}
        self._cache_loaded_at: float = 0.0

    async def _refresh(self) -> None:
        row = await self.pool.fetchrow(
            "SELECT payload, updated_at FROM app_runtime_state WHERE state_key = $1",
            CREDENTIAL_STORE_DB_KEY,
        )
        if row is None:
            log.warning("broker_credentials row not found in app_runtime_state")
            self._cache = {}
            self._cache_loaded_at = time.monotonic()
            return
        payload = row["payload"] or {}
        # asyncpg returns a python dict already if jsonb. Defensive parse.
        if isinstance(payload, str):
            import json
            payload = json.loads(payload)
        data = payload.get("data", payload)  # tolerate both shapes
        self._cache = data
        self._cache_loaded_at = time.monotonic()
        log.info("Refreshed broker credentials cache (brokers: %s)", list(data.keys()))

    async def get_raw(self, broker: str) -> dict[str, Any]:
        if (time.monotonic() - self._cache_loaded_at) > self._refresh_ttl:
            await self._refresh()
        return self._cache.get(broker, {}) or {}

    async def get_fyers(self) -> FyersCreds | None:
        raw = await self.get_raw("fyers")
        if not raw:
            return None
        try:
            return FyersCreds(
                app_id=_decrypt_value(raw.get("app_id"), self._fernet),
                secret=_decrypt_value(raw.get("secret"), self._fernet),
                access_token=_decrypt_value(raw.get("access_token"), self._fernet),
                refresh_token=_decrypt_value(raw.get("refresh_token"), self._fernet) or None,
                redirect_uri=_decrypt_value(raw.get("redirect_uri"), self._fernet) or None,
                token_saved_at=raw.get("token_saved_at"),
            )
        except ValueError as e:
            log.error("Fyers cred decryption failed: %s", e)
            return None
