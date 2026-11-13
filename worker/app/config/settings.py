"""Worker environment configuration.

All settings are read from environment variables with the ``WORKER_`` prefix
(e.g. ``WORKER_LOG_LEVEL``). Pydantic validates the values at startup, so
misconfiguration fails fast instead of surfacing mid-job.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
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

    # --- Phase 4: transcription / analysis ------------------------------------
    #: Local Whisper model (tiny, base, small, medium, large-v3, ...).
    #: EMPTY = transcription disabled: analysis jobs fail honestly with
    #: ``transcription_unavailable`` (never fabricated output).
    whisper_model: str = ""
    #: Force a transcription language (e.g. "en", "fr"); empty = auto-detect.
    transcription_language: str | None = None
    #: Long-recording chunking: chunk length + overlap between consecutive
    #: chunks (the overlap is what keeps sentences whole at boundaries).
    transcription_chunk_seconds: int = Field(default=300, ge=30, le=1800)
    transcription_overlap_seconds: int = Field(default=15, ge=0, le=120)
    #: Whisper runtime (local inference only — no cloud, no credentials).
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    #: Audio extraction format / limits.
    audio_sample_rate: int = Field(default=16000, ge=8000, le=48000)
    audio_extract_timeout_seconds: float = Field(default=600.0, gt=0, le=3600)

    @field_validator("transcription_language")
    @classmethod
    def _empty_language_to_none(cls, value: str | None) -> str | None:
        """An empty env var means 'auto-detect', not the literal ''."""
        return value or None

    @model_validator(mode="after")
    def _chunking_sane(self) -> "Settings":
        """The overlap must be strictly smaller than the chunk (fail fast)."""
        if self.transcription_overlap_seconds >= self.transcription_chunk_seconds:
            raise ValueError(
                "WORKER_TRANSCRIPTION_OVERLAP_SECONDS must be smaller than WORKER_TRANSCRIPTION_CHUNK_SECONDS"
            )
        return self

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
