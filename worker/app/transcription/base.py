"""Transcription abstraction.

Phase 1 defined the contract; Phase 4 implements it for real:

- :class:`BaseTranscriber` — the provider interface.
- :class:`TranscriptionWord` / :class:`TranscriptionSegment` /
  :class:`TranscriptionResult` — the data contract (word-level timestamps,
  language, segment metadata).
- :class:`TranscriptionError` / :class:`TranscriptionUnavailableError` —
  honest failure types. A provider that is not configured MUST raise
  ``TranscriptionUnavailableError`` — it must NEVER return fabricated text.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class TranscriptionError(RuntimeError):
    """A transcription attempt failed (transport, model, decode)."""


class TranscriptionUnavailableError(TranscriptionError):
    """No transcription engine is configured/available on this deployment.

    This is a PERMANENT failure for the job: retrying will not make an
    unconfigured engine appear.
    """


@dataclass(frozen=True)
class TranscriptionWord:
    word: str
    start: float  # seconds (absolute within the audio)
    end: float  # seconds


@dataclass(frozen=True)
class TranscriptionSegment:
    """One timed chunk of spoken text (Whisper segment granularity).

    ``words`` carries the word-level timestamps when the provider supplies
    them (required for subtitle-quality cuts); it is empty only for
    providers that cannot produce them.
    """

    text: str
    start: float  # seconds
    end: float  # seconds
    words: tuple[TranscriptionWord, ...] = field(default_factory=tuple)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class TranscriptionResult:
    language: str
    language_probability: float | None
    duration_seconds: float
    segments: tuple[TranscriptionSegment, ...]
    full_text: str

    @property
    def word_count(self) -> int:
        return sum(len(s.words) for s in self.segments)


class BaseTranscriber(ABC):
    """Interface for audio transcription backends (Phase 4)."""

    name: str = "base"

    @abstractmethod
    def transcribe(self, audio_path: str, language: str | None = None) -> TranscriptionResult:
        """Return the timed transcription of ``audio_path``.

        Must raise :class:`TranscriptionError` (or a subclass) on failure —
        never return invented text.
        """
        raise NotImplementedError

    # Kept from the Phase 1 contract for backward compatibility.
    def transcribe_legacy(self, audio_path: str) -> list[TranscriptionSegment]:
        return list(self.transcribe(audio_path).segments)
