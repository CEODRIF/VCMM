"""MediaPipe face model wrapper (TASK 008) — local inference only.

Model & dependency policy (enforced here):
- **Local-first, CPU**: ``BaseOptions.Delegate.CPU`` always; no cloud API,
  no paid service, no GPU requirement.
- **No weight download at runtime**: model files must exist at the
  explicitly configured paths (``FacecamConfig.detector_model_path`` /
  ``landmarker_model_path``) and are validated on first use. Missing or
  unreadable models raise ``FacecamModelError`` → the analyzer reports an
  honest ``unavailable`` result (never fabricated events).
- **Optional dependency**: ``mediapipe`` is imported lazily and only when
  facecam is enabled; the base installation does not include it. Envs
  without it produce the same honest unavailable result.
- Models (permissively licensed — Apache 2.0 per their model cards):
  - ``blaze_face_short_range.tflite`` (~225 KB) — face detection
    (bounding box + confidence; identity recognition explicitly out of
    the model's scope per its model card);
  - ``face_landmarker.task`` (~3.6 MB) — 478 landmarks + 52 ARKit-style
    blendshapes + facial transformation matrices (enables the geometry
    metrics). Optional: without it, face PRESENCE is still reported.

MediaPipe 1.x exposes only the Tasks API (the legacy Solutions API was
removed), so imports are ``mediapipe.tasks.python.vision`` +
``mediapipe.tasks.python.core.base_options``. Instances are NOT
thread-safe: all inference runs in one worker thread guarded by an
asyncio lock (the analyzer is per-job, chunks are sequential).
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import numpy as np

from .config import FacecamConfig
from .geometry import (
    brow_geometry,
    mouth_open_ratio,
    rotation_delta_deg,
    smile_geometry,
)
from .region import FaceMeasure, FrameObservation

logger = logging.getLogger("clipforge.intel.facecam.model")


class FacecamModelError(RuntimeError):
    """Model import/load/validation failed (safe message)."""


class MediaPipeFacecamModel:
    """Lazy, validated MediaPipe Tasks models (detector + optional landmarker)."""

    #: Model identity recorded in evidence/diagnostics.
    NAME = "mediapipe-tasks-face (blaze_face_short_range + face_landmarker, CPU)"

    def __init__(self, config: FacecamConfig) -> None:
        self._cfg = config
        self._detector = None
        self._landmarker = None
        self._lock = asyncio.Lock()
        self._last_rotation = None  # primary-face rotation matrix (per-chunk)

    # -- lifecycle --------------------------------------------------------------

    async def reset_chunk(self) -> None:
        """Resets per-chunk temporal state (pose deltas start fresh)."""
        self._last_rotation = None

    async def create(self) -> None:
        """Imports mediapipe and builds the models (once). Any failure is a
        FacecamModelError → honest unavailable (no partial fabrication)."""

        def _build() -> None:
            try:
                import mediapipe as mp  # noqa: PLC0415 — optional dependency
                from mediapipe.tasks.python import vision  # noqa: PLC0415
                from mediapipe.tasks.python.core.base_options import BaseOptions  # noqa: PLC0415
            except ImportError as exc:
                raise FacecamModelError("mediapipe is not installed (optional extra: "
                                         "worker/requirements-facecam.txt)") from exc

            cfg = self._cfg
            for label, path in (
                ("detector model", cfg.detector_model_path),
                ("landmarker model", cfg.landmarker_model_path),
            ):
                if path is not None and not Path(path).is_file():
                    raise FacecamModelError(f"{label} is not a readable file: {Path(path).name}")
            if cfg.detector_model_path is None:
                raise FacecamModelError("no detector model configured")

            delegate = BaseOptions.Delegate.CPU  # CPU-only by policy
            self._mp = mp
            self._vision = vision
            base = BaseOptions(model_asset_path=cfg.detector_model_path, delegate=delegate)
            self._detector = vision.FaceDetector.create_from_options(
                vision.FaceDetectorOptions(base_options=base,
                                           min_detection_confidence=cfg.min_confidence)
            )
            if cfg.landmarker_model_path is not None:
                base_lm = BaseOptions(model_asset_path=cfg.landmarker_model_path, delegate=delegate)
                self._landmarker = vision.FaceLandmarker.create_from_options(
                    vision.FaceLandmarkerOptions(
                        base_options=base_lm,
                        num_faces=cfg.max_faces,
                        min_face_detection_confidence=cfg.min_confidence,
                        min_face_presence_confidence=cfg.min_confidence,
                        output_face_blendshapes=True,
                        output_facial_transformation_matrixes=True,
                    )
                )

        try:
            await asyncio.to_thread(_build)
        except FacecamModelError:
            raise
        except Exception as exc:  # noqa: BLE001 — native/lib load boundary
            raise FacecamModelError(f"face model could not be loaded ({exc.__class__.__name__})") from exc

    async def close(self) -> None:
        def _close() -> None:
            for attr in ("_detector", "_landmarker"):
                obj = getattr(self, attr, None)
                if obj is not None:
                    try:
                        obj.close()
                    except Exception:  # noqa: BLE001 — teardown best-effort
                        pass
                    setattr(self, attr, None)

        try:
            await asyncio.to_thread(_close)
        except Exception:  # noqa: BLE001
            logger.debug("facecam model close raised", exc_info=True)

    # -- per-frame inference -------------------------------------------------------

    async def analyze_frame(self, path: Path, t_rel: float) -> FrameObservation:
        """Runs detector (+ landmarker) on ONE frame; returns the observation.

        All model access happens under ``_lock`` in a single worker thread
        (instances are not thread-safe; CPU-only). Pose deltas are tracked
        for the PRIMARY (first) face across consecutive frames.
        """
        async with self._lock:
            t0 = time.perf_counter()

            def _run() -> FrameObservation:
                mp, vision = self._mp, self._vision
                cfg = self._cfg
                image = mp.Image.create_from_file(str(path))
                result = self._detector.detect(image)
                faces: list[FaceMeasure] = []
                dets = list(result.detections)
                if dets:
                    dets = sorted(dets, key=lambda d: -(d.categories[0].score if d.categories else 0.0))
                    for det in dets[: cfg.max_faces]:
                        bb = det.bounding_box
                        conf = float(det.categories[0].score) if det.categories else 0.0
                        w, h = max(1, image.width), max(1, image.height)
                        bbox = (
                            round(bb.origin_x / w, 4),
                            round(bb.origin_y / h, 4),
                            round(bb.width / w, 4),
                            round(bb.height / h, 4),
                        )
                        if conf < cfg.min_confidence:
                            continue  # low-confidence: dropped, counted by caller
                        faces.append(FaceMeasure(bbox=bbox, confidence=round(conf, 4),
                                                 visibility=round(min(1.0, conf), 4)))
                if self._landmarker is not None and faces:
                    lm_result = self._landmarker.detect(image)
                    landmarks = getattr(lm_result, "face_landmarks", None) or []
                    blends = getattr(lm_result, "face_blendshapes", None) or []
                    matrices = getattr(lm_result, "facial_transformation_matrixes", None) or []
                    for i, face in enumerate(faces[: len(landmarks)]):
                        if i >= len(landmarks):
                            break
                        pts = [(float(p.x), float(p.y)) for p in landmarks[i]]
                        face.mouth_open = round(mouth_open_ratio(pts), 4)
                        face.brow_lift = round(brow_geometry(pts), 4)
                        smile_geom = smile_geometry(pts)
                        bs = {}
                        if i < len(blends) and blends[i]:
                            bs = {b.category_name: b.score for b in blends[i]}
                        blend_smile = max(bs.get("mouthSmileLeft") or 0.0, bs.get("mouthSmileRight") or 0.0)
                        # blendshapes are MEASURED model outputs; combined with
                        # geometry (0.6 model / 0.4 shape) as a smile-like score.
                        face.smile_score = round(min(1.0, 0.6 * blend_smile + 0.4 * smile_geom), 4)
                        face.brow_lift = round(
                            min(1.0, max(face.brow_lift, bs.get("browInnerUp") or 0.0,
                                         (bs.get("browOuterUpLeft") or 0.0) / 2,
                                         (bs.get("browOuterUpRight") or 0.0) / 2)),
                            4,
                        )
                        if i < len(matrices):
                            rot = np.asarray(matrices[i], dtype="float64")[:3, :3]
                            if i == 0:  # primary face carries the pose delta
                                delta = rotation_delta_deg(self._last_rotation, rot)
                                face.pose_delta_deg = round(delta, 2) if delta is not None else None
                                self._last_rotation = rot
                return FrameObservation(t_rel=t_rel, faces=faces,
                                        inference_ms=(time.perf_counter() - t0) * 1000.0)

            return await asyncio.to_thread(_run)
