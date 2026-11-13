"""Pure-geometry facial measures (TASK 008) — numpy only, NO mediapipe.

Every function here operates on normalized 2-D landmark tuples
``((x0, y0), (x1, y1), ...)`` (the model layer strips z/visibility) or on
3x3 rotation matrices, so the entire observable-geometry pipeline is
unit-testable WITHOUT the ML dependency installed.

These are MEASURABLE visual patterns — mouth aperture, smile-like mouth
width, brow lift, head rotation delta. They are deliberately NOT labeled
as emotions or states; any higher-level interpretation lives in the
selection layer and is always marked ``heuristic`` there.

Landmark indices follow the MediaPipe Face Landmarker 478-point mesh:
13/14 inner upper/lower lip, 61/291 mouth corners, 10/152 forehead/chin,
105/55 outer brow, 159/145 outer eye corner, 33/263 inner eye corners,
1 nose tip.
"""

from __future__ import annotations

import math

import numpy as np

# -- landmark indices (MediaPipe Face Landmarker mesh) ------------------------
LIP_TOP = 13
LIP_BOTTOM = 14
MOUTH_LEFT = 61
MOUTH_RIGHT = 291
FOREHEAD = 10
CHIN = 152
BROW_LEFT = 105
BROW_RIGHT = 55
EYE_OUT_LEFT = 159
EYE_OUT_RIGHT = 145
EYE_IN_LEFT = 33
EYE_IN_RIGHT = 263
NOSE = 1

#: Baseline mouth-corner width / interocular width on a NEUTRAL face
#: (measured against the bundled test fixture: 0.556). A visible smile
#: pulls the corners out to ≈ 0.72+; the measure reaches 1.0 at +0.18.
_NEUTRAL_MOUTH_OVER_IOD = 0.56
_SMILE_MOUTH_OVER_IOD_SPAN = 0.18


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def face_width(pts: list[tuple[float, float]]) -> float:
    """Inter-ocular-mouth width: distance between mouth corners (0..1)."""
    return _dist(pts[MOUTH_LEFT], pts[MOUTH_RIGHT])


def face_height(pts: list[tuple[float, float]]) -> float:
    """Forehead-to-chin distance (0..1)."""
    return _dist(pts[FOREHEAD], pts[CHIN])


def mouth_open_ratio(pts: list[tuple[float, float]]) -> float:
    """Mouth opening ratio: inner-lip distance normalized by face height.

    Neutral/closed face ≈ 0.02–0.05; an open (shouting) mouth reaches
    ≈ 0.15–0.30. A MEASURED geometry — never an emotion claim.
    """
    h = face_height(pts)
    if h <= 1e-6:
        return 0.0
    return _dist(pts[LIP_TOP], pts[LIP_BOTTOM]) / h


def interocular(pts: list[tuple[float, float]]) -> float:
    """Inner-eye-corner distance (scale reference, 0..1)."""
    return _dist(pts[EYE_IN_LEFT], pts[EYE_IN_RIGHT])


def smile_geometry(pts: list[tuple[float, float]]) -> float:
    """Smile-like GEOMETRY score (0..1): mouth-corner width relative to the
    neutral baseline (measured 0.56 mouth/interocular on a neutral face).
    A shape measure only — it says nothing about why the mouth is wide.
    The detector combines it with the model's smile blendshapes."""
    iod = interocular(pts)
    if iod <= 1e-6:
        return 0.0
    ratio = _dist(pts[MOUTH_LEFT], pts[MOUTH_RIGHT]) / iod
    return max(0.0, min(1.0, (ratio - _NEUTRAL_MOUTH_OVER_IOD) / _SMILE_MOUTH_OVER_IOD_SPAN))


def brow_geometry(pts: list[tuple[float, float]]) -> float:
    """Brow-to-eye vertical gap normalized by face height (raw ratio).

    Neutral ≈ 0.20–0.26; raised brows push it up. The activity detector
    compares this against the chunk median (relative), so per-face
    calibration is not required.
    """
    h = face_height(pts)
    if h <= 1e-6:
        return 0.0
    gap_l = _dist(pts[BROW_LEFT], pts[EYE_OUT_LEFT])
    gap_r = _dist(pts[BROW_RIGHT], pts[EYE_OUT_RIGHT])
    return (gap_l + gap_r) / (2.0 * h)


def rotation_delta_deg(prev: np.ndarray | None, curr: np.ndarray) -> float | None:
    """Angle (degrees) of the head rotation between two consecutive frames.

    ``prev``/``curr`` are the 3x3 rotation parts of the model's facial
    transformation matrices. The angle of ``prevᵀ · curr`` is
    convention-independent (no yaw/pitch/roll assumption) — exactly what
    a "sudden head movement" detector needs. Returns None when there is no
    previous frame to compare against.
    """
    if prev is None:
        return None
    rot = np.asarray(prev, dtype=np.float64).T @ np.asarray(curr, dtype=np.float64)
    cos_angle = max(-1.0, min(1.0, (float(np.trace(rot)) - 1.0) / 2.0))
    return math.degrees(math.acos(cos_angle))


def bbox_iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """Intersection-over-union of two (x, y, w, h) normalized boxes."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix0, iy0 = max(ax, bx), max(ay, by)
    ix1, iy1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0
