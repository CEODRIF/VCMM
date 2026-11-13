"""TASK 008 — facecam intelligence tests.

Two layers:

1. ALWAYS-RUN (no ML dependency): geometry, region persistence, signal
   detection, fusion/selection rules, builder honesty, frame extraction
   (real FFmpeg), cancellation/cleanup, pipeline-level ownership + dedup,
   and Phase 4-7 regression guarantees — all over SYNTHETIC fixtures.
   No real-world accuracy is claimed anywhere: the face fixture images
   are synthetic test assets, and every "detected" behavior below is an
   implementation contract, not a performance number.

2. REAL-MODEL smoke tests (skipped unless facecam test models + mediapipe
   are present): face detected / no face / multiple faces / CPU-only
   operation / timestamp grid / geometry sanity on the bundled fixtures.

Model paths for the real-model tests come from ``FACECAM_TEST_MODELS_DIR``
(default ``/root/models`` locally; CI downloads the two Apache-2.0 model
files into a workspace dir and sets the variable).
"""

from __future__ import annotations

import asyncio
import math
import os
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

from app.intel.facecam import (
    FacecamCancelled,
    MediaPipeFacecamAnalyzer,
    NullFacecamAnalyzer,
    build_facecam_analyzer,
    summarize_facecam,
    unavailable_report,
)
from app.intel.facecam.config import FacecamConfig
from app.intel.facecam.frames import FrameExtractionError, extract_sampled_frames
from app.intel.facecam.geometry import (
    brow_geometry,
    bbox_iou,
    mouth_open_ratio,
    rotation_delta_deg,
    smile_geometry,
)
from app.intel.facecam.region import (
    FaceMeasure,
    FrameObservation,
    detect_facecam_region,
    primary_face,
)
from app.intel.facecam.signals import detect_facecam_events
from app.intel.signals import (
    KIND_ENERGY_SPIKE,
    KIND_FACE_BROW_ACTIVITY,
    KIND_FACE_HEAD_MOTION,
    KIND_FACE_MOUTH_BURST,
    KIND_FACE_SMILE_GEOMETRY,
    KIND_TRANSIENT,
    SignalEvent,
    event_key,
)
from app.intel.fusion import FusionConfig, fuse

MODELS_DIR = Path(os.environ.get("FACECAM_TEST_MODELS_DIR", "/root/models"))
HAS_MODELS = (
    os.path.isfile(MODELS_DIR / "blaze_face_short_range.tflite")
    and os.path.isfile(MODELS_DIR / "face_landmarker.task")
)

def _mediapipe_runtime_ok() -> bool:
    """Probes the FULL native runtime (import alone is not enough: the
    mediapipe CPU inference path links EGL/GLES — the real failure mode on
    systems without mesa). Returns False → real-model tests skip."""
    try:
        from mediapipe.tasks.python import vision  # noqa: F401
        import ctypes
        ctypes.CDLL("libEGL.so.1")
        return True
    except Exception:  # noqa: BLE001 — any native/import failure = extra absent
        return False


HAS_MEDIAPIPE = _mediapipe_runtime_ok()

REAL_MODEL = pytest.mark.skipif(
    not (HAS_MODELS and HAS_MEDIAPIPE),
    reason="facecam test models / mediapipe native runtime not present (optional extra)",
)

CFG = FacecamConfig(detector_model_path=str(MODELS_DIR / "blaze_face_short_range.tflite"),
                    landmarker_model_path=str(MODELS_DIR / "face_landmarker.task"))
FIXTURES = Path(__file__).parent / "fixtures"


# -- helpers --------------------------------------------------------------------

def _face(bbox=(0.10, 0.10, 0.20, 0.20), conf=0.9, mouth=None, smile=None,
          brow=None, pose=None) -> FaceMeasure:
    return FaceMeasure(bbox=bbox, confidence=conf, mouth_open=mouth, smile_score=smile,
                       brow_lift=brow, pose_delta_deg=pose, visibility=min(1.0, conf))


def _obs(t_rel: float, faces: list[FaceMeasure]) -> FrameObservation:
    return FrameObservation(t_rel=t_rel, faces=faces)


def _neutral_pts() -> list[tuple[float, float]]:
    """A synthetic 478-point landmark set with KNOWN neutral geometry."""
    pts = [(0.5, 0.5)] * 478
    pts[10] = (0.5, 0.2)    # forehead
    pts[152] = (0.5, 0.8)   # chin  -> face_h = 0.6
    pts[61] = (0.4555, 0.6) # mouth corners -> mouth_w = 0.089
    pts[291] = (0.5445, 0.6)
    pts[13] = (0.5, 0.58)   # inner lips -> mouth_open = 0.04/0.6
    pts[14] = (0.5, 0.62)
    pts[33] = (0.42, 0.4)   # inner eyes -> iod = 0.16
    pts[263] = (0.58, 0.4)
    pts[105] = (0.40, 0.33) # brows
    pts[55] = (0.60, 0.33)
    pts[159] = (0.40, 0.36)
    pts[145] = (0.60, 0.36)
    pts[1] = (0.5, 0.5)     # nose
    return pts


def _rot_z(deg: float) -> np.ndarray:
    r = math.radians(deg)
    return np.array([[math.cos(r), -math.sin(r), 0.0],
                     [math.sin(r), math.cos(r), 0.0],
                     [0.0, 0.0, 1.0]])


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# -- 1. geometry (pure) -----------------------------------------------------------

def test_geometry_neutral_face_measures():
    pts = _neutral_pts()
    assert mouth_open_ratio(pts) == pytest.approx(0.04 / 0.6, abs=1e-3)
    assert smile_geometry(pts) == pytest.approx(0.0, abs=0.05)  # neutral mouth width


def test_geometry_smile_mouth_wider():
    pts = _neutral_pts()
    pts[61] = (0.43, 0.61)   # corners pulled out + up
    pts[291] = (0.57, 0.61)  # mouth_w = 0.14 -> ratio 0.875
    assert smile_geometry(pts) > 1.0 - 1e-9  # saturated smile-like geometry


def test_geometry_brow_raise():
    base = _neutral_pts()
    raised = _neutral_pts()
    raised[105] = (0.40, 0.30)  # brow moves away from the eye
    raised[55] = (0.60, 0.30)
    assert brow_geometry(raised) > brow_geometry(base)


def test_rotation_delta_identity_and_30deg():
    assert rotation_delta_deg(None, _rot_z(10.0)) is None
    assert rotation_delta_deg(_rot_z(0.0), _rot_z(0.0)) == pytest.approx(0.0, abs=1e-6)
    assert rotation_delta_deg(_rot_z(0.0), _rot_z(30.0)) == pytest.approx(30.0, abs=1e-3)
    assert rotation_delta_deg(_rot_z(10.0), _rot_z(40.0)) == pytest.approx(30.0, abs=1e-3)


def test_bbox_iou():
    assert bbox_iou((0, 0, 1, 1), (0, 0, 1, 1)) == pytest.approx(1.0)
    assert bbox_iou((0, 0, 0.5, 0.5), (0.6, 0.6, 0.5, 0.5)) == 0.0
    assert bbox_iou((0, 0, 1, 1), (0.5, 0.5, 1, 1)) == pytest.approx(0.25 / 1.75)


# -- 2. region: persistence vs transient ------------------------------------------

def test_facecam_overlay_persistence_establishes_region():
    obs = [_obs(i, [_face()]) for i in range(20)]
    region, tracks = detect_facecam_region(obs, CFG)
    assert region is not None
    assert region.presence_ratio == pytest.approx(1.0)
    assert region.bbox_iou == pytest.approx(1.0)
    assert region.stable is True
    assert len(tracks) == 1


def test_transient_in_game_face_does_not_establish_region():
    # A face that appears for only 3 of 20 frames (a cutscene character)
    # is a transient: observations exist, but NO facecam region.
    obs = [_obs(i, [_face(bbox=(0.4, 0.4, 0.3, 0.3))]) for i in (0, 1, 2)]
    obs += [_obs(i, []) for i in range(3, 20)]
    region, tracks = detect_facecam_region(obs, CFG)
    assert region is None
    assert len(tracks) == 1
    assert len(tracks[0].frame_indices) == 3


def test_multiple_faces_persistence_wins_over_size():
    # Track A: small face, persistent corner overlay (20/20 frames).
    # Track B: LARGE face, moving with the camera, present 12/20.
    obs = []
    for i in range(20):
        faces = [_face(bbox=(0.05, 0.05, 0.15, 0.15))]  # small, stable
        if i < 12:
            w = 0.4  # large, drifting
            faces.append(_face(bbox=(0.3 + 0.01 * i, 0.3, w, w)))
        obs.append(_obs(i, faces))
    region, tracks = detect_facecam_region(obs, CFG)
    assert region is not None
    # The region must be the small PERSISTENT overlay — not the larger one.
    assert region.bbox[2] < 0.2
    assert region.presence_ratio == pytest.approx(1.0)


def test_occluded_face_low_presence_no_region():
    # Occlusion: face visible in 8/20 frames (0.4 < 0.5 persistence).
    obs = [_obs(i, [_face()]) if i < 8 else _obs(i, []) for i in range(20)]
    region, _ = detect_facecam_region(obs, CFG)
    assert region is None


def test_full_frame_face_is_not_an_overlay_region():
    # A full-frame face (webcam-style primary video) is a track, not an
    # overlay region (area > region_max_area_ratio).
    obs = [_obs(i, [_face(bbox=(0.0, 0.0, 1.0, 1.0))]) for i in range(20)]
    region, tracks = detect_facecam_region(obs, CFG)
    assert region is None
    assert len(tracks) == 1


def test_primary_face_prefers_region_track():
    region, _ = detect_facecam_region([_obs(i, [_face()]) for i in range(10)], CFG)
    obs = _obs(0.0, [
        _face(bbox=(0.05, 0.05, 0.15, 0.15), conf=0.7),  # near region
        _face(bbox=(0.6, 0.6, 0.3, 0.3), conf=0.99),     # high conf but elsewhere
    ])
    # region bbox ~ (0.1, 0.1, 0.2, 0.2): the 0.05 face has IoU >= 0.1 with it
    assert primary_face(obs, region, CFG).confidence == 0.7


# -- 3. signals: measured patterns -> events ---------------------------------------

def test_no_observations_no_events():
    assert detect_facecam_events([], None, CFG, offset=100.0) == []
    assert detect_facecam_events([_obs(i, []) for i in range(10)], None, CFG, 0.0) == []


def test_mouth_burst_event_absolute_timestamp_and_evidence():
    obs = [_obs(i, [_face(mouth=0.03)]) for i in range(10)]
    obs[5] = _obs(5.0, [_face(mouth=0.30)])  # burst on the sampling grid
    events = detect_facecam_events(obs, None, CFG, offset=600.0)
    assert len(events) == 1
    e = events[0]
    assert e.family == "facecam"
    assert e.kind == KIND_FACE_MOUTH_BURST
    assert e.t == 605.0  # ABSOLUTE (offset + grid)
    assert e.t_end == 605.0
    assert e.strength > 0
    assert e.evidence["peak_mouth_open"] == 0.30
    assert e.evidence["chunk_median_mouth_open"] == pytest.approx(0.03)
    assert e.evidence["model"] is None or "mediapipe" in e.evidence["model"]
    assert "NOT an emotion" in e.evidence["note"]


def test_mouth_burst_needs_absolute_floor():
    # All frames at 0.30 (median 0.30): a relative spike, but below the
    # absolute floor? 0.30 >= 0.12 AND >= 2x median? 0.30 < 0.60 -> NO event.
    obs = [_obs(i, [_face(mouth=0.30)]) for i in range(10)]
    assert detect_facecam_events(obs, None, CFG, 0.0) == []


def test_smile_sustained_emits_one_event():
    obs = [_obs(i, [_face(smile=0.05)]) for i in range(8)]
    obs[3] = _obs(3.0, [_face(smile=0.60)])
    obs[4] = _obs(4.0, [_face(smile=0.65)])
    obs[5] = _obs(5.0, [_face(smile=0.62)])
    events = detect_facecam_events(obs, None, CFG, offset=100.0)
    smiles = [e for e in events if e.kind == KIND_FACE_SMILE_GEOMETRY]
    assert len(smiles) == 1
    assert smiles[0].t == 103.0 and smiles[0].t_end == 105.0
    assert smiles[0].evidence["sustained_frames"] == 3


def test_smile_single_frame_does_not_emit():
    obs = [_obs(i, [_face(smile=0.05)]) for i in range(8)]
    obs[4] = _obs(4.0, [_face(smile=0.80)])
    events = detect_facecam_events(obs, None, CFG, 0.0)
    assert not [e for e in events if e.kind == KIND_FACE_SMILE_GEOMETRY]


def test_head_motion_event_on_sudden_pose_change():
    obs = [_obs(i, [_face(pose=0.0 if i == 0 else None)]) for i in range(6)]
    obs[3] = _obs(3.0, [_face(pose=25.0)])  # 25 deg jump >= 12
    events = detect_facecam_events(obs, None, CFG, offset=50.0)
    heads = [e for e in events if e.kind == KIND_FACE_HEAD_MOTION]
    assert len(heads) == 1
    assert heads[0].t == 52.0 and heads[0].t_end == 53.0
    assert heads[0].evidence["rotation_delta_deg"] == 25.0


def test_brow_activity_event():
    obs = [_obs(i, [_face(brow=0.12)]) for i in range(10)]
    obs[6] = _obs(6.0, [_face(brow=0.40)])  # >= 0.30 and >= 2x median 0.12
    events = detect_facecam_events(obs, None, CFG, 0.0)
    brows = [e for e in events if e.kind == KIND_FACE_BROW_ACTIVITY]
    assert len(brows) == 1
    assert brows[0].t == 6.0


def test_low_confidence_scales_strength_down():
    obs_hi = [_obs(i, [_face(mouth=0.03, conf=0.9)]) for i in range(10)]
    obs_hi[5] = _obs(5.0, [_face(mouth=0.30, conf=0.9)])
    obs_lo = [_obs(i, [_face(mouth=0.03, conf=0.5)]) for i in range(10)]
    obs_lo[5] = _obs(5.0, [_face(mouth=0.30, conf=0.5)])
    hi = detect_facecam_events(obs_hi, None, CFG, 0.0)
    lo = detect_facecam_events(obs_lo, None, CFG, 0.0)
    assert len(hi) == len(lo) == 1
    assert lo[0].strength == pytest.approx(hi[0].strength * (0.5 / 0.9), abs=1e-3)


def test_event_keys_stable_for_same_moment():
    a = SignalEvent(t=600.01, t_end=600.05, family="facecam", kind=KIND_FACE_MOUTH_BURST, strength=0.5)
    b = SignalEvent(t=600.04, t_end=600.06, family="facecam", kind=KIND_FACE_MOUTH_BURST, strength=0.51)
    c = SignalEvent(t=600.15, t_end=600.2, family="facecam", kind=KIND_FACE_MOUTH_BURST, strength=0.5)
    assert event_key("job", a) == event_key("job", b)  # same 0.1 s bucket
    assert event_key("job", a) != event_key("job", c)  # different bucket
    assert event_key("jobA", a) != event_key("jobB", a)


def test_geometry_fields_absent_are_not_events():
    # Presence without landmarker metrics (detector-only mode): no
    # mouth/smile/brow/head events are fabricated from missing data.
    obs = [_obs(i, [_face()]) for i in range(10)]
    assert detect_facecam_events(obs, None, CFG, 0.0) == []


# -- 4. fusion rules ----------------------------------------------------------------

def _ev(t, family, kind, strength):
    return SignalEvent(t=t, t_end=t + 0.2, family=family, kind=kind, strength=strength,
                       evidence={"family": family, "kind": kind, "strength": strength})


def test_facecam_only_low_confidence_cannot_qualify():
    windows = fuse([_ev(10.0, "facecam", KIND_FACE_MOUTH_BURST, 0.4)], FusionConfig())
    assert windows == []  # 100*0.4*0.4 = 16 < 25 min_score


def test_facecam_only_high_strength_qualifies_with_discount():
    windows = fuse([_ev(10.0, "facecam", KIND_FACE_MOUTH_BURST, 0.9)], FusionConfig())
    assert len(windows) == 1
    assert windows[0].reasons["solo_mode"] == "facecam_only"
    assert windows[0].score == pytest.approx(100 * 0.9 * 0.4)  # discounted, not full
    assert windows[0].confidence == pytest.approx(0.5)


def test_facecam_plus_audio_gets_diversity_bonus():
    windows = fuse([_ev(10.0, "facecam", KIND_FACE_MOUTH_BURST, 0.8),
                    _ev(10.5, "audio", KIND_ENERGY_SPIKE, 0.8)], FusionConfig())
    assert len(windows) == 1
    w = windows[0]
    assert set(w.families) == {"audio", "facecam"}
    assert w.confidence == pytest.approx(1.0)
    assert w.score == pytest.approx(100 * 0.8 * 1.25)  # diversity bonus


def test_existing_audio_only_behavior_unchanged():
    windows = fuse([_ev(10.0, "audio", KIND_ENERGY_SPIKE, 0.8)], FusionConfig())
    assert len(windows) == 1
    assert windows[0].score == pytest.approx(100 * 0.8 * 0.5)  # classic solo 0.5


def test_existing_sfx_only_behavior_unchanged():
    windows = fuse([_ev(10.0, "audio", KIND_TRANSIENT, 1.0)], FusionConfig())
    assert windows == []  # 100*1.0*0.2 = 20 < 25


def test_three_families_agree():
    windows = fuse([_ev(10.0, "facecam", KIND_FACE_SMILE_GEOMETRY, 0.7),
                    _ev(10.2, "audio", KIND_ENERGY_SPIKE, 0.7),
                    _ev(10.4, "visual", "motion_burst", 0.7)], FusionConfig())
    assert len(windows) == 1
    assert set(windows[0].families) == {"audio", "facecam", "visual"}
    assert windows[0].score == pytest.approx(100 * 0.7 * 1.25)


# -- 5. selection categorization (heuristic labels) ----------------------------------

def test_smile_plus_vocal_categorizes_funny_heuristic():
    from app.intel.selection import CATEGORY_FUNNY, _categorize
    from app.intel.fusion import CandidateWindow
    w = CandidateWindow(start=10, end=11,
                        events=[_ev(10, "audio", KIND_ENERGY_SPIKE, 0.8),
                                _ev(10.5, "facecam", KIND_FACE_SMILE_GEOMETRY, 0.7)],
                        families=("audio", "facecam"),
                        family_strengths={"audio": 0.8, "facecam": 0.7})
    cat, why = _categorize(w)
    assert cat == CATEGORY_FUNNY
    assert "heuristic" in why


def test_mouth_burst_plus_vocal_categorizes_crazy_reaction():
    from app.intel.selection import CATEGORY_CRAZY_REACTION, _categorize
    from app.intel.fusion import CandidateWindow
    w = CandidateWindow(start=10, end=11,
                        events=[_ev(10, "audio", KIND_ENERGY_SPIKE, 0.9),
                                _ev(10.3, "facecam", KIND_FACE_MOUTH_BURST, 0.8)],
                        families=("audio", "facecam"),
                        family_strengths={"audio": 0.9, "facecam": 0.8})
    cat, why = _categorize(w)
    assert cat == CATEGORY_CRAZY_REACTION
    assert "heuristic" in why


def test_facecam_only_stays_unclassified_labeled():
    from app.intel.selection import CATEGORY_UNCLASSIFIED, _categorize
    from app.intel.fusion import CandidateWindow
    w = CandidateWindow(start=10, end=11,
                        events=[_ev(10, "facecam", KIND_FACE_HEAD_MOTION, 0.8)],
                        families=("facecam",), family_strengths={"facecam": 0.8})
    cat, why = _categorize(w)
    assert cat == CATEGORY_UNCLASSIFIED
    assert "not an emotion" in why


# -- 6. builder + analyzer honesty ---------------------------------------------------

def test_builder_disabled_returns_null():
    class S:  # minimal settings stand-in
        facecam_enabled = False
    a = build_facecam_analyzer(S())
    assert isinstance(a, NullFacecamAnalyzer)
    assert a.available is False
    assert "disabled" in a.unavailable_reason


def test_builder_enabled_without_model_returns_null():
    class S:
        facecam_enabled = True
        facecam_detector_model_path = None
    a = build_facecam_analyzer(S())
    assert isinstance(a, NullFacecamAnalyzer)
    assert "no detector model" in a.unavailable_reason


def test_builder_misconfigured_returns_null():
    class S:
        facecam_enabled = True
        facecam_detector_model_path = "/x/det.tflite"
        facecam_sample_fps = 99.0
        facecam_min_confidence = 0.5
        facecam_max_faces = 3
        facecam_max_concurrency = 1
        facecam_landmarker_model_path = None
        facecam_frame_timeout_seconds = 120.0
    a = build_facecam_analyzer(S())
    assert isinstance(a, NullFacecamAnalyzer)
    assert "misconfigured" in a.unavailable_reason


def test_null_analyzer_report_is_honest():
    a = NullFacecamAnalyzer("facecam disabled (test)")
    rep = run(a.analyze_chunk("ffmpeg", Path("/nonexistent"), 0.0, 10.0, Path("/tmp/nc")))
    assert rep.available is False
    assert rep.unavailable_reason == "facecam disabled (test)"
    assert rep.observations == [] and rep.region is None
    assert detect_facecam_events(rep.observations, rep.region, CFG, 0.0) == []


def test_missing_model_file_is_honest_unavailable():
    cfg = FacecamConfig(detector_model_path="/definitely/not/here/det.tflite")
    a = MediaPipeFacecamAnalyzer(cfg)
    rep = run(a.analyze_chunk("ffmpeg", Path("/nonexistent"), 0.0, 10.0, Path("/tmp/mm")))
    assert rep.available is False
    assert "not a readable file" in (rep.unavailable_reason or "")
    assert a.available is False  # sticky for the job (no retry storm)
    run(a.close())


def test_missing_dependency_is_honest_unavailable(monkeypatch):
    # Force `import mediapipe` to fail (the base-install case).
    monkeypatch.setitem(sys.modules, "mediapipe", None)
    cfg = FacecamConfig(detector_model_path=str(MODELS_DIR / "blaze_face_short_range.tflite"))
    a = MediaPipeFacecamAnalyzer(cfg)
    rep = run(a.analyze_chunk("ffmpeg", Path("/nonexistent"), 0.0, 10.0, Path("/tmp/md")))
    assert rep.available is False
    assert "not installed" in (rep.unavailable_reason or "")


def test_summarize_facecam_aggregates():
    s1 = {"available": True, "model": "m", "presence_ratio": 1.0, "frames_analyzed": 600,
          "inference_ms_total": 1000.0,
          "region": {"bbox": [0.1, 0.1, 0.2, 0.2], "presence_ratio": 1.0,
                     "bbox_iou": 0.9, "area_ratio": 0.04}}
    s2 = {"available": True, "model": "m", "presence_ratio": 0.5, "frames_analyzed": 600,
          "inference_ms_total": 900.0,
          "region": {"bbox": [0.11, 0.09, 0.21, 0.21], "presence_ratio": 0.98,
                     "bbox_iou": 0.9, "area_ratio": 0.04}}
    s3 = {"available": False, "unavailable_reason": "model load failed"}
    out = summarize_facecam([s1, s2, s3])
    assert out["available"] is True
    assert out["chunks_analyzed"] == 2
    assert out["presence_ratio"] == pytest.approx(0.75)
    assert out["region"]["presence_ratio"] in (1.0, 0.98)  # majority vote
    assert summarize_facecam([s3])["available"] is False
    assert summarize_facecam([s3])["reason"] == "model load failed"
    assert summarize_facecam([]) is not None  # caller guards; still safe


# -- 7. frame extraction (real FFmpeg, bounded) ---------------------------------------

def _make_video(ffmpeg: str, out: Path, seconds: float = 12.0, src: str = "testsrc",
                audio: bool = True) -> Path:
    """A short H.264 clip. ``audio=True`` adds a 440 Hz tone (the pipeline's
    audio pass requires an audio track, mirroring the Phase 4 contract)."""
    args = ["-v", "error"]
    if src.startswith("file:"):
        args += ["-loop", "1", "-t", str(seconds), "-i", src[5:]]
    else:
        args += ["-f", "lavfi", "-i", f"{src}=duration={seconds}:size=640x360:rate=24"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    else:
        args += ["-an"]
    args.append(str(out))
    import subprocess
    subprocess.run([ffmpeg, *args], check=True, capture_output=True)
    return out


def test_frame_extraction_bounded_and_timed(ffmpeg_bin, tmp_path):
    vid = _make_video(ffmpeg_bin, tmp_path / "v.mp4", seconds=12)
    out = tmp_path / "frames"
    frames = run(extract_sampled_frames(ffmpeg_bin, vid, 0.0, 12.0, 1.0, 1200, out, 60.0))
    assert len(frames) == 12
    assert frames[0][0] == 0.0 and frames[-1][0] == 11.0
    assert all(p.suffix == ".jpg" and p.exists() for _, p in frames)
    # 0.5 fps halves the count
    frames2 = run(extract_sampled_frames(ffmpeg_bin, vid, 0.0, 12.0, 0.5, 1200, tmp_path / "f2", 60.0))
    assert len(frames2) == 6
    # hard cap on frames
    frames3 = run(extract_sampled_frames(ffmpeg_bin, vid, 0.0, 12.0, 2.0, 5, tmp_path / "f3", 60.0))
    assert len(frames3) == 5
    # offset window: the SAME 4 grid points, returned chunk-relative
    frames4 = run(extract_sampled_frames(ffmpeg_bin, vid, 5.0, 4.0, 1.0, 1200, tmp_path / "f4", 60.0))
    assert len(frames4) == 4 and frames4[0][0] == pytest.approx(0.0)


def test_frame_extraction_errors_honestly(ffmpeg_bin, tmp_path):
    out = tmp_path / "frames"
    with pytest.raises(FrameExtractionError):
        run(extract_sampled_frames(ffmpeg_bin, tmp_path / "missing.mp4", 0.0, 5.0, 1.0, 100, out, 10.0))
    vid = _make_video(ffmpeg_bin, tmp_path / "v.mp4", seconds=6)
    with pytest.raises(FrameExtractionError):
        run(extract_sampled_frames(ffmpeg_bin, vid, 0.0, 6.0, 1.0, 100, tmp_path / "f5", 0.001))


def test_frame_extraction_no_face_source_yields_frames(ffmpeg_bin, tmp_path):
    # A faceless source still yields frames (the MODEL then finds no faces).
    vid = _make_video(ffmpeg_bin, tmp_path / "v.mp4", seconds=4)
    out = tmp_path / "frames"
    frames = run(extract_sampled_frames(ffmpeg_bin, vid, 0.0, 4.0, 1.0, 100, out, 30.0))
    assert len(frames) == 4


# -- 8. pipeline-level integration (real orchestration path) ------------------------

from app.intel.pipeline import PipelineCancelled, PipelineConfig, StreamIntelPipeline  # noqa: E402
from app.intel.checkpoints import IntelStore  # noqa: E402
from app.intel.testing import FakeSupabaseClient  # noqa: E402
from app.intel.facecam import FacecamAnalyzer, FacecamChunkReport  # noqa: E402
from app.intel.signals import KIND_ENERGY_SPIKE as _KIND_ENERGY  # noqa: E402


class StubAnalyzer(FacecamAnalyzer):
    """Scripted analyzer for pipeline tests (no model, no frames)."""

    name = "stub"

    def __init__(self, script):
        super().__init__()
        self.script = script  # (offset, length) -> FacecamChunkReport
        self.available = True
        self.unavailable_reason = None
        self.calls: list[tuple[float, float]] = []

    async def analyze_chunk(self, ffmpeg_path, source, offset, length, out_dir,
                            should_stop=None):
        self.calls.append((offset, length))
        shutil.rmtree(out_dir, ignore_errors=True)
        return self.script(offset, length)

    async def close(self):
        return None


def _chunk_report(offset: float, length: float, bursts: list[float]) -> FacecamChunkReport:
    """A 1 fps observation grid covering the whole chunk; mouth bursts at
    the given ABSOLUTE times (else a flat neutral face)."""
    n = int(math.ceil(length))
    obs = []
    for i in range(n):
        t_rel = float(i)
        burst = any(abs((offset + t_rel) - b) < 0.05 for b in bursts)
        obs.append(_obs(t_rel, [_face(mouth=0.30 if burst else 0.03)]))
    region, _ = detect_facecam_region(obs, CFG)
    return FacecamChunkReport(
        offset=offset, duration=length, available=True, unavailable_reason=None,
        sample_fps=1.0, model="stub-model", frames_analyzed=n,
        frames_without_face=0, observations=obs, region=region,
        presence_ratio=1.0, inference_ms_total=float(n),
    )


def _store() -> tuple[IntelStore, FakeSupabaseClient]:
    client = FakeSupabaseClient()
    return IntelStore(client), client


def _cfg(**kw) -> PipelineConfig:
    base = dict(chunk_seconds=60, overlap_seconds=30, chunks_per_claim=kw.pop("claim", 2))
    base.update(kw)
    return PipelineConfig(facecam=CFG, **base)


def test_pipeline_disabled_behavior_is_unchanged(ffmpeg_bin, tmp_path):
    # cfg.facecam=None → the analyzer is NEVER called (Phase 7 guarantee).
    vid = _make_video(ffmpeg_bin, tmp_path / "v.mp4", seconds=30)
    store, client = _store()
    stub = StubAnalyzer(lambda o, l: _chunk_report(o, l, None))
    pipeline = StreamIntelPipeline(
        ffmpeg_bin, store,
        PipelineConfig(chunk_seconds=30, overlap_seconds=15), facecam_analyzer=stub)
    job = "11111111-1111-4111-8111-111111111111"
    result = run(pipeline.run(job, vid, 30.0, tmp_path))
    assert result.status == "complete"
    assert stub.calls == []          # facecam never ran
    assert result.facecam == {}      # no facecam metadata
    # no facecam rows in the store
    assert all("facecam" not in (r.get("features") or {})
               for r in client.backend.checkpoints.get(job, {}).values())


def test_pipeline_facecam_events_persist_resume_and_dedup(ffmpeg_bin, tmp_path):
    """75 s source, 60/30 chunks ([0,60), [30,75)), budget 1 per claim.

    - burst at t=10: owned by chunk 0 (upper=45);
    - burst at t=45: in BOTH chunks' grids — midpoint ownership gives it to
      chunk 1 (lower=45). The re-detection must exact-dedup, not duplicate.
    """
    job = "22222222-2222-4222-8222-222222222222"
    vid = _make_video(ffmpeg_bin, tmp_path / "v.mp4", seconds=75)
    store, client = _store()

    # BOTH chunks re-detect the burst at t=45 (it sits in the overlap):
    # chunk 0 sees [10, 45], chunk 1 sees [45]. Midpoint ownership gives
    # t=45 to chunk 1 (lower bound = 45), and the event_key exact-dedups
    # any double-persist on retry.
    def script(offset, length):
        bursts = [b for b in (10.0, 45.0) if offset <= b < offset + length]
        return _chunk_report(offset, length, bursts)

    stub = StubAnalyzer(script)
    p1 = StreamIntelPipeline(ffmpeg_bin, store, _cfg(claim=1), facecam_analyzer=stub)
    r1 = run(p1.run(job, vid, 100.0, tmp_path))
    assert r1.status == "incomplete"
    assert [c[0] for c in stub.calls] == [0.0]
    # after run 1: only t=10 persisted (t=45 not owned by chunk 0)
    assert len(client.backend.events.get(job, {})) == 1

    stub2 = StubAnalyzer(script)
    p2 = StreamIntelPipeline(ffmpeg_bin, store, _cfg(claim=1), facecam_analyzer=stub2)
    r2 = run(p2.run(job, vid, 75.0, tmp_path))
    assert r2.status == "complete"
    assert [c[0] for c in stub2.calls] == [30.0]  # chunk 0 NOT reprocessed

    fc_events = [e for e in r2.events if e.family == "facecam"]
    ts = sorted(e.t for e in fc_events)
    assert ts == [10.0, 45.0]  # t=45 exactly ONCE (ownership + dedup)
    assert len(client.backend.events.get(job, {})) == len(r2.events)  # nothing lost


def test_pipeline_facecam_unavailable_records_honest_reason(ffmpeg_bin, tmp_path):
    vid = _make_video(ffmpeg_bin, tmp_path / "v.mp4", seconds=30)
    store, client = _store()

    class UnavailableStub(FacecamAnalyzer):
        name = "unavailable-stub"

        def __init__(self):
            super().__init__()
            self.available = False
            self.unavailable_reason = "model load failed (test)"

        async def analyze_chunk(self, ffmpeg_path, source, offset, length, out_dir,
                                should_stop=None):
            raise AssertionError("unavailable analyzer must not be invoked")

        async def close(self):
            return None

    job = "33333333-3333-4333-8333-333333333333"
    p = StreamIntelPipeline(
        ffmpeg_bin, store,
        PipelineConfig(chunk_seconds=30, overlap_seconds=15, facecam=CFG),
        facecam_analyzer=UnavailableStub())
    result = run(p.run(job, vid, 30.0, tmp_path))
    assert result.status == "complete"                      # availability is NOT an error
    assert result.facecam["available"] is False
    assert result.facecam["reason"] == "model load failed (test)"
    # the honest reason is recorded in the checkpoint summary too
    feats = next(iter(client.backend.checkpoints[job].values()))["features"]
    assert feats["facecam"]["available"] is False
    assert feats["facecam"]["unavailable_reason"] == "model load failed (test)"
    assert not any(e.family == "facecam" for e in result.events)  # nothing fabricated


def test_pipeline_cancellation_no_checkpoint_written(ffmpeg_bin, tmp_path):
    vid = _make_video(ffmpeg_bin, tmp_path / "v.mp4", seconds=30)
    store, client = _store()

    class CancelStub(FacecamAnalyzer):
        name = "cancel-stub"

        def __init__(self):
            super().__init__()
            self.available = True
            self.unavailable_reason = None

        async def analyze_chunk(self, ffmpeg_path, source, offset, length, out_dir,
                                should_stop=None):
            shutil.rmtree(out_dir, ignore_errors=True)
            raise FacecamCancelled()

        async def close(self):
            return None

    p = StreamIntelPipeline(
        ffmpeg_bin, store,
        PipelineConfig(chunk_seconds=30, overlap_seconds=15, facecam=CFG),
        facecam_analyzer=CancelStub())
    with pytest.raises(PipelineCancelled):
        run(p.run("44444444-4444-4444-8444-444444444444", vid, 30.0, tmp_path))
    assert client.backend.checkpoints == {}   # no checkpoint
    assert client.backend.events == {}        # no partial events


def test_analyzer_cancellation_cleans_frames(ffmpeg_bin, tmp_path):
    cfg = FacecamConfig(detector_model_path="/irrelevant/for/cancel.tflite")
    an = MediaPipeFacecamAnalyzer(cfg)

    async def fake_create():
        return None

    an._model.create = fake_create  # no real model needed for cancellation
    vid = _make_video(ffmpeg_bin, tmp_path / "v.mp4", seconds=6)
    out = tmp_path / "fc-frames"
    with pytest.raises(FacecamCancelled):
        run(an.analyze_chunk(ffmpeg_bin, vid, 0.0, 6.0, out, should_stop=lambda: True))
    assert not out.exists()  # temp frames ALWAYS cleaned
    run(an.close())


# -- 9. real-model smoke tests (synthetic fixtures; NO accuracy claims) ---------------

def _face_video(ffmpeg_bin: str, tmp_path: Path, name: str, jpg: str, seconds: float) -> Path:
    vid = tmp_path / name
    import subprocess
    subprocess.run([ffmpeg_bin, "-v", "error", "-loop", "1", "-t", str(seconds),
                    "-i", str(FIXTURES / jpg), "-vf", "scale=1280:720",
                    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-an", str(vid)], check=True, capture_output=True)
    return vid


@REAL_MODEL
def test_real_model_face_detected(ffmpeg_bin, tmp_path):
    vid = _face_video(ffmpeg_bin, tmp_path, "face.mp4", "facecam_neutral_face.jpg", 8)
    an = MediaPipeFacecamAnalyzer(CFG)
    rep = run(an.analyze_chunk(ffmpeg_bin, vid, 0.0, 8.0, tmp_path / "fc"))
    assert rep.available is True
    assert rep.frames_analyzed == 8
    assert rep.presence_ratio >= 0.9          # static face: seen in (almost) every frame
    assert rep.region is not None             # persistent overlay -> region
    for obs in rep.observations:
        for f in obs.faces:
            assert f.confidence >= CFG.min_confidence
            assert 0.0 <= f.mouth_open <= 1.0  # measured values stay in range
    assert not (tmp_path / "fc").exists()     # frames cleaned
    run(an.close())


@REAL_MODEL
def test_real_model_no_face_is_honest_absence(ffmpeg_bin, tmp_path):
    vid = _face_video(ffmpeg_bin, tmp_path, "noface.mp4", "facecam_no_face.jpg", 6)
    an = MediaPipeFacecamAnalyzer(CFG)
    rep = run(an.analyze_chunk(ffmpeg_bin, vid, 0.0, 6.0, tmp_path / "fc"))
    assert rep.available is True              # available modality, ABSENT face
    assert rep.presence_ratio == 0.0
    assert rep.region is None
    assert detect_facecam_events(rep.observations, rep.region, CFG, 0.0) == []
    run(an.close())


@REAL_MODEL
def test_real_model_multiple_faces(ffmpeg_bin, tmp_path):
    vid = _face_video(ffmpeg_bin, tmp_path, "two.mp4", "facecam_two_faces.jpg", 6)
    an = MediaPipeFacecamAnalyzer(CFG)
    rep = run(an.analyze_chunk(ffmpeg_bin, vid, 0.0, 6.0, tmp_path / "fc"))
    assert rep.available is True
    max_faces = max(len(o.faces) for o in rep.observations)
    assert max_faces >= 2                      # both faces detected, no largest-face assumption
    run(an.close())


@REAL_MODEL
def test_real_model_timestamp_grid(ffmpeg_bin, tmp_path):
    # 40 s video so the 30 s seek lands inside it; 8 s of frames after.
    vid = _face_video(ffmpeg_bin, tmp_path, "face.mp4", "facecam_neutral_face.jpg", 40)
    an = MediaPipeFacecamAnalyzer(CFG)
    rep = run(an.analyze_chunk(ffmpeg_bin, vid, 30.0, 8.0, tmp_path / "fc"))
    ts = [o.t_rel for o in rep.observations]
    assert len(ts) == 8
    for i, t_rel in enumerate(ts):
        assert abs(t_rel - i / CFG.sample_fps) <= 0.5 / CFG.sample_fps + 0.05
    run(an.close())


@REAL_MODEL
def test_real_model_cpu_only_operation(ffmpeg_bin, tmp_path):
    vid = _face_video(ffmpeg_bin, tmp_path, "face.mp4", "facecam_neutral_face.jpg", 4)
    an = MediaPipeFacecamAnalyzer(CFG)
    rep = run(an.analyze_chunk(ffmpeg_bin, vid, 0.0, 4.0, tmp_path / "fc"))
    assert rep.available is True
    assert "CPU" in (an._model.NAME or "")     # delegate is CPU by policy
    assert rep.inference_ms_total > 0          # timing is recorded
    run(an.close())
