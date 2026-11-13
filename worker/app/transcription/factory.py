"""Transcription provider factory (Phase 4).

Configuration is explicit and default-safe:

- ``WORKER_WHISPER_MODEL`` empty  → :class:`UnavailableTranscriber` (the
  worker fails analysis jobs with a clear, permanent
  ``transcription_unavailable`` error — it never fabricates output).
- ``WORKER_WHISPER_MODEL`` set    → :class:`LocalWhisperTranscriber` when the
  optional ``faster-whisper`` dependency imports; if the dependency is
  missing the factory falls back to the unavailable provider and logs why.

No provider here requires cloud credentials or makes paid API calls.
"""

from __future__ import annotations

import logging

from ..config.settings import Settings
from .base import BaseTranscriber, TranscriptionError, TranscriptionResult, TranscriptionUnavailableError

logger = logging.getLogger("clipforge.transcription")


class UnavailableTranscriber(BaseTranscriber):
    """Honest stand-in for a deployment with no transcription engine."""

    name = "unavailable"

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def transcribe(self, audio_path: str, language: str | None = None) -> TranscriptionResult:
        raise TranscriptionUnavailableError(
            "No transcription engine is configured on this deployment. "
            "Install the optional worker dependencies (worker/requirements-optional.txt) "
            f"and set WORKER_WHISPER_MODEL. Details: {self._reason}"
        )


def build_transcription_provider(settings: Settings) -> BaseTranscriber:
    if not settings.whisper_model:
        logger.info("transcription disabled (WORKER_WHISPER_MODEL not set)")
        return UnavailableTranscriber("WORKER_WHISPER_MODEL is not set")

    try:
        from .local_whisper import LocalWhisperTranscriber
    except ImportError:  # pragma: no cover — exercised when optional deps absent
        logger.warning("WORKER_WHISPER_MODEL=%s set but faster-whisper is not installed", settings.whisper_model)
        return UnavailableTranscriber("faster-whisper package not installed in this worker")

    try:
        provider: BaseTranscriber = LocalWhisperTranscriber(
            model_name=settings.whisper_model,
            language=settings.transcription_language,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
        )
    except ValueError as exc:
        logger.warning("invalid whisper configuration (%s); transcription disabled", exc)
        return UnavailableTranscriber(str(exc))
    return provider
