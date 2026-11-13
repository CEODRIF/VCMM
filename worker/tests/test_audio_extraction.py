"""Audio extraction tests — real FFmpeg, synthetic media from conftest."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from app.processors.audio import (
    AUDIO_SAMPLE_RATE,
    AudioExtractionError,
    extract_audio,
    probe_wav_duration,
)
from app.queue.models import ERROR_AUDIO_EXTRACTION, ERROR_INVALID_FILE


def run(coro):
    return asyncio.run(coro)


def _wav_stream_info(ffprobe: str, wav: Path) -> dict:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a:0",
         "-show_streams", "-print_format", "json", str(wav)],
        capture_output=True,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout.decode("utf-8", "replace"))
    return (payload.get("streams") or [{}])[0]


def test_extraction_produces_mono_16k_pcm_wav(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    source = synthetic_videos["valid"]  # 2s, video + AAC audio
    destination = tmp_path / "audio.wav"

    run(extract_audio(ffmpeg_bin, source, destination, timeout_seconds=30))

    assert destination.is_file()
    assert destination.stat().st_size > 0
    stream = _wav_stream_info(ffprobe_bin, destination)
    assert stream.get("codec_name") == "pcm_s16le"
    assert stream.get("sample_rate") == str(AUDIO_SAMPLE_RATE)
    assert stream.get("channels") == 1
    # Duration matches the source (±15%).
    duration = run(probe_wav_duration(ffprobe_bin, destination, 30))
    assert 1.7 <= duration <= 2.3


def test_extraction_failure_when_no_audio_stream(ffmpeg_bin, synthetic_videos, tmp_path):
    source = synthetic_videos["video_only"]  # video without any audio track
    destination = tmp_path / "audio.wav"

    with pytest.raises(AudioExtractionError) as exc:
        run(extract_audio(ffmpeg_bin, source, destination, timeout_seconds=30))

    assert exc.value.category == ERROR_AUDIO_EXTRACTION
    assert "corrupted" in exc.value.message.lower() or "could not" in exc.value.message.lower()
    # No partial output left behind.
    assert not destination.exists()


def test_extraction_failure_on_corrupt_media(ffmpeg_bin, synthetic_videos, tmp_path):
    source = synthetic_videos["corrupt"]
    destination = tmp_path / "audio.wav"

    with pytest.raises(AudioExtractionError) as exc:
        run(extract_audio(ffmpeg_bin, source, destination, timeout_seconds=30))

    assert exc.value.category == ERROR_AUDIO_EXTRACTION
    assert not destination.exists()


def test_extraction_rejects_missing_source(ffmpeg_bin, tmp_path):
    with pytest.raises(AudioExtractionError) as exc:
        run(extract_audio(ffmpeg_bin, tmp_path / "nope.mp4", tmp_path / "audio.wav", timeout_seconds=30))
    assert exc.value.category == ERROR_INVALID_FILE


def test_extraction_rejects_empty_source(ffmpeg_bin, synthetic_videos, tmp_path):
    source = synthetic_videos["empty"]
    with pytest.raises(AudioExtractionError) as exc:
        run(extract_audio(ffmpeg_bin, source, tmp_path / "audio.wav", timeout_seconds=30))
    assert exc.value.category == ERROR_INVALID_FILE


def test_extraction_timeout_kills_process_and_cleans_up(ffmpeg_bin, synthetic_videos, tmp_path):
    source = synthetic_videos["valid"]
    destination = tmp_path / "audio.wav"

    # 100 µs is far below process spawn time: the timeout is guaranteed to
    # fire before any output can be produced.
    with pytest.raises(AudioExtractionError) as exc:
        run(extract_audio(ffmpeg_bin, source, destination, timeout_seconds=0.0001))

    assert exc.value.category == ERROR_AUDIO_EXTRACTION
    assert "timed out" in exc.value.message
    assert not destination.exists()
