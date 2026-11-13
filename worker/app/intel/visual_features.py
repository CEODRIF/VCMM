"""Bounded visual feature extraction (TASK 007) — FFmpeg-only, zero ML deps.

Two DECIMATED passes over one bounded ``-ss/-t`` range of the source
(null muxer — nothing is written to disk, frames are never accumulated):

1. **Scene changes** — ``select='gt(scene,T)',showinfo`` reports the
   pts_time of every measured cut above threshold ``T``.
2. **Motion** — ``tblend=all_mode=difference`` + ``signalstats`` +
   ``metadata=print`` yields the per-frame (at 2 fps) mean absolute
   frame difference (YAVG): a cheap, deterministic motion measure that
   catches sudden gameplay/camera changes without any model.

Facecam presence/expression and HUD/OCR are deliberately NOT implemented
here (see docs/stream-intelligence.md §5): their interfaces exist in
``detectors.py`` consumers as absent evidence, never as fabricated data.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("clipforge.intel.visual")

#: Frames per second for the decimated passes (bounded CPU + memory).
PASS_FPS = 2
#: ffmpeg scene-score threshold for a "cut".
DEFAULT_SCENE_THRESHOLD = 0.3
#: Motion (frame-difference YAVG, 0..1) baseline multiplier for a burst.
MOTION_BURST_RATIO = 2.5
MOTION_BURST_MIN = 0.03  # absolute floor (0.5 on the 0..1 scale)


class VisualFeatureError(RuntimeError):
    """The chunk's video could not be analyzed (safe, no internals)."""


@dataclass
class VisualChunkFeatures:
    """In-memory visual features for one chunk (absolute timeline)."""

    offset: float
    duration: float
    scene_changes: list[float]                    # ABSOLUTE seconds
    motion: list[tuple[float, float]]             # (ABSOLUTE seconds, strength 0..1)
    motion_peaks: list[tuple[float, float]]       # ABSOLUTE (t, strength) bursts

    def storage_summary(self) -> dict[str, Any]:
        """Compact 1 Hz-ish summary — the ONLY thing persisted per chunk."""
        return {
            "family": "visual",
            "offset": round(self.offset, 3),
            "duration": round(self.duration, 3),
            "scene_changes": [round(t, 3) for t in self.scene_changes],
            "motion_peaks": [[round(t, 3), round(s, 4)] for t, s in self.motion_peaks],
        }


_SCENES_RE = re.compile(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)")
_YAVG_RE = re.compile(r"lavfi\.signalstats\.YAVG=([0-9]+(?:\.[0-9]+)?)")
_LAVFI_PTS_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


async def _run(ffmpeg_path: str, args: list[str], timeout_seconds: float) -> str:
    """Runs an FFmpeg metadata pass (null muxer) and returns stderr/stdout."""
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_path, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise VisualFeatureError("visual pass timed out") from None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode not in (0, 1):  # 1 = no matching frames selected (legit)
        raise VisualFeatureError("visual pass failed")
    return (stdout or b"").decode("utf-8", "replace") + "\n" + (stderr or b"").decode("utf-8", "replace")


async def _scene_pass(ffmpeg_path: str, source: Path, offset: float, length: float,
                      threshold: float, timeout_seconds: float) -> list[float]:
    text = await _run(
        ffmpeg_path,
        [
            "-hide_banner", "-nostdin",
            "-ss", f"{offset:.3f}", "-t", f"{length:.3f}", "-i", str(source),
            "-vf", f"fps={PASS_FPS},select='gt(scene,{threshold})',showinfo",
            "-an", "-f", "null", "-",
        ],
        timeout_seconds,
    )
    times = []
    for line in text.splitlines():
        m = _SCENES_RE.search(line)
        if m and "Parsed_showinfo" in line:
            times.append(offset + float(m.group(1)))
    return sorted(times)


async def _motion_pass(ffmpeg_path: str, source: Path, offset: float, length: float,
                       timeout_seconds: float) -> list[tuple[float, float]]:
    text = await _run(
        ffmpeg_path,
        [
            "-hide_banner", "-nostdin",
            "-ss", f"{offset:.3f}", "-t", f"{length:.3f}", "-i", str(source),
            "-vf", f"fps={PASS_FPS},tblend=all_mode=difference,signalstats,metadata=print:file=-",
            "-an", "-f", "null", "-",
        ],
        timeout_seconds,
    )
    samples: list[tuple[float, float]] = []
    current_pts: float | None = None
    for line in text.splitlines():
        m = _LAVFI_PTS_RE.search(line)
        if m:
            current_pts = float(m.group(1))
        y = _YAVG_RE.search(line)
        if y and current_pts is not None:
            samples.append((offset + current_pts, float(y.group(1)) / 255.0))
    return samples


def _motion_peaks(samples: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Local maxima above ratio × median (with an absolute floor)."""
    if len(samples) < 5:
        return []
    values = [v for _, v in samples]
    values_sorted = sorted(values)
    median = values_sorted[len(values_sorted) // 2]
    baseline = max(median, 1e-6)
    peaks: list[tuple[float, float]] = []
    for i in range(1, len(samples) - 1):
        t, v = samples[i]
        if v >= MOTION_BURST_MIN and v >= MOTION_BURST_RATIO * baseline and v > samples[i - 1][1] and v >= samples[i + 1][1]:
            peaks.append((t, min(1.0, v / 0.25)))
    return peaks


async def extract_visual_features(
    ffmpeg_path: str,
    source: Path,
    offset: float,
    length: float,
    timeout_seconds: float,
    scene_threshold: float = DEFAULT_SCENE_THRESHOLD,
) -> VisualChunkFeatures:
    """Runs both bounded passes and returns chunk-local visual features."""
    if not source.is_file() or source.stat().st_size == 0:
        raise VisualFeatureError("source file is missing or empty")
    scenes = await _scene_pass(ffmpeg_path, source, offset, length, scene_threshold, timeout_seconds)
    motion = await _motion_pass(ffmpeg_path, source, offset, length, timeout_seconds)
    return VisualChunkFeatures(
        offset=offset,
        duration=length,
        scene_changes=scenes,
        motion=motion,
        motion_peaks=_motion_peaks(motion),
    )
