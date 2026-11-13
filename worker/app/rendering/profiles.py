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

    def ffmpeg_video_args(self) -> list[str]:
        return [
            "-c:v", self.video_codec,
            "-preset", self.preset,
            "-crf", str(self.crf),
            "-pix_fmt", self.pix_fmt,
            "-r", str(self.fps),
        ]

    def ffmpeg_audio_args(self) -> list[str]:
        return ["-c:a", self.audio_codec, "-b:a", self.audio_bitrate]

    def ffmpeg_container_args(self) -> list[str]:
        return ["-movflags", self.mov_flags]

    def video_filter(self, source_width: int, source_height: int, rotation_degrees: float) -> str:
        return plan_video_filter(source_width, source_height, rotation_degrees,
                                 self.width, self.height, self.fit)
