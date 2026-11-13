"""Transcription abstraction.

Phase 1 defines the contract only — no model is downloaded or installed.
The Whisper-based implementation (word-level timings for subtitles) lands
in Phase 4.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class TranscriptionSegment:
    text: str
    start: float  # seconds
    end: float  # seconds


class BaseTranscriber(ABC):
    """Interface for audio transcription backends."""

    @abstractmethod
    def transcribe(self, audio_path: str) -> list[TranscriptionSegment]:
        """Return timed transcription segments for an audio file."""
        raise NotImplementedError
