"""Kill-feed analysis (TASK 009) — transient text in a region.

A kill-feed entry is text that APPEARS in the region, persists for a
short while (fades out), and is NOT one of the chunk's stable HUD
elements. The algorithm works on a time-ordered series of per-frame OCR
line lists and is deliberately conservative:

- new event  = a line key that was not active and was not seen within
  ``reappear_suppress_seconds`` of a previous disappearance (fade /
  jitter does not re-trigger);
- persistent text (stable HUD elements, computed by ``hud``) is excluded
  entirely;
- each event carries its measured first/last seen times, frames count,
  mean OCR confidence and the raw text — no interpretation here
  (interpretation lives in ``events.py``);
- the SAME physical line can never be counted twice in one chunk: a
  fade-out jitter re-opening an event RE-ATTACHES to the original
  appearance time (the tentatively-closed record is withdrawn, not
  duplicated).

Timestamps are CHUNK-RELATIVE; the signals layer applies the chunk
offset exactly once.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import GameVisionConfig
from .ocr import OcrLine


@dataclass
class KillFeedEvent:
    """One measured transient text event in the region."""

    text: str                       # last readable OCR form (evidence)
    key: str                        # dedupe key (identity across frames)
    appeared_at: float              # chunk-relative seconds (first seen)
    disappeared_at: float | None    # chunk-relative seconds (gone by this sample)
    frames_seen: int
    mean_confidence: float          # 0..100
    bboxes: list[tuple[int, int, int, int]] = field(default_factory=list)


@dataclass
class _ActiveLine:
    text: str
    key: str
    confs: list[float]
    bboxes: list[tuple[int, int, int, int]]
    first: float
    last: float
    frames: int
    gone_by: float | None = None  # set when closed


def analyze_kill_feed(
    frame_lines: list[tuple[float, list[OcrLine]]],
    cfg: GameVisionConfig,
    stable: set[str],
) -> list[KillFeedEvent]:
    """Detects transient kill-feed events in a time-ordered series.

    ``frame_lines``: ``[(t_rel, lines), ...]``; ``stable``: dedupe keys of
    the chunk's stable HUD elements (excluded). Returns events in
    ``appeared_at`` order.
    """
    active: dict[str, _ActiveLine] = {}
    #: Most recent CLOSED-but-NOT-FINALIZED instance per key. A suppressed
    #: re-appearance MERGES into it (same physical line, original time); a
    #: re-appearance beyond the window FINALIZES it as a distinct event.
    tentative: dict[str, _ActiveLine] = {}
    emitted: list[KillFeedEvent] = []

    def _sight(a: _ActiveLine, line: OcrLine, t_rel: float) -> None:
        a.text = line.text          # keep the freshest readable form
        a.last = t_rel
        a.frames += 1
        a.confs.append(line.confidence)
        a.bboxes.append(line.bbox)

    for t_rel, lines in frame_lines:
        present = {l.key: l for l in lines}

        # Close active lines absent from this frame (they faded out).
        for key in list(active):
            if key not in present:
                a = active.pop(key)
                a.gone_by = t_rel
                tentative[key] = a

        for line in lines:
            if line.key in stable:
                continue
            if not (cfg.kill_feed_min_chars <= len(line.text)
                    <= cfg.kill_feed_max_chars):
                continue
            if line.key in active:
                _sight(active[line.key], line, t_rel)
            elif line.key in tentative:
                gap = t_rel - tentative[line.key].gone_by
                if gap <= cfg.reappear_suppress_seconds:
                    # Fade jitter of the SAME physical event: merge.
                    a = tentative.pop(line.key)
                    a.gone_by = None
                    active[line.key] = a
                    _sight(a, line, t_rel)
                else:
                    # A DISTINCT event with the same text: finalize the old.
                    emitted.append(_to_event(tentative.pop(line.key)))
                    active[line.key] = _ActiveLine(
                        text=line.text, key=line.key, confs=[line.confidence],
                        bboxes=[line.bbox], first=t_rel, last=t_rel, frames=1,
                    )
            else:
                active[line.key] = _ActiveLine(
                    text=line.text, key=line.key, confs=[line.confidence],
                    bboxes=[line.bbox], first=t_rel, last=t_rel, frames=1,
                )

    # Chunk end: finalize everything still open (active or tentative).
    for a in active.values():
        a.gone_by = None
        emitted.append(_to_event(a))
    for a in tentative.values():
        emitted.append(_to_event(a))

    emitted.sort(key=lambda e: (e.appeared_at, e.key))
    return emitted


def _to_event(a: _ActiveLine) -> KillFeedEvent:
    return KillFeedEvent(
        text=a.text, key=a.key, appeared_at=a.first, disappeared_at=a.gone_by,
        frames_seen=a.frames, mean_confidence=round(sum(a.confs) / len(a.confs), 2),
        bboxes=list(a.bboxes),
    )
