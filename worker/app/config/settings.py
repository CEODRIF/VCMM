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
    #: NOTE (TASK 007): there is intentionally NO source-duration cap —
    #: recordings are analyzed incrementally (see the intel settings below).
    #: The size limit stays: it bounds storage and temp disk, not duration.
    max_upload_size_bytes: int = Field(default=2 * 1024 * 1024 * 1024, gt=0)
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

    # --- Phase 5: rendering ---------------------------------------------------
    #: How many clips of one job may be rendered concurrently (each clip is
    #: one FFmpeg subprocess; this bounds CPU/memory per worker).
    render_concurrency: int = Field(default=2, ge=1, le=8)
    #: Wall-clock budget for ONE clip render (the subprocess is killed after).
    render_timeout_seconds: float = Field(default=900.0, gt=0, le=7200)
    #: A rendered clip larger than this is a contract violation and is
    #: rejected (not uploaded).
    max_clip_size_bytes: int = Field(default=200 * 1024 * 1024, gt=0)
    #: Source-preserved frame rate (TASK 007, Priority 4): the clip is
    #: rendered at the MEASURED source rate (fractional NTSC rates included,
    #: e.g. 23.976 / 29.97 / 59.94), capped at ``render_fps_high`` — the
    #: output never exceeds the measured rate, so frames are never duplicated
    #: to fake a higher rate. ``render_fps_low`` is the fallback used only
    #: when the source rate cannot be measured.
    render_fps_low: int = Field(default=30, ge=12, le=60)
    render_fps_high: int = Field(default=60, ge=30, le=120)
    #: Retained for settings compatibility with the earlier two-tier design;
    #: the source-preserved selection no longer uses it.
    render_fps_high_source_threshold: float = Field(default=55.0, gt=0, le=120)

    # --- Phase 5B: caption style (burned-in animated subtitles) ----------------
    #: Font family passed to libass (resolved by fontconfig; deterministic
    #: fallback = the system default). No ASS markup characters allowed.
    caption_font_family: str = "DejaVu Sans"
    #: Glyph size in PlayRes pixels (output is 1080x1920).
    caption_font_size: int = Field(default=88, ge=24, le=200)
    #: RRGGBB colors: base = a word as it is spoken (ASS PrimaryColour);
    #: highlight = upcoming words (ASS SecondaryColour); outline around glyphs.
    caption_base_color: str = "FFFFFF"
    caption_highlight_color: str = "00D7FF"
    caption_outline_color: str = "000000"
    #: Outline thickness in PlayRes px (readability on any background).
    caption_outline_width: int = Field(default=6, ge=0, le=24)
    #: Distance of the caption block from the bottom edge (safe area).
    caption_margin_v: int = Field(default=520, ge=60, le=1500)
    #: Consecutive words per on-screen line.
    caption_words_per_line: int = Field(default=3, ge=1, le=8)
    #: Uppercase Latin words (Arabic/CJK are never transformed).
    caption_uppercase: bool = True

    # --- Phase 7: stream intelligence (bounded, resumable long-form analysis) --
    # These are OPERATIONAL limits (per-chunk decode size, per-claim budget),
    # NOT a maximum recording duration: the chunk planner has no upper bound,
    # a 100-hour VOD simply yields more chunks processed across more claims.
    #: Audio/visual chunk length + overlap (the overlap keeps boundary events
    #: whole: both neighbours see them, the midpoint owner persists them).
    intel_chunk_seconds: int = Field(default=600, ge=60, le=3600)
    intel_overlap_seconds: int = Field(default=30, ge=0, le=300)
    #: How many chunks ONE worker claim processes before returning
    #: ``incomplete`` (resume on the next claim). Bounds wall-clock per claim.
    intel_chunks_per_claim: int = Field(default=20, ge=1, le=1000)
    #: ffmpeg scene-score threshold for a "measured cut".
    intel_scene_threshold: float = Field(default=0.3, gt=0.05, le=0.95)
    #: Per-chunk decode / visual-pass wall-clock budgets (subprocess killed).
    intel_chunk_timeout_seconds: float = Field(default=300.0, gt=0, le=3600)
    #: How many candidate clips a completed run ranks out (the job can still
    #: request more; this is the analysis-side default).
    intel_requested_clips: int = Field(default=5, ge=1, le=25)
    #: SAFETY CAP on incomplete-analysis resumes (Phase 7b). A resume is
    #: progress, not a failure, so it is NOT bounded by the failure
    #: ``max_attempts`` budget — it has its own cap. The value is generous
    #: enough that NO legitimate recording (even at ``intel_chunks_per_claim``
    #: = 1) can hit it, but a genuinely stuck pipeline eventually fails the
    #: job instead of resuming forever. Env: WORKER_INTEL_MAX_RESUMES.
    intel_max_resumes: int = Field(default=100000, ge=1)

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
        if self.intel_overlap_seconds >= self.intel_chunk_seconds:
            raise ValueError(
                "WORKER_INTEL_OVERLAP_SECONDS must be smaller than WORKER_INTEL_CHUNK_SECONDS"
            )
        if self.render_fps_low > self.render_fps_high:
            raise ValueError("WORKER_RENDER_FPS_LOW must be <= WORKER_RENDER_FPS_HIGH")
        return self

    @field_validator("caption_base_color", "caption_highlight_color", "caption_outline_color")
    @classmethod
    def _caption_color_sane(cls, value: str) -> str:
        """RRGGBB hex only — libass ASS colors are built from these."""
        v = value.strip().upper()
        if len(v) == 6 and all(c in "0123456789ABCDEF" for c in v):
            return v
        raise ValueError("caption colors must be 6-digit RRGGBB hex, e.g. FFD700")

    @field_validator("caption_font_family")
    @classmethod
    def _caption_font_sane(cls, value: str) -> str:
        """Reject ASS markup / filtergraph characters in the font name."""
        v = value.strip()
        if not v or len(v) > 64:
            raise ValueError("caption font family must be 1-64 characters")
        if any(c in v for c in '{}\\"\'\\;,=[]'):
            raise ValueError("caption font family must not contain markup characters")
        return v

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
