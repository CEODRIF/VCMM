"""Output contract validation tests (real FFmpeg + FFprobe binaries).

Every rendered file is re-probed as raw bytes and must satisfy: MP4
container, H.264 video, EXACTLY 1080x1920, yuv420p, AAC audio, 0 stored
rotation, duration within tolerance. Any violation is a hard error — a
non-conforming clip must never be uploaded or reported ready.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.queue.models import ERROR_RENDER_INVALID
from app.rendering.profiles import RenderProfile
from app.rendering.validation import RenderValidationError, probe_render_output, verify_render_output
from conftest import _run_ffmpeg, patch_mp4_rotation


def run(coro):
    return asyncio.run(coro)


PROFILE = RenderProfile()


def compliant_output(ffmpeg_bin: str, source: Path, out: Path, duration: float = 1.0) -> None:
    """Produces a file that satisfies the full output contract."""
    _run_ffmpeg(
        ffmpeg_bin,
        ["-ss", "0", "-t", str(duration), "-autorotate", "-i", str(source),
         "-vf", "scale=1080:1920,crop=1080:1920",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)],
    )


def verify(ffprobe_bin: str, out: Path, expected_duration: float) -> object:
    return run(verify_render_output(
        ffprobe_bin, out, PROFILE,
        expected_duration=expected_duration, timeout_seconds=30.0,
    ))


def test_compliant_output_passes_the_full_contract(ffmpeg_bin, ffprobe_bin, render_media, tmp_path):
    out = tmp_path / "ok.mp4"
    compliant_output(ffmpeg_bin, render_media["landscape"], out, duration=1.0)
    info = verify(ffprobe_bin, out, expected_duration=1.0)
    assert "mp4" in info.container
    assert info.video_codec == "h264"
    assert (info.width, info.height) == (1080, 1920)
    assert info.pix_fmt == "yuv420p"
    assert info.audio_codec == "aac"
    assert abs(info.rotation) <= 0.5
    assert abs(info.duration_seconds - 1.0) <= 0.15
    assert info.file_size_bytes > 0


def test_wrong_resolution_rejected(ffmpeg_bin, ffprobe_bin, render_media, tmp_path):
    out = tmp_path / "res.mp4"
    _run_ffmpeg(
        ffmpeg_bin,
        ["-ss", "0", "-t", "1", "-i", str(render_media["landscape"]),
         "-vf", "scale=640:360",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-an",
         str(out)],
    )
    with pytest.raises(RenderValidationError) as exc:
        verify(ffprobe_bin, out, expected_duration=1.0)
    assert "640x360" in exc.value.message
    assert exc.value.category == ERROR_RENDER_INVALID


def test_wrong_video_codec_rejected(ffmpeg_bin, ffprobe_bin, render_media, tmp_path):
    out = tmp_path / "codec.mp4"
    _run_ffmpeg(
        ffmpeg_bin,
        ["-ss", "0", "-t", "1", "-i", str(render_media["landscape"]),
         "-vf", "scale=1080:1920",
         "-c:v", "mpeg4", "-pix_fmt", "yuv420p", "-an",
         str(out)],
    )
    with pytest.raises(RenderValidationError) as exc:
        verify(ffprobe_bin, out, expected_duration=1.0)
    assert "H.264" in exc.value.message


def test_wrong_pixel_format_rejected(ffmpeg_bin, ffprobe_bin, render_media, tmp_path):
    out = tmp_path / "pix.mp4"
    _run_ffmpeg(
        ffmpeg_bin,
        ["-ss", "0", "-t", "1", "-i", str(render_media["landscape"]),
         "-vf", "scale=1080:1920,format=yuv444p",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv444p", "-an",
         "-c:a", "aac", str(out)],
    )
    with pytest.raises(RenderValidationError) as exc:
        verify(ffprobe_bin, out, expected_duration=1.0)
    assert "yuv444p" in exc.value.message


def test_missing_audio_rejected(ffmpeg_bin, ffprobe_bin, render_media, tmp_path):
    # Parked sources always carry audio (analysis requires it) — an output
    # without an AAC track is therefore a contract violation.
    out = tmp_path / "noaudio.mp4"
    _run_ffmpeg(
        ffmpeg_bin,
        ["-ss", "0", "-t", "1", "-i", str(render_media["landscape"]),
         "-vf", "scale=1080:1920",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-an", str(out)],
    )
    with pytest.raises(RenderValidationError) as exc:
        verify(ffprobe_bin, out, expected_duration=1.0)
    assert "AAC" in exc.value.message


def test_wrong_container_rejected(ffmpeg_bin, ffprobe_bin, render_media, tmp_path):
    out = tmp_path / "other.mkv"
    _run_ffmpeg(
        ffmpeg_bin,
        ["-ss", "0", "-t", "1", "-i", str(render_media["landscape"]),
         "-vf", "scale=1080:1920",
         "-c:v", "mpeg4", "-pix_fmt", "yuv420p", "-an",
         "-f", "matroska", str(out)],
    )
    with pytest.raises(RenderValidationError) as exc:
        verify(ffprobe_bin, out, expected_duration=1.0)
    assert "MP4" in exc.value.message


def test_output_still_rotated_rejected(ffmpeg_bin, ffprobe_bin, render_media, tmp_path):
    # Bake a rotation tag into a compliant file: the output must store 0.
    # (No +faststart here: the rot-box patcher requires moov to be last.)
    ok = tmp_path / "ok.mp4"
    _run_ffmpeg(
        ffmpeg_bin,
        ["-ss", "0", "-t", "1", "-autorotate", "-i", str(render_media["landscape"]),
         "-vf", "scale=1080:1920,crop=1080:1920",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30",
         "-c:a", "aac", "-b:a", "128k", str(ok)],
    )
    patch_mp4_rotation(ok, 90)
    with pytest.raises(RenderValidationError) as exc:
        verify(ffprobe_bin, ok, expected_duration=1.0)
    assert "rotation" in exc.value.message


def test_duration_deviation_rejected(ffmpeg_bin, ffprobe_bin, render_media, tmp_path):
    out = tmp_path / "short.mp4"
    compliant_output(ffmpeg_bin, render_media["landscape"], out, duration=1.0)
    with pytest.raises(RenderValidationError) as exc:
        verify(ffprobe_bin, out, expected_duration=5.0)  # claim 5s, measure 1s
    assert "duration" in exc.value.message.lower()


def test_duration_within_tolerance_accepted(ffmpeg_bin, ffprobe_bin, render_media, tmp_path):
    out = tmp_path / "short.mp4"
    compliant_output(ffmpeg_bin, render_media["landscape"], out, duration=1.0)
    # 0.9s expected vs ~1.0s measured: inside the 15% tolerance.
    assert verify(ffprobe_bin, out, expected_duration=0.9) is not None


def test_corrupt_file_rejected(ffprobe_bin, tmp_path):
    bad = tmp_path / "corrupt.mp4"
    bad.write_bytes(b"not really an mp4 " * 32)
    with pytest.raises(RenderValidationError):
        verify(ffprobe_bin, bad, expected_duration=1.0)


def test_missing_or_empty_file_rejected(ffprobe_bin, tmp_path):
    with pytest.raises(RenderValidationError):
        verify(ffprobe_bin, tmp_path / "missing.mp4", expected_duration=1.0)
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(RenderValidationError):
        verify(ffprobe_bin, empty, expected_duration=1.0)


def test_probe_returns_measured_facts(ffmpeg_bin, ffprobe_bin, render_media, tmp_path):
    out = tmp_path / "ok.mp4"
    compliant_output(ffmpeg_bin, render_media["landscape"], out, duration=1.0)
    info = run(probe_render_output(ffprobe_bin, out, 30.0))
    assert info.width == 1080
    assert info.height == 1920
    assert info.video_codec == "h264"
    assert info.audio_codec == "aac"
    assert info.rotation == 0.0
