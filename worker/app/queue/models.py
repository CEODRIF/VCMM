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
# Phase 5 (rendering):
ERROR_RENDER_FAILED = "render_failed"
ERROR_RENDER_INVALID = "render_invalid"
ERROR_CLIP_UPLOAD = "clip_upload_failed"
ERROR_NO_CLIPS_TO_RENDER = "no_clips_to_render"
# Phase 7b (stream-intelligence integration):
# A chunk failed irrecoverably for THIS run (transient: the next claim resumes
# from checkpoints and retries the failed chunk). NEVER a silent incomplete
# success.
ERROR_INTEL_ANALYSIS = "intel_analysis_failed"
# Phase 8 (TASK 011 — source providers):
# The provider is not configured/enabled on this deployment (retries
# cannot configure it).
ERROR_PROVIDER_UNAVAILABLE = "provider_unavailable"
# The URL is malformed or not claimed by any adapter (input cannot change).
ERROR_PROVIDER_INVALID_URL = "provider_invalid_url"
# The deployment's provider credentials failed (expired token, bad
# grant) — the operator must act, so the job must not burn retries.
ERROR_PROVIDER_UNAUTHORIZED = "provider_unauthorized"
# The source exists but is not usable: deleted/expired, private,
# age/region-restricted, live, or a platform policy the deployment
# cannot authorize (input cannot change for this job).
ERROR_SOURCE_REJECTED = "source_rejected"
# Metadata is permitted but NO authorized materialization path exists
# for this provider (spec: expose as unsupported, never work around).
ERROR_MATERIALIZATION_UNSUPPORTED = "materialization_unsupported"
# The provider could not be reached / answered malformed / timed out
# (transient: the next claim may succeed).
ERROR_PROVIDER_METADATA = "provider_metadata_unavailable"
# Provider rate limit — retryable (bounded by the job attempt budget).
ERROR_PROVIDER_RATE_LIMITED = "provider_rate_limited"
# A permitted transfer broke (network/provider fault) — retryable.
ERROR_TRANSFER_INTERRUPTED = "transfer_interrupted"
# Materialized file could not be stored in the private bucket — retryable.
ERROR_SOURCE_UPLOAD = "source_upload_failed"
# The analysis resume budget was exhausted without completion (a genuinely
# stuck run) — permanent.
ERROR_INTEL_RESUME_EXHAUSTED = "intel_resume_exhausted"
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
        ERROR_RENDER_INVALID,
        ERROR_NO_CLIPS_TO_RENDER,
        # TASK 011: provider inputs/authorizations cannot change on retry
        ERROR_PROVIDER_UNAVAILABLE,
        ERROR_PROVIDER_INVALID_URL,
        ERROR_PROVIDER_UNAUTHORIZED,
        ERROR_SOURCE_REJECTED,
        ERROR_MATERIALIZATION_UNSUPPORTED,
    }
)
