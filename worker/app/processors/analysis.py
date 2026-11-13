"""Job analysis processor (Phase 4): transcribe → detect → score → persist.

A claimed job arrives as ``transcribing`` (fresh ``ready`` jobs, stage-aware
claim) or ``analyzing`` (crash recovery of an analysis that died mid-flight).
The lifecycle:

    transcribing ──► (audio + transcription) ──► analyzing
    analyzing    ──► (detection + scoring + persistence) ──► PARKED

Phase 4 FINISHES with the job parked at ``analyzing``: the lock is cleared,
progress is 100, and the detected clips are in ``public.clips``. The job is
then NOT claimable — it is picked up by the Phase 5 renderer, which will
extend the claim to parked ``analyzing`` jobs and move them to ``rendering``.
The worker NEVER marks a job ``completed`` in this phase.

INTEGRATION POINT (Phase 5 rendering):
    - ``claim_next_job`` must also claim ``analyzing`` jobs whose lock is
      CLEARED (parked) and transition them to ``rendering``;
    - the renderer consumes ``clips`` rows (start/end already set) for the
      job and marks the job ``completed`` when every clip is rendered.

Safety: same lease/ownership contract as ingestion — a renewal task keeps
the lease alive, ownership loss aborts the job without state writes,
cancellation (shutdown) performs no writes, and the temp directory is
always removed.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

from ..ai.clip_detection import NoClipsDetectedError, TranscriptClipDetector
from ..config.settings import Settings
from ..queue.models import (
    ERROR_CLIPS_PERSIST,
    ERROR_NO_AUDIO,
    ERROR_NO_CLIPS,
    ERROR_NO_SPEECH,
    ERROR_STORAGE_DOWNLOAD,
    ERROR_TRANSCRIPTION,
    ERROR_TRANSCRIPTION_UNAVAILABLE,
    ERROR_UNKNOWN,
    JobStatus,
    PERMANENT_ERROR_CATEGORIES,
)
from ..queue.supabase_queue import SupabaseQueue
from ..storage.clip_store import ClipsPersistenceError, SupabaseClipStore
from ..storage.supabase_storage import StorageDownloadError, SupabaseStorage
from ..transcription.base import BaseTranscriber, TranscriptionError, TranscriptionUnavailableError
from ..transcription.chunking import transcribe_chunked
from ..validation.media import MediaValidationError, probe_media
from .audio import AudioExtractionError, extract_audio
from .ingestion import LeaseLostError

logger = logging.getLogger("clipforge.analysis")


class AnalysisProcessor:
    def __init__(
        self,
        settings: Settings,
        queue: SupabaseQueue,
        storage: SupabaseStorage,
        transcriber: BaseTranscriber,
        clip_store: SupabaseClipStore,
        detector: Optional[TranscriptClipDetector] = None,
    ) -> None:
        self._settings = settings
        self._queue = queue
        self._storage = storage
        self._transcriber = transcriber
        self._clip_store = clip_store
        self._detector = detector or TranscriptClipDetector()

    async def process_job(self, job: dict[str, Any], on_lease_lost: Optional[Callable[[], None]] = None) -> None:
        """Runs the full analysis lifecycle for one claimed job.

        Guarantees: at most one terminal queue write (fail) or one park,
        lease-aware abort, and temp-file cleanup on every path.
        """
        job_id = str(job["id"])
        worker_id = str(job.get("_worker_id") or "")
        source_path = job.get("source_path")
        tmp_dir: Path = self._settings.tmp_dir / job_id
        lease_task: Optional[asyncio.Task] = None
        try:
            status = str(job.get("status") or "")
            if status not in (JobStatus.TRANSCRIBING.value, JobStatus.ANALYZING.value):
                # Internal invariant: the claim only yields analysis-stage
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
            await self._report(job_id, worker_id, 10)
            try:
                await self._storage.download_to(source_path, destination)
            except StorageDownloadError as exc:
                await self._safe_fail(job_id, worker_id, ERROR_STORAGE_DOWNLOAD,
                                      str(exc), permanent=False)
                return

            # 2) Re-probe (never trust the previous probe) and require audio.
            info = await probe_media(
                self._settings.ffprobe_path,
                destination,
                self._settings.ffprobe_timeout_seconds,
            )
            if not info.has_audio:
                await self._safe_fail(job_id, worker_id, ERROR_NO_AUDIO,
                                      "the video has no audio track to transcribe",
                                      permanent=True)
                return

            # 3) Extract the mono 16 kHz audio track.
            audio_path = tmp_dir / "audio.wav"
            try:
                await extract_audio(
                    self._settings.ffmpeg_path,
                    destination,
                    audio_path,
                    self._settings.audio_extract_timeout_seconds,
                    self._settings.audio_sample_rate,
                )
            except AudioExtractionError as exc:
                permanent = exc.category in PERMANENT_ERROR_CATEGORIES
                await self._safe_fail(job_id, worker_id, exc.category, exc.message, permanent=permanent)
                return
            await self._report(job_id, worker_id, 30)

            # 4) Transcribe (chunked for long recordings, real engine only).
            try:
                result = await transcribe_chunked(
                    self._transcriber,
                    ffmpeg_path=self._settings.ffmpeg_path,
                    ffprobe_path=self._settings.ffprobe_path,
                    audio_path=audio_path,
                    chunk_seconds=self._settings.transcription_chunk_seconds,
                    overlap_seconds=self._settings.transcription_overlap_seconds,
                    probe_timeout_seconds=self._settings.ffprobe_timeout_seconds,
                    split_timeout_seconds=self._settings.audio_extract_timeout_seconds,
                    language=self._settings.transcription_language,
                )
            except TranscriptionUnavailableError as exc:
                await self._safe_fail(job_id, worker_id, ERROR_TRANSCRIPTION_UNAVAILABLE,
                                      str(exc), permanent=True)
                return
            except TranscriptionError as exc:
                await self._safe_fail(job_id, worker_id, ERROR_TRANSCRIPTION,
                                      str(exc), permanent=False)
                return
            if result.word_count == 0:
                await self._safe_fail(job_id, worker_id, ERROR_NO_SPEECH,
                                      "no detectable speech in the audio track",
                                      permanent=True)
                return
            await self._report(job_id, worker_id, 55)

            # 5) transcribing → analyzing (guarded; recovery = 'already').
            stage = await self._queue.transition_stage(job_id, worker_id, "transcribing", "analyzing")
            if stage == "not_owner":
                logger.warning("job %s: lost ownership before analysis; abandoning", job_id)
                return
            if stage == "already":
                logger.info("job %s: crash recovery — continuing from analyzing", job_id)

            # 6) Detect + score the best complete-idea windows.
            requested = int(job.get("requested_clips") or 3)
            target = float(job.get("clip_duration_seconds") or 90)
            try:
                clips = self._detector.detect_scored(list(result.segments), requested, int(target))
            except NoClipsDetectedError as exc:
                await self._safe_fail(job_id, worker_id, ERROR_NO_CLIPS, str(exc), permanent=True)
                return
            await self._report(job_id, worker_id, 80)

            # 7) Persist the explainable clip set (scoped to this job).
            try:
                count = await self._clip_store.save_clips(job_id, clips)
            except ClipsPersistenceError as exc:
                await self._safe_fail(job_id, worker_id, ERROR_CLIPS_PERSIST,
                                      str(exc), permanent=False)
                return

            await self._report(job_id, worker_id, 95)

            # 8) PARK at analyzing (lock cleared). Phase 5 renders from here.
            parked = await self._queue.park_after_analysis(job_id, worker_id)
            if not parked:
                logger.warning("job %s: could not park — ownership lost; clips may persist", job_id)
                return
            logger.info(
                "job %s analysis complete: %d clips (%s) parked at 'analyzing' — awaiting Phase 5 rendering",
                job_id, count, ", ".join(f"{c.score:.0f}" for c in clips),
            )
        except LeaseLostError:
            logger.warning("job %s: lease lost during analysis; abandoning", job_id)
        except asyncio.CancelledError:
            # Shutdown / lease-loss cancel — do NOT write with a stale lease.
            logger.info("job %s: analysis cancelled (lease loss or shutdown)", job_id)
            raise
        except MediaValidationError as exc:
            permanent = exc.category in PERMANENT_ERROR_CATEGORIES
            await self._safe_fail(job_id, worker_id, exc.category, exc.message, permanent=permanent)
        except Exception as exc:  # noqa: BLE001 — top-level job boundary
            logger.exception("job %s: unexpected analysis error: %s", job_id, exc.__class__.__name__)
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
                logger.info("job %s: analysis failed (%s) -> %s", job_id, category, new_status)
        except Exception as exc:  # noqa: BLE001
            logger.error("job %s: could not record failure: %s", job_id, exc.__class__.__name__)


def is_analysis_stage(status: str) -> bool:
    """True for the stages the Phase 4 processor handles."""
    return status in (JobStatus.TRANSCRIBING.value, JobStatus.ANALYZING.value)
