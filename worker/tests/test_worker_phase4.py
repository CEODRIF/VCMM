"""Worker dispatch tests (Phase 4): stage-aware processor selection."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

from app.config.settings import Settings
from app.worker import IngestionWorker


class RecordingProcessor:
    def __init__(self, name: str) -> None:
        self.name = name
        self.processed: list[dict] = []

    async def process_job(self, job, on_lease_lost=None):
        self.processed.append(dict(job))


class FakeQueue:
    def __init__(self) -> None:
        self.releases: list[tuple[str, str]] = []
        self.fail_calls: list = []

    async def claim_next_job(self, worker_id, lease_seconds):
        return None

    async def renew_job_lease(self, job_id, worker_id, lease_seconds):
        return True

    async def update_job_progress(self, job_id, worker_id, progress):
        return True

    async def complete_job_ingestion(self, job_id, worker_id, source_metadata):
        return True

    async def fail_job(self, job_id, worker_id, error_category, error_message, permanent=False):
        self.fail_calls.append((job_id, worker_id, error_category, error_message, permanent))
        return "failed" if permanent else "ready"

    async def release_job(self, job_id, worker_id):
        self.releases.append((job_id, worker_id))
        return True

    async def transition_stage(self, job_id, worker_id, from_status, to_status):
        return "transitioned"

    async def park_after_analysis(self, job_id, worker_id):
        return True


class FakeStorage:
    async def download_to(self, object_path, destination):
        return 0


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="development",
        log_level="WARNING",
        storage_root=tmp_path,
        poll_interval=0.05,
        concurrency=2,
        job_lease_seconds=300,
    )


def job_with_status(status: str) -> dict:
    return {"id": str(uuid4()), "status": status, "source_path": None}


def run(coro):
    return asyncio.run(coro)


def test_transcribing_job_goes_to_analysis_processor(tmp_path):
    queue = FakeQueue()
    ingestion, analysis = RecordingProcessor("ingestion"), RecordingProcessor("analysis")
    worker = IngestionWorker(make_settings(tmp_path), queue, FakeStorage(),
                             analysis=analysis, ingestion=ingestion)

    run(worker._guarded(job_with_status("transcribing")))

    assert len(analysis.processed) == 1
    assert ingestion.processed == []


def test_analyzing_job_goes_to_analysis_processor(tmp_path):
    queue = FakeQueue()
    ingestion, analysis = RecordingProcessor("ingestion"), RecordingProcessor("analysis")
    worker = IngestionWorker(make_settings(tmp_path), queue, FakeStorage(),
                             analysis=analysis, ingestion=ingestion)

    run(worker._guarded(job_with_status("analyzing")))

    assert len(analysis.processed) == 1
    assert ingestion.processed == []


def test_validating_job_still_goes_to_ingestion(tmp_path):
    # Phase 3 regression: ingestion behavior is untouched.
    queue = FakeQueue()
    ingestion, analysis = RecordingProcessor("ingestion"), RecordingProcessor("analysis")
    worker = IngestionWorker(make_settings(tmp_path), queue, FakeStorage(),
                             analysis=analysis, ingestion=ingestion)

    run(worker._guarded(job_with_status("validating")))

    assert len(ingestion.processed) == 1
    assert analysis.processed == []


def test_analysis_stage_without_analysis_capability_releases_not_fails(tmp_path):
    queue = FakeQueue()
    worker = IngestionWorker(make_settings(tmp_path), queue, FakeStorage(),
                             analysis=None, ingestion=RecordingProcessor("ingestion"))

    job = job_with_status("transcribing")
    run(worker._guarded(job))

    assert queue.releases == [(job["id"], worker.worker_id)]
    assert queue.fail_calls == []


def test_heartbeat_shape_with_analysis_enabled(tmp_path):
    async def scenario():
        worker = IngestionWorker(make_settings(tmp_path), FakeQueue(), FakeStorage(),
                                 analysis=RecordingProcessor("analysis"),
                                 ingestion=RecordingProcessor("ingestion"))
        heartbeat = worker.heartbeat
        assert heartbeat["active_jobs"] == 0
        assert heartbeat["worker_id"]

    run(scenario())
