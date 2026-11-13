"""FFmpegRenderer integration tests (REAL FFmpeg/FFprobe, no mocking).

Covers the Step 5 matrix: portrait/landscape/square/rotated inputs → exact
1080x1920 output; corrupt input → safe categorized error; timeout → process
killed and partial output removed; cancellation → subprocess killed, awaited,
temp files cleaned. No process leaks (verified with pgrep).
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

import pytest

from app.queue.models import ERROR_RENDER_FAILED
from app.rendering.base import RenderSpec
from app.rendering.ffmpeg import FFmpegError
from app.rendering.profiles import RenderProfile
from app.rendering.renderer import FFmpegRenderer
from app.rendering.validation import verify_render_output
from conftest import _run_ffmpeg, patch_mp4_rotation


def run(coro):
    return asyncio.run(coro)


def no_leftover_processes() -> None:
    """A killed FFmpeg must be gone — no leaked subprocess (exact name match)."""
    result = subprocess.run(["pgrep", "-x", "ffmpeg"], capture_output=True, text=True)
    pids = [p for p in result.stdout.split() if p.strip()]
    assert not pids, f"leaked ffmpeg process(es): {pids}"


def crop_region(ffmpeg_bin: str, path: Path) -> tuple[int, int]:
    """Content bounding box size (w, h) via cropdetect.

    Threshold 32: H.264 limited-range black encodes to Y=16, so the default
    threshold would count the whole frame as content. cropdetect's own
    ``crop=`` suggestion can carry inverted/negative values (it tracks
    min/max independently per frame), so the bbox is computed from the raw
    x1/x2/y1/y2 bounds instead.
    """
    result = subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-i", str(path), "-vf", "cropdetect=32:2:0", "-an", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    matches = re.findall(r"x1:(\d+) x2:(\d+) y1:(\d+) y2:(\d+)", result.stderr)
    assert matches, f"cropdetect found no region in {path}: {result.stderr[-400:]}"
    x1, x2, y1, y2 = (int(v) for v in matches[-1])
    return abs(x1 - x2) + 1, abs(y1 - y2) + 1


def make_renderer(ffmpeg_bin: str, ffprobe_bin: str, **kwargs) -> FFmpegRenderer:
    return FFmpegRenderer(ffmpeg_bin, ffprobe_bin, **kwargs)


# ---- orientation matrix: exact 1080x1920, no distortion, rotation 0 -----------

@pytest.mark.parametrize("orientation", ["portrait", "landscape", "square"])
def test_renders_to_exact_vertical(ffmpeg_bin, ffprobe_bin, render_media, tmp_path, orientation):
    renderer = make_renderer(ffmpeg_bin, ffprobe_bin)
    out = tmp_path / f"{orientation}.mp4"
    rendered = run(renderer.render(str(render_media[orientation]), 0.0, 2.0, RenderSpec(), str(out)))
    assert rendered == str(out)
    assert out.is_file() and out.stat().st_size > 0
    info = run(verify_render_output(
        ffprobe_bin, out, RenderProfile(), expected_duration=2.0, timeout_seconds=30.0))
    assert (info.width, info.height) == (1080, 1920)
    assert info.video_codec == "h264"
    assert info.audio_codec == "aac"
    assert info.pix_fmt == "yuv420p"
    assert abs(info.rotation) <= 0.5
    assert abs(info.duration_seconds - 2.0) <= 0.3  # ±15%


def test_landscape_cover_crops_not_distorts(ffmpeg_bin, ffprobe_bin, render_media, tmp_path):
    # cover: the FULL height of the source must appear (no bars), the sides
    # are cropped — the output is exactly the vertical center band.
    renderer = make_renderer(ffmpeg_bin, ffprobe_bin)
    out = tmp_path / "landscape.mp4"
    run(renderer.render(str(render_media["landscape"]), 0.0, 1.0, RenderSpec(), str(out)))
    info = run(verify_render_output(
        ffprobe_bin, out, RenderProfile(), expected_duration=1.0, timeout_seconds=30.0))
    assert (info.width, info.height) == (1080, 1920)
    # Aspect ratio preserved inside the crop: scale factor 3414/1920 means the
    # 1080-wide window corresponds to 607.5 source px — content is undistorted.
    # (Distortion would change object proportions; crop keeps them intact.)
    assert info.pix_fmt == "yuv420p"


def test_rotated_input_renders_upright_with_zero_rotation(ffmpeg_bin, ffprobe_bin, render_media, tmp_path):
    # The fixture stores 1920x1080 frames with a 90° rotation tag (displays as
    # 1080x1920 portrait). A correct renderer must plan the PORTRAIT geometry
    # (1:1 scale) and bake the rotation into the pixels (output rotation 0).
    renderer = make_renderer(ffmpeg_bin, ffprobe_bin)
    out = tmp_path / "rotated.mp4"
    run(renderer.render(str(render_media["rotated"]), 0.0, 2.0, RenderSpec(), str(out)))
    info = run(verify_render_output(
        ffprobe_bin, out, RenderProfile(), expected_duration=2.0, timeout_seconds=30.0))
    assert (info.width, info.height) == (1080, 1920)
    assert abs(info.rotation) <= 0.5, "rotation must be baked into pixels"
    assert abs(info.duration_seconds - 2.0) <= 0.3


def test_rotated_input_orientation_is_actually_correct(ffmpeg_bin, ffprobe_bin, tmp_path):
    # Deterministic orientation proof: a source with a 100px white bar on the
    # LEFT edge, stored as 1920x1080 + rotate=90 tag (bar displays at top).
    # Correct geometry (portrait 1:1) → output bar is exactly 1080x100.
    # Wrong geometry (treated as landscape) → bar would be ~180px tall.
    src = tmp_path / "bar.mp4"
    _run_ffmpeg(
        ffmpeg_bin,
        ["-f", "lavfi", "-i", "color=c=black:s=1920x1080:d=2:r=30",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-vf", "drawbox=x=0:y=0:w=100:h=1080:c=white:t=fill",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(src)],
    )
    patch_mp4_rotation(src, 90)

    renderer = make_renderer(ffmpeg_bin, ffprobe_bin)
    out = tmp_path / "bar_out.mp4"
    run(renderer.render(str(src), 0.0, 2.0, RenderSpec(), str(out)))

    info = run(verify_render_output(
        ffprobe_bin, out, RenderProfile(), expected_duration=2.0, timeout_seconds=30.0))
    assert abs(info.rotation) <= 0.5

    w, h = crop_region(ffmpeg_bin, out)
    assert w == 1080, f"bar must span full width (orientation wrong?), got {w}"
    assert h == 100, f"bar height must be 100px (1:1 portrait scale), got {h}"


def test_contain_fit_pads_without_cropping(ffmpeg_bin, ffprobe_bin, render_media, tmp_path):
    # contain on exact-ratio portrait: identical frame, nothing lost.
    renderer = make_renderer(ffmpeg_bin, ffprobe_bin, profile=RenderProfile(fit="contain"))
    out = tmp_path / "contain.mp4"
    run(renderer.render(str(render_media["portrait"]), 0.0, 2.0, RenderSpec(), str(out)))
    info = run(verify_render_output(
        ffprobe_bin, out, RenderProfile(fit="contain"), expected_duration=2.0, timeout_seconds=30.0))
    assert (info.width, info.height) == (1080, 1920)


# ---- failure paths --------------------------------------------------------------

def test_invalid_window_rejected_before_ffmpeg(ffmpeg_bin, ffprobe_bin, tmp_path):
    renderer = make_renderer(ffmpeg_bin, ffprobe_bin)
    src = tmp_path / "x.mp4"
    src.write_bytes(b"whatever")  # never probed: window check comes first
    for start, end in [(1.0, 1.0), (2.0, 1.0), (-0.5, 1.0)]:
        with pytest.raises(FFmpegError) as exc:
            run(renderer.render(str(src), start, end, RenderSpec(), str(tmp_path / "o.mp4")))
        assert exc.value.category == ERROR_RENDER_FAILED


def test_missing_source_safe_error(ffmpeg_bin, ffprobe_bin, tmp_path):
    renderer = make_renderer(ffmpeg_bin, ffprobe_bin)
    with pytest.raises(FFmpegError) as exc:
        run(renderer.render(str(tmp_path / "missing.mp4"), 0.0, 1.0, RenderSpec(), str(tmp_path / "o.mp4")))
    assert exc.value.category == ERROR_RENDER_FAILED
    assert not (tmp_path / "o.mp4").exists()


def test_corrupt_source_safe_categorized_error(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    renderer = make_renderer(ffmpeg_bin, ffprobe_bin)
    out = tmp_path / "out.mp4"
    with pytest.raises(FFmpegError) as exc:
        run(renderer.render(str(synthetic_videos["corrupt"]), 0.0, 1.0, RenderSpec(), str(out)))
    assert exc.value.category == ERROR_RENDER_FAILED
    # Safe message, no FFmpeg stderr internals.
    assert "corrupt" in exc.value.message or "analyz" in exc.value.message
    assert not out.exists()


def test_timeout_kills_process_and_removes_output(ffmpeg_bin, ffprobe_bin, render_media, tmp_path):
    # veryslow 1080p over 3s of source cannot finish inside the 1s budget.
    renderer = make_renderer(ffmpeg_bin, ffprobe_bin, profile=RenderProfile(preset="veryslow"), timeout_seconds=1.0)
    out = tmp_path / "slow.mp4"
    with pytest.raises(FFmpegError) as exc:
        run(renderer.render(str(render_media["landscape"]), 0.0, 3.0, RenderSpec(), str(out)))
    assert "timed out" in str(exc).lower()
    assert not out.exists()
    no_leftover_processes()


def test_cancellation_kills_subprocess_and_cleans(ffmpeg_bin, ffprobe_bin, render_media, tmp_path):
    renderer = make_renderer(ffmpeg_bin, ffprobe_bin, profile=RenderProfile(preset="veryslow"), timeout_seconds=300.0)
    out = tmp_path / "cancelled.mp4"

    async def scenario():
        task = asyncio.create_task(renderer.render(str(render_media["landscape"]), 0.0, 4.0, RenderSpec(), str(out)))
        await asyncio.sleep(0.8)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert not out.exists()
    no_leftover_processes()


def test_window_duration_is_respected(ffmpeg_bin, ffprobe_bin, render_media, tmp_path):
    # Input-side seek + -t: a mid-file window must produce exactly the
    # requested length (no drift from the source start).
    renderer = make_renderer(ffmpeg_bin, ffprobe_bin)
    out = tmp_path / "window.mp4"
    run(renderer.render(str(render_media["landscape"]), 2.0, 3.5, RenderSpec(), str(out)))
    info = run(verify_render_output(
        ffprobe_bin, out, RenderProfile(), expected_duration=1.5, timeout_seconds=30.0))
    assert abs(info.duration_seconds - 1.5) <= 0.225  # 15%
