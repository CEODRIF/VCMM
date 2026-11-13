"""HUD analysis (TASK 009) — stable interface elements, game-agnostic.

Two independent, measurable capabilities:

1. **Stable text elements** — OCR lines that appear in a substantial
   fraction of a chunk's frames (matched by their dedupe key) are stable
   interface text (scores, names, labels). This is what distinguishes
   *persistent HUD text* from *transient kill-feed text*. No game-specific
   assumptions: purely text+time persistence. These elements are excluded
   from kill-feed events and reported as measured region-stability
   evidence (bbox center + persistence ratio).

2. **Region change metric** — frame-to-frame mean absolute grayscale
   difference on a small fixed grid (64x36). Measures how much the region
   is VISUALLY changing between consecutive samples (a stable HUD scores
   low, an active gameplay region scores high). It is a sub-metric: when
   Pillow is not installed it is honestly skipped (``None``), never
   fabricated. Used only as evidence — never as the sole basis of an
   event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import GameVisionConfig
from .ocr import OcrLine

#: Fixed downscale grid for the change metric (deterministic, tiny).
DIFF_GRID = (64, 36)


@dataclass(frozen=True)
class HudElement:
    """A stable (persistent) text element measured in a chunk."""

    key: str                      # dedupe key (cross-frame identity)
    text: str                     # first-seen conservative text (evidence)
    frames_seen: int
    total_frames: int
    persistence: float            # frames_seen / total_frames
    mean_confidence: float        # mean OCR confidence (0..100)
    bbox_center: tuple[float, float]  # mean center (px, in the sampled ROI)
    first_seen: float             # chunk-relative seconds
    last_seen: float


@dataclass
class FrameDiff:
    """Change metric between two consecutive sampled frames."""

    t_rel: float                  # time of the LATER frame (chunk-relative)
    change_ratio: float | None    # 0..1; None when Pillow is unavailable


def detect_stable_elements(
    frame_lines: list[tuple[float, list[OcrLine]]], cfg: GameVisionConfig
) -> list[HudElement]:
    """Returns the elements present in >= ``persistent_frame_ratio`` of
    the chunk's frames, most persistent first. Deterministic ordering."""
    total = len(frame_lines)
    if total == 0:
        return []

    groups: dict[str, dict] = {}
    for t_rel, lines in frame_lines:
        seen_keys: set[str] = set()
        for line in lines:
            if line.key in seen_keys:
                continue  # one count per frame per key
            seen_keys.add(line.key)
            g = groups.setdefault(line.key, {
                "text": line.text, "confs": [], "centers": [],
                "first": t_rel, "last": t_rel, "frames": 0,
            })
            g["frames"] += 1
            g["confs"].append(line.confidence)
            g["centers"].append((line.bbox[0] + line.bbox[2] / 2.0,
                                 line.bbox[1] + line.bbox[3] / 2.0))
            g["first"] = min(g["first"], t_rel)
            g["last"] = max(g["last"], t_rel)

    elements: list[HudElement] = []
    for key, g in groups.items():
        persistence = g["frames"] / total
        if persistence < cfg.persistent_frame_ratio:
            continue
        cx = sum(c[0] for c in g["centers"]) / len(g["centers"])
        cy = sum(c[1] for c in g["centers"]) / len(g["centers"])
        elements.append(HudElement(
            key=key, text=g["text"], frames_seen=g["frames"], total_frames=total,
            persistence=round(persistence, 4),
            mean_confidence=round(sum(g["confs"]) / len(g["confs"]), 2),
            bbox_center=(round(cx, 1), round(cy, 1)),
            first_seen=g["first"], last_seen=g["last"],
        ))
    elements.sort(key=lambda e: (-e.persistence, e.key))
    return elements


def stable_keys(elements: list[HudElement]) -> set[str]:
    return {e.key for e in elements}


def frame_change_ratio(prev_path: Path, curr_path: Path) -> float | None:
    """Mean absolute grayscale difference (0..1) on the fixed grid.

    Returns ``None`` (honest skip) when Pillow is unavailable or either
    image cannot be read — the metric is auxiliary evidence, so a skip
    must never look like a measurement.
    """
    try:
        from PIL import Image  # optional (in requirements-game-vision.txt)
    except ImportError:
        return None
    try:
        with Image.open(prev_path) as a, Image.open(curr_path) as b:
            pa = a.convert("L").resize(DIFF_GRID)
            pb = b.convert("L").resize(DIFF_GRID)
        da = pa.getdata()
        db = pb.getdata()
        total = sum(abs(x - y) for x, y in zip(da, db))
        return round(total / (len(da) * 255.0), 4)
    except (OSError, ValueError):
        return None


def measure_frame_diffs(frames: list[tuple[float, Path]]) -> list[FrameDiff]:
    """Consecutive-pair change metric over an ordered frame list.

    The first frame has no predecessor -> ratio None (documented, not a
    missing measurement error).
    """
    out: list[FrameDiff] = []
    prev: Path | None = None
    for t_rel, path in frames:
        out.append(FrameDiff(t_rel=t_rel,
                             change_ratio=(frame_change_ratio(prev, path)
                                           if prev is not None else None)))
        prev = path
    return out
