"""IngestionWorker loop tests: claiming, bounded processing, backoff,
graceful shutdown. Queue + storage are fakes; the processor and loop are real.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from uuid import uuid4

from app.config.settings import Settings
from app.queue.models import ERROR_PROVIDER_INVALID_URL
from app.queue.supabase_queue import QueueError
from app.worker import IngestionWorker


class FakeQueue:
    def __init__(self) -> None:
        self.claims = 0
        self.last_worker_id: str | None = None
        self.fail_calls: list[tuple[str, str, str, str, bool]] = []
        self.jobs_to_claim: list[dict] = []
        self.raise_queue_error_times = 0

    async def claim_next_job(self, worker_id, lease_seconds):
        self.claims += 1
        self.last_worker_id = worker_id
        if self.raise_queue_error_times > 0:
            self.raise_queue_error_times -= 1
            raise QueueError("claim_next_job failed: RuntimeError")
        if self.jobs_to_claim:
            return self.jobs_to_claim.pop(0)
        return None

    async def renew_job_lease(self, job_id, worker_id, lease_seconds):
        return True

    async def update_job_progress(self, job_id, worker_id, progress):
        return True

    async def complete_job_ingestion(self, job_id, worker_id, source_metadata):
        return True

    async def fail_job(self, job_id, worker_id, error_category, error_message, permanent=False):
        self.fail_calls.append((job_id, worker_id, error_category, error_message, permanent))
        return "failed" if permanent else "queued"

    async def release_job(self, job_id, worker_id):
        return True


class FakeStorage:
    async def download_to(self, object_path, destination):
        raise AssertionError("storage should not be touched for non-upload jobs")


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="development",
        log_level="WARNING",
        storage_root=tmp_path,
        ffprobe_path="ffprobe",  # unused: jobs below fail before probing
        poll_interval=0.05,
        concurrency=2,
        job_lease_seconds=300,
    )


def youtube_job() -> dict:
    job_id = str(uuid4())
    return {
        "id": job_id,
        # Phase 8 (TASK 011): dispatched to the provider layer; without a
        # source URL it fails permanently with a provider input error
        # (still before ANY network or storage traffic).
        "source_type": "youtube",
        "source_path": None,
    }


async def wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"condition not met within {timeout}s")


def run(coro):
    return asyncio.run(coro)


def test_heartbeat_shape_before_start(tmp_path):
    async def scenario():
        worker = IngestionWorker(make_settings(tmp_path), FakeQueue(), FakeStorage())
        heartbeat = worker.heartbeat
        assert heartbeat["worker_id"]
        assert isinstance(heartbeat["worker_id"], str)
        assert heartbeat["active_jobs"] == 0
        assert heartbeat["uptime_seconds"] == 0.0

    run(scenario())


def test_worker_claims_processes_and_stops(tmp_path):
    job = youtube_job()
    queue = FakeQueue()
    queue.jobs_to_claim.append(job)

    async def scenario():
        worker = IngestionWorker(make_settings(tmp_path), queue, FakeStorage())
        await worker.start()
        try:
            await wait_until(lambda: len(queue.fail_calls) == 1)
            # The processor failed the non-upload job permanently...
            job_id, worker_id, category, message, permanent = queue.fail_calls[0]
            assert job_id == job["id"]
            assert worker_id == worker.worker_id
            assert category == ERROR_PROVIDER_INVALID_URL
            assert permanent is True
            # ...wait for the processing task to fully drain...
            await wait_until(lambda: worker.active_jobs == 0)
            await asyncio.sleep(0.15)  # let the (0.1s-rounded) uptime tick
            # ...the heartbeat reflects the running, idle loop...
            assert worker.heartbeat["uptime_seconds"] > 0
            assert worker.heartbeat["active_jobs"] == 0
            # ...and the loop keeps polling after the job drains.
            await wait_until(lambda: queue.claims >= 2)
        finally:
            await worker.stop()
        assert worker.active_jobs == 0
        assert worker._loop_task.done()

    run(scenario())
    assert queue.claims >= 2
    assert len(queue.fail_calls) == 1


def test_graceful_stop_drains_active_jobs_and_cancels_loop(tmp_path):
    queue = FakeQueue()

    async def scenario():
        worker = IngestionWorker(make_settings(tmp_path), queue, FakeStorage())
        await worker.start()
        await wait_until(lambda: queue.claims >= 1)
        await worker.stop()
        assert worker._loop_task.done()
        assert worker.active_jobs == 0
        # No further claims after stop.
        claims_after = queue.claims
        await asyncio.sleep(0.15)
        assert queue.claims == claims_after

    run(scenario())


def test_exponential_backoff_on_claim_errors_resets_on_success(tmp_path):
    queue = FakeQueue()
    queue.raise_queue_error_times = 2

    observed: list[int] = []
    final_streak: list[int] = []

    async def scenario():
        worker = IngestionWorker(make_settings(tmp_path), queue, FakeStorage())
        await worker.start()
        try:
            # Two transport errors, then a successful (empty) claim: the
            # streak climbs 1 → 2, and the success resets it to 0.
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                observed.append(worker._consecutive_claim_errors)
                if queue.claims >= 3:
                    final_streak.append(worker._consecutive_claim_errors)
                    break
                await asyncio.sleep(0.01)
        finally:
            await worker.stop()

    run(scenario())
    assert queue.claims >= 3
    assert queue.fail_calls == []
    assert 2 in observed, "the error streak must climb to 2"
    assert final_streak and final_streak[0] == 0, "a successful claim must reset the streak"


def test_multiple_jobs_are_all_processed(tmp_path):
    queue = FakeQueue()
    queue.jobs_to_claim.extend([youtube_job(), youtube_job()])

    async def scenario():
        worker = IngestionWorker(make_settings(tmp_path), queue, FakeStorage())
        await worker.start()
        try:
            await wait_until(lambda: len(queue.fail_calls) == 2)
        finally:
            await worker.stop()

    run(scenario())
    assert len(queue.fail_calls) == 2
