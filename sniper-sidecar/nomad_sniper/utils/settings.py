"""Centralized settings loader. Reads `.env` and environment variables.

Phase 0 only needs paths and logging; broker credentials are declared but not used.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Paths
    data_dir: Path = Field(default=Path("./data"))
    artifact_dir: Path = Field(default=Path("./artifacts"))

    # Logging
    log_level: str = "INFO"

    # Broker creds — declared but optional in Phase 0
    fyers_client_id: str | None = None
    fyers_secret_key: str | None = None
    fyers_redirect_uri: str | None = None
    fyers_static_ip: str | None = None

    upstox_api_key: str | None = None
    upstox_api_secret: str | None = None

    zerodha_api_key: str | None = None
    zerodha_api_secret: str | None = None

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def interim_dir(self) -> Path:
        return self.data_dir / "interim"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"


settings = Settings()
