"""Evidence provenance (TASK 010, layer 4b).

Every candidate moment carries a traceable, size-bounded explanation so a
user can understand WHY the moment was selected:

- supporting event keys + families/kinds/timestamps/original strengths/
  qualities/detectors (capped; the TOTAL count is always recorded, so
  nothing is silently discarded);
- independent observations and independent modalities (the dedup result);
- temporal relationships (bounded graph edges) + a one-line graph chain;
- extracted multi-stage sequences;
- the matched pattern (candidate interpretation, never ground truth);
- excluded/duplicate evidence (exact, repeated, overflow — counted +
  the cluster's dedup tiers are explained in the correlation module);
- contradictory evidence (the conservative rule set);
- the FULL heuristic-confidence calculation (every input + penalty) with
  its version;
- the detector availability snapshot (missing data stays visible and
  never becomes negative evidence).

Size bound: the serialized provenance is capped at ``PROVENANCE_MAX_BYTES``
(deterministic cascade: relationships → supporting → confidence weights →
proximity weights; a ``provenance_truncated`` flag is set when the cascade
runs). This keeps nested-JSON storage (jsonb) small — no migration, no
large payloads to ordinary frontend endpoints.
"""

from __future__ import annotations

import json
from typing import Any

from .confidence import ConfidenceResult
from .config import MultimodalConfig
from .correlation import TemporalCluster
from .event_graph import EventGraph
from .sequences import PatternMatch

PROVENANCE_MAX_SUPPORTING = 64
PROVENANCE_MAX_EDGES = 16
#: Hard cap on the serialized provenance (bytes). The clip evidence is
#: nested JSONB on the clips table; 8 KiB keeps a job's clips table well
#: within sane sizes even at the candidate cap (200 × 8 KiB = 1.6 MiB worst
#: case, and real provenance is 1-3 KiB).
PROVENANCE_MAX_BYTES = 8192


def _size_bytes(prov: dict[str, Any]) -> int:
    return len(json.dumps(prov, sort_keys=True,
                          separators=(",", ":")).encode("utf-8"))


def _fits(prov: dict[str, Any]) -> bool:
    return _size_bytes(prov) <= PROVENANCE_MAX_BYTES


def _enforce_size_bound(prov: dict[str, Any]) -> dict[str, Any]:
    """Deterministic shrink cascade (largest sections first)."""
    if _fits(prov):
        return prov
    # 1) graph edges
    prov["relationships"] = prov["relationships"][:4]
    if _fits(prov):
        return prov
    # 2) supporting evidence (total is still recorded)
    prov["supporting"] = prov["supporting"][:16]
    if _fits(prov):
        return prov
    # 3) confidence calc detail (the number + version stay)
    calc = prov.get("confidence", {}).get("calc", {})
    calc.get("proximity", {}).pop("weights", None)
    if _fits(prov):
        return prov
    # 4) proximity weights
    prov["proximity_weights"] = []
    prov["provenance_truncated"] = True
    return prov


def build_provenance(
    cluster: TemporalCluster,
    graph: EventGraph,
    sequences: list[list[str]],
    pattern: PatternMatch | None,
    contradictions: list[str],
    confidence: ConfidenceResult,
    cfg: MultimodalConfig,
    availability: dict[str, str],
) -> dict[str, Any]:
    """Assembles the full provenance record for one cluster."""
    kept = cluster.events  # all kept signals, time-ordered
    supporting = [
        {
            "key": s.key,
            "family": s.family,
            "kind": s.kind,
            "t": s.t,
            "t_end": s.t_end,
            "strength": s.strength,     # ORIGINAL measured strength
            "quality": s.quality,       # derived (never overrides strength)
            "detector": s.detector,
        }
        for s in kept
    ]
    supporting_total = len(supporting)
    if len(supporting) > PROVENANCE_MAX_SUPPORTING:
        supporting = supporting[:PROVENANCE_MAX_SUPPORTING]

    families: dict[str, dict[str, Any]] = {}
    for s in kept + cluster.overflow:
        f = families.setdefault(s.family, {"max_strength": 0.0,
                                           "detectors": [], "n_events": 0})
        f["max_strength"] = max(f["max_strength"], s.strength)
        f["n_events"] += 1
        if s.detector and s.detector not in f["detectors"]:
            f["detectors"].append(s.detector)

    prov: dict[str, Any] = {
        "version": cfg.confidence_version,
        "cluster": {"start": cluster.start, "end": cluster.end},
        "independent_observations": [list(k) for k in
                                     sorted(cluster.independent_observations)],
        "independent_modalities": sorted(cluster.independent_modalities),
        "families": {
            fam: {
                "max_strength": fam_data["max_strength"],
                "detectors": fam_data["detectors"][:4],
                "n_events": fam_data["n_events"],
            }
            for fam, fam_data in sorted(families.items())
        },
        "supporting": supporting,
        "supporting_total": supporting_total,
        "relationships": [
            {"a": e.a, "b": e.b, "relation": e.relation, "gap": e.gap}
            for e in graph.edges[:PROVENANCE_MAX_EDGES]
        ],
        "graph_explanation": graph.explanation(),
        "sequences": sequences,
        "pattern": (
            {
                "name": pattern.pattern,
                "stages": [s.kind for s in pattern.stages],
                "stage_keys": [s.key for s in pattern.stages],
                "independent_modalities": pattern.independent_modalities,
                "explanation": pattern.explanation,
            }
            if pattern is not None else None
        ),
        "duplicates": {
            "exact": cluster.duplicates_exact,
            "repeated": cluster.duplicates_repeated,
            "overflow": len(cluster.overflow),
        },
        "contradictions": list(contradictions),
        "proximity_weights": list(cluster.proximity_weights),
        "availability": dict(availability),
        "confidence": confidence.to_dict(),
    }
    return _enforce_size_bound(prov)
