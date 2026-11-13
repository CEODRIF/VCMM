"""AI clip detection contract.

Placeholder only. The scoring/selection implementation (built on top of the
Phase 4 transcription) arrives in Phase 4. No model is downloaded here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..transcription.base import TranscriptionSegment


@dataclass(frozen=True)
class ClipCandidate:
    start: float  # seconds into the source video
    end: float  # seconds into the source video
    score: float
    title: str


class BaseClipDetector(ABC):
    """Interface for selecting the strongest short-form moments."""

    @abstractmethod
    def detect(
        self,
        segments: "list[TranscriptionSegment]",
        clip_count: int,
        duration_seconds: int,
    ) -> list[ClipCandidate]:
        """Select ``clip_count`` moments, each ~``duration_seconds`` long."""
        raise NotImplementedError
