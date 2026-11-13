"""CandidateWindow adapter (TASK 010, layer 5 output).

Bridges the engine's ranked candidates onto the EXISTING selection
contract — the same ``CandidateWindow`` type Phase 7's ``fuse()``
returns, so selection/merge/dedup/categorize/rank and the renderer are
all untouched:

- ``events`` — the cluster's ORIGINAL ``SignalEvent`` list (kept
  multiset, time-ordered; overflow included so the scored multiset is
  exactly the one that was scored);
- ``score`` / qualification — from the UNCHANGED Phase 7
  ``score_cluster`` (identical bar, identical number);
- ``confidence`` — the engine's ``heuristic_confidence`` (replaces the
  crude Phase 7 ``0.5 × n_families`` ONLY when multimodal mode is on);
- ``reasons`` — the Phase 7 scorer reasons plus ``"multimodal"`` (the
  full provenance). Phase 7 keys are unchanged → backward compatible;
  ``select_clips`` passes the provenance through into the clip evidence.
"""

from __future__ import annotations

from ..fusion import CandidateWindow, FusionConfig
from .ranking import RankedCandidate


def to_candidate_window(rc: RankedCandidate, fusion_cfg: FusionConfig) -> CandidateWindow:
    """Adapts one kept :class:`RankedCandidate` to a CandidateWindow."""
    events = sorted(
        rc.cluster.events + rc.cluster.overflow,
        key=lambda s: (s.t, s.t_end, s.family, s.kind, s.key),
    )
    originals = [s.original for s in events]
    return CandidateWindow(
        start=rc.start,
        end=rc.end,
        events=originals,
        score=rc.score,
        confidence=rc.confidence.heuristic_confidence,
        families=tuple(rc.score_reasons["families"]),
        family_strengths=dict(rc.score_reasons["family_strengths"]),
        reasons={**rc.score_reasons, "multimodal": rc.provenance},
        min_score=fusion_cfg.min_score,
    )
