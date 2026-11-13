"""Controlled chunking of long recordings for transcription (Phase 4).

Audio longer than ``chunk_seconds`` is split with FFmpeg into overlapping
chunks (``-ss/-t``), each chunk is transcribed independently, and the
results are merged back into one timeline:

- every segment/word is shifted by the chunk offset,
- each chunk "owns" its segments up to the MIDPOINT of its overlap window
  with the next chunk, so a segment spoken across the boundary is transcribed
  once (by the earlier chunk) and never duplicated or dropped,
- language is taken from the first chunk that detected one.

The chunking logic is provider-agnostic (tested with a fake provider) and
only touches the worker's temp directory.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .base import BaseTranscriber, TranscriptionResult, TranscriptionSegment, TranscriptionWord

logger = logging.getLogger("clipforge.transcription")


@dataclass(frozen=True)
class AudioChunk:
    path: Path
    offset_seconds: float  # where this chunk starts in the full recording
    duration_seconds: float  # actual length of the chunk file (includes overlap)

    @property
    def end_seconds(self) -> float:
        return self.offset_seconds + self.duration_seconds


async def probe_duration(ffprobe_path: str, file_path: Path, timeout_seconds: float) -> float:
    """Best-effort duration probe (raises on unreadable files)."""
    proc = await asyncio.create_subprocess_exec(
        ffprobe_path, "-v", "error", "-show_entries", "format=duration",
        "-print_format", "json", str(file_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ValueError("duration probe timed out") from None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode != 0:
        raise ValueError("duration probe failed")
    payload = json.loads(stdout.decode("utf-8", "replace") or "{}")
    duration = (payload.get("format") or {}).get("duration")
    try:
        value = float(duration)
    except (TypeError, ValueError):
        raise ValueError("duration could not be determined") from None
    if not value > 0:
        raise ValueError("duration could not be determined")
    return value


async def split_audio(
    ffmpeg_path: str,
    source: Path,
    dest_dir: Path,
    chunk_seconds: int,
    overlap_seconds: int,
    total_duration: float,
    timeout_seconds: float,
) -> list[AudioChunk]:
    """Splits ``source`` into overlapping chunks (or returns a single chunk).

    Short recordings (<= chunk_seconds) are NOT re-encoded — the original
    file is used as the only chunk.
    """
    if total_duration <= chunk_seconds:
        return [AudioChunk(path=source, offset_seconds=0.0, duration_seconds=total_duration)]

    dest_dir.mkdir(parents=True, exist_ok=True)
    step = max(1, chunk_seconds - overlap_seconds)
    chunks: list[AudioChunk] = []
    index = 0
    offset = 0.0
    while offset < total_duration - 0.5:
        # Stop as soon as the previous chunk already reaches the tail —
        # avoids creating a tiny redundant final chunk.
        if chunks and chunks[-1].end_seconds >= total_duration - 0.5:
            break
        remaining = total_duration - offset
        length = min(float(chunk_seconds), remaining)
        if length <= 0:
            break
        dest = dest_dir / f"chunk-{index:03d}.wav"
        proc = await asyncio.create_subprocess_exec(
            ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{offset:.3f}", "-i", str(source),
            "-t", f"{length:.3f}",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            str(dest),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise ValueError(f"chunk {index} split timed out") from None
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
            detail = (stderr or b"").decode("utf-8", "replace")[:200].strip()
            raise ValueError(f"chunk {index} split failed" + (f": {detail}" if detail else ""))
        chunks.append(AudioChunk(path=dest, offset_seconds=offset, duration_seconds=length))
        index += 1
        offset += step
        if offset >= total_duration:
            break
    if not chunks:
        raise ValueError("audio split produced no chunks")
    return chunks


def _shift_segment(segment: TranscriptionSegment, offset: float) -> TranscriptionSegment:
    words = tuple(
        TranscriptionWord(word=w.word, start=w.start + offset, end=w.end + offset) for w in segment.words
    )
    return TranscriptionSegment(
        text=segment.text,
        start=segment.start + offset,
        end=segment.end + offset,
        words=words,
    )


def merge_chunk_results(
    chunks: list[AudioChunk],
    results: list[TranscriptionResult],
    overlap_seconds: int,
) -> TranscriptionResult:
    """Merges per-chunk transcriptions into one aligned timeline.

    Ownership rule: chunk ``i`` keeps the segments whose ABSOLUTE start time
    falls in ``[lower_i, upper_i)`` where

    - ``lower_0`` = 0, and for ``i > 0`` the midpoint of the overlap between
      chunk ``i-1`` and chunk ``i`` (``offset_i + overlap/2``), and
    - ``upper_i`` = the midpoint of the overlap between chunk ``i`` and chunk
      ``i+1`` (``offset_{i+1} + overlap/2``), infinity for the last chunk.

    Consecutive chunks overlap, so every spoken region is captured by at
    least one chunk, and the midpoint rule keeps each segment exactly once
    (no duplication, no silent gaps) even when a sentence straddles a
    boundary.
    """
    if len(chunks) != len(results):
        raise ValueError("chunk/result count mismatch")

    total_duration = max((c.end_seconds for c in chunks), default=0.0)
    segments: list[TranscriptionSegment] = []
    language: str | None = None
    language_probability: float | None = None

    for i, (chunk, result) in enumerate(zip(chunks, results)):
        if language is None and result.full_text.strip():
            language = result.language
            language_probability = result.language_probability
        upper = chunks[i + 1].offset_seconds + overlap_seconds / 2.0 if i + 1 < len(chunks) else float("inf")
        lower = 0.0 if i == 0 else chunk.offset_seconds + overlap_seconds / 2.0
        for segment in result.segments:
            if not segment.text:
                continue
            start = segment.start + chunk.offset_seconds
            if start < lower or start >= upper:
                continue  # owned by a neighbouring chunk
            segments.append(_shift_segment(segment, chunk.offset_seconds))

    segments.sort(key=lambda s: s.start)
    full_text = " ".join(s.text for s in segments)
    return TranscriptionResult(
        language=language or (results[0].language if results else "unknown"),
        language_probability=language_probability,
        duration_seconds=round(total_duration, 3),
        segments=tuple(segments),
        full_text=full_text,
    )


async def transcribe_chunked(
    transcriber: BaseTranscriber,
    ffmpeg_path: str,
    ffprobe_path: str,
    audio_path: Path,
    chunk_seconds: int,
    overlap_seconds: int,
    probe_timeout_seconds: float,
    split_timeout_seconds: float,
    language: str | None = None,
) -> TranscriptionResult:
    """Transcribes ``audio_path``, chunking it when it exceeds the limit.

    The provider call is CPU-bound (local Whisper inference) and is run in a
    worker thread via ``asyncio.to_thread`` so the event loop — and with it
    the job's lease-renewal task — stays responsive during long recordings.
    """
    total_duration = await probe_duration(ffprobe_path, audio_path, probe_timeout_seconds)
    chunks = await split_audio(
        ffmpeg_path, audio_path, audio_path.parent / "chunks",
        chunk_seconds, overlap_seconds, total_duration, split_timeout_seconds,
    )
    if len(chunks) == 1:
        return await asyncio.to_thread(transcriber.transcribe, str(chunks[0].path), language)
    logger.info("transcribing %d overlapping chunks (%ss, %ss overlap)",
                len(chunks), chunk_seconds, overlap_seconds)
    results: list[TranscriptionResult] = []
    for chunk in chunks:
        results.append(await asyncio.to_thread(transcriber.transcribe, str(chunk.path), language))
    return merge_chunk_results(chunks, results, overlap_seconds)
