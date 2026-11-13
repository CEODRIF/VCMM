"""Facecam intelligence (TASK 008) — local-first, optional modality.

Public API:

- ``FacecamConfig``          — validated per-job configuration;
- ``FacecamChunkReport``     — one chunk's measured facecam state;
- ``FacecamAnalyzer``        — the interface the pipeline consumes;
- ``NullFacecamAnalyzer``    — the honest fallback (disabled/unconfigured);
- ``MediaPipeFacecamAnalyzer`` — the real detector (MediaPipe Tasks, CPU);
- ``build_facecam_analyzer`` — settings → analyzer (never raises).

Honesty contract: a missing dependency, missing/unreadable model, or
extraction failure ALWAYS yields an ``available=False`` report with the
reason recorded — the pipeline continues on audio/visual evidence and no
facecam event is ever fabricated.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .config import FacecamConfig
from .frames import FrameExtractionError, extract_sampled_frames
from .model import FacecamModelError, MediaPipeFacecamModel
from .region import FacecamRegion, FaceMeasure, FrameObservation, detect_facecam_region
from .signals import detect_facecam_events

logger = logging.getLogger("clipforge.intel.facecam")

__all__ = [
    "FacecamCancelled",
    "FacecamChunkReport",
    "FacecamConfig",
    "FacecamRegion",
    "FaceMeasure",
    "FrameObservation",
    "FacecamAnalyzer",
    "NullFacecamAnalyzer",
    "MediaPipeFacecamAnalyzer",
    "build_facecam_analyzer",
    "summarize_facecam",
    "detect_facecam_events",
]


class FacecamCancelled(Exception):
    """A stop was requested (shutdown/lease loss) mid-facecam-work.

    Defined here (NOT imported from ``pipeline``) to keep the dependency
    one-way: pipeline → facecam. The pipeline maps this to
    ``PipelineCancelled`` so the existing no-write cancellation contract
    applies unchanged.
    """


@dataclass
class FacecamChunkReport:
    """One chunk's measured facecam state (never fabricated)."""

    offset: float
    duration: float
    available: bool
    unavailable_reason: str | None
    sample_fps: float
    model: str | None
    frames_analyzed: int
    frames_without_face: int
    observations: list[FrameObservation] = field(default_factory=list)
    region: FacecamRegion | None = None
    presence_ratio: float = 0.0
    inference_ms_total: float = 0.0

    def storage_summary(self) -> dict[str, Any]:
        """Compact per-chunk summary — persisted in the checkpoint row."""
        return {
            "family": "facecam",
            "offset": round(self.offset, 3),
            "duration": round(self.duration, 3),
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "model": self.model,
            "sample_fps": self.sample_fps,
            "frames_analyzed": self.frames_analyzed,
            "presence_ratio": round(self.presence_ratio, 4),
            "inference_ms_total": round(self.inference_ms_total, 1),
            "region": (
                {
                    "bbox": list(self.region.bbox),
                    "presence_ratio": self.region.presence_ratio,
                    "bbox_iou": self.region.bbox_iou,
                    "area_ratio": self.region.area_ratio,
                }
                if self.region else None
            ),
        }


def unavailable_report(
    offset: float, duration: float, sample_fps: float, reason: str
) -> FacecamChunkReport:
    return FacecamChunkReport(
        offset=offset, duration=duration, available=False, unavailable_reason=reason,
        sample_fps=sample_fps, model=None, frames_analyzed=0, frames_without_face=0,
    )


class FacecamAnalyzer:
    """Interface the pipeline consumes. ``available`` is checked BEFORE any
    chunk work; a not-available analyzer costs nothing and changes nothing."""

    name: str = "base"

    def __init__(self) -> None:
        self.available = False
        self.unavailable_reason: str | None = None

    async def analyze_chunk(
        self,
        ffmpeg_path: str,
        source: Path,
        offset: float,
        length: float,
        out_dir: Path,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> FacecamChunkReport:  # pragma: no cover - interface
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover - interface
        return None


class NullFacecamAnalyzer(FacecamAnalyzer):
    """Honest fallback: facecam is disabled/unconfigured → no events, no cost."""

    name = "null"

    def __init__(self, reason: str) -> None:
        super().__init__()
        self.available = False
        self.unavailable_reason = reason

    async def analyze_chunk(self, ffmpeg_path, source, offset, length, out_dir,
                            should_stop=None) -> FacecamChunkReport:
        return unavailable_report(offset, length, 0.0, self.unavailable_reason)


class MediaPipeFacecamAnalyzer(FacecamAnalyzer):
    """The real analyzer: bounded frame extraction + MediaPipe Tasks (CPU).

    Per-job instance (no cross-job model state). The model is created
    lazily on the first chunk; ANY failure to create/validate it marks the
    analyzer unavailable for the whole job (honest, recorded, no retry
    storm) while the rest of the pipeline proceeds.
    """

    name = "mediapipe-tasks-face"

    def __init__(self, config: FacecamConfig) -> None:
        super().__init__()
        self._cfg = config
        self._model = MediaPipeFacecamModel(config)
        self.available = True
        self.unavailable_reason = None

    async def analyze_chunk(
        self,
        ffmpeg_path: str,
        source: Path,
        offset: float,
        length: float,
        out_dir: Path,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> FacecamChunkReport:
        cfg = self._cfg
        try:
            await self._model.create()  # validates models; may mark unavailable
        except FacecamModelError as exc:
            self.available = False
            self.unavailable_reason = str(exc)
            logger.info("facecam unavailable for job: %s", exc)
            return unavailable_report(offset, length, cfg.sample_fps, str(exc))

        try:
            frames = await extract_sampled_frames(
                ffmpeg_path, source, offset, length,
                cfg.sample_fps, cfg.max_frames_per_chunk, out_dir,
                cfg.frame_timeout_seconds,
            )
        except FrameExtractionError as exc:
            # Optional modality: record honestly, keep audio/visual evidence.
            logger.warning("facecam frame extraction failed (chunk @ %.0fs): %s", offset, exc)
            return unavailable_report(offset, length, cfg.sample_fps, str(exc))

        observations: list[FrameObservation] = []
        try:
            for t_rel, path in frames:
                if should_stop is not None and should_stop():
                    raise FacecamCancelled()
                obs = await self._model.analyze_frame(path, t_rel)
                observations.append(obs)
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)  # ALWAYS cleaned

        region, _tracks = detect_facecam_region(observations, cfg)
        present = sum(1 for o in observations if o.faces)
        report = FacecamChunkReport(
            offset=offset,
            duration=length,
            available=True,
            unavailable_reason=None,
            sample_fps=cfg.sample_fps,
            model=MediaPipeFacecamModel.NAME,
            frames_analyzed=len(observations),
            frames_without_face=len(observations) - present,
            observations=observations,
            region=region,
            presence_ratio=(present / len(observations)) if observations else 0.0,
            inference_ms_total=sum(o.inference_ms for o in observations),
        )
        return report

    async def close(self) -> None:
        await self._model.close()


def build_facecam_analyzer(settings: Any) -> FacecamAnalyzer:
    """Settings → analyzer. NEVER raises: disabled/unconfigured → Null.

    Follows the existing settings conventions (``WORKER_`` prefix,
    validated fields on ``Settings``).
    """
    if not getattr(settings, "facecam_enabled", False):
        return NullFacecamAnalyzer("facecam disabled (WORKER_FACECAM_ENABLED=false)")
    if not getattr(settings, "facecam_detector_model_path", None):
        return NullFacecamAnalyzer(
            "facecam enabled but no detector model configured "
            "(set WORKER_FACECAM_DETECTOR_MODEL_PATH)"
        )
    try:
        config = FacecamConfig(
            sample_fps=float(settings.facecam_sample_fps),
            min_confidence=float(settings.facecam_min_confidence),
            max_faces=int(settings.facecam_max_faces),
            max_concurrency=int(settings.facecam_max_concurrency),
            detector_model_path=str(settings.facecam_detector_model_path),
            landmarker_model_path=(
                str(settings.facecam_landmarker_model_path)
                if getattr(settings, "facecam_landmarker_model_path", None) else None
            ),
            frame_timeout_seconds=float(settings.facecam_frame_timeout_seconds),
        )
    except ValueError as exc:
        return NullFacecamAnalyzer(f"facecam misconfigured: {exc}")
    return MediaPipeFacecamAnalyzer(config)


def summarize_facecam(chunk_summaries: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregates per-chunk facecam summaries (this run + resumed runs)
    into the job-level ``PipelineResult.facecam`` metadata."""
    available = [s for s in chunk_summaries if s.get("available")]
    if not available:
        reason = next((s.get("unavailable_reason") for s in chunk_summaries
                       if s.get("unavailable_reason")), "facecam not run")
        return {"available": False, "reason": reason, "chunks_analyzed": 0}

    regions: list[dict[str, Any]] = [s["region"] for s in available if s.get("region")]
    majority_region = None
    if regions:
        # Majority vote on a rounded bbox key (robust to per-chunk jitter).
        counts: dict[tuple[float, float, float, float], dict[str, Any]] = {}
        for r in regions:
            key = tuple(round(v, 2) for v in r["bbox"])
            if key not in counts:
                counts[key] = dict(r)
            counts[key]["_votes"] = counts[key].get("_votes", 0) + 1
        best = max(counts.values(), key=lambda r: r.get("_votes", 0))
        best.pop("_votes", None)
        majority_region = best

    return {
        "available": True,
        "model": available[0].get("model"),
        "chunks_analyzed": len(available),
        "presence_ratio": round(sum(s.get("presence_ratio", 0.0) for s in available) / len(available), 4),
        "region": majority_region,
        "frames_analyzed": sum(int(s.get("frames_analyzed", 0)) for s in available),
        "inference_ms_total": round(sum(float(s.get("inference_ms_total", 0.0)) for s in available), 1),
        "note": ("facecam presence/region/observations are MEASURED; any clip "
                 "category derived from them is a HEURISTIC visual pattern, "
                 "not an emotion classification"),
    }
