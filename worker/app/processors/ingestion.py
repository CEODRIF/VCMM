"""Job ingestion processor (Phase 3): download → validate → ready.

The worker NEVER marks a job ``completed`` in this phase — ingestion ends at
``ready``; the product pipeline (transcribing → analyzing → rendering →
completed) continues in Phases 4-5.

Lease safety: while a job is being processed, a renewal task extends the
lease. If renewal is ever rejected (we lost ownership), the callback cancels
the processing task; no state write is then attempted with a stale lease
(the RPCs would answer ``not_owner`` anyway).

TASK 011 (Phase 8) adds the provider branch: ``youtube``/``twitch`` jobs go
through the source-provider layer (worker/app/providers). The pipeline
below the provider is IDENTICAL for every source type — the download,
probe, and completion code is shared, so the analysis pipeline never learns
where a source came from. Provider sources are authorized, validated, and
materialized through the provider layer ONLY; when no authorized
materialization path exists (YouTube; Twitch VOD/live URLs), the job fails
permanently with a clear, honest error instead of guessing.

TASK 014 adds two materializing source types through the same branch:
``twitch`` CLIP URLs (official Get Clips Download under creator
authorization) and ``direct_url`` (SSRF-guarded streaming import of public
video-file URLs) — no pipeline changes below the provider.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

from ..config.settings import Settings
from ..providers.base import MaterializationUnavailable, STATUS_AVAILABLE, StreamProvider
from ..providers.errors import (
    CODE_CANCELLED,
    CODE_INVALID_URL,
    CODE_MATERIALIZATION_UNSUPPORTED,
    CODE_METADATA_UNAVAILABLE,
    CODE_PROVIDER_UNAVAILABLE,
    CODE_RATE_LIMITED,
    CODE_SIZE_DURATION_EXCEEDED,
    CODE_SOURCE_EXPIRED,
    CODE_SOURCE_RESTRICTED,
    CODE_STORAGE_FAILED,
    CODE_TRANSFER_INTERRUPTED,
    CODE_UNAUTHORIZED,
    CODE_UNSUPPORTED_PROVIDER,
    ProviderError,
    SourceCancelledError,
    error_for_status,
)
from ..providers.materialization import MaterializationService
from ..providers.registry import default_providers, provider_by_type, provider_is_configured
from ..queue.models import (
    ERROR_MATERIALIZATION_UNSUPPORTED,
    ERROR_PROVIDER_INVALID_URL,
    ERROR_PROVIDER_METADATA,
    ERROR_PROVIDER_RATE_LIMITED,
    ERROR_PROVIDER_UNAVAILABLE,
    ERROR_PROVIDER_UNAUTHORIZED,
    ERROR_SOURCE_REJECTED,
    ERROR_SOURCE_TOO_LARGE,
    ERROR_SOURCE_UPLOAD,
    ERROR_STORAGE_DOWNLOAD,
    ERROR_TRANSFER_INTERRUPTED,
    ERROR_UNKNOWN,
    JobStatus,
    PERMANENT_ERROR_CATEGORIES,
)
from ..queue.supabase_queue import SupabaseQueue
from ..storage.supabase_storage import StorageDownloadError, SupabaseStorage
from ..validation.media import MediaValidationError, probe_media, validate_media

logger = logging.getLogger("clipforge.ingestion")


def _content_type_for(local_path: Path) -> str:
    """Storage content type from the MATERIALIZED file's suffix.

    The provider chose the suffix from the verified transfer (never from
    a user-claimed MIME type), and the bytes were FFprobe-checked by the
    provider before storage; the default (mp4) keeps every existing
    source type byte-identical.
    """
    return {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
    }.get(Path(local_path).suffix.lower(), "video/mp4")


class LeaseLostError(RuntimeError):
    """Raised when we detect we no longer own the job lease."""


#: Provider error code → queue category (spec section 13). ``None`` =
#: do not write a failure (cancellation/lease loss: the job continues on
#: its next claim; writing a failure with a lost lease would be wrong).
_CODE_TO_CATEGORY = {
    CODE_INVALID_URL: ERROR_PROVIDER_INVALID_URL,
    CODE_UNSUPPORTED_PROVIDER: ERROR_PROVIDER_INVALID_URL,
    CODE_UNAUTHORIZED: ERROR_PROVIDER_UNAUTHORIZED,
    CODE_METADATA_UNAVAILABLE: ERROR_PROVIDER_METADATA,
    CODE_MATERIALIZATION_UNSUPPORTED: ERROR_MATERIALIZATION_UNSUPPORTED,
    CODE_RATE_LIMITED: ERROR_PROVIDER_RATE_LIMITED,
    CODE_PROVIDER_UNAVAILABLE: ERROR_PROVIDER_UNAVAILABLE,
    CODE_SOURCE_RESTRICTED: ERROR_SOURCE_REJECTED,
    CODE_SOURCE_EXPIRED: ERROR_SOURCE_REJECTED,
    CODE_SIZE_DURATION_EXCEEDED: ERROR_SOURCE_TOO_LARGE,
    CODE_TRANSFER_INTERRUPTED: ERROR_TRANSFER_INTERRUPTED,
    CODE_STORAGE_FAILED: ERROR_SOURCE_UPLOAD,
    CODE_CANCELLED: None,
}


class IngestionProcessor:
    def __init__(
        self,
        settings: Settings,
        queue: SupabaseQueue,
        storage: SupabaseStorage,
        *,
        providers: tuple[StreamProvider, ...] | None = None,
        materialization_service: MaterializationService | None = None,
    ) -> None:
        self._settings = settings
        self._queue = queue
        self._storage = storage
        # TASK 011: the provider layer. Injectable for tests (fakes); by
        # default the adapters from the registry, configured from Settings.
        self._providers = (
            tuple(providers)
            if providers is not None
            else default_providers(settings.ffprobe_path, settings)
        )
        self._materialization = materialization_service or MaterializationService(
            storage,
            queue,
            max_source_bytes=settings.materialize_max_source_bytes,
            chunk_bytes=settings.materialize_chunk_bytes,
            max_attempts=settings.provider_max_retries + 1,
            backoff_seconds=settings.provider_retry_backoff_seconds,
            max_redirects=settings.materialize_max_redirects,
        )

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
                if not self._settings.source_providers_enabled:
                    # Master switch OFF: fail permanently and honestly
                    # (direct uploads are unaffected by this branch).
                    await self._safe_fail(
                        job_id, worker_id, ERROR_PROVIDER_UNAVAILABLE,
                        "source providers are disabled on this deployment — "
                        "use direct video upload", permanent=True,
                    )
                    return
                provider = self._find_provider(str(job.get("source_type") or ""))
                if provider is None:
                    await self._safe_fail(job_id, worker_id, ERROR_UNKNOWN,
                                          "unsupported source type", permanent=True)
                    return
                await self._process_provider_job(
                    job, job_id, worker_id, tmp_dir, provider, on_lease_lost
                )
                return
            if not source_path:
                await self._safe_fail(job_id, worker_id, ERROR_UNKNOWN,
                                      "job has no source object", permanent=True)
                return

            tmp_dir.mkdir(parents=True, exist_ok=True)
            destination = tmp_dir / "source.bin"

            lease_task = asyncio.create_task(self._renew_lease(job_id, worker_id, on_lease_lost))

            await self._report(job_id, worker_id, 10)

            await self._finalize(job_id, worker_id, source_path, destination, 40, 80)
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

    # -- provider flow (TASK 011) --------------------------------------------------

    def _find_provider(self, source_type: str) -> StreamProvider | None:
        return provider_by_type(source_type, self._providers)

    async def _process_provider_job(
        self,
        job: dict[str, Any],
        job_id: str,
        worker_id: str,
        tmp_dir: Path,
        provider: StreamProvider,
        on_lease_lost: Optional[Callable[[], None]],
    ) -> None:
        """Provider-sourced job: authorize → validate → materialize (only if
        the adapter has an AUTHORIZED path) → store → shared finalize tail.

        Every refusal here is typed and honest (spec section 7): metadata
        permitted + no authorized download ⇒ permanent
        ``materialization_unsupported`` with a "upload the file directly"
        message — never a workaround, never a silent dead job.
        """
        source_url = job.get("source_url")
        existing_source_path = job.get("source_path")

        # Cooperative cancellation: the renewal task flips this on lease
        # loss; the transfer probes it between chunks.
        state = {"lease_lost": False}

        def _on_lease_lost() -> None:
            state["lease_lost"] = True
            if on_lease_lost is not None:
                on_lease_lost()

        lease_task = asyncio.create_task(self._renew_lease(job_id, worker_id, _on_lease_lost))
        try:
            if not provider_is_configured(provider):
                if provider.name == "direct_url":
                    # The adapter is code-complete but operator-disabled:
                    # name the exact switch so the operator can act.
                    not_configured_message = (
                        "direct video URL import is disabled on this deployment "
                        "(WORKER_DIRECT_URLS_ENABLED) — upload the file directly "
                        "instead"
                    )
                else:
                    not_configured_message = (
                        f"the {provider.name} source provider is not configured on this "
                        "deployment — direct video file upload is available"
                    )
                await self._safe_fail(
                    job_id, worker_id, ERROR_PROVIDER_UNAVAILABLE,
                    not_configured_message,
                    permanent=True,
                )
                return
            if not source_url:
                await self._safe_fail(job_id, worker_id, ERROR_PROVIDER_INVALID_URL,
                                      "job has no source URL", permanent=True)
                return

            await self._report(job_id, worker_id, 10)

            # 1) Official availability + metadata (never downloads media).
            try:
                check = await provider.validate(source_url)
            except ProviderError as exc:
                await self._fail_provider_error(job_id, worker_id, exc)
                return

            # 2) Non-available status → typed, categorized outcome.
            if check.status != STATUS_AVAILABLE:
                error = error_for_status(check.status, provider.name, check.reason)
                await self._fail_provider_error(job_id, worker_id, error)
                return

            # 3) The materialization gate (spec section 7).
            if not provider.supports_materialization:
                await self._safe_fail(
                    job_id, worker_id, ERROR_MATERIALIZATION_UNSUPPORTED,
                    f"{provider.name.capitalize()} metadata is available, but this "
                    "deployment has no authorized mechanism to download "
                    f"{provider.name} media (the official APIs expose no "
                    "media-download endpoint). Please upload the video file directly.",
                    permanent=True,
                )
                return

            # 4) Authorized materialization. Only adapters with an explicit,
            #    policy-compliant download path reach this line: youtube
            #    ships none (the gate above stops it); twitch materializes
            #    CLIP URLs through the official Get Clips Download endpoint
            #    under creator authorization; direct_url streams public
            #    video-file URLs through the SSRF guard (TASK 014).
            tmp_dir.mkdir(parents=True, exist_ok=True)
            destination = tmp_dir / "source.bin"
            source_path = existing_source_path
            if not source_path:
                local_path = await provider.materialize(
                    source_url,
                    tmp_dir,
                    progress=self._transfer_progress(job_id, worker_id, state),
                    should_cancel=lambda: state["lease_lost"],
                )
                source_path = await self._materialization.store_source(
                    job_id, worker_id, local_path,
                    content_type=_content_type_for(local_path),
                )
                await self._report(job_id, worker_id, 70)
            else:
                # Idempotent re-claim (crash between pin and complete):
                # skip the transfer; the shared tail re-verifies the object.
                logger.info("job %s: source already materialized — re-verifying", job_id)

            # 5) THE shared tail (identical to direct uploads): download the
            #    stored object, probe the actual bytes, complete ingestion.
            await self._finalize(job_id, worker_id, source_path, destination, 75, 85)
        except SourceCancelledError:
            # Lease lost mid-transfer — abandon WITHOUT a failure write;
            # the job is claimable again for its rightful owner.
            logger.warning("job %s: lease lost during materialization; abandoning", job_id)
        except MaterializationUnavailable as exc:
            # Defense in depth: an adapter whose capability flag lied.
            await self._safe_fail(job_id, worker_id, ERROR_MATERIALIZATION_UNSUPPORTED,
                                  str(exc), permanent=True)
        except ProviderError as exc:
            await self._fail_provider_error(job_id, worker_id, exc)
        finally:
            lease_task.cancel()

    async def _fail_provider_error(self, job_id: str, worker_id: str, exc: ProviderError) -> None:
        if exc.code == CODE_CANCELLED:
            logger.warning("job %s: cancelled; abandoning", job_id)
            return
        category = _CODE_TO_CATEGORY.get(exc.code, ERROR_PROVIDER_METADATA)
        permanent = category in PERMANENT_ERROR_CATEGORIES
        await self._safe_fail(job_id, worker_id, category, exc.safe_message, permanent=permanent)

    def _transfer_progress(self, job_id: str, worker_id: str, state: dict[str, bool]):
        """Sync progress callback for the streaming transfer (chunk loop).

        Maps bytes → job progress 15..65 (10 = started, 70 = stored).
        Best-effort and lease-guarded: updates are throttled and go
        through the worker-owned RPC, so a lost lease can never write.
        """
        last = {"value": 14}

        def _progress(written: int, total: int | None) -> None:
            if state["lease_lost"] or not total or total <= 0:
                return
            value = 15 + int(50 * min(1.0, written / total))
            if value <= last["value"]:
                return
            last["value"] = value
            asyncio.get_running_loop().create_task(self._safe_update(job_id, worker_id, value))

        return _progress

    async def _safe_update(self, job_id: str, worker_id: str, value: int) -> None:
        try:
            await self._queue.update_job_progress(job_id, worker_id, value)
        except Exception:  # noqa: BLE001 — progress is best-effort
            pass

    # -- shared finalize tail (uploads + provider sources) -------------------------

    async def _finalize(
        self,
        job_id: str,
        worker_id: str,
        source_path: str,
        destination: Path,
        download_progress: int,
        validated_progress: int,
    ) -> None:
        """Download the source object → probe the bytes → complete ingestion.

        This is the ONE source-handling tail: direct uploads and provider
        sources both end here, so the pipeline sees normalized files only.
        """
        # 1) Download the source from private storage to ephemeral disk.
        try:
            await self._storage.download_to(source_path, destination)
        except StorageDownloadError as exc:
            await self._safe_fail(job_id, worker_id, ERROR_STORAGE_DOWNLOAD,
                                  str(exc), permanent=False)
            return

        await self._report(job_id, worker_id, download_progress)

        # 2) Probe and validate the actual bytes (never trust the browser).
        info = await probe_media(
            self._settings.ffprobe_path,
            destination,
            self._settings.ffprobe_timeout_seconds,
        )
        # No source-duration cap (TASK 007): the size cap bounds disk,
        # long recordings are analyzed incrementally downstream.
        metadata = validate_media(info, self._settings.max_upload_size_bytes)
        await self._report(job_id, worker_id, validated_progress)

        # 3) Mark ingestion complete (validating → ready, lock released).
        completed = await self._queue.complete_job_ingestion(job_id, worker_id, dict(metadata))
        if not completed:
            logger.warning("job %s: completion rejected — lease no longer owned", job_id)
            return

        logger.info(
            "job %s ingestion ready (duration=%.1fs %dx%d)",
            job_id, metadata["duration_seconds"], metadata["width"], metadata["height"],
        )

    # -- internals -------------------------------------------------------------

    async def aclose(self) -> None:
        """TASK 011: release provider resources on worker shutdown (the
        token-exchange clients, when OAuth / app-token flows are
        configured). Never raises — shutdown must complete."""
        for provider in self._providers:
            close = getattr(provider, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    pass

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
