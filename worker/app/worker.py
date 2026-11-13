"""Worker loop (Phase 3 ingestion + Phase 4 analysis + Phase 5 rendering).

- Polls the queue and claims jobs atomically via the restricted RPC
  (stage-aware: queued → validating, ready → transcribing, PARKED analyzing
  → rendering, expired active stages recovered in place).
- Dispatches by the claimed stage: ``validating`` → ingestion processor,
  ``transcribing``/``analyzing`` → analysis processor, ``rendering`` →
  rendering processor.
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
import random
import shutil
import time
import uuid
from typing import Optional

from .config.settings import Settings
from .observability import context as log_context
from .observability import metrics as m
from .processors.analysis import AnalysisProcessor, is_analysis_stage
from .processors.ingestion import IngestionProcessor
from .processors.rendering import RenderProcessor, is_rendering_stage
from .queue.supabase_queue import QueueError, SupabaseQueue
from .storage.supabase_storage import SupabaseStorage

logger = logging.getLogger("clipforge.worker")


class IngestionWorker:
    def __init__(
        self,
        settings: Settings,
        queue: SupabaseQueue,
        storage: SupabaseStorage,
        analysis: Optional[AnalysisProcessor] = None,
        ingestion: Optional[IngestionProcessor] = None,
        rendering: Optional[RenderProcessor] = None,
    ) -> None:
        self._settings = settings
        self._queue = queue
        self._storage = storage
        self._ingestion = ingestion or IngestionProcessor(settings, queue, storage)
        self._analysis = analysis
        self._rendering = rendering
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
        m.gauge_uptime(0.0)
        # TASK 012: reclaim per-job scratch dirs abandoned by a crashed
        # worker (conservative age threshold + live-job exclusion).
        self._cleanup_stale_tmp()
        self._loop_task = asyncio.create_task(self._loop(), name="job-loop")
        logger.info("worker %s started (concurrency=%d, lease=%ds, poll=%.1fs, analysis=%s)",
                    self._worker_id, self._settings.concurrency,
                    self._settings.job_lease_seconds, self._settings.poll_interval,
                    "on" if self._analysis is not None else "off")

    def _cleanup_stale_tmp(self) -> None:
        """Removes per-job temp directories abandoned by a CRASHED worker.

        Conservative by construction: a directory is removed only when its
        mtime is older than ``worker_tmp_stale_seconds`` (default 48 h —
        longer than any legitimate single-claim wall time) AND its name is
        not one of this worker's active job tasks. A directory that is
        currently referenced by a live job can therefore never be deleted.
        """
        tmp_root = self._settings.tmp_dir
        try:
            entries = list(tmp_root.iterdir())
        except (FileNotFoundError, OSError):
            return
        cutoff = time.time() - self._settings.worker_tmp_stale_seconds
        active_ids = {
            (task.get_name() or "").removeprefix("job-") for task in list(self._active)
        }
        for entry in entries:
            if not entry.is_dir() or entry.name in active_ids:
                continue
            try:
                if entry.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            logger.info("removed stale temp directory %s (older than %.0f h)",
                        entry.name, self._settings.worker_tmp_stale_seconds / 3600)

    async def stop(self) -> None:
        """Graceful shutdown: stop claiming; let in-flight jobs finish
        safely within a BOUNDED grace period (TASK 012).

        Jobs still running when the grace expires are ABANDONED (cancelled):
        the processors' cancellation paths write NO final state (the lease
        simply expires and another worker recovers the job), and every
        subprocess helper kills its child process on cancellation — so a
        slow shutdown can neither orphan a process nor mis-fail a job.
        """
        self._stop.set()
        loop_task = getattr(self, "_loop_task", None)
        if loop_task is not None:
            loop_task.cancel()
            try:
                await loop_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._active:
            grace = float(self._settings.worker_shutdown_grace_seconds)
            logger.info("waiting for %d active job(s) (grace %.0fs)", len(self._active), grace)
            done, pending = await asyncio.wait(list(self._active), timeout=grace)
            if pending:
                logger.warning("shutdown grace expired: abandoning %d job(s) without a "
                               "state write (their leases expire and are recovered "
                               "by the claim RPC)", len(pending))
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        # TASK 011: release provider-layer resources (token clients).
        await self._ingestion.aclose()
        # TASK 012: close the Supabase HTTP clients (queue + storage).
        await self._queue.aclose()
        await self._storage.aclose()
        m.gauge_uptime(0.0)
        logger.info("worker %s stopped", self._worker_id)

    # -- readiness probing (TASK 012, spec section 7) ----------------------------

    async def probe_dependencies(self, timeout: float = 5.0, cache_ttl: float = 30.0) -> dict:
        """Liveness-checked dependency probe for ``GET /ready``.

        Never reports "ready" on process startup alone: the database and
        storage probes are REAL (bounded, cached) checks. Optional
        dependencies (transcription / facecam / game vision) and provider
        configuration are REPORTED but never gate readiness when disabled —
        a missing optional dependency only degrades its own feature.
        """
        now = time.monotonic()
        cached = getattr(self, "_probe_cache", None)
        if cached is not None and now - cached[0] < cache_ttl:
            return cached[1]
        db_ok, storage_ok = await asyncio.gather(
            self._probe(self._queue.ping, timeout),
            self._probe(self._storage.ping, timeout),
        )
        payload = {
            "database": "ok" if db_ok else "error",
            "storage": "ok" if storage_ok else "error",
            "queue": "ok" if db_ok else "error",  # the queue IS the database RPCs
            "optional": self._optional_components(),
            "providers": self._provider_components(),
        }
        self._probe_cache = (now, payload)
        return payload

    @staticmethod
    async def _probe(coro_fn, timeout: float) -> bool:
        try:
            return bool(await asyncio.wait_for(coro_fn(), timeout=timeout))
        except Exception:  # noqa: BLE001 — a probe must never raise
            return False

    def _optional_components(self) -> dict:
        """Honest state of the OPTIONAL features. 'degraded' = enabled but a
        runtime dependency is missing (jobs then fail with a clear, typed,
        permanent error — never fabricated output)."""
        import importlib.util
        import shutil as _shutil

        s = self._settings
        components: dict[str, str] = {}
        # transcription: enabled when a whisper model is configured.
        components["transcription"] = "enabled" if s.whisper_model else "disabled"
        # facecam: needs the optional mediapipe extra AND explicit model files.
        if not s.facecam_enabled:
            components["facecam"] = "disabled"
        else:
            ok = importlib.util.find_spec("mediapipe") is not None
            if ok:
                from pathlib import Path

                ok = bool(s.facecam_detector_model_path) and Path(s.facecam_detector_model_path).is_file()
            components["facecam"] = "enabled" if ok else "degraded"
        # game vision: needs the tesseract binary.
        if not s.game_vision_enabled:
            components["game_vision"] = "disabled"
        else:
            binary = s.game_vision_tesseract_binary or "tesseract"
            ok = _shutil.which(binary) is not None
            components["game_vision"] = "enabled" if ok else "degraded"
        # core binaries: missing ffmpeg/ffprobe degrades ALL video work.
        from pathlib import Path

        core_ok = (
            _shutil.which(s.ffmpeg_path) is not None or Path(s.ffmpeg_path).is_file()
        ) and (_shutil.which(s.ffprobe_path) is not None or Path(s.ffprobe_path).is_file())
        components["ffmpeg"] = "enabled" if core_ok else "degraded"
        return components

    def _provider_components(self) -> dict:
        """Which OFFICIAL capability level each provider is configured for
        (never credentials — only the mode name)."""
        s = self._settings
        if s.youtube_api_key:
            yt = "data-api-key"
        elif s.youtube_oauth_client_id and s.youtube_oauth_client_secret and s.youtube_oauth_refresh_token:
            yt = "oauth"
        else:
            yt = "keyless-oembed"
        if s.twitch_client_id and s.twitch_client_secret:
            tw = "app-token"
        elif s.twitch_client_id:
            tw = "client-id"
        else:
            tw = "unconfigured"
        return {
            "youtube": yt,
            "twitch": tw,
            "master_switch": "on" if s.source_providers_enabled else "off",
            "materialization": "unsupported (no official media-download API — by design)",
        }

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
                m.claim_errors_total.inc()
                m.gauge_claim_errors(self._consecutive_claim_errors)
                # TASK 012: JITTERED exponential backoff, capped at 60s
                # (2s, 4s, 8s, ...). Full jitter on the upper half: a fleet
                # of workers recovering from the same outage must not
                # thundering-herd the queue on identical timers.
                base = min(60.0, self._settings.poll_interval * (2 ** min(self._consecutive_claim_errors, 5)))
                delay = random.uniform(base / 2.0, base)
                logger.warning("claim failed (%s); backing off %.1fs", exc.__class__.__name__, delay)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — loop boundary
                logger.exception("worker loop error: %s", exc.__class__.__name__)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            m.gauge_uptime(time.monotonic() - self._started_at)
            m.gauge_active_jobs(len(self._active))

    def _processor_for(self, job: dict):
        """Stage-aware dispatch (Phases 3-5): validating → ingestion,
        transcribing/analyzing → analysis, rendering → rendering."""
        status = str(job.get("status") or "")
        if is_analysis_stage(status):
            return self._analysis
        if is_rendering_stage(status):
            return self._rendering
        return self._ingestion

    async def _guarded(self, job: dict) -> None:
        async with self._semaphore:
            status = str(job.get("status") or "")
            stage = m.stage_for_status(status)
            # Provider jobs carry the provider in their log context too.
            provider = None
            if job.get("source_type") in ("youtube", "twitch"):
                provider = str(job["source_type"])
            started = time.monotonic()
            # TASK 012: every log line emitted during this claim (including
            # the queue client, storage, providers, subprocesses' callers)
            # carries the job's correlation context.
            with log_context.job_context(
                job_id=str(job["id"]), worker_id=self._worker_id,
                stage=stage, attempt=job.get("attempt_count"), provider=provider,
            ):
                processor = self._processor_for(job)
                if processor is None:
                    # Analysis/rendering-stage job but no such capability in
                    # this build: release gracefully (back to the stage entry
                    # point: ready for analysis, parked analyzing for
                    # rendering) instead of failing — another worker may
                    # handle it.
                    released = await self._queue.release_job(str(job["id"]), self._worker_id)
                    logger.warning("job %s: no processor for stage %r — released (%s)",
                                   job.get("id"), job.get("status"), "ok" if released else "not_owner")
                    return
                processing_task = asyncio.current_task()
                try:
                    await processor.process_job(job, on_lease_lost=processing_task.cancel)
                    m.observe_processing_time(stage, time.monotonic() - started)
                except asyncio.CancelledError:
                    # Lease loss / shutdown: the job continues on its next
                    # claim; count the cancellation, write no state.
                    m.jobs_cancelled_total.inc({"stage": stage})
                    raise
                except Exception as exc:  # noqa: BLE001 — never kill the loop
                    m.observe_processing_time(stage, time.monotonic() - started)
                    logger.exception("job %s crashed: %s", job.get("id"), exc.__class__.__name__)
