"""FFprobe-based output validation for rendered clips (Phase 5).

Every rendered file is re-probed as raw bytes (never trusted): container,
codecs, exact resolution, pixel format, rotation and duration are measured
and checked against the :class:`~app.rendering.profiles.RenderProfile`
contract. A mismatch is a hard error — a non-conforming clip must never be
uploaded to storage or reported as ready.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..queue.models import ERROR_RENDER_INVALID
from .profiles import RenderProfile

_MP4_CONTAINER_BOXES = frozenset({b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"edts"})

#: MP4 family container names FFprobe reports.
_MP4_CONTAINERS = frozenset({
    "mov,mp4,m4a,3gp,3g2,mj2",
    "mov,mp4,m4a,3gp,3g2,mj2 (MP4 base Media)",
    "mp4",
    "mov",
})

_ROTATION_RE = re.compile(r"roll:\s*(-?\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class RenderOutput:
    """Measured facts about a rendered file."""

    container: str
    width: int
    height: int
    video_codec: str
    pix_fmt: str
    audio_codec: str | None
    duration_seconds: float
    rotation: float
    file_size_bytes: int


class RenderValidationError(ValueError):
    """A rendered file does not meet the output contract."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.category = ERROR_RENDER_INVALID
        self.message = message


def rotation_of(stream: dict) -> float:
    """Best-effort rotation in degrees from a video stream (0.0 if none).

    Reads, in order: the QuickTime ``rotate`` tag, stream side data
    (``rotation`` / displaymatrix ``roll``). Shared with the renderer,
    which needs the SOURCE rotation to plan the reframe.
    """
    tags = stream.get("tags") or {}
    if isinstance(tags.get("rotate"), str) and tags["rotate"].strip():
        try:
            return float(tags["rotate"])
        except ValueError:
            pass
    for side in stream.get("side_data_list") or []:
        if not isinstance(side, dict):
            continue
        if side.get("rotation") is not None:
            try:
                return float(side["rotation"])
            except (TypeError, ValueError):
                pass
        matrix = side.get("displaymatrix")
        if isinstance(matrix, str):
            match = _ROTATION_RE.search(matrix)
            if match:
                # The stored display-matrix roll is counter-clockwise
                # positive; normalize to the clockwise convention.
                return -float(match.group(1))
    return 0.0


def _mp4_boxes(data: bytes, start: int, end: int):
    """Yields (pos, size, type) of the boxes directly inside data[start:end]."""
    pos = start
    while pos + 8 <= end:
        size = int.from_bytes(data[pos:pos + 4], "big")
        box_type = bytes(data[pos + 4:pos + 8])
        if size < 8:
            return
        yield pos, size, box_type
        pos += size


def _contains_box(data: bytes, start: int, end: int, name: bytes) -> bool:
    for _pos, size, box_type in _mp4_boxes(data, start, end):
        if box_type == name:
            return True
        if box_type in _MP4_CONTAINER_BOXES and size > 8 and _contains_box(data, _pos + 8, _pos + size, name):
            return True
    return False


def mp4_rotation_degrees(path: Path) -> float | None:
    """Reads rotation straight from the MP4 boxes (ground truth).

    Checks each video track's ``mdia`` for the legacy ``rot `` atom (value in
    1/16 degrees — the phone-camera convention) and the modern ``dtrt`` box
    (value in degrees). Returns None for non-MP4 files or files without a
    rotation box.

    Why not ffprobe only? Older ffprobe (4.x) does not expose side data in
    JSON at all, and recent ffmpeg (5+) no longer writes a ``rot `` box from
    ``-metadata rotate=`` — so the box is the only version-independent
    source. The ffprobe value is kept as a fallback for MOV/WEBM.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < 16:
        return None
    top = list(_mp4_boxes(data, 0, len(data)))
    if not any(box_type == b"ftyp" for _p, _s, box_type in top):
        return None  # not an MP4-family file
    moovs = [(_p, _s) for _p, _s, box_type in top if box_type == b"moov"]
    if not moovs:
        return None
    moov_pos, moov_size = moovs[0]
    for trak_pos, trak_size, _t in _mp4_boxes(data, moov_pos + 8, moov_pos + moov_size):
        if not _contains_box(data, trak_pos + 8, trak_pos + trak_size, b"vmhd"):
            continue  # audio track
        for box_pos, box_size, box_type in _mp4_boxes(data, trak_pos + 8, trak_pos + trak_size):
            if box_type != b"mdia":
                continue
            for sub_pos, sub_size, sub_type in _mp4_boxes(data, box_pos + 8, box_pos + box_size):
                if sub_size < 12:
                    continue
                if sub_type == b"rot ":
                    return float(int.from_bytes(data[sub_pos + 8:sub_pos + 12], "big")) / 16.0
                if sub_type == b"dtrt":
                    return float(int.from_bytes(data[sub_pos + 8:sub_pos + 12], "big") % 360)
    return None


async def probe_render_output(ffprobe_path: str, output: Path, timeout_seconds: float) -> RenderOutput:
    """Probes a rendered file; raises RenderValidationError when unreadable."""
    if not output.is_file() or output.stat().st_size == 0:
        raise RenderValidationError("rendered file is missing or empty")

    proc = await asyncio.create_subprocess_exec(
        ffprobe_path, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(output),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RenderValidationError("rendered file could not be analyzed (probe timeout)") from None
    except asyncio.CancelledError:
        proc.kill()
        try:
            await proc.wait()
        except asyncio.CancelledError:
            pass
        raise
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    if proc.returncode != 0:
        raise RenderValidationError("rendered file could not be analyzed")
    try:
        payload = json.loads(stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        raise RenderValidationError("rendered file metadata could not be parsed") from None

    format_info = payload.get("format") or {}
    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not format_info.get("format_name") or video is None:
        raise RenderValidationError("rendered file has no video stream")
    try:
        duration = float(format_info.get("duration") or video.get("duration") or 0)
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
    except (TypeError, ValueError):
        raise RenderValidationError("rendered file dimensions/duration could not be determined") from None
    if duration <= 0 or width <= 0 or height <= 0:
        raise RenderValidationError("rendered file has invalid dimensions or duration")

    return RenderOutput(
        container=str(format_info["format_name"]),
        width=width,
        height=height,
        video_codec=str(video.get("codec_name") or "unknown"),
        pix_fmt=str(video.get("pix_fmt") or "unknown"),
        audio_codec=str(audio.get("codec_name")) if audio else None,
        duration_seconds=round(duration, 3),
        rotation=_measured_rotation(video, output),
        file_size_bytes=output.stat().st_size,
    )


def _measured_rotation(stream: dict, path: Path) -> float:
    """Rotation as stored in the file: MP4 box first, ffprobe fallback."""
    mp4_rotation = mp4_rotation_degrees(path)
    if mp4_rotation is not None:
        return mp4_rotation
    return rotation_of(stream)


async def verify_render_output(
    ffprobe_path: str,
    output: Path,
    profile: RenderProfile,
    expected_duration: float,
    timeout_seconds: float,
    tolerance_ratio: float = 0.15,
) -> RenderOutput:
    """Checks a rendered file against the full output contract.

    Raises :class:`RenderValidationError` (category ``render_invalid``,
    permanent) on any violation; returns the measured facts otherwise.
    """
    info = await probe_render_output(ffprobe_path, output, timeout_seconds)

    if info.container not in _MP4_CONTAINERS:
        raise RenderValidationError("output is not an MP4 container")
    if info.video_codec != "h264":
        raise RenderValidationError("output video codec is not H.264")
    if (info.width, info.height) != (profile.width, profile.height):
        raise RenderValidationError(f"output resolution is {info.width}x{info.height}, expected {profile.width}x{profile.height}")
    if info.pix_fmt != profile.pix_fmt:
        raise RenderValidationError(f"output pixel format is {info.pix_fmt}, expected {profile.pix_fmt}")
    if info.audio_codec != profile.audio_codec:
        raise RenderValidationError("output audio codec is not AAC")
    # The output must carry NO rotation: -autorotate bakes the source
    # orientation into the pixels, so the stored frames are upright (0).
    if abs(info.rotation) > 0.5:
        raise RenderValidationError("output still carries a rotation tag")

    if expected_duration > 0:
        drift = abs(info.duration_seconds - expected_duration) / expected_duration
        if drift > tolerance_ratio:
            raise RenderValidationError(
                f"output duration {info.duration_seconds}s deviates >{int(tolerance_ratio * 100)}% "
                f"from the expected {expected_duration}s"
            )
    return info
