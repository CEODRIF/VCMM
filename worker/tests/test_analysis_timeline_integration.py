"""TASK 015 — AnalysisProcessor × full-timeline INTEGRATION tests.

REAL pipeline path (probe → transcribe → detect → timeline → persist →
park) with: real FFmpeg/ffprobe, real AnalysisProcessor, REAL GeminiClient
over httpx.MockTransport (no network, no real keys), and an in-memory
job-scoped timeline store (idempotent upsert semantics, same row shape as
the Supabase store). This is pipeline integration, not unit: it proves the
coverage record is built from the actual probe duration + actual
transcript and that Gemini failures never fake analysis.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from app.ai.gemini import GeminiClient, GeminiConfig
from app.config.settings import Settings
from app.db.timeline_store import TimelineStoreError
from app.processors.analysis import AnalysisProcessor
from app.queue.models import ERROR_TIMELINE
from app.storage.clip_store import SupabaseClipStore
from app.storage.supabase_storage import SupabaseStorage
from app.transcription.base import (
    BaseTranscriber,
    TranscriptionResult,
    TranscriptionSegment,
)

JOB_ID = str(uuid4())
WORKER_ID = "worker-test-timeline"
HOST = "gemini.test"
BASE = f"https://{HOST}/v1beta"
KEY_A = "key-a-sentinel"
KEY_B = "key-b-sentinel"


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Fakes (queue/storage/clip-store/timeline-store) — same contracts as the
# real classes; the timeline store keeps the exact DB row shape.
# --------------------------------------------------------------------------

class FakeQueue:
    def __init__(self) -> None:
        self.fail_calls: list[tuple[str, str, str, str, bool]] = []
        self.park_calls: list[tuple[str, str]] = []
        self.transition_calls: list[tuple] = []

    async def renew_job_lease(self, job_id, worker_id, lease_seconds):
        return True

    async def update_job_progress(self, job_id, worker_id, progress):
        return True

    async def fail_job(self, job_id, worker_id, error_category, error_message, permanent=False):
        self.fail_calls.append((job_id, worker_id, error_category, error_message, permanent))
        return "failed" if permanent else "ready"

    async def transition_stage(self, job_id, worker_id, from_status, to_status):
        self.transition_calls.append((job_id, worker_id, from_status, to_status))
        return "transitioned"

    async def park_after_analysis(self, job_id, worker_id):
        self.park_calls.append((job_id, worker_id))
        return True


class FakeStorage:
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def download_to(self, object_path: str, destination: Path) -> int:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.content)
        return len(self.content)


class FakeClipStore(SupabaseClipStore):
    def __init__(self) -> None:
        self.saved: list[tuple[str, list]] = []

    async def save_clips(self, job_id: str, clips) -> int:
        self.saved.append((job_id, list(clips)))
        return len(clips)


class FakeTimelineStore:
    """In-memory stand-in for TimelineStore — same row shape + upsert
    idempotency (keyed on window_index)."""

    def __init__(self, fail_upsert: bool = False,
                 seed: dict[int, dict] | None = None) -> None:
        self.rows: dict[int, dict[str, Any]] = dict(seed or {})
        self.fail_upsert = fail_upsert
        self.upsert_calls = 0

    async def load_rows(self, job_id: str) -> dict[int, dict[str, Any]]:
        assert job_id == JOB_ID  # job-scoped
        return dict(self.rows)

    async def upsert_rows(self, job_id: str, rows: list[dict[str, Any]]) -> int:
        assert job_id == JOB_ID  # job-scoped
        if self.fail_upsert:
            raise TimelineStoreError("simulated persistence failure")
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


def make_gemini(handler: Any, *, keys=(KEY_A, KEY_B),
                max_retries: int = 1) -> tuple[GeminiClient, list, list]:
    state: dict[str, Any] = {"requests": [], "sleeps": []}

    def wrapped(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        return handler(request)

    async def fake_sleep(s: float) -> None:
        state["sleeps"].append(s)

    config = GeminiConfig(api_keys=tuple(keys), model="test-model", api_base=BASE,
                          timeout_seconds=5.0, max_retries=max_retries,
                          backoff_seconds=0.01, cooldown_seconds=60.0)
    client = GeminiClient(config, client=httpx.AsyncClient(
        transport=httpx.MockTransport(wrapped)), sleep=fake_sleep)
    return client, state["requests"], state["sleeps"]


def ok_handler(state: dict[str, Any]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == HOST
        assert request.headers.get("x-goog-api-key") in (KEY_A, KEY_B)
        idx = state.get("next_index", 0)
        state["next_index"] = idx + 1
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": json.dumps({
                "scene_description": f"window {idx} scene",
                "event_type": "combat",
                "event_importance": 0.5 + idx * 0.1,
                "candidate_clip": idx == 1,
                "reasoning": "measured",
            })}]}}]
        })
    return handler


def make_settings(tmp_path: Path, ffmpeg: str, ffprobe: str) -> Settings:
    return Settings(
        environment="development",
        log_level="WARNING",
        storage_root=tmp_path,
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        job_lease_seconds=300,
        gemini_timeline_window_seconds=10,  # 25s video → 3 windows, last = 5s remainder
    )


def make_job() -> dict:
    return {
        "id": JOB_ID,
        "status": "transcribing",
        "source_type": "upload",
        "source_path": f"sources/{JOB_ID}/original/generated.mp4",
        "requested_clips": 3,
        "clip_duration_seconds": 90,
        "_worker_id": WORKER_ID,
    }


@pytest.fixture()
def video25(ffmpeg_bin: str, tmp_path: Path) -> bytes:
    """A real 25s H.264+AAC mp4 (3 windows @10s + a 5s remainder)."""
    from conftest import _run_ffmpeg  # session helper (same pattern as the intel tests)
    out = tmp_path / "src25.mp4"
    _run_ffmpeg(
        ffmpeg_bin,
        ["-f", "lavfi", "-i", "testsrc=duration=25:size=320x240:rate=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=25",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         str(out)],
    )
    return out.read_bytes()


# --------------------------------------------------------------------------
# Integration tests
# --------------------------------------------------------------------------

def test_transcript_path_persists_full_coverage_with_gemini(tmp_path, ffmpeg_bin, ffprobe_bin, video25):
    queue, store, tl = FakeQueue(), FakeClipStore(), FakeTimelineStore()
    state: dict[str, Any] = {"next_index": 0}
    gemini, requests, _ = make_gemini(ok_handler(state))
    processor = AnalysisProcessor(
        make_settings(tmp_path, ffmpeg_bin, ffprobe_bin), queue,
        FakeStorage(video25), ScriptedTranscriber(talk_segments()), store,
        timeline_store=tl, gemini=gemini,
    )
    run(processor.process_job(make_job()))
    run(gemini.close())

    # The job succeeded normally — the timeline did not block the pipeline.
    assert queue.fail_calls == []
    assert queue.park_calls == [(JOB_ID, WORKER_ID)]
    # 3 windows, one Gemini call each.
    assert len(requests) == 3
    # Full coverage: tiles [0, 25] exactly, remainder included.
    assert sorted(tl.rows) == [0, 1, 2]
    spans = [(tl.rows[i]["start_time"], tl.rows[i]["end_time"]) for i in (0, 1, 2)]
    assert spans[0][0] == 0.0
    assert spans[0][1] == spans[1][0] == 10.0
    assert spans[1][1] == spans[2][0] == 20.0
    assert spans[2][1] == 25.0  # the final remainder
    for i in (0, 1, 2):
        row = tl.rows[i]
        assert row["gemini_assessment"]["status"] == "ok"
        assert row["event_type"] == "combat"
        assert row["scene_description"] == f"window {i} scene"
    # Transcripts are the ACTUAL words overlapping each window (not invented).
    assert tl.rows[0]["transcript"] == "segment 1 talk segment 2 talk"
    assert tl.rows[1]["transcript"] == "segment 3 talk segment 4 talk"
    assert tl.rows[2]["transcript"] == "segment 5 talk"
    # The key never appears in any persisted record.
    blob = json.dumps(tl.rows)
    assert KEY_A not in blob and KEY_B not in blob


def test_resume_skips_windows_already_assessed(tmp_path, ffmpeg_bin, ffprobe_bin, video25):
    queue, store, _ = FakeQueue(), FakeClipStore(), None
    seed_row = {
        "job_id": JOB_ID, "window_index": 0, "start_time": 0.0, "end_time": 10.0,
        "event_type": "intro", "transcript": "segment 1 talk segment 2 talk",
        "scene_description": "window 0 scene", "visual_evidence": {},
        "audio_evidence": {}, "event_importance": 0.5, "candidate_clip": False,
        "confidence": None,
        "gemini_assessment": {"status": "ok", "scene_description": "window 0 scene",
                              "event_type": "intro", "event_importance": 0.5,
                              "candidate_clip": False, "reasoning": "measured"},
    }
    tl = FakeTimelineStore(seed={0: seed_row})
    state: dict[str, Any] = {"next_index": 1}  # the persisted window is index 0
    gemini, requests, _ = make_gemini(ok_handler(state))
    processor = AnalysisProcessor(
        make_settings(tmp_path, ffmpeg_bin, ffprobe_bin), queue,
        FakeStorage(video25), ScriptedTranscriber(talk_segments()), store,
        timeline_store=tl, gemini=gemini,
    )
    run(processor.process_job(make_job()))
    run(gemini.close())

    # Resume: only the 2 unassessed windows were billed to Gemini.
    assert len(requests) == 2
    assert sorted(tl.rows) == [0, 1, 2]
    assert tl.rows[0]["gemini_assessment"]["status"] == "ok"
    assert queue.park_calls == [(JOB_ID, WORKER_ID)]
    assert queue.fail_calls == []


def test_gemini_failure_is_honest_and_never_blocks_the_job(tmp_path, ffmpeg_bin, ffprobe_bin, video25):
    queue, store, tl = FakeQueue(), FakeClipStore(), FakeTimelineStore()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    gemini, requests, _ = make_gemini(handler, max_retries=0)
    processor = AnalysisProcessor(
        make_settings(tmp_path, ffmpeg_bin, ffprobe_bin), queue,
        FakeStorage(video25), ScriptedTranscriber(talk_segments()), store,
        timeline_store=tl, gemini=gemini,
    )
    run(processor.process_job(make_job()))
    run(gemini.close())

    # The JOB still completes (clips from local evidence) — but the timeline
    # is NOT marked analyzed: every row carries the honest failed state.
    assert queue.park_calls == [(JOB_ID, WORKER_ID)]
    assert queue.fail_calls == []
    assert len(requests) == 3  # 1 attempt per window (max_retries=0)
    for i in (0, 1, 2):
        row = tl.rows[i]
        assert row["gemini_assessment"]["status"] == "failed"
        assert "GeminiTransientError" in row["gemini_assessment"]["reason"]
        assert row["event_importance"] is None
        assert row["candidate_clip"] is False
        assert row["event_type"] == "unknown"
        # no key leak in the failure reason
        assert KEY_A not in row["gemini_assessment"]["reason"]
    # coverage still exact
    assert tl.rows[2]["end_time"] == 25.0


def test_unconfigured_gemini_records_not_configured_without_network(
        tmp_path, ffmpeg_bin, ffprobe_bin, video25):
    queue, store, tl = FakeQueue(), FakeClipStore(), FakeTimelineStore()
    state: dict[str, Any] = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(200, json={})

    async def fake_sleep(_s: float) -> None:
        return None

    gemini = GeminiClient(GeminiConfig(api_keys=(), api_base=BASE),
                          client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                          sleep=fake_sleep)
    processor = AnalysisProcessor(
        make_settings(tmp_path, ffmpeg_bin, ffprobe_bin), queue,
        FakeStorage(video25), ScriptedTranscriber(talk_segments()), store,
        timeline_store=tl, gemini=gemini,
    )
    run(processor.process_job(make_job()))
    run(gemini.close())

    assert state["n"] == 0  # ZERO network calls when unconfigured
    assert queue.park_calls == [(JOB_ID, WORKER_ID)]
    assert sorted(tl.rows) == [0, 1, 2]
    for i in (0, 1, 2):
        assert tl.rows[i]["gemini_assessment"] == {"status": "not_configured"}
    # local evidence still captured (measured confidence may be null)
    assert tl.rows[0]["transcript"] == "segment 1 talk segment 2 talk"


def test_timeline_store_error_fails_the_job_retriably(tmp_path, ffmpeg_bin, ffprobe_bin, video25):
    queue, store, tl = FakeQueue(), FakeClipStore(), FakeTimelineStore(fail_upsert=True)
    state: dict[str, Any] = {"next_index": 0}
    gemini, _, _ = make_gemini(ok_handler(state))
    processor = AnalysisProcessor(
        make_settings(tmp_path, ffmpeg_bin, ffprobe_bin), queue,
        FakeStorage(video25), ScriptedTranscriber(talk_segments()), store,
        timeline_store=tl, gemini=gemini,
    )
    run(processor.process_job(make_job()))
    run(gemini.close())

    assert queue.park_calls == []  # never parks without the coverage record
    assert len(queue.fail_calls) == 1
    job_id, worker_id, category, message, permanent = queue.fail_calls[0]
    assert (job_id, worker_id) == (JOB_ID, WORKER_ID)
    assert category == ERROR_TIMELINE
    assert permanent is False  # re-claim resumes from persisted windows
    assert "simulated persistence failure" in message


def test_intel_events_flow_into_the_same_timeline(tmp_path, ffmpeg_bin, ffprobe_bin, video25):
    """The intel (signal) path contributes to the SAME job timeline:
    measured SignalEvents appear as window evidence (no transcript on the
    signal path — recorded as empty, never invented)."""
    tl = FakeTimelineStore()
    state: dict[str, Any] = {"next_index": 0}
    gemini, requests, _ = make_gemini(ok_handler(state))
    processor = AnalysisProcessor(
        make_settings(tmp_path, ffmpeg_bin, ffprobe_bin), FakeQueue(),
        FakeStorage(video25), ScriptedTranscriber(talk_segments()), FakeClipStore(),
        timeline_store=tl, gemini=gemini,
    )
    # Synthetic intel events: an audio energy peak in window 0 and a visual
    # scene cut in window 2 (the remainder window).
    events = [
        {"family": "audio", "kind": "energy_peak", "t": 3.0, "t_end": 4.0,
         "strength": 0.9, "evidence": {"db": -10.0}},
        {"family": "visual", "kind": "scene_cut", "t": 22.0, "t_end": 22.0,
         "strength": 0.7, "evidence": {}},
    ]
    ok, rows = run(processor._run_timeline(JOB_ID, WORKER_ID, 25.0, events, [],
                                           Path("/nonexistent-src.bin")))
    run(gemini.close())
    assert ok is True
    assert rows is not None and len(rows) == 3  # the rows feed MODE B ranking
    assert sorted(tl.rows) == [0, 1, 2]
    row0 = tl.rows[0]
    assert row0["audio_evidence"]["events"][0]["kind"] == "energy_peak"
    assert row0["confidence"] == 0.9
    assert row0["transcript"] == ""  # signal path: no transcript, honest
    row2 = tl.rows[2]
    assert row2["visual_evidence"]["events"][0]["kind"] == "scene_cut"
    assert row2["confidence"] == 0.7
    # the coverage tile is identical to the transcript path (same engine)
    assert row2["end_time"] == 25.0
    assert len(requests) == 3
