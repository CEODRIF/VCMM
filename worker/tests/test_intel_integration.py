"""Integration tests — the REAL job orchestration path (TASK 007, Phase 7b).

These are NOT isolated unit tests: they drive the genuine
``AnalysisProcessor.process_job`` with the resumable stream-intelligence
pipeline wired in (``intel_store`` set), a real FFmpeg decode of a synthetic
source, a real (in-memory) checkpoint store, and a fake queue that records the
EXACT queue-side terminal writes (requeue / transition / park / fail /
progress). Together with the pglite DB tests (tests/database/phase7b-
integration.test.ts) they prove the intelligence pipeline is integrated into
the production job lifecycle, not just present as a module.

Covered (the definition-of-done for the integration):
  1. complete-with-candidates   -> transition, persist intel clips, park;
  2. complete-no-candidates     -> Phase 4 transcript fallback, persist, park;
  3. incomplete (chunk budget)  -> requeue for resume, NO park / NO fail
                                   (never marked complete while chunks remain);
  4. resume after requeue       -> continues from checkpoints, zero
                                   reprocessing, then parks;
  5. a FAILED chunk             -> fail_job (transient), never a silent
                                   incomplete success;
  6. crash recovery at analyzing-> idempotent intel run, 'already' transition,
                                   persist + park;
  7. ownership loss on requeue  -> abandon with no state writes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from app.config.settings import Settings
from app.intel.checkpoints import IntelStore
from app.intel.testing import FakeSupabaseClient
from app.processors.analysis import AnalysisProcessor
from app.queue.models import (
    ERROR_INTEL_ANALYSIS,
    ERROR_INTEL_RESUME_EXHAUSTED,
    ERROR_NO_CLIPS,
)
from app.storage.clip_store import ClipsPersistenceError, SupabaseClipStore
from app.storage.supabase_storage import StorageDownloadError
from app.transcription.base import (
    BaseTranscriber,
    TranscriptionResult,
    TranscriptionSegment,
    TranscriptionWord,
)

from conftest import _run_ffmpeg
from intel_fixtures import FakeSupabaseClient as _IntelFakeClient  # noqa: F401  (re-export sanity)

WORKER_ID = "worker-intel-integration"


# --------------------------------------------------------------------------- #
# Fakes (record the exact queue-side terminal writes)                          #
# --------------------------------------------------------------------------- #

class RecordingQueue:
    """A queue double that records EVERY terminal/state write the processor
    makes, so the tests can assert the precise orchestration."""

    def __init__(
        self,
        transition_result: str = "transitioned",
        park_ok: bool = True,
        requeue_result: str = "ready",
        progress_ok: bool = True,
        renew_ok: bool = True,
    ) -> None:
        self.transition_result = transition_result
        self.park_ok = park_ok
        self.requeue_result = requeue_result
        self.progress_ok = progress_ok
        self.renew_ok = renew_ok
        self.transition_calls: list[tuple] = []
        self.park_calls: list[tuple] = []
        self.requeue_calls: list[tuple] = []
        self.fail_calls: list[tuple] = []
        self.progress_calls: list[tuple] = []

    async def claim_next_job(self, worker_id, lease_seconds):
        return None

    async def renew_job_lease(self, job_id, worker_id, lease_seconds):
        return self.renew_ok

    async def update_job_progress(self, job_id, worker_id, progress):
        self.progress_calls.append((job_id, worker_id, progress))
        return self.progress_ok

    async def complete_job_ingestion(self, job_id, worker_id, source_metadata):
        return True

    async def complete_job_rendering(self, job_id, worker_id):
        return True

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

    async def requeue_incomplete_analysis(self, job_id, worker_id, max_resumes):
        self.requeue_calls.append((job_id, worker_id, max_resumes))
        return self.requeue_result


class FileStorage:
    """Downloads a real video file to the destination (like SupabaseStorage)."""

    def __init__(self, source: Path, fail: bool = False) -> None:
        self.source = source
        self.fail = fail
        self.downloads: list[tuple] = []

    async def download_to(self, object_path, destination: Path) -> int:
        self.downloads.append((object_path, str(destination)))
        if self.fail:
            raise StorageDownloadError("simulated transport failure")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.source.read_bytes())
        return destination.stat().st_size


class RecordingClipStore:
    """Records persisted clip sets; can be made to fail."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.saved: list[tuple[str, list]] = []

    async def save_clips(self, job_id, clips) -> int:
        if self.fail:
            raise ClipsPersistenceError("simulated persistence failure")
        self.saved.append((job_id, list(clips)))
        return len(clips)


def talk_transcript(total_seconds: int = 300) -> TranscriptionResult:
    patterns = [
        "Point number {k} matters because it explains step {k} in detail.",
        "What if you could learn secret {k} in one go?",
        "Here is fact number {k} that most people miss.",
    ]
    segments: list[TranscriptionSegment] = []
    t = 0.0
    k = 0
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

    def __init__(self, result: TranscriptionResult | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[str] = []

    def transcribe(self, audio_path, language=None) -> TranscriptionResult:
        self.calls.append(str(audio_path))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


# --------------------------------------------------------------------------- #
# Source fixtures (real FFmpeg, deterministic)                                 #
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def candidate_source(ffmpeg_bin, tmp_path_factory) -> Path:
    """70 s: quiet bed + a hard cut at 35 s + a noise burst at 35 s.

    The co-located audio reaction + measured cut (mid-source) produce a
    qualifying, multi-family candidate whose context-preserving window pads to
    the 60-180 s product clip contract. Under the 60 s chunk plan this is 2
    chunks -> completes within the budget-2 single claim.
    """
    out = tmp_path_factory.mktemp("intel-int-candidate")
    src = out / "candidate.mp4"
    _run_ffmpeg(
        ffmpeg_bin,
        ["-f", "lavfi", "-i", "testsrc=duration=35:size=320x240:rate=10",
         "-f", "lavfi", "-i", "color=c=red:d=35:s=320x240:r=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=70",
         "-f", "lavfi", "-i", "anoisesrc=d=0.3:color=white:amplitude=0.4",
         "-filter_complex",
         "[0:v][1:v]concat=n=2:v=1:a=0[vid];"
         "[2:a]volume=0.05[a0];[3:a]adelay=35000:all=1[a1];"
         "[a0][a1]amix=inputs=2:normalize=0[aout]",
         "-map", "[vid]", "-map", "[aout]",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(src)],
    )
    return src


@pytest.fixture(scope="module")
def no_signal_source(ffmpeg_bin, tmp_path_factory) -> Path:
    """8 s: fully static color + a constant quiet tone -> NO strong signals,
    so a completed intel run yields zero candidates (drives the fallback)."""
    out = tmp_path_factory.mktemp("intel-int-quiet")
    src = out / "quiet.mp4"
    _run_ffmpeg(
        ffmpeg_bin,
        ["-f", "lavfi", "-i", "color=c=0x204060:d=8:s=320x240:r=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
         "-filter_complex", "[1:a]volume=0.05[aout]",
         "-map", "0:v", "-map", "[aout]",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(src)],
    )
    return src


@pytest.fixture(scope="module")
def long_stream_source(ffmpeg_bin, tmp_path_factory) -> Path:
    """100 s: static red -> static blue hard cut at 50 s + a noise burst at 50 s
    over a quiet bed. Under a 60 s / 30 s overlap plan this is 3 chunks, so a
    per-claim budget of 2 makes the FIRST run incomplete and the SECOND run
    resume from checkpoints and finish (the real resume path)."""
    out = tmp_path_factory.mktemp("intel-int-long")
    src = out / "long.mp4"
    _run_ffmpeg(
        ffmpeg_bin,
        ["-f", "lavfi", "-i", "color=c=red:d=50:s=320x240:r=10",
         "-f", "lavfi", "-i", "color=c=blue:d=50:s=320x240:r=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=100",
         "-f", "lavfi", "-i", "anoisesrc=d=0.3:color=white:amplitude=0.4",
         "-filter_complex",
         "[0:v][1:v]concat=n=2:v=1:a=0[vid];"
         "[2:a]volume=0.05[a0];[3:a]adelay=50000:all=1[a1];"
         "[a0][a1]amix=inputs=2:normalize=0[aout]",
         "-map", "[vid]", "-map", "[aout]",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(src)],
    )
    return src


# --------------------------------------------------------------------------- #
# Processor factory                                                            #
# --------------------------------------------------------------------------- #

def make_settings(tmp_path: Path, ffmpeg: str, ffprobe: str, **overrides) -> Settings:
    base = dict(
        environment="development",
        log_level="WARNING",
        storage_root=tmp_path,
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        job_lease_seconds=300,          # renewal interval 100s: none fires in-test
        # A 60 s chunk / 30 s overlap / budget-2 plan -> the long (100 s) source
        # is 3 chunks, so one claim is always incomplete (drives the resume).
        intel_chunk_seconds=60,
        intel_overlap_seconds=30,
        intel_chunks_per_claim=2,
        intel_chunk_timeout_seconds=180.0,
        intel_scene_threshold=0.3,
        intel_requested_clips=5,
        intel_max_resumes=100000,
    )
    base.update(overrides)
    return Settings(**base)


def make_job(**overrides) -> dict:
    job = {
        "id": str(uuid4()),
        "status": "transcribing",
        "source_type": "upload",
        "source_path": "sources/x/original/v.mp4",
        "requested_clips": 3,
        "clip_duration_seconds": 90,
        "_worker_id": WORKER_ID,
    }
    job.update(overrides)
    return job


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# 1. complete-with-candidates -> transition, persist intel clips, park         #
# --------------------------------------------------------------------------- #

def test_intel_complete_with_candidates_persists_and_parks(
    ffmpeg_bin, ffprobe_bin, candidate_source, tmp_path
):
    queue = RecordingQueue()
    clip_store = RecordingClipStore()
    processor = AnalysisProcessor(
        make_settings(tmp_path, ffmpeg_bin, ffprobe_bin),
        queue, FileStorage(candidate_source), ScriptedTranscriber(result=talk_transcript()),
        clip_store, intel_store=IntelStore(FakeSupabaseClient()),
    )
    job = make_job()
    run(processor.process_job(job))

    assert queue.fail_calls == []
    # Finalization transition happened exactly once.
    assert queue.transition_calls == [(job["id"], WORKER_ID, "transcribing", "analyzing")]
    # No resume requeue (the run completed in a single claim).
    assert queue.requeue_calls == []
    # Parked (awaiting Phase 5 rendering).
    assert queue.park_calls == [(job["id"], WORKER_ID)]
    # Intel clips persisted (NOT transcript clips).
    assert len(clip_store.saved) == 1
    _jid, clips = clip_store.saved[0]
    assert clips, "the burst+cut must yield at least one signal clip"
    for clip in clips:
        dur = clip.end - clip.start
        assert 60 <= dur <= 180, f"clip duration {dur} outside the 60-180s contract"
        assert clip.title.strip()
        assert 0 <= clip.score <= 100
        assert clip.words == ()            # signal clips carry no subtitles
        assert clip.scoring["method"].startswith("stream-intelligence")
    # Temp scratch cleaned.
    assert not (tmp_path / "tmp" / job["id"]).exists()


# --------------------------------------------------------------------------- #
# 2. complete-no-candidates -> transcript fallback, persist, park              #
# --------------------------------------------------------------------------- #

def test_intel_no_candidates_falls_back_to_transcript(
    ffmpeg_bin, ffprobe_bin, no_signal_source, tmp_path
):
    queue = RecordingQueue()
    storage = FileStorage(no_signal_source)
    transcriber = ScriptedTranscriber(result=talk_transcript())
    clip_store = RecordingClipStore()
    processor = AnalysisProcessor(
        make_settings(tmp_path, ffmpeg_bin, ffprobe_bin),
        queue, storage, transcriber, clip_store, intel_store=IntelStore(FakeSupabaseClient()),
    )
    job = make_job()
    run(processor.process_job(job))

    assert queue.fail_calls == []
    assert queue.requeue_calls == []
    assert queue.park_calls == [(job["id"], WORKER_ID)]
    # The transcript fallback actually ran (audio extracted + transcribed).
    assert transcriber.calls, "the fallback must transcribe when intel yields nothing"
    # Transcript (Phase 4) clips persisted — they DO carry the 5 transcript criteria.
    _jid, clips = clip_store.saved[0]
    assert clips
    for clip in clips:
        assert len(clip.criterions) == 5     # the Phase 4 transcript scoring shape


# --------------------------------------------------------------------------- #
# 3+4. incomplete -> requeue (no park/fail) -> resume -> complete -> park      #
# --------------------------------------------------------------------------- #

def test_incomplete_requeues_then_resumes_and_parks(
    ffmpeg_bin, ffprobe_bin, long_stream_source, tmp_path
):
    job_id = str(uuid4())
    store_client = FakeSupabaseClient()     # shared across both claims (the checkpoints)
    queue = RecordingQueue()
    storage = FileStorage(long_stream_source)
    clip_store = RecordingClipStore()
    processor = AnalysisProcessor(
        make_settings(tmp_path, ffmpeg_bin, ffprobe_bin),
        queue, storage, ScriptedTranscriber(result=talk_transcript()),
        clip_store, intel_store=IntelStore(store_client),
    )

    # -- claim 1: the 3-chunk plan vs a budget of 2 -> INCOMPLETE ------------
    job1 = make_job(id=job_id, status="transcribing")
    run(processor.process_job(job1))
    assert queue.fail_calls == []
    assert queue.park_calls == []            # NOT completed while chunks remain
    assert len(queue.requeue_calls) == 1
    _j, _w, max_resumes = queue.requeue_calls[0]
    assert _j == job_id and max_resumes == 100000
    # Progress was reported for the partial run.
    assert queue.progress_calls

    # -- claim 2 (after the requeue -> re-claim as transcribing): RESUME ------
    queue.requeue_calls.clear()
    job2 = make_job(id=job_id, status="transcribing")
    run(processor.process_job(job2))
    assert queue.fail_calls == []
    assert queue.requeue_calls == []         # this run completed (no further resume)
    assert queue.transition_calls == [(job_id, WORKER_ID, "transcribing", "analyzing")]
    assert queue.park_calls == [(job_id, WORKER_ID)]
    _jid, clips = clip_store.saved[0]
    assert clips, "the resumed run must finish with signal clips"
    assert not (tmp_path / "tmp" / job_id).exists()


# --------------------------------------------------------------------------- #
# 5. a FAILED chunk -> fail_job (transient), never a silent incomplete success #
# --------------------------------------------------------------------------- #

def test_failed_chunk_fails_the_job_not_silent_incomplete(
    ffmpeg_bin, ffprobe_bin, candidate_source, tmp_path, monkeypatch
):
    import app.intel.pipeline as pipeline_mod

    real_extract = pipeline_mod.extract_chunk_wav

    async def boom(ffmpeg_path, source, offset, length, destination, timeout_seconds):
        raise pipeline_mod.AudioFeatureError("chunk audio could not be decoded")

    monkeypatch.setattr(pipeline_mod, "extract_chunk_wav", boom)

    queue = RecordingQueue()
    processor = AnalysisProcessor(
        make_settings(tmp_path, ffmpeg_bin, ffprobe_bin),
        queue, FileStorage(candidate_source), ScriptedTranscriber(result=talk_transcript()),
        RecordingClipStore(), intel_store=IntelStore(FakeSupabaseClient()),
    )
    job = make_job()
    run(processor.process_job(job))

    # Exactly ONE terminal write: a TRANSIENT failure (retry resumes the chunk).
    assert len(queue.fail_calls) == 1
    _j, _w, category, message, permanent = queue.fail_calls[0]
    assert category == ERROR_INTEL_ANALYSIS
    assert permanent is False
    assert "Traceback" not in message
    # No park, no requeue-as-incomplete, no clips persisted.
    assert queue.park_calls == []
    assert queue.requeue_calls == []
    assert processor._clip_store.saved == []
    monkeypatch.setattr(pipeline_mod, "extract_chunk_wav", real_extract)


def test_resume_budget_exhausted_fails_permanently(
    ffmpeg_bin, ffprobe_bin, candidate_source, tmp_path, monkeypatch
):
    # Force an "incomplete" pipeline result so the requeue path is exercised and
    # its 'resume_exhausted' outcome (cap reached) is handled as a permanent fail.
    import app.intel.pipeline as pipeline_mod
    from app.intel.pipeline import PipelineResult

    async def incomplete_run(self, job_id, source_path, total_duration, temp_dir, should_stop=None):
        return PipelineResult(status="incomplete", total_chunks=3, processed_chunks=2,
                              remaining_chunks=1, events=[], candidates=[])

    monkeypatch.setattr(pipeline_mod.StreamIntelPipeline, "run", incomplete_run)

    queue = RecordingQueue(requeue_result="resume_exhausted")
    processor = AnalysisProcessor(
        make_settings(tmp_path, ffmpeg_bin, ffprobe_bin),
        queue, FileStorage(candidate_source), ScriptedTranscriber(result=talk_transcript()),
        RecordingClipStore(), intel_store=IntelStore(FakeSupabaseClient()),
    )
    job = make_job()
    run(processor.process_job(job))

    assert len(queue.fail_calls) == 1
    _j, _w, category, _msg, permanent = queue.fail_calls[0]
    assert category == ERROR_INTEL_RESUME_EXHAUSTED
    assert permanent is True
    assert queue.park_calls == []
    assert queue.requeue_calls == [(job["id"], WORKER_ID, 100000)]


# --------------------------------------------------------------------------- #
# 6. crash recovery at analyzing -> idempotent intel run + park               #
# --------------------------------------------------------------------------- #

def test_crash_recovery_at_analyzing_is_idempotent_and_parks(
    ffmpeg_bin, ffprobe_bin, candidate_source, tmp_path
):
    job_id = str(uuid4())
    store_client = FakeSupabaseClient()

    # Seed: a previous run did the analysis and checkpointed the chunk, then
    # CRASHED after the transition (before parking). Simulate by running the
    # pipeline directly (checkpoints persist in store_client).
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin)
    from app.intel.pipeline import PipelineConfig, StreamIntelPipeline

    cfg = PipelineConfig(chunk_seconds=60, overlap_seconds=30, chunks_per_claim=20,
                         chunk_decode_timeout_seconds=180.0, visual_pass_timeout_seconds=180.0,
                         requested_clips=3)
    pipe = StreamIntelPipeline(ffmpeg_bin, IntelStore(store_client), cfg)
    src = candidate_source
    from app.validation.media import probe_media
    info = run(probe_media(ffprobe_bin, src, 60.0))
    seeded = run(pipe.run(job_id, src, info.duration_seconds, tmp_path / "seed"))
    assert seeded.status == "complete"
    assert seeded.candidates, "seed run must have produced candidates"

    # Now the worker RE-CLAIMS the job at 'analyzing' (the crash-recovery stage).
    queue = RecordingQueue(transition_result="already")   # already at analyzing
    clip_store = RecordingClipStore()
    processor = AnalysisProcessor(
        settings, queue, FileStorage(src), ScriptedTranscriber(result=talk_transcript()),
        clip_store, intel_store=IntelStore(store_client),
    )
    run(processor.process_job(make_job(id=job_id, status="analyzing")))

    assert queue.fail_calls == []
    assert queue.requeue_calls == []
    assert queue.park_calls == [(job_id, WORKER_ID)]
    _jid, clips = clip_store.saved[0]
    assert clips, "recovery must persist the (already-computed) signal clips"


# --------------------------------------------------------------------------- #
# 7. ownership lost on the resume requeue -> abandon, no state writes         #
# --------------------------------------------------------------------------- #

def test_ownership_loss_on_requeue_abandons_without_writes(
    ffmpeg_bin, ffprobe_bin, candidate_source, tmp_path, monkeypatch
):
    import app.intel.pipeline as pipeline_mod
    from app.intel.pipeline import PipelineResult

    async def incomplete_run(self, job_id, source_path, total_duration, temp_dir, should_stop=None):
        return PipelineResult(status="incomplete", total_chunks=2, processed_chunks=1,
                              remaining_chunks=1, events=[], candidates=[])

    monkeypatch.setattr(pipeline_mod.StreamIntelPipeline, "run", incomplete_run)

    queue = RecordingQueue(requeue_result="not_owner")
    clip_store = RecordingClipStore()
    processor = AnalysisProcessor(
        make_settings(tmp_path, ffmpeg_bin, ffprobe_bin),
        queue, FileStorage(candidate_source), ScriptedTranscriber(result=talk_transcript()),
        clip_store, intel_store=IntelStore(FakeSupabaseClient()),
    )
    job = make_job()
    run(processor.process_job(job))

    # Abandoned: no fail, no park, no clips, no further writes after the requeue.
    assert queue.fail_calls == []
    assert queue.park_calls == []
    assert clip_store.saved == []
    assert queue.requeue_calls == [(job["id"], WORKER_ID, 100000)]
