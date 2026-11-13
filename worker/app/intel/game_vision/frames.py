"""Bounded FFmpeg frame extraction for game-vision analysis (TASK 009).

Same bounded pattern as facecam (TASK 008) — a SMALL, hard-capped set of
JPEG frames from one bounded ``-ss/-t`` window; the VOD is NEVER decoded
whole — plus two game-vision extensions:

- ``roi`` — an OPTIONAL region of interest (normalized x, y, w, h)
  applied with FFmpeg's ``crop`` filter INSIDE the decode (only the ROI
  is written to disk — OCR cost stays bounded on 4K sources);
- ``max_dim`` — an OPTIONAL ``scale`` cap so images never exceed the
  configured longest side.

Frame times are the deterministic sampling grid
``t_i = offset + i / sample_fps`` (the ``fps`` filter picks the source
frame nearest each grid point — sub-sample jitter is intrinsic to
decimated sampling and is documented in the evidence, not hidden).
Returns the CHUNK-RELATIVE component ``i / sample_fps``; the caller
applies ``offset`` exactly once.
"""

from __future__ import annotations

import asyncio
from pathlib import Path


class FrameExtractionError(RuntimeError):
    """The chunk's frames could not be extracted (safe message)."""


def _vf_chain(sample_fps: float, roi: tuple[float, float, float, float] | None,
              max_dim: int | None) -> str:
    chain = [f"fps={sample_fps:g}"]
    if roi is not None:
        x, y, w, h = roi
        # crop=w:h:x:y expressed in source pixels (rounded to int).
        chain.append(f"crop=iw*{w:g}:ih*{h:g}:iw*{x:g}:ih*{y:g}")
    if max_dim is not None:
        chain.append(f"scale='min(iw,{int(max_dim)})':'min(ih,{int(max_dim)})'")
    return ",".join(chain)


async def extract_roi_frames(
    ffmpeg_path: str,
    source: Path,
    offset: float,
    length: float,
    sample_fps: float,
    max_frames: int,
    out_dir: Path,
    timeout_seconds: float,
    roi: tuple[float, float, float, float] | None = None,
    max_dim: int | None = None,
) -> list[tuple[float, Path]]:
    """Extracts up to ``max_frames`` JPEGs at ``sample_fps`` from the
    window, optionally cropped to ``roi`` and capped at ``max_dim``.

    Returns ``[(chunk_relative_time_seconds, frame_path), ...]`` in time
    order. An empty window yields an empty list (not an error).
    """
    if not source.is_file() or source.stat().st_size == 0:
        raise FrameExtractionError("source file is missing or empty")
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = out_dir / "f_%05d.jpg"
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_path,
        "-hide_banner", "-nostdin", "-loglevel", "error",
        "-ss", f"{offset:.3f}", "-t", f"{length:.3f}", "-i", str(source),
        "-vf", _vf_chain(sample_fps, roi, max_dim),
        "-frames:v", str(max_frames),
        "-q:v", "3", "-an", str(pattern),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, _stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise FrameExtractionError("frame extraction timed out") from None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode not in (0, 1):  # 1 = nothing matched (legit)
        raise FrameExtractionError("frame extraction failed")

    frames: list[tuple[float, Path]] = []
    for path in sorted(out_dir.glob("f_*.jpg")):
        index = int(path.stem.split("_")[1]) - 1  # f_%05d is 1-based
        frames.append((index / sample_fps, path))  # chunk-RELATIVE
    return frames
