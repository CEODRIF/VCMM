"""IngestionProcessor tests: download → FFprobe validation → ready, with
lease-loss abort, permanent/transient failure routing and temp cleanup.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from app.config.settings import Settings
from app.processors.ingestion import IngestionProcessor, is_terminal_status
from app.queue.models import (
    ERROR_CORRUPT_MEDIA,
    ERROR_STORAGE_DOWNLOAD,
    ERROR_UNKNOWN,
    JobStatus,
)
from app.storage.supabase_storage import StorageDownloadError

JOB_ID = str(uuid4())
WORKER_ID = "worker-test-1"
SOURCE_PATH = f"sources/{JOB_ID}/original/generated.mp4"


class FakeQueue:
    def __init__(self, progress_ok: bool = True, renew_ok: bool = True, complete_ok: bool = True) -> None:
        self.progress_ok = progress_ok
        self.renew_ok = renew_ok
        self.complete_ok = complete_ok
        self.progress_calls: list[tuple[str, str, int]] = []
        self.renew_calls: list[tuple[str, str, int]] = []
        self.complete_calls: list[tuple[str, str, dict]] = []
        self.fail_calls: list[tuple[str, str, str, str, bool]] = []
        self.release_calls: list[tuple[str, str]] = []

    async def claim_next_job(self, worker_id, lease_seconds):
        return None

    async def renew_job_lease(self, job_id, worker_id, lease_seconds):
        self.renew_calls.append((job_id, worker_id, lease_seconds))
        return self.renew_ok

    async def update_job_progress(self, job_id, worker_id, progress):
        self.progress_calls.append((job_id, worker_id, progress))
        return self.progress_ok

    async def complete_job_ingestion(self, job_id, worker_id, source_metadata):
        self.complete_calls.append((job_id, worker_id, source_metadata))
        return self.complete_ok

    async def fail_job(self, job_id, worker_id, error_category, error_message, permanent=False):
        self.fail_calls.append((job_id, worker_id, error_category, error_message, permanent))
        return "failed" if permanent else "queued"

    async def release_job(self, job_id, worker_id):
        self.release_calls.append((job_id, worker_id))
        return True


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


def make_settings(tmp_path: Path, ffprobe: str = "ffprobe") -> Settings:
    # lease=300s → renewal interval 100s, so no renewal fires during the test.
    return Settings(
        environment="development",
        log_level="WARNING",
        storage_root=tmp_path,
        ffprobe_path=ffprobe,
        job_lease_seconds=300,
    )


def make_job(**overrides) -> dict:
    job = {
        "id": JOB_ID,
        "source_type": "upload",
        "source_path": SOURCE_PATH,
        "_worker_id": WORKER_ID,
    }
    job.update(overrides)
    return job


def run(coro):
    return asyncio.run(coro)


def test_valid_ingestion_completes_at_ready_with_measured_metadata(ffprobe_bin, synthetic_videos, tmp_path):
    video_bytes = synthetic_videos["valid"].read_bytes()
    queue, storage = FakeQueue(), FakeStorage(content=video_bytes)
    processor = IngestionProcessor(make_settings(tmp_path, ffprobe_bin), queue, storage)

    run(processor.process_job(make_job()))

    assert queue.fail_calls == []
    assert len(queue.complete_calls) == 1
    job_id, worker_id, metadata = queue.complete_calls[0]
    assert (job_id, worker_id) == (JOB_ID, WORKER_ID)
    assert metadata["width"] == 320
    assert metadata["height"] == 240
    assert metadata["video_codec"] == "h264"
    assert metadata["has_audio"] is True
    assert 1.8 <= metadata["duration_seconds"] <= 2.4
    # Progress was reported (10 → 40 → 80).
    assert [p for (_, _, p) in queue.progress_calls] == [10, 40, 80]
    # Temp scratch was cleaned up.
    assert not (tmp_path / "tmp" / JOB_ID).exists()


def test_storage_download_failure_is_transient(ffprobe_bin, tmp_path):
    queue, storage = FakeQueue(), FakeStorage(fail=True)
    processor = IngestionProcessor(make_settings(tmp_path, ffprobe_bin), queue, storage)

    run(processor.process_job(make_job()))

    assert queue.complete_calls == []
    assert len(queue.fail_calls) == 1
    job_id, worker_id, category, message, permanent = queue.fail_calls[0]
    assert (job_id, worker_id) == (JOB_ID, WORKER_ID)
    assert category == ERROR_STORAGE_DOWNLOAD
    assert permanent is False
    assert not (tmp_path / "tmp" / JOB_ID).exists()


def test_corrupt_media_fails_permanently(ffprobe_bin, synthetic_videos, tmp_path):
    garbage = synthetic_videos["corrupt"].read_bytes()
    queue, storage = FakeQueue(), FakeStorage(content=garbage)
    processor = IngestionProcessor(make_settings(tmp_path, ffprobe_bin), queue, storage)

    run(processor.process_job(make_job()))

    assert queue.complete_calls == []
    assert len(queue.fail_calls) == 1
    _, _, category, message, permanent = queue.fail_calls[0]
    assert category == ERROR_CORRUPT_MEDIA
    assert permanent is True
    # Safe, user-facing message — no stack traces.
    assert "Traceback" not in message


def test_missing_source_path_fails_permanently(ffprobe_bin, tmp_path):
    queue, storage = FakeQueue(), FakeStorage()
    processor = IngestionProcessor(make_settings(tmp_path, ffprobe_bin), queue, storage)

    run(processor.process_job(make_job(source_path=None)))

    assert len(queue.fail_calls) == 1
    assert queue.fail_calls[0][2] == ERROR_UNKNOWN
    assert queue.fail_calls[0][4] is True
    assert storage.downloads == []


def test_non_upload_source_type_fails_permanently(ffprobe_bin, tmp_path):
    queue, storage = FakeQueue(), FakeStorage()
    processor = IngestionProcessor(make_settings(tmp_path, ffprobe_bin), queue, storage)

    run(processor.process_job(make_job(source_type="youtube")))

    assert len(queue.fail_calls) == 1
    assert queue.fail_calls[0][2] == ERROR_UNKNOWN
    assert queue.fail_calls[0][4] is True
    assert storage.downloads == []


def test_progress_rejection_aborts_without_any_state_write(ffprobe_bin, tmp_path):
    # The queue refusing a progress update means we lost the lease — the
    # processor must stop and write NOTHING (no fail, no complete).
    queue, storage = FakeQueue(progress_ok=False), FakeStorage(fail=True)
    processor = IngestionProcessor(make_settings(tmp_path, ffprobe_bin), queue, storage)

    run(processor.process_job(make_job()))

    assert queue.complete_calls == []
    assert queue.fail_calls == []
    assert not (tmp_path / "tmp" / JOB_ID).exists()


def test_lease_renewal_rejection_triggers_callback_and_aborts(ffprobe_bin, synthetic_videos, tmp_path, monkeypatch):
    video_bytes = synthetic_videos["valid"].read_bytes()
    queue, storage = FakeQueue(renew_ok=False), FakeStorage(content=video_bytes)
    settings = Settings(
        environment="development",
        log_level="WARNING",
        storage_root=tmp_path,
        ffprobe_path=ffprobe_bin,
        job_lease_seconds=30,  # → renewal interval 10s; sleep is faked below
    )
    processor = IngestionProcessor(settings, queue, storage)

    import app.processors.ingestion as ingestion_module

    async def fake_sleep(_seconds):
        # MUST NOT await anything (asyncio.sleep is what we are replacing).
        return None

    monkeypatch.setattr(ingestion_module.asyncio, "sleep", fake_sleep)

    job = make_job()

    async def scenario():
        processing_task = asyncio.current_task()
        await processor.process_job(job, on_lease_lost=processing_task.cancel)

    with pytest.raises(asyncio.CancelledError):
        run(scenario())

    assert len(queue.renew_calls) >= 1
    assert queue.complete_calls == []
    assert queue.fail_calls == []
    assert not (tmp_path / "tmp" / JOB_ID).exists()


def test_is_terminal_status_marks_ready_terminal_for_ingestion():
    assert is_terminal_status(JobStatus.READY) is True
    assert is_terminal_status(JobStatus.COMPLETED) is True
    assert is_terminal_status(JobStatus.FAILED) is True
    assert is_terminal_status(JobStatus.VALIDATING) is False
    assert is_terminal_status(JobStatus.QUEUED) is False
