"""Game-vision SignalEvent conversion (TASK 009).

Converts conservative ``GameplayCandidate``s into the existing
``SignalEvent`` architecture (family ``game_vision``):

- ABSOLUTE source timestamps: the chunk ``offset`` is applied exactly
  once here (candidates carry chunk-relative times) — consistent with
  the facecam and audio/visual families;
- every event carries measured evidence + detector metadata (engine,
  version, sample rate, OCR confidence, observed-vs-heuristic flag);
- ``event_key`` 0.1 s bucketing + midpoint ownership are UNCHANGED —
  the pipeline inserts these into ``raw_events`` before the ownership
  filter, so existing checkpoint/dedup semantics apply for free
  (an event in the overlap is persisted exactly once).
"""

from __future__ import annotations

from ..signals import (
    KIND_GF_KILL_FEED_ENTRY,
    KIND_GF_MATCH_RESULT_TEXT,
    KIND_GF_SCOREBOARD_APPEARANCE,
    KIND_GF_SCORE_CHANGE,
    SignalEvent,
)
from .config import GameVisionConfig
from .events import (
    KILL_FEED_ENTRY,
    MATCH_RESULT_TEXT,
    SCOREBOARD_APPEARANCE,
    SCORE_CHANGE,
    GameplayCandidate,
)

_KIND_MAP = {
    KILL_FEED_ENTRY: KIND_GF_KILL_FEED_ENTRY,
    SCOREBOARD_APPEARANCE: KIND_GF_SCOREBOARD_APPEARANCE,
    SCORE_CHANGE: KIND_GF_SCORE_CHANGE,
    MATCH_RESULT_TEXT: KIND_GF_MATCH_RESULT_TEXT,
}


def detect_game_vision_events(
    candidates: list[GameplayCandidate],
    cfg: GameVisionConfig,
    offset: float,
    engine_name: str | None = None,
    engine_version: str | None = None,
) -> list[SignalEvent]:
    """Maps candidates to SignalEvents (absolute times, full evidence)."""
    events: list[SignalEvent] = []
    for c in candidates:
        kind = _KIND_MAP.get(c.kind)
        if kind is None:  # unknown candidate kind: never emit silently
            continue
        strength = round(min(1.0, max(0.0, c.strength)), 4)
        # family/kind/strength are stored IN the evidence so a re-hydrated
        # row (from_row after resume) materializes the SAME SignalEvent —
        # the existing persistence contract (same as facecam).
        evidence = dict(c.evidence)
        evidence.update({
            "family": "game_vision",
            "kind": kind,
            "strength": strength,
            "engine": engine_name,
            "engine_version": engine_version,
            "sample_fps": cfg.sample_fps,
            "directly_observed": c.directly_observed,
        })
        events.append(SignalEvent(
            t=round(offset + c.t_rel, 3),
            t_end=round(offset + max(c.t_end_rel, c.t_rel), 3),
            family="game_vision",
            kind=kind,
            strength=strength,
            evidence=evidence,
        ))
    return events
