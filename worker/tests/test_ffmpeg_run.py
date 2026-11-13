"""FFmpeg subprocess abstraction tests (real binary).

Contract: hard wall-clock timeout (process killed), guaranteed removal of
the partial output on EVERY failure path, safe error mapping (no FFmpeg
stderr internals), and cancellation safety.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.queue.models import ERROR_RENDER_FAILED
from app.rendering.ffmpeg import FFmpegError, run_ffmpeg


def run(coro):
    return asyncio.run(coro)


def test_success_keeps_the_output(ffmpeg_bin, synthetic_videos, tmp_path):
    out = tmp_path / "copy.mp4"
    run(run_ffmpeg(
        ffmpeg_bin,
        ["-i", str(synthetic_videos["valid"]), "-c", "copy", str(out)],
        timeout_seconds=60.0,
        output_file=out,
    ))
    assert out.is_file()
    assert out.stat().st_size > 0


def test_nonzero_exit_raises_safe_error_and_removes_output(ffmpeg_bin, tmp_path):
    # A bogus filter makes FFmpeg exit non-zero before any output is written.
    out = tmp_path / "bad.mp4"
    out.write_bytes(b"partial")  # simulate a partial file that must be cleaned
    with pytest.raises(FFmpegError) as exc:
        run(run_ffmpeg(
            ffmpeg_bin,
            ["-f", "lavfi", "-i", "testsrc=duration=0.1:size=64x64:rate=10",
             "-vf", "totally_bogus_filter=oops", str(out)],
            timeout_seconds=60.0,
            output_file=out,
        ))
    assert exc.value.category == ERROR_RENDER_FAILED
    # Safe message: no FFmpeg stderr dump, no filter name leak.
    assert str(exc.value) == "Rendering failed; the clip could not be produced."
    assert "totally_bogus_filter" not in str(exc.value)
    assert not out.exists()


def test_missing_binary_raises_ffmpeg_error(ffmpeg_bin, tmp_path):
    out = tmp_path / "x.mp4"
    with pytest.raises(FFmpegError):
        run(run_ffmpeg(
            "/nonexistent/ffmpeg",
            ["-i", "whatever", str(out)],
            timeout_seconds=10.0,
            output_file=out,
        ))
    assert not out.exists()


def test_timeout_kills_process_and_removes_output(ffmpeg_bin, tmp_path):
    # 600s of source encoded with a very slow preset: cannot finish in 1s.
    out = tmp_path / "slow.mp4"
    with pytest.raises(FFmpegError) as exc:
        run(run_ffmpeg(
            ffmpeg_bin,
            ["-f", "lavfi", "-i", "testsrc=duration=600:size=320x240:rate=10",
             "-c:v", "libx264", "-preset", "veryslow", "-an", str(out)],
            timeout_seconds=1.0,
            output_file=out,
        ))
    assert "timed out" in str(exc.value).lower()
    assert exc.value.category == ERROR_RENDER_FAILED
    assert not out.exists()


def test_cancellation_kills_process_and_removes_output(ffmpeg_bin, tmp_path):
    out = tmp_path / "cancelled.mp4"

    async def scenario():
        task = asyncio.create_task(run_ffmpeg(
            ffmpeg_bin,
            ["-f", "lavfi", "-i", "testsrc=duration=600:size=320x240:rate=10",
             "-c:v", "libx264", "-preset", "veryslow", "-an", str(out)],
            timeout_seconds=300.0,
            output_file=out,
        ))
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert not out.exists()


def test_no_output_file_argument_still_runs(ffmpeg_bin, tmp_path):
    # Without output_file the wrapper only manages the process lifecycle.
    out = tmp_path / "direct.mp4"
    run(run_ffmpeg(
        ffmpeg_bin,
        ["-f", "lavfi", "-i", "testsrc=duration=0.1:size=64x64:rate=10",
         "-an", str(out)],
        timeout_seconds=60.0,
    ))
    assert out.is_file()
