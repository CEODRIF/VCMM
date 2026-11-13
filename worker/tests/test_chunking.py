"""Chunking tests: real FFmpeg splitting + provider-agnostic merge logic.

The merge is exercised with a scripted fake provider so the ownership
boundaries (midpoint of the overlap) are asserted exactly, without any
model involved.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from app.transcription.base import (
    BaseTranscriber,
    TranscriptionResult,
    TranscriptionSegment,
    TranscriptionWord,
)
from app.transcription.chunking import (
    AudioChunk,
    merge_chunk_results,
    probe_duration,
    split_audio,
    transcribe_chunked,
)


def run(coro):
    return asyncio.run(coro)


def seg(text: str, start: float, end: float, words: bool = True) -> TranscriptionSegment:
    if not words:
        return TranscriptionSegment(text=text, start=start, end=end)
    tokens = text.split()
    span = max(0.001, end - start)
    share = span / len(tokens)
    ws = tuple(
        TranscriptionWord(word=t, start=start + i * share, end=start + (i + 1) * share)
        for i, t in enumerate(tokens)
    )
    return TranscriptionSegment(text=text, start=start, end=end, words=ws)


def result(language: str, segments: list[TranscriptionSegment], prob: float = 0.9) -> TranscriptionResult:
    return TranscriptionResult(
        language=language,
        language_probability=prob,
        duration_seconds=100.0,
        segments=tuple(segments),
        full_text=" ".join(s.text for s in segments),
    )


class ScriptedTranscriber(BaseTranscriber):
    """Returns canned results keyed by chunk file name (chunk-NNN.wav)."""

    name = "scripted"

    def __init__(self, scripts: dict[str, TranscriptionResult]) -> None:
        self.scripts = scripts
        self.calls: list[str] = []

    def transcribe(self, audio_path: str, language: str | None = None) -> TranscriptionResult:
        self.calls.append(str(Path(audio_path).name))
        key = Path(audio_path).name
        if key not in self.scripts:
            raise AssertionError(f"unexpected transcription call for {key}")
        return self.scripts[key]


# ---- split_audio (real FFmpeg) -------------------------------------------------

def test_split_produces_overlapping_chunks_with_correct_offsets(ffmpeg_bin, ffprobe_bin, tmp_path):
    # 13s mono tone, split with 5s chunks / 1s overlap → step 4 → offsets 0, 4, 8.
    source = tmp_path / "full.wav"
    subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=13",
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(source)],
        capture_output=True, check=True,
    )
    duration = run(probe_duration(ffprobe_bin, source, 30))
    assert 12.7 <= duration <= 13.3

    chunks = run(split_audio(ffmpeg_bin, source, tmp_path / "chunks", 5, 1, duration, 30))

    assert [round(c.offset_seconds, 1) for c in chunks] == [0.0, 4.0, 8.0]
    # Last chunk covers the tail (no tiny 1s remainder chunk).
    assert chunks[-1].end_seconds >= duration - 0.5
    for chunk in chunks:
        assert chunk.path.is_file() and chunk.path.stat().st_size > 0
        actual = run(probe_duration(ffprobe_bin, chunk.path, 30))
        assert abs(actual - chunk.duration_seconds) < 0.35
    # The source is untouched.
    assert run(probe_duration(ffprobe_bin, source, 30)) == pytest.approx(duration, abs=0.05)


def test_split_short_recording_is_single_chunk_without_reencode(ffmpeg_bin, ffprobe_bin, tmp_path):
    source = tmp_path / "short.wav"
    subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(source)],
        capture_output=True, check=True,
    )
    duration = run(probe_duration(ffprobe_bin, source, 30))
    chunks = run(split_audio(ffmpeg_bin, source, tmp_path / "chunks", 300, 15, duration, 30))
    assert len(chunks) == 1
    assert chunks[0].path is source  # no re-encode for short audio
    assert not (tmp_path / "chunks").exists()


def test_probe_duration_rejects_garbage(ffprobe_bin, tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav file at all " * 16)
    with pytest.raises(ValueError):
        run(probe_duration(ffprobe_bin, bad, 5))


# ---- merge_chunk_results (ownership boundaries) --------------------------------

def _three_chunks() -> list[AudioChunk]:
    return [
        AudioChunk(path=Path("chunk-000.wav"), offset_seconds=0.0, duration_seconds=5.0),
        AudioChunk(path=Path("chunk-001.wav"), offset_seconds=4.0, duration_seconds=5.0),
        AudioChunk(path=Path("chunk-002.wav"), offset_seconds=8.0, duration_seconds=5.0),
    ]


def test_merge_uses_overlap_midpoint_ownership_and_shifts_words():
    # overlap = 1s → boundaries at 4.5 and 8.5 (midpoints of the overlaps).
    chunks = _three_chunks()
    scripts = {
        "chunk-000.wav": result("en", [
            seg("alpha one two", 0.0, 2.0),        # abs 0.0-2.0 → owned by c0
            seg("beta three four", 3.5, 4.4),      # abs 3.5-4.4 → c0 (start < 4.5)
            seg("gamma five", 4.0, 4.8),           # abs 4.0 → c0; DUPLICATED in c1
        ], 0.9),
        "chunk-001.wav": result("en", [
            seg("gamma five", 0.0, 0.8),           # abs 4.0-4.8 dup → NOT owned by c1 (4.0 < 4.5)
            seg("delta six seven", 1.0, 2.0),      # abs 5.0-6.0 → c1
            seg("epsilon eight", 4.4, 5.2),        # abs 8.4-9.2 → c1 (8.4 < 8.5); dup in c2
        ], 0.5),
        "chunk-002.wav": result("en", [
            seg("epsilon eight", 0.4, 1.2),        # abs 8.4 → NOT owned by c2 (8.4 < 8.5)
            seg("zeta nine ten", 0.6, 1.4),        # abs 8.6-9.4 → c2
        ], 0.7),
    }
    merged = merge_chunk_results(chunks, [scripts[c.path.name] for c in chunks], overlap_seconds=1)

    texts = [s.text for s in merged.segments]
    # Exactly one copy of every utterance — duplicates at the boundary were
    # dropped, nothing was lost.
    assert texts == ["alpha one two", "beta three four", "gamma five",
                     "delta six seven", "epsilon eight", "zeta nine ten"]
    # Absolute timings after the chunk-offset shift.
    starts = [s.start for s in merged.segments]
    assert starts == [0.0, 3.5, 4.0, 5.0, 8.4, 8.6]
    # Word-level timestamps are shifted too.
    gamma = merged.segments[2]
    assert gamma.words[0].start == pytest.approx(4.0)
    assert gamma.words[-1].end == pytest.approx(4.8)
    # Language from the first non-empty chunk.
    assert merged.language == "en"
    assert merged.language_probability == pytest.approx(0.9)
    # Total duration from the chunk layout.
    assert merged.duration_seconds == pytest.approx(13.0)
    assert merged.full_text == " ".join(texts)
    # Sorted by start.
    assert starts == sorted(starts)


def test_merge_no_overlap_keeps_everything_in_order():
    chunks = [
        AudioChunk(path=Path("a.wav"), offset_seconds=0.0, duration_seconds=5.0),
        AudioChunk(path=Path("b.wav"), offset_seconds=5.0, duration_seconds=5.0),
    ]
    r1 = result("fr", [seg("un deux", 0.0, 1.0)], 0.8)
    r2 = result("fr", [seg("trois quatre", 0.5, 1.5)], 0.6)
    merged = merge_chunk_results(chunks, [r1, r2], overlap_seconds=0)
    assert [s.text for s in merged.segments] == ["un deux", "trois quatre"]
    assert [s.start for s in merged.segments] == [0.0, 5.5]
    assert merged.language_probability == pytest.approx(0.8)


def test_merge_rejects_count_mismatch():
    chunks = _three_chunks()
    with pytest.raises(ValueError, match="mismatch"):
        merge_chunk_results(chunks, [], overlap_seconds=1)


def test_merge_single_chunk_passthrough():
    chunks = [AudioChunk(path=Path("only.wav"), offset_seconds=0.0, duration_seconds=10.0)]
    only = result("en", [seg("hello world", 1.0, 2.0)])
    merged = merge_chunk_results(chunks, [only], overlap_seconds=1)
    assert [s.text for s in merged.segments] == ["hello world"]
    assert merged.segments[0].start == 1.0


# ---- transcribe_chunked (async orchestration) ----------------------------------

def test_transcribe_chunked_single_chunk_calls_provider_once(ffmpeg_bin, ffprobe_bin, tmp_path):
    source = tmp_path / "short.wav"
    subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(source)],
        capture_output=True, check=True,
    )
    script = result("en", [seg("just one segment", 0.0, 1.0)])
    provider = ScriptedTranscriber({"short.wav": script})
    merged = run(transcribe_chunked(provider, ffmpeg_bin, ffprobe_bin, source,
                                     chunk_seconds=300, overlap_seconds=15,
                                     probe_timeout_seconds=30, split_timeout_seconds=30))
    assert provider.calls == ["short.wav"]
    assert merged.full_text == "just one segment"


def test_transcribe_chunked_splits_and_merges(ffmpeg_bin, ffprobe_bin, tmp_path):
    source = tmp_path / "full.wav"
    subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=13",
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(source)],
        capture_output=True, check=True,
    )
    scripts = {
        "chunk-000.wav": result("en", [seg("one two", 0.0, 1.0)]),
        "chunk-001.wav": result("en", [seg("three four", 1.0, 2.0)]),
        "chunk-002.wav": result("en", [seg("five six", 1.0, 2.0)]),
    }
    provider = ScriptedTranscriber(scripts)
    merged = run(transcribe_chunked(provider, ffmpeg_bin, ffprobe_bin, source,
                                     chunk_seconds=5, overlap_seconds=1,
                                     probe_timeout_seconds=30, split_timeout_seconds=30))
    assert provider.calls == ["chunk-000.wav", "chunk-001.wav", "chunk-002.wav"]
    assert [s.start for s in merged.segments] == [0.0, 5.0, 9.0]
    assert merged.full_text == "one two three four five six"
