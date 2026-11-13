"""TASK 012 worker-lifecycle tests (spec sections 4, 5, 7, 10):

- Bounded, JITTERED exponential backoff on claim failures (deterministic —
  ``random`` is monkeypatched).
- Graceful shutdown: in-flight jobs that finish inside the grace window are
  awaited; jobs still running after the grace window are ABANDONED (cancelled)
  and NO final state write is performed (the lease simply expires).
- Stale temp-directory cleanup on start (age threshold + live-job exclusion).
- Dependency probing for /ready (real, bounded, cached; probes never raise).
- Shutdown releases HTTP clients (queue + storage + ingestion ``aclose``).

Everything is fake queue / fake storage — deterministic and offline.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from uuid import uuid4

from app.config.settings import Settings
from app.queue.supabase_queue import QueueError
from app.worker import IngestionWorker


# -- fakes --------------------------------------------------------------------------


class FakeQueue:
    def __init__(self) -> None:
        self.closed = False
        self.ping_failures = 0
        self.claims = 0
        self.jobs_to_claim: list[dict] = []
        self.fail_calls: list[tuple] = []
        self.complete_calls: list[tuple] = []
        self.raise_queue_error_times = 0

    async def ping(self):
        if self.ping_failures > 0:
            self.ping_failures -= 1
            raise RuntimeError("db down")
        return True

    async def aclose(self):
        self.closed = True

    async def claim_next_job(self, worker_id, lease_seconds):
        self.claims += 1
        if self.raise_queue_error_times > 0:
            self.raise_queue_error_times -= 1
            raise QueueError("claim_next_job failed: RuntimeError")
        return self.jobs_to_claim.pop(0) if self.jobs_to_claim else None

    async def renew_job_lease(self, job_id, worker_id, lease_seconds):
        return True

    async def update_job_progress(self, job_id, worker_id, progress):
        return True

    async def complete_job_ingestion(self, job_id, worker_id, source_metadata):
        self.complete_calls.append((job_id, worker_id))
        return True

    async def fail_job(self, job_id, worker_id, error_category, error_message, permanent=False):
        self.fail_calls.append((job_id, worker_id, error_category, error_message, permanent))
        return "failed" if permanent else "queued"

    async def release_job(self, job_id, worker_id):
        return True


class FakeStorage:
    def __init__(self) -> None:
        self.closed = False
        self.ping_failures = 0

    async def ping(self):
        if self.ping_failures > 0:
            self.ping_failures -= 1
            raise RuntimeError("storage down")
        return True

    async def aclose(self):
        self.closed = True

    async def download_to(self, object_path, destination):
        raise AssertionError("storage should not be touched for these jobs")


class SlowIngestion:
    """Ingestion double that simulates a long-running job: it completes after
    ``delay`` seconds, or runs forever until cancelled."""

    def __init__(self, delay: float | None = None) -> None:
        self.delay = delay
        self.processed: list[str] = []
        self.cancelled: list[str] = []
        self.aclose_called = False
        self.on_lease_lost_seen: list[str | None] = []

    async def aclose(self):
        self.aclose_called = True

    async def process_job(self, job, on_lease_lost=None):
        self.on_lease_lost_seen.append(job.get("id"))
        if self.delay is not None:
            await asyncio.sleep(self.delay)
            self.processed.append(job["id"])
            return
        try:
            await asyncio.sleep(3600)  # "forever" until cancelled
        except asyncio.CancelledError:
            self.cancelled.append(job["id"])
            raise


def make_settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        environment="development",
        log_level="WARNING",
        storage_root=tmp_path,
        ffprobe_path="ffprobe",
        poll_interval=0.02,
        concurrency=2,
        job_lease_seconds=300,
    )
    base.update(overrides)
    return Settings(**base)


def validating_job() -> dict:
    return {"id": str(uuid4()), "status": "validating", "source_type": "upload",
            "source_path": "x.mp4"}


def run(coro):
    return asyncio.run(coro)


# -- backoff: bounded, exponential, JITTERED, deterministic --------------------------


def test_claim_backoff_is_bounded_exponential_and_jittered(tmp_path, monkeypatch):
    """Monkeypatched random: uniform(a, b) is recorded AND returns a value.
    With poll_interval=0.02 the base doubles per consecutive error:
    0.04, 0.08, 0.16, 0.32, 0.64, then capped (2**5 is the max exponent).
    The jittered delay always lands in [base/2, base] — deterministic under
    the patched random."""
    calls: list[tuple[float, float]] = []

    def fake_uniform(a, b):
        calls.append((a, b))
        return b  # deterministic: pick the top of the jitter range

    monkeypatch.setattr("app.worker.random.uniform", fake_uniform)
    settings = make_settings(tmp_path, poll_interval=0.02)
    queue = FakeQueue()
    queue.raise_queue_error_times = 6
    worker = IngestionWorker(settings, queue, FakeStorage(), ingestion=SlowIngestion())

    async def scenario():
        await worker.start()
        # Let the loop hit the claim path (and back off) a handful of times.
        deadline = time.monotonic() + 5.0
        while len(calls) < 6 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        await worker.stop()

    run(scenario())

    assert len(calls) >= 6, f"expected >=6 jitter draws, got {len(calls)}"
    for i, (a, b) in enumerate(calls[:5], start=1):
        base = min(60.0, 0.02 * (2 ** min(i, 5)))
        assert a == base / 2.0, f"error #{i}: jitter low {a} != {base / 2.0}"
        assert b == base, f"error #{i}: jitter high {b} != {base}"
    # The 6th error uses the capped exponent (min(n, 5) -> base frozen).
    assert calls[5][1] == calls[4][1]


def test_claim_backoff_resets_after_success(tmp_path, monkeypatch):
    """A successful claim must reset the consecutive-error counter so the next
    failure starts backoff from the FIRST (smallest) step again."""
    calls: list[tuple[float, float]] = []

    def fake_uniform(a, b):
        calls.append((a, b))
        return b

    def wait_for(pred):
        async def _w():
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not pred():
                await asyncio.sleep(0.01)
            assert pred(), "condition not met within 5s"
        return _w()

    monkeypatch.setattr("app.worker.random.uniform", fake_uniform)
    settings = make_settings(tmp_path, poll_interval=0.02)
    queue = FakeQueue()
    queue.raise_queue_error_times = 1
    queue.jobs_to_claim.append(validating_job())
    worker = IngestionWorker(settings, queue, FakeStorage(), ingestion=SlowIngestion(delay=0))

    async def scenario():
        await worker.start()
        # 1) one claim error -> backoff draw #1 (step 1)
        await wait_for(lambda: len(calls) == 1)
        # 2) a successful claim (the queued job is processed)
        await wait_for(lambda: worker._ingestion.processed)
        # 3) one more claim error -> draw #2 must be step 1 AGAIN
        queue.raise_queue_error_times = 1
        await wait_for(lambda: len(calls) == 2)
        await worker.stop()

    run(scenario())

    # First failure: step 1 (poll*2^1). Second failure after a success:
    # step 1 again — the counter was reset by the successful claim.
    assert calls[0][1] == 0.04
    assert calls[1][1] == 0.04


# -- graceful shutdown ---------------------------------------------------------------


def test_shutdown_waits_for_jobs_finishing_inside_grace(tmp_path):
    """A job that finishes BEFORE the grace deadline must be processed to
    completion — never cancelled, never marked failed by the shutdown."""
    queue = FakeQueue()
    job = validating_job()
    queue.jobs_to_claim.append(job)
    worker = IngestionWorker(
        make_settings(tmp_path, worker_shutdown_grace_seconds=30),
        queue, FakeStorage(), ingestion=SlowIngestion(delay=0.05),
    )

    async def scenario():
        await worker.start()
        deadline = time.monotonic() + 5.0
        while not worker._active and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert worker._active, "job task never started"
        await worker.stop()

    run(scenario())

    assert worker._ingestion.processed == [job["id"]]
    assert worker._ingestion.cancelled == []
    assert queue.fail_calls == []  # shutdown never writes a failure
    assert worker.active_jobs == 0


def test_shutdown_abandons_jobs_past_grace_without_state_writes(tmp_path):
    """A job still running when the grace window expires is abandoned:
    cancelled, counted as a cancellation, and NO fail/complete write happens
    (the lease expires; another worker recovers the job)."""
    queue = FakeQueue()
    job = validating_job()
    queue.jobs_to_claim.append(job)
    settings = make_settings(tmp_path, worker_shutdown_grace_seconds=30)
    # The grace is read at stop() time; shrink it so the test stays fast
    # (Settings has no validate_assignment, so a direct set is safe).
    settings.worker_shutdown_grace_seconds = 0.2
    worker = IngestionWorker(settings, queue, FakeStorage(),
                             ingestion=SlowIngestion())  # never finishes

    async def scenario():
        await worker.start()
        deadline = time.monotonic() + 5.0
        while not worker._active and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert worker._active
        await worker.stop()

    run(scenario())

    assert worker._ingestion.cancelled == [job["id"]]
    assert worker._ingestion.processed == []
    assert queue.fail_calls == [], "abandoned job must not be marked failed"
    assert queue.complete_calls == [], "abandoned job must not be marked complete"
    assert worker.active_jobs == 0


def test_shutdown_closes_queue_storage_and_ingestion_clients(tmp_path):
    queue, storage, ingestion = FakeQueue(), FakeStorage(), SlowIngestion()
    worker = IngestionWorker(make_settings(tmp_path), queue, storage, ingestion=ingestion)

    async def scenario():
        await worker.start()
        await worker.stop()

    run(scenario())
    assert queue.closed is True
    assert storage.closed is True
    assert ingestion.aclose_called is True


def test_shutdown_is_idempotent_and_safe_before_start(tmp_path):
    worker = IngestionWorker(make_settings(tmp_path), FakeQueue(), FakeStorage(),
                             ingestion=SlowIngestion())

    async def scenario():
        await worker.stop()  # never started: must not raise
        await worker.start()
        await worker.stop()
        await worker.stop()  # double stop: must not raise

    run(scenario())


# -- stale temp-directory cleanup ------------------------------------------------------


def test_stale_tmp_cleanup_removes_old_dirs_only(tmp_path):
    settings = make_settings(tmp_path, worker_tmp_stale_seconds=3600)
    tmp_root = settings.tmp_dir
    tmp_root.mkdir(parents=True, exist_ok=True)

    old = tmp_root / "job-old"
    old.mkdir()
    (old / "f.wav").write_bytes(b"x")
    age = time.time() - 7200  # 2 h old > 1 h threshold
    os_utime = time.mktime(time.localtime(age))
    import os as _os
    _os.utime(old, (os_utime, os_utime))
    _os.utime(old / "f.wav", (os_utime, os_utime))

    fresh = tmp_root / "job-fresh"
    fresh.mkdir()
    loose = tmp_root / "stray-file.txt"
    loose.write_bytes(b"not a dir, must survive")

    worker = IngestionWorker(settings, FakeQueue(), FakeStorage(), ingestion=SlowIngestion())

    async def scenario():
        await worker.start()
        await worker.stop()

    run(scenario())

    assert not old.exists(), "stale dir (older than threshold) must be removed"
    assert fresh.exists(), "fresh dir must survive"
    assert loose.exists(), "non-directory entries must survive"


def test_stale_tmp_cleanup_never_touches_active_job_dirs(tmp_path):
    """A directory whose name matches a LIVE job task must never be deleted,
    even if its mtime is ancient."""
    settings = make_settings(tmp_path, worker_tmp_stale_seconds=3600)
    tmp_root = settings.tmp_dir
    tmp_root.mkdir(parents=True, exist_ok=True)

    import os as _os

    active_job = validating_job()
    active_dir = tmp_root / active_job["id"]
    queue = FakeQueue()
    queue.jobs_to_claim.append(active_job)
    # Tiny shutdown grace: the job never finishes, so stop() must abandon it
    # quickly instead of waiting out the default 600s grace.
    settings.worker_shutdown_grace_seconds = 0.2
    worker = IngestionWorker(settings, queue, FakeStorage(), ingestion=SlowIngestion())

    async def scenario():
        await worker.start()  # start-time cleanup runs BEFORE the claim
        deadline = time.monotonic() + 5.0
        while not worker._active and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert worker._active, "job task never started"
        # The job is now LIVE; its scratch dir exists and is ancient. A
        # cleanup run at this moment must spare it via the active-job
        # exclusion (its name matches the running task 'job-{id}').
        active_dir.mkdir(parents=True, exist_ok=True)
        ancient = time.mktime(time.localtime(time.time() - 86400 * 3))
        _os.utime(active_dir, (ancient, ancient))
        worker._cleanup_stale_tmp()
        assert active_dir.exists(), "active job dir must never be deleted"
        await worker.stop()

    run(scenario())


# -- dependency probing (/ready backing) ------------------------------------------------


def test_probe_reports_ok_when_dependencies_are_up(tmp_path):
    worker = IngestionWorker(make_settings(tmp_path), FakeQueue(), FakeStorage())

    async def scenario():
        return await worker.probe_dependencies()

    payload = run(scenario())
    assert payload["database"] == "ok"
    assert payload["storage"] == "ok"
    assert payload["queue"] == "ok"  # the queue IS the database RPCs
    assert "optional" in payload and "providers" in payload
    # Provider config is REPORTED (mode names only, never credentials).
    assert payload["providers"]["youtube"] in ("keyless-oembed", "data-api-key", "oauth")
    assert "materialization" in payload["providers"]


def test_probe_reports_errors_without_raising(tmp_path):
    queue, storage = FakeQueue(), FakeStorage()
    queue.ping_failures = 1
    storage.ping_failures = 1
    worker = IngestionWorker(make_settings(tmp_path), queue, storage)

    async def scenario():
        return await worker.probe_dependencies()

    payload = run(scenario())
    assert payload["database"] == "error"
    assert payload["storage"] == "error"
    assert payload["queue"] == "error"


def test_probe_results_are_cached_within_ttl(tmp_path):
    """Within the TTL the real probes are NOT re-run (bounded cost): the
    cached 'ok' survives even if the dependencies die afterwards."""
    queue, storage = FakeQueue(), FakeStorage()
    worker = IngestionWorker(make_settings(tmp_path), queue, storage)

    async def scenario():
        first = await worker.probe_dependencies()
        queue.ping_failures = 1
        storage.ping_failures = 1
        second = await worker.probe_dependencies()
        # Force a re-probe past the TTL.
        worker._probe_cache = (time.monotonic() - 60, first)
        third = await worker.probe_dependencies(cache_ttl=30)
        return first, second, third

    first, second, third = run(scenario())
    assert first["database"] == "ok"
    assert second["database"] == "ok"  # served from cache
    assert third["database"] == "error"  # re-probed after TTL expiry


def test_optional_dependency_missing_does_not_gate_core_state(tmp_path):
    """A missing optional dependency must surface as 'degraded'/'disabled'
    for ITS feature only — the core database/storage/queue state is
    independent of it."""
    settings = make_settings(tmp_path)
    # ffmpeg intentionally absent (binary name that does not exist).
    settings.ffmpeg_path = "definitely-not-a-real-ffmpeg-binary"
    settings.ffprobe_path = "definitely-not-a-real-ffprobe-binary"
    settings.facecam_enabled = False
    settings.game_vision_enabled = False
    worker = IngestionWorker(settings, FakeQueue(), FakeStorage())

    payload = run(worker.probe_dependencies())
    assert payload["database"] == "ok"
    assert payload["optional"]["ffmpeg"] == "degraded"
    assert payload["optional"]["facecam"] == "disabled"
    assert payload["optional"]["game_vision"] == "disabled"
