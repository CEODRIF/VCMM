"""Audio transcription (Phase 4): provider base, local Whisper, chunking."""

from .base import (
    BaseTranscriber,
    TranscriptionError,
    TranscriptionResult,
    TranscriptionSegment,
    TranscriptionUnavailableError,
    TranscriptionWord,
)

__all__ = [
    "BaseTranscriber",
    "TranscriptionError",
    "TranscriptionResult",
    "TranscriptionSegment",
    "TranscriptionUnavailableError",
    "TranscriptionWord",
]

