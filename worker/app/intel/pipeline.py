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


class StreamIntelPipeline:
    """Runs (or resumes) the intel analysis for one job's source file."""

    def __init__(self, ffmpeg_path: str, store: IntelStore, config: PipelineConfig) -> None:
        self._ffmpeg = ffmpeg_path
        self._store = store
        self._config = config

    async def run(
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
            lower, upper = bounds[span.index]
            owned = [e for e in raw_events if lower <= e.t < upper]

            # Events FIRST (idempotent), then the checkpoint — a crash in
            # between only causes a safe re-detect + dedup on resume.
            summary = {
                **audio_feats.storage_summary(),
                **visual_feats.storage_summary(),
            }
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
        for row in all_summaries.values():
            feats = row.get("features") or {}
            summaries.append(feats)
            for gap in feats.get("silence_gaps") or []:
                silence_gaps.append((float(gap[0]), float(gap[1])))
        silence_gaps.sort()
        candidates = select_clips(
            windows,
            total_duration=total_duration,
            silence_gaps=silence_gaps,
            requested_clips=cfg.requested_clips,
            cfg=cfg.selection,
        )
        return PipelineResult(
            status="complete",
            total_chunks=len(spans),
            processed_chunks=processed,
            remaining_chunks=0,
            events=events,
            candidates=candidates,
            features_summaries=summaries,
        )
