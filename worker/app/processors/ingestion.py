"""Job ingestion processor (Phase 3): download → validate → ready.

The worker NEVER marks a job ``completed`` in this phase — ingestion ends at
``ready``; the product pipeline (transcribing → analyzing → rendering →
completed) continues in Phases 4-5.

Lease safety: while a job is being processed, a renewal task extends the
lease. If renewal is ever rejected (we lost ownership), the callback cancels
the processing task; no state write is then attempted with a stale lease
(the RPCs would answer ``not_owner`` anyway).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

from ..config.settings import Settings
from ..queue.models import (
    ERROR_STORAGE_DOWNLOAD,
    ERROR_UNKNOWN,
    JobStatus,
    PERMANENT_ERROR_CATEGORIES,
)
from ..queue.supabase_queue import SupabaseQueue
from ..storage.supabase_storage import StorageDownloadError, SupabaseStorage
from ..validation.media import MediaValidationError, probe_media, validate_media

logger = logging.getLogger("clipforge.ingestion")


class LeaseLostError(RuntimeError):
    """Raised when we detect we no longer own the job lease."""


class IngestionProcessor:
    def __init__(self, settings: Settings, queue: SupabaseQueue, storage: SupabaseStorage) -> None:
        self._settings = settings
        self._queue = queue
        self._storage = storage

    async def process_job(self, job: dict[str, Any], on_lease_lost: Optional[Callable[[], None]] = None) -> None:
        """Runs the full ingestion lifecycle for one claimed job.

        Guarantees: at most one terminal queue write (complete or fail),
        lease-aware abort, and temp-file cleanup on every path.
        """
        job_id = str(job["id"])
        worker_id = str(job.get("_worker_id") or "")
        source_path = job.get("source_path")
        tmp_dir: Path = self._settings.tmp_dir / job_id
        lease_task: Optional[asyncio.Task] = None
        try:
            if job.get("source_type") != "upload":
                await self._safe_fail(job_id, worker_id, ERROR_UNKNOWN,
                                      "unsupported source type", permanent=True)
                return
            if not source_path:
                await self._safe_fail(job_id, worker_id, ERROR_UNKNOWN,
                                      "job has no source object", permanent=True)
                return

            tmp_dir.mkdir(parents=True, exist_ok=True)
            destination = tmp_dir / "source.bin"

            lease_task = asyncio.create_task(self._renew_lease(job_id, worker_id, on_lease_lost))

            await self._report(job_id, worker_id, 10)

            # 1) Download the source from private storage to ephemeral disk.
            try:
                await self._storage.download_to(source_path, destination)
            except StorageDownloadError as exc:
                await self._safe_fail(job_id, worker_id, ERROR_STORAGE_DOWNLOAD,
                                      str(exc), permanent=False)
                return

            await self._report(job_id, worker_id, 40)

            # 2) Probe and validate the actual bytes (never trust the browser).
            info = await probe_media(
                self._settings.ffprobe_path,
                destination,
                self._settings.ffprobe_timeout_seconds,
            )
            # No source-duration cap (TASK 007): the size cap bounds disk,
            # long recordings are analyzed incrementally downstream.
            metadata = validate_media(info, self._settings.max_upload_size_bytes)
            await self._report(job_id, worker_id, 80)

            # 3) Mark ingestion complete (validating → ready, lock released).
            completed = await self._queue.complete_job_ingestion(job_id, worker_id, dict(metadata))
            if not completed:
                logger.warning("job %s: completion rejected — lease no longer owned", job_id)
                return

            logger.info(
                "job %s ingestion ready (duration=%.1fs %dx%d)",
                job_id, metadata["duration_seconds"], metadata["width"], metadata["height"],
            )
        except LeaseLostError:
            logger.warning("job %s: lease lost during processing; abandoning", job_id)
        except asyncio.CancelledError:
            # Lease loss / graceful shutdown cancelled us — do NOT write a
            # failure with a lease we may no longer own.
            logger.info("job %s: processing cancelled (lease loss or shutdown)", job_id)
            raise
        except MediaValidationError as exc:
            permanent = exc.category in PERMANENT_ERROR_CATEGORIES
            await self._safe_fail(job_id, worker_id, exc.category, exc.message, permanent=permanent)
        except Exception as exc:  # noqa: BLE001 — top-level job boundary
            logger.exception("job %s: unexpected ingestion error: %s", job_id, exc.__class__.__name__)
            await self._safe_fail(job_id, worker_id, ERROR_UNKNOWN,
                                  f"unexpected error: {exc.__class__.__name__}", permanent=False)
        finally:
            if lease_task is not None:
                lease_task.cancel()
            # Temp storage is scratch only — always cleaned up.
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # -- internals -------------------------------------------------------------

    async def _renew_lease(self, job_id: str, worker_id: str, on_lease_lost: Optional[Callable[[], None]]) -> None:
        interval = max(5.0, self._settings.job_lease_seconds / 3.0)
        try:
            while True:
                await asyncio.sleep(interval)
                renewed = await self._queue.renew_job_lease(job_id, worker_id, self._settings.job_lease_seconds)
                if not renewed:
                    logger.warning("job %s: lease renewal rejected — ownership lost", job_id)
                    if on_lease_lost is not None:
                        on_lease_lost()
                    return
        except asyncio.CancelledError:
            return

    async def _report(self, job_id: str, worker_id: str, value: int) -> None:
        updated = await self._queue.update_job_progress(job_id, worker_id, value)
        if not updated:
            raise LeaseLostError("progress update rejected (lease lost)")

    async def _safe_fail(
        self,
        job_id: str,
        worker_id: str,
        category: str,
        message: str,
        permanent: bool,
    ) -> None:
        try:
            new_status = await self._queue.fail_job(job_id, worker_id, category, message, permanent)
            if new_status == "not_owner":
                logger.warning("job %s: fail reported but lease no longer owned", job_id)
            else:
                logger.info("job %s: failed (%s) -> %s", job_id, category, new_status)
        except Exception as exc:  # noqa: BLE001
            logger.error("job %s: could not record failure: %s", job_id, exc.__class__.__name__)


def is_terminal_status(status: JobStatus) -> bool:
    """States where the worker does no further work in Phase 3.

    ``ready`` is terminal FOR INGESTION (Phases 4-5 continue the pipeline).
    """
    return status in (JobStatus.READY, JobStatus.COMPLETED, JobStatus.FAILED)
