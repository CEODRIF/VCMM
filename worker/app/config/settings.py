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

    Phase 3 adds Supabase connection, queue/lease, concurrency and media
    validation settings. Secrets are environment-only and are never logged.
    """

    model_config = SettingsConfigDict(env_prefix="WORKER_", env_file=".env", extra="ignore")

    environment: Environment = "development"
    log_level: LogLevel = "INFO"
    service_name: str = "clipforge-worker"
    version: str = "0.1.0"
    storage_root: Path = Field(default=Path("storage"))
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=300)

    # --- Phase 3: queue / ingestion -----------------------------------------
    #: Run the ingestion loop alongside the API (disable for API-only use).
    worker_enabled: bool = True
    #: Poll interval between claim attempts when the queue is idle (seconds).
    #: Env: WORKER_POLL_INTERVAL.
    poll_interval: float = Field(default=2.0, gt=0, le=60)
    #: How many jobs may be processed concurrently.
    concurrency: int = Field(default=2, ge=1, le=16)
    #: Lease duration claimed per job (seconds). Must be >= 30 (RPC rule).
    job_lease_seconds: int = Field(default=300, ge=30, le=3600)
    #: Reference retry budget; the authoritative per-job limit is the
    #: ``jobs.max_attempts`` column (DB default 3).
    max_attempts: int = Field(default=3, ge=1, le=10)
    #: Supabase project (server-side credentials — never exposed to clients).
    supabase_url: str | None = None
    supabase_service_role_key: str | None = None
    #: Media validation limits (mirrors the frontend MAX_* variables).
    max_upload_size_bytes: int = Field(default=2 * 1024 * 1024 * 1024, gt=0)
    max_source_duration_seconds: float = Field(default=8 * 3600, gt=0)
    #: Binary locations (overridable for CI/static builds).
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    ffprobe_timeout_seconds: float = Field(default=60.0, gt=0, le=600)

    @property
    def upload_dir(self) -> Path:
        return self.storage_root / "uploads"

    @property
    def output_dir(self) -> Path:
        return self.storage_root / "outputs"

    @property
    def tmp_dir(self) -> Path:
        return self.storage_root / "tmp"

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_role_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor (call :func:`reset_settings_cache` in tests)."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache (test helper)."""
    get_settings.cache_clear()
