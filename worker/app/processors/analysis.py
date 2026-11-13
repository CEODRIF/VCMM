"""Job analysis processor (Phase 4): transcribe → detect → score → persist.

A claimed job arrives as ``transcribing`` (fresh ``ready`` jobs, stage-aware
claim) or ``analyzing`` (crash recovery of an analysis that died mid-flight).
The lifecycle:

    transcribing ──► (audio + transcription) ──► analyzing
    analyzing    ──► (detection + scoring + persistence) ──► PARKED

Phase 4 FINISHES with the job parked at ``analyzing``: the lock is cleared,
progress is 100, and the detected clips are in ``public.clips``. The job is
then NOT claimable as analysis — the Phase 5A claim re-enters parked
``analyzing`` jobs (render budget available) and moves them to ``rendering``.
The worker NEVER marks a job ``completed`` in this phase.

DELIVERED in Phase 5A (this branch):
    - ``claim_next_job`` claims PARKED ``analyzing`` jobs (render budget
      available) and transitions them to ``rendering``;
    - the renderer consumes the job's ``clips`` rows (start/end already set)
      and the job becomes ``completed`` ONLY via ``complete_job_rendering``
      when every clip is ready with a storage path.

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

from ..ai.clip_detection import NoClipsDetectedError, ScoredClip, TranscriptClipDetector
from ..config.settings import Settings
from ..intel.audio_features import AudioFeatureError
from ..intel.checkpoints import IntelStore, IntelStoreError
from ..intel.facecam import FacecamConfig, build_facecam_analyzer
from ..intel.pipeline import PipelineCancelled, PipelineConfig, PipelineError, StreamIntelPipeline
from ..intel.selection import SelectedClip
from ..intel.visual_features import VisualFeatureError
from ..queue.models import (
    ERROR_CLIPS_PERSIST,
    ERROR_INTEL_ANALYSIS,
    ERROR_INTEL_RESUME_EXHAUSTED,
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
from ..validation.media import MediaInfo, MediaValidationError, probe_media
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
        intel_store: Optional[IntelStore] = None,
    ) -> None:
        self._settings = settings
        self._queue = queue
        self._storage = storage
        self._transcriber = transcriber
        self._clip_store = clip_store
        self._detector = detector or TranscriptClipDetector()
        #: Phase 7b: the checkpoint store for the resumable stream-intelligence
        #: pipeline (primary analysis path) or None (Phase 4 transcript-only
        #: path). When set, ``process_job`` runs a per-job pipeline built from
        #: the settings + this store; the transcript detector remains the
        #: fallback for a completed run that yields no signal candidates.
        self._intel_store = intel_store

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

            # 2b) Phase 7b: when the stream-intelligence pipeline is configured
            #     it is the PRIMARY analysis path (chunked, bounded, resumable,
            #     signal-based). It owns every terminal outcome (requeue /
            #     park / fail) and returns; the Phase 4 transcript path below
            #     runs only when intel is NOT configured, or as the fallback
            #     for a completed intel run that yielded no candidates.
            if self._intel_store is not None:
                await self._run_intel(job, job_id, worker_id, info, destination, tmp_dir)
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

    # -- Phase 7b: stream-intelligence (primary analysis path) -----------------

    #: Human-readable labels for the heuristic intel categories (title only).
    _INTEL_CATEGORY_LABELS = {
        "crazy_reaction": "Crazy reaction",
        "funny_moment": "Funny moment",
        "unexpected_moment": "Unexpected moment",
        "clutch_moment": "Clutch moment",
        "epic_kill": "Epic kill",
        "rage_moment": "Rage moment",
        "victory_moment": "Victory moment",
        "unexpected_failure": "Unexpected failure",
        "unclassified": "Highlight",
    }

    async def _run_intel(
        self,
        job: dict[str, Any],
        job_id: str,
        worker_id: str,
        info: MediaInfo,
        destination: Path,
        tmp_dir: Path,
    ) -> None:
        """Runs the resumable intel pipeline as the PRIMARY analysis path.

        Exactly one terminal outcome per call:
        - ``incomplete`` -> report chunk progress + requeue to resume (the next
          claim resumes from checkpoints, zero reprocessing);
        - ``complete``   -> transition to ``analyzing``, persist the signal
          candidates (or run the transcript fallback when none qualify) and
          park;
        - pipeline error -> ``fail_job`` (transient: the next claim resumes and
          retries the failed chunk). A failed chunk NEVER yields a silent
          incomplete success.
        Cancellation (lease loss / shutdown) performs no state writes; the
        completed chunks stay checkpointed for crash recovery.
        """
        # TASK 008: facecam modality (optional). Disabled/unconfigured →
        # None config + Null analyzer → the pipeline is exactly Phase 7.
        # Enabled → per-job analyzer (model created lazily on the first
        # chunk; a missing model/dep degrades to an honest unavailable).
        facecam_config: FacecamConfig | None = None
        facecam_analyzer = None
        if self._settings.facecam_enabled and self._settings.facecam_detector_model_path:
            facecam_config = FacecamConfig(
                sample_fps=float(self._settings.facecam_sample_fps),
                min_confidence=float(self._settings.facecam_min_confidence),
                max_faces=int(self._settings.facecam_max_faces),
                max_concurrency=int(self._settings.facecam_max_concurrency),
                detector_model_path=str(self._settings.facecam_detector_model_path),
                landmarker_model_path=(
                    str(self._settings.facecam_landmarker_model_path)
                    if self._settings.facecam_landmarker_model_path else None
                ),
                frame_timeout_seconds=float(self._settings.facecam_frame_timeout_seconds),
            )
            facecam_analyzer = build_facecam_analyzer(self._settings)

        # Per-job pipeline: the chunk/overlap/budget come from the settings
        # (operational limits); the clip cap honours the JOB's request.
        config = PipelineConfig(
            chunk_seconds=self._settings.intel_chunk_seconds,
            overlap_seconds=self._settings.intel_overlap_seconds,
            chunks_per_claim=self._settings.intel_chunks_per_claim,
            chunk_decode_timeout_seconds=self._settings.intel_chunk_timeout_seconds,
            visual_pass_timeout_seconds=self._settings.intel_chunk_timeout_seconds,
            scene_threshold=self._settings.intel_scene_threshold,
            requested_clips=int(job.get("requested_clips") or self._settings.intel_requested_clips),
            facecam=facecam_config,
        )
        pipeline = StreamIntelPipeline(
            self._settings.ffmpeg_path, self._intel_store, config,
            facecam_analyzer=facecam_analyzer,
        )
        try:
            result = await pipeline.run(job_id, destination, info.duration_seconds, tmp_dir)
        except PipelineCancelled:
            logger.info("job %s: intel run cancelled; progress stays checkpointed", job_id)
            return
        except (PipelineError, IntelStoreError, AudioFeatureError, VisualFeatureError) as exc:
            # A chunk failed irrecoverably for THIS run. Transient: resume from
            # checkpoints and retry the failed chunk on the next claim.
            await self._safe_fail(job_id, worker_id, ERROR_INTEL_ANALYSIS, str(exc), permanent=False)
            return

        if result.status == "incomplete":
            await self._report_chunk_progress(job_id, worker_id, result)
            outcome = await self._queue.requeue_incomplete_analysis(
                job_id, worker_id, self._settings.intel_max_resumes
            )
            if outcome == "not_owner":
                logger.warning("job %s: lost ownership before resume requeue; abandoning", job_id)
                return
            if outcome == "resume_exhausted":
                await self._safe_fail(
                    job_id, worker_id, ERROR_INTEL_RESUME_EXHAUSTED,
                    "analysis resume budget exhausted without completion", permanent=True,
                )
                return
            logger.info("job %s: intel incomplete (%d/%d chunks); re-queued to resume",
                        job_id, result.processed_chunks, result.total_chunks)
            return

        # ---- complete: finalize (crash-safe: transition first) --------------
        stage = await self._queue.transition_stage(job_id, worker_id, "transcribing", "analyzing")
        if stage == "not_owner":
            logger.warning("job %s: lost ownership before finalization; abandoning", job_id)
            return
        if stage == "already":
            logger.info("job %s: crash recovery — finalizing the completed intel run", job_id)

        if result.candidates:
            clips: list[ScoredClip] = [self._intel_clip(c) for c in result.candidates]
            await self._report(job_id, worker_id, 80)
        else:
            # No qualifying signal candidates: fall back to the Phase 4
            # transcript detector (extract + transcribe + detect).
            clips = await self._transcript_clips(job, job_id, worker_id, destination, tmp_dir)
            if clips is None:
                return  # the fallback already recorded its failure
        await self._persist_and_park(job_id, worker_id, clips)

    async def _report_chunk_progress(self, job_id: str, worker_id: str, result) -> None:
        """Reports overall chunk completion as progress (monotonic across resumes)."""
        if result.total_chunks:
            done = result.total_chunks - result.remaining_chunks
            pct = int(10 + 79 * done / result.total_chunks)
            await self._report(job_id, worker_id, max(10, min(89, pct)))

    async def _transcript_clips(
        self,
        job: dict[str, Any],
        job_id: str,
        worker_id: str,
        destination: Path,
        tmp_dir: Path,
    ) -> Optional[list[ScoredClip]]:
        """The Phase 4 transcript detection (extract + transcribe + detect),
        invoked as the fallback when a completed intel run yields no signal
        candidates. Returns the scored clips, or None after a recorded failure
        (the caller then returns without further terminal writes).
        """
        requested = int(job.get("requested_clips") or 3)
        target = float(job.get("clip_duration_seconds") or 90)

        audio_path = tmp_dir / "audio.wav"
        try:
            await extract_audio(
                self._settings.ffmpeg_path, destination, audio_path,
                self._settings.audio_extract_timeout_seconds, self._settings.audio_sample_rate,
            )
        except AudioExtractionError as exc:
            permanent = exc.category in PERMANENT_ERROR_CATEGORIES
            await self._safe_fail(job_id, worker_id, exc.category, exc.message, permanent=permanent)
            return None
        await self._report(job_id, worker_id, 30)

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
            await self._safe_fail(job_id, worker_id, ERROR_TRANSCRIPTION_UNAVAILABLE, str(exc), permanent=True)
            return None
        except TranscriptionError as exc:
            await self._safe_fail(job_id, worker_id, ERROR_TRANSCRIPTION, str(exc), permanent=False)
            return None
        if result.word_count == 0:
            await self._safe_fail(job_id, worker_id, ERROR_NO_SPEECH,
                                  "no detectable speech in the audio track", permanent=True)
            return None
        await self._report(job_id, worker_id, 55)

        try:
            clips = self._detector.detect_scored(list(result.segments), requested, int(target))
        except NoClipsDetectedError as exc:
            await self._safe_fail(job_id, worker_id, ERROR_NO_CLIPS, str(exc), permanent=True)
            return None
        await self._report(job_id, worker_id, 80)
        return clips

    async def _persist_and_park(self, job_id: str, worker_id: str, clips: list[ScoredClip]) -> None:
        """Persists the final clip set and parks the job (shared by the intel
        candidates path and the transcript fallback)."""
        try:
            count = await self._clip_store.save_clips(job_id, clips)
        except ClipsPersistenceError as exc:
            await self._safe_fail(job_id, worker_id, ERROR_CLIPS_PERSIST, str(exc), permanent=False)
            return
        await self._report(job_id, worker_id, 95)
        parked = await self._queue.park_after_analysis(job_id, worker_id)
        if not parked:
            logger.warning("job %s: could not park — ownership lost; clips may persist", job_id)
            return
        logger.info("job %s analysis complete: %d clip(s) parked at 'analyzing'", job_id, count)

    def _intel_clip(self, candidate: SelectedClip) -> ScoredClip:
        """Maps an intel-selected candidate to a persistable :class:`ScoredClip`.

        The ``scoring`` payload is the MEASURED signal evidence (heuristic,
        labeled) — NOT a transcript score and NOT a virality prediction. Signal
        clips carry no transcribed words (no burned-in subtitles).
        """
        label = self._INTEL_CATEGORY_LABELS.get(candidate.category, "Highlight")
        minutes, seconds = divmod(int(candidate.start), 60)
        title = f"{label} at {minutes:02d}:{seconds:02d}"
        scoring = {
            "total": round(candidate.score, 2),
            "scale": "0-100",
            "method": "stream-intelligence (signal-based, heuristic)",
            "category": candidate.category,
            "confidence": round(candidate.confidence, 4),
            "evidence": dict(candidate.evidence or {}),
            "criteria": [],  # no transcript criteria apply to signal clips
        }
        return ScoredClip(
            start=round(candidate.start, 3),
            end=round(candidate.end, 3),
            title=title,
            score=round(candidate.score, 2),
            criterions=(),
            scoring=scoring,
            words=(),
        )


def is_analysis_stage(status: str) -> bool:
    """True for the stages the Phase 4 processor handles."""
    return status in (JobStatus.TRANSCRIBING.value, JobStatus.ANALYZING.value)
