"""Bounded FFmpeg frame extraction for facecam analysis (TASK 008).

Extracts a SMALL, hard-capped set of JPEG frames from one bounded
``-ss/-t`` window of the source (default 1 fps) into a caller-owned temp
directory. Nothing is ever decoded whole: the ``fps`` + ``frames`` filter
chain limits both the count and the rate, so RAM and disk usage are
bounded by ``sample_fps × chunk_seconds × max_frames_per_chunk``.

Frame times are the deterministic sampling grid
``t_i = offset + i / sample_fps`` (the ffmpeg ``fps`` filter picks the
source frame nearest each grid point — sub-sample jitter is intrinsic to
decimated sampling and is documented in the evidence, not hidden).
This function returns the CHUNK-RELATIVE component ``i / sample_fps``;
the caller (signals layer) applies ``offset`` exactly once.
"""

from __future__ import annotations

import asyncio
from pathlib import Path


class FrameExtractionError(RuntimeError):
    """The chunk's frames could not be extracted (safe message)."""


async def extract_sampled_frames(
    ffmpeg_path: str,
    source: Path,
    offset: float,
    length: float,
    sample_fps: float,
    max_frames: int,
    out_dir: Path,
    timeout_seconds: float,
) -> list[tuple[float, Path]]:
    """Extracts up to ``max_frames`` JPEGs at ``sample_fps`` from the window.

    Returns ``[(absolute_time_seconds, frame_path), ...]`` in time order.
    An empty window yields an empty list (no faces — not an error).
    """
    if not source.is_file() or source.stat().st_size == 0:
        raise FrameExtractionError("source file is missing or empty")
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = out_dir / "f_%05d.jpg"
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_path,
        "-hide_banner", "-nostdin", "-loglevel", "error",
        "-ss", f"{offset:.3f}", "-t", f"{length:.3f}", "-i", str(source),
        "-vf", f"fps={sample_fps:g}",
        "-frames:v", str(max_frames),
        "-q:v", "3", "-an", str(pattern),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise FrameExtractionError("frame extraction timed out") from None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode not in (0, 1):  # 1 = nothing matched (legit for faceless video)
        raise FrameExtractionError("frame extraction failed")

    frames: list[tuple[float, Path]] = []
    for path in sorted(out_dir.glob("f_*.jpg")):
        index = int(path.stem.split("_")[1]) - 1  # f_%05d is 1-based
        frames.append((index / sample_fps, path))  # chunk-RELATIVE
    return frames
