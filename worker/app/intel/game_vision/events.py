"""Gameplay event interpretation (TASK 009) — conservative candidates.

Turns MEASURED observations (kill-feed events, scoreboard visibility,
score changes) into conservative ``GameplayCandidate`` events.

Honesty contract (spec section 4E): every candidate is flagged
``directly_observed`` — True when the underlying text/values were
measured by OCR, False for the heuristic visibility flag. The
higher-level INTERPRETATION ("this line probably means a kill", "the
match may have ended") is always recorded as a heuristic note in the
evidence — measured text is never dressed up as verified game state.

Event kinds (conservatively few):
- ``kill_feed_entry``        — new transient text in the kill-feed region
- ``scoreboard_appearance``  — scoreboard likely visible (heuristic)
- ``score_change``           — an OCR-observed value changed
- ``match_result_text``      — full-line text exactly matching a tiny
                               generic result-token set (text observed;
                               "match ended" interpretation = heuristic)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import GameVisionConfig, MATCH_RESULT_TOKENS
from .kill_feed import KillFeedEvent
from .scoreboard import ScoreValueChange, ScoreboardObservation

KILL_FEED_ENTRY = "kill_feed_entry"
SCOREBOARD_APPEARANCE = "scoreboard_appearance"
SCORE_CHANGE = "score_change"
MATCH_RESULT_TEXT = "match_result_text"

NOTE = ("game vision evidence is OCR-observed visual content; any "
        "higher-level interpretation (kill, death, result) is a "
        "HEURISTIC, not a verified game state")


@dataclass
class GameplayCandidate:
    """One conservative candidate gameplay moment (chunk-relative)."""

    kind: str
    t_rel: float
    t_end_rel: float
    strength: float            # 0..1, measured (confidence-driven)
    directly_observed: bool    # True = OCR-measured text/values underlie it
    evidence: dict[str, Any] = field(default_factory=dict)


def _one_sample(cfg: GameVisionConfig) -> float:
    return 1.0 / cfg.sample_fps


def interpret_gameplay(
    kill_feed_events: list[KillFeedEvent],
    scoreboard_observations: list[ScoreboardObservation],
    score_changes: list[ScoreValueChange],
    cfg: GameVisionConfig,
) -> list[GameplayCandidate]:
    out: list[GameplayCandidate] = []

    # -- kill-feed entries (observed text; result tokens split out) ------
    for e in kill_feed_events:
        end = e.disappeared_at if e.disappeared_at is not None \
            else e.appeared_at + _one_sample(cfg)
        strength = min(1.0, e.mean_confidence / 100.0)
        evidence = {
            "text": e.text,
            "ocr_confidence": e.mean_confidence,
            "frames_seen": e.frames_seen,
            "disappeared_at": e.disappeared_at,
            "note": NOTE,
        }
        if e.key in MATCH_RESULT_TOKENS:
            out.append(GameplayCandidate(
                kind=MATCH_RESULT_TEXT, t_rel=e.appeared_at,
                t_end_rel=max(e.appeared_at + 0.5, end), strength=strength,
                directly_observed=True,
                evidence={**evidence,
                          "interpretation": "possibly a match/round result "
                                            "(generic token match; heuristic)"},
            ))
        else:
            out.append(GameplayCandidate(
                kind=KILL_FEED_ENTRY, t_rel=e.appeared_at, t_end_rel=end,
                strength=strength, directly_observed=True,
                evidence={**evidence,
                          "interpretation": "possibly a kill/elimination "
                                            "notification (heuristic)"},
            ))

    # -- scoreboard appearances (one per contiguous visible run) ---------
    run_start: float | None = None
    run_end: float | None = None
    run_numeric = 0
    def _flush() -> None:
        if run_start is not None:
            strength = min(1.0, 0.4 + 0.05 * min(run_numeric, 12))
            out.append(GameplayCandidate(
                kind=SCOREBOARD_APPEARANCE, t_rel=run_start,
                t_end_rel=max(run_end, run_start + _one_sample(cfg)),
                strength=strength, directly_observed=False,
                evidence={
                    "numeric_tokens_observed": run_numeric,
                    "interpretation": "scoreboard likely on screen "
                                      "(text-density heuristic)",
                    "note": NOTE,
                },
            ))
    for obs in scoreboard_observations:
        if obs.visible:
            if run_start is None:
                run_start, run_numeric = obs.t_rel, obs.numeric_count
            run_end = obs.t_rel
            run_numeric = max(run_numeric, obs.numeric_count)
        else:
            _flush()
            run_start = run_end = None
            run_numeric = 0
    _flush()

    # -- score changes (both values OCR-observed) --------------------------
    for c in score_changes:
        out.append(GameplayCandidate(
            kind=SCORE_CHANGE, t_rel=c.t_rel,
            t_end_rel=c.t_rel + _one_sample(cfg),
            strength=min(1.0, c.confidence / 100.0), directly_observed=True,
            evidence={
                "label": c.label, "before": c.before, "after": c.after,
                "ocr_confidence": c.confidence, "note": NOTE,
            },
        ))

    out.sort(key=lambda c: c.t_rel)
    return out
