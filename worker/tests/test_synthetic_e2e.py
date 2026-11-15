"""TASK 017 P2 — SYNTHETIC END-TO-END validation (worker).

The single test path that proves the WHOLE product journey on locally
generated media — no live external services, no credentials:

    queued → validating → ready → transcribing → analyzing →
    rendering → completed

running on the REAL worker loop (``IngestionWorker._loop`` claim
machine), the REAL Ingestion / Analysis / Render processors, REAL
FFmpeg rendering + REAL ffprobe verification. Only the transports are
deterministic fakes:

* ``InMemoryQueue`` — mirrors the ACTUAL ``claim_next_job`` semantics of
  migration 20261125 (phase7b) branch by branch, plus the guarded
  RPC behaviors (complete / fail / transition / park), re-validating
  ownership (locked_by) exactly like the SECURITY DEFINER RPCs.
* ``E2EStorage`` — file-backed object store emulating the private
  buckets (download_to / upload_from / delete_object).
* ``E2EClipStore`` — in-memory ``public.clips`` (save/list/mark).
* ``ScriptedTranscriber`` — deterministic timed words (the Whisper
  backend is not part of the E2E claim; the transcript is a fixture).

Honesty boundary: success here proves the pipeline, state machine and
render contract on synthetic media — NOT real-world detection quality
on live streams/VODs (those remain gated by provider configuration).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.ai.clip_detection import NoClipsDetectedError, ScoredClip, TranscriptClipDetector
from app.queue.models import (
    ERROR_CORRUPT_MEDIA,
    ERROR_INVALID_FILE,
    ERROR_NO_CLIPS,
)
from app.config.settings import Settings
from app.processors.analysis import AnalysisProcessor
from app.processors.ingestion import IngestionProcessor
from app.processors.rendering import RenderProcessor
from app.rendering.renderer import FFmpegRenderer
from app.storage.supabase_storage import StorageDownloadError
from app.worker import IngestionWorker

from test_analysis_processor import ScriptedTranscriber, talk_transcript
from test_render_processor import make_settings

ACTIVE_STAGES = ("validating", "transcribing", "analyzing", "rendering")
TERMINAL = ("completed", "failed")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# In-memory queue mirroring the phase7b claim_next_job semantics EXACTLY
# ---------------------------------------------------------------------------

class InMemoryQueue:
    """Deterministic twin of the Supabase queue RPCs (phase3 + phase7b)."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.clip_store: Any = None  # bound by the harness (render guard)
        self.claims: list[str] = []
        self.progress_history: list[tuple[str, int]] = []
        self.fail_calls: list[tuple[str, str, str, bool]] = []
        self.ingestion_completions: list[str] = []
        self.rendering_completions: list[str] = []

    def add_job(self, job: dict[str, Any]) -> None:
        """Registers a job row, filling the columns the claim/fail/reaper
        logic reads with their schema defaults (as an INSERT would)."""
        job.setdefault("created_at", _utcnow())
        job.setdefault("updated_at", _utcnow())
        job.setdefault("attempt_count", 0)
        job.setdefault("max_attempts", 3)
        job.setdefault("locked_by", None)
        job.setdefault("locked_at", None)
        job.setdefault("lease_expires_at", None)
        job.setdefault("progress", 0)
        job.setdefault("analysis_resumes", 0)
        job.setdefault("render_attempts", 0)
        job.setdefault("last_error", None)
        job.setdefault("error_category", None)
        job.setdefault("source_metadata", None)
        self.jobs[job["id"]] = job

    async def aclose(self) -> None:  # worker stop() contract
        return None

    async def reap_stale_uploading_jobs(self, max_age_seconds: int) -> int:  # TASK 017
        reaped = 0
        for job in self.jobs.values():
            if job["status"] != "uploading":
                continue
            age = (_utcnow() - job["created_at"]).total_seconds()
            if age > max_age_seconds:
                job["status"] = "failed"
                job["last_error"] = "The upload did not finish in time."
                job["locked_by"] = job["locked_at"] = job["lease_expires_at"] = None
                reaped += 1
        return reaped

    # -- claim (phase7b, branch by branch) ---------------------------------
    async def claim_next_job(self, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
        now = _utcnow()
        candidates = []
        for job in self.jobs.values():
            st = job["status"]
            claimable = (
                (st in ("queued", "ready") and job["attempt_count"] < job["max_attempts"])
                or (st == "ready" and job.get("analysis_resumes", 0) > 0)
                or (st == "analyzing" and job.get("locked_by") is None
                    and job.get("render_attempts", 0) < job.get("max_attempts", 3))
                or (st in ACTIVE_STAGES and job.get("lease_expires_at") is not None
                    and job["lease_expires_at"] < now
                    and job["attempt_count"] < job["max_attempts"])
            )
            if claimable:
                candidates.append(job)
        if not candidates:
            return None
        job = min(candidates, key=lambda j: j["created_at"])
        was_ready_resume = job["status"] == "ready" and job.get("analysis_resumes", 0) > 0
        if job["status"] == "queued":
            job["status"] = "validating"
        elif job["status"] == "ready":
            job["status"] = "transcribing"
        elif job["status"] == "analyzing" and job.get("locked_by") is None:
            job["status"] = "rendering"
        # A mid-analysis resume claim is PROGRESS, not a failure retry.
        if not was_ready_resume:
            job["attempt_count"] += 1
        job["locked_by"] = worker_id
        job["locked_at"] = now
        job["lease_expires_at"] = now + timedelta(seconds=lease_seconds)
        self.claims.append(job["id"])
        return dict(job)

    # -- guarded operations (owner + stage re-validated) ---------------------
    def _owned(self, job_id: str, worker_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        if job is None or job.get("locked_by") != worker_id:
            return None
        return job

    async def renew_job_lease(self, job_id: str, worker_id: str, lease_seconds: int) -> bool:
        job = self._owned(job_id, worker_id)
        if job is None:
            return False
        job["lease_expires_at"] = _utcnow() + timedelta(seconds=lease_seconds)
        return True

    async def update_job_progress(self, job_id: str, worker_id: str, progress: int) -> bool:
        job = self._owned(job_id, worker_id)
        if job is None:
            return False
        job["progress"] = int(progress)
        self.progress_history.append((job_id, int(progress)))
        return True

    async def complete_job_ingestion(self, job_id: str, worker_id: str, source_metadata: dict) -> bool:
        job = self._owned(job_id, worker_id)
        if job is None or job["status"] != "validating":
            return False
        job["status"] = "ready"
        job["source_metadata"] = dict(source_metadata)
        job["locked_by"] = None
        job["locked_at"] = None
        job["lease_expires_at"] = None
        self.ingestion_completions.append(job_id)
        return True

    async def fail_job(self, job_id, worker_id, error_category, error_message,
                       permanent=False) -> str:
        job = self._owned(job_id, worker_id)
        if job is None or job["status"] not in ACTIVE_STAGES:
            return "not_owner"
        job["error_category"] = str(error_category)
        job["last_error"] = str(error_message)
        self.fail_calls.append((job_id, str(error_category), str(error_message), bool(permanent)))
        if permanent or job["attempt_count"] >= job["max_attempts"]:
            job["status"] = "failed"
        else:
            job["status"] = "queued"  # retryable: re-queued for another claim
        job["locked_by"] = None
        job["locked_at"] = None
        job["lease_expires_at"] = None
        return job["status"]

    async def release_job(self, job_id: str, worker_id: str) -> bool:
        job = self._owned(job_id, worker_id)
        if job is None:
            return False
        job["locked_by"] = None
        job["locked_at"] = None
        job["lease_expires_at"] = None
        return True

    async def transition_stage(self, job_id, worker_id, from_status, to_status) -> str:
        job = self.jobs.get(job_id)
        if job is not None and job.get("locked_by") == worker_id:
            if job["status"] == from_status:
                job["status"] = to_status
                return "transitioned"
            if job["status"] == to_status:
                return "already"
        return "not_owner"

    async def park_after_analysis(self, job_id: str, worker_id: str) -> bool:
        job = self._owned(job_id, worker_id)
        if job is None or job["status"] != "analyzing":
            return False
        job["locked_by"] = None
        job["locked_at"] = None
        job["lease_expires_at"] = None
        job["progress"] = 100
        return True

    async def set_job_source(self, job_id: str, worker_id: str, source_path: str) -> bool:
        job = self._owned(job_id, worker_id)
        if job is None or job["status"] != "validating":
            return False
        job["source_path"] = source_path
        return True

    async def requeue_incomplete_analysis(self, job_id, worker_id, max_resumes) -> str:
        job = self._owned(job_id, worker_id)
        if job is None or job["status"] not in ("transcribing", "analyzing"):
            return "not_owner"
        if job.get("analysis_resumes", 0) >= int(max_resumes):
            return "resume_exhausted"
        job["analysis_resumes"] = job.get("analysis_resumes", 0) + 1
        job["status"] = "ready"
        job["locked_by"] = None
        job["locked_at"] = None
        job["lease_expires_at"] = None
        return "ready"

    async def complete_job_rendering(self, job_id: str, worker_id: str) -> bool:
        job = self._owned(job_id, worker_id)
        if job is None or job["status"] != "rendering":
            return False
        rows = [c for c in (self.clip_store.clips if self.clip_store else [])
                if c.get("job_id") == job_id]
        # The DB guard: at least one clip, and EVERY clip ready + stored.
        if not rows or any(c.get("status") != "ready" or not c.get("storage_path")
                           for c in rows):
            return False
        job["status"] = "completed"
        job["progress"] = 100
        job["completed_at"] = _utcnow()
        job["last_error"] = None
        job["locked_by"] = None
        job["locked_at"] = None
        job["lease_expires_at"] = None
        self.rendering_completions.append(job_id)
        return True


# ---------------------------------------------------------------------------
# File-backed storage + clip-store emulations
# ---------------------------------------------------------------------------

class E2EStorage:
    """Owns its bytes: every object is COPIED into the store's root, so a
    source file deleted by its owner (e.g. the worker's temp cleanup) can
    never leave a dangling 'stored' object."""

    def __init__(self, objects: dict[str, Path] | None = None, root: Path | None = None) -> None:
        self.root = root
        self.objects: dict[str, Path] = {}
        for path, source in (objects or {}).items():
            self._put(path, source)
        self.downloads: list[str] = []
        self.uploads: list[tuple[str, Path]] = []
        self.deletes: list[str] = []

    def _put(self, object_path: str, source: Path) -> Path:
        assert self.root is not None
        dest = self.root / object_path.replace("/", "_")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        self.objects[object_path] = dest
        return dest

    async def download_to(self, object_path: str, destination: Path, *a, **k) -> None:
        self.downloads.append(object_path)
        source = self.objects.get(object_path)
        if source is None:
            raise StorageDownloadError(f"object not found: {object_path}")
        shutil.copyfile(source, destination)

    async def upload_from(self, object_path: str, source_file: Path, max_bytes: int, *a, **k) -> str:
        self.uploads.append((object_path, Path(source_file)))
        self._put(object_path, Path(source_file))
        return object_path

    async def delete_object(self, object_path: str, *, source: bool = True) -> bool:
        self.deletes.append(object_path)
        self.objects.pop(object_path, None)
        return True

    async def object_exists(self, object_path: str) -> bool:
        return object_path in self.objects

    async def aclose(self) -> None:  # worker stop() contract
        return None


class E2EClipStore:
    """In-memory public.clips with the worker's exact method contract."""

    def __init__(self) -> None:
        self.clips: list[dict[str, Any]] = []
        self.saves: list[int] = []
        self.marks: list[tuple[str, str, str, str | None]] = []
        self.mark_meta: list[dict] = []

    async def save_clips(self, job_id: str, clips: list) -> int:
        # DELETE-THEN-INSERT scoped to the job (idempotent per job).
        self.clips = [c for c in self.clips if c.get("job_id") != job_id]
        rows = [
            {
                "job_id": job_id,
                "id": str(uuid4()),
                "title": clip.title,
                "start_time": clip.start,
                "end_time": clip.end,
                "score": clip.score,
                "scoring": clip.scoring,
                "captions": None,
                "mode": getattr(clip, "mode", "highlights"),
                "sort_order": index,
                "status": "pending",
                "storage_path": None,
                "width": None, "height": None, "output_fps": None, "file_size_bytes": None,
            }
            for index, clip in enumerate(clips)
        ]
        self.clips.extend(rows)
        self.saves.append(len(rows))
        return len(rows)

    async def list_clips(self, job_id: str) -> list[dict[str, Any]]:
        return [dict(c) for c in self.clips if c.get("job_id") == job_id]

    async def mark_clip(self, job_id, clip_id, status, storage_path=None, **meta) -> bool:
        self.marks.append((job_id, clip_id, status, storage_path))
        if meta:
            self.mark_meta.append(meta)
        for clip in self.clips:
            if clip["id"] == clip_id and clip["job_id"] == job_id:
                clip["status"] = status
                if storage_path is not None:
                    clip["storage_path"] = storage_path
                for key, value in meta.items():
                    clip[key] = value
                return True
        return False

    def rows(self, job_id: str) -> list[dict[str, Any]]:
        return [dict(c) for c in self.clips if c.get("job_id") == job_id]


# ---------------------------------------------------------------------------
# Harness: the REAL worker + processors around one synthetic job
# ---------------------------------------------------------------------------

class NoSignalDetector(TranscriptClipDetector):
    """Deterministic detector that finds NO candidate window (mirrors the
    real detector's honest NoClipsDetectedError for signal-less input)."""

    def detect_scored(self, segments, clip_count, duration_seconds) -> list[ScoredClip]:
        raise NoClipsDetectedError("no qualifying window")


def build_world(tmp_path: Path, ffmpeg_bin: str, ffprobe: str, *,
                source_file: Path, job_overrides: dict[str, Any],
                transcript_seconds: float, detector: Any = None) -> dict[str, Any]:
    settings: Settings = make_settings(
        tmp_path, ffmpeg_bin, ffprobe,
        poll_interval=0.05, render_concurrency=2,
    )
    job_id = str(uuid4())
    job: dict[str, Any] = {
        "id": job_id,
        "status": "queued",
        "source_type": "upload",
        "source_path": f"sources/{job_id}/original/video.mp4",
        "session_id": "e2e-session",
        "captions_enabled": False,
        **job_overrides,
    }
    job["id"] = job_id
    job["source_path"] = f"sources/{job_id}/original/video.mp4"

    storage = E2EStorage({job["source_path"]: source_file}, root=tmp_path / "bucket")
    queue = InMemoryQueue()
    queue.add_job(job)
    clip_store = E2EClipStore()
    queue.clip_store = clip_store
    transcriber = ScriptedTranscriber(talk_transcript(max(4, int(transcript_seconds))))
    analysis = AnalysisProcessor(settings, queue, storage, transcriber, clip_store,
                                 detector=detector)
    ingestion = IngestionProcessor(settings, queue, storage)
    renderer = FFmpegRenderer(ffmpeg_bin, ffprobe, timeout_seconds=240)
    rendering = RenderProcessor(settings, queue, storage, clip_store, renderer)
    worker = IngestionWorker(settings, queue, storage,
                             analysis=analysis, ingestion=ingestion, rendering=rendering)
    return {"settings": settings, "job_id": job_id, "job": queue.jobs[job_id],
            "queue": queue, "storage": storage, "clip_store": clip_store,
            "transcriber": transcriber, "worker": worker}


async def run_until_terminal(world: dict[str, Any], timeout: float = 240.0) -> None:
    queue: InMemoryQueue = world["queue"]
    job_id = world["job_id"]
    worker: IngestionWorker = world["worker"]
    await worker.start()
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if queue.jobs[job_id]["status"] in TERMINAL:
                return
            await asyncio.sleep(0.1)
        pytest.fail(f"job {job_id} did not reach a terminal state; "
                   f"status={queue.jobs[job_id]['status']!r} claims={queue.claims}")
    finally:
        await worker.stop()


def _ffprobe_json(ffprobe_bin: str, path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [ffprobe_bin, "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout)


def _make_long_source(tmp_path: Path, ffmpeg_bin: str, seconds: int) -> Path:
    out = tmp_path / f"source_{seconds}s.mp4"
    subprocess.run([
        ffmpeg_bin, "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=1920x1080:rate=30",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-shortest", str(out),
    ], check=True)
    return out


# ---------------------------------------------------------------------------
# 6. missing source object → typed RETRYABLE failure, then terminal after
#    the attempt budget is exhausted (never a silent success)
# ---------------------------------------------------------------------------

def test_e2e_missing_source_object_retries_then_fails_typed(
        tmp_path, ffmpeg_bin, ffprobe_bin, render_media):
    world = build_world(
        tmp_path, ffmpeg_bin, ffprobe_bin,
        source_file=render_media["landscape"],
        job_overrides={"generation_mode": "full_coverage", "requested_clips": 2,
                       "clip_duration_seconds": 3, "output_fps": 30},
        transcript_seconds=6,
    )
    job_id = world["job_id"]
    queue: InMemoryQueue = world["queue"]
    # The object vanished (e.g. a storage-side deletion race): ingestion's
    # download must fail with the typed, RETRYABLE storage category.
    del world["storage"].objects[world["job"]["source_path"]]

    asyncio.run(run_until_terminal(world, timeout=120))

    job = queue.jobs[job_id]
    assert job["status"] == "failed"
    assert job["error_category"] == "storage_download", job["error_category"]
    # Non-permanent: it consumed the FULL attempt budget (3 claims) before
    # going terminal — the retry path was real, not skipped.
    assert queue.claims.count(job_id) == job["max_attempts"] == 3
    assert len(queue.fail_calls) == 3 and all(not p for *_, p in queue.fail_calls)
    assert not world["clip_store"].rows(job_id)
    assert job["last_error"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ffprobe_json(ffprobe_bin: str, path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [ffprobe_bin, "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout)


def _make_long_source(tmp_path: Path, ffmpeg_bin: str, seconds: int) -> Path:
    out = tmp_path / f"source_{seconds}s.mp4"
    subprocess.run([
        ffmpeg_bin, "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=1920x1080:rate=30",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-shortest", str(out),
    ], check=True)
    return out


# ---------------------------------------------------------------------------
# 1. full_coverage: the complete journey — measured, persisted, verified
# ---------------------------------------------------------------------------

def test_e2e_full_coverage_lifecycle_completes_with_verified_clips(
        tmp_path, ffmpeg_bin, ffprobe_bin, render_media):
    world = build_world(
        tmp_path, ffmpeg_bin, ffprobe_bin,
        source_file=render_media["landscape"],
        job_overrides={"generation_mode": "full_coverage", "requested_clips": 2,
                       "clip_duration_seconds": 3, "output_fps": 30},
        transcript_seconds=6,
    )
    queue: InMemoryQueue = world["queue"]
    job_id = world["job_id"]
    asyncio.run(run_until_terminal(world, timeout=180))

    job = queue.jobs[job_id]
    # Completed — through the legal stage path only (never a skipped stage).
    assert job["status"] == "completed", f"status={job['status']} error={job['last_error']}"
    assert job["progress"] == 100
    assert job["completed_at"] is not None
    assert queue.claims.count(job_id) >= 3  # validating → transcribing → rendering
    # Ingestion recorded REAL source metadata (from the real ffprobe).
    assert (job["source_metadata"] or {}).get("duration_seconds") == pytest.approx(6.0, abs=0.35)
    # Progress values are always sane (0..100); final is 100.
    history = [p for jid, p in queue.progress_history if jid == job_id]
    assert history and all(0 <= p <= 100 for p in history) and history[-1] == 100
    # BOTH clips rendered, stored at canonical paths, with measured facts.
    clips = world["clip_store"].rows(job_id)
    assert len(clips) == 2
    for clip in clips:
        assert clip["status"] == "ready"
        assert clip["storage_path"] == f"clips/{job_id}/{clip['id']}.mp4"
        assert clip["width"] == 1080 and clip["height"] == 1920
        assert clip["output_fps"] == pytest.approx(30.0, rel=0.05)
        assert (clip["file_size_bytes"] or 0) > 0
        # The stored object is a REAL, ffprobe-verified MP4.
        meta = _ffprobe_json(ffprobe_bin, world["storage"].objects[clip["storage_path"]])
        v = next(s for s in meta["streams"] if s["codec_type"] == "video")
        assert v["codec_name"] == "h264"
        assert (v["width"], v["height"]) == (1080, 1920)
        assert any(s["codec_type"] == "audio" and s["codec_name"] == "aac"
                   for s in meta["streams"])
        assert int(meta["format"]["size"]) > 0
        assert float(meta["format"]["duration"]) == pytest.approx(3.0, abs=0.35)
    # Default retention: the source object is kept, nothing deleted.
    assert f"sources/{job_id}/original/video.mp4" in world["storage"].objects
    assert world["storage"].deletes == []


# ---------------------------------------------------------------------------
# 2. highlights: Gemini unconfigured, real detector, real 90 s source
# ---------------------------------------------------------------------------

def test_e2e_highlights_lifecycle_without_gemini(tmp_path, ffmpeg_bin, ffprobe_bin):
    source = _make_long_source(tmp_path, ffmpeg_bin, 90)
    world = build_world(
        tmp_path, ffmpeg_bin, ffprobe_bin,
        source_file=source,
        job_overrides={"generation_mode": "highlights", "requested_clips": 1,
                       "clip_duration_seconds": 60, "output_fps": 30},
        transcript_seconds=90,
    )
    queue: InMemoryQueue = world["queue"]
    job_id = world["job_id"]
    asyncio.run(run_until_terminal(world, timeout=420))

    job = queue.jobs[job_id]
    assert job["status"] == "completed", f"status={job['status']} error={job['last_error']}"
    clips = world["clip_store"].rows(job_id)
    # Exactly the requested count was found, rendered and stored.
    assert len(clips) == 1
    clip = clips[0]
    assert clip["status"] == "ready" and clip["storage_path"]
    duration = clip["end_time"] - clip["start_time"]
    assert 45 <= duration <= 90  # legal highlight length, clipped to the source
    meta = _ffprobe_json(ffprobe_bin, world["storage"].objects[clip["storage_path"]])
    assert next(s for s in meta["streams"] if s["codec_type"] == "video")["codec_name"] == "h264"
    # Gemini unconfigured: no enrichment, no network, no failure.
    assert job["error_category"] is None
    assert world["transcriber"].calls  # the (fake) transcription actually ran


# ---------------------------------------------------------------------------
# 3. no qualifying highlights → typed permanent failure (never fake success)
# ---------------------------------------------------------------------------

def test_e2e_highlights_with_no_candidates_fails_typed(tmp_path, ffmpeg_bin, ffprobe_bin):
    source = _make_long_source(tmp_path, ffmpeg_bin, 90)
    world = build_world(
        tmp_path, ffmpeg_bin, ffprobe_bin,
        source_file=source,
        job_overrides={"generation_mode": "highlights", "requested_clips": 1,
                       "clip_duration_seconds": 60, "output_fps": 30},
        transcript_seconds=90,
        detector=NoSignalDetector(),
    )
    queue: InMemoryQueue = world["queue"]
    job_id = world["job_id"]
    asyncio.run(run_until_terminal(world, timeout=120))

    job = queue.jobs[job_id]
    assert job["status"] == "failed"
    assert job["error_category"] == ERROR_NO_CLIPS
    # The failure is at ANALYSIS: nothing downstream ran, nothing was stored.
    assert not world["clip_store"].rows(job_id)
    assert not any(p.startswith(f"clips/{job_id}") for p in world["storage"].objects)
    assert not world["storage"].uploads
    assert (job["last_error"] or "").strip()  # a real, user-visible message
    # Permanent: the failed job is NOT re-queued for retry.
    assert job["status"] == "failed" and job["locked_by"] is None


# ---------------------------------------------------------------------------
# 4. invalid source media → typed permanent failure at ingestion
# ---------------------------------------------------------------------------

def test_e2e_invalid_source_media_fails_typed(tmp_path, ffmpeg_bin, ffprobe_bin):
    garbage = tmp_path / "garbage.mp4"
    garbage.write_bytes(b"this is definitely not a video file" * 40)
    world = build_world(
        tmp_path, ffmpeg_bin, ffprobe_bin,
        source_file=garbage,
        job_overrides={"generation_mode": "full_coverage", "requested_clips": 2,
                       "clip_duration_seconds": 3, "output_fps": 30},
        transcript_seconds=6,
    )
    queue: InMemoryQueue = world["queue"]
    job_id = world["job_id"]
    asyncio.run(run_until_terminal(world, timeout=60))

    job = queue.jobs[job_id]
    assert job["status"] == "failed"
    assert job["error_category"] in (ERROR_INVALID_FILE, ERROR_CORRUPT_MEDIA), job["error_category"]
    # Failed at INGESTION: exactly one claim (permanent — no retry), and no
    # downstream stage ever ran.
    assert queue.claims == [job_id]
    assert not world["clip_store"].rows(job_id)
    assert not world["storage"].uploads
    assert job["last_error"]


# ---------------------------------------------------------------------------
# 5. crash recovery: expired lease → re-claim resumes the SAME stage
# ---------------------------------------------------------------------------

def test_e2e_crashed_claim_is_recovered_without_data_loss(
        tmp_path, ffmpeg_bin, ffprobe_bin, render_media):
    world = build_world(
        tmp_path, ffmpeg_bin, ffprobe_bin,
        source_file=render_media["landscape"],
        job_overrides={"generation_mode": "full_coverage", "requested_clips": 2,
                       "clip_duration_seconds": 3, "output_fps": 30},
        transcript_seconds=6,
    )
    queue: InMemoryQueue = world["queue"]
    job_id = world["job_id"]

    async def scenario():
        # Phase 1 — the worker runs the job through ingestion (ready).
        worker: IngestionWorker = world["worker"]
        await worker.start()
        try:
            deadline = time.monotonic() + 60
            while queue.jobs[job_id]["status"] not in ("ready", *TERMINAL):
                if time.monotonic() > deadline:
                    pytest.fail(f"stuck before crash simulation: {queue.jobs[job_id]['status']}")
                await asyncio.sleep(0.05)
            assert queue.jobs[job_id]["status"] == "ready"
        finally:
            await worker.stop()

        # Phase 2 — deterministic crash: the job is in the analysis stage
        # (either just claimed by the stopped worker or claimed now by a
        # "crashed-worker") and its lease simply expires, un-renewed.
        j = queue.jobs[job_id]
        if j["status"] == "ready":
            claimed = await queue.claim_next_job("crashed-worker", 30)
            assert claimed is not None and claimed["status"] == "transcribing"
        assert j["status"] == "transcribing"
        j["locked_by"] = "crashed-worker"
        j["lease_expires_at"] = _utcnow() - timedelta(seconds=1)

        # Phase 3 — a fresh live worker must re-take the job via the
        # lease-expiry branch and complete it from the SAME stage.
        worker2 = IngestionWorker(
            world["settings"], queue, world["storage"],
            analysis=world["worker"]._analysis, ingestion=world["worker"]._ingestion,
            rendering=world["worker"]._rendering,
        )
        world["worker"] = worker2
        await run_until_terminal(world, timeout=180)

    asyncio.run(scenario())

    job = queue.jobs[job_id]
    assert job["status"] == "completed", f"error={job['last_error']}"
    assert len(world["clip_store"].rows(job_id)) == 2
    # Ingestion ran exactly ONCE (no double materialization after recovery).
    assert queue.ingestion_completions.count(job_id) == 1
    assert queue.rendering_completions.count(job_id) == 1
