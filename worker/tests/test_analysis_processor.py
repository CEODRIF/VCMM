"""AnalysisProcessor tests: full Phase 4 lifecycle with real FFmpeg extraction,
scripted transcription and fake queue/storage/store — plus every failure path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from app.config.settings import Settings
from app.processors.analysis import AnalysisProcessor
from app.queue.models import (
    ERROR_CLIPS_PERSIST,
    ERROR_CORRUPT_MEDIA,
    ERROR_NO_AUDIO,
    ERROR_NO_CLIPS,
    ERROR_NO_SPEECH,
    ERROR_STORAGE_DOWNLOAD,
    ERROR_TRANSCRIPTION,
    ERROR_TRANSCRIPTION_UNAVAILABLE,
    ERROR_UNKNOWN,
)
from app.storage.clip_store import ClipsPersistenceError, SupabaseClipStore
from app.storage.supabase_storage import StorageDownloadError
from app.transcription.base import (
    BaseTranscriber,
    TranscriptionError,
    TranscriptionResult,
    TranscriptionSegment,
    TranscriptionUnavailableError,
    TranscriptionWord,
)

JOB_ID = str(uuid4())
WORKER_ID = "worker-test-analysis"
SOURCE_PATH = f"sources/{JOB_ID}/original/generated.mp4"


def talk_transcript(total_seconds: int = 300) -> TranscriptionResult:
    """Deterministic 4s-sentence / 1s-pause talk with word timings."""
    patterns = [
        "Point number {k} matters because it explains step {k} in detail.",
        "What if you could learn secret {k} in one go?",
        "Here is fact number {k} that most people miss.",
        "First, you should notice signal {k} clearly.",
        "The truth about habit {k} is simpler than you think.",
    ]
    segments: list[TranscriptionSegment] = []
    k = 0
    t = 0.0
    while t + 4 <= total_seconds:
        text = patterns[k % len(patterns)].format(k=k + 1)
        tokens = text.split()
        share = 4.0 / len(tokens)
        words = tuple(
            TranscriptionWord(word=w, start=t + i * share, end=t + (i + 1) * share)
            for i, w in enumerate(tokens)
        )
        segments.append(TranscriptionSegment(text=text, start=t, end=t + 4.0, words=words))
        k += 1
        t += 5.0
    return TranscriptionResult("en", 0.92, float(total_seconds), tuple(segments),
                               " ".join(s.text for s in segments))


class ScriptedTranscriber(BaseTranscriber):
    name = "scripted"

    def __init__(self, result: TranscriptionResult | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []

    def transcribe(self, audio_path: str, language: str | None = None) -> TranscriptionResult:
        self.calls.append(str(audio_path))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class FakeQueue:
    def __init__(self, progress_ok=True, renew_ok=True, transition_result="transitioned", park_ok=True):
        self.progress_ok = progress_ok
        self.renew_ok = renew_ok
        self.transition_result = transition_result
        self.park_ok = park_ok
        self.progress_calls: list[tuple[str, str, int]] = []
        self.renew_calls: list[tuple[str, str, int]] = []
        self.fail_calls: list[tuple[str, str, str, str, bool]] = []
        self.transition_calls: list[tuple[str, str, str, str]] = []
        self.park_calls: list[tuple[str, str]] = []

    async def claim_next_job(self, worker_id, lease_seconds):
        return None

    async def renew_job_lease(self, job_id, worker_id, lease_seconds):
        self.renew_calls.append((job_id, worker_id, lease_seconds))
        return self.renew_ok

    async def update_job_progress(self, job_id, worker_id, progress):
        self.progress_calls.append((job_id, worker_id, progress))
        return self.progress_ok

    async def complete_job_ingestion(self, job_id, worker_id, source_metadata):
        return False

    async def fail_job(self, job_id, worker_id, error_category, error_message, permanent=False):
        self.fail_calls.append((job_id, worker_id, error_category, error_message, permanent))
        return "failed" if permanent else "ready"

    async def release_job(self, job_id, worker_id):
        return True

    async def transition_stage(self, job_id, worker_id, from_status, to_status):
        self.transition_calls.append((job_id, worker_id, from_status, to_status))
        return self.transition_result

    async def park_after_analysis(self, job_id, worker_id):
        self.park_calls.append((job_id, worker_id))
        return self.park_ok


class FakeStorage:
    def __init__(self, content: bytes | None = None, fail: bool = False) -> None:
        self.content = content
        self.fail = fail
        self.downloads: list[tuple[str, Path]] = []

    async def download_to(self, object_path: str, destination: Path) -> int:
        self.downloads.append((object_path, destination))
        if self.fail:
            raise StorageDownloadError("simulated transport failure")
        assert self.content is not None
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.content)
        return len(self.content)


class FakeClipStore:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.saved: list[tuple[str, list]] = []

    async def save_clips(self, job_id: str, clips) -> int:
        if self.fail:
            raise ClipsPersistenceError("simulated persistence failure")
        self.saved.append((job_id, list(clips)))
        return len(clips)


def make_settings(tmp_path: Path, ffmpeg: str, ffprobe: str) -> Settings:
    # lease=300s → renewal interval 100s: no renewal fires during a test.
    return Settings(
        environment="development",
        log_level="WARNING",
        storage_root=tmp_path,
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        job_lease_seconds=300,
    )


def make_job(**overrides) -> dict:
    job = {
        "id": JOB_ID,
        "status": "transcribing",
        "source_type": "upload",
        "source_path": SOURCE_PATH,
        "requested_clips": 3,
        "clip_duration_seconds": 90,
        "_worker_id": WORKER_ID,
    }
    job.update(overrides)
    return job


def build(tmp_path, ffmpeg, ffprobe, queue, storage, transcriber, store) -> AnalysisProcessor:
    return AnalysisProcessor(make_settings(tmp_path, ffmpeg, ffprobe), queue, storage, transcriber, store)


def run(coro):
    return asyncio.run(coro)


# ---- happy path ----------------------------------------------------------------

def test_happy_path_transcribes_detects_persists_and_parks(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    queue, storage = FakeQueue(), FakeStorage(content=synthetic_videos["valid"].read_bytes())
    transcriber = ScriptedTranscriber(result=talk_transcript())
    store = FakeClipStore()
    processor = build(tmp_path, ffmpeg_bin, ffprobe_bin, queue, storage, transcriber, store)

    run(processor.process_job(make_job()))

    assert queue.fail_calls == []
    assert queue.transition_calls == [(JOB_ID, WORKER_ID, "transcribing", "analyzing")]
    assert queue.park_calls == [(JOB_ID, WORKER_ID)]
    assert len(transcriber.calls) == 1  # 2s audio → single chunk
    assert transcriber.calls[0].endswith("audio.wav")
    # Clips persisted with the explainable payload.
    job_id, clips = store.saved[0]
    assert job_id == JOB_ID
    assert len(clips) == 3
    for clip in clips:
        assert 60 <= clip.end - clip.start <= 180
        assert clip.title.strip()
        assert 0 <= clip.score <= 100
        assert len(clip.criterions) == 5
        assert clip.scoring["scale"] == "0-100"
    # Progress reported across the whole analysis.
    assert [p for (_, _, p) in queue.progress_calls] == [10, 30, 55, 80, 95]
    # Temp scratch cleaned up.
    assert not (tmp_path / "tmp" / JOB_ID).exists()


def test_crash_recovery_resumes_from_analyzing(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    queue, storage = FakeQueue(transition_result="already"), FakeStorage(content=synthetic_videos["valid"].read_bytes())
    transcriber = ScriptedTranscriber(result=talk_transcript())
    store = FakeClipStore()
    processor = build(tmp_path, ffmpeg_bin, ffprobe_bin, queue, storage, transcriber, store)

    run(processor.process_job(make_job(status="analyzing")))

    assert queue.fail_calls == []
    assert queue.park_calls == [(JOB_ID, WORKER_ID)]
    assert len(store.saved) == 1


# ---- honest failure paths --------------------------------------------------------

def test_unavailable_provider_fails_permanently(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    queue, storage = FakeQueue(), FakeStorage(content=synthetic_videos["valid"].read_bytes())
    transcriber = ScriptedTranscriber(error=TranscriptionUnavailableError("no engine configured"))
    store = FakeClipStore()
    processor = build(tmp_path, ffmpeg_bin, ffprobe_bin, queue, storage, transcriber, store)

    run(processor.process_job(make_job()))

    assert len(queue.fail_calls) == 1
    _, _, category, message, permanent = queue.fail_calls[0]
    assert category == ERROR_TRANSCRIPTION_UNAVAILABLE
    assert permanent is True
    assert "Traceback" not in message
    assert queue.transition_calls == [] and queue.park_calls == [] and store.saved == []
    assert not (tmp_path / "tmp" / JOB_ID).exists()


def test_video_without_audio_fails_permanently(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    queue, storage = FakeQueue(), FakeStorage(content=synthetic_videos["video_only"].read_bytes())
    transcriber = ScriptedTranscriber(result=talk_transcript())
    store = FakeClipStore()
    processor = build(tmp_path, ffmpeg_bin, ffprobe_bin, queue, storage, transcriber, store)

    run(processor.process_job(make_job()))

    assert queue.fail_calls[0][2] == ERROR_NO_AUDIO
    assert queue.fail_calls[0][4] is True
    assert transcriber.calls == []  # never transcribes without audio
    assert store.saved == []


def test_corrupt_source_fails_permanently_with_safe_message(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    queue, storage = FakeQueue(), FakeStorage(content=synthetic_videos["corrupt"].read_bytes())
    processor = build(tmp_path, ffmpeg_bin, ffprobe_bin, queue, storage,
                      ScriptedTranscriber(result=talk_transcript()), FakeClipStore())

    run(processor.process_job(make_job()))

    assert queue.fail_calls[0][2] == ERROR_CORRUPT_MEDIA
    assert queue.fail_calls[0][4] is True
    assert "Traceback" not in queue.fail_calls[0][3]


def test_silent_audio_fails_as_no_speech_permanently(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    empty = TranscriptionResult("en", 0.5, 2.0, (), "")
    queue, storage = FakeQueue(), FakeStorage(content=synthetic_videos["valid"].read_bytes())
    store = FakeClipStore()
    processor = build(tmp_path, ffmpeg_bin, ffprobe_bin, queue, storage, ScriptedTranscriber(result=empty), store)

    run(processor.process_job(make_job()))

    assert queue.fail_calls[0][2] == ERROR_NO_SPEECH
    assert queue.fail_calls[0][4] is True
    assert queue.transition_calls == []  # failed before transcribing → analyzing
    assert store.saved == []


def test_no_detectable_clips_fails_permanently(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    short = talk_transcript(total_seconds=30)
    queue, storage = FakeQueue(), FakeStorage(content=synthetic_videos["valid"].read_bytes())
    store = FakeClipStore()
    processor = build(tmp_path, ffmpeg_bin, ffprobe_bin, queue, storage, ScriptedTranscriber(result=short), store)

    run(processor.process_job(make_job()))

    assert queue.fail_calls[0][2] == ERROR_NO_CLIPS
    assert queue.fail_calls[0][4] is True
    # The stage transition happened (analysis ran), but nothing was persisted.
    assert queue.transition_calls == [(JOB_ID, WORKER_ID, "transcribing", "analyzing")]
    assert store.saved == []


def test_transcription_runtime_error_is_transient(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    queue, storage = FakeQueue(), FakeStorage(content=synthetic_videos["valid"].read_bytes())
    processor = build(tmp_path, ffmpeg_bin, ffprobe_bin, queue, storage,
                      ScriptedTranscriber(error=TranscriptionError("whisper inference failed: RuntimeError")),
                      FakeClipStore())

    run(processor.process_job(make_job()))

    assert queue.fail_calls[0][2] == ERROR_TRANSCRIPTION
    assert queue.fail_calls[0][4] is False


def test_storage_download_failure_is_transient(ffmpeg_bin, ffprobe_bin, tmp_path):
    queue, storage = FakeQueue(), FakeStorage(fail=True)
    processor = build(tmp_path, ffmpeg_bin, ffprobe_bin, queue, storage,
                      ScriptedTranscriber(result=talk_transcript()), FakeClipStore())

    run(processor.process_job(make_job()))

    assert queue.fail_calls[0][2] == ERROR_STORAGE_DOWNLOAD
    assert queue.fail_calls[0][4] is False
    assert not (tmp_path / "tmp" / JOB_ID).exists()


def test_clip_persistence_failure_is_transient(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    queue, storage = FakeQueue(), FakeStorage(content=synthetic_videos["valid"].read_bytes())
    processor = build(tmp_path, ffmpeg_bin, ffprobe_bin, queue, storage,
                      ScriptedTranscriber(result=talk_transcript()), FakeClipStore(fail=True))

    run(processor.process_job(make_job()))

    assert queue.fail_calls[0][2] == ERROR_CLIPS_PERSIST
    assert queue.fail_calls[0][4] is False
    assert queue.park_calls == []  # never parks when persistence failed


def test_missing_ffmpeg_binary_fails_as_unknown_transient(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    queue, storage = FakeQueue(), FakeStorage(content=synthetic_videos["valid"].read_bytes())
    processor = build(tmp_path, "/nonexistent/ffmpeg", ffprobe_bin, queue, storage,
                      ScriptedTranscriber(result=talk_transcript()), FakeClipStore())

    run(processor.process_job(make_job()))

    assert queue.fail_calls[0][2] == ERROR_UNKNOWN
    assert queue.fail_calls[0][4] is False


# ---- ownership / lease safety ----------------------------------------------------

def test_lost_ownership_before_analysis_aborts_without_writes(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    queue, storage = FakeQueue(transition_result="not_owner"), FakeStorage(content=synthetic_videos["valid"].read_bytes())
    store = FakeClipStore()
    processor = build(tmp_path, ffmpeg_bin, ffprobe_bin, queue, storage,
                      ScriptedTranscriber(result=talk_transcript()), store)

    run(processor.process_job(make_job()))

    assert queue.fail_calls == []
    assert queue.park_calls == []
    assert store.saved == []


def test_park_rejection_leaves_no_extra_writes(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    queue, storage = FakeQueue(park_ok=False), FakeStorage(content=synthetic_videos["valid"].read_bytes())
    store = FakeClipStore()
    processor = build(tmp_path, ffmpeg_bin, ffprobe_bin, queue, storage,
                      ScriptedTranscriber(result=talk_transcript()), store)

    run(processor.process_job(make_job()))

    assert len(store.saved) == 1  # clips were written...
    assert queue.park_calls == [(JOB_ID, WORKER_ID)]  # ...but parking failed
    assert queue.fail_calls == []  # ...and we do NOT fake a failure on top of it


def test_progress_rejection_aborts_without_state_writes(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    queue, storage = FakeQueue(progress_ok=False), FakeStorage(fail=True)
    store = FakeClipStore()
    processor = build(tmp_path, ffmpeg_bin, ffprobe_bin, queue, storage,
                      ScriptedTranscriber(result=talk_transcript()), store)

    run(processor.process_job(make_job()))

    assert queue.fail_calls == []
    assert queue.park_calls == []
    assert store.saved == []
    assert not (tmp_path / "tmp" / JOB_ID).exists()


def test_lease_renewal_rejection_cancels_without_writes(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path, monkeypatch):
    queue, storage = FakeQueue(renew_ok=False), FakeStorage(content=synthetic_videos["valid"].read_bytes())
    transcriber = ScriptedTranscriber(result=talk_transcript())
    store = FakeClipStore()
    settings = Settings(
        environment="development", log_level="WARNING", storage_root=tmp_path,
        ffmpeg_path=ffmpeg_bin, ffprobe_path=ffprobe_bin, job_lease_seconds=30,
    )
    processor = AnalysisProcessor(settings, queue, storage, transcriber, store)

    import app.processors.analysis as analysis_module

    async def fake_sleep(_seconds):
        return None  # MUST NOT await anything

    monkeypatch.setattr(analysis_module.asyncio, "sleep", fake_sleep)

    async def scenario():
        processing_task = asyncio.current_task()
        await processor.process_job(make_job(), on_lease_lost=processing_task.cancel)

    with pytest.raises(asyncio.CancelledError):
        run(scenario())

    assert queue.renew_calls
    assert queue.fail_calls == []
    assert queue.park_calls == []
    assert store.saved == []
    assert not (tmp_path / "tmp" / JOB_ID).exists()


def test_unexpected_claimed_status_abandons_silently(ffmpeg_bin, ffprobe_bin, tmp_path):
    queue, storage = FakeQueue(), FakeStorage(fail=True)
    processor = build(tmp_path, ffmpeg_bin, ffprobe_bin, queue, storage,
                      ScriptedTranscriber(result=talk_transcript()), FakeClipStore())

    run(processor.process_job(make_job(status="validating", source_path=None)))

    assert queue.fail_calls == []
    assert queue.park_calls == []
    assert storage.downloads == []


# ---- clip store row mapping ------------------------------------------------------

class FakeTable:
    def __init__(self, owner, name):
        self._owner, self.name = owner, name

    def delete(self):
        return _FakeBuilder(self._owner, self.name, "delete", None)

    def insert(self, rows):
        return _FakeBuilder(self._owner, self.name, "insert", rows)


class _FakeBuilder:
    def __init__(self, owner, table, action, values):
        self._owner, self.table, self.action, self.values = owner, table, action, values
        self.filters: dict = {}

    def eq(self, column, value):
        self.filters[column] = value
        return self

    async def execute(self):
        # AsyncPostgrestQueryBuilder.execute() is a coroutine in supabase-py.
        self._owner.calls.append((self.table, self.action, self.values, dict(self.filters)))
        return type("R", (), {"data": []})()


class FakeClipClient:
    def __init__(self):
        self.calls: list = []

    def table(self, name):
        return FakeTable(self, name)


def test_clip_store_rows_are_scoped_to_the_job_and_shape_correct():
    from app.ai.clip_detection import ScoredClip
    from app.ai.scoring import score_candidate, scoring_payload

    client = FakeClipClient()
    store = SupabaseClipStore(client)
    job_uuid = str(uuid4())
    criterions = score_candidate(10.0, 100.0, [], [], 90.0)
    clips = [
        ScoredClip(start=10.0, end=100.0, title="First idea", score=72.5,
                   criterions=tuple(criterions), scoring=scoring_payload(criterions)),
        ScoredClip(start=150.0, end=240.0, title="Second idea", score=61.0, criterions=(), scoring={}),
    ]
    count = run(store.save_clips(job_uuid, clips))

    assert count == 2
    assert [c[1] for c in client.calls] == ["delete", "insert"]
    # DELETE is scoped to this job only — never another job's rows.
    _t, _a, _v, delete_filters = client.calls[0]
    assert delete_filters == {"job_id": job_uuid}
    _t2, _a2, rows, _f2 = client.calls[1]
    assert len(rows) == 2
    row0 = rows[0]
    assert row0["job_id"] == job_uuid
    assert row0["title"] == "First idea"
    assert row0["start_time"] == 10.0
    assert row0["end_time"] == 100.0
    assert row0["score"] == 72.5
    assert row0["sort_order"] == 0
    assert row0["status"] == "pending"
    assert row0["storage_path"] is None
    assert "duration" not in row0  # DB-generated column, never set by the client
    assert rows[1]["sort_order"] == 1


def test_clip_store_rejects_non_uuid_job_ids():
    store = SupabaseClipStore(FakeClipClient())
    with pytest.raises(ValueError):
        run(store.save_clips("../etc", []))
    with pytest.raises(ValueError):
        run(store.save_clips("not-a-uuid", []))
