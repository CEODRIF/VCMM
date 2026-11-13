"""FFmpeg subprocess abstraction for rendering (Phase 5 foundation).

One small, safe wrapper shared by the renderer:

- a hard wall-clock timeout (the process is killed and waited on),
- guaranteed cleanup of the partial output on every failure path,
- safe error mapping (no FFmpeg stderr internals in user-facing messages),
- cancellation-safe: the process is killed when the task is cancelled.

Callers own the argument list (see :mod:`app.rendering.profiles`) and the
input/output validation; this module owns the process lifecycle only.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..queue.models import ERROR_RENDER_FAILED


class FFmpegError(RuntimeError):
    """FFmpeg failed for a specific, safe reason."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


async def run_ffmpeg(
    ffmpeg_path: str,
    args: list[str],
    timeout_seconds: float,
    output_file: Path | None = None,
) -> None:
    """Runs ``ffmpeg -hide_banner -loglevel error -y <args>``.

    Raises :class:`FFmpegError` on timeout, non-zero exit, or a missing/
    empty output file (the partial file is removed first).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg_path,
            "-hide_banner", "-loglevel", "error", "-y", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        if output_file is not None:
            output_file.unlink(missing_ok=True)
        raise FFmpegError(ERROR_RENDER_FAILED, "the rendering engine could not be started") from None
    timed_out = False
    try:
        try:
            await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            timed_out = True
    except asyncio.CancelledError:
        proc.kill()
        try:
            await proc.wait()
        except asyncio.CancelledError:
            pass
        if output_file is not None:
            output_file.unlink(missing_ok=True)
        raise
    finally:
        # Guarantee the child cannot outlive this call.
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    if timed_out:
        if output_file is not None:
            output_file.unlink(missing_ok=True)
        raise FFmpegError(ERROR_RENDER_FAILED, "Rendering timed out and was cancelled.")

    if proc.returncode != 0:
        if output_file is not None:
            output_file.unlink(missing_ok=True)
        raise FFmpegError(ERROR_RENDER_FAILED, "Rendering failed; the clip could not be produced.")

    if output_file is not None:
        if not output_file.is_file() or output_file.stat().st_size == 0:
            output_file.unlink(missing_ok=True)
            raise FFmpegError(ERROR_RENDER_FAILED, "Rendering produced an empty file.")
        # Keep the output on success — the caller owns it.
