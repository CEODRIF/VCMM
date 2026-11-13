"""FFmpeg clip renderer (Phase 5 foundation).

Cuts one window out of the downloaded source and re-encodes it to the fixed
vertical profile (1080x1920, H.264 + AAC, yuv420p, fast-start) with the
aspect ratio NEVER distorted:

- the source geometry is measured as raw bytes (FFprobe) plus a direct MP4
  box scan for the rotation tag (phones store portrait video as landscape
  frames + a ``rot ``/``dtrt`` box; ffmpeg >= 5 may ignore those boxes and
  older ffprobe cannot read them, so the box is the ground truth),
- the :mod:`app.rendering.profiles` planner decides the single uniform
  scale + center-crop (``cover``, default) or black-pad (``contain``),
- ``-ss`` is placed BEFORE ``-i`` (input seek: fast and bounded, instead of
  decoding from 0), and because an input-side seek resets the timestamps the
  window length is passed as ``-t duration`` (never ``-to end``),
- a DETECTED clockwise rotation C is baked into the pixels with an EXPLICIT
  filter prepended to the plan (``transpose=1`` / ``hflip,vflip`` /
  ``transpose=2``). Why not ``-autorotate``? It is a no-op on FFmpeg
  7.0.2 (ffmpeg-static) for MP4 ``rot ``/``dtrt`` boxes — verified
  empirically — and the explicit filter is deterministic across FFmpeg
  versions. The output therefore always stores 0 rotation.

The renderer produces the file; the OUTPUT CONTRACT (exact codecs,
resolution, pixel format, rotation, duration) is enforced by
:func:`app.rendering.validation.verify_render_output` in the processor —
a non-conforming clip is never uploaded.

Subtitles (Phase 5B) are burned in the SAME command via a libass ``ass=``
filter appended after the reframe plan (PlayRes == output size, so no
rescaling): the per-job temp directory holds the generated ASS file, whose
path is filtergraph-escaped and removed on EVERY path (success, failure,
timeout, cancellation). With no caption words the command line is exactly
the Phase 5A one.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

from ..queue.models import ERROR_RENDER_FAILED
from ..validation.media import parse_frame_rate
from .base import BaseRenderer, RenderSpec
from .captions import CaptionsError, build_captions_ass, filter_escape, write_ass
from .ffmpeg import FFmpegError, run_ffmpeg
from .profiles import RenderProfile, normalize_rotation_cw, select_output_fps
from .validation import mp4_rotation_degrees, rotation_of

#: Explicit filter that bakes a clockwise source rotation into the pixels,
#: applied BEFORE the scale/crop plan (which is computed on the displayed
#: orientation). transpose=1 = 90 CW, hflip,vflip = 180, transpose=2 =
#: 90 CCW (= 270 CW). Verified against ffmpeg 7.0.2, where ``-autorotate``
#: does not read the MP4 ``rot ``/``dtrt`` boxes at all.
_ROTATION_FILTERS: dict[int, str] = {
    0: "",
    90: "transpose=1",
    180: "hflip,vflip",
    270: "transpose=2",
}


class FFmpegRenderer(BaseRenderer):
    """The real rendering backend: one managed FFmpeg subprocess per clip."""

    def __init__(
        self,
        ffmpeg_path: str,
        ffprobe_path: str,
        profile: Optional[RenderProfile] = None,
        timeout_seconds: float = 900.0,
        probe_timeout_seconds: float = 60.0,
        fps_low: int = 30,
        fps_high: int = 60,
        fps_high_source_threshold: float = 55.0,
    ) -> None:
        self._ffmpeg = ffmpeg_path
        self._ffprobe = ffprobe_path
        self._profile = profile or RenderProfile()
        self._timeout = timeout_seconds
        self._probe_timeout = probe_timeout_seconds
        # Source-aware frame-rate selection (TASK 007): high-fps sources are
        # rendered at fps_high instead of being dropped to the profile rate.
        self._fps_low = fps_low
        self._fps_high = fps_high
        self._fps_high_source_threshold = fps_high_source_threshold

    @property
    def profile(self) -> RenderProfile:
        """The output profile this renderer produces (contract source of truth)."""
        return self._profile

    async def render(
        self,
        source_path: str,
        start: float,
        end: float,
        spec: RenderSpec,
        output_path: str,
    ) -> str:
        """Cuts ``[start, end)`` from the source and renders it to the profile.

        Raises :class:`FFmpegError` (safe category/message) on an invalid
        window, an unprobeable source, or any FFmpeg failure; the partial
        output file is removed on every failure path (see ``run_ffmpeg``).
        """
        if not (0 <= start < end):
            raise FFmpegError(ERROR_RENDER_FAILED, "clip window is invalid")

        source = Path(source_path)
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        width, height, rotation, source_fps = await self._probe_source(source)
        cw = normalize_rotation_cw(rotation)
        base_plan = self._profile.video_filter(width, height, cw)
        rotate_filter = _ROTATION_FILTERS[cw]
        filter_chain = f"{rotate_filter},{base_plan}" if rotate_filter else base_plan
        # Preserve the source rate where practical: 60 fps gameplay stays 60;
        # slower/unknown sources keep the profile default (no frame dupes).
        out_fps = select_output_fps(
            source_fps, self._fps_low, self._fps_high, self._fps_high_source_threshold
        )

        # Captions (Phase 5B): real words only, prepared BEFORE the process
        # starts (deterministic), written into the per-job temp dir, and
        # removed on every path. No words -> the exact Phase 5A command.
        ass_path: Path | None = None
        if spec.captions and spec.caption_words:
            ass_text = build_captions_ass(
                spec.caption_words,
                spec.caption_style,
                duration=end - start,
                play_res_x=spec.width,
                play_res_y=spec.height,
            )
            if ass_text is not None:
                try:
                    ass_path = write_ass(ass_text, output.parent / "captions.ass")
                    filter_chain = f"{filter_chain},ass={filter_escape(str(ass_path))}"
                except (CaptionsError, OSError):
                    ass_path = None
                    raise FFmpegError(ERROR_RENDER_FAILED, "captions could not be prepared") from None

        # All options before -i are INPUT options: -ss/-t seek the input
        # (fast, bounded). -noautorotate disables FFmpeg's default display-
        # matrix auto-rotation: the source rotation is applied EXPLICITLY by
        # the transpose filter (deterministic across FFmpeg versions — this
        # build's -autorotate ignores MP4 rot/dtrt boxes entirely). Note:
        # with an input-side seek the timestamps reset, hence -t (duration)
        # and never -to (absolute end).
        args = [
            "-ss", f"{start:.3f}",
            "-t", f"{end - start:.3f}",
            "-noautorotate",
            "-i", str(source),
            "-vf", filter_chain,
            *self._profile.ffmpeg_video_args(out_fps),
            *self._profile.ffmpeg_audio_args(),
            *self._profile.ffmpeg_container_args(),
            str(output),
        ]
        try:
            await run_ffmpeg(self._ffmpeg, args, self._timeout, output_file=output)
        finally:
            if ass_path is not None:
                ass_path.unlink(missing_ok=True)
        return str(output)

    async def _probe_source(self, source: Path) -> tuple[int, int, float, float | None]:
        """Measures the source video geometry + rotation + avg fps (raw bytes, never trusted)."""
        if not source.is_file() or source.stat().st_size == 0:
            raise FFmpegError(ERROR_RENDER_FAILED, "source file is missing or empty")

        proc = await asyncio.create_subprocess_exec(
            self._ffprobe,
            "-v", "error",
            "-print_format", "json",
            "-show_streams",
            str(source),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self._probe_timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise FFmpegError(ERROR_RENDER_FAILED, "source analysis timed out") from None
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
            raise FFmpegError(ERROR_RENDER_FAILED, "the source could not be analyzed")
        try:
            payload = json.loads(stdout.decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError:
            raise FFmpegError(ERROR_RENDER_FAILED, "the source metadata could not be parsed") from None

        video = next((s for s in payload.get("streams") or [] if s.get("codec_type") == "video"), None)
        if video is None:
            raise FFmpegError(ERROR_RENDER_FAILED, "the source has no video stream")
        try:
            width = int(video.get("width") or 0)
            height = int(video.get("height") or 0)
        except (TypeError, ValueError):
            raise FFmpegError(ERROR_RENDER_FAILED, "the source resolution could not be determined") from None
        if width <= 0 or height <= 0:
            raise FFmpegError(ERROR_RENDER_FAILED, "the source resolution could not be determined")

        # Rotation: prefer the MP4 box (ground truth, any ffprobe version);
        # fall back to the ffprobe side data (covers MOV/WEBM).
        rotation = rotation_of(video)
        mp4_rotation = mp4_rotation_degrees(source)
        if mp4_rotation is not None:
            rotation = mp4_rotation
        # Measured average frame rate (drives source-aware output fps).
        source_fps = parse_frame_rate(video.get("avg_frame_rate")) or parse_frame_rate(
            video.get("r_frame_rate")
        )
        return width, height, rotation, source_fps
