"""Facecam-region detection: temporal persistence + spatial consistency.

A facecam is a face that PERSISTS in a consistent region across a
substantial fraction of the chunk's sampled frames (an overlay / webcam
box). An in-game face (a character's portrait, a cutscene) appears only
briefly and/or moves with the camera — it fails the persistence test.

Nothing here assumes the largest face is the streamer: tracks are linked
by bbox IoU and the WINNING region is the one with the highest persistence
(stability breaks ties). The result is a ``FacecamRegion`` (or None) plus
per-track statistics, which the signal layer attaches to evidence.

Pure functions over ``FrameObservation``s — testable without any ML dep.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import FacecamConfig
from .geometry import bbox_iou


@dataclass
class FrameObservation:
    """One sampled frame's face analysis result (chunk-relative time)."""

    t_rel: float
    faces: list["FaceMeasure"] = field(default_factory=list)
    inference_ms: float = 0.0


@dataclass
class FaceMeasure:
    """One detected face in one frame (measured, absolute-agnostic).

    ``bbox`` is normalized (x, y, w, h) in 0..1 frame coordinates.
    Geometry fields are None when the landmarker model is unavailable
    (face PRESENCE without facial-activity metrics — honest partial data).
    """

    bbox: tuple[float, float, float, float]
    confidence: float
    mouth_open: float | None = None     # ratio, face-height normalized
    smile_score: float | None = None    # 0..1 (blendshapes + geometry)
    brow_lift: float | None = None      # 0..1 (blendshapes + geometry)
    pose_delta_deg: float | None = None # rotation vs previous frame (primary face)
    visibility: float = 1.0             # min(1, confidence) — tracking confidence


@dataclass
class Track:
    """A face track linked across consecutive frames (greedy IoU)."""

    index: int
    bboxes: list[tuple[float, float, float, float]] = field(default_factory=list)
    frame_indices: list[int] = field(default_factory=list)
    confidences: list[float] = field(default_factory=list)


@dataclass
class FacecamRegion:
    """A likely facecam overlay region (persistent + spatially stable)."""

    bbox: tuple[float, float, float, float]
    presence_ratio: float   # fraction of sampled frames the track is present
    bbox_iou: float         # mean pairwise bbox IoU (spatial consistency)
    area_ratio: float       # bbox area as a fraction of the frame
    stable: bool = True


def link_tracks(observations: list[FrameObservation], cfg: FacecamConfig) -> list[Track]:
    """Greedy IoU linking: a face continues a live track when its bbox IoU
    with that track's LAST bbox is ≥ ``track_link_iou``; otherwise it starts
    a new track (multiple faces supported, no size assumption)."""
    tracks: list[Track] = []
    for frame_idx, obs in enumerate(observations):
        used: set[int] = set()
        for face in obs.faces:
            best, best_iou = None, 0.0
            for ti, track in enumerate(tracks):
                if ti in used or not track.bboxes:
                    continue
                iou = bbox_iou(face.bbox, track.bboxes[-1])
                if iou >= cfg.track_link_iou and iou > best_iou:
                    best, best_iou = ti, iou
            if best is not None:
                track = tracks[best]
                track.bboxes.append(face.bbox)
                track.frame_indices.append(frame_idx)
                track.confidences.append(face.confidence)
                used.add(best)
            else:
                new = Track(index=len(tracks), bboxes=[face.bbox],
                            frame_indices=[frame_idx], confidences=[face.confidence])
                tracks.append(new)
    return tracks


def _mean_pairwise_iou(bboxes: list[tuple[float, float, float, float]]) -> float:
    if len(bboxes) < 2:
        return 1.0
    total, count = 0.0, 0
    for i in range(len(bboxes)):
        for j in range(i + 1, len(bboxes)):
            total += bbox_iou(bboxes[i], bboxes[j])
            count += 1
    return total / count if count else 1.0


def _mean_bbox(bboxes: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    n = len(bboxes)
    x = sum(b[0] for b in bboxes) / n
    y = sum(b[1] for b in bboxes) / n
    w = sum(b[2] for b in bboxes) / n
    h = sum(b[3] for b in bboxes) / n
    return (round(x, 4), round(y, 4), round(w, 4), round(h, 4))


def detect_facecam_region(
    observations: list[FrameObservation], cfg: FacecamConfig
) -> tuple[FacecamRegion | None, list[Track]]:
    """Returns (region_or_None, all_tracks).

    A track qualifies as a facecam region when:
    - presence_ratio ≥ ``region_persistence_ratio`` (temporal persistence);
    - mean pairwise bbox IoU ≥ ``region_min_iou`` (spatial consistency);
    - bbox area ≤ ``region_max_area_ratio`` (an OVERLAY, not a full-frame
      primary webcam — full-frame faces are reported as tracks but do not
      establish an overlay region).
    The most persistent qualifying track wins (stability breaks ties).
    """
    tracks = link_tracks(observations, cfg)
    n_frames = len(observations)
    if n_frames == 0:
        return None, tracks

    candidates: list[tuple[float, float, Track]] = []
    for track in tracks:
        presence = len(track.frame_indices) / n_frames
        iou = _mean_pairwise_iou(track.bboxes)
        mean = _mean_bbox(track.bboxes)
        area = mean[2] * mean[3]
        if (
            presence >= cfg.region_persistence_ratio
            and iou >= cfg.region_min_iou
            and area <= cfg.region_max_area_ratio
        ):
            candidates.append((presence, iou, track))

    if not candidates:
        return None, tracks
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    _, stability, track = candidates[0]
    mean = _mean_bbox(track.bboxes)
    region = FacecamRegion(
        bbox=mean,
        presence_ratio=round(len(track.frame_indices) / n_frames, 4),
        bbox_iou=round(stability, 4),
        area_ratio=round(mean[2] * mean[3], 4),
        stable=True,
    )
    return region, tracks


def primary_face(
    observation: FrameObservation, region: FacecamRegion | None, cfg: FacecamConfig
) -> FaceMeasure | None:
    """The face this frame's ACTIVITY metrics describe.

    When a region is established, prefer the face inside (or nearest to)
    that region — the likely facecam — without assuming it is the largest
    face. Otherwise: the highest-confidence face.
    """
    if not observation.faces:
        return None
    if region is not None:
        in_region = [
            f for f in observation.faces
            if bbox_iou(f.bbox, region.bbox) >= 0.1
        ]
        pool = in_region or observation.faces
    else:
        pool = observation.faces
    return max(pool, key=lambda f: (f.confidence,))
