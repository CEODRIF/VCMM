"""RenderProcessor lifecycle tests (Phase 5).

Fake queue/storage/clip-store + REAL FFmpeg for the rendering path (the
happy path and the resume path render genuine clips end-to-end; failure
injection uses a fake renderer or monkeypatched verification).

Covers: full success, resume (ready clips skipped, no duplicate uploads),
no clips, download/probe/ffmpeg/validation/upload/persistence failures,
lease loss, cancellation, progress refusal, complete refusal, sibling
cancellation, concurrency bounds, and idempotent retry.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

import pytest

from app.config.settings import Settings
from app.processors import rendering as rendering_mod
from app.processors.rendering import RenderProcessor
from app.queue.models import (
    ERROR_CLIPS_PERSIST,
    ERROR_CLIP_UPLOAD,
    ERROR_NO_CLIPS_TO_RENDER,
    ERROR_RENDER_FAILED,
    ERROR_STORAGE_DOWNLOAD,
    ERROR_UNKNOWN,
)
from app.rendering.base import BaseRenderer, RenderSpec
from app.rendering.ffmpeg import FFmpegError
from app.rendering.profiles import RenderProfile
from app.rendering.renderer import FFmpegRenderer
from app.rendering.validation import RenderValidationError
from app.storage.clip_store import ClipsPersistenceError
from app.storage.supabase_storage import StorageDownloadError, StorageUploadError
from tests.conftest import _run_ffmpeg

JOB_ID = str(UUID("11111111-1111-4111-8111-111111111111"))
WORKER = "worker-test"
CLIP_A = str(UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
CLIP_B = str(UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"))
CLIP_C = str(UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"))
PATH_A = f"clips/{JOB_ID}/{CLIP_A}.mp4"
PATH_B = f"clips/{JOB_ID}/{CLIP_B}.mp4"
PATH_C = f"clips/{JOB_ID}/{CLIP_C}.mp4"


def run(coro):
    return asyncio.run(coro)


# ---- fakes --------------------------------------------------------------------

class FakeQueue:
    def __init__(self, progress_ok=True, renew_ok=True, complete_ok=True):
        self.progress_ok = progress_ok
        self.renew_ok = renew_ok
        self.complete_ok = complete_ok
        self.progress: list[tuple[str, str, int]] = []
        self.renew_calls: list[tuple[str, str, float]] = []
        self.complete_calls: list[tuple[str, str]] = []
        self.fail_calls: list[tuple[str, str, str, str, bool]] = []
        self.releases: list[str] = []

    async def update_job_progress(self, job_id, worker_id, value):
        self.progress.append((job_id, worker_id, value))
        return self.progress_ok

    async def renew_job_lease(self, job_id, worker_id, seconds):
        self.renew_calls.append((job_id, worker_id, seconds))
        return self.renew_ok

    async def complete_job_rendering(self, job_id, worker_id):
        self.complete_calls.append((job_id, worker_id))
        return self.complete_ok

    async def fail_job(self, job_id, worker_id, category, message, permanent):
        self.fail_calls.append((job_id, worker_id, category, message, permanent))
        return "failed" if permanent else "queued"

    async def release_job(self, job_id, worker_id):
        self.releases.append(job_id)
        return True


class FakeStorage:
    def __init__(self, content: bytes, download_error=False, upload_error=False, upload_error_paths=()):
        self.content = content
        self.download_error = download_error
        self.upload_error = upload_error
        self.upload_error_paths = set(upload_error_paths)
        self.download_calls: list[tuple[str, Path]] = []
        self.uploads: list[tuple[str, int]] = []  # (object_path, bytes at upload time)

    async def download_to(self, object_path: str, destination: Path) -> int:
        self.download_calls.append((object_path, destination))
        if self.download_error:
            raise StorageDownloadError("download failed")
        destination.write_bytes(self.content)
        return len(self.content)

    async def upload_from(self, object_path: str, source_file: Path, max_bytes: Optional[int] = None) -> int:
        if object_path in self.upload_error_paths or self.upload_error:
            raise StorageUploadError("upload failed")
        size = Path(source_file).stat().st_size
        self.uploads.append((object_path, size))
        return size


class FakeClipStore:
    def __init__(self, clips: Optional[list[dict[str, Any]]] = None, list_error=False, mark_error=False):
        self.clips = clips if clips is not None else []
        self.list_error = list_error
        self.mark_error = mark_error
        self.list_calls: list[str] = []
        self.marks: list[tuple[str, str, str, Optional[str]]] = []  # (job, clip, status, path)

    async def list_clips(self, job_id: str) -> list[dict[str, Any]]:
        self.list_calls.append(job_id)
        if self.list_error:
            raise ClipsPersistenceError("list failed")
        return [dict(c) for c in self.clips]

    async def mark_clip(self, job_id: str, clip_id: str, status: str, storage_path: Optional[str] = None) -> bool:
        if self.mark_error:
            raise ClipsPersistenceError("mark failed")
        self.marks.append((job_id, clip_id, status, storage_path))
        for clip in self.clips:
            if clip["id"] == clip_id and clip["job_id"] == job_id:
                clip["status"] = status
                if storage_path is not None:
                    clip["storage_path"] = storage_path
                return True
        return False


class FakeRenderer:
    """Scriptable renderer: per-clip delay / error, in-flight tracking."""

    profile = RenderProfile()

    def __init__(self, error: Optional[Exception] = None, delay: float = 0.0,
                 fail_clip_index: Optional[int] = None):
        self.error = error
        self.delay = delay
        self.fail_clip_index = fail_clip_index
        self.calls: list[tuple[str, float, float, RenderSpec, str]] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def render(self, source_path: str, start: float, end: float,
                     spec: RenderSpec, output_path: str) -> str:
        index = len(self.calls)
        self.calls.append((source_path, start, end, spec, output_path))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.fail_clip_index is not None and index == self.fail_clip_index:
                raise self.error or FFmpegError(ERROR_RENDER_FAILED, "scripted failure")
            if self.error is not None and self.fail_clip_index is None:
                raise self.error
            if self.delay:
                await asyncio.sleep(self.delay)
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"fake-mp4")
            return output_path
        finally:
            self.in_flight -= 1


def clip(id: str, start: float, end: float, status: str = "pending", storage_path: Optional[str] = None, order: int = 0) -> dict[str, Any]:
    return {
        "id": id, "job_id": JOB_ID, "title": f"clip {id[:8]}",
        "start_time": start, "end_time": end, "status": status,
        "storage_path": storage_path, "sort_order": order,
    }


def make_settings(tmp_path: Path, ffmpeg: str, ffprobe: str, **overrides) -> Settings:
    return Settings(
        environment="development",
        log_level="WARNING",
        worker_enabled=False,
        supabase_url="http://localhost:54321",
        supabase_service_role_key="test-service-role",
        storage_root=tmp_path / "storage",
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        **overrides,
    )


def make_job(status: str = "rendering", source_path: str = f"sources/{JOB_ID}/original/video.mp4") -> dict[str, Any]:
    job: dict[str, Any] = {"id": JOB_ID, "status": status, "_worker_id": WORKER}
    if source_path is not None:
        job["source_path"] = source_path
    return job


def make_processor(settings, queue, storage, store, renderer) -> RenderProcessor:
    return RenderProcessor(settings, queue, storage, store, renderer)


class RecordingFFmpegRenderer(FFmpegRenderer):
    """Real renderer + a window log (the full-stack tests assert on it)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.windows: list[tuple[float, float]] = []

    async def render(self, source_path, start, end, spec, output_path):
        self.windows.append((start, end))
        return await super().render(source_path, start, end, spec, output_path)


async def pass_verify(*_a, **_k):
    """Output-contract stub for transport/logic tests (the fake renderer
    emits placeholder bytes, not real media)."""
    return None


# ---- full success (REAL ffmpeg, real verify, fake transport) -------------------

def test_full_success_renders_uploads_and_completes(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    """Two clips of the 2s fixture: both rendered, verified, uploaded, ready,
    then the DB completion guard is invoked exactly once."""
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin)
    queue = FakeQueue()
    storage = FakeStorage(synthetic_videos["valid"].read_bytes())
    store = FakeClipStore([clip(CLIP_A, 0.0, 1.0, order=0), clip(CLIP_B, 1.0, 2.0, order=1)])
    renderer = RecordingFFmpegRenderer(ffmpeg_bin, ffprobe_bin, timeout_seconds=120)

    processor = make_processor(settings, queue, storage, store, renderer)
    run(processor.process_job(make_job()))

    # Real renders happened for both windows.
    assert renderer.windows == [(0.0, 1.0), (1.0, 2.0)]
    # Both uploaded under the exact object convention, non-empty.
    assert sorted(p for p, _ in storage.uploads) == sorted([PATH_A, PATH_B])
    assert all(size > 0 for _, size in storage.uploads)
    # Every clip: processing → ready with the storage path.
    for cid, path in ((CLIP_A, PATH_A), (CLIP_B, PATH_B)):
        statuses = [m[2] for m in store.marks if m[1] == cid]
        assert statuses == ["processing", "ready"], statuses
        ready = [m for m in store.marks if m[1] == cid and m[2] == "ready"][0]
        assert ready[3] == path
    # Terminal completion: exactly once, owned.
    assert queue.complete_calls == [(JOB_ID, WORKER)]
    assert queue.fail_calls == []
    # Progress: milestones 5/10/15 in order, then 57 and 100 (clip order may vary).
    values = [v for _, _, v in queue.progress]
    assert values[:3] == [5, 10, 15]
    assert sorted(values[3:]) == [57, 100]
    assert values == sorted(values)  # monotonic
    # Temp cleanup.
    assert not (settings.tmp_dir / JOB_ID).exists()


def test_resume_skips_ready_clips_and_avoids_duplicate_uploads(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    """A re-claimed job with clip A already ready renders ONLY clip B and
    never re-uploads A."""
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin)
    queue = FakeQueue()
    storage = FakeStorage(synthetic_videos["valid"].read_bytes())
    store = FakeClipStore([
        clip(CLIP_A, 0.0, 1.0, status="ready", storage_path=PATH_A, order=0),
        clip(CLIP_B, 1.0, 2.0, order=1),
    ])
    renderer = RecordingFFmpegRenderer(ffmpeg_bin, ffprobe_bin, timeout_seconds=120)

    processor = make_processor(settings, queue, storage, store, renderer)
    run(processor.process_job(make_job()))

    assert renderer.windows == [(1.0, 2.0)]  # only clip B rendered
    assert [p for p, _ in storage.uploads] == [PATH_B]              # A never re-uploaded
    assert all(m[1] == CLIP_B for m in store.marks)                 # A untouched
    assert queue.complete_calls == [(JOB_ID, WORKER)]
    assert queue.fail_calls == []


def test_idempotent_retry_after_partial_failure(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path, monkeypatch):
    monkeypatch.setattr(rendering_mod, "verify_render_output", pass_verify)
    """Attempt 1: clip A uploads, clip B's upload fails → transient fail
    (re-queued to parked). Attempt 2 (fresh claim): only B is rendered and
    uploaded; A's object is uploaded EXACTLY once across both attempts."""
    content = synthetic_videos["valid"].read_bytes()
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin, job_lease_seconds=60)
    store = FakeClipStore([clip(CLIP_A, 0.0, 1.0, order=0), clip(CLIP_B, 1.0, 2.0, order=1)])
    storage = FakeStorage(content, upload_error_paths={PATH_B})
    renderer = FakeRenderer()

    # Attempt 1.
    queue1 = FakeQueue()
    processor = make_processor(settings, queue1, storage, store, renderer)
    run(processor.process_job(make_job()))
    assert queue1.fail_calls and queue1.fail_calls[0][2] == ERROR_CLIP_UPLOAD
    assert queue1.fail_calls[0][4] is False  # transient
    assert queue1.complete_calls == []
    assert store.clips[0]["status"] == "ready"       # A persisted
    assert store.clips[1]["status"] == "processing"  # B failed mid-flight; re-rendered on resume (never ready)

    # Attempt 2: a new worker re-claims the parked job.
    storage2 = FakeStorage(content)
    queue2 = FakeQueue()
    renderer2 = FakeRenderer()
    processor2 = make_processor(settings, queue2, storage2, store, renderer2)
    run(processor2.process_job(make_job()))

    assert queue2.complete_calls == [(JOB_ID, WORKER)]
    assert queue2.fail_calls == []
    assert [p for p, _ in storage2.uploads] == [PATH_B]  # only B this attempt
    # No duplicate: A was uploaded once total (attempt 1), B once (attempt 2).
    all_uploads = [p for p, _ in storage.uploads] + [p for p, _ in storage2.uploads]
    assert all_uploads.count(PATH_A) == 1
    assert all_uploads.count(PATH_B) == 1
    assert [(c[1], c[2]) for c in renderer2.calls] == [(1.0, 2.0)]


# ---- failure paths ----------------------------------------------------------------

def test_no_clips_fails_permanently(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin)
    queue = FakeQueue()
    # The source probe happens BEFORE the clip list is read — it must pass.
    storage = FakeStorage(synthetic_videos["valid"].read_bytes())
    store = FakeClipStore([])
    renderer = FakeRenderer()
    run(make_processor(settings, queue, storage, store, renderer).process_job(make_job()))
    assert queue.fail_calls == [(JOB_ID, WORKER, ERROR_NO_CLIPS_TO_RENDER, "the job has no clips to render", True)]
    assert queue.complete_calls == []
    assert renderer.calls == []


def test_download_failure_fails_transiently(ffmpeg_bin, ffprobe_bin, tmp_path):
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin)
    queue = FakeQueue()
    storage = FakeStorage(b"data", download_error=True)
    store = FakeClipStore([clip(CLIP_A, 0.0, 1.0)])
    renderer = FakeRenderer()
    run(make_processor(settings, queue, storage, store, renderer).process_job(make_job()))
    assert queue.fail_calls and queue.fail_calls[0][2] == ERROR_STORAGE_DOWNLOAD
    assert queue.fail_calls[0][4] is False
    assert queue.complete_calls == []
    assert renderer.calls == []


def test_probe_rejects_corrupt_source(ffmpeg_bin, ffprobe_bin, tmp_path, synthetic_videos):
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin)
    queue = FakeQueue()
    storage = FakeStorage(synthetic_videos["corrupt"].read_bytes())
    store = FakeClipStore([clip(CLIP_A, 0.0, 1.0)])
    renderer = FakeRenderer()
    run(make_processor(settings, queue, storage, store, renderer).process_job(make_job()))
    assert queue.fail_calls and queue.fail_calls[0][2] == "corrupt_media"
    assert queue.fail_calls[0][4] is True  # permanent
    assert renderer.calls == []


def test_ffmpeg_failure_fails_transiently(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin)
    queue = FakeQueue()
    storage = FakeStorage(synthetic_videos["valid"].read_bytes())
    store = FakeClipStore([clip(CLIP_A, 0.0, 1.0)])
    renderer = FakeRenderer(error=FFmpegError(ERROR_RENDER_FAILED, "boom"))
    run(make_processor(settings, queue, storage, store, renderer).process_job(make_job()))
    assert queue.fail_calls and queue.fail_calls[0][2] == ERROR_RENDER_FAILED
    assert queue.fail_calls[0][4] is False
    assert queue.complete_calls == []
    assert storage.uploads == []
    assert [m[2] for m in store.marks] == ["processing"]  # never marked ready


def test_output_validation_failure_fails_permanently(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path, monkeypatch):
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin)
    queue = FakeQueue()
    storage = FakeStorage(synthetic_videos["valid"].read_bytes())
    store = FakeClipStore([clip(CLIP_A, 0.0, 1.0)])
    renderer = FakeRenderer()

    async def fake_verify(*_a, **_k):
        raise RenderValidationError("contract violated")

    monkeypatch.setattr(rendering_mod, "verify_render_output", fake_verify)
    run(make_processor(settings, queue, storage, store, renderer).process_job(make_job()))

    assert queue.fail_calls and queue.fail_calls[0][2] == "render_invalid"
    assert queue.fail_calls[0][4] is True  # permanent
    assert storage.uploads == []
    assert queue.complete_calls == []


def test_upload_failure_fails_transiently(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path, monkeypatch):
    monkeypatch.setattr(rendering_mod, "verify_render_output", pass_verify)
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin)
    queue = FakeQueue()
    storage = FakeStorage(synthetic_videos["valid"].read_bytes(), upload_error=True)
    store = FakeClipStore([clip(CLIP_A, 0.0, 1.0)])
    renderer = FakeRenderer()
    run(make_processor(settings, queue, storage, store, renderer).process_job(make_job()))
    assert queue.fail_calls and queue.fail_calls[0][2] == ERROR_CLIP_UPLOAD
    assert queue.fail_calls[0][4] is False
    assert [m[2] for m in store.marks] == ["processing"]
    assert queue.complete_calls == []


def test_clip_list_persistence_failure_fails_transiently(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin)
    queue = FakeQueue()
    storage = FakeStorage(synthetic_videos["valid"].read_bytes())
    store = FakeClipStore(list_error=True)
    renderer = FakeRenderer()
    run(make_processor(settings, queue, storage, store, renderer).process_job(make_job()))
    assert queue.fail_calls and queue.fail_calls[0][2] == ERROR_CLIPS_PERSIST
    assert queue.fail_calls[0][4] is False
    assert renderer.calls == []


def test_mark_persistence_failure_fails_transiently(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin)
    queue = FakeQueue()
    storage = FakeStorage(synthetic_videos["valid"].read_bytes())
    store = FakeClipStore([clip(CLIP_A, 0.0, 1.0)], mark_error=True)
    renderer = FakeRenderer()
    run(make_processor(settings, queue, storage, store, renderer).process_job(make_job()))
    assert queue.fail_calls and queue.fail_calls[0][2] == ERROR_CLIPS_PERSIST
    assert queue.fail_calls[0][4] is False
    assert storage.uploads == []


def test_missing_source_path_fails_permanently(ffmpeg_bin, ffprobe_bin, tmp_path):
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin)
    queue = FakeQueue()
    storage = FakeStorage(b"data")
    store = FakeClipStore([clip(CLIP_A, 0.0, 1.0)])
    renderer = FakeRenderer()
    run(make_processor(settings, queue, storage, store, renderer).process_job(make_job(source_path=None)))
    assert queue.fail_calls and queue.fail_calls[0][2] == ERROR_UNKNOWN
    assert queue.fail_calls[0][4] is True
    assert queue.complete_calls == []
    assert not (settings.tmp_dir / JOB_ID).exists()


def test_unexpected_status_is_silently_abandoned(ffmpeg_bin, ffprobe_bin, tmp_path):
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin)
    queue = FakeQueue()
    storage = FakeStorage(b"data")
    store = FakeClipStore([clip(CLIP_A, 0.0, 1.0)])
    renderer = FakeRenderer()
    run(make_processor(settings, queue, storage, store, renderer).process_job(make_job(status="validating")))
    assert queue.fail_calls == []
    assert queue.complete_calls == []
    assert storage.download_calls == []
    assert renderer.calls == []
    assert not (settings.tmp_dir / JOB_ID).exists()


# ---- lease / cancellation / ownership ---------------------------------------------

def test_lease_renewal_rejection_abandons_without_writes(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path, monkeypatch):
    """renew_ok=False → on_lease_lost cancels the processing task: the job
    dies with NO state writes (the other worker will recover it via the
    expired-lease claim)."""
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin, job_lease_seconds=30)  # renewal interval 10.0
    queue = FakeQueue(renew_ok=False)
    storage = FakeStorage(synthetic_videos["valid"].read_bytes())
    store = FakeClipStore([clip(CLIP_A, 0.0, 1.0)])
    renderer = FakeRenderer()
    processor = make_processor(settings, queue, storage, store, renderer)

    real_sleep = asyncio.sleep

    async def fake_sleep(delay_seconds):
        # The lease interval (10.0) fast-forwards; everything else is a real
        # zero-delay yield (keeps the event loop fair without waiting).
        if delay_seconds == 10.0:
            return None
        return await real_sleep(0)

    monkeypatch.setattr(rendering_mod.asyncio, "sleep", fake_sleep)

    async def scenario():
        processing_task = asyncio.current_task()
        await processor.process_job(make_job(), on_lease_lost=processing_task.cancel)

    with pytest.raises(asyncio.CancelledError):
        run(scenario())

    assert queue.renew_calls and queue.renew_calls[0][:2] == (JOB_ID, WORKER)
    # Ownership lost: NO state writes of any kind.
    assert queue.fail_calls == []
    assert queue.complete_calls == []
    assert storage.uploads == []
    assert all(m[2] != "ready" for m in store.marks)
    assert not (settings.tmp_dir / JOB_ID).exists()


def test_external_cancellation_cleans_up(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin)
    queue = FakeQueue()
    storage = FakeStorage(synthetic_videos["valid"].read_bytes())
    store = FakeClipStore([clip(CLIP_A, 0.0, 1.0)])
    renderer = FakeRenderer(delay=5.0)

    async def scenario():
        task = asyncio.create_task(make_processor(settings, queue, storage, store, renderer).process_job(make_job()))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert queue.fail_calls == []
    assert queue.complete_calls == []
    assert storage.uploads == []
    assert renderer.in_flight == 0  # render slot released by the cancellation
    assert not (settings.tmp_dir / JOB_ID).exists()


def test_progress_refusal_abandons_without_writes(ffmpeg_bin, ffprobe_bin, tmp_path):
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin)
    queue = FakeQueue(progress_ok=False)
    storage = FakeStorage(b"data")
    store = FakeClipStore([clip(CLIP_A, 0.0, 1.0)])
    renderer = FakeRenderer()
    run(make_processor(settings, queue, storage, store, renderer).process_job(make_job()))
    assert queue.progress and queue.progress[0] == (JOB_ID, WORKER, 5)
    assert storage.download_calls == []  # refused before downloading
    assert queue.fail_calls == []
    assert queue.complete_calls == []
    assert not (settings.tmp_dir / JOB_ID).exists()


def test_complete_refusal_abandons_without_fail(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path, monkeypatch):
    monkeypatch.setattr(rendering_mod, "verify_render_output", pass_verify)
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin)
    queue = FakeQueue(complete_ok=False)
    storage = FakeStorage(synthetic_videos["valid"].read_bytes())
    store = FakeClipStore([clip(CLIP_A, 0.0, 1.0)])
    renderer = FakeRenderer()
    run(make_processor(settings, queue, storage, store, renderer).process_job(make_job()))
    # The work itself succeeded...
    assert [p for p, _ in storage.uploads] == [PATH_A]
    assert store.clips[0]["status"] == "ready"
    # ...but completion was refused → abandon (NO fail, no retry loop).
    assert queue.complete_calls == [(JOB_ID, WORKER)]
    assert queue.fail_calls == []


def test_ready_mark_rejection_abandons_without_writes(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path, monkeypatch):
    monkeypatch.setattr(rendering_mod, "verify_render_output", pass_verify)
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin)
    queue = FakeQueue()
    storage = FakeStorage(synthetic_videos["valid"].read_bytes())
    store = FakeClipStore([clip(CLIP_A, 0.0, 1.0)])
    # mark_clip returns False (clip no longer owned) on the READY mark.
    real_mark = store.mark_clip

    async def mark(job_id, clip_id, status, storage_path=None):
        if status == "ready":
            return False
        return await real_mark(job_id, clip_id, status, storage_path)

    store.mark_clip = mark  # type: ignore[method-assign]
    renderer = FakeRenderer()
    # LeaseLostError is caught at the job boundary → clean abandon, no raise.
    run(make_processor(settings, queue, storage, store, renderer).process_job(make_job()))
    assert queue.fail_calls == []
    assert queue.complete_calls == []
    assert store.clips[0]["status"] != "ready"
    assert not (settings.tmp_dir / JOB_ID).exists()


# ---- concurrency and sibling cleanup -------------------------------------------------

def test_sibling_cancellation_when_one_clip_fails(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path, monkeypatch):
    monkeypatch.setattr(rendering_mod, "verify_render_output", pass_verify)
    """Clip B fails immediately; clip A (slow) must be cancelled, never
    uploaded, and the job fails exactly once with the first error."""
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin, render_concurrency=2)
    queue = FakeQueue()
    storage = FakeStorage(synthetic_videos["valid"].read_bytes())
    store = FakeClipStore([clip(CLIP_A, 0.0, 1.0, order=0), clip(CLIP_B, 1.0, 2.0, order=1)])
    renderer = FakeRenderer(delay=3.0, fail_clip_index=1, error=FFmpegError(ERROR_RENDER_FAILED, "clip B broken"))

    run(make_processor(settings, queue, storage, store, renderer).process_job(make_job()))

    assert len(queue.fail_calls) == 1
    assert queue.fail_calls[0][2] == ERROR_RENDER_FAILED
    assert queue.fail_calls[0][4] is False
    assert queue.complete_calls == []
    assert storage.uploads == []  # A was still rendering when B failed → cancelled
    assert store.clips[0]["status"] == "processing"  # A: in flight, never ready
    assert store.clips[1]["status"] == "processing"  # B: failed mid-flight
    assert renderer.max_in_flight == 2  # both ran concurrently before B's failure
    assert renderer.in_flight == 0  # A's slot was released by the cancellation
    assert not (settings.tmp_dir / JOB_ID).exists()


def test_concurrency_limit_bounds_simultaneous_renders(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path, monkeypatch):
    monkeypatch.setattr(rendering_mod, "verify_render_output", pass_verify)
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin, render_concurrency=1)
    queue = FakeQueue()
    storage = FakeStorage(synthetic_videos["valid"].read_bytes())
    store = FakeClipStore([
        clip(CLIP_A, 0.0, 1.0, order=0),
        clip(CLIP_B, 1.0, 2.0, order=1),
        clip(CLIP_C, 0.0, 0.5, order=2),
    ])
    renderer = FakeRenderer(delay=0.05)
    run(make_processor(settings, queue, storage, store, renderer).process_job(make_job()))
    assert renderer.max_in_flight == 1
    assert queue.complete_calls == [(JOB_ID, WORKER)]


def test_concurrency_two_allows_overlap(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path, monkeypatch):
    monkeypatch.setattr(rendering_mod, "verify_render_output", pass_verify)
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin, render_concurrency=2)
    queue = FakeQueue()
    storage = FakeStorage(synthetic_videos["valid"].read_bytes())
    store = FakeClipStore([
        clip(CLIP_A, 0.0, 1.0, order=0),
        clip(CLIP_B, 1.0, 2.0, order=1),
        clip(CLIP_C, 0.0, 0.5, order=2),
    ])
    renderer = FakeRenderer(delay=0.05)
    run(make_processor(settings, queue, storage, store, renderer).process_job(make_job()))
    assert renderer.max_in_flight == 2
    assert queue.complete_calls == [(JOB_ID, WORKER)]


def test_invalid_clip_window_fails_safely(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin)
    queue = FakeQueue()
    storage = FakeStorage(synthetic_videos["valid"].read_bytes())
    store = FakeClipStore([clip(CLIP_A, 2.0, 1.0)])  # start >= end
    renderer = FakeRenderer()
    run(make_processor(settings, queue, storage, store, renderer).process_job(make_job()))
    assert queue.fail_calls and queue.fail_calls[0][2] == ERROR_RENDER_FAILED
    assert queue.complete_calls == []
    assert renderer.calls == []  # window rejected before touching FFmpeg
