"""Local Whisper transcription (Phase 4) — no cloud, no credentials.

Uses the ``faster-whisper`` package (CTranslate2 runtime) as an OPTIONAL
dependency: it is listed in ``worker/requirements-optional.txt`` and is NOT
installed by the base ``requirements.txt``, so a deployment without it runs
with the honest "transcription unavailable" provider instead of silently
degrading. Model weights are downloaded once from Hugging Face to the local
cache when the engine is first used — that is a plain file download, not a
paid API call.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from .base import (
    BaseTranscriber,
    TranscriptionError,
    TranscriptionResult,
    TranscriptionSegment,
    TranscriptionWord,
)

logger = logging.getLogger("clipforge.transcription")

#: Whisper model sizes accepted (tiny..large-v3; distil-* also work).
_VALID_MODELS = frozenset(
    {
        "tiny", "tiny.en", "base", "base.en", "small", "small.en",
        "medium", "medium.en", "large-v1", "large-v2", "large-v3", "large",
        "distil-large-v2", "distil-large-v3",
    }
)


class LocalWhisperTranscriber(BaseTranscriber):
    """Runs Whisper fully locally on CPU (or CUDA when available)."""

    name = "local-whisper"

    def __init__(
        self,
        model_name: str,
        language: Optional[str] = None,
        device: str = "cpu",
        compute_type: str = "int8",
    ) -> None:
        if model_name not in _VALID_MODELS:
            raise ValueError(f"unknown whisper model: {model_name}")
        self._model_name = model_name
        self._language = language
        self._device = device
        self._compute_type = compute_type
        self._model: Any = None

    def _ensure_model(self) -> Any:
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:  # pragma: no cover — import-guarded
                raise TranscriptionError(
                    "the faster-whisper package is not installed in this worker"
                ) from exc
            try:
                self._model = WhisperModel(
                    self._model_name,
                    device=self._device,
                    compute_type=self._compute_type,
                )
            except Exception as exc:  # noqa: BLE001 — model download/load
                raise TranscriptionError(f"whisper model load failed: {exc.__class__.__name__}") from exc
        return self._model

    def transcribe(self, audio_path: str, language: Optional[str] = None) -> TranscriptionResult:
        if not os.path.isfile(audio_path):
            raise TranscriptionError("audio file is missing")

        model = self._ensure_model()
        try:
            raw_segments, info = model.transcribe(
                audio_path,
                language=language or self._language,
                beam_size=1,  # deterministic, fast, good enough for cut detection
                vad_filter=True,
                word_timestamps=True,
            )
        except Exception as exc:  # noqa: BLE001 — inference boundary
            raise TranscriptionError(f"whisper inference failed: {exc.__class__.__name__}") from exc

        segments: list[TranscriptionSegment] = []
        for raw in raw_segments:
            words = tuple(
                TranscriptionWord(word=w.word.strip(), start=float(w.start), end=float(w.end))
                for w in (raw.words or ())
                if w.word and w.word.strip()
            )
            segments.append(
                TranscriptionSegment(
                    text=raw.text.strip(),
                    start=float(raw.start),
                    end=float(raw.end),
                    words=words,
                )
            )

        language_name = getattr(info, "language", None) or (language or self._language) or "unknown"
        language_prob = getattr(info, "language_probability", None)
        return TranscriptionResult(
            language=str(language_name),
            language_probability=float(language_prob) if language_prob is not None else None,
            duration_seconds=float(getattr(info, "duration", 0.0) or 0.0),
            segments=tuple(segments),
            full_text=" ".join(s.text for s in segments if s.text),
        )
