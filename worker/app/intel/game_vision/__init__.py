"""Game Vision Intelligence (TASK 009) — local-first, optional modality.

Public API:

- ``GameVisionConfig``       — validated per-job configuration + limits;
- ``GameVisionChunkReport``  — one chunk's measured game-vision state;
- ``GameVisionAnalyzer``     — the interface the pipeline consumes;
- ``NullGameVisionAnalyzer`` — the honest fallback (disabled/unconfigured);
- ``TesseractGameVisionAnalyzer`` — the real analyzer (local tesseract);
- ``build_game_vision_analyzer`` — settings → analyzer (never raises);
- ``summarize_game_vision``  — job-level metadata for PipelineResult.

Honesty contract (same discipline as facecam, TASK 008): a missing
binary, missing/unreadable tessdata, repeated OCR failures, or an
extraction failure ALWAYS yields an ``available=False`` report (or an
honest budget/truncation record) with the reason persisted — the
pipeline continues on audio/visual/facecam evidence and no game-vision
event is ever fabricated. Disabled/unavailable => the pipeline behaves
exactly as before.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .config import GameVisionConfig
from .events import GameplayCandidate, interpret_gameplay
from .frames import FrameExtractionError, extract_roi_frames
from .hud import FrameDiff, HudElement, detect_stable_elements, measure_frame_diffs, stable_keys
from .kill_feed import KillFeedEvent, analyze_kill_feed
from .ocr import OcrEngine, OcrError, OcrLine, NullOcrEngine, TesseractOcrEngine
from .scoreboard import ScoreValueChange, ScoreboardObservation, detect_scoreboard, track_score_changes
from .signals import detect_game_vision_events

logger = logging.getLogger("clipforge.intel.game_vision")

__all__ = [
    "GameVisionCancelled",
    "GameVisionChunkReport",
    "GameVisionConfig",
    "GameplayCandidate",
    "GameVisionAnalyzer",
    "NullGameVisionAnalyzer",
    "TesseractGameVisionAnalyzer",
    "build_game_vision_analyzer",
    "summarize_game_vision",
    "detect_game_vision_events",
    "unavailable_report",
]


class GameVisionCancelled(Exception):
    """A stop was requested (shutdown/lease loss) mid game-vision work.

    Defined here (NOT imported from ``pipeline``) to keep the dependency
    one-way: pipeline → game_vision. The pipeline maps this to
    ``PipelineCancelled`` so the existing no-write cancellation contract
    applies unchanged.
    """


@dataclass
class GameVisionChunkReport:
    """One chunk's measured game-vision state (never fabricated)."""

    offset: float
    duration: float
    available: bool
    unavailable_reason: str | None
    sample_fps: float
    engine: str | None = None
    engine_version: str | None = None
    frames_analyzed: int = 0
    ocr_operations: int = 0
    budget_exceeded: bool = False
    frames_remaining: int = 0
    processing_seconds: float = 0.0
    kill_feed_events: list[KillFeedEvent] = field(default_factory=list)
    stable_elements: list[HudElement] = field(default_factory=list)
    scoreboard_observations: list[ScoreboardObservation] = field(default_factory=list)
    score_changes: list[ScoreValueChange] = field(default_factory=list)
    frame_diffs: list[FrameDiff] = field(default_factory=list)
    candidates: list[GameplayCandidate] = field(default_factory=list)

    def storage_summary(self) -> dict[str, Any]:
        """Compact per-chunk summary — persisted (NESTED) in the checkpoint
        row's ``features`` jsonb: no schema change, no migration."""
        return {
            "family": "game_vision",
            "offset": round(self.offset, 3),
            "duration": round(self.duration, 3),
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "engine": self.engine,
            "engine_version": self.engine_version,
            "sample_fps": self.sample_fps,
            "frames_analyzed": self.frames_analyzed,
            "ocr_operations": self.ocr_operations,
            "budget_exceeded": self.budget_exceeded,
            "frames_remaining": self.frames_remaining,
            "processing_seconds": round(self.processing_seconds, 1),
            "kill_feed_events": len(self.kill_feed_events),
            "stable_elements": len(self.stable_elements),
            "scoreboard_visible_frames": sum(1 for o in self.scoreboard_observations if o.visible),
            "scoreboard_scanned_frames": len(self.scoreboard_observations),
            "score_changes": len(self.score_changes),
            "candidate_kinds": sorted({c.kind for c in self.candidates}),
            "note": ("game vision evidence is OCR-observed visual content; "
                     "interpretation is heuristic, not verified game state"),
        }


def unavailable_report(
    offset: float, duration: float, sample_fps: float, reason: str,
    engine: str | None = None,
) -> GameVisionChunkReport:
    return GameVisionChunkReport(
        offset=offset, duration=duration, available=False, unavailable_reason=reason,
        sample_fps=sample_fps, engine=engine,
    )


class GameVisionAnalyzer:
    """Interface the pipeline consumes. ``available`` is checked BEFORE
    any chunk work; a not-available analyzer costs nothing and changes
    nothing."""

    name: str = "base"

    def __init__(self) -> None:
        self.available = False
        self.unavailable_reason: str | None = None
        #: The validated config this analyzer runs with (None for Null —
        #: the pipeline mirrors it into PipelineConfig, single source of
        #: truth, no double construction from settings).
        self.config: GameVisionConfig | None = None

    async def analyze_chunk(
        self,
        ffmpeg_path: str,
        source: Path,
        offset: float,
        length: float,
        out_dir: Path,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> GameVisionChunkReport:  # pragma: no cover - interface
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover - interface
        return None


class NullGameVisionAnalyzer(GameVisionAnalyzer):
    """Honest fallback: game vision disabled/unconfigured → no events, no cost."""

    name = "null"

    def __init__(self, reason: str) -> None:
        super().__init__()
        self.available = False
        self.unavailable_reason = reason

    async def analyze_chunk(self, ffmpeg_path, source, offset, length, out_dir,
                            should_stop=None) -> GameVisionChunkReport:
        return unavailable_report(offset, length, 0.0, self.unavailable_reason)


class TesseractGameVisionAnalyzer(GameVisionAnalyzer):
    """The real analyzer: bounded frame extraction + local tesseract OCR.

    Per-job instance. The engine is validated lazily on the FIRST chunk;
    ANY validation failure marks the analyzer unavailable for the whole
    job (honest, recorded, no retry storm) while the rest of the pipeline
    proceeds. Repeated OCR failures during a chunk do the same.
    """

    name = "tesseract-ocr"

    def __init__(self, config: GameVisionConfig, engine: OcrEngine) -> None:
        super().__init__()
        self._cfg = config
        self.config = config
        self._engine = engine
        self.available = True
        self.unavailable_reason = None
        self._validated = False

    async def analyze_chunk(
        self,
        ffmpeg_path: str,
        source: Path,
        offset: float,
        length: float,
        out_dir: Path,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> GameVisionChunkReport:
        cfg = self._cfg
        t0 = time.monotonic()

        # 1) one-time engine validation (sticky honest degradation)
        if not self._validated:
            try:
                await self._engine.validate()
                self._validated = True
            except OcrError as exc:
                self.available = False
                self.unavailable_reason = str(exc)
                logger.info("game vision unavailable for job: %s", exc)
                return unavailable_report(offset, length, cfg.sample_fps, str(exc),
                                          engine=self._engine.name)

        # 2) bounded frame extraction: kill-feed ROI + full frame (scoreboard)
        kf_dir = out_dir / "killfeed"
        full_dir = out_dir / "full"
        try:
            kf_frames = await extract_roi_frames(
                ffmpeg_path, source, offset, length,
                cfg.sample_fps, cfg.max_frames_per_chunk, kf_dir,
                cfg.frame_timeout_seconds, roi=cfg.kill_feed_roi,
                max_dim=cfg.max_image_dimension,
            )
        except FrameExtractionError as exc:
            logger.warning("game vision frame extraction failed (chunk @ %.0fs): %s",
                           offset, exc)
            shutil.rmtree(out_dir, ignore_errors=True)
            return unavailable_report(offset, length, cfg.sample_fps, str(exc),
                                      engine=self._engine.name)

        full_frames: list[tuple[float, Path]] = []
        try:
            full_frames = await extract_roi_frames(
                ffmpeg_path, source, offset, length,
                cfg.sample_fps, cfg.max_frames_per_chunk, full_dir,
                cfg.frame_timeout_seconds, roi=None,
                max_dim=cfg.max_image_dimension,
            )
        except FrameExtractionError:
            full_frames = []  # scoreboard sub-modality skipped (recorded below)

        # 3) OCR loop: bounded by ops cap + wall budget; checked BETWEEN ops
        ocr_work = out_dir / "ocr"
        ocr_work.mkdir(parents=True, exist_ok=True)
        kf_lines: list[tuple[float, list[OcrLine]]] = []
        full_lines: list[tuple[float, list[OcrLine]]] = []
        ops = 0
        consec_errors = 0
        budget = False
        remaining = 0
        try:
            for i, (t_rel, kf_path) in enumerate(kf_frames):
                if should_stop is not None and should_stop():
                    raise GameVisionCancelled()
                if time.monotonic() - t0 > cfg.max_chunk_seconds:
                    budget, remaining = True, len(kf_frames) - i
                    break
                # full-frame OCR (scoreboard) on the sub-sampled grid
                if i < len(full_frames) and i % cfg.scoreboard_scan_interval == 0:
                    if ops >= cfg.max_ocr_operations_per_chunk:
                        budget, remaining = True, len(kf_frames) - i
                        break
                    ops += 1
                    try:
                        full_lines.append((full_frames[i][0],
                                           await self._engine.recognize(full_frames[i][1], ocr_work)))
                        consec_errors = 0
                    except OcrError:
                        consec_errors += 1
                        full_lines.append((full_frames[i][0], []))
                if ops >= cfg.max_ocr_operations_per_chunk:
                    budget, remaining = True, len(kf_frames) - i
                    break
                ops += 1
                try:
                    kf_lines.append((t_rel, await self._engine.recognize(kf_path, ocr_work)))
                    consec_errors = 0
                except OcrError:
                    consec_errors += 1
                    kf_lines.append((t_rel, []))
                    if consec_errors >= cfg.max_consecutive_ocr_errors:
                        self.available = False
                        self.unavailable_reason = "repeated OCR failures (engine degraded)"
                        logger.warning("game vision unavailable: repeated OCR failures")
                        return unavailable_report(
                            offset, length, cfg.sample_fps, self.unavailable_reason,
                            engine=self._engine.name)
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)  # ALWAYS cleaned

        # 4) analysis — pure functions over measured lines (deterministic)
        stable_elements = detect_stable_elements(kf_lines, cfg)
        kf_events = analyze_kill_feed(kf_lines, cfg, stable_keys(stable_elements))
        sb_obs = detect_scoreboard(full_lines, cfg) if full_frames else []
        score_changes = track_score_changes(sb_obs, cfg)
        candidates = interpret_gameplay(kf_events, sb_obs, score_changes, cfg)
        diffs = measure_frame_diffs(kf_frames)

        return GameVisionChunkReport(
            offset=offset, duration=length, available=True, unavailable_reason=None,
            sample_fps=cfg.sample_fps, engine=self._engine.name,
            engine_version=self._engine.version,
            frames_analyzed=len(kf_lines), ocr_operations=ops,
            budget_exceeded=budget, frames_remaining=remaining,
            processing_seconds=time.monotonic() - t0,
            kill_feed_events=kf_events, stable_elements=stable_elements,
            scoreboard_observations=sb_obs, score_changes=score_changes,
            frame_diffs=diffs, candidates=candidates,
        )

    async def close(self) -> None:
        await self._engine.close()


def build_game_vision_analyzer(settings: Any) -> GameVisionAnalyzer:
    """Settings → analyzer. NEVER raises: disabled/misconfigured → Null.

    Follows the existing settings conventions (``WORKER_`` prefix,
    validated fields on ``Settings``) — same discipline as TASK 008.
    """
    if not getattr(settings, "game_vision_enabled", False):
        return NullGameVisionAnalyzer("game vision disabled (WORKER_GAME_VISION_ENABLED=false)")
    roi_raw = getattr(settings, "game_vision_kill_feed_roi", None)
    roi: tuple[float, float, float, float] | None = None
    if roi_raw:
        try:
            parts = [float(p) for p in str(roi_raw).split(",")]
            if len(parts) == 4:
                roi = (parts[0], parts[1], parts[2], parts[3])
        except ValueError:
            return NullGameVisionAnalyzer(
                "game vision misconfigured: WORKER_GAME_VISION_KILL_FEED_ROI must be "
                "'x,y,w,h' normalized values")
    try:
        config = GameVisionConfig(
            sample_fps=float(settings.game_vision_sample_fps),
            max_frames_per_chunk=int(settings.game_vision_max_frames_per_chunk),
            frame_timeout_seconds=float(settings.game_vision_frame_timeout_seconds),
            ocr_timeout_seconds=float(settings.game_vision_ocr_timeout_seconds),
            max_ocr_operations_per_chunk=int(settings.game_vision_max_ocr_operations_per_chunk),
            max_chunk_seconds=float(settings.game_vision_max_chunk_seconds),
            min_ocr_confidence=float(settings.game_vision_min_ocr_confidence),
            ocr_language=str(settings.game_vision_ocr_language or "eng"),
            tessdata_dir=str(settings.game_vision_tessdata_dir)
            if getattr(settings, "game_vision_tessdata_dir", None) else None,
            tesseract_binary=str(settings.game_vision_tesseract_binary)
            if getattr(settings, "game_vision_tesseract_binary", None) else None,
            kill_feed_roi=roi,
        )
    except ValueError as exc:
        return NullGameVisionAnalyzer(f"game vision misconfigured: {exc}")
    return TesseractGameVisionAnalyzer(config, TesseractOcrEngine(config))


def summarize_game_vision(chunk_summaries: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregates per-chunk summaries (this run + resumed runs) into the
    job-level ``PipelineResult.game_vision`` metadata."""
    available = [s for s in chunk_summaries if s.get("available")]
    if not available:
        reason = next((s.get("unavailable_reason") for s in chunk_summaries
                       if s.get("unavailable_reason")), "game vision not run")
        return {"available": False, "reason": reason, "chunks_analyzed": 0}
    return {
        "available": True,
        "engine": available[0].get("engine"),
        "engine_version": available[0].get("engine_version"),
        "chunks_analyzed": len(available),
        "frames_analyzed": sum(int(s.get("frames_analyzed", 0)) for s in available),
        "ocr_operations": sum(int(s.get("ocr_operations", 0)) for s in available),
        "kill_feed_events": sum(int(s.get("kill_feed_events", 0)) for s in available),
        "scoreboard_visible_frames": sum(int(s.get("scoreboard_visible_frames", 0)) for s in available),
        "scoreboard_scanned_frames": sum(int(s.get("scoreboard_scanned_frames", 0)) for s in available),
        "score_changes": sum(int(s.get("score_changes", 0)) for s in available),
        "budget_exceeded_chunks": sum(1 for s in available if s.get("budget_exceeded")),
        "processing_seconds": round(sum(float(s.get("processing_seconds", 0.0)) for s in available), 1),
        "note": ("game vision evidence is OCR-observed visual content; "
                 "interpretation is heuristic, not verified game state"),
    }
