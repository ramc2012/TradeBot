from __future__ import annotations
from functools import lru_cache
from pathlib import Path
from typing import List
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_BACKEND_DIR = Path(__file__).resolve().parents[1]
_PROJECT_ROOT = _BACKEND_DIR.parent
FYERS_FIXED_REDIRECT_URI = "https://trade.fyers.in/api-login/redirect-uri/index.html"


def normalize_fyers_redirect_uri(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return FYERS_FIXED_REDIRECT_URI
    if raw.endswith("/api/auth/fyers/callback"):
        return FYERS_FIXED_REDIRECT_URI
    return raw


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(
            str(_PROJECT_ROOT / ".env"),
            str(_BACKEND_DIR / ".env"),
        ),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"
    BACKEND_CORS_ORIGINS: List[str] = ["http://localhost:3000"]
    BACKEND_CORS_ORIGIN_REGEX: str | None = None

    # DB / Redis
    DATABASE_URL: str = "postgresql+asyncpg://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
    DATABASE_POOL_SIZE: int = 0
    DATABASE_MAX_OVERFLOW: int = 0
    DATABASE_POOL_TIMEOUT_SECONDS: int = 15
    DATABASE_POOL_RECYCLE_SECONDS: int = 900
    REDIS_URL: str = "redis://localhost:6383/0"
    RESEARCH_SYNC_AUTO_ENABLED: bool = False
    STRATEGY_SPOT_SYNC_ENABLED: bool = False
    NSE_STRATEGY_BYPASS_MARKET_PROFILE_GATE: bool = False
    PAPER_TRADING_ONLY: bool = False
    PAPER_RUNTIME_PREWARM_ENABLED: bool = True

    # Security
    SECRET_KEY: str = "change-me-to-a-random-secret-key"

    # Fyers
    FYERS_APP_ID: str = ""
    FYERS_SECRET: str = ""
    FYERS_REDIRECT_URI: str = FYERS_FIXED_REDIRECT_URI

    # Upstox
    UPSTOX_API_KEY: str = ""
    UPSTOX_SECRET: str = ""
    UPSTOX_REDIRECT_URI: str = "https://www.google.com"

    # 5Paisa
    FIVEPAISA_APP_NAME: str = ""
    FIVEPAISA_APP_SOURCE: str = ""
    FIVEPAISA_USER_ID: str = ""      # client code (e.g. NL0BYabni01)
    FIVEPAISA_EMAIL: str = ""        # registered email address (for TOTP login)
    FIVEPAISA_PASSWORD: str = ""     # account password / TPIN
    FIVEPAISA_USER_KEY: str = ""
    FIVEPAISA_ENCRYPTION_KEY: str = ""

    # ICICI Direct Breeze
    ICICI_BREEZE_API_KEY: str = ""
    ICICI_BREEZE_SECRET: str = ""

    # Anthropic
    ANTHROPIC_API_KEY: str = ""

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    TELEGRAM_REPORTS_ENABLED: bool = False
    TELEGRAM_REPORT_INTERVAL: str = "1h"

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors(cls, v):
        if isinstance(v, str):
            import json
            return json.loads(v)
        return v

    @field_validator("BACKEND_CORS_ORIGIN_REGEX", mode="before")
    @classmethod
    def parse_optional_regex(cls, v):
        if v is None:
            return None
        value = str(v).strip()
        return value or None

    @field_validator("FYERS_REDIRECT_URI", mode="before")
    @classmethod
    def normalize_fyers_redirect(cls, v):
        return normalize_fyers_redirect_uri(v)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
