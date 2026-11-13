"""Provider-independent materialization orchestration (TASK 011, §8).

The single place that turns an AUTHORIZED media transfer into a verified
source object in the private ``clipforge-sources`` bucket, and records it
on the job. It is provider-agnostic by design: providers own the
authorization decision (they either raise
:class:`~app.providers.base.MaterializationUnavailable` or hand this
service a URL they are permitted to fetch, through their own
SSRF-guarded client). Nothing in this module knows about YouTube/Twitch.

Guarantees (all tested):

- streaming in bounded chunks — a whole video is NEVER loaded into
  memory (chunk size is configurable, capped);
- size cap enforced DURING the transfer (abort + partial cleanup);
- progress reporting (bytes/total) and cooperative cancellation
  (lease loss / shutdown) with temp cleanup on every exit path;
- retry ONLY for retryable failures, bounded, with backoff;
- idempotency: a re-claimed job that already has a ``source_path``
  skips the transfer and re-verifies instead;
- the object path is 100% server-generated (``sources/{job}/original/
  {uuid}.mp4``) — no user input can reach the storage path;
- the job row is updated only through the lease-guarded queue path
  (status='validating' AND locked_by=worker), so a lost lease can never
  write state;
- temporary files are always removed (finally paths).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ..queue.supabase_queue import SupabaseQueue
from ..storage.supabase_storage import (
    StorageUploadError,
    SupabaseStorage,
)
from .errors import (
    SourceCancelledError,
    StorageFailedError,
    TransferInterruptedError,
)
from .http_guard import SafeHttpClient

logger = logging.getLogger("clipforge.providers.materialization")

#: Progress callback signature: (bytes_written, total_bytes_or_None).
ProgressCallback = Callable[[int, Optional[int]], Any]
#: Cancellation probe: returns True when the job must stop now.
CancelProbe = Callable[[], bool]


class MaterializationService:
    """Bounded, idempotent transfer of authorized media into storage."""

    def __init__(
        self,
        storage: SupabaseStorage,
        queue: SupabaseQueue,
        *,
        max_source_bytes: int,
        chunk_bytes: int = 1024 * 1024,
        max_attempts: int = 1,
        backoff_seconds: float = 1.0,
        max_redirects: int = 3,
    ) -> None:
        self._storage = storage
        self._queue = queue
        self._max_bytes = int(max_source_bytes)
        self._chunk_bytes = int(chunk_bytes)
        self._max_attempts = max(1, int(max_attempts))
        self._backoff = float(backoff_seconds)
        self._max_redirects = int(max_redirects)

    # -- transfer ----------------------------------------------------------------

    async def fetch_authorized_media(
        self,
        http: SafeHttpClient,
        media_url: str,
        destination_dir: Path,
        *,
        progress: Optional[ProgressCallback] = None,
        should_cancel: Optional[CancelProbe] = None,
    ) -> Path:
        """Streams an authorized media URL to ``destination_dir/source.bin``.

        Raises typed provider errors (TransferInterruptedError /
        RateLimitedError / SourceCancelledError / InvalidUrlError); the
        partial file is removed on every failure path.
        """
        destination = Path(destination_dir) / "source.bin"
        await http.stream_to_file(
            media_url,
            destination,
            max_bytes=self._max_bytes,
            chunk_size=self._chunk_bytes,
            progress=progress,
            should_cancel=should_cancel,
            max_attempts=self._max_attempts,
            backoff_seconds=self._backoff,
        )
        if not destination.is_file() or destination.stat().st_size == 0:
            destination.unlink(missing_ok=True)
            raise TransferInterruptedError(
                "provider transfer produced an empty file"
            )
        return destination

    # -- store -------------------------------------------------------------------

    async def store_source(
        self,
        job_id: str,
        worker_id: str,
        file_path: Path,
        *,
        content_type: str = "video/mp4",
        existing_source_path: str | None = None,
    ) -> str:
        """Uploads the materialized file and pins it on the job.

        Idempotent: when the job ALREADY has a source_path (crash between
        the bucket upload and the guarded job update, then re-claim), the
        transfer is skipped and the recorded path is returned — the
        downstream download will verify the object exists.

        Returns the object path. Raises StorageFailedError (retryable) on
        storage/queue problems, and SourceCancelledError when the guard
        reports the lease was lost (the caller must abandon, not retry).
        """
        if existing_source_path:
            logger.info("job %s: source already materialized — reusing %s",
                        job_id, existing_source_path)
            return existing_source_path
        try:
            path = await self._storage.upload_source(job_id, Path(file_path),
                                                     content_type=content_type)
        except StorageUploadError as exc:
            raise StorageFailedError(str(exc)) from None
        pinned = await self._queue.set_job_source(job_id, worker_id, path)
        if not pinned:
            # The lease guard did not match: we no longer own the job.
            # The uploaded object is orphaned (bounded: ≤ max_attempts per
            # job, private bucket, per-job directory) and the job will be
            # re-claimed by its rightful owner.
            raise SourceCancelledError(
                "job lease lost during source pinning — abandoning"
            )
        return path

    def build_http(self, allowed_hosts: list[str], **kwargs: Any) -> SafeHttpClient:
        """Convenience for providers: a guard-scoped client with the
        service's transfer budgets (redirect cap included)."""
        return SafeHttpClient(allowed_hosts, max_redirects=self._max_redirects,
                              **kwargs)
