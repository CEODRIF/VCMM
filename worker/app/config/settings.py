"""Worker environment configuration.

All settings are read from environment variables with the ``WORKER_`` prefix
(e.g. ``WORKER_LOG_LEVEL``). Pydantic validates the values at startup, so
misconfiguration fails fast instead of surfacing mid-job.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "staging", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Runtime configuration for the ClipForge worker.

    Phase 1 exposes only what the health endpoint and logging need. Queue,
    source-provider and AI provider settings arrive in Phases 3-4.
    """

    model_config = SettingsConfigDict(env_prefix="WORKER_", env_file=".env", extra="ignore")

    environment: Environment = "development"
    log_level: LogLevel = "INFO"
    service_name: str = "clipforge-worker"
    version: str = "0.1.0"
    storage_root: Path = Field(default=Path("storage"))
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=300)

    @property
    def upload_dir(self) -> Path:
        return self.storage_root / "uploads"

    @property
    def output_dir(self) -> Path:
        return self.storage_root / "outputs"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor (call :func:`reset_settings_cache` in tests)."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache (test helper)."""
    get_settings.cache_clear()
