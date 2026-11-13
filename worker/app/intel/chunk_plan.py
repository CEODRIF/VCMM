"""Deterministic chunk planning for long recordings (TASK 007).

Pure functions — no I/O — so the plan for a recording is ALWAYS the same
for the same (duration, chunk_seconds, overlap_seconds), which is what
makes resume cheap: the plan is recomputed, the completed chunks are
loaded from the checkpoint table, and only the remainder is processed.

There is NO upper bound on ``total_duration`` by design: a 100-hour VOD
yields a plan of ~600 spans (integers only). The per-claim EXECUTION
budget (how many chunks one run processes) is a separate operational
control enforced by the pipeline, not by the planner.

Boundary ownership (same midpoint rule as the proven transcription
chunking, ``app/transcription/chunking.py``): consecutive chunks overlap
by ``overlap_seconds``; a signal at absolute time ``t`` is OWNED by chunk
``i`` when

    lower_i  = 0                        (i == 0)
             = start_i + overlap / 2    (i > 0)
    upper_i  = start_{i+1} + overlap / 2  (i < last)
             = +inf                       (last)

Because every chunk re-decodes the overlap region, a signal straddling a
chunk boundary is detected by both neighbours but persisted by exactly
one owner — never lost, never duplicated.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChunkSpan:
    """One bounded decode/analysis range of the full recording."""

    index: int
    start: float  # absolute seconds in the source
    end: float    # absolute seconds in the source

    @property
    def duration(self) -> float:
        return self.end - self.start


def plan_chunks(total_duration: float, chunk_seconds: int, overlap_seconds: int) -> list[ChunkSpan]:
    """Plans overlapping chunks covering ``[0, total_duration)``.

    - short recordings (<= chunk_seconds) are a SINGLE span (no re-encode);
    - the tail never produces a tiny redundant final span (same rule as
      the transcription splitter);
    - spans are contiguous-by-overlap: ``start_{i+1} = start_i + (chunk - overlap)``.
    """
    if not total_duration > 0:
        raise ValueError("total_duration must be positive")
    if chunk_seconds < 1:
        raise ValueError("chunk_seconds must be >= 1")
    if overlap_seconds < 0 or overlap_seconds >= chunk_seconds:
        raise ValueError("overlap_seconds must satisfy 0 <= overlap < chunk")

    if total_duration <= chunk_seconds:
        return [ChunkSpan(0, 0.0, float(total_duration))]

    step = chunk_seconds - overlap_seconds
    spans: list[ChunkSpan] = []
    index = 0
    offset = 0.0
    while offset < total_duration - 0.5:
        # Stop as soon as the previous span already reaches the tail.
        if spans and spans[-1].end >= total_duration - 0.5:
            break
        length = min(float(chunk_seconds), total_duration - offset)
        if length <= 0:
            break
        spans.append(ChunkSpan(index, offset, offset + length))
        index += 1
        offset += step
        if offset >= total_duration:
            break
    if not spans:
        raise ValueError("chunk plan produced no spans")
    # Invariant: full coverage of [0, duration).
    if spans[-1].end < total_duration - 0.5:
        raise ValueError("chunk plan does not cover the recording tail")
    return spans


def ownership_bounds(spans: list[ChunkSpan], overlap_seconds: int) -> list[tuple[float, float]]:
    """Per-span ownership intervals ``[lower, upper)`` on the ABSOLUTE timeline.

    The midpoint rule keeps a boundary-straddling signal exactly once.
    For a single span the whole timeline (minus a hair at the end) is owned.
    """
    if not spans:
        return []
    bounds: list[tuple[float, float]] = []
    for i, span in enumerate(spans):
        lower = 0.0 if i == 0 else span.start + overlap_seconds / 2.0
        upper = spans[i + 1].start + overlap_seconds / 2.0 if i + 1 < len(spans) else float("inf")
        bounds.append((lower, upper))
    return bounds


def owning_span_index(t: float, bounds: list[tuple[float, float]]) -> int | None:
    """Index of the span that owns absolute time ``t`` (None when out of plan)."""
    for i, (lower, upper) in enumerate(bounds):
        if lower <= t < upper:
            return i
    return None
