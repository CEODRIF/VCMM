"""Game Vision configuration (TASK 009).

Frozen, validated per-job configuration for the game-vision modality.
The pipeline treats game vision as an OPTIONAL signal family (exactly
like facecam, TASK 008): a ``None`` config (or an unavailable analyzer)
means the rest of the pipeline behaves exactly as before.

Resource bounds are explicit (measured and enforced, not aspirational):
- ``sample_fps`` — sampled frames per second of source (the VOD is NEVER
  decoded whole; only a bounded set of small JPEGs per chunk);
- ``max_frames_per_chunk`` — hard cap on sampled frames per chunk;
- ``max_ocr_operations_per_chunk`` — hard cap on OCR subprocess runs;
- ``max_chunk_seconds`` — wall-clock budget for the whole per-chunk game
  vision pass (checked BETWEEN operations; the in-flight FFmpeg/OCR
  subprocess is the only thing that can exceed it, by at most its own
  timeout);
- ``frame_timeout_seconds`` / ``ocr_timeout_seconds`` — per-subprocess
  budgets; the subprocess is KILLED after each.

Regions of interest are normalized (x, y, w, h in [0, 1]) and fully
configurable — nothing here assumes one specific game's layout. A
``None`` ROI means "full frame".
"""

from __future__ import annotations

from dataclasses import dataclass

#: Default kill-feed ROI: the top-right strip, the most common
#: convention. It is a DEFAULT, not an assumption — override per job, or
#: set to None to scan the full frame (slower, more robust to layout).
DEFAULT_KILL_FEED_ROI: tuple[float, float, float, float] = (0.80, 0.02, 0.18, 0.12)

#: Generic, game-agnostic full-line tokens interpreted as "match result"
#: text. Matched EXACTLY (case-folded, normalized) against a whole OCR
#: line — never substring — to keep false positives low. The
#: INTERPRETATION ("match ended") is heuristic even when the TEXT was
#: directly observed.
MATCH_RESULT_TOKENS: frozenset[str] = frozenset({
    "victory", "defeat", "win", "loss", "gg", "gg wp", "surrender",
    "forfeit", "game over",
})


@dataclass(frozen=True)
class GameVisionConfig:
    # -- sampling / resource limits -------------------------------------
    sample_fps: float = 1.0
    max_frames_per_chunk: int = 1200
    frame_timeout_seconds: float = 120.0
    ocr_timeout_seconds: float = 30.0
    max_ocr_operations_per_chunk: int = 800
    max_chunk_seconds: float = 240.0
    #: Longest image side (px) after the FFmpeg scale filter — keeps OCR
    #: cost bounded on high-resolution sources.
    max_image_dimension: int = 1280
    # -- OCR engine (explicit, validated; never downloaded at runtime) --
    min_ocr_confidence: float = 60.0
    ocr_language: str = "eng"
    #: Explicit tessdata directory containing ``<language>.traineddata``;
    #: None = trust the system default location.
    tessdata_dir: str | None = None
    #: Explicit tesseract binary path; None = resolve from PATH.
    tesseract_binary: str | None = None
    #: Consecutive per-frame OCR errors before the engine is marked
    #: unavailable for the whole job (honest sticky degradation).
    max_consecutive_ocr_errors: int = 10
    # -- regions of interest (normalized, game-agnostic) -----------------
    kill_feed_roi: tuple[float, float, float, float] | None = DEFAULT_KILL_FEED_ROI
    # -- scoreboard (full-frame, conservative heuristic + observed text) -
    #: OCR one full frame every N sampled frames (full-frame OCR is the
    #: expensive part — this is what keeps the op budget bounded).
    scoreboard_scan_interval: int = 5
    #: A frame is "scoreboard likely visible" when its OCR line count is
    #: at least this multiple of the chunk's MEDIAN line count...
    scoreboard_density_ratio: float = 3.0
    #: ...and at least this many lines...
    scoreboard_min_lines: int = 4
    #: ...and at least this many numeric tokens (scores are numbers).
    scoreboard_min_numeric_tokens: int = 4
    #: Cap on OCR lines kept per observation in the report (evidence
    #: stays bounded even on dense frames).
    scoreboard_max_kept_lines: int = 24
    # -- kill feed --------------------------------------------------------
    kill_feed_min_chars: int = 3
    kill_feed_max_chars: int = 64
    #: Lines present in at least this fraction of a chunk's frames are
    #: STABLE HUD text (scores, names) — excluded from kill-feed events.
    persistent_frame_ratio: float = 0.5
    #: A line re-appearing within this many seconds of its last
    #: disappearance is the SAME event (fade), not a new one.
    reappear_suppress_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not 0 < self.sample_fps <= 4:
            raise ValueError("sample_fps must be within (0, 4]")
        if not 0 < self.max_frames_per_chunk:
            raise ValueError("max_frames_per_chunk must be positive")
        if self.frame_timeout_seconds <= 0 or self.ocr_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        if self.max_ocr_operations_per_chunk < 2:
            raise ValueError("max_ocr_operations_per_chunk must be >= 2")
        if self.max_chunk_seconds <= 0:
            raise ValueError("max_chunk_seconds must be positive")
        if self.max_image_dimension < 64:
            raise ValueError("max_image_dimension must be >= 64")
        if not 0 <= self.min_ocr_confidence <= 100:
            raise ValueError("min_ocr_confidence must be within [0, 100]")
        if not self.ocr_language:
            raise ValueError("ocr_language must be non-empty")
        if self.scoreboard_scan_interval < 1:
            raise ValueError("scoreboard_scan_interval must be >= 1")
        if self.scoreboard_density_ratio <= 1:
            raise ValueError("scoreboard_density_ratio must be > 1")
        if self.scoreboard_min_lines < 1 or self.scoreboard_min_numeric_tokens < 1:
            raise ValueError("scoreboard minima must be >= 1")
        if self.kill_feed_min_chars < 1 or self.kill_feed_max_chars < self.kill_feed_min_chars:
            raise ValueError("kill_feed char bounds are invalid")
        if not 0 < self.persistent_frame_ratio <= 1:
            raise ValueError("persistent_frame_ratio must be within (0, 1]")
        if self.reappear_suppress_seconds < 0:
            raise ValueError("reappear_suppress_seconds must be >= 0")
        if self.kill_feed_roi is not None:
            x, y, w, h = self.kill_feed_roi
            if not all(0 <= v <= 1 for v in (x, y, w, h)) or w <= 0 or h <= 0:
                raise ValueError("kill_feed_roi must be normalized with positive size")
            if x + w > 1 + 1e-9 or y + h > 1 + 1e-9:
                raise ValueError("kill_feed_roi must lie within the frame")
