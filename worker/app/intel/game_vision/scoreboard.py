"""Scoreboard analysis (TASK 009) — visibility + observed values.

Two conservative, clearly-separated capabilities:

1. **Visibility (HEURISTIC)** — a scanned frame is "scoreboard likely
   visible" when its OCR line count is a large multiple of the chunk's
   MEDIAN line count AND it contains at least N lines AND M numeric
   tokens. The heuristic is deliberately strict (a scoreboard is one of
   the text-densest things on a gaming stream) and the flag is always
   labeled ``heuristic`` in evidence.

2. **Values (OBSERVED)** — where OCR actually read ``label value``
   adjacencies (the last numeric token of a line, preceded by a label
   token), values are tracked across visible observations. A change is
   reported ONLY when both the before and after values were OCR-observed
   — unreadable/ambiguous values never produce a change event
   (no fabrication, per spec).
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from .config import GameVisionConfig
from .ocr import OcrLine

_NUM = re.compile(r"^\d+(?:[.,]\d+)?$")


def numeric_tokens(lines: list[OcrLine]) -> int:
    """Counts whole-word numeric tokens across lines (observed only)."""
    count = 0
    for line in lines:
        for word in line.text.split():
            if _NUM.match(word.strip(",.;:")):
                count += 1
    return count


def _label_value_pairs(line: OcrLine) -> list[tuple[str, str]]:
    """Conservative ``label value`` extraction: the last numeric token of
    the line, with the last non-numeric token immediately before it as
    the label. No positional magic — just what OCR read in reading order.
    """
    words = line.text.split()
    pairs: list[tuple[str, str]] = []
    for i in range(1, len(words)):
        value = words[i].strip(",.;:")
        if _NUM.match(value):
            label = words[i - 1].strip(",.;:").casefold()
            if label and not _NUM.match(label):
                pairs.append((label, value))
    return pairs


@dataclass
class ScoreboardObservation:
    """One scanned frame's measured text density + visibility flag."""

    t_rel: float
    line_count: int
    numeric_count: int
    visible: bool                  # HEURISTIC flag (documented in evidence)
    density_vs_median: float
    lines: list[OcrLine] = field(default_factory=list)  # capped evidence


def detect_scoreboard(
    scanned_frames: list[tuple[float, list[OcrLine]]],
    cfg: GameVisionConfig,
) -> list[ScoreboardObservation]:
    """Runs the visibility heuristic over the (sub-sampled) full-frame OCR
    observations. Deterministic: median baseline over all scanned frames.
    """
    if not scanned_frames:
        return []
    counts = [len(lines) for _t, lines in scanned_frames]
    median = statistics.median(counts) or 0.0
    floor = max(median * cfg.scoreboard_density_ratio, 1.0)

    observations: list[ScoreboardObservation] = []
    for t_rel, lines in scanned_frames:
        n_lines = len(lines)
        n_num = numeric_tokens(lines)
        visible = (
            n_lines >= cfg.scoreboard_min_lines
            and n_lines >= floor
            and n_num >= cfg.scoreboard_min_numeric_tokens
        )
        observations.append(ScoreboardObservation(
            t_rel=t_rel,
            line_count=n_lines,
            numeric_count=n_num,
            visible=visible,
            density_vs_median=round(n_lines / median, 3) if median else float(n_lines),
            lines=lines[: cfg.scoreboard_max_kept_lines],
        ))
    return observations


@dataclass
class ScoreValueChange:
    """A value that changed between two OCR-observed scoreboard frames."""

    t_rel: float                    # time of the AFTER reading (chunk-relative)
    label: str
    before: str
    after: str
    confidence: float               # min OCR confidence of the two readings


def track_score_changes(
    observations: list[ScoreboardObservation], cfg: GameVisionConfig
) -> list[ScoreValueChange]:
    """Tracks ``label value`` pairs across VISIBLE observations.

    Only consecutive visible frames are compared (a scoreboard shown at
    t1 and t3 with nothing between is one change, measured t1 -> t3).
    Missing/unreadable label in either frame => no change reported.
    """
    visible = [o for o in observations if o.visible]
    changes: list[ScoreValueChange] = []
    prev: dict[str, tuple[str, float]] = {}
    for obs in visible:
        cur: dict[str, tuple[str, float]] = {}
        for line in obs.lines:
            for label, value in _label_value_pairs(line):
                # First occurrence of a label in a frame wins (deterministic).
                if label not in cur:
                    cur[label] = (value, line.confidence)
        for label, (value, conf) in cur.items():
            if label in prev:
                before_value, before_conf = prev[label]
                if _values_differ(before_value, value):
                    changes.append(ScoreValueChange(
                        t_rel=obs.t_rel, label=label, before=before_value,
                        after=value, confidence=round(min(conf, before_conf), 2),
                    ))
        prev = cur
    return changes


def _values_differ(a: str, b: str) -> bool:
    try:
        return float(a.replace(",", ".")) != float(b.replace(",", "."))
    except ValueError:
        return a != b
