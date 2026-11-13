"""Ingestion worker loop (Phase 3).

- Polls the queue and claims jobs atomically via the restricted RPC.
- Bounded concurrency (asyncio semaphore).
- Lease renewal happens inside each job's processing task.
- Exponential backoff on consecutive claim failures (transport errors).
- Graceful shutdown: stop claiming, release or abandon active jobs safely
  (leases expire and are recovered by other workers), clean up temp files.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import time
import uuid

from .config.settings import Settings
from .processors.ingestion import IngestionProcessor
from .queue.supabase_queue import QueueError, SupabaseQueue
from .storage.supabase_storage import SupabaseStorage

logger = logging.getLogger("clipforge.worker")


class IngestionWorker:
    def __init__(self, settings: Settings, queue: SupabaseQueue, storage: SupabaseStorage) -> None:
        self._settings = settings
        self._queue = queue
        self._processor = IngestionProcessor(settings, queue, storage)
        self._worker_id = f"{platform.node() or 'worker'}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._semaphore = asyncio.Semaphore(settings.concurrency)
        self._active: set[asyncio.Task] = set()
        self._stop = asyncio.Event()
        self._started_at: float = 0.0
        self._last_claim_error_at: float | None = None
        self._consecutive_claim_errors: int = 0

    # -- lifecycle -------------------------------------------------------------

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def active_jobs(self) -> int:
        return len(self._active)

    @property
    def heartbeat(self) -> dict:
        return {
            "worker_id": self._worker_id,
            "active_jobs": self.active_jobs,
            "uptime_seconds": round(time.monotonic() - self._started_at, 1) if self._started_at else 0.0,
        }

    async def start(self) -> None:
        self._started_at = time.monotonic()
        self._stop.clear()
        self._loop_task = asyncio.create_task(self._loop(), name="ingestion-loop")
        logger.info("ingestion worker %s started (concurrency=%d, lease=%ds, poll=%.1fs)",
                    self._worker_id, self._settings.concurrency,
                    self._settings.job_lease_seconds, self._settings.poll_interval)

    async def stop(self) -> None:
        """Graceful shutdown: stop claiming; let in-flight jobs finish safely."""
        self._stop.set()
        loop_task = getattr(self, "_loop_task", None)
        if loop_task is not None:
            loop_task.cancel()
            try:
                await loop_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._active:
            logger.info("waiting for %d active job(s) to finish or release their leases", len(self._active))
            await asyncio.gather(*list(self._active), return_exceptions=True)
        logger.info("ingestion worker %s stopped", self._worker_id)

    # -- internals -------------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stop.is_set():
            delay = self._settings.poll_interval
            try:
                job = await self._queue.claim_next_job(self._worker_id, self._settings.job_lease_seconds)
                self._consecutive_claim_errors = 0
                if job:
                    job["_worker_id"] = self._worker_id
                    task = asyncio.create_task(self._guarded(job), name=f"job-{job['id']}")
                    self._active.add(task)
                    task.add_done_callback(self._active.discard)
            except QueueError as exc:
                self._consecutive_claim_errors += 1
                self._last_claim_error_at = time.monotonic()
                # Exponential backoff, capped at 60s: 2s, 4s, 8s, ...
                delay = min(60.0, self._settings.poll_interval * (2 ** min(self._consecutive_claim_errors, 5)))
                logger.warning("claim failed (%s); backing off %.1fs", exc.__class__.__name__, delay)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — loop boundary
                logger.exception("ingestion loop error: %s", exc.__class__.__name__)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _guarded(self, job: dict) -> None:
        async with self._semaphore:
            processing_task = asyncio.current_task()
            try:
                await self._processor.process_job(job, on_lease_lost=processing_task.cancel)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — never kill the loop
                logger.exception("job %s crashed: %s", job.get("id"), exc.__class__.__name__)
