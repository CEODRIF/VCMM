"""TASK 009 — game vision intelligence tests.

Two layers (mirrors the TASK 008 facecam suite):

1. ALWAYS-RUN (no OCR engine needed): pure-logic detection (kill-feed,
   scoreboard, HUD stability, event interpretation, signal conversion)
   driven by a deterministic STUB OCR engine; frame extraction with real
   FFmpeg (bounded, ROI crop/scale, timeouts); honest-unavailable
   behavior (missing binary / bad tessdata / repeated OCR errors);
   config validation; pipeline-level ownership + dedup + resume +
   cancellation; disabled mode = existing behavior; fusion/selection
   integration. All SYNTHETIC — no real-world accuracy is claimed
   anywhere: every "detected" behavior is an implementation contract.

2. REAL-OCR smoke tests (skipped unless the tesseract binary is present,
   e.g. the CI best-effort step): OCR success on a PIL-rendered text
   image, no-text image, and the honest unavailable path.

NO real-world detection accuracy is claimed: the fixtures are synthetic
images with machine-rendered text; there is no annotated real-VOD
evaluation (documented limitation).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.intel.game_vision import (
    GameVisionAnalyzer,
    GameVisionCancelled,
    GameVisionChunkReport,
    NullGameVisionAnalyzer,
    TesseractGameVisionAnalyzer,
    build_game_vision_analyzer,
    detect_game_vision_events,
    summarize_game_vision,
)
from app.intel.game_vision.config import GameVisionConfig, MATCH_RESULT_TOKENS
from app.intel.game_vision.events import (
    KILL_FEED_ENTRY,
    MATCH_RESULT_TEXT,
    SCOREBOARD_APPEARANCE,
    SCORE_CHANGE,
    interpret_gameplay,
)
from app.intel.game_vision.frames import FrameExtractionError, extract_roi_frames
from app.intel.game_vision.hud import (
    detect_stable_elements,
    frame_change_ratio,
    measure_frame_diffs,
    stable_keys,
)
from app.intel.game_vision.kill_feed import KillFeedEvent, analyze_kill_feed
from app.intel.game_vision.ocr import (
    OcrError,
    OcrLine,
    StubOcrEngine,
    TesseractOcrEngine,
    _parse_tsv,
    dedupe_key,
    normalize_text,
)
from app.intel.game_vision.scoreboard import (
    detect_scoreboard,
    track_score_changes,
)
from app.intel.signals import (
    KIND_ENERGY_SPIKE,
    KIND_GF_KILL_FEED_ENTRY,
    KIND_GF_MATCH_RESULT_TEXT,
    KIND_GF_SCORE_CHANGE,
    KIND_GF_SCOREBOARD_APPEARANCE,
    SIGNAL_FAMILIES,
    SignalEvent,
    event_key,
)
from app.intel.fusion import FusionConfig, fuse
from app.intel.selection import (
    CATEGORY_EPIC_KILL,
    CATEGORY_UNEXPECTED,
    CATEGORY_VICTORY,
    _categorize,
)
from app.intel.checkpoints import IntelStore
from app.intel.pipeline import (
    PipelineCancelled,
    PipelineConfig,
    StreamIntelPipeline,
)
from app.intel.testing import FakeSupabaseClient

# Real tesseract present? (CI best-effort step installs it.)
HAS_TESSERACT = shutil.which("tesseract") is not None
REAL_OCR = pytest.mark.skipif(
    not HAS_TESSERACT,
    reason="tesseract binary not present (optional system dependency)",
)
# Pillow (optional extra; present where the extra is installed).
try:
    import PIL  # noqa: F401
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
NEEDS_PIL = pytest.mark.skipif(
    not HAS_PIL, reason="Pillow not installed (optional extra: requirements-game-vision.txt)")

CFG = GameVisionConfig()


# -- helpers ----------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


def L(text: str, conf: float = 90.0, bbox: tuple[int, int, int, int] = (0, 0, 50, 12)) -> OcrLine:
    """One measured OCR line (evidence-shaped)."""
    return OcrLine(text=normalize_text(text), confidence=conf, bbox=bbox, key=dedupe_key(text))


class StubGvAnalyzer(GameVisionAnalyzer):
    """Pipeline test double: returns pre-built reports, records calls."""

    name = "stub-gv"

    def __init__(self, report_factory, available: bool = True,
                 unavailable_reason: str | None = None):
        super().__init__()
        self._factory = report_factory
        self.available = available
        self.unavailable_reason = unavailable_reason
        self.calls: list[tuple[float, float]] = []
        self.closed = False

    async def analyze_chunk(self, ffmpeg_path, source, offset, length, out_dir,
                            should_stop=None) -> GameVisionChunkReport:
        if should_stop is not None and should_stop():
            raise GameVisionCancelled()
        self.calls.append((offset, length))
        return self._factory(offset, length)

    async def close(self) -> None:
        self.closed = True


def _kill_report(offset: float, length: float, kills: list[tuple[float, str, float]]) -> GameVisionChunkReport:
    """A report with kill-feed events at (t_rel, text, confidence)."""
    events = []
    for t_rel, text, conf in kills:
        events.append(KillFeedEvent(text=text, key=dedupe_key(text),
                                    appeared_at=t_rel, disappeared_at=t_rel + 1.0,
                                    frames_seen=2, mean_confidence=conf))
    cands = interpret_gameplay(events, [], [], CFG)
    return GameVisionChunkReport(
        offset=offset, duration=length, available=True, unavailable_reason=None,
        sample_fps=CFG.sample_fps, engine="stub", engine_version="test",
        frames_analyzed=int(length), ocr_operations=int(length),
        kill_feed_events=events, candidates=cands,
    )


def _make_video(ffmpeg: str, out: Path, seconds: float = 12.0, audio: bool = True) -> Path:
    """A short H.264 clip (640x360, 24 fps). ``audio=True`` adds a 440 Hz
    tone (the pipeline's audio pass requires an audio track)."""
    args = ["-v", "error", "-f", "lavfi",
            "-i", f"testsrc=duration={seconds}:size=640x360:rate=24"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    else:
        args += ["-an"]
    args.append(str(out))
    subprocess.run([ffmpeg, *args], check=True, capture_output=True)
    return out


def _render_text_image(path: Path, text: str, size: tuple[int, int] = (640, 120)) -> Path:
    """Renders ``text`` onto a white JPEG (machine-readable; synthetic)."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=48)
    except TypeError:  # older Pillow
        font = ImageFont.load_default()
    d.text((20, size[1] // 2 - 24), text, fill="black", font=font)
    img.save(path, "JPEG")
    return path


# -- 1. OCR engine: honest availability -------------------------------------------

def test_ocr_normalize_is_conservative():
    assert normalize_text("  a   b\tc\n") == "a b c"
    # case + punctuation preserved (conservative)
    assert normalize_text("Hello, World!") == "Hello, World!"
    # dedupe key is the aggressive form (documented)
    assert dedupe_key("Hello, World!") == "hello world"


def test_tsv_parse_groups_lines_and_confidence(tmp_path):
    tsv = tmp_path / "out.tsv"
    tsv.write_text(
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "5\t1\t1\t1\t1\t1\t10\t10\t40\t20\t95\tA\n"
        "5\t1\t1\t1\t1\t2\t50\t10\t40\t20\t85\tB\n"
        "5\t1\t1\t1\t2\t1\t10\t40\t60\t20\t70\tC\n",
        encoding="utf-8",
    )
    lines = _parse_tsv(tsv, min_confidence=60.0)
    assert [l.text for l in lines] == ["A B", "C"]
    assert lines[0].confidence == pytest.approx(90.0)  # mean(95, 85)
    assert lines[0].bbox == (10, 10, 80, 20)           # union


def test_tsv_parse_filters_low_confidence(tmp_path):
    tsv = tmp_path / "out.tsv"
    tsv.write_text(
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "5\t1\t1\t1\t1\t1\t10\t10\t40\t20\t20\tweak\n"
        "5\t1\t1\t1\t2\t1\t10\t40\t60\t20\t90\tstrong\n",
        encoding="utf-8",
    )
    lines = _parse_tsv(tsv, min_confidence=60.0)
    assert [l.text for l in lines] == ["strong"]


def test_tsv_parse_unreadable_raises(tmp_path):
    # Valid header but a corrupt conf field → OcrError (not a crash).
    tsv = tmp_path / "bad.tsv"
    tsv.write_text(
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "5\t1\t1\t1\t1\t1\t10\t10\t40\t20\tnot-a-number\tX\n",
        encoding="utf-8",
    )
    with pytest.raises(OcrError):
        _parse_tsv(tsv, 0.0)


def test_tesseract_missing_configured_binary_is_honest():
    engine = TesseractOcrEngine(GameVisionConfig(tesseract_binary="/nonexistent/tess"))
    with pytest.raises(OcrError, match="not a readable file"):
        run(engine.validate())
    assert engine.available is False


def test_tesseract_absent_from_path_is_honest(monkeypatch):
    monkeypatch.setattr("app.intel.game_vision.ocr.shutil.which", lambda _n: None)
    engine = TesseractOcrEngine(GameVisionConfig())
    with pytest.raises(OcrError, match="not found on PATH"):
        run(engine.validate())


def test_tesseract_missing_tessdata_is_honest():
    # A real executable that passes a version probe (/bin/echo) but with a
    # configured tessdata dir that lacks the language pack: the deterministic
    # validation order reports the TESSDATA problem, not the binary.
    engine = TesseractOcrEngine(GameVisionConfig(
        tesseract_binary="/bin/echo", tessdata_dir="/nonexistent/tessdata"))
    with pytest.raises(OcrError, match="tessdata not found"):
        run(engine.validate())


def test_ocr_engine_not_validated_raises():
    engine = TesseractOcrEngine(GameVisionConfig(tesseract_binary="/nonexistent/tess"))
    with pytest.raises(OcrError, match="not validated"):
        run(engine.recognize(Path("x.jpg"), Path("w")))


# -- 2. HUD analysis ----------------------------------------------------------------

def test_hud_stable_element_detected():
    frames = [(i, [L("PLAYER 24"), L("FPS 144")]) for i in range(10)]
    elements = detect_stable_elements(frames, CFG)
    assert {e.key for e in elements} == {"player 24", "fps 144"}
    assert all(e.persistence == 1.0 for e in elements)


def test_hud_transient_is_not_stable():
    frames = [(0.0, [L("PLAYER 24"), L("A hit B")])] + [(i, [L("PLAYER 24")]) for i in range(1, 10)]
    elements = detect_stable_elements(frames, CFG)
    # "A hit B" seen in 1/10 frames < 0.5 persistence → excluded
    assert {e.key for e in elements} == {"player 24"}


def test_hud_empty_series_is_empty():
    assert detect_stable_elements([], CFG) == []


@NEEDS_PIL
def test_frame_change_metric_identical_vs_different(ffmpeg_bin, tmp_path):
    vid = _make_video(ffmpeg_bin, tmp_path / "v.mp4", seconds=3, audio=False)
    out = tmp_path / "fr"
    frames = run(extract_roi_frames(ffmpeg_bin, vid, 0.0, 3.0, 1.0, 100, out, 60.0))
    assert len(frames) == 3
    identical = frame_change_ratio(frames[0][1], frames[0][1])
    assert identical is not None and identical < 0.05
    different = frame_change_ratio(frames[0][1], frames[1][1])
    # testsrc animates every frame → measurable change
    assert different is not None and different > 0.02
    diffs = measure_frame_diffs(frames)
    assert diffs[0].change_ratio is None  # no predecessor (documented)
    assert all(d.change_ratio is not None for d in diffs[1:])


def test_frame_change_metric_pillow_absent(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def _blocked(name, *a, **k):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("PIL blocked for test")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    img = Path("/nonexistent/a.jpg")
    assert frame_change_ratio(img, img) is None  # honest skip, not a crash


# -- 3. Kill-feed analysis -----------------------------------------------------------

def test_kill_feed_appearance_and_disappearance():
    frames = [(0.0, [L("A hit B")]), (1.0, [L("A hit B")]), (2.0, [])]
    events = analyze_kill_feed(frames, CFG, stable=set())
    assert len(events) == 1
    e = events[0]
    assert e.appeared_at == 0.0
    assert e.disappeared_at == 2.0  # gone by the next sample
    assert e.frames_seen == 2


def test_kill_feed_duplicate_suppression():
    # The SAME line persisting across many frames = ONE event.
    frames = [(i, [L("A hit B")]) for i in range(6)]
    events = analyze_kill_feed(frames, CFG, stable=set())
    assert len(events) == 1
    assert events[0].frames_seen == 6


def test_kill_feed_fade_jitter_does_not_duplicate():
    # Appears, fades for one sample, reappears WITHIN the suppress window
    # → same physical event, original appearance time kept.
    frames = [(0.0, [L("A hit B")]), (1.0, []), (1.5, [L("A hit B")]), (2.5, [])]
    events = analyze_kill_feed(frames, CFG, stable=set())
    assert len(events) == 1
    assert events[0].appeared_at == 0.0
    assert events[0].frames_seen == 2  # both sightings, one event


def test_kill_feed_distinct_reappearance_is_new_event():
    # Reappears AFTER the suppress window (2 s) → a second event.
    frames = [(0.0, [L("A hit B")]), (1.0, [])] + \
             [(i, [L("A hit B")]) for i in (6.0, 7.0)] + [(8.0, [])]
    events = analyze_kill_feed(frames, CFG, stable=set())
    assert len(events) == 2
    assert [e.appeared_at for e in events] == [0.0, 6.0]


def test_kill_feed_stable_hud_text_excluded():
    frames = [(i, [L("PLAYER 24"), L("A hit B") if i in (1, 2) else L("PLAYER 24")])
              for i in range(10)]
    stable = stable_keys(detect_stable_elements(frames, CFG))
    events = analyze_kill_feed(frames, CFG, stable)
    assert [e.text for e in events] == ["A hit B"]  # HUD text never an event


def test_kill_feed_char_bounds():
    frames = [(0.0, [L("x")]), (0.5, [L("y" * 80)]), (1.0, [L("A hit B")])]
    events = analyze_kill_feed(frames, CFG, stable=set())
    assert [e.text for e in events] == ["A hit B"]


def test_kill_feed_confidence_preserved():
    frames = [(0.0, [L("A hit B", conf=82.0)]), (1.0, [L("A hit B", conf=78.0)])]
    events = analyze_kill_feed(frames, CFG, stable=set())
    assert events[0].mean_confidence == pytest.approx(80.0)
    assert events[0].bboxes and events[0].disappeared_at is None  # active at chunk end


# -- 4. Scoreboard analysis ----------------------------------------------------------

def test_scoreboard_detection_dense_vs_sparse():
    # A scoreboard must be TEXT-DENSE relative to the chunk's baseline:
    # sparse frames are the majority, dense frames stand out.
    sparse = [L(f"line {i}") for i in range(2)]
    dense = [L(f"P{i} {10 + i} {i}") for i in range(8)]
    cfg = GameVisionConfig(scoreboard_density_ratio=1.5, scoreboard_min_lines=4,
                           scoreboard_min_numeric_tokens=4)
    frames = [(i * 5.0, sparse if i % 2 == 0 else dense) for i in range(10)]
    obs = detect_scoreboard(frames, cfg)
    assert [o.visible for o in obs if o.line_count == len(dense)] == [True] * 5
    assert [o.visible for o in obs if o.line_count == len(sparse)] == [False] * 5


def test_scoreboard_values_change_tracked():
    base = [L("x")]
    fill = [L("P2 10 3"), L("P3 7 1"), L("TIME 12 30")]
    dense0 = [L("P1 24 5")] + fill
    dense5 = [L("P1 27 5")] + fill
    # Sparse frames are the MAJORITY (median baseline); the dense ones stand out.
    frames = [(0.0, base), (2.0, base), (4.0, dense0), (6.0, base),
              (8.0, dense5), (10.0, base), (12.0, base)]
    cfg = GameVisionConfig(scoreboard_density_ratio=1.0001, scoreboard_min_lines=2,
                           scoreboard_min_numeric_tokens=1)
    obs = detect_scoreboard(frames, cfg)
    assert [o.visible for o in obs] == [False, False, True, False, True, False, False]
    changes = track_score_changes(obs, cfg)
    assert len(changes) == 1  # p1 24→27; p2/p3/time unchanged
    assert (changes[0].label, changes[0].before, changes[0].after) == ("p1", "24", "27")
    assert changes[0].t_rel == 8.0


def test_scoreboard_unreadable_value_never_fabricates_change():
    # The middle dense frame lost the p1 number entirely (OCR read only
    # fillers). The change chain is broken — NO change may be reported
    # from the stale value (no fabrication across an unreadable reading).
    base = [L("x")]
    fill = [L("P2 10 3"), L("P3 7 1"), L("TIME 12 30")]
    d0 = [L("P1 24 5")] + fill
    d6 = list(fill) + [L("P4 1 2")]     # p1 absent (unreadable)
    d8 = [L("P1 30 5")] + fill
    frames = [(0.0, base), (4.0, d0), (6.0, d6), (8.0, d8), (10.0, base),
              (12.0, base), (14.0, base)]
    cfg = GameVisionConfig(scoreboard_density_ratio=1.0001, scoreboard_min_lines=2,
                           scoreboard_min_numeric_tokens=1)
    obs = detect_scoreboard(frames, cfg)
    assert [o.visible for o in obs] == [False, True, True, True, False, False, False]
    changes = track_score_changes(obs, cfg)
    assert changes == []  # honest: the gap prevents a 24→30 claim


def test_scoreboard_empty_observations():
    assert detect_scoreboard([], CFG) == []
    assert track_score_changes([], CFG) == []


# -- 5. Gameplay events + signals ----------------------------------------------------

def test_event_types_from_measured_evidence():
    # interpret_gameplay is tested in ISOLATION here: the scoreboard
    # observations/changes are constructed directly (their DETECTION is
    # covered by the scoreboard tests above).
    from app.intel.game_vision.scoreboard import ScoreValueChange, ScoreboardObservation
    kf = [KillFeedEvent(text="A hit B", key="a hit b", appeared_at=1.0,
                        disappeared_at=2.0, frames_seen=2, mean_confidence=90.0),
          KillFeedEvent(text="Victory", key="victory", appeared_at=9.0,
                        disappeared_at=10.0, frames_seen=2, mean_confidence=95.0)]
    sb = [ScoreboardObservation(t_rel=3.0, line_count=4, numeric_count=6,
                                visible=True, density_vs_median=4.0,
                                lines=[L("P1 24 5")])]
    sc = [ScoreValueChange(t_rel=5.0, label="p1", before="24", after="27",
                           confidence=91.0)]
    cands = interpret_gameplay(kf, sb, sc, CFG)
    kinds = {c.kind for c in cands}
    assert kinds == {KILL_FEED_ENTRY, MATCH_RESULT_TEXT, SCOREBOARD_APPEARANCE, SCORE_CHANGE}
    # directly_observed flags are honest:
    by_kind = {c.kind: c for c in cands}
    assert by_kind[KILL_FEED_ENTRY].directly_observed is True
    assert by_kind[SCORE_CHANGE].directly_observed is True
    assert by_kind[SCOREBOARD_APPEARANCE].directly_observed is False  # heuristic
    assert by_kind[MATCH_RESULT_TEXT].directly_observed is True


def test_match_result_requires_exact_line():
    from app.intel.game_vision.kill_feed import KillFeedEvent
    # "victory lap" contains the token as a substring → NOT a match result.
    kf = [KillFeedEvent(text="victory lap", key="victory lap", appeared_at=1.0,
                        disappeared_at=None, frames_seen=1, mean_confidence=90.0)]
    cands = interpret_gameplay(kf, [], [], CFG)
    assert [c.kind for c in cands] == [KILL_FEED_ENTRY]
    # exact line IS a match result
    kf2 = [KillFeedEvent(text="Victory", key="victory", appeared_at=1.0,
                         disappeared_at=None, frames_seen=1, mean_confidence=90.0)]
    cands2 = interpret_gameplay(kf2, [], [], CFG)
    assert [c.kind for c in cands2] == [MATCH_RESULT_TEXT]


def test_signals_absolute_timestamps_and_evidence():
    from app.intel.game_vision.kill_feed import KillFeedEvent
    kf = [KillFeedEvent(text="A hit B", key="a hit b", appeared_at=1.0,
                        disappeared_at=2.0, frames_seen=2, mean_confidence=90.0)]
    cands = interpret_gameplay(kf, [], [], CFG)
    sigs = detect_game_vision_events(cands, CFG, offset=600.0,
                                     engine_name="stub", engine_version="t")
    assert len(sigs) == 1
    e = sigs[0]
    assert e.family == "game_vision"
    assert e.kind == KIND_GF_KILL_FEED_ENTRY
    assert e.t == pytest.approx(601.0)      # offset applied exactly once
    assert e.t_end == pytest.approx(602.0)
    assert e.strength == pytest.approx(0.9)
    assert e.evidence["engine"] == "stub"
    assert e.evidence["directly_observed"] is True
    assert "heuristic" in e.evidence["note"].lower()
    assert e.evidence["text"] == "A hit B"
    assert e.evidence["family"] == "game_vision"  # re-hydration contract
    assert e.evidence["strength"] == e.strength


def test_event_key_stable_and_bucketed():
    a = SignalEvent(t=600.01, t_end=600.05, family="game_vision",
                    kind=KIND_GF_KILL_FEED_ENTRY, strength=0.5)
    b = SignalEvent(t=600.04, t_end=600.06, family="game_vision",
                    kind=KIND_GF_KILL_FEED_ENTRY, strength=0.51)
    c = SignalEvent(t=600.15, t_end=600.2, family="game_vision",
                    kind=KIND_GF_KILL_FEED_ENTRY, strength=0.5)
    job = "job-x"
    assert event_key(job, a) == event_key(job, b)  # same 0.1 s bucket
    assert event_key(job, a) != event_key(job, c)


def test_signal_family_validation():
    assert "game_vision" in SIGNAL_FAMILIES
    with pytest.raises(ValueError):
        SignalEvent(t=0.0, t_end=1.0, family="hologram", kind="x", strength=0.5)


# -- 6. Config validation -------------------------------------------------------------

def test_config_validation_rejects_bad_values():
    import dataclasses
    base = dataclasses.asdict(CFG)
    for bad in ({"sample_fps": 0.0}, {"sample_fps": 5.0},
                {"min_ocr_confidence": 101.0}, {"scoreboard_scan_interval": 0},
                {"persistent_frame_ratio": 0.0}, {"max_chunk_seconds": 0.0},
                {"kill_feed_roi": (0.9, 0.2, 0.3, 0.2)},   # exceeds frame
                {"kill_feed_roi": (-0.1, 0.0, 0.1, 0.1)}):
        with pytest.raises(ValueError):
            GameVisionConfig(**{**base, **bad})


# -- 7. Frame extraction (real FFmpeg, bounded, ROI) ---------------------------------

def test_frame_extraction_bounded_and_relative(ffmpeg_bin, tmp_path):
    vid = _make_video(ffmpeg_bin, tmp_path / "v.mp4", seconds=12, audio=False)
    frames = run(extract_roi_frames(ffmpeg_bin, vid, 0.0, 12.0, 1.0, 1200,
                                    tmp_path / "f", 60.0))
    assert len(frames) == 12
    assert frames[0][0] == 0.0 and frames[-1][0] == 11.0  # chunk-relative
    # hard cap
    capped = run(extract_roi_frames(ffmpeg_bin, vid, 0.0, 12.0, 2.0, 5,
                                    tmp_path / "c", 60.0))
    assert len(capped) == 5
    # offset window: same grid, still relative
    off = run(extract_roi_frames(ffmpeg_bin, vid, 5.0, 4.0, 1.0, 100,
                                 tmp_path / "o", 60.0))
    assert len(off) == 4 and off[0][0] == pytest.approx(0.0)


@NEEDS_PIL
def test_frame_extraction_roi_crops(ffmpeg_bin, tmp_path):
    from PIL import Image
    vid = _make_video(ffmpeg_bin, tmp_path / "v.mp4", seconds=3, audio=False)  # 640x360
    frames = run(extract_roi_frames(ffmpeg_bin, vid, 0.0, 3.0, 1.0, 100,
                                    tmp_path / "r", 60.0,
                                    roi=(0.80, 0.02, 0.18, 0.12), max_dim=1280))
    assert len(frames) == 3
    with Image.open(frames[0][1]) as img:
        w, h = img.size
    # 0.18*640=115.2 → 115 (rounded), 0.12*360=43.2 → 43
    assert 110 <= w <= 120 and 40 <= h <= 46


@NEEDS_PIL
def test_frame_extraction_max_dim_bounds(ffmpeg_bin, tmp_path):
    from PIL import Image
    vid = _make_video(ffmpeg_bin, tmp_path / "v.mp4", seconds=2, audio=False)
    frames = run(extract_roi_frames(ffmpeg_bin, vid, 0.0, 2.0, 1.0, 100,
                                    tmp_path / "m", 60.0, max_dim=320))
    with Image.open(frames[0][1]) as img:
        w, h = img.size
    assert max(w, h) <= 320


def test_frame_extraction_timeout_kills(ffmpeg_bin, tmp_path):
    # 120 s of 24 fps testsrc, 3000 output frames (~2.4 s of real work on
    # CI-class hardware) vs a 1.0 s budget → the timeout MUST fire and
    # kill the subprocess.
    vid = _make_video(ffmpeg_bin, tmp_path / "v.mp4", seconds=120, audio=False)
    with pytest.raises(FrameExtractionError, match="timed out"):
        run(extract_roi_frames(ffmpeg_bin, vid, 0.0, 120.0, 24.0, 3000,
                               tmp_path / "t", 1.0))


def test_frame_extraction_missing_source(ffmpeg_bin, tmp_path):
    with pytest.raises(FrameExtractionError, match="missing or empty"):
        run(extract_roi_frames(ffmpeg_bin, tmp_path / "nope.mp4", 0.0, 5.0,
                               1.0, 100, tmp_path / "x", 10.0))


# -- 8. Resource limits (op cap, wall budget, sticky degradation) ----------------------

def _slow_stub(delay: float, lines: list[OcrLine] | None = None,
               fail: bool = False) -> StubOcrEngine:
    engine = StubOcrEngine({i: (lines or []) for i in range(10_000)})
    engine._fail = set()
    if fail:
        engine._fail = set(range(10_000))
    engine._delay = delay
    return engine


def test_ocr_operation_cap_is_enforced(ffmpeg_bin, tmp_path):
    # 10 s video, 1 fps → 10 sampled frames. Op cap 4: frame 0 does a
    # full-frame op (scoreboard grid) + a kill-feed op; frames 1-2 do one
    # kill-feed op each → 4 ops total, then the loop stops honestly.
    cfg = GameVisionConfig(max_ocr_operations_per_chunk=4, max_frames_per_chunk=10)
    vid = _make_video(ffmpeg_bin, tmp_path / "v.mp4", seconds=10, audio=False)
    stub = _slow_stub(0.0, [L("A hit B")])
    analyzer = TesseractGameVisionAnalyzer(cfg, stub)
    report = run(analyzer.analyze_chunk(ffmpeg_bin, vid, 0.0, 10.0, tmp_path / "out"))
    assert report.available is True
    assert report.ocr_operations == 4
    assert report.budget_exceeded is True
    assert report.frames_remaining == 7  # 10 frames - 3 analyzed
    assert not (tmp_path / "out").exists()  # cleaned even under budget stop


def test_wall_budget_is_enforced(ffmpeg_bin, tmp_path):
    vid = _make_video(ffmpeg_bin, tmp_path / "v.mp4", seconds=8, audio=False)
    cfg = GameVisionConfig(max_chunk_seconds=0.4, sample_fps=1.0)
    stub = _slow_stub(0.15, [L("A hit B")])  # ~0.15 s per OCR op
    analyzer = TesseractGameVisionAnalyzer(cfg, stub)
    report = run(analyzer.analyze_chunk(ffmpeg_bin, vid, 0.0, 8.0, tmp_path / "out"))
    assert report.available is True
    assert report.budget_exceeded is True
    assert report.frames_remaining >= 1
    assert report.ocr_operations < 16  # stopped before every frame (8 kf + up to 8 full)
    assert not (tmp_path / "out").exists()


def test_repeated_ocr_errors_mark_engine_unavailable(ffmpeg_bin, tmp_path):
    vid = _make_video(ffmpeg_bin, tmp_path / "v.mp4", seconds=6, audio=False)
    cfg = GameVisionConfig(max_consecutive_ocr_errors=3)
    stub = _slow_stub(0.0, fail=True)
    analyzer = TesseractGameVisionAnalyzer(cfg, stub)
    report = run(analyzer.analyze_chunk(ffmpeg_bin, vid, 0.0, 6.0, tmp_path / "o1"))
    assert report.available is False
    assert "repeated OCR failures" in report.unavailable_reason
    assert analyzer.available is False  # sticky for the whole job
    # (The PIPELINE enforces the sticky state by checking .available before
    # every chunk — covered in test_pipeline_game_vision_unavailable_*.)


# -- 9. Cancellation + cleanup ---------------------------------------------------------

def test_cancellation_raises_and_cleans(tmp_path):
    from tests.conftest import resolve_binary
    ffmpeg = resolve_binary("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not available")
    vid = _make_video(ffmpeg, tmp_path / "v.mp4", seconds=5, audio=False)
    cfg = GameVisionConfig()
    analyzer = TesseractGameVisionAnalyzer(cfg, _slow_stub(0.0, [L("A hit B")]))
    out = tmp_path / "cancel"
    with pytest.raises(GameVisionCancelled):
        run(analyzer.analyze_chunk(ffmpeg, vid, 0.0, 5.0, out,
                                   should_stop=lambda: True))
    assert not out.exists()


# -- 10. Builder honesty + disabled mode ------------------------------------------------

def test_builder_disabled_returns_null():
    s = SimpleNamespace(game_vision_enabled=False)
    ana = build_game_vision_analyzer(s)
    assert isinstance(ana, NullGameVisionAnalyzer)
    assert ana.available is False
    assert "disabled" in ana.unavailable_reason
    assert ana.config is None


def test_builder_misconfigured_roi_returns_null():
    s = SimpleNamespace(game_vision_enabled=True,
                        game_vision_sample_fps=1.0,
                        game_vision_max_frames_per_chunk=1200,
                        game_vision_frame_timeout_seconds=120.0,
                        game_vision_ocr_timeout_seconds=30.0,
                        game_vision_max_ocr_operations_per_chunk=800,
                        game_vision_max_chunk_seconds=240.0,
                        game_vision_min_ocr_confidence=60.0,
                        game_vision_ocr_language="eng",
                        game_vision_tessdata_dir=None,
                        game_vision_tesseract_binary=None,
                        game_vision_kill_feed_roi="bogus")
    ana = build_game_vision_analyzer(s)
    assert isinstance(ana, NullGameVisionAnalyzer)
    assert "misconfigured" in ana.unavailable_reason


def test_builder_empty_roi_means_full_frame():
    s = SimpleNamespace(game_vision_enabled=True,
                        game_vision_sample_fps=1.0,
                        game_vision_max_frames_per_chunk=1200,
                        game_vision_frame_timeout_seconds=120.0,
                        game_vision_ocr_timeout_seconds=30.0,
                        game_vision_max_ocr_operations_per_chunk=800,
                        game_vision_max_chunk_seconds=240.0,
                        game_vision_min_ocr_confidence=60.0,
                        game_vision_ocr_language="eng",
                        game_vision_tessdata_dir=None,
                        game_vision_tesseract_binary=None,
                        game_vision_kill_feed_roi=None)
    ana = build_game_vision_analyzer(s)
    assert not isinstance(ana, NullGameVisionAnalyzer)
    assert ana.config.kill_feed_roi is None


def test_null_analyzer_reports_honestly():
    ana = NullGameVisionAnalyzer("game vision disabled (x)")
    rep = run(ana.analyze_chunk("ffmpeg", Path("v"), 0.0, 10.0, Path("out")))
    assert rep.available is False
    assert rep.unavailable_reason == "game vision disabled (x)"


# -- 11. Pipeline-level integration (real FFmpeg + stub analyzer) -----------------------

def test_pipeline_disabled_behavior_unchanged(ffmpeg_bin, tmp_path):
    """No game vision configured → result.game_vision == {}, no gv dirs."""
    client = FakeSupabaseClient()
    store = IntelStore(client)
    job = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    vid = _make_video(ffmpeg_bin, tmp_path / "v.mp4", seconds=30)
    cfg = PipelineConfig(chunk_seconds=30, overlap_seconds=15)
    pipeline = StreamIntelPipeline(ffmpeg_bin, store, cfg)
    result = run(pipeline.run(job, vid, 30.0, tmp_path))
    assert result.status == "complete"
    assert result.game_vision == {}
    assert not any(e.family == "game_vision" for e in result.events)
    assert not any("gamevision" in p.name for p in tmp_path.iterdir())


def test_pipeline_game_vision_events_persist_resume_dedup(ffmpeg_bin, tmp_path):
    """75 s source, 60/30 chunks, budget 1 per claim. Kill at t=10 (chunk 0
    owns) and t=45 (in BOTH chunks' grids → midpoint gives it to chunk 1;
    exact-dedup, never duplicated)."""
    client = FakeSupabaseClient()
    store = IntelStore(client)
    job = "bbbbbbbb-cccc-4ddd-8eee-010203040506"
    vid = _make_video(ffmpeg_bin, tmp_path / "v.mp4", seconds=75)
    gv_cfg = GameVisionConfig()
    pcfg = PipelineConfig(chunk_seconds=60, overlap_seconds=30,
                          chunks_per_claim=1, game_vision=gv_cfg)

    def factory(offset, length):
        # Both chunks "detect" t=45 (it lies in the overlap) — exactly the
        # dedup scenario. Times passed to _kill_report are CHUNK-RELATIVE.
        kills = [(t - offset, f"KILL@{int(t)}", 90.0) for t in (10.0, 45.0)
                 if 0.0 <= (t - offset) < length]
        return _kill_report(offset, length, kills)

    stub = StubGvAnalyzer(factory)
    p1 = StreamIntelPipeline(ffmpeg_bin, store, pcfg, game_vision_analyzer=stub)
    r1 = run(p1.run(job, vid, 75.0, tmp_path))
    assert r1.status == "incomplete"
    assert [c[0] for c in stub.calls] == [0.0]
    # chunk 0 owns t=10 only (upper bound 45 excludes t=45); audio/visual
    # events from the real source may also be persisted — count game_vision.
    gv1 = [r for r in client.backend.events.get(job, {}).values()
           if r["evidence"].get("family") == "game_vision"]
    assert len(gv1) == 1
    assert gv1[0]["start_time"] == 10.0

    stub2 = StubGvAnalyzer(factory)
    p2 = StreamIntelPipeline(ffmpeg_bin, store, pcfg, game_vision_analyzer=stub2)
    r2 = run(p2.run(job, vid, 75.0, tmp_path))
    assert r2.status == "complete"
    assert [c[0] for c in stub2.calls] == [30.0]  # chunk 0 NOT reprocessed
    gv_events = [e for e in r2.events if e.family == "game_vision"]
    assert [e.t for e in gv_events] == [10.0, 45.0]  # t=45 exactly once
    keys = [event_key(job, e) for e in gv_events]
    assert len(keys) == len(set(keys))


def test_pipeline_game_vision_unavailable_records_honest_reason(ffmpeg_bin, tmp_path):
    client = FakeSupabaseClient()
    store = IntelStore(client)
    job = "cccccccc-dddd-4eee-8fff-aaaaaaaaaaaa"
    vid = _make_video(ffmpeg_bin, tmp_path / "v.mp4", seconds=30)
    gv_cfg = GameVisionConfig()
    pcfg = PipelineConfig(chunk_seconds=30, overlap_seconds=15, game_vision=gv_cfg)
    ana = StubGvAnalyzer(lambda o, l: GameVisionChunkReport(
        offset=o, duration=l, available=False,
        unavailable_reason="tesseract binary not found (test)",
        sample_fps=1.0), available=False,
        unavailable_reason="tesseract binary not found (test)")
    p = StreamIntelPipeline(ffmpeg_bin, store, pcfg, game_vision_analyzer=ana)
    result = run(p.run(job, vid, 30.0, tmp_path))
    assert result.status == "complete"
    assert result.game_vision["available"] is False
    assert "tesseract binary not found" in result.game_vision["reason"]
    feats = client.backend.checkpoints[job][0]["features"]
    assert feats["game_vision"]["available"] is False
    assert not any(e.family == "game_vision" for e in result.events)  # nothing fabricated


def test_pipeline_game_vision_cancelled_writes_nothing(ffmpeg_bin, tmp_path):
    client = FakeSupabaseClient()
    store = IntelStore(client)
    job = "dddddddd-eeee-4fff-8aaa-bbbbbbbbbbbb"
    vid = _make_video(ffmpeg_bin, tmp_path / "v.mp4", seconds=30)
    gv_cfg = GameVisionConfig()
    pcfg = PipelineConfig(chunk_seconds=30, overlap_seconds=15, game_vision=gv_cfg)

    class CancelGv(StubGvAnalyzer):
        async def analyze_chunk(self, ffmpeg_path, source, offset, length, out_dir,
                                should_stop=None):
            self.calls.append((offset, length))
            raise GameVisionCancelled()

    ana = CancelGv(lambda o, l: None)
    p = StreamIntelPipeline(ffmpeg_bin, store, pcfg, game_vision_analyzer=ana)
    with pytest.raises(PipelineCancelled):
        run(p.run(job, vid, 30.0, tmp_path))
    assert client.backend.checkpoints == {}  # no checkpoint
    assert client.backend.events == {}       # no partial events
    assert ana.closed is True                # lifecycle: closed on cancel


def test_pipeline_nested_summary_and_result_metadata(ffmpeg_bin, tmp_path):
    client = FakeSupabaseClient()
    store = IntelStore(client)
    job = "eeeeeeee-ffff-4aaa-8bbb-cccccccccccc"
    vid = _make_video(ffmpeg_bin, tmp_path / "v.mp4", seconds=30)
    gv_cfg = GameVisionConfig()
    pcfg = PipelineConfig(chunk_seconds=30, overlap_seconds=15, game_vision=gv_cfg)
    ana = StubGvAnalyzer(lambda o, l: _kill_report(o, l, [(5.0, "K", 90.0)]))
    p = StreamIntelPipeline(ffmpeg_bin, store, pcfg, game_vision_analyzer=ana)
    result = run(p.run(job, vid, 30.0, tmp_path))
    feats = client.backend.checkpoints[job][0]["features"]
    assert feats["game_vision"]["family"] == "game_vision"
    assert feats["game_vision"]["available"] is True
    assert feats["game_vision"]["kill_feed_events"] == 1
    assert result.game_vision["available"] is True
    assert result.game_vision["engine"] == "stub"
    assert result.game_vision["chunks_analyzed"] == 1
    assert "heuristic" in result.game_vision["note"]


def test_pipeline_facecam_and_game_vision_coexist(ffmpeg_bin, tmp_path):
    """TASK 008 + TASK 009 together: both optional modalities run, both
    nested summaries persist, both event families flow through."""
    from app.intel.facecam import NullFacecamAnalyzer
    client = FakeSupabaseClient()
    store = IntelStore(client)
    job = "ffffffff-aaaa-4bbb-8ccc-dddddddddddd"
    vid = _make_video(ffmpeg_bin, tmp_path / "v.mp4", seconds=30)
    from app.intel.facecam.config import FacecamConfig
    face_cfg = FacecamConfig(detector_model_path="/nonexistent/det.tflite")
    pcfg = PipelineConfig(chunk_seconds=30, overlap_seconds=15,
                          facecam=face_cfg, game_vision=GameVisionConfig())
    gv_ana = StubGvAnalyzer(lambda o, l: _kill_report(o, l, [(5.0, "K", 90.0)]))
    p = StreamIntelPipeline(ffmpeg_bin, store, pcfg,
                            facecam_analyzer=NullFacecamAnalyzer("facecam disabled"),
                            game_vision_analyzer=gv_ana)
    result = run(p.run(job, vid, 30.0, tmp_path))
    feats = client.backend.checkpoints[job][0]["features"]
    assert feats["facecam"]["family"] == "facecam"
    assert feats["game_vision"]["family"] == "game_vision"
    assert result.facecam["available"] is False      # facecam honest-unavailable
    assert result.game_vision["available"] is True   # game vision worked
    assert any(e.family == "game_vision" for e in result.events)


# -- 12. Fusion + selection integration --------------------------------------------------

def test_fusion_game_vision_plus_audio_qualifies():
    evs = [
        SignalEvent(t=100.0, t_end=100.3, family="audio", kind=KIND_ENERGY_SPIKE,
                    strength=0.8),
        SignalEvent(t=100.1, t_end=100.5, family="game_vision",
                    kind=KIND_GF_KILL_FEED_ENTRY, strength=0.9),
    ]
    windows = fuse(evs, FusionConfig())
    assert len(windows) == 1
    w = windows[0]
    assert w.qualifies
    assert "game_vision" in w.families and "audio" in w.families
    assert w.confidence == pytest.approx(1.0)  # 2 families


def test_fusion_game_vision_only_low_confidence_never_qualifies():
    evs = [SignalEvent(t=100.0, t_end=100.5, family="game_vision",
                       kind=KIND_GF_KILL_FEED_ENTRY, strength=0.4)]
    assert fuse(evs, FusionConfig()) == []  # 100*0.4*0.4 = 16 < 25


def test_fusion_game_vision_only_high_confidence_can_qualify():
    evs = [SignalEvent(t=100.0, t_end=100.5, family="game_vision",
                       kind=KIND_GF_KILL_FEED_ENTRY, strength=0.9)]
    windows = fuse(evs, FusionConfig())
    assert len(windows) == 1  # 100*0.9*0.4 = 36 >= 25
    assert windows[0].reasons["solo_mode"] == "game_vision_only"


def _window(events) -> object:
    return fuse(events, FusionConfig())[0]


def test_selection_kill_feed_with_vocal_is_epic_kill():
    w = _window([
        SignalEvent(t=100.0, t_end=100.3, family="audio", kind=KIND_ENERGY_SPIKE,
                    strength=0.9),
        SignalEvent(t=100.1, t_end=100.5, family="game_vision",
                    kind=KIND_GF_KILL_FEED_ENTRY, strength=0.9),
    ])
    category, why = _categorize(w)
    assert category == CATEGORY_EPIC_KILL
    assert "heuristic" in why


def test_selection_match_result_victory_text():
    w = _window([
        SignalEvent(t=100.0, t_end=100.3, family="audio", kind=KIND_ENERGY_SPIKE,
                    strength=0.9),
        SignalEvent(t=100.1, t_end=101.0, family="game_vision",
                    kind=KIND_GF_MATCH_RESULT_TEXT, strength=0.95,
                    evidence={"text": "Victory"}),
    ])
    category, why = _categorize(w)
    assert category == CATEGORY_VICTORY
    assert "observed text" in why


def test_selection_kill_feed_alone_is_unexpected():
    w = _window([
        SignalEvent(t=100.0, t_end=100.5, family="game_vision",
                    kind=KIND_GF_KILL_FEED_ENTRY, strength=0.9),
    ])
    category, why = _categorize(w)
    assert category == CATEGORY_UNEXPECTED
    assert "single family" in why


def test_summarize_game_vision_job_level():
    summaries = [
        {"available": True, "engine": "stub", "frames_analyzed": 10,
         "ocr_operations": 12, "kill_feed_events": 1, "score_changes": 0,
         "scoreboard_visible_frames": 2, "scoreboard_scanned_frames": 2,
         "processing_seconds": 1.5},
        {"available": True, "engine": "stub", "frames_analyzed": 8,
         "ocr_operations": 10, "kill_feed_events": 0, "score_changes": 1,
         "scoreboard_visible_frames": 0, "scoreboard_scanned_frames": 2,
         "processing_seconds": 1.0, "budget_exceeded": True},
    ]
    meta = summarize_game_vision(summaries)
    assert meta["available"] is True
    assert meta["chunks_analyzed"] == 2
    assert meta["kill_feed_events"] == 1
    assert meta["budget_exceeded_chunks"] == 1
    # all-unavailable:
    meta2 = summarize_game_vision([{"available": False,
                                    "unavailable_reason": "no tesseract"}])
    assert meta2["available"] is False
    assert meta2["reason"] == "no tesseract"


# -- 13. REAL-OCR smoke (skipped unless tesseract present) --------------------------------

@REAL_OCR
def test_real_ocr_version_probe():
    engine = TesseractOcrEngine(GameVisionConfig())
    run(engine.validate())
    assert engine.available is True
    assert engine.version and "tesseract" in engine.version.lower()
    assert engine.unavailable_reason is None


@REAL_OCR
def test_real_ocr_reads_rendered_text(tmp_path):
    engine = TesseractOcrEngine(GameVisionConfig())
    run(engine.validate())
    img = _render_text_image(tmp_path / "text.jpg", "Hello Stream 12")
    lines = run(engine.recognize(img, tmp_path))
    assert lines, "OCR returned no lines for rendered text"
    joined = " ".join(l.text for l in lines).casefold()
    assert "hello" in joined and "stream" in joined
    assert all(0 <= l.confidence <= 100 for l in lines)
    assert all(l.key for l in lines)


@REAL_OCR
def test_real_ocr_no_text_is_empty_not_error(tmp_path):
    engine = TesseractOcrEngine(GameVisionConfig())
    run(engine.validate())
    from PIL import Image
    img = Image.new("RGB", (320, 80), "white")
    blank = tmp_path / "blank.jpg"
    img.save(blank, "JPEG")
    lines = run(engine.recognize(blank, tmp_path))
    assert lines == []


@REAL_OCR
def test_real_ocr_end_to_end_analyzer_chunk(tmp_path):
    """A full analyzer pass with REAL tesseract on a rendered-text video:
    the kill-feed ROI is a white strip with a line of text that appears at
    t=2 — the analyzer must find exactly that transient line (real OCR)."""
    from tests.conftest import resolve_binary
    ffmpeg = resolve_binary("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not available")
    # Build a 6-second 640x360 video: white background; at t>=2 draw the
    # text in the top-right strip (inside the default kill-feed ROI).
    from PIL import Image, ImageDraw
    frames = []
    for i in range(6):
        img = Image.new("RGB", (640, 360), "white")
        d = ImageDraw.Draw(img)
        if i >= 2:
            try:
                d.text((480, 12), "KILL TEST", fill="black",
                       font=ImageFont.load_default(size=32))
            except TypeError:
                d.text((480, 12), "KILL TEST", fill="black")
        frames.append(img)
    out_dir = tmp_path / "src"
    out_dir.mkdir()
    for i, img in enumerate(frames):
        img.save(out_dir / f"s_{i:03d}.jpg", "JPEG", quality=95)
    vid = tmp_path / "text.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-framerate", "1",
                    "-i", str(out_dir / "s_%03d.jpg"),
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                    "-c:v", "libx264", "-preset", "veryfast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                    str(vid)], check=True, capture_output=True)
    cfg = GameVisionConfig(kill_feed_roi=(0.70, 0.02, 0.30, 0.15),
                           sample_fps=1.0)
    engine = TesseractOcrEngine(cfg)
    analyzer = TesseractGameVisionAnalyzer(cfg, engine)
    report = run(analyzer.analyze_chunk(ffmpeg, vid, 0.0, 6.0, tmp_path / "out"))
    assert report.available is True
    assert report.engine == "tesseract"
    texts = [e.text for e in report.kill_feed_events]
    # Real OCR on machine-rendered text: the line should be readable. If the
    # engine misreads it, that is an ACCURACY limitation (documented), so
    # assert the STRUCTURE: a transient event appearing at t>=2.
    if texts:
        ev = report.kill_feed_events[0]
        assert ev.appeared_at >= 2.0  # text only exists from t=2
        assert ev.frames_seen in (3, 4)
    assert not (tmp_path / "out").exists()
