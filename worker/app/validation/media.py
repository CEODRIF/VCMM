"""FFprobe-based media validation (Phase 3).

The browser's file name, extension and MIME type are NEVER trusted. Every
downloaded source is probed as raw bytes: container format, video stream
presence, audio availability, codecs, duration, resolution and file size are
measured and validated against explicit infrastructure limits.

Note: the product supports REQUESTED clip durations of 60-180 seconds — that
is a different setting from the SOURCE video duration, which is bounded only
by the configured infrastructure limit (``max_source_duration_seconds``).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from ..queue.models import (
    ERROR_CORRUPT_MEDIA,
    ERROR_FFPROBE,
    ERROR_INVALID_FILE,
    ERROR_NO_VIDEO_STREAM,
    ERROR_SOURCE_TOO_LARGE,
    ERROR_SOURCE_TOO_LONG,
    ERROR_UNSUPPORTED_FORMAT,
)

#: Container format names accepted by FFprobe (format_name values).
SUPPORTED_CONTAINERS = frozenset(
    {
        "mov,mp4,m4a,3gp,3g2,mj2",  # mp4 family
        "mov,mp4,m4a,3gp,3g2,mj2 (MP4 base Media)",
        "quicktime",
        "matroska,webm",
        "webm",
    }
)

#: FFprobe's "we gave up on this" signature: without a container hint it
#: demuxes arbitrary bytes as a `data` container with a `bintext` stream.
#: That is corrupt input, not merely an unsupported format.
GARBAGE_FORMAT_NAMES = frozenset({"data"})
GARBAGE_CODEC_NAMES = frozenset({"bintext"})


class MediaValidationError(ValueError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


@dataclass(frozen=True)
class MediaInfo:
    container: str
    duration_seconds: float
    width: int
    height: int
    video_codec: str
    has_video: bool
    has_audio: bool
    audio_codec: str | None
    file_size_bytes: int


async def probe_media(ffprobe_path: str, file_path: Path, timeout_seconds: float) -> MediaInfo:
    """Runs ffprobe on the file and returns measured media info.

    Raises MediaValidationError with a safe category/message when the file
    cannot be read as a container or has no usable video stream.
    """
    if not file_path.is_file() or file_path.stat().st_size == 0:
        raise MediaValidationError(ERROR_INVALID_FILE, "The uploaded file is empty or unreadable.")

    proc = await asyncio.create_subprocess_exec(
        ffprobe_path,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(file_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise MediaValidationError(ERROR_FFPROBE, "Media analysis timed out; the file may be corrupted.") from None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    if proc.returncode != 0:
        raise MediaValidationError(
            ERROR_CORRUPT_MEDIA,
            "The file could not be read as a valid video. It may be corrupted or not a supported video file.",
        )

    try:
        payload = json.loads(stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        raise MediaValidationError(ERROR_CORRUPT_MEDIA, "Media metadata could not be parsed.") from None

    format_info = payload.get("format") or {}
    streams = payload.get("streams") or []
    if not format_info.get("format_name") or not streams:
        raise MediaValidationError(ERROR_CORRUPT_MEDIA, "The file could not be read as a valid video container.")

    container = str(format_info["format_name"])

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    # Garbage-byte signature (raw bytes with no real container).
    if container in GARBAGE_FORMAT_NAMES or (
        video_stream is not None and video_stream.get("codec_name") in GARBAGE_CODEC_NAMES
    ):
        raise MediaValidationError(
            ERROR_CORRUPT_MEDIA,
            "The file could not be read as a valid video. It may be corrupted or not a supported video file.",
        )

    if container not in SUPPORTED_CONTAINERS:
        raise MediaValidationError(
            ERROR_UNSUPPORTED_FORMAT,
            "Unsupported video format. Please upload an MP4, MOV or WEBM file.",
        )

    if video_stream is None:
        raise MediaValidationError(ERROR_NO_VIDEO_STREAM, "No video track was found in the file.")

    try:
        duration = float(format_info.get("duration") or (video_stream.get("duration") or 0))
    except (TypeError, ValueError):
        raise MediaValidationError(ERROR_CORRUPT_MEDIA, "Media duration could not be determined.") from None
    if not duration > 0:
        raise MediaValidationError(ERROR_CORRUPT_MEDIA, "Media duration could not be determined.")

    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise MediaValidationError(ERROR_CORRUPT_MEDIA, "Media resolution could not be determined.")

    return MediaInfo(
        container=container,
        duration_seconds=round(duration, 3),
        width=width,
        height=height,
        video_codec=str(video_stream.get("codec_name") or "unknown"),
        has_video=True,
        has_audio=audio_stream is not None,
        audio_codec=str(audio_stream.get("codec_name")) if audio_stream else None,
        file_size_bytes=file_path.stat().st_size,
    )


def validate_media(info: MediaInfo, max_source_duration_seconds: float, max_upload_size_bytes: int) -> dict[str, object]:
    """Validates measured media info; returns the safe metadata payload.

    Duration here is the SOURCE video duration — bounded by the
    infrastructure limit, NOT by the 60-180s requested clip length.
    """
    if info.file_size_bytes > max_upload_size_bytes:
        raise MediaValidationError(
            ERROR_SOURCE_TOO_LARGE,
            f"Video file is larger than the {max_upload_size_bytes}-byte infrastructure limit.",
        )
    if info.duration_seconds > max_source_duration_seconds:
        raise MediaValidationError(
            ERROR_SOURCE_TOO_LONG,
            f"Video is longer than the {int(max_source_duration_seconds)}-second infrastructure limit.",
        )
    if not info.has_video:
        raise MediaValidationError(ERROR_NO_VIDEO_STREAM, "No video track was found in the file.")

    return {
        "container": info.container,
        "duration_seconds": info.duration_seconds,
        "width": info.width,
        "height": info.height,
        "video_codec": info.video_codec,
        "has_audio": info.has_audio,
        "audio_codec": info.audio_codec,
        "file_size_bytes": info.file_size_bytes,
    }
