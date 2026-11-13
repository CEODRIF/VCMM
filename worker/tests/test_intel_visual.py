"""Visual feature extraction + visual detector tests (TASK 007).

Real FFmpeg, deterministic lavfi fixtures:
- ``cut``    — testsrc → solid red with a HARD CUT at t=3.0 (scene_change);
- ``motion`` — black → testsrc(2s) → black: frame-difference surges at the
  two transitions (motion_burst);
- ``still``  — solid color only: NO cuts, NO motion (negative case).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.intel.detectors import detect_visual_events
from app.intel.visual_features import (
    VisualFeatureError,
    extract_visual_features,
)
from app.intel.signals import KIND_MOTION_BURST, KIND_SCENE_CHANGE
from conftest import _run_ffmpeg


@pytest.fixture(scope="module")
def visual_media(ffmpeg_bin, tmp_path_factory) -> dict[str, Path]:
    out_dir = tmp_path_factory.mktemp("visual-media")
    media: dict[str, Path] = {}

    # Hard cut at t=3.0 (no audio needed — the visual passes use -an).
    cut = out_dir / "cut.mp4"
    _run_ffmpeg(
        ffmpeg_bin,
        ["-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=10",
         "-f", "lavfi", "-i", "color=c=red:d=3:s=320x240:r=10",
         "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cut)],
    )
    media["cut"] = cut

    # Motion: static → moving (2 s) → static. Transitions at t=2.0 and t=4.0.
    motion = out_dir / "motion.mp4"
    _run_ffmpeg(
        ffmpeg_bin,
        ["-f", "lavfi", "-i", "color=c=black:d=2:s=320x240:r=10",
         "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
         "-f", "lavfi", "-i", "color=c=black:d=2:s=320x240:r=10",
         "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(motion)],
    )
    media["motion"] = motion

    # Still: one solid color — nothing changes, nothing should be detected.
    still = out_dir / "still.mp4"
    _run_ffmpeg(
        ffmpeg_bin,
        ["-f", "lavfi", "-i", "color=c=blue:d=6:s=320x240:r=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(still)],
    )
    media["still"] = still
    return media


# ---- scene changes ----------------------------------------------------------------

def test_scene_change_detected_at_scripted_cut(visual_media, ffmpeg_bin):
    feats = asyncio.run(extract_visual_features(ffmpeg_bin, visual_media["cut"], 0.0, 6.0, 60.0))
    assert feats.scene_changes, "the scripted hard cut must be detected"
    assert any(2.5 <= t <= 3.5 for t in feats.scene_changes), f"cut should be ~3.0, got {feats.scene_changes}"


def test_scene_change_offset_maps_to_absolute(visual_media, ffmpeg_bin):
    # Extract [2, 6): the cut at absolute 3.0 is relative 1.0 → must report 3.0.
    feats = asyncio.run(extract_visual_features(ffmpeg_bin, visual_media["cut"], 2.0, 4.0, 60.0))
    assert feats.scene_changes
    assert all(abs(t - 3.0) <= 0.75 for t in feats.scene_changes), f"expected ~3.0 absolute, got {feats.scene_changes}"


def test_still_video_has_no_scene_changes(visual_media, ffmpeg_bin):
    feats = asyncio.run(extract_visual_features(ffmpeg_bin, visual_media["still"], 0.0, 6.0, 60.0))
    assert feats.scene_changes == []


# ---- motion bursts ------------------------------------------------------------------

def test_motion_burst_at_scripted_transitions(visual_media, ffmpeg_bin):
    feats = asyncio.run(extract_visual_features(ffmpeg_bin, visual_media["motion"], 0.0, 6.0, 60.0))
    peaks = [t for t, _ in feats.motion_peaks]
    assert peaks, "static→moving transitions must produce motion peaks"
    assert any(1.7 <= t <= 2.4 for t in peaks), f"expected a peak at ~2.0, got {peaks}"
    assert any(3.7 <= t <= 4.4 for t in peaks), f"expected a peak at ~4.0, got {peaks}"


def test_still_video_has_no_motion_peaks(visual_media, ffmpeg_bin):
    feats = asyncio.run(extract_visual_features(ffmpeg_bin, visual_media["still"], 0.0, 6.0, 60.0))
    assert feats.motion_peaks == []


def test_motion_values_are_bounded(visual_media, ffmpeg_bin):
    feats = asyncio.run(extract_visual_features(ffmpeg_bin, visual_media["motion"], 0.0, 6.0, 60.0))
    for t, v in feats.motion:
        assert 0.0 <= v <= 1.0
        assert t >= 0.0  # absolute (offset 0 here)


# ---- detectors over measured features -----------------------------------------------

def test_visual_detector_emits_both_kinds_with_evidence(visual_media, ffmpeg_bin):
    feats = asyncio.run(extract_visual_features(ffmpeg_bin, visual_media["motion"], 0.0, 6.0, 60.0))
    events = detect_visual_events(feats)
    kinds = {e.kind for e in events}
    assert KIND_MOTION_BURST in kinds
    for e in events:
        assert e.family == "visual"
        assert e.t == pytest.approx(e.t_end)  # point-in-time signal
        assert 0.0 <= e.strength <= 1.0
        assert e.evidence["family"] == "visual"
        assert e.evidence["kind"] == e.kind
        assert "note" in e.evidence  # measured, explained, never fabricated


def test_visual_detector_negative_case(visual_media, ffmpeg_bin):
    feats = asyncio.run(extract_visual_features(ffmpeg_bin, visual_media["still"], 0.0, 6.0, 60.0))
    assert detect_visual_events(feats) == []


def test_scene_detector_carries_absolute_time(visual_media, ffmpeg_bin):
    feats = asyncio.run(extract_visual_features(ffmpeg_bin, visual_media["cut"], 2.0, 4.0, 60.0))
    events = [e for e in detect_visual_events(feats) if e.kind == KIND_SCENE_CHANGE]
    assert events
    assert any(abs(e.t - 3.0) <= 0.75 for e in events)


def test_storage_summary_shape(visual_media, ffmpeg_bin):
    feats = asyncio.run(extract_visual_features(ffmpeg_bin, visual_media["cut"], 0.0, 6.0, 60.0))
    summary = feats.storage_summary()
    assert summary["family"] == "visual"
    assert summary["offset"] == 0.0
    assert summary["duration"] == 6.0
    assert isinstance(summary["scene_changes"], list)
    assert isinstance(summary["motion_peaks"], list)


# ---- failure paths --------------------------------------------------------------------

def test_extract_visual_missing_source(tmp_path):
    with pytest.raises(VisualFeatureError):
        asyncio.run(extract_visual_features("ffmpeg", tmp_path / "nope.mp4", 0.0, 1.0, 10.0))


def test_extract_visual_timeout(ffmpeg_bin, visual_media, tmp_path):
    # 10 ms: the subprocess cannot finish — timeout path must raise + kill.
    with pytest.raises(VisualFeatureError):
        asyncio.run(extract_visual_features(ffmpeg_bin, visual_media["cut"], 0.0, 6.0, 0.01))
