"""Processing pipeline stage definitions.

The stage order is declared here; the actual stage handlers live in the
processors: ingestion (validating, Phase 3), analysis
(transcribing/analyzing, Phase 4), rendering (rendering, Phase 5A cut/
reframe/encode + Phase 5B animated subtitle burn-in). ``handler`` stays
``None``: dispatch is done by the worker, not this table.
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
