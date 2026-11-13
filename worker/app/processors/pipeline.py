"""Processing pipeline stage definitions.

Phase 1 only declares the stage order. The actual stage handlers
(source fetch, transcription, analysis, rendering) are implemented in
Phases 3-5; ``handler`` stays ``None`` until then.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from ..queue.models import JobQueueEntry, JobStatus


@dataclass(frozen=True)
class PipelineStage:
    name: str
    status: JobStatus
    handler: Optional[Callable[[JobQueueEntry], None]] = None


PIPELINE_STAGES: tuple[PipelineStage, ...] = (
    PipelineStage(name="source", status=JobStatus.UPLOADING),
    PipelineStage(name="transcription", status=JobStatus.TRANSCRIBING),
    PipelineStage(name="analysis", status=JobStatus.ANALYZING),
    PipelineStage(name="rendering", status=JobStatus.RENDERING),
)


def stage_order() -> list[str]:
    """Names of the pipeline stages in execution order."""
    return [stage.name for stage in PIPELINE_STAGES]
