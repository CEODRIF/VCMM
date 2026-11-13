"""Transcription provider tests: honest unavailability, factory routing,
and a guarded real-Whisper smoke test (no cloud, no credentials)."""

from __future__ import annotations

import glob
import os
import sys

import pytest

from app.config.settings import Settings
from app.transcription.base import (
    BaseTranscriber,
    TranscriptionError,
    TranscriptionResult,
    TranscriptionSegment,
    TranscriptionUnavailableError,
    TranscriptionWord,
)
from app.transcription.factory import UnavailableTranscriber, build_transcription_provider

SINE_WAV = "sine.wav"


def make_settings(**overrides) -> Settings:
    base = dict(environment="development", log_level="WARNING")
    base.update(overrides)
    return Settings(**base)


# ---- unavailability contract (the honest path) -------------------------------

def test_unconfigured_deployment_yields_unavailable_provider():
    provider = build_transcription_provider(make_settings())  # whisper_model=""
    assert provider.name == "unavailable"
    with pytest.raises(TranscriptionUnavailableError) as exc:
        provider.transcribe("/any/path.wav")
    # Safe message: points at the fix, leaks nothing.
    assert "WORKER_WHISPER_MODEL" in str(exc.value)


def test_unavailable_error_is_permanent_category():
    # The processor maps this to a PERMANENT failure — assert the type
    # hierarchy used for that mapping.
    assert issubclass(TranscriptionUnavailableError, TranscriptionError)


def test_configured_but_missing_dependency_falls_back_to_unavailable(monkeypatch):
    # Simulate 'faster-whisper not installed' by making the import fail.
    monkeypatch.setitem(sys.modules, "app.transcription.local_whisper", None)
    provider = build_transcription_provider(make_settings(whisper_model="tiny"))
    assert isinstance(provider, UnavailableTranscriber)
    with pytest.raises(TranscriptionUnavailableError):
        provider.transcribe("/any/path.wav")


def test_unknown_model_name_is_rejected_as_unavailable():
    provider = build_transcription_provider(make_settings(whisper_model="gigantic-x"))
    assert isinstance(provider, UnavailableTranscriber)
    with pytest.raises(TranscriptionUnavailableError) as exc:
        provider.transcribe("/any/path.wav")
    assert "gigantic-x" in str(exc.value)


def test_valid_model_returns_local_whisper_provider():
    pytest.importorskip("faster_whisper")
    provider = build_transcription_provider(make_settings(whisper_model="tiny.en", transcription_language="en"))
    assert provider.name == "local-whisper"


def test_local_whisper_rejects_missing_file_before_model_load():
    pytest.importorskip("faster_whisper")
    from app.transcription.local_whisper import LocalWhisperTranscriber

    transcriber = LocalWhisperTranscriber("tiny.en")
    with pytest.raises(TranscriptionError) as exc:
        transcriber.transcribe("/nonexistent/audio.wav")
    assert "missing" in str(exc.value)
    # The model must NOT have been loaded/downloaded for a missing file.
    assert transcriber._model is None


def test_base_transcriber_contract_shape():
    result = TranscriptionResult(
        language="en",
        language_probability=0.9,
        duration_seconds=10.0,
        segments=(TranscriptionSegment(text="hi there", start=0.0, end=1.0,
                                       words=(TranscriptionWord("hi", 0.0, 0.5),
                                              TranscriptionWord("there", 0.5, 1.0))),),
        full_text="hi there",
    )
    assert result.word_count == 2
    assert result.segments[0].duration == pytest.approx(1.0)


def test_legacy_shim_returns_segments():
    class _Stub(BaseTranscriber):
        def transcribe(self, audio_path, language=None):
            return TranscriptionResult("en", None, 1.0,
                                       (TranscriptionSegment("x", 0.0, 1.0),), "x")

    assert [s.text for s in _Stub().transcribe_legacy("p")] == ["x"]


# ---- guarded real-engine smoke test ------------------------------------------

def _whisper_tiny_cached() -> bool:
    """True when the tiny model weights are already in the local HF cache."""
    for hub in (
        os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub"),
        os.path.join(os.environ.get("HF_HOME", ""), "hub"),
    ):
        if glob.glob(os.path.join(hub, "models--Systran--faster-whisper-tiny*")):
            return True
    return False


@pytest.mark.skipif(not _whisper_tiny_cached(), reason="whisper tiny weights not cached locally (offline-safe)")
def test_real_whisper_transcribes_tone_honestly(ffmpeg_bin, tmp_path):
    """A pure sine tone has NO speech: the engine must return an empty (but
    valid) result — never fabricated text."""
    pytest.importorskip("faster_whisper")
    import subprocess

    from app.transcription.local_whisper import LocalWhisperTranscriber

    wav = tmp_path / SINE_WAV
    # 3s of 440 Hz tone, mono 16 kHz.
    result = subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")[:300]

    transcriber = LocalWhisperTranscriber("tiny.en", language="en")
    result = transcriber.transcribe(str(wav))
    assert isinstance(result, TranscriptionResult)
    # Honest output: no speech → no segments (VAD removes the tone).
    assert result.segments == ()
    assert result.full_text == ""
    assert result.word_count == 0
