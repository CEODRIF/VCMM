"""Facecam signal detector (TASK 008) — measured facial patterns → events.

Pure function over one chunk's ``FrameObservation``s (no I/O, no model):
every emitted ``SignalEvent`` has family ``"facecam"`` and carries the
MEASURED values it was computed from plus detector metadata.

What is measured vs. what is inferred (kept strictly separate):
- MEASURED (this module): mouth opening ratio, smile-like score
  (blendshapes + geometry), brow lift, head rotation delta, face
  presence/visibility — with absolute timestamps on the sampling grid.
- INFERRED (the selection layer only): heuristic category labels, always
  flagged ``heuristic``. No event here claims an emotion, state,
  identity, or intent.

Low-confidence observations never emit: faces below ``min_confidence``
are dropped before detection, and event strength is scaled by visibility.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..signals import (
    KIND_FACE_BROW_ACTIVITY,
    KIND_FACE_HEAD_MOTION,
    KIND_FACE_MOUTH_BURST,
    KIND_FACE_SMILE_GEOMETRY,
    SignalEvent,
)
from .config import FacecamConfig
from .region import FrameObservation, FacecamRegion, primary_face

_NOTE = ("heuristic visual pattern (measured facial geometry/motion) — "
         "NOT an emotion, state, or identity classification")


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    arr = np.asarray(values, dtype=np.float64)
    return float(np.median(arr))


def detect_facecam_events(
    observations: list[FrameObservation],
    region: FacecamRegion | None,
    cfg: FacecamConfig,
    offset: float,
    model_name: str | None = None,
) -> list[SignalEvent]:
    """Detects facecam signal kinds within one chunk (ABSOLUTE times).

    Times come from the sampling grid (``offset + t_rel``); the 0.1 s
    event-key bucketing means a re-detection of the same physical moment
    in the overlapping neighbour chunk hashes equal and exact-dedups.
    """
    if not observations:
        return []

    def _meta(extra: dict[str, Any]) -> dict[str, Any]:
        return {
            "model": model_name,
            "sample_fps": cfg.sample_fps,
            "region_bbox": list(region.bbox) if region else None,
            "region_presence_ratio": region.presence_ratio if region else None,
            **extra,
        }

    # -- per-frame primary-face series -----------------------------------------
    series: list[tuple[float, Any]] = []  # (t_rel, FaceMeasure)
    for obs in observations:
        face = primary_face(obs, region, cfg)
        if face is not None:
            series.append((obs.t_rel, face))

    events: list[SignalEvent] = []
    if series:
        mouth_values = [f.mouth_open for _, f in series if f.mouth_open is not None]
        brow_values = [f.brow_lift for _, f in series if f.brow_lift is not None]
        mouth_med = max(_median(mouth_values), 1e-4)
        brow_med = max(_median(brow_values), 1e-4)

        # -- mouth bursts (sudden opening) -----------------------------------
        i = 0
        while i < len(series):
            t_rel, f = series[i]
            if (f.mouth_open is not None
                    and f.mouth_open >= cfg.mouth_burst_min
                    and f.mouth_open >= cfg.mouth_burst_ratio * mouth_med):
                j = i
                peak = f.mouth_open
                while (j + 1 < len(series) and series[j + 1][1].mouth_open is not None
                       and series[j + 1][1].mouth_open >= cfg.mouth_burst_ratio * mouth_med):
                    j += 1
                    peak = max(peak, series[j][1].mouth_open)
                t0 = offset + series[i][0]
                t1 = offset + series[j][0]
                strength = min(1.0, (peak / mouth_med - 1.0) / 2.0) * f.visibility
                events.append(SignalEvent(
                    t=round(t0, 3), t_end=round(max(t1, t0), 3),
                    family="facecam", kind=KIND_FACE_MOUTH_BURST,
                    strength=round(float(strength), 4),
                    evidence=_meta({
                        "family": "facecam", "kind": KIND_FACE_MOUTH_BURST,
                        "strength": round(float(strength), 4),
                        "peak_mouth_open": round(peak, 4),
                        "chunk_median_mouth_open": round(mouth_med, 4),
                        "face_confidence": f.confidence,
                        "note": f"measured mouth opening burst — {_NOTE}",
                    }),
                ))
                i = j + 1
            else:
                i += 1

        # -- smile-like geometry (sustained) -----------------------------------
        i = 0
        while i < len(series):
            t_rel, f = series[i]
            if f.smile_score is not None and f.smile_score >= cfg.smile_min:
                j = i
                peak = f.smile_score
                while (j + 1 < len(series) and series[j + 1][1].smile_score is not None
                       and series[j + 1][1].smile_score >= cfg.smile_min):
                    j += 1
                    peak = max(peak, series[j + 1][1].smile_score or 0.0)
                if (j - i + 1) >= cfg.smile_min_frames:
                    t0 = offset + series[i][0]
                    t1 = offset + series[j][0]
                    strength = min(1.0, peak / 0.9) * f.visibility
                    events.append(SignalEvent(
                        t=round(t0, 3), t_end=round(max(t1, t0), 3),
                        family="facecam", kind=KIND_FACE_SMILE_GEOMETRY,
                        strength=round(float(strength), 4),
                        evidence=_meta({
                            "family": "facecam", "kind": KIND_FACE_SMILE_GEOMETRY,
                            "strength": round(float(strength), 4),
                            "peak_smile_score": round(peak, 4),
                            "sustained_frames": j - i + 1,
                            "face_confidence": f.confidence,
                            "note": f"measured smile-like geometry (blendshapes + mouth width) — {_NOTE}",
                        }),
                    ))
                i = j + 1
            else:
                i += 1

        # -- sudden head motion (rotation delta between consecutive samples) ---
        for k in range(1, len(series)):
            t_rel, f = series[k]
            if f.pose_delta_deg is not None and f.pose_delta_deg >= cfg.head_motion_deg:
                t0 = offset + series[k - 1][0]
                t1 = offset + t_rel
                strength = min(1.0, f.pose_delta_deg / 30.0) * f.visibility
                events.append(SignalEvent(
                    t=round(t0, 3), t_end=round(t1, 3),
                    family="facecam", kind=KIND_FACE_HEAD_MOTION,
                    strength=round(float(strength), 4),
                    evidence=_meta({
                        "family": "facecam", "kind": KIND_FACE_HEAD_MOTION,
                        "strength": round(float(strength), 4),
                        "rotation_delta_deg": f.pose_delta_deg,
                        "face_confidence": f.confidence,
                        "note": f"measured sudden head pose change ({f.pose_delta_deg:.0f}°) — {_NOTE}",
                    }),
                ))

        # -- brow activity (lift vs chunk median) ------------------------------
        i = 0
        while i < len(series):
            t_rel, f = series[i]
            if (f.brow_lift is not None
                    and f.brow_lift >= cfg.brow_min
                    and f.brow_lift >= cfg.brow_ratio * brow_med):
                j = i
                peak = f.brow_lift
                while (j + 1 < len(series) and series[j + 1][1].brow_lift is not None
                       and series[j + 1][1].brow_lift >= cfg.brow_ratio * brow_med):
                    j += 1
                    peak = max(peak, series[j + 1][1].brow_lift or 0.0)
                t0 = offset + series[i][0]
                t1 = offset + series[j][0]
                strength = min(1.0, (peak / brow_med - 1.0) / 2.0) * f.visibility
                events.append(SignalEvent(
                    t=round(t0, 3), t_end=round(max(t1, t0), 3),
                    family="facecam", kind=KIND_FACE_BROW_ACTIVITY,
                    strength=round(float(strength), 4),
                    evidence=_meta({
                        "family": "facecam", "kind": KIND_FACE_BROW_ACTIVITY,
                        "strength": round(float(strength), 4),
                        "peak_brow_lift": round(peak, 4),
                        "chunk_median_brow_lift": round(brow_med, 4),
                        "face_confidence": f.confidence,
                        "note": f"measured brow/upper-face activity — {_NOTE}",
                    }),
                ))
                i = j + 1
            else:
                i += 1
    return events
