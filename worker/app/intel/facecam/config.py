"""Facecam intelligence configuration (TASK 008).

Frozen, validated per-job configuration for the facecam modality. The
pipeline treats facecam as an OPTIONAL signal family: a ``None`` config
(or an unavailable analyzer) means the rest of the pipeline behaves
exactly as before (Phase 7 audio/visual evidence only).

Resource bounds are explicit:
- ``sample_fps`` — how many frames per second of the source are sampled
  for face analysis (default 1; the whole VOD is NEVER decoded into RAM,
  only a bounded number of small JPEG frames per chunk);
- ``max_frames_per_chunk`` — hard cap on frames per chunk (bounded disk +
  bounded inference time);
- ``max_concurrency`` — bound on concurrent inference sections (one
  per-job model instance; sequential within a chunk by design).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FacecamConfig:
    #: Sampling rate for face analysis (frames per second of source).
    sample_fps: float = 1.0
    #: Minimum detector confidence for a face to count (0..1).
    min_confidence: float = 0.5
    #: Maximum faces considered per frame (no "largest face = streamer"
    #: assumption — region persistence, not size, selects the facecam).
    max_faces: int = 3
    #: Concurrency bound for inference sections (1 = strict sequential).
    max_concurrency: int = 1
    #: Hard cap on sampled frames per chunk (bounded RAM/disk/CPU).
    max_frames_per_chunk: int = 1200
    #: Wall-clock budget for the FFmpeg frame-extraction pass (subprocess).
    frame_timeout_seconds: float = 120.0
    #: EXPLICIT model paths (validated on first use). The detector model
    #: is required; the landmarker model enables facial-geometry metrics
    #: (mouth/smile/brow/head) — without it only face PRESENCE is reported.
    detector_model_path: str | None = None
    landmarker_model_path: str | None = None
    # -- activity thresholds (relative where possible) ------------------------
    #: A mouth burst needs a geometric opening ratio >= this absolute floor...
    mouth_burst_min: float = 0.12
    #: ...and >= this multiple of the chunk's median opening.
    mouth_burst_ratio: float = 2.0
    #: Smile-like blendshape score (0..1, model-measured) to qualify.
    smile_min: float = 0.35
    #: ...sustained for at least this many consecutive sampled frames.
    smile_min_frames: int = 2
    #: Sudden head-pose rotation delta (degrees) between consecutive samples.
    head_motion_deg: float = 12.0
    #: Brow activity: lift score >= floor...
    brow_min: float = 0.30
    #: ...and >= this multiple of the chunk median.
    brow_ratio: float = 2.0
    # -- facecam-region (overlay vs transient in-game face) --------------------
    #: Fraction of sampled frames a track must persist in to establish a
    #: facecam region (temporal persistence).
    region_persistence_ratio: float = 0.5
    #: Mean pairwise bbox IoU of a persistent track (spatial consistency).
    region_min_iou: float = 0.4
    #: Max normalized bbox area for an OVERLAY-style facecam region
    #: (a full-frame face is a webcam-style primary video, not an overlay).
    region_max_area_ratio: float = 0.25
    #: IoU threshold for linking a face to the same track across frames.
    track_link_iou: float = 0.25

    def __post_init__(self) -> None:
        if not 0 < self.sample_fps <= 4:
            raise ValueError("sample_fps must be within (0, 4]")
        if not 0 < self.min_confidence <= 1:
            raise ValueError("min_confidence must be within (0, 1]")
        if self.max_faces < 1 or self.max_concurrency < 1:
            raise ValueError("max_faces / max_concurrency must be >= 1")
        if self.max_frames_per_chunk < 2:
            raise ValueError("max_frames_per_chunk must be >= 2")
        if self.frame_timeout_seconds <= 0:
            raise ValueError("frame_timeout_seconds must be positive")
        if self.detector_model_path is None:
            raise ValueError("detector_model_path is required for facecam")
        for name in ("mouth_burst_min", "smile_min", "brow_min",
                     "region_persistence_ratio", "region_min_iou", "track_link_iou"):
            if not 0 <= getattr(self, name) <= 1:
                raise ValueError(f"{name} must be within [0, 1]")
        if self.head_motion_deg <= 0:
            raise ValueError("head_motion_deg must be positive")
        if self.region_max_area_ratio <= 0:
            raise ValueError("region_max_area_ratio must be positive")
