"""Cross-modal pattern matching (TASK 010, layer 3b).

Configurable, evidence-backed patterns (spec section 6). A pattern match
is a CANDIDATE INTERPRETATION of measured events — never verified game
state. Matching is deterministic and conservative:

- the pattern's ANCHOR kind(s) must be present in the cluster;
- each non-anchor stage is satisfied by the EARLIEST kept (unused)
  signal of one of the stage's kinds whose START is after the previous
  stage's START and at most ``step_window_seconds`` later (stages are
  ordered by START — overlapping reactions, e.g. a face burst that
  begins mid-vocal-spike, still chain; timing uncertainty tolerated).
  A stage may be skipped when unsatisfied;
- the match must span at least ``min_independent_modalities`` distinct
  families (from the INDEPENDENT observations — corroborations and
  same-family repeats never help a pattern qualify).

Returns the matched pattern + the concrete stage signals (provenance).
At most ONE pattern per cluster (the best: most stages satisfied, then
most independent modalities, then earliest start, then name — fully
deterministic tie-break).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import MultimodalConfig, PatternConfig
from .correlation import TemporalCluster
from .timeline import NormalSignal


@dataclass
class PatternMatch:
    pattern: str
    stages: list[NormalSignal]          # one per satisfied stage (time-ordered)
    independent_modalities: int
    explanation: str


def _next_after(events: list[NormalSignal], kinds: frozenset[str],
                after_start: float, window: float,
                used: set[str]) -> NormalSignal | None:
    """Earliest (unused) signal of one of ``kinds`` whose START is at
    ``after_start`` (simultaneous ok) or within ``window`` seconds after
    it (start-based ordering — see module docstring)."""
    for s in events:  # time-ordered by start
        if s.t > after_start + window:
            break
        if s.key in used or s.kind not in kinds:
            continue
        if s.t >= after_start - 1e-9:
            return s
    return None


def _earliest_of(events: list[NormalSignal], kinds: frozenset[str]) -> NormalSignal | None:
    for s in events:  # time-ordered
        if s.kind in kinds:
            return s
    return None


def match_patterns(
    cluster: TemporalCluster, cfg: MultimodalConfig
) -> PatternMatch | None:
    events = cluster.events  # time-ordered
    if not events:
        return None

    best: PatternMatch | None = None
    best_rank: tuple[int, int, float, str] | None = None

    for pattern in cfg.patterns:
        # anchor must be present
        anchor = _earliest_of(events, pattern.anchor_kinds)
        if anchor is None:
            continue

        stages: list[NormalSignal] = []
        cursor = anchor.t  # stage 0 = anchor; stages chain by START
        stages.append(anchor)
        matched = 1
        # subsequent stages (skippable, in order, within tolerance)
        for stage_kinds in pattern.step_kinds[1:]:
            candidate = _next_after(events, stage_kinds, cursor,
                                     pattern.step_window_seconds,
                                     used={s.key for s in stages})
            if candidate is not None:
                stages.append(candidate)
                cursor = candidate.t
                matched += 1
            # skipped stages are simply not counted (conservative)

        families = {s.family for s in stages}
        if len(families) < pattern.min_independent_modalities:
            continue
        # special: strong_audio_only needs a STRONG audio anchor
        if pattern.name == "strong_audio_only":
            if max(s.strength for s in stages) < cfg.strong_audio_only_threshold:
                continue

        m = PatternMatch(
            pattern=pattern.name,
            stages=stages,
            independent_modalities=len(families),
            explanation=" → ".join(s.kind for s in stages),
        )
        rank = (matched, len(families), -stages[0].t, m.pattern)
        if best_rank is None or rank > best_rank:
            best, best_rank = m, rank
    return best
