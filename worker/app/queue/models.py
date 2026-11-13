"""Queue data models.

``JobStatus`` mirrors the frontend processing state machine exactly
(``src/config/app.ts`` -> ``processingStates``).
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    QUEUED = "queued"
    UPLOADING = "uploading"
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
