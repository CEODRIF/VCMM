"""FFmpeg-based audio extraction (Phase 4).

Extracts a mono 16 kHz WAV from the downloaded source into the job's temp
directory. The extracted file is the transcription input: the format is
fixed (PCM s16le, 1 channel, 16000 Hz) because Whisper consumes exactly
that and it keeps the temp footprint small (~30 MB/hour).

Safety rules:
- input and output files are validated before and after the run;
- a timeout kills the process and is reported as a safe error;
- corrupt/undecodable audio surfaces as :class:`AudioExtractionError`
  with a user-facing message (no stack traces, no binary internals).

Missing audio STREAMS are not an extraction error: the caller checks the
probe's ``has_audio`` flag first and fails the job with ``no_audio``
(permanent) before extraction is even attempted.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..queue.models import ERROR_AUDIO_EXTRACTION, ERROR_INVALID_FILE

#: Fixed transcription format (mono 16 kHz PCM s16le).
AUDIO_SAMPLE_RATE = 16000
AUDIO_CHANNELS = 1
AUDIO_CODEC = "pcm_s16le"


class AudioExtractionError(RuntimeError):
    """Audio extraction failed for a specific, safe reason."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


async def extract_audio(
    ffmpeg_path: str,
    source: Path,
    destination: Path,
    timeout_seconds: float,
    sample_rate: int = AUDIO_SAMPLE_RATE,
) -> Path:
    """Extracts the audio track of ``source`` to ``destination`` (WAV).

    Returns the destination path on success. Raises
    :class:`AudioExtractionError` when the input is invalid, the decode
    fails (corrupt audio), or the process times out.
    """
    if not source.is_file() or source.stat().st_size == 0:
        raise AudioExtractionError(ERROR_INVALID_FILE, "The source file is empty or unreadable.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    # Never leave a partial output behind on failure.
    destination.unlink(missing_ok=True)

    proc = await asyncio.create_subprocess_exec(
        ffmpeg_path,
        "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-vn",  # drop any video stream
        "-ac", str(AUDIO_CHANNELS),
        "-ar", str(sample_rate),
        "-c:a", AUDIO_CODEC,
        str(destination),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        destination.unlink(missing_ok=True)
        raise AudioExtractionError(
            ERROR_AUDIO_EXTRACTION, "Audio extraction timed out; the file may be corrupted."
        ) from None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    if proc.returncode != 0:
        destination.unlink(missing_ok=True)
        raise AudioExtractionError(
            ERROR_AUDIO_EXTRACTION,
            "The audio track could not be extracted; the file may be corrupted.",
        )

    if not destination.is_file() or destination.stat().st_size == 0:
        destination.unlink(missing_ok=True)
        raise AudioExtractionError(
            ERROR_AUDIO_EXTRACTION,
            "Audio extraction produced an empty file; the file may be corrupted.",
        )

    # Sanity check: the output must be readable by FFprobe as WAV/PCM.
    return destination


async def probe_wav_duration(ffprobe_path: str, wav_path: Path, timeout_seconds: float) -> float:
    """Best-effort duration of the extracted WAV (raises ValueError on error)."""
    payload = await _run_ffprobe(ffprobe_path, wav_path, timeout_seconds)
    try:
        value = float((payload.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        raise ValueError("extracted audio duration could not be determined") from None
    if not value > 0:
        raise ValueError("extracted audio duration could not be determined")
    return value


async def _run_ffprobe(ffprobe_path: str, file_path: Path, timeout_seconds: float) -> dict:
    import json

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
        raise ValueError("audio probe timed out") from None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode != 0:
        raise ValueError("extracted audio is not readable")
    try:
        return json.loads(stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        raise ValueError("extracted audio metadata could not be parsed") from None
