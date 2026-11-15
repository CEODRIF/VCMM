"""TASK 015 — full-timeline coverage engine (pure, no I/O).

The product requirement: the system must understand the ENTIRE source
timeline, not only selected moments. This module provides the
deterministic core:

- :func:`plan_windows` — sequential windows over ``[0, duration]`` with
  NO gaps and NO overlaps. Every source second belongs to exactly one
  window. The final window may be shorter (the explicit remainder).
  When the source would exceed ``max_windows`` (cost bound), the window
  size is widened — coverage is NEVER dropped.
- :func:`window_text` — the ACTUAL transcribed text overlapping a window
  (never invented; empty when nothing was transcribed there).
- :func:`window_evidence` — the MEASURED local signals (audio / visual)
  overlapping a window, grouped by family.
- :func:`build_timeline_rows` — the unified timeline: one row per window
  with ``start_time / end_time / event_type / transcript /
  scene_description / visual_evidence / audio_evidence /
  event_importance / candidate_clip / confidence`` plus the honest
  Gemini assessment state (``ok`` / ``failed`` / ``not_configured``).
- :func:`verify_coverage` — the explicit coverage check: raises
  :class:`TimelineCoverageError` when the rows do NOT exactly tile
  ``[0, duration]``. Agentic/semantic exploration is NEVER accepted as
  proof of coverage — this tile check is the proof.

Honesty rules: ``event_importance`` is only set from a completed Gemini
assessment (or stays None — never guessed); ``confidence`` is only
derived from MEASURED local signal strength; unprocessed windows are
recorded as such, never silently skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

#: Allowed unified event types (the Gemini prompt constrains to this set;
#: local-only rows use "unknown").
EVENT_TYPES = frozenset({
    "gameplay", "highlight", "combat", "victory", "defeat", "reaction",
    "story", "intro", "outro", "transition", "miscellaneous", "unknown",
})

#: Tolerance for floating-point tiling (the plan is exact; probe durations
#: are not).
_EPS = 0.05


class TimelineCoverageError(RuntimeError):
    """The rows do NOT exactly tile [0, duration] — a coverage invariant
    violation (never swallowed, never papered over)."""


@dataclass(frozen=True)
class TimelineWindow:
    """One sequential slice of the source timeline."""

    index: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class TimelineRow:
    """The unified timeline record for ONE window (mirrors the
    ``analysis_timeline`` table)."""

    window_index: int
    start_time: float
    end_time: float
    event_type: str = "unknown"
    transcript: str = ""
    scene_description: str | None = None
    visual_evidence: dict[str, Any] = field(default_factory=dict)
    audio_evidence: dict[str, Any] = field(default_factory=dict)
    event_importance: float | None = None
    candidate_clip: bool = False
    confidence: float | None = None
    gemini_assessment: dict[str, Any] | None = None

    def to_db_row(self, job_id: str) -> dict[str, Any]:
        """Column dict for the analysis_timeline upsert (no secrets by
        construction — evidence holds measured signal summaries only)."""
        return {
            "job_id": job_id,
            "window_index": int(self.window_index),
            "start_time": round(float(self.start_time), 3),
            "end_time": round(float(self.end_time), 3),
            "event_type": self.event_type,
            "transcript": self.transcript,
            "scene_description": self.scene_description,
            "visual_evidence": self.visual_evidence,
            "audio_evidence": self.audio_evidence,
            "event_importance": self.event_importance,
            "candidate_clip": bool(self.candidate_clip),
            "confidence": self.confidence,
            "gemini_assessment": self.gemini_assessment,
        }


# --------------------------------------------------------------------------
# Window planning (full coverage, no gaps, no overlaps)
# --------------------------------------------------------------------------

def plan_windows(
    duration_seconds: float,
    window_seconds: int,
    max_windows: int,
) -> list[TimelineWindow]:
    """Sequential windows tiling ``[0, duration]`` exactly.

    - gap/overlap free by construction (window i+1 starts where i ends);
    - the last window is the explicit remainder (may be short);
    - if ``ceil(duration / window) > max_windows`` the window size is
      widened to ``ceil(duration / max_windows)`` (cost bound, coverage
      preserved);
    - a sub-window-length source yields a single window.
    """
    if duration_seconds <= 0:
        raise ValueError("duration must be > 0")
    if window_seconds < 1 or max_windows < 1:
        raise ValueError("window_seconds and max_windows must be >= 1")

    size = float(max(1, window_seconds))
    # Windows needed at the requested size; widen if over the cost bound
    # (coverage is preserved — the window size grows, never the gaps).
    needed = max(1, int(-(-round(duration_seconds) // int(size))))
    if needed > max_windows:
        size = float(max(1, int(-(-round(duration_seconds) // max_windows))))

    windows: list[TimelineWindow] = []
    index = 0
    cursor = 0.0
    while cursor < duration_seconds - _EPS:
        end = min(cursor + size, duration_seconds)
        windows.append(TimelineWindow(index, cursor, end))
        cursor = end
        index += 1
    # Guard: a zero-length probe or FP edge must still yield >= 1 window.
    if not windows:
        windows.append(TimelineWindow(0, 0.0, duration_seconds))
    return windows


def verify_coverage(rows: Sequence[TimelineRow], duration_seconds: float) -> None:
    """Raises TimelineCoverageError unless ``rows`` exactly tile
    [0, duration]: sorted by window_index, contiguous, bounded."""
    if not rows:
        raise TimelineCoverageError("timeline has no rows")
    ordered = sorted(rows, key=lambda r: r.window_index)
    first = ordered[0]
    if abs(first.start_time) > _EPS:
        raise TimelineCoverageError(
            f"timeline starts at {first.start_time:.3f}s, expected 0s")
    for prev, cur in zip(ordered, ordered[1:]):
        if cur.start_time < prev.end_time - _EPS:
            raise TimelineCoverageError(
                f"windows {prev.window_index} and {cur.window_index} overlap")
        if cur.start_time > prev.end_time + _EPS:
            raise TimelineCoverageError(
                f"gap between windows {prev.window_index} and {cur.window_index} "
                f"({prev.end_time:.3f}s - {cur.start_time:.3f}s)")
    last = ordered[-1]
    if last.end_time < duration_seconds - _EPS:
        raise TimelineCoverageError(
            f"timeline ends at {last.end_time:.3f}s, source is {duration_seconds:.3f}s")
    if last.end_time > duration_seconds + _EPS:
        raise TimelineCoverageError(
            f"timeline extends to {last.end_time:.3f}s past source end "
            f"{duration_seconds:.3f}s")


# --------------------------------------------------------------------------
# Per-window evidence extraction (measured data only — never invented)
# --------------------------------------------------------------------------

def window_text(segments: Iterable[Any], start: float, end: float,
                max_chars: int = 4000) -> str:
    """The ACTUAL transcribed text overlapping ``[start, end)``.

    ``segments`` expose ``.text / .start / .end`` (transcription segment
    objects or plain mappings). Segments outside the window are excluded;
    an empty window returns "" — no filler, no invention.
    """
    parts: list[str] = []
    total = 0
    for seg in segments or ():
        s_start = float(seg.get("start") if isinstance(seg, dict) else seg.start)
        s_end = float(seg.get("end") if isinstance(seg, dict) else seg.end)
        if s_end <= start or s_start >= end:
            continue
        text = (seg.get("text") if isinstance(seg, dict) else seg.text) or ""
        text = text.strip()
        if not text:
            continue
        parts.append(text)
        total += len(text)
        if total >= max_chars:
            break
    return " ".join(parts)[:max_chars]


def _event_kind(event: Any) -> str:
    return str(event.get("kind") if isinstance(event, dict) else getattr(event, "kind", ""))


def _event_time(event: Any) -> tuple[float, float]:
    if isinstance(event, dict):
        return (float(event.get("t", 0.0)), float(event.get("t_end", event.get("t", 0.0))))
    return (float(getattr(event, "t", 0.0)), float(getattr(event, "t_end", 0.0)))


def window_evidence(events: Sequence[Any], start: float, end: float,
                    max_per_family: int = 12) -> dict[str, Any]:
    """MEASURED signals overlapping ``[start, end)``, grouped by family.

    Output shape: ``{"audio": [...], "visual": [...], "total": n}`` where
    each entry is ``{"kind", "t", "strength", "evidence"}`` (evidence is
    capped to safe scalars — the store already bounds it).
    """
    grouped: dict[str, list[dict[str, Any]]] = {"audio": [], "visual": []}
    total = 0
    for event in events or ():
        family = "audio" if (
            (event.get("family") if isinstance(event, dict) else getattr(event, "family", "")) == "audio"
        ) else "visual"
        t, t_end = _event_time(event)
        if t_end > t:
            # Durable event: overlaps the half-open window [start, end).
            if t_end <= start or t >= end:
                continue
        else:
            # Point event (e.g. a scene cut): the window containing its
            # timestamp owns it (start <= t < end) — a cut exactly on a
            # window boundary belongs to the window that STARTS there.
            if t < start or t >= end:
                continue
        ev = (event.get("evidence") if isinstance(event, dict) else getattr(event, "evidence", {})) or {}
        grouped[family].append({
            "kind": _event_kind(event),
            "t": round(t, 3),
            "strength": round(float(event.get("strength") if isinstance(event, dict)
                                    else getattr(event, "strength", 0.0)), 4),
            "evidence": {k: v for k, v in list(ev.items())[:6]
                         if isinstance(v, (str, int, float, bool))},
        })
        total += 1
    for family in grouped:
        grouped[family].sort(key=lambda e: e["t"])
        grouped[family] = grouped[family][:max_per_family]
    return {"audio": grouped["audio"], "visual": grouped["visual"], "total": total}


def _window_confidence(evidence: dict[str, Any]) -> float | None:
    """Confidence from MEASURED signal strength only: the peak strength of
    any event in the window, else None (no signal measured — never a
    guessed number)."""
    strengths = [e["strength"] for fam in ("audio", "visual") for e in evidence.get(fam, [])]
    return round(max(strengths), 4) if strengths else None


# --------------------------------------------------------------------------
# Unified timeline assembly
# --------------------------------------------------------------------------

def build_timeline_rows(
    duration_seconds: float,
    windows: Sequence[TimelineWindow],
    segments: Iterable[Any],
    events: Sequence[Any],
    assessments: dict[int, dict[str, Any]] | None = None,
) -> list[TimelineRow]:
    """One row per window — the unified timeline.

    ``assessments`` maps window_index → the Gemini assessment dict
    (``{"status": "ok", ...}`` or ``{"status": "failed", "reason": ...}``).
    Missing entries are recorded honestly as ``{"status": "not_configured"}``
    when Gemini was not available, or ``{"status": "pending"}`` semantics
    are NEVER used — every returned row is terminal.
    """
    segments_list = list(segments or ())
    events_list = list(events or ())
    assessments = assessments or {}
    rows: list[TimelineRow] = []
    for window in windows:
        text = window_text(segments_list, window.start, window.end)
        evidence = window_evidence(events_list, window.start, window.end)
        assessment = assessments.get(window.index)
        row = TimelineRow(
            window_index=window.index,
            start_time=window.start,
            end_time=window.end,
            transcript=text,
            visual_evidence={"events": evidence["visual"], "total": len(evidence["visual"])},
            audio_evidence={"events": evidence["audio"], "total": len(evidence["audio"])},
            confidence=_window_confidence(evidence),
        )
        if assessment is None:
            row.gemini_assessment = {"status": "not_configured"}
        elif assessment.get("status") == "ok":
            row.scene_description = assessment.get("scene_description")
            event_type = assessment.get("event_type")
            row.event_type = event_type if event_type in EVENT_TYPES else "unknown"
            importance = assessment.get("event_importance")
            row.event_importance = (
                round(float(importance), 4)
                if isinstance(importance, (int, float)) and 0.0 <= float(importance) <= 1.0
                else None
            )
            row.candidate_clip = bool(assessment.get("candidate_clip"))
            row.gemini_assessment = dict(assessment)
        else:
            # Failed / not-yet-available: keep honest state, no invented
            # semantics. event_importance stays None, candidate False.
            row.gemini_assessment = {
                "status": "failed",
                "reason": str(assessment.get("reason") or "unavailable")[:300],
            }
        rows.append(row)
    verify_coverage(rows, duration_seconds)
    return rows
