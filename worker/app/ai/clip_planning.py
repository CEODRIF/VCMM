"""TASK 015 P3 — clip generation planning (pure, no I/O).

Two INDEPENDENT generation modes:

MODE A — full coverage (:func:`plan_full_coverage`)
    The ENTIRE source is tiled into sequential clips of the user-selected
    duration: ``count = ceil(duration / clip_duration)`` with the final
    remainder handled by the documented policy (kept as its own clip when
    >= ``MIN_REMAINDER_SECONDS``, otherwise appended to the previous clip
    — the source end is covered, NO footage is ever invented to pad the
    final clip, and nothing is skipped: ordinary / uninteresting sections
    are rendered exactly like the rest). Exact tiling: no gaps, no
    overlaps, chronological order, verified by :func:`verify_clip_tiling`.

MODE B — viral highlights (:func:`rank_highlights`)
    Independent candidate clips ranked by a TRANSPARENT composite score
    built ONLY from measured facts on the P2 timeline (never a virality
    promise): ``100 * (0.5*importance + 0.3*confidence + 0.2*candidate)``.
    Candidates expand over ADJACENT windows to preserve event context
    (beginning/ending not cut off), near-duplicates are merged (IoU), and
    ``max_clips`` (the user's requested_clips) is a hard upper bound.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

#: A remainder shorter than this is appended to the previous clip instead
#: of becoming its own (uselessly short) reel. 10s is the documented
#: product policy (also the floor of the relaxed clips.duration check).
MIN_REMAINDER_SECONDS = 10.0

#: A candidate timeline window: semantic importance >= this (from a
#: completed Gemini assessment) qualifies it for Mode B expansion.
IMPORTANCE_CANDIDATE = 0.5

#: Measured local-signal confidence >= this qualifies a window even
#: without a Gemini assessment (Mode B works without Gemini).
CONFIDENCE_CANDIDATE = 0.6

#: Windows overlapping above this IoU are the SAME moment (dedup).
DUPLICATE_IOU = 0.3


@dataclass(frozen=True)
class ClipSpan:
    """One planned clip window in ABSOLUTE source seconds."""

    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def _iou(a: ClipSpan, b: ClipSpan) -> float:
    inter = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    if inter <= 0:
        return 0.0
    shorter = min(a.duration, b.duration)
    return inter / shorter if shorter > 0 else 0.0


def spans_overlap_ratio(
    a_start: float, a_end: float, b_start: float, b_end: float,
) -> float:
    """Overlap of two absolute windows relative to the SHORTER window
    (0 = disjoint, 1 = the shorter is contained in the other). Public
    for the analysis stage's cross-source candidate dedup."""
    return _iou(ClipSpan(a_start, a_end), ClipSpan(b_start, b_end))


def verify_clip_tiling(spans: Sequence[ClipSpan], duration_seconds: float,
                       eps: float = 0.05) -> None:
    """Raises ValueError unless ``spans`` exactly tile [0, duration]
    (chronological, no gaps, no overlaps) — the Mode A invariant."""
    if not spans:
        raise ValueError("full coverage requires at least one clip")
    if abs(spans[0].start) > eps:
        raise ValueError(f"coverage starts at {spans[0].start}, expected 0")
    for prev, cur in zip(spans, spans[1:]):
        if cur.start < prev.end - eps:
            raise ValueError(f"clips {prev} and {cur} overlap")
        if cur.start > prev.end + eps:
            raise ValueError(f"gap between {prev.end} and {cur.start}")
    if spans[-1].end < duration_seconds - eps:
        raise ValueError(f"coverage ends at {spans[-1].end}, source is {duration_seconds}")
    if spans[-1].end > duration_seconds + eps:
        raise ValueError(f"coverage extends to {spans[-1].end} past source end")


def expected_clip_count(duration_seconds: float, clip_duration: float) -> int:
    """ceil(duration / clip_duration) — the MODE A contract (the explicit
    remainder policy can only LOWER the count by one, via the merge)."""
    if duration_seconds <= 0 or clip_duration <= 0:
        raise ValueError("durations must be > 0")
    return int(-(-int(round(duration_seconds)) // int(round(clip_duration)))) if \
        duration_seconds >= clip_duration else 1


def plan_full_coverage(duration_seconds: float, clip_duration: float) -> list[ClipSpan]:
    """MODE A: sequential clips covering the whole source exactly once.

    - source shorter than one clip duration -> ONE clip of the source
      duration (the resulting output duration equals the source duration;
      nothing is padded);
    - otherwise full clips of ``clip_duration`` + the final remainder,
      which becomes its own clip when >= MIN_REMAINDER_SECONDS, else is
      appended to the previous clip.
    """
    if duration_seconds <= 0 or clip_duration <= 0:
        raise ValueError("durations must be > 0")

    if duration_seconds <= clip_duration:
        return [ClipSpan(0.0, duration_seconds)]

    full_count = int(duration_seconds // clip_duration)
    remainder = duration_seconds - full_count * clip_duration

    spans: list[ClipSpan] = []
    cursor = 0.0
    for _ in range(full_count):
        spans.append(ClipSpan(cursor, cursor + clip_duration))
        cursor += clip_duration
    if remainder >= MIN_REMAINDER_SECONDS:
        spans.append(ClipSpan(cursor, duration_seconds))
    else:
        # Merge the short remainder into the previous clip (no padding).
        spans[-1] = ClipSpan(spans[-1].start, duration_seconds)
    verify_clip_tiling(spans, duration_seconds)
    return spans


# --------------------------------------------------------------------------
# MODE B — transparent ranking over the P2 timeline
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class HighlightCandidate:
    start: float
    end: float
    score: float  # 0-100, transparent composite
    importance: float | None  # max window importance inside (None = unassessed)
    confidence: float | None  # max measured signal strength inside
    window_count: int
    candidate_windows: int  # windows flagged candidate by the assessment

    @property
    def duration(self) -> float:
        return self.end - self.start


def _window_value(row: dict[str, Any]) -> tuple[float | None, float | None, bool]:
    """(importance, confidence, candidate_flag) from a timeline row dict.
    Unknowns stay unknown (None) — never guessed."""
    assessment = row.get("gemini_assessment") or {}
    status = assessment.get("status") if isinstance(assessment, dict) else None
    importance = row.get("event_importance")
    importance = float(importance) if isinstance(importance, (int, float)) else None
    if status != "ok":
        importance = None  # only a COMPLETED assessment may assert semantics
    confidence = row.get("confidence")
    confidence = float(confidence) if isinstance(confidence, (int, float)) else None
    candidate = status == "ok" and bool(assessment.get("candidate_clip"))
    return importance, confidence, candidate


def rank_highlights(timeline_rows: Sequence[dict[str, Any]],
                    clip_duration: float,
                    max_clips: int,
                    duration_seconds: float | None = None,
                    ) -> list[HighlightCandidate]:
    """MODE B: ranks candidate moments from the unified timeline.

    Transparent score per candidate:
        score = 100 * (0.5 * max_importance + 0.3 * max_confidence
                       + 0.2 * candidate_window_fraction)
    with missing (unassessed / unmeasured) components contributing 0 —
    the score NEVER claims what was not measured. Candidates are groups of
    adjacent windows: a qualifying window expands to its adjacent windows
    while those still carry signal (context preservation — the beginning
    and end of an event are not cut off). Near-duplicates (IoU >
    DUPLICATE_IOU) keep the higher-scoring group. ``max_clips`` is a hard
    upper bound (requested_clips semantics).

    Returns [] when no window qualifies (the caller falls back to the
    existing detector — Mode B never invents highlights).
    """
    if not timeline_rows or clip_duration <= 0 or max_clips <= 0:
        return []
    rows = sorted(timeline_rows, key=lambda r: (r["start_time"], r["window_index"]))
    n = len(rows)

    def qualifies(i: int) -> bool:
        importance, confidence, candidate = _window_value(rows[i])
        return (importance is not None and importance >= IMPORTANCE_CANDIDATE) \
            or (confidence is not None and confidence >= CONFIDENCE_CANDIDATE) \
            or candidate

    def _connected(a: int, b: int) -> bool:
        """Adjacent context: window b directly next to a qualifying seed
        with a transcript or measured signal (event continuation)."""
        if abs(rows[a]["window_index"] - rows[b]["window_index"]) != 1:
            return False
        _, confidence, _ = _window_value(rows[a])
        return bool(rows[a].get("transcript")) or (confidence is not None and confidence > 0)

    # Seed = every qualifying window; expand to adjacent qualifying or
    # semantically-connected windows (one step of context each side).
    seeds = [i for i in range(n) if qualifies(i)]
    groups: list[list[int]] = []
    used: set[int] = set()
    for seed in seeds:
        if seed in used:
            continue
        group = [seed]
        left = seed - 1
        while left >= 0 and (qualifies(left) or _connected(left, seed)):
            group.insert(0, left)
            left -= 1
            if seed - left > 3:  # bounded context (never swallows the source)
                break
        right = seed + 1
        while right < n and (qualifies(right) or _connected(right, seed)):
            group.append(right)
            right += 1
            if right - seed > 3:
                break
        used.update(group)
        groups.append(group)
    if not groups:
        return []

    candidates: list[HighlightCandidate] = []
    for group in groups:
        spans = [ClipSpan(float(rows[i]["start_time"]), float(rows[i]["end_time"])) for i in group]
        merged = ClipSpan(min(s.start for s in spans), max(s.end for s in spans))
        importances = [_window_value(rows[i])[0] for i in group]
        confidences = [_window_value(rows[i])[1] for i in group]
        flags = [_window_value(rows[i])[2] for i in group]
        imp = max((v for v in importances if v is not None), default=None)
        conf = max((v for v in confidences if v is not None), default=None)
        cand_frac = sum(1 for f in flags if f) / len(flags)
        score = 100.0 * (0.5 * (imp or 0.0) + 0.3 * (conf or 0.0) + 0.2 * cand_frac)
        candidates.append(HighlightCandidate(
            start=merged.start, end=merged.end, score=round(score, 2),
            importance=imp, confidence=conf,
            window_count=len(group), candidate_windows=sum(1 for f in flags if f),
        ))

    # Duration shaping: candidates far outside the user's clip band are
    # kept (context wins over an exact length — the renderer produces the
    # real duration), but absurdly long groups are capped at 3x the band.
    cap = clip_duration * 3
    candidates = [
        HighlightCandidate(min(c.start, c.end - cap), min(c.end, c.start + cap),
                           c.score, c.importance, c.confidence,
                           c.window_count, c.candidate_windows)
        if c.duration > cap else c
        for c in candidates
    ]
    if duration_seconds is not None:
        candidates = [
            HighlightCandidate(max(0.0, c.start), min(c.end, duration_seconds),
                               c.score, c.importance, c.confidence,
                               c.window_count, c.candidate_windows)
            for c in candidates
        ]

    # Dedup near-duplicates (keep the higher score), then rank + bound.
    candidates.sort(key=lambda c: (-c.score, c.start))
    kept: list[HighlightCandidate] = []
    for cand in candidates:
        if all(_iou(ClipSpan(cand.start, cand.end), ClipSpan(k.start, k.end)) <= DUPLICATE_IOU
               for k in kept):
            kept.append(cand)
    kept.sort(key=lambda c: (-c.score, c.start))
    return kept[:max_clips]
