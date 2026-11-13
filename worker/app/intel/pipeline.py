"""Stream-intelligence pipeline orchestrator (TASK 007).

Duration-independent, bounded, RESUMABLE analysis of one source file:

    plan chunks (deterministic, unbounded duration)
      → for each NOT-done chunk (bounded per-claim budget):
            bounded audio decode (temp file, deleted in finally)
            → audio features → audio events
            bounded decimated visual passes (null muxer, no disk)
            → visual events
            → keep the events this chunk OWNS (midpoint rule)
            → upsert events (exact-dedup) THEN mark chunk done
      → when all chunks are done:
            load all events + per-chunk storage summaries
            → fusion (scored, multi-family candidates)
            → selection (context, silence-snap, merge, dedup, categories)

Resource bounds (enforced, not aspirational):
- RAM: one chunk's PCM at a time (bounded by chunk_seconds); fusion input
  is the stored 1 Hz summaries + event rows — O(chunks), tiny for hours;
- disk: one chunk WAV (≤ chunk_seconds × 32 KB/s) under the job temp dir,
  removed in ``finally`` on EVERY path (success, failure, cancellation);
- concurrency: chunks are processed strictly sequentially (one FFmpeg
  pass at a time) — deterministic order, bounded CPU;
- per-claim work: at most ``chunks_per_claim`` chunks, then the run
  returns ``incomplete`` (NOT an error) — the next invocation resumes from
  checkpoints. A crash between events-upsert and checkpoint is safe: the
  retry re-detects and the upsert exact-deduplicates.

Nothing here touches the job state machine: the caller (future processor
hook, tests, benchmarks) owns the lease/reporting.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .audio_features import (
    AudioFeatureError,
    compute_audio_features,
    extract_chunk_wav,
    read_pcm16,
)
from .chunk_plan import plan_chunks, ownership_bounds
from .checkpoints import IntelStore
from .detectors import detect_audio_events, detect_visual_events
from .facecam import (
    FacecamAnalyzer,
    FacecamCancelled,
    FacecamConfig,
    detect_facecam_events,
    summarize_facecam,
    unavailable_report,
)
from .game_vision import (
    GameVisionAnalyzer,
    GameVisionCancelled,
    GameVisionChunkReport,
    GameVisionConfig,
    detect_game_vision_events,
    summarize_game_vision,
)
from .game_vision.ocr import OcrError
from .fusion import FusionConfig, fuse
from .selection import SelectedClip, SelectionConfig, select_clips
from .signals import SignalEvent
from .visual_features import VisualFeatureError, extract_visual_features

logger = logging.getLogger("clipforge.intel.pipeline")


class PipelineError(RuntimeError):
    """A chunk failed irrecoverably for THIS run (safe message)."""


class PipelineCancelled(Exception):
    """The run was asked to stop (shutdown/lease loss) — no state written for
    the in-flight chunk; completed chunks stay checkpointed."""


@dataclass(frozen=True)
class PipelineConfig:
    chunk_seconds: int = 600
    overlap_seconds: int = 30
    chunks_per_claim: int = 20
    chunk_decode_timeout_seconds: float = 300.0
    visual_pass_timeout_seconds: float = 300.0
    scene_threshold: float = 0.3
    fusion: FusionConfig = field(default_factory=FusionConfig)
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    requested_clips: int = 5
    #: TASK 008: facecam modality config (None = disabled → the pipeline
    #: behaves EXACTLY as before, zero overhead, zero behavioral change).
    facecam: FacecamConfig | None = None
    #: TASK 009: game vision modality config (None = disabled → same
    #: guarantee as facecam: zero overhead, zero behavioral change).
    game_vision: GameVisionConfig | None = None

    def __post_init__(self) -> None:
        if self.overlap_seconds < 0 or self.overlap_seconds >= self.chunk_seconds:
            raise ValueError("overlap must satisfy 0 <= overlap < chunk_seconds")
        if self.chunks_per_claim < 1:
            raise ValueError("chunks_per_claim must be >= 1")


@dataclass
class PipelineResult:
    status: str  # "complete" | "incomplete"
    total_chunks: int
    processed_chunks: int      # chunks done by THIS run
    remaining_chunks: int      # chunks still pending after this run
    events: list[SignalEvent]  # all persisted events (complete) / [] (incomplete)
    candidates: list[SelectedClip]  # ranked (complete) / [] (incomplete)
    features_summaries: list[dict[str, Any]] = field(default_factory=list)
    #: TASK 008: job-level facecam metadata (measured presence/region/model)
    #: — {} when facecam was disabled, {"available": False, "reason": ...}
    #: when it was configured but unavailable (never fabricated).
    facecam: dict[str, Any] = field(default_factory=dict)
    #: TASK 009: job-level game-vision metadata (engine, measured event
    #: counts, processing time) — {} when disabled, {"available": False,
    #: "reason": ...} when configured but unavailable (never fabricated).
    game_vision: dict[str, Any] = field(default_factory=dict)


class StreamIntelPipeline:
    """Runs (or resumes) the intel analysis for one job's source file."""

    def __init__(
        self,
        ffmpeg_path: str,
        store: IntelStore,
        config: PipelineConfig,
        facecam_analyzer: FacecamAnalyzer | None = None,
        game_vision_analyzer: GameVisionAnalyzer | None = None,
    ) -> None:
        self._ffmpeg = ffmpeg_path
        self._store = store
        self._config = config
        #: TASK 008: optional facecam analyzer (per-job; None or not
        #: available → the pipeline is byte-for-byte the Phase 7 behavior).
        self._facecam = facecam_analyzer
        #: TASK 009: optional game-vision analyzer (per-job; same
        #: guarantee — disabled/None → zero overhead, zero change).
        self._game_vision = game_vision_analyzer

    async def run(
        self,
        job_id: str,
        source_path: Path,
        total_duration: float,
        temp_dir: Path,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> PipelineResult:
        """Runs (or resumes) the analysis. Owns the optional analyzers'
        lifecycle: facecam (TASK 008) and game vision (TASK 009) resources
        are closed on EVERY exit path (complete, incomplete, cancelled,
        error)."""
        try:
            return await self._run_impl(job_id, source_path, total_duration,
                                        temp_dir, should_stop)
        finally:
            for _analyzer, _label in ((self._facecam, "facecam"),
                                      (self._game_vision, "game vision")):
                if _analyzer is not None:
                    try:
                        await _analyzer.close()
                    except Exception:  # noqa: BLE001 — teardown best-effort
                        logger.debug("%s analyzer close raised", _label, exc_info=True)

    async def _run_impl(
        self,
        job_id: str,
        source_path: Path,
        total_duration: float,
        temp_dir: Path,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> PipelineResult:
        cfg = self._config
        spans = plan_chunks(total_duration, cfg.chunk_seconds, cfg.overlap_seconds)
        bounds = ownership_bounds(spans, cfg.overlap_seconds)
        done = await self._store.load_done_chunks(job_id)

        pending = [s for s in spans if s.index not in done]
        to_process = pending[: cfg.chunks_per_claim]

        processed = 0
        new_summaries: dict[int, dict[str, Any]] = {}
        for span in to_process:
            if should_stop is not None and should_stop():
                raise PipelineCancelled()
            chunk_wav = temp_dir / f"intel-chunk-{span.index:04d}.wav"
            try:
                await extract_chunk_wav(
                    self._ffmpeg, source_path, span.start, span.duration, chunk_wav,
                    cfg.chunk_decode_timeout_seconds,
                )
                samples = await asyncio.to_thread(read_pcm16, chunk_wav)
                audio_feats = compute_audio_features(samples, span.start, span.duration)
                del samples  # release the PCM buffer before the visual passes
            finally:
                chunk_wav.unlink(missing_ok=True)

            visual_feats = await extract_visual_features(
                self._ffmpeg, source_path, span.start, span.duration,
                cfg.visual_pass_timeout_seconds, cfg.scene_threshold,
            )

            raw_events = detect_audio_events(audio_feats) + detect_visual_events(visual_feats)

            # TASK 008: optional facecam pass. An UNAVAILABLE facecam (no
            # model, missing dep, extraction failure) changes NOTHING about
            # the chunk outcome — audio/visual events persist as before and
            # the unavailable reason is recorded honestly in the summary.
            facecam_summary = None
            if cfg.facecam is not None and self._facecam is not None:
                if self._facecam.available:
                    facecam_dir = temp_dir / f"facecam-chunk-{span.index:04d}"
                    try:
                        report = await self._facecam.analyze_chunk(
                            self._ffmpeg, source_path, span.start, span.duration,
                            facecam_dir, should_stop,
                        )
                    except FacecamCancelled:
                        raise PipelineCancelled()
                    except Exception as exc:  # noqa: BLE001 — optional modality boundary
                        logger.warning("job %s: facecam chunk %d failed (%s); continuing without it",
                                       job_id, span.index, exc.__class__.__name__)
                        report = unavailable_report(span.start, span.duration,
                                                     cfg.facecam.sample_fps,
                                                     f"unexpected: {exc.__class__.__name__}")
                    facecam_summary = report.storage_summary()
                    if report.available:
                        raw_events = raw_events + detect_facecam_events(
                            report.observations, report.region, cfg.facecam, span.start,
                            model_name=report.model,
                        )
                else:
                    # Honest record: facecam was configured but unavailable
                    # (disabled, no model, missing dep) — no events, no cost.
                    facecam_summary = unavailable_report(
                        span.start, span.duration, cfg.facecam.sample_fps,
                        self._facecam.unavailable_reason or "facecam unavailable",
                    ).storage_summary()

            # TASK 009: optional game-vision pass — same contract as
            # facecam: an UNAVAILABLE game vision changes NOTHING about the
            # chunk outcome; events enter raw_events BEFORE ownership so
            # the existing midpoint/dedup semantics apply unchanged.
            game_vision_summary = None
            if cfg.game_vision is not None and self._game_vision is not None:
                if self._game_vision.available:
                    gv_dir = temp_dir / f"gamevision-chunk-{span.index:04d}"
                    try:
                        gv_report: GameVisionChunkReport = await \
                            self._game_vision.analyze_chunk(
                                self._ffmpeg, source_path, span.start,
                                span.duration, gv_dir, should_stop,
                            )
                    except GameVisionCancelled:
                        raise PipelineCancelled()
                    except OcrError as exc:
                        logger.warning("job %s: game vision chunk %d ocr failed (%s); continuing without it",
                                       job_id, span.index, exc)
                        gv_report = GameVisionChunkReport(
                            offset=span.start, duration=span.duration, available=False,
                            unavailable_reason=str(exc),
                            sample_fps=cfg.game_vision.sample_fps)
                    except Exception as exc:  # noqa: BLE001 — optional modality boundary
                        logger.warning("job %s: game vision chunk %d failed (%s); continuing without it",
                                       job_id, span.index, exc.__class__.__name__)
                        gv_report = GameVisionChunkReport(
                            offset=span.start, duration=span.duration, available=False,
                            unavailable_reason=f"unexpected: {exc.__class__.__name__}",
                            sample_fps=cfg.game_vision.sample_fps)
                    game_vision_summary = gv_report.storage_summary()
                    if gv_report.available:
                        raw_events = raw_events + detect_game_vision_events(
                            gv_report.candidates, cfg.game_vision, span.start,
                            engine_name=gv_report.engine,
                            engine_version=gv_report.engine_version,
                        )
                else:
                    game_vision_summary = GameVisionChunkReport(
                        offset=span.start, duration=span.duration, available=False,
                        unavailable_reason=(self._game_vision.unavailable_reason
                                            or "game vision unavailable"),
                        sample_fps=cfg.game_vision.sample_fps,
                    ).storage_summary()

            lower, upper = bounds[span.index]
            owned = [e for e in raw_events if lower <= e.t < upper]

            # Events FIRST (idempotent), then the checkpoint — a crash in
            # between only causes a safe re-detect + dedup on resume.
            summary = {
                **audio_feats.storage_summary(),
                **visual_feats.storage_summary(),
            }
            if facecam_summary is not None:
                summary["facecam"] = facecam_summary  # nested: no key collision
            if game_vision_summary is not None:
                summary["game_vision"] = game_vision_summary  # nested: no key collision
            await self._store.upsert_events(job_id, owned)
            await self._store.mark_chunk_done(job_id, span.index, span.start, span.end, summary)
            new_summaries[span.index] = summary
            processed += 1
            logger.info(
                "job %s: intel chunk %d/%d done (%d events owned)",
                job_id, span.index, len(spans), len(owned),
            )

        remaining = len(pending) - processed
        if remaining > 0:
            # Budget exhausted (or nothing else to do): resume next claim.
            return PipelineResult(
                status="incomplete",
                total_chunks=len(spans),
                processed_chunks=processed,
                remaining_chunks=remaining,
                events=[],
                candidates=[],
            )

        # -- complete: fusion + selection over the stored evidence -------------
        events = await self._store.load_events(job_id)
        windows = fuse(events, cfg.fusion)
        # Silence gaps + per-chunk summaries for boundary snapping
        # (chunks done in a PREVIOUS run AND in this one).
        all_summaries = {**done, **{idx: {"features": s} for idx, s in new_summaries.items()}}
        silence_gaps: list[tuple[float, float]] = []
        summaries: list[dict[str, Any]] = []
        facecam_summaries: list[dict[str, Any]] = []
        game_vision_summaries: list[dict[str, Any]] = []
        for row in all_summaries.values():
            feats = row.get("features") or {}
            summaries.append(feats)
            for gap in feats.get("silence_gaps") or []:
                silence_gaps.append((float(gap[0]), float(gap[1])))
            if "facecam" in feats:
                facecam_summaries.append(feats["facecam"])
            if "game_vision" in feats:
                game_vision_summaries.append(feats["game_vision"])
        silence_gaps.sort()
        candidates = select_clips(
            windows,
            total_duration=total_duration,
            silence_gaps=silence_gaps,
            requested_clips=cfg.requested_clips,
            cfg=cfg.selection,
        )
        facecam_meta = (
            summarize_facecam(facecam_summaries)
            if cfg.facecam is not None and facecam_summaries else {}
        )
        game_vision_meta = (
            summarize_game_vision(game_vision_summaries)
            if cfg.game_vision is not None and game_vision_summaries else {}
        )
        return PipelineResult(
            status="complete",
            total_chunks=len(spans),
            processed_chunks=processed,
            remaining_chunks=0,
            events=events,
            candidates=candidates,
            features_summaries=summaries,
            facecam=facecam_meta,
            game_vision=game_vision_meta,
        )
