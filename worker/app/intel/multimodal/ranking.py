"""Candidate moment generation + ranking hygiene (TASK 010, layer 5).

Turns analyzed clusters into candidate moments with MEASURED, clamped
boundaries and enforced hygiene rules:

- boundaries = the cluster's measured span ± configurable padding,
  clamped to ``[0, total_duration]`` (never negative, never beyond the
  source);
- qualification uses the UNCHANGED Phase 7 ``score_cluster`` bar
  (``min_score``) — multimodal mode never re-defines what "qualifies";
- suppression (recorded, never silent):
  * ``empty_interval`` — clamped bounds collapsed (defensive);
  * ``below_min_score`` — same bar as Phase 7 fusion;
  * ``insufficient_independent_modalities`` — the configured floor;
  * ``below_confidence_threshold`` — the heuristic-confidence floor
    (conservative: multimodal mode may select LESS than Phase 7,
    never arbitrarily more);
  * ``single_source_repetition`` — one family with max strength below
    ``single_source_min_strength`` (repeated observations of ONE source
    cannot justify a candidate; a strong single-source moment — e.g.
    the spec's "strong audio-only" — still can);
- the output is capped at ``max_candidates`` (deterministic: highest
  score first, then earliest; survivors re-sorted in time order).

``RankedCandidate`` is the per-cluster analysis bundle that flows into
the CandidateWindow adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..fusion import FusionConfig
from .confidence import ConfidenceResult
from .config import MultimodalConfig
from .correlation import TemporalCluster
from .event_graph import EventGraph
from .sequences import PatternMatch


@dataclass
class RankedCandidate:
    """One analyzed cluster: the measurement + its interpretation."""

    cluster: TemporalCluster
    score: float                      # Phase 7 score_cluster (unchanged)
    score_reasons: dict[str, Any]     # Phase 7 scorer reasons
    confidence: ConfidenceResult      # heuristic confidence + full calc
    pattern: PatternMatch | None
    contradictions: list[str]
    graph: EventGraph
    sequences: list[list[str]]
    provenance: dict[str, Any]
    start: float = 0.0
    end: float = 0.0
    suppressed: str | None = None     # suppression reason (None = kept)


def candidate_bounds(
    cluster: TemporalCluster, cfg: MultimodalConfig, total_duration: float
) -> tuple[float, float]:
    """Measured span ± padding, clamped to the source timeline."""
    start = max(0.0, cluster.start - cfg.pre_padding_seconds)
    end = min(total_duration, cluster.end + cfg.post_padding_seconds)
    return round(start, 3), round(end, 3)


def suppression_reason(
    cluster: TemporalCluster,
    *,
    score: float,
    heuristic_confidence: float,
    fusion_cfg: FusionConfig,
    cfg: MultimodalConfig,
    total_duration: float,
) -> str | None:
    """None if the cluster may become a candidate; otherwise the reason.

    Checks are ordered deterministically (the FIRST failing rule wins).
    """
    start, end = candidate_bounds(cluster, cfg, total_duration)
    if end - start <= 0:
        return "empty_interval"
    if score < fusion_cfg.min_score:
        return "below_min_score"
    if len(cluster.independent_modalities) < cfg.min_independent_modalities:
        return "insufficient_independent_modalities"
    if heuristic_confidence < cfg.confidence_threshold:
        return "below_confidence_threshold"
    if len(cluster.independent_modalities) == 1:
        max_strength = max((s.strength for s in cluster.events), default=0.0)
        if max_strength < cfg.single_source_min_strength:
            return "single_source_repetition"
    return None


def apply_candidate_cap(
    candidates: list[RankedCandidate], max_candidates: int
) -> tuple[list[RankedCandidate], int]:
    """Deterministic cap: keep the strongest (score desc, start asc);
    survivors are returned in TIME order for downstream selection."""
    if len(candidates) <= max_candidates:
        return candidates, 0
    ranked = sorted(candidates, key=lambda rc: (-rc.score, rc.cluster.start))
    kept = ranked[:max_candidates]
    dropped = len(candidates) - max_candidates
    kept.sort(key=lambda rc: rc.cluster.start)
    return kept, dropped
