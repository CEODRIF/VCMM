"""Rendering profiles + geometry plan tests (pure math, no binaries).

Contract under test: the output is ALWAYS 1080x1920 (9:16), the source is
scaled by a SINGLE uniform factor (no distortion — the aspect ratio is
preserved), dimensions are rounded to EVEN (yuv420p), and the source
rotation tag decides the effective orientation.
"""

from __future__ import annotations

import math

import pytest

from app.rendering.profiles import (
    RenderProfile,
    effective_dimensions,
    plan_video_filter,
    select_output_fps,
)


def _scale_of(plan: str) -> tuple[int, int]:
    """Extracts the (w, h) of the scale step from a filter plan."""
    for part in plan.split(","):
        if part.startswith("scale="):
            w, h = part[len("scale="):].split(":")
            return int(w), int(h)
    raise AssertionError(f"no scale step in plan {plan!r}")


# ---- effective dimensions (rotation) -----------------------------------------

def test_effective_dimensions_rotation_90_swaps():
    assert effective_dimensions(1920, 1080, 90) == (1080, 1920)


def test_effective_dimensions_rotation_270_swaps():
    assert effective_dimensions(1920, 1080, 270) == (1080, 1920)


def test_effective_dimensions_rotation_minus_90_swaps():
    assert effective_dimensions(1920, 1080, -90) == (1080, 1920)


def test_effective_dimensions_rotation_0_and_180_keep():
    assert effective_dimensions(1920, 1080, 0) == (1920, 1080)
    assert effective_dimensions(1920, 1080, 180) == (1920, 1080)


def test_effective_dimensions_portrait_source_90_becomes_landscape():
    assert effective_dimensions(720, 1280, 90) == (1280, 720)


# ---- cover (default): scale to fill + center crop -----------------------------

def test_cover_landscape_crops_edges():
    plan = plan_video_filter(1920, 1080, 0)
    assert plan == "scale=3414:1920,crop=1080:1920"


def test_cover_portrait_is_1_1_when_exact_ratio():
    # 720x1280 is exactly 9:16 → pure upscale, crop is a no-op.
    plan = plan_video_filter(720, 1280, 0)
    assert plan == "scale=1080:1920,crop=1080:1920"


def test_cover_square_crops_sides():
    plan = plan_video_filter(640, 640, 0)
    assert plan == "scale=1920:1920,crop=1080:1920"


def test_cover_rotated_landscape_plans_as_portrait():
    # rotate=90: the source DISPLAYS as 1080x1920 → identical to a native
    # portrait source (this is what -autorotate will bake into pixels).
    assert plan_video_filter(1920, 1080, 90) == plan_video_filter(1080, 1920, 0)


# ---- contain: scale to fit + black pad ----------------------------------------

def test_contain_landscape_pads_top_and_bottom():
    plan = plan_video_filter(1920, 1080, 0, fit="contain")
    assert plan == "scale=1080:606,pad=1080:1920:(ow-1080)/2:(oh-606)/2:black"


def test_contain_portrait_exact_ratio_has_no_padding():
    plan = plan_video_filter(720, 1280, 0, fit="contain")
    assert plan == "scale=1080:1920,pad=1080:1920:(ow-1080)/2:(oh-1920)/2:black"


# ---- invariants across a matrix of sources ------------------------------------

@pytest.mark.parametrize(
    ("w", "h", "rot"),
    [
        (1920, 1080, 0),
        (1080, 1920, 0),
        (640, 640, 0),
        (1920, 1080, 90),
        (1920, 1080, 270),
        (320, 240, 0),
        (100, 1000, 0),
        (1000, 100, 0),
        (719, 1281, 0),
        (123, 456, 90),
    ],
)
def test_cover_plan_is_even_and_distortion_free(w, h, rot):
    plan = plan_video_filter(w, h, rot)
    assert plan.endswith(f"crop=1080:1920")
    sw, sh = _scale_of(plan)
    assert sw % 2 == 0 and sh % 2 == 0  # yuv420p requires even dims
    eff_w, eff_h = effective_dimensions(w, h, rot)
    # Single uniform scale: the scaled frame keeps the source aspect ratio
    # (rounding may shift the ratio by at most one even pixel).
    assert abs((sw / sh) - (eff_w / eff_h)) / (eff_w / eff_h) < 0.01
    # Cover: the scaled frame must be AT LEAST as big as the target in BOTH
    # dimensions (or it would leave gaps before the crop).
    assert sw >= 1080 and sh >= 1920


@pytest.mark.parametrize(
    ("w", "h", "rot"),
    [
        (1920, 1080, 0),
        (1080, 1920, 0),
        (640, 640, 0),
        (320, 240, 0),
        (719, 1281, 0),
        (123, 456, 90),
    ],
)
def test_contain_plan_is_even_and_distortion_free(w, h, rot):
    plan = plan_video_filter(w, h, rot, fit="contain")
    assert plan.startswith("scale=")
    assert plan.endswith("black")
    sw, sh = _scale_of(plan)
    assert sw % 2 == 0 and sh % 2 == 0
    eff_w, eff_h = effective_dimensions(w, h, rot)
    assert abs((sw / sh) - (eff_w / eff_h)) / (eff_w / eff_h) < 0.01
    # Contain: the scaled frame must FIT inside the target in BOTH dims.
    assert sw <= 1080 and sh <= 1920
    assert math.gcd(sw, sh)  # sanity: positive dims


def test_contain_padding_offsets_centered():
    plan = plan_video_filter(1920, 1080, 0, fit="contain")
    # pad=1080:1920:x:y:black with x = (1080-sw)/2, y = (1920-sh)/2.
    pad = plan.split("pad=")[1]
    out_w, out_h, x, y, color = pad.split(":")
    sw, sh = _scale_of(plan)
    assert (int(out_w), int(out_h)) == (1080, 1920)
    assert color == "black"
    assert x == f"(ow-{sw})/2"
    assert y == f"(oh-{sh})/2"
    assert (1080 - sw) % 2 == 0 and (1920 - sh) % 2 == 0  # offsets are exact


# ---- input validation -----------------------------------------------------------

def test_unknown_fit_raises():
    with pytest.raises(ValueError):
        plan_video_filter(1920, 1080, 0, fit="stretch")


def test_non_positive_source_dimensions_raise():
    with pytest.raises(ValueError):
        plan_video_filter(0, 1080, 0)
    with pytest.raises(ValueError):
        plan_video_filter(1920, -1, 0)


def test_non_positive_output_dimensions_raise():
    with pytest.raises(ValueError):
        plan_video_filter(1920, 1080, 0, out_width=0)


# ---- the fixed product profile ---------------------------------------------------

def test_profile_is_the_fixed_vertical_contract():
    p = RenderProfile()
    assert (p.width, p.height) == (1080, 1920)
    assert p.fps == 30
    assert p.video_codec == "libx264"
    assert p.pix_fmt == "yuv420p"
    assert p.audio_codec == "aac"
    assert p.mov_flags == "+faststart"
    assert p.fit == "cover"


def test_profile_video_args_encode_the_contract():
    p = RenderProfile()
    args = p.ffmpeg_video_args()
    assert args == [
        "-c:v", "libx264",
        "-preset", p.preset,
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-r", "30",
    ]
    assert p.ffmpeg_audio_args() == ["-c:a", "aac", "-b:a", "128k"]
    assert p.ffmpeg_container_args() == ["-movflags", "+faststart"]


def test_profile_video_filter_delegates_to_planner():
    p = RenderProfile()
    assert p.video_filter(1920, 1080, 0) == plan_video_filter(1920, 1080, 0, 1080, 1920, "cover")


def test_profile_contain_fit_is_selectable():
    p = RenderProfile(fit="contain")
    assert p.video_filter(1920, 1080, 0) == plan_video_filter(1920, 1080, 0, 1080, 1920, "contain")


def test_ffmpeg_video_args_fps_override():
    p = RenderProfile()
    # Default keeps the profile rate...
    assert p.ffmpeg_video_args()[-2:] == ["-r", "30"]
    # ...and a source-preserved override changes only the -r value.
    assert p.ffmpeg_video_args(60)[-2:] == ["-r", "60"]
    assert p.ffmpeg_video_args(30)[-2:] == ["-r", "30"]
    # Fractional NTSC rates format exactly — no "30.0" padding and no
    # silent rounding of 59.94 to 60.
    assert p.ffmpeg_video_args(59.94)[-2:] == ["-r", "59.94"]
    assert p.ffmpeg_video_args(23.976)[-2:] == ["-r", "23.976"]


# ---- source-preserved frame-rate selection (TASK 007, Priority 4) ------------------

def test_select_output_fps_preserves_measured_source_rate():
    # The measured rate passes through UNCLAMPED inside the operating
    # window — including fractional NTSC rates.
    assert select_output_fps(23.976) == pytest.approx(23.976)
    assert select_output_fps(24.0) == pytest.approx(24.0)
    assert select_output_fps(30.0) == pytest.approx(30.0)
    assert select_output_fps(55.0) == pytest.approx(55.0)
    assert select_output_fps(59.94) == pytest.approx(59.94)
    assert select_output_fps(60.0) == pytest.approx(60.0)


def test_select_output_fps_never_upsamples_above_source():
    # The output rate never exceeds the measured source rate: frames are
    # never duplicated to fake a higher rate (the old 24 → 30 behavior).
    for rate in (10.0, 12.0, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0):
        assert select_output_fps(rate) <= rate + 1e-9


def test_select_output_fps_caps_sources_above_the_operating_max():
    # Out-of-window sources are BOUNDED (an explicit, documented cap) —
    # in-window sources are never touched.
    assert select_output_fps(120.0) == pytest.approx(60.0)
    assert select_output_fps(65.0) == pytest.approx(60.0)
    assert select_output_fps(70.0, fps_low=25, fps_high=48) == pytest.approx(48.0)
    assert select_output_fps(45.0, fps_low=25, fps_high=48) == pytest.approx(45.0)


def test_select_output_fps_unknown_rate_falls_back_to_low():
    assert select_output_fps(None) == pytest.approx(30.0)
    assert select_output_fps(None, fps_low=25) == pytest.approx(25.0)


def test_select_output_fps_legacy_threshold_param_is_harmless():
    # The legacy two-tier parameter is accepted (settings compatibility)
    # but no longer changes the selection.
    assert select_output_fps(24.0, high_source_threshold=40.0) == pytest.approx(24.0)
    assert select_output_fps(55.0, fps_low=30, fps_high=60, high_source_threshold=55.0) == pytest.approx(55.0)
