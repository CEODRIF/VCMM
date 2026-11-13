"""Rendering profiles and geometry planning (Phase 5 foundation).

The product output is FIXED: 1080x1920 (9:16 portrait), H.264 + AAC in MP4,
``yuv420p`` (maximum player compatibility), fast-start for streaming.

Geometry contract — inputs of ANY orientation are supported without
distortion (the aspect ratio is always preserved; the frame is never
stretched):

- the SOURCE orientation is its stored dimensions adjusted for the
  container rotation metadata (``rotate`` 90/270 swaps width/height —
  phones store portrait video as landscape frames + a rotation tag);
- ``fit="cover"`` (default): the source is SCALED to cover the full
  1080x1920 frame and CENTER-CROPPED to exactly 1080x1920. No black
  bars; the outer edges of a landscape source are cropped.
- ``fit="contain"``: the source is SCALED to fit inside 1080x1920 and
  PADDED with black bars (top/bottom or left/right) to exactly 1080x1920.
  Nothing is cropped; letterboxing is visible.

Scales are computed as a single uniform factor, then rounded to even
dimensions (required by ``yuv420p``). ``cover`` rounds UP (then crops —
guarantees full coverage), ``contain`` rounds DOWN (then pads —
guarantees no overflow).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def normalize_rotation_cw(rotation_degrees: float) -> int:
    """Normalizes any detected rotation to a CLOCKWISE quarter turn {0,90,180,270}.

    Accepts the raw container value (the ``rot `` box is stored in 1/16
    degrees and converted here, negative angles, 360 offsets) and snaps to
    the nearest quarter turn. The clockwise convention matches what
    ``effective_dimensions`` and the ``transpose`` filter expect.
    """
    c = int(round(rotation_degrees)) % 360
    return int(round(c / 90.0)) % 4 * 90


def effective_dimensions(width: int, height: int, rotation_degrees: float) -> tuple[int, int]:
    """Displayed dimensions after applying the container rotation tag.

    90/270 (or -90) swap width and height; 0/180 keep them.
    """
    # 90 / -90 / 270 all normalize to 90 (Python: -90 % 180 == 90).
    if normalize_rotation_cw(rotation_degrees) % 180 == 90:
        return height, width
    return width, height


def plan_video_filter(
    source_width: int,
    source_height: int,
    rotation_degrees: float,
    out_width: int = 1080,
    out_height: int = 1920,
    fit: str = "cover",
) -> str:
    """Builds the ``-vf`` filter chain for the fixed vertical output.

    Returns a deterministic filter string, e.g.
    ``scale=1080:1920,crop=1080:1920`` (cover) or
    ``scale=540:960,pad=1080:1920:270:480:black`` (contain).
    """
    if source_width <= 0 or source_height <= 0:
        raise ValueError("source dimensions must be positive")
    if out_width <= 0 or out_height <= 0:
        raise ValueError("output dimensions must be positive")
    if fit not in ("cover", "contain"):
        raise ValueError(f"unknown fit mode: {fit!r}")

    eff_w, eff_h = effective_dimensions(source_width, source_height, rotation_degrees)
    scale = max(out_width / eff_w, out_height / eff_h) if fit == "cover" else min(out_width / eff_w, out_height / eff_h)

    if fit == "cover":
        sw = max(2, _even_ceil(eff_w * scale))
        sh = max(2, _even_ceil(eff_h * scale))
        return f"scale={sw}:{sh},crop={out_width}:{out_height}"
    sw = max(2, _even_floor(eff_w * scale))
    sh = max(2, _even_floor(eff_h * scale))
    return f"scale={sw}:{sh},pad={out_width}:{out_height}:(ow-{sw})/2:(oh-{sh})/2:black"


def select_output_fps(
    source_fps: float | None,
    fps_low: float = 30.0,
    fps_high: float = 60.0,
    high_source_threshold: float = 55.0,
) -> float:
    """Picks the output frame rate from the MEASURED source rate (TASK 007).

    Priority 4: the measured source rate is PRESERVED — fractional NTSC
    rates included (23.976, 29.97, 59.94):

    - source rate unknown (``None``) → ``fps_low``: the legacy default,
      used only when the rate cannot be measured;
    - source rate known → the source rate itself, CAPPED at ``fps_high``:
      the output never exceeds the measured rate, so frames are never
      duplicated to fake a higher rate, and every rate inside the
      operating window (23.976 … 60) passes through unclamped. A source
      above ``fps_high`` (e.g. 120 fps) is bounded to it — an explicit,
      documented operational limit, applied only out-of-window.

    ``high_source_threshold`` is accepted for compatibility with the
    earlier two-tier design and the settings surface; it no longer affects
    the selection. Deterministic; the returned value is what ``-r`` is
    passed, so the rendered clip's fps is a measured fact (verified by
    ffprobe in tests), not a claim.
    """
    if source_fps is None:
        return float(fps_low)
    return min(source_fps, float(fps_high))


def format_frame_rate(fps: float) -> str:
    """Formats a frame rate for FFmpeg's ``-r`` option.

    Integral values render as plain integers (``30.0`` → ``"30"``);
    fractional NTSC rates keep their exact decimals (``59.94`` →
    ``"59.94"``, ``23.976`` → ``"23.976"``) — never a padded ``"30.0"``
    or a rounded ``"60"`` that would silently change the rate.
    """
    return f"{round(float(fps), 3):g}"


def _even_ceil(x: float) -> int:
    n = int(math.ceil(x))
    return n + 1 if n % 2 else n


def _even_floor(x: float) -> int:
    n = int(math.floor(x))
    return n - 1 if n % 2 else n


@dataclass(frozen=True)
class RenderProfile:
    """The fixed product output profile (codec/quality/compatibility)."""

    width: int = 1080
    height: int = 1920
    #: Default (fallback) output frame rate. Source-aware selection
    #: (``select_output_fps``) overrides this per clip (TASK 007).
    fps: int = 30
    video_codec: str = "libx264"
    crf: int = 20
    preset: str = "veryfast"
    pix_fmt: str = "yuv420p"
    audio_codec: str = "aac"
    audio_bitrate: str = "128k"
    mov_flags: str = "+faststart"
    #: "cover" (center crop, default) or "contain" (black pad).
    fit: str = "cover"

    def ffmpeg_video_args(self, fps: float | None = None) -> list[str]:
        """Encoder args. ``fps`` overrides the profile default with the
        source-preserved rate (TASK 007, Priority 4); omitted = profile
        ``fps``. Fractional rates are formatted exactly (``59.94``)."""
        effective = float(self.fps) if fps is None else float(fps)
        return [
            "-c:v", self.video_codec,
            "-preset", self.preset,
            "-crf", str(self.crf),
            "-pix_fmt", self.pix_fmt,
            "-r", format_frame_rate(effective),
        ]

    def ffmpeg_audio_args(self) -> list[str]:
        return ["-c:a", self.audio_codec, "-b:a", self.audio_bitrate]

    def ffmpeg_container_args(self) -> list[str]:
        return ["-movflags", self.mov_flags]

    def video_filter(self, source_width: int, source_height: int, rotation_degrees: float) -> str:
        return plan_video_filter(source_width, source_height, rotation_degrees,
                                 self.width, self.height, self.fit)
