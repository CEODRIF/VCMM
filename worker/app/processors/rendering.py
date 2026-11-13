"""Job rendering processor (Phase 5): download → render → verify → upload.

A claimed job arrives as ``rendering`` — either from a parked ``analyzing``
job (the normal path, after Phase 4 analysis) or as crash recovery of a
rendering that died mid-flight (the stage is kept). The lifecycle:

    PARKED analyzing ──claim──► rendering ──► (every clip rendered + stored)
                                 ──► complete_job_rendering ──► completed
    rendering ──transient error──► fail_job ──► PARKED analyzing (retry)
    rendering ──permanent error / budget exhausted──► failed

Resume semantics: a re-claimed job SKIPS clips already ``ready`` (rendered
AND stored by an earlier attempt) and re-renders the rest; uploads are
upserts, so storage is idempotent.

Terminal completion is enforced BY THE DATABASE: the processor calls
``complete_job_rendering`` and the RPC only succeeds when at least one clip
exists and every clip is ``ready`` WITH a ``storage_path``. The worker
itself can never write ``completed`` directly.

Safety: same lease/ownership contract as analysis — a renewal task keeps
the lease alive, ownership loss aborts the job without state writes,
cancellation (shutdown) performs no writes, and the temp directory is
always removed. Rendering runs with bounded per-job concurrency
(``render_concurrency``); the first clip error stops the job (siblings are
cancelled), and the DB re-queues it to the parked state until the render
budget is exhausted.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

from ..config.settings import Settings
from ..queue.models import (
    ERROR_CLIPS_PERSIST,
    ERROR_CLIP_UPLOAD,
    ERROR_NO_CLIPS_TO_RENDER,
    ERROR_RENDER_FAILED,
    ERROR_RENDER_INVALID,
    ERROR_STORAGE_DOWNLOAD,
    ERROR_UNKNOWN,
    JobStatus,
    PERMANENT_ERROR_CATEGORIES,
)
from ..queue.supabase_queue import SupabaseQueue
from ..rendering.base import BaseRenderer, CaptionStyle, RenderSpec
from ..rendering.captions import parse_caption_payload
from ..rendering.ffmpeg import FFmpegError
from ..rendering.validation import RenderValidationError, verify_render_output
from ..storage.clip_store import ClipsPersistenceError, SupabaseClipStore
from ..storage.supabase_storage import StorageDownloadError, StorageUploadError, SupabaseStorage
from ..validation.media import MediaValidationError, probe_media
from .ingestion import LeaseLostError

logger = logging.getLogger("clipforge.rendering")

#: Progress milestones before the per-clip phase (5 download, 10 probe, 15 clips loaded).
_PROGRESS_RENDER_BASE = 15


def is_rendering_stage(status: str) -> bool:
    """True for the stage the Phase 5 processor handles."""
    return status == JobStatus.RENDERING.value


class RenderProcessor:
    def __init__(
        self,
        settings: Settings,
        queue: SupabaseQueue,
        storage: SupabaseStorage,
        clip_store: SupabaseClipStore,
        renderer: BaseRenderer,
    ) -> None:
        self._settings = settings
        self._queue = queue
        self._storage = storage
        self._clip_store = clip_store
        self._renderer = renderer

    async def process_job(self, job: dict[str, Any], on_lease_lost: Optional[Callable[[], None]] = None) -> None:
        """Runs the full rendering lifecycle for one claimed job.

        Guarantees: at most one terminal queue write (fail or complete),
        lease-aware abort, and temp-file cleanup on every path.
        """
        job_id = str(job["id"])
        worker_id = str(job.get("_worker_id") or "")
        source_path = job.get("source_path")
        tmp_dir: Path = self._settings.tmp_dir / job_id
        lease_task: Optional[asyncio.Task] = None
        try:
            status = str(job.get("status") or "")
            if status != JobStatus.RENDERING.value:
                # Internal invariant: the claim only yields rendering-stage
                # jobs. Do NOT write state — the lease will recover it.
                logger.error("job %s: claimed with unexpected status %r; abandoning", job_id, status)
                return
            if not source_path:
                await self._safe_fail(job_id, worker_id, ERROR_UNKNOWN,
                                      "job has no source object", permanent=True)
                return

            tmp_dir.mkdir(parents=True, exist_ok=True)
            destination = tmp_dir / "source.bin"

            lease_task = asyncio.create_task(self._renew_lease(job_id, worker_id, on_lease_lost))

            # 1) Re-download the verified source from private storage.
            await self._report(job_id, worker_id, 5)
            try:
                await self._storage.download_to(source_path, destination)
            except StorageDownloadError as exc:
                await self._safe_fail(job_id, worker_id, ERROR_STORAGE_DOWNLOAD,
                                      str(exc), permanent=False)
                return

            # 2) Re-probe (never trust the previous probe).
            try:
                await probe_media(
                    self._settings.ffprobe_path,
                    destination,
                    self._settings.ffprobe_timeout_seconds,
                )
            except MediaValidationError as exc:
                permanent = exc.category in PERMANENT_ERROR_CATEGORIES
                await self._safe_fail(job_id, worker_id, exc.category, exc.message, permanent=permanent)
                return
            await self._report(job_id, worker_id, 10)

            # 3) Load the clip set; resume skips clips already ready.
            try:
                clips = await self._clip_store.list_clips(job_id)
            except ClipsPersistenceError as exc:
                await self._safe_fail(job_id, worker_id, ERROR_CLIPS_PERSIST,
                                      str(exc), permanent=False)
                return
            if not clips:
                await self._safe_fail(job_id, worker_id, ERROR_NO_CLIPS_TO_RENDER,
                                      "the job has no clips to render", permanent=True)
                return
            todo = [c for c in clips if c.get("status") != "ready"]
            await self._report(job_id, worker_id, _PROGRESS_RENDER_BASE)

            # 4) Render the remaining clips (bounded concurrency, first error wins).
            #    The job-level caption toggle (Phase 2 column) + the configured
            #    style form the base spec; per-clip real words come from the
            #    persisted `clips.captions` payload (Phase 5B).
            captions_enabled = bool(job.get("captions_enabled", True))
            base_spec = RenderSpec(
                captions=captions_enabled,
                caption_style=CaptionStyle(
                    font_family=self._settings.caption_font_family,
                    font_size=self._settings.caption_font_size,
                    base_color=self._settings.caption_base_color,
                    highlight_color=self._settings.caption_highlight_color,
                    outline_color=self._settings.caption_outline_color,
                    outline_width=self._settings.caption_outline_width,
                    margin_v=self._settings.caption_margin_v,
                    words_per_line=self._settings.caption_words_per_line,
                    uppercase=self._settings.caption_uppercase,
                ),
            )
            clip_error = await self._render_clips(job_id, worker_id, destination, todo, base_spec)
            if clip_error is not None:
                category, permanent = self._categorize(clip_error)
                await self._safe_fail(job_id, worker_id, category, str(clip_error), permanent=permanent)
                return

            # 5) Terminal completion — the DB refuses unless EVERY clip is
            #    ready with a storage path (a stale worker cannot fake it).
            completed = await self._queue.complete_job_rendering(job_id, worker_id)
            if not completed:
                logger.warning("job %s: completion refused (lease lost or clip state mismatch); abandoning", job_id)
                return
            logger.info("job %s rendering complete: %d clip(s) rendered and stored", job_id, len(clips))
        except LeaseLostError:
            logger.warning("job %s: lease lost during rendering; abandoning", job_id)
        except asyncio.CancelledError:
            # Shutdown / lease-loss cancel — do NOT write with a stale lease.
            logger.info("job %s: rendering cancelled (lease loss or shutdown)", job_id)
            raise
        except Exception as exc:  # noqa: BLE001 — top-level job boundary
            logger.exception("job %s: unexpected rendering error: %s", job_id, exc.__class__.__name__)
            await self._safe_fail(job_id, worker_id, ERROR_UNKNOWN,
                                  f"unexpected error: {exc.__class__.__name__}", permanent=False)
        finally:
            if lease_task is not None:
                lease_task.cancel()
            # Temp storage is scratch only — always cleaned up.
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # -- internals -------------------------------------------------------------

    async def _render_clips(
        self,
        job_id: str,
        worker_id: str,
        destination: Path,
        todo: list[dict[str, Any]],
        base_spec: RenderSpec,
    ) -> Exception | None:
        """Renders every remaining clip; returns the first clip error or None.

        Clip tasks run under a TaskGroup with a semaphore bounding
        concurrent FFmpeg subprocesses: the first failure cancels the
        siblings and is returned (the job is re-queued and re-claimed, and
        already-ready clips are skipped then).
        """
        if not todo:
            return None

        total = len(todo)
        profile = self._renderer.profile
        semaphore = asyncio.Semaphore(self._settings.render_concurrency)
        state = {"done": 0}
        state_lock = asyncio.Lock()

        async def render_one(index: int, clip: dict[str, Any]) -> None:
            clip_id = str(clip["id"])
            try:
                start, end = float(clip["start_time"]), float(clip["end_time"])
            except (KeyError, TypeError, ValueError) as exc:
                raise FFmpegError(ERROR_RENDER_FAILED, "clip row is missing a valid window") from exc
            if not (0 <= start < end):
                raise FFmpegError(ERROR_RENDER_FAILED, "clip window is invalid")

            # Captions (Phase 5B): attach the clip's REAL words (persisted by
            # the analysis stage, absolute -> clip-relative). No valid words
            # (or captions disabled) -> the exact Phase 5A render.
            spec = base_spec
            if base_spec.captions:
                words = parse_caption_payload(clip.get("captions"), start, end - start)
                if words:
                    spec = dataclasses.replace(base_spec, caption_words=tuple(words))

            async with semaphore:
                out_file = destination.parent / f"clip-{index}.mp4"
                object_path = f"clips/{job_id}/{clip_id}.mp4"
                try:
                    await self._clip_store.mark_clip(job_id, clip_id, "processing")
                    await self._renderer.render(str(destination), start, end, spec, str(out_file))
                    try:
                        await verify_render_output(
                            self._settings.ffprobe_path,
                            out_file,
                            profile,
                            expected_duration=end - start,
                            timeout_seconds=self._settings.ffprobe_timeout_seconds,
                        )
                    except RenderValidationError:
                        out_file.unlink(missing_ok=True)
                        raise
                except (FFmpegError, RenderValidationError):
                    out_file.unlink(missing_ok=True)
                    raise
                try:
                    await self._storage.upload_from(object_path, out_file, self._settings.max_clip_size_bytes)
                except StorageUploadError:
                    out_file.unlink(missing_ok=True)
                    raise
                marked = await self._clip_store.mark_clip(job_id, clip_id, "ready", storage_path=object_path)
                out_file.unlink(missing_ok=True)
                if not marked:
                    raise LeaseLostError("clip completion update rejected (lease lost)")
                async with state_lock:
                    state["done"] += 1
                    await self._report(job_id, worker_id, _PROGRESS_RENDER_BASE + int(85 * state["done"] / total))

        clip_error: Exception | None = None
        try:
            async with asyncio.TaskGroup() as tg:
                for index, clip in enumerate(todo):
                    tg.create_task(render_one(index, clip), name=f"clip-{index}")
        except* LeaseLostError as eg:
            raise eg.exceptions[0]
        except* (FFmpegError, RenderValidationError, StorageUploadError, ClipsPersistenceError) as eg:
            clip_error = eg.exceptions[0]
        return clip_error

    @staticmethod
    def _categorize(exc: Exception) -> tuple[str, bool]:
        """Maps a clip-level exception to (error_category, permanent)."""
        if isinstance(exc, RenderValidationError):
            return ERROR_RENDER_INVALID, True
        if isinstance(exc, FFmpegError):
            return exc.category, exc.category in PERMANENT_ERROR_CATEGORIES
        if isinstance(exc, StorageUploadError):
            return ERROR_CLIP_UPLOAD, False
        if isinstance(exc, ClipsPersistenceError):
            return ERROR_CLIPS_PERSIST, False
        return ERROR_UNKNOWN, False

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
                logger.info("job %s: rendering failed (%s) -> %s", job_id, category, new_status)
        except Exception as exc:  # noqa: BLE001
            logger.error("job %s: could not record failure: %s", job_id, exc.__class__.__name__)
