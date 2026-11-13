"""Real-FFmpeg subtitle burn-in integration tests (Phase 5B).

No mocks for the render path: a genuine FFmpeg subprocess burns libass
subtitles into real frames, and the tests PROVE the burn-in by pixel
analysis (RGB frames extracted at chosen timestamps):

- the verified libass \\k semantics are asserted at MULTIPLE timestamps
  (spoken words in the base color, upcoming words in the highlight color),
- a no-captions baseline proves the subtitle pixels are the only diff,
- portrait + rotated (90° ``rot `` box) sources still produce the exact
  1080x1920 contract output WITH subtitles,
- Arabic (RTL) and German (umlauts) render without error,
- ASS markup in transcribed text is escaped (no filter/override injection:
  a fake ``\\pos`` must not move the text out of the caption band),
- timeout kills the process and removes BOTH the partial mp4 and the ass
  file; cancellation does the same; success removes the ass file.

The source is a flat dark frame (0x202020) + sine audio so ANY
non-background pixel in the caption region is a subtitle pixel.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from app.rendering.base import CaptionStyle, CaptionWord, RenderSpec
from app.rendering.ffmpeg import FFmpegError
from app.rendering.renderer import FFmpegRenderer
from app.rendering.validation import verify_render_output
from tests.conftest import _run_ffmpeg, patch_mp4_rotation

BASE_RGB = (255, 255, 255)  # default base color (spoken words)
HIGHLIGHT_RGB = (0, 215, 255)  # 00D7FF (upcoming words)
TOL = 45

# 6 words -> 2 lines of 3: line1 1.0-3.0, line2 3.2-5.2
WORDS = [
    CaptionWord(word="alpha", start=1.0, end=1.6),
    CaptionWord(word="bravo", start=1.7, end=2.3),
    CaptionWord(word="charlie", start=2.4, end=3.0),
    CaptionWord(word="delta", start=3.2, end=3.8),
    CaptionWord(word="echo", start=3.9, end=4.5),
    CaptionWord(word="foxtrot", start=4.6, end=5.2),
]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def dark_sources(ffmpeg_bin: str, tmp_path_factory) -> dict[str, Path]:
    """Flat-dark H.264+AAC sources (landscape / portrait / rotated-90)."""
    out_dir = tmp_path_factory.mktemp("caption-sources")
    ffmpeg = ffmpeg_bin
    landscape = out_dir / "landscape.mp4"
    _run_ffmpeg(ffmpeg, [
        "-f", "lavfi", "-i", "color=c=0x202020:s=1920x1080:rate=30:duration=8",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(landscape),
    ])
    portrait = out_dir / "portrait.mp4"
    _run_ffmpeg(ffmpeg, [
        "-f", "lavfi", "-i", "color=c=0x202020:s=720x1280:rate=30:duration=8",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(portrait),
    ])
    rotated = out_dir / "rotated.mp4"
    rotated.write_bytes(landscape.read_bytes())
    patch_mp4_rotation(rotated, 90)
    return {"landscape": landscape, "portrait": portrait, "rotated": rotated}


def make_renderer(ffmpeg: str, ffprobe: str, timeout: float = 120.0) -> FFmpegRenderer:
    return FFmpegRenderer(ffmpeg, ffprobe, timeout_seconds=timeout)


def render_with(renderer, source: Path, spec: RenderSpec, out_dir: Path, name: str = "out.mp4") -> Path:
    out = out_dir / name
    run(renderer.render(str(source), 0.0, 8.0, spec, str(out)))
    return out


def _frame_rgb(ffmpeg: str, video: Path, t: float) -> bytes:
    p = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(video),
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    )
    return p.stdout


def _count(data: bytes, color: tuple[int, int, int], tol: int = TOL) -> int:
    n = 0
    for i in range(0, len(data), 3):
        if abs(data[i] - color[0]) <= tol and abs(data[i + 1] - color[1]) <= tol and abs(data[i + 2] - color[2]) <= tol:
            n += 1
    return n


def _band(data: bytes, y0: int, y1: int) -> bytes:
    w = 1080
    out = bytearray()
    for y in range(y0, y1):
        out += data[y * w * 3:(y + 1) * w * 3]
    return bytes(out)


def _nonbg(data: bytes, tol: int = 30) -> int:
    return sum(
        1 for i in range(0, len(data), 3)
        if abs(data[i] - 0x22) > tol or abs(data[i + 1] - 0x22) > tol or abs(data[i + 2] - 0x22) > tol
    )


# ---------------------------------------------------------------------------

def test_contract_holds_with_burned_captions(ffmpeg_bin, ffprobe_bin, dark_sources, tmp_path):
    """The Phase 5A output contract is preserved with subtitles burned in."""
    renderer = make_renderer(ffmpeg_bin, ffprobe_bin)
    spec = RenderSpec(captions=True, caption_words=tuple(WORDS))
    out = render_with(renderer, dark_sources["landscape"], spec, tmp_path)
    info = run(verify_render_output(ffprobe_bin, out, renderer.profile, expected_duration=8.0,
                                    timeout_seconds=60))
    assert (info.width, info.height) == (1080, 1920)
    assert info.video_codec == "h264" and info.audio_codec == "aac"
    assert info.pix_fmt == "yuv420p" and abs(info.rotation) <= 0.5
    # the ASS file is cleaned up after a SUCCESSFUL render
    assert not (out.parent / "captions.ass").exists()


def test_no_subtitle_pixels_before_any_line(ffmpeg_bin, ffprobe_bin, dark_sources, tmp_path):
    renderer = make_renderer(ffmpeg_bin, ffprobe_bin)
    spec = RenderSpec(captions=True, caption_words=tuple(WORDS))
    out = render_with(renderer, dark_sources["landscape"], spec, tmp_path)
    frame = _frame_rgb(ffmpeg_bin, out, 0.5)
    assert _count(frame, BASE_RGB) < 50
    assert _count(frame, HIGHLIGHT_RGB) < 50


def test_active_word_progression_at_multiple_timestamps(ffmpeg_bin, ffprobe_bin, dark_sources, tmp_path):
    """libass \\k semantics, asserted across the whole 2-line timeline."""
    renderer = make_renderer(ffmpeg_bin, ffprobe_bin)
    spec = RenderSpec(captions=True, caption_words=tuple(WORDS))
    out = render_with(renderer, dark_sources["landscape"], spec, tmp_path)

    def counts(t: float) -> tuple[int, int]:
        f = _frame_rgb(ffmpeg_bin, out, t)
        return _count(f, BASE_RGB), _count(f, HIGHLIGHT_RGB)

    # t=1.3 (word1 speaking): base=alpha only; highlight=bravo+charlie
    b1, h1 = counts(1.3)
    assert b1 > 300 and h1 > 300
    assert h1 > b1
    # t=1.9 (word2 speaking): base=alpha+bravo; highlight=charlie
    b2, h2 = counts(1.9)
    assert b2 > 300 and h2 > 300
    assert b2 > h1 * 0.5 and b2 > b1
    # t=2.7 (word3 speaking): all of line 1 spoken -> base, no highlight left
    b3, h3 = counts(2.7)
    assert b3 > b2 and h3 < 100
    # t=3.5 (line 2, word4 speaking): base=delta; highlight=echo+foxtrot
    b4, h4 = counts(3.5)
    assert b4 > 300 and h4 > 300 and h4 > b4
    # t=6.0 (after everything): no subtitle pixels at all
    b5, h5 = counts(6.0)
    assert b5 < 100 and h5 < 100


def test_captions_disabled_matches_phase5a_pixels(ffmpeg_bin, ffprobe_bin, dark_sources, tmp_path):
    """captions=False (and no words) -> no subtitle pixels; the burn-in is
    the ONLY difference from the Phase 5A render."""
    renderer = make_renderer(ffmpeg_bin, ffprobe_bin)
    out_plain = render_with(renderer, dark_sources["landscape"], RenderSpec(captions=False), tmp_path, "plain.mp4")
    out_caps = render_with(renderer, dark_sources["landscape"],
                           RenderSpec(captions=True, caption_words=tuple(WORDS)), tmp_path, "caps.mp4")
    t = 1.9
    plain = _frame_rgb(ffmpeg_bin, out_plain, t)
    caps = _frame_rgb(ffmpeg_bin, out_caps, t)
    assert _count(plain, BASE_RGB) < 50 and _count(plain, HIGHLIGHT_RGB) < 50
    assert _count(caps, BASE_RGB) > 300


def test_portrait_source_with_captions_stays_1080x1920(ffmpeg_bin, ffprobe_bin, dark_sources, tmp_path):
    renderer = make_renderer(ffmpeg_bin, ffprobe_bin)
    spec = RenderSpec(captions=True, caption_words=tuple(WORDS))
    out = render_with(renderer, dark_sources["portrait"], spec, tmp_path)
    info = run(verify_render_output(ffprobe_bin, out, renderer.profile, expected_duration=8.0,
                                    timeout_seconds=60))
    assert (info.width, info.height) == (1080, 1920)
    frame = _frame_rgb(ffmpeg_bin, out, 3.5)
    assert _count(frame, HIGHLIGHT_RGB) > 300


def test_rotated_source_with_captions_zero_rotation(ffmpeg_bin, ffprobe_bin, dark_sources, tmp_path):
    """Phone-camera case: 90° ``rot `` box -> baked upright, 0 stored
    rotation, subtitles present."""
    renderer = make_renderer(ffmpeg_bin, ffprobe_bin)
    spec = RenderSpec(captions=True, caption_words=tuple(WORDS))
    out = render_with(renderer, dark_sources["rotated"], spec, tmp_path)
    info = run(verify_render_output(ffprobe_bin, out, renderer.profile, expected_duration=8.0,
                                    timeout_seconds=60))
    assert (info.width, info.height) == (1080, 1920)
    assert abs(info.rotation) <= 0.5
    frame = _frame_rgb(ffmpeg_bin, out, 3.5)
    assert _count(frame, HIGHLIGHT_RGB) > 300


def test_arabic_rtl_and_german_umlauts_render(ffmpeg_bin, ffprobe_bin, dark_sources, tmp_path):
    words = [
        CaptionWord(word="Guten", start=1.0, end=1.8),
        CaptionWord(word="Tag", start=1.9, end=2.6),
        CaptionWord(word="مرحبا", start=3.0, end=4.0),
        CaptionWord(word="بالعالم", start=4.1, end=5.1),
    ]
    renderer = make_renderer(ffmpeg_bin, ffprobe_bin)
    spec = RenderSpec(captions=True, caption_words=tuple(words))
    out = render_with(renderer, dark_sources["landscape"], spec, tmp_path)
    run(verify_render_output(ffprobe_bin, out, renderer.profile, expected_duration=8.0, timeout_seconds=60))
    # german line visible at 2.2, arabic line visible at 3.5
    assert _nonbg(_frame_rgb(ffmpeg_bin, out, 2.2)) > 300
    assert _nonbg(_frame_rgb(ffmpeg_bin, out, 3.5)) > 300
    # nothing after the last line
    assert _nonbg(_frame_rgb(ffmpeg_bin, out, 6.0)) < 300


def test_ass_markup_in_text_is_not_injected(ffmpeg_bin, ffprobe_bin, dark_sources, tmp_path):
    """Transcribed text containing ASS override markup must render as
    PLAIN TEXT in the caption band — ``\\pos 50 50`` must not relocate it."""
    words = [
        CaptionWord(word='{\\pos 50 50}', start=1.0, end=2.0),
        CaptionWord(word="}x{\\blur", start=2.2, end=3.2),
    ]
    renderer = make_renderer(ffmpeg_bin, ffprobe_bin)
    spec = RenderSpec(captions=True, caption_words=tuple(words))
    out = render_with(renderer, dark_sources["landscape"], spec, tmp_path)
    run(verify_render_output(ffprobe_bin, out, renderer.profile, expected_duration=8.0, timeout_seconds=60))
    frame = _frame_rgb(ffmpeg_bin, out, 1.5)
    # top-left 300x300 box: must stay background (the fake \pos had no effect)
    topleft = frame[: 300 * 1080 * 3]
    assert _nonbg(topleft) < 20
    # the caption band (around MarginV 520 from the bottom): text visible
    band = _band(frame, 1920 - 520 - 180, 1920 - 520 + 80)
    assert _nonbg(band) > 100


def test_style_configuration_changes_appear_in_pixels(ffmpeg_bin, ffprobe_bin, dark_sources, tmp_path):
    """A different highlight color is observable in the rendered frames."""
    renderer = make_renderer(ffmpeg_bin, ffprobe_bin)
    red = (255, 0, 0)
    style = CaptionStyle(highlight_color="FF0000", base_color="00FF00")
    words = [CaptionWord(word="one", start=1.0, end=1.8),
             CaptionWord(word="two", start=1.9, end=2.7)]
    spec = RenderSpec(captions=True, caption_words=tuple(words), caption_style=style)
    out = render_with(renderer, dark_sources["landscape"], spec, tmp_path)
    frame = _frame_rgb(ffmpeg_bin, out, 1.5)
    assert _count(frame, red) > 300       # "two" (upcoming) glows red
    assert _count(frame, (0, 255, 0)) > 200  # "one" (spoken) is green


def test_timeout_kills_and_removes_ass_and_output(ffmpeg_bin, ffprobe_bin, dark_sources, tmp_path):
    renderer = make_renderer(ffmpeg_bin, ffprobe_bin, timeout=0.3)
    spec = RenderSpec(captions=True, caption_words=tuple(WORDS))
    out = tmp_path / "timeout.mp4"
    with pytest.raises(FFmpegError) as excinfo:
        run(renderer.render(str(dark_sources["landscape"]), 0.0, 8.0, spec, str(out)))
    assert "timed out" in str(excinfo.value)
    assert not out.exists()
    assert not (tmp_path / "captions.ass").exists()


def test_cancellation_kills_and_cleans(ffmpeg_bin, ffprobe_bin, dark_sources, tmp_path):
    renderer = make_renderer(ffmpeg_bin, ffprobe_bin, timeout=60)
    spec = RenderSpec(captions=True, caption_words=tuple(WORDS))
    out = tmp_path / "cancelled.mp4"

    async def scenario() -> None:
        task = asyncio.get_running_loop().create_task(
            renderer.render(str(dark_sources["landscape"]), 0.0, 8.0, spec, str(out))
        )
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert not out.exists()
    assert not (tmp_path / "captions.ass").exists()
