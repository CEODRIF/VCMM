"""Queue data models.

``JobStatus`` mirrors the frontend processing state machine exactly
(``src/config/app.ts`` -> ``processingStates``) and the PostgreSQL status
constraint (migration 20261115000000).
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    UPLOADING = "uploading"
    QUEUED = "queued"
    VALIDATING = "validating"
    READY = "ready"
    TRANSCRIBING = "transcribing"
    ANALYZING = "analyzing"
    RENDERING = "rendering"
    COMPLETED = "completed"
    FAILED = "failed"


class JobQueueEntry(BaseModel):
    """Shape of a job as it travels through the queue (Phase 3)."""

    job_id: str
    status: JobStatus = JobStatus.QUEUED
    progress: int = Field(default=0, ge=0, le=100)
    error_message: Optional[str] = None
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    locked_by: Optional[str] = None
    lease_expires_at: Optional[str] = None
    source_type: Optional[str] = None
    source_path: Optional[str] = None


# Error categories recorded on failed jobs (kept safe — no stack traces).
# Phase 3 (ingestion):
ERROR_INVALID_FILE = "invalid_file"
ERROR_CORRUPT_MEDIA = "corrupt_media"
ERROR_UNSUPPORTED_FORMAT = "unsupported_format"
ERROR_NO_VIDEO_STREAM = "no_video_stream"
ERROR_SOURCE_TOO_LARGE = "source_too_large"
ERROR_SOURCE_TOO_LONG = "source_too_long"
ERROR_STORAGE_DOWNLOAD = "storage_download"
ERROR_FFPROBE = "ffprobe_failed"
# Phase 4 (analysis):
ERROR_NO_AUDIO = "no_audio"
ERROR_AUDIO_EXTRACTION = "audio_extraction_failed"
ERROR_TRANSCRIPTION = "transcription_failed"
ERROR_TRANSCRIPTION_UNAVAILABLE = "transcription_unavailable"
ERROR_NO_SPEECH = "no_speech"
ERROR_NO_CLIPS = "no_clips_detected"
ERROR_CLIPS_PERSIST = "clips_persistence_failed"
ERROR_UNKNOWN = "unknown"

#: Categories that must never be retried (the input cannot change, or the
#: deployment is missing a capability that retries will not fix).
PERMANENT_ERROR_CATEGORIES = frozenset(
    {
        ERROR_INVALID_FILE,
        ERROR_CORRUPT_MEDIA,
        ERROR_UNSUPPORTED_FORMAT,
        ERROR_NO_VIDEO_STREAM,
        ERROR_SOURCE_TOO_LARGE,
        ERROR_SOURCE_TOO_LONG,
        ERROR_NO_AUDIO,
        ERROR_TRANSCRIPTION_UNAVAILABLE,
        ERROR_NO_SPEECH,
        ERROR_NO_CLIPS,
    }
)
