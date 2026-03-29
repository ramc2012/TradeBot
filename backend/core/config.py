from __future__ import annotations
from functools import lru_cache
from pathlib import Path
from typing import List
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_BACKEND_DIR = Path(__file__).resolve().parents[1]
_PROJECT_ROOT = _BACKEND_DIR.parent


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

    # DB / Redis
    DATABASE_URL: str = "postgresql+asyncpg://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
    REDIS_URL: str = "redis://localhost:6383/0"

    # Security
    SECRET_KEY: str = "change-me-to-a-random-secret-key"

    # Fyers
    FYERS_APP_ID: str = ""
    FYERS_SECRET: str = ""
    FYERS_REDIRECT_URI: str = "http://localhost:8000/api/auth/fyers/callback"

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

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors(cls, v):
        if isinstance(v, str):
            import json
            return json.loads(v)
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
