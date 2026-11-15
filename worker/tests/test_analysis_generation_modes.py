"""TASK 015 P3 — AnalysisProcessor generation-mode INTEGRATION tests.

REAL probe (ffprobe over a genuine 25s H.264+AAC lavfi video) + REAL
FFmpeg audio extraction + scripted transcription + in-memory fakes for the
queue/storage/clip-store. This is pipeline integration: it proves the two
INDEPENDENT generation modes produce the correct clip SETS through the
actual processor (Mode A: exact full-coverage tiling with the documented
remainder policy; Mode B: detector + timeline merge, bounded, deduped,
transparent-scored), and that clip generation is idempotent across re-runs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from app.ai.clip_detection import ScoredClip
from app.ai.timeline import TimelineRow
from app.config.settings import Settings
from app.processors.analysis import AnalysisProcessor
from test_analysis_timeline_integration import make_gemini, ok_handler
from app.transcription.base import (
    BaseTranscriber,
    TranscriptionResult,
    TranscriptionSegment,
)
from conftest import _run_ffmpeg

JOB_ID = str(uuid4())
WORKER_ID = "worker-test-modes"
SOURCE_PATH = f"sources/{JOB_ID}/original/generated.mp4"


def run(coro):
    return asyncio.run(coro)


class FakeQueue:
    def __init__(self, park_ok: bool = True) -> None:
        self.park_ok = park_ok
        self.fail_calls: list[tuple] = []
        self.park_calls: list[tuple[str, str]] = []
        self.transition_calls: list[tuple] = []

    async def renew_job_lease(self, job_id, worker_id, lease_seconds):
        return True

    async def update_job_progress(self, job_id, worker_id, progress):
        return True

    async def fail_job(self, job_id, worker_id, category, message, permanent=False):
        self.fail_calls.append((job_id, worker_id, category, message, permanent))
        return "failed" if permanent else "ready"

    async def transition_stage(self, job_id, worker_id, from_status, to_status):
        self.transition_calls.append((job_id, worker_id, from_status, to_status))
        return "transitioned"

    async def park_after_analysis(self, job_id, worker_id):
        self.park_calls.append((job_id, worker_id))
        return self.park_ok


class FakeStorage:
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def download_to(self, object_path: str, destination: Path) -> int:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.content)
        return len(self.content)


class FakeClipStore:
    def __init__(self) -> None:
        self.saved: list[tuple[str, list]] = []

    async def save_clips(self, job_id: str, clips) -> int:
        self.saved.append((job_id, list(clips)))
        return len(clips)


class FakeTimelineStore:
    """In-memory job-scoped timeline store (upsert by window_index)."""

    def __init__(self) -> None:
        self.rows: dict[int, dict] = {}
        self.upsert_calls = 0

    async def load_rows(self, job_id: str) -> dict[int, dict]:
        assert job_id == JOB_ID
        return dict(self.rows)

    async def upsert_rows(self, job_id: str, rows: list[dict]) -> int:
        assert job_id == JOB_ID
        self.upsert_calls += 1
        self.rows.update({int(r["window_index"]): r for r in rows})
        return len(rows)

    async def reset_for_job(self, job_id: str) -> None:
        self.rows.clear()


class ScriptedTranscriber(BaseTranscriber):
    name = "scripted"

    def __init__(self, segments: list[TranscriptionSegment]) -> None:
        self._segments = segments

    def transcribe(self, audio_path: str, language: str | None = None) -> TranscriptionResult:
        return TranscriptionResult(
            "en", 0.9, 300.0, tuple(self._segments),
            " ".join(s.text for s in self._segments),
        )


def talk_segments(total_seconds: float = 300.0) -> list[TranscriptionSegment]:
    from app.transcription.base import TranscriptionWord

    segs = []
    k = 0
    t = 0.0
    while t + 3 <= total_seconds:
        text = f"segment {k + 1} talk"
        tokens = text.split()
        share = 3.0 / len(tokens)
        words = tuple(
            TranscriptionWord(word=w, start=t + i * share, end=t + (i + 1) * share)
            for i, w in enumerate(tokens)
        )
        segs.append(TranscriptionSegment(text=text, start=t, end=t + 3, words=words))
        k += 1
        t += 5.0
    return segs


def make_settings(tmp_path: Path, ffmpeg: str, ffprobe: str) -> Settings:
    return Settings(
        environment="development",
        log_level="WARNING",
        storage_root=tmp_path,
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        job_lease_seconds=300,
        gemini_timeline_window_seconds=10,  # 25s video -> 3 windows + 5s remainder
    )


def make_job(**overrides) -> dict:
    job = {
        "id": JOB_ID,
        "status": "transcribing",
        "source_type": "upload",
        "source_path": SOURCE_PATH,
        "requested_clips": 3,
        "clip_duration_seconds": 10,
        "_worker_id": WORKER_ID,
    }
    job.update(overrides)
    return job


@pytest.fixture()
def video25(ffmpeg_bin: str, tmp_path: Path) -> bytes:
    """A real 25s H.264+AAC mp4 (the probed source duration is 25.0)."""
    out = tmp_path / "src25.mp4"
    _run_ffmpeg(
        ffmpeg_bin,
        ["-f", "lavfi", "-i", "testsrc=duration=25:size=320x240:rate=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=25",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         str(out)],
    )
    return out.read_bytes()


# ---- MODE A — full video to reels ------------------------------------------------

def test_full_coverage_tiles_the_whole_source_25s_at_10s(
        tmp_path, ffmpeg_bin, ffprobe_bin, video25):
    """25s @ 10s -> 2 clips [0,10] + [10,25]: the 5s remainder (< 10s) is
    appended to the previous clip (no padding, nothing skipped)."""
    queue, store = FakeQueue(), FakeClipStore()
    processor = AnalysisProcessor(
        make_settings(tmp_path, ffmpeg_bin, ffprobe_bin), queue,
        FakeStorage(video25), ScriptedTranscriber(talk_segments()), store,
    )
    run(processor.process_job(make_job(generation_mode="full_coverage",
                                       requested_clips=None)))

    assert queue.fail_calls == []
    assert queue.park_calls == [(JOB_ID, WORKER_ID)]
    job_id, clips = store.saved[0]
    assert job_id == JOB_ID
    spans = [(c.start, c.end) for c in clips]
    # 25s @ 10s -> 2 clips; the 5s remainder is merged (no padding).
    assert len(spans) == 2
    assert spans[0] == (0.0, 10.0)
    assert spans[1][0] == 10.0  # no gap, no overlap
    assert spans[1][1] == pytest.approx(25.0, abs=0.06)  # probed source end
    # Exact coverage of the probed source: total rendered == source length.
    assert sum(c.end - c.start for c in clips) == pytest.approx(spans[1][1], abs=1e-6)
    for clip in clips:
        assert clip.mode == "full_coverage"
        assert clip.score == 50.0  # neutral completeness label
        assert clip.scoring["method"].startswith("full-coverage")
        assert clip.title  # "Reel i/n"
    assert clips[0].title == "Reel 1/2"
    assert clips[1].title == "Reel 2/2"


def test_full_coverage_remainder_kept_when_long_enough_25s_at_13s(
        tmp_path, ffmpeg_bin, ffprobe_bin, video25):
    """25s @ 13s -> [0,13] + [13,25]: the 12s remainder (>= 10s) is kept as
    its own clip (the ceil count is reached)."""
    queue, store = FakeQueue(), FakeClipStore()
    processor = AnalysisProcessor(
        make_settings(tmp_path, ffmpeg_bin, ffprobe_bin), queue,
        FakeStorage(video25), ScriptedTranscriber(talk_segments()), store,
    )
    run(processor.process_job(make_job(generation_mode="full_coverage",
                                       requested_clips=None,
                                       clip_duration_seconds=13)))
    job_id, clips = store.saved[0]
    spans = [(c.start, c.end) for c in clips]
    assert len(spans) == 2
    assert spans[0] == (0.0, 13.0)
    assert spans[1][0] == 13.0
    assert spans[1][1] == pytest.approx(25.0, abs=0.06)  # 12s remainder kept
    assert all(c.mode == "full_coverage" for c in clips)


def test_full_coverage_source_shorter_than_clip_is_one_clip(
        tmp_path, ffmpeg_bin, ffprobe_bin, video25):
    """25s @ 60s -> a single [0,25] clip (never padded to 60s)."""
    queue, store = FakeQueue(), FakeClipStore()
    processor = AnalysisProcessor(
        make_settings(tmp_path, ffmpeg_bin, ffprobe_bin), queue,
        FakeStorage(video25), ScriptedTranscriber(talk_segments()), store,
    )
    run(processor.process_job(make_job(generation_mode="full_coverage",
                                       requested_clips=None,
                                       clip_duration_seconds=60)))
    job_id, clips = store.saved[0]
    assert len(clips) == 1
    assert clips[0].start == 0.0
    assert clips[0].end == pytest.approx(25.0, abs=0.06)  # never padded to 60s
    assert clips[0].mode == "full_coverage"


def test_full_coverage_never_truncated_by_a_user_count(
        tmp_path, ffmpeg_bin, ffprobe_bin, video25):
    """Defensive guard: even if a stale requested_clips value reaches a
    full_coverage job, it is a derivation slot — the tiling is complete."""
    queue, store = FakeQueue(), FakeClipStore()
    processor = AnalysisProcessor(
        make_settings(tmp_path, ffmpeg_bin, ffprobe_bin), queue,
        FakeStorage(video25), ScriptedTranscriber(talk_segments()), store,
    )
    run(processor.process_job(make_job(generation_mode="full_coverage",
                                       requested_clips=1,  # must be ignored
                                       clip_duration_seconds=10)))
    job_id, clips = store.saved[0]
    assert len(clips) == 2  # the full tiling, NOT truncated to 1
    assert clips[0].start == 0.0 and clips[0].end == 10.0
    assert clips[1].start == 10.0
    assert clips[1].end == pytest.approx(25.0, abs=0.06)


def test_full_coverage_clip_generation_is_idempotent_across_reruns(
        tmp_path, ffmpeg_bin, ffprobe_bin, video25):
    """Re-running the analysis (safe retry) reproduces the EXACT same clip
    set — no duplicates, no drift."""
    queue, store = FakeQueue(), FakeClipStore()
    processor = AnalysisProcessor(
        make_settings(tmp_path, ffmpeg_bin, ffprobe_bin), queue,
        FakeStorage(video25), ScriptedTranscriber(talk_segments()), store,
    )
    job = make_job(generation_mode="full_coverage", requested_clips=None,
                   clip_duration_seconds=10)
    run(processor.process_job(job))
    run(processor.process_job(job))  # re-claim / retry

    assert len(store.saved) == 2
    first = [(c.start, c.end, c.title) for c in store.saved[0][1]]
    second = [(c.start, c.end, c.title) for c in store.saved[1][1]]
    assert first == second  # deterministic, idempotent clip generation
    assert len(first) == 2
    assert first[0][2] == "Reel 1/2" and first[1][2] == "Reel 2/2"
    assert first[0][0] == 0.0 and first[0][1] == 10.0 and first[1][0] == 10.0
    assert first[1][1] == pytest.approx(25.0, abs=0.06)
    # No duplicates within a set.
    assert len(set(first)) == len(first)


# ---- MODE B — viral highlights ----------------------------------------------------

def test_highlights_mode_without_timeline_uses_the_detector(
        tmp_path, ffmpeg_bin, ffprobe_bin, video25):
    """No timeline store wired: the classic detector path, bounded by the
    requested count, every clip tagged 'highlights' (the default). The
    detector's window band is the product band (60-180s), so the target is
    90s like every existing highlights job."""
    queue, store = FakeQueue(), FakeClipStore()
    processor = AnalysisProcessor(
        make_settings(tmp_path, ffmpeg_bin, ffprobe_bin), queue,
        FakeStorage(video25), ScriptedTranscriber(talk_segments()), store,
    )
    run(processor.process_job(make_job(generation_mode="highlights",
                                       requested_clips=2,
                                       clip_duration_seconds=90)))
    assert queue.fail_calls == []
    job_id, clips = store.saved[0]
    assert 1 <= len(clips) <= 2  # requested count is an upper bound
    for clip in clips:
        assert clip.mode == "highlights"
        assert clip.scoring["scale"] == "0-100"
        assert 0 <= clip.start < clip.end
        # deterministic timestamps
        assert clip.start == round(clip.start, 3)


def test_highlights_mode_merges_timeline_candidates(
        tmp_path, ffmpeg_bin, ffprobe_bin, video25):
    """With the P2 timeline + Gemini wired (mocked transport, no network):
    Mode B merges the detector set with the timeline-ranked candidates
    (the transparent score), keeps exact source timestamps, dedupes
    near-overlaps (IoU > 0.5 of the shorter window), and honours the
    requested count as a hard upper bound."""
    queue, store, tl = FakeQueue(), FakeClipStore(), FakeTimelineStore()
    state: dict = {"next_index": 0}
    gemini, requests, _ = make_gemini(ok_handler(state))
    processor = AnalysisProcessor(
        make_settings(tmp_path, ffmpeg_bin, ffprobe_bin), queue,
        FakeStorage(video25), ScriptedTranscriber(talk_segments()), store,
        timeline_store=tl, gemini=gemini,
    )
    run(processor.process_job(make_job(generation_mode="highlights",
                                       requested_clips=3,
                                       clip_duration_seconds=90)))
    run(gemini.close())

    assert queue.fail_calls == []
    assert queue.park_calls == [(JOB_ID, WORKER_ID)]
    # The P2 timeline was built and persisted (3 windows over 25s), each
    # assessed by Gemini (3 requests — the mocked transport, no network).
    assert sorted(tl.rows) == [0, 1, 2]
    assert tl.rows[2]["end_time"] == 25.0
    assert len(requests) == 3
    assert all(tl.rows[i]["gemini_assessment"]["status"] == "ok" for i in (0, 1, 2))

    job_id, clips = store.saved[0]
    assert 1 <= len(clips) <= 3  # hard upper bound
    for clip in clips:
        assert clip.mode == "highlights"
        assert 0.0 <= clip.start < clip.end
        # transparent scoring payload present (no virality claims)
        assert clip.scoring["scale"] == "0-100"
        assert clip.scoring["total"] is not None
        method = clip.scoring.get("method", "")
        # The P2 timeline candidates are clamped to the probed source
        # duration (exact source timestamps, never past the end).
        if method.startswith("timeline ranking"):
            assert clip.end <= 25.0 + 1e-6
            assert clip.scoring["note"] == "potential estimate, not a virality guarantee"
    # No near-duplicates survived the merge.
    for i in range(len(clips)):
        for j in range(i + 1, len(clips)):
            a, b = clips[i], clips[j]
            inter = max(0.0, min(a.end, b.end) - max(a.start, b.start))
            shorter = min(b.end - b.start, a.end - a.start)
            assert inter / shorter <= 0.5, f"clips {i} and {j} overlap too much"
    # Deterministic: a re-run yields the identical set (and Gemini is NOT
    # re-billed for the windows whose assessment is already 'ok').
    run(processor.process_job(make_job(generation_mode="highlights",
                                       requested_clips=3,
                                       clip_duration_seconds=90)))
    run(gemini.close())
    first = [(c.start, c.end) for c in store.saved[0][1]]
    second = [(c.start, c.end) for c in store.saved[1][1]]
    assert first == second
    assert len(requests) == 3  # resume: the persisted 'ok' windows were skipped


def test_merge_timeline_highlights_dedupes_and_bounds_deterministically(tmp_path):
    """The merge itself, directly (deterministic): the timeline candidate
    (transparent score 75.67 over the [0,30) context group) beats the
    overlapping weaker base clip, keeps the disjoint base clip, and the
    requested count is a hard bound."""
    processor = AnalysisProcessor(
        make_settings(tmp_path, "ffmpeg", "ffprobe"), FakeQueue(),
        FakeStorage(b""), ScriptedTranscriber([]), FakeClipStore(),
    )
    rows = [
        TimelineRow(0, 0.0, 10.0, transcript="setup talk",
                    event_importance=0.4,
                    gemini_assessment={"status": "ok", "event_importance": 0.4,
                                       "candidate_clip": False}),
        TimelineRow(1, 10.0, 20.0, transcript="the big play",
                    event_importance=0.9, confidence=0.8, candidate_clip=True,
                    gemini_assessment={"status": "ok", "event_importance": 0.9,
                                       "candidate_clip": True}),
        TimelineRow(2, 20.0, 30.0, transcript="aftermath",
                    event_importance=0.4,
                    gemini_assessment={"status": "ok", "event_importance": 0.4,
                                       "candidate_clip": False}),
    ]
    base = [
        ScoredClip(start=0.0, end=30.0, title="base overlap", score=40.0),
        ScoredClip(start=35.0, end=45.0, title="base disjoint", score=50.0),
    ]
    # 100 * (0.5*0.9 + 0.3*0.8 + 0.2*(1/3)) = 45 + 24 + 6.67 = 75.67
    merged = processor._merge_timeline_highlights(base, rows, requested=2,
                                                  clip_duration=10,
                                                  duration_seconds=30.0)
    assert [(c.start, c.end, c.title) for c in merged] == [
        (0.0, 30.0, "Highlight at 00:00"),  # timeline candidate (75.67 > 40)
        (35.0, 45.0, "base disjoint"),
    ]
    assert merged[0].scoring["method"].startswith("timeline ranking")
    assert merged[0].score == pytest.approx(75.67, abs=0.01)
    assert merged[0].mode == "highlights"
    # requested=1 is a HARD upper bound: only the best survives.
    merged1 = processor._merge_timeline_highlights(base, rows, requested=1,
                                                   clip_duration=10,
                                                   duration_seconds=30.0)
    assert [(c.start, c.end) for c in merged1] == [(0.0, 30.0)]
    # No timeline signal (all rows unassessed) -> the base set unchanged.
    bare = [TimelineRow(i, i * 10.0, (i + 1) * 10.0, transcript="") for i in range(3)]
    unchanged = processor._merge_timeline_highlights(base, bare, requested=10,
                                                     clip_duration=10,
                                                     duration_seconds=30.0)
    assert [(c.start, c.end, c.title) for c in unchanged] == [
        (0.0, 30.0, "base overlap"), (35.0, 45.0, "base disjoint"),
    ]


def test_timeline_row_to_dict_shape_matches_ranking_input(
        tmp_path, ffmpeg_bin, ffprobe_bin, video25):
    """The rows handed to rank_highlights carry the measured fields (this is
    what makes the Mode B score transparent and reproducible)."""
    tl = FakeTimelineStore()
    processor = AnalysisProcessor(
        make_settings(tmp_path, ffmpeg_bin, ffprobe_bin), FakeQueue(),
        FakeStorage(video25), ScriptedTranscriber(talk_segments()),
        FakeClipStore(), timeline_store=tl,
    )
    ok, rows = run(processor._run_timeline(JOB_ID, WORKER_ID, 25.0, [],
                                           talk_segments(), Path("/nonexistent.bin")))
    assert ok is True
    assert rows is not None and len(rows) == 3
    row = rows[0]
    assert isinstance(row, TimelineRow)
    for field in ("window_index", "start_time", "end_time", "transcript",
                  "confidence", "event_importance", "gemini_assessment"):
        assert hasattr(row, field)
    # Without Gemini: honest not_configured state, no invented importance.
    assert row.gemini_assessment == {"status": "not_configured"}
    assert row.event_importance is None
