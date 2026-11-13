"""Advanced multimodal fusion engine (TASK 010).

An OPTIONAL temporal intelligence layer over the Phase 7 fusion — not a
replacement. Five conceptual layers:

1. **Signal normalization** — :mod:`.timeline` (validated re-projection of
   the existing ``SignalEvent`` contract; quarantine, never crash);
2. **Temporal correlation** — :mod:`.correlation` (sweep-line clustering,
   three-tier duplicate suppression, evidence independence);
3. **Event & sequence interpretation** — :mod:`.event_graph` (bounded
   graph + distinct-kind sequences) and :mod:`.sequences` (conservative
   cross-modal patterns);
4. **Confidence & evidence evaluation** — :mod:`.confidence`
   (``heuristic_confidence`` — bounded, deterministic, fully explained;
   NOT a calibrated probability until TASK 013) and :mod:`.evidence`
   (size-bounded provenance);
5. **Candidate moment ranking** — :mod:`.ranking` (measured+clamped
   boundaries, suppression rules, caps) and :mod:`.signals` (the
   CandidateWindow adapter that keeps selection/renderer unchanged).

Entry point: :func:`fuse_multimodal` — a PURE function of the stored
events + configs, which is what makes it chunk-compatible and resumable
(fusion always runs over the persisted evidence, never over in-flight
detector state).

Disabled mode: ``PipelineConfig.multimodal is None`` (the default) → the
pipeline calls Phase 7 ``fuse()`` exactly as before. Verified by an A/B
pipeline test (byte-identical windows).

Availability states (per family; missing data is NEVER negative
evidence — an unavailable detector simply contributes no modality):

- ``disabled`` — the modality was not enabled for this job;
- ``unavailable`` — enabled but could not run (no model/binary/dep);
- ``available`` — ran, produced events;
- ``available_no_events`` — ran, measured, nothing detected;
- ``partial`` — ran on some chunks (others failed/were budget-exceeded);
- ``failed`` — ran, failed irrecoverably this run;
- ``cancelled`` — run was cancelled mid-modality.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..fusion import CandidateWindow, FusionConfig, score_cluster
from ..signals import SIGNAL_FAMILIES, SignalEvent
from .confidence import ConfidenceResult, compute_confidence, detect_contradictions
from .config import CONFIDENCE_VERSION, MultimodalConfig, PatternConfig
from .correlation import Observation, TemporalCluster, correlate
from .evidence import build_provenance
from .event_graph import (
    REL_FOLLOWS,
    REL_OVERLAP,
    REL_PRECEDES,
    Edge,
    EventGraph,
    build_graph,
    extract_sequences,
)
from .ranking import (
    RankedCandidate,
    apply_candidate_cap,
    candidate_bounds,
    suppression_reason,
)
from .sequences import PatternMatch, match_patterns
from .signals import to_candidate_window
from .timeline import NormalSignal, NormalizationResult, normalize_events

__all__ = [
    "AVAIL_AVAILABLE",
    "AVAIL_AVAILABLE_NO_EVENTS",
    "AVAIL_CANCELLED",
    "AVAIL_DISABLED",
    "AVAIL_FAILED",
    "AVAIL_PARTIAL",
    "AVAIL_UNAVAILABLE",
    "CONFIDENCE_VERSION",
    "MultimodalConfig",
    "MultimodalFusionEngine",
    "MultimodalFusionResult",
    "PatternConfig",
    "fuse_multimodal",
    "normalize_availability",
]

AVAIL_DISABLED = "disabled"
AVAIL_UNAVAILABLE = "unavailable"
AVAIL_AVAILABLE = "available"
AVAIL_AVAILABLE_NO_EVENTS = "available_no_events"
AVAIL_PARTIAL = "partial"
AVAIL_FAILED = "failed"
AVAIL_CANCELLED = "cancelled"
KNOWN_AVAILABILITY_STATES = frozenset({
    AVAIL_DISABLED, AVAIL_UNAVAILABLE, AVAIL_AVAILABLE,
    AVAIL_AVAILABLE_NO_EVENTS, AVAIL_PARTIAL, AVAIL_FAILED, AVAIL_CANCELLED,
})


def normalize_availability(
    availability: dict[str, str] | None,
) -> tuple[dict[str, str], int]:
    """Validates the availability snapshot: only known families and known
    states survive (unknown entries are counted, not invented)."""
    out: dict[str, str] = {}
    dropped = 0
    for family, state in (availability or {}).items():
        if family in SIGNAL_FAMILIES and state in KNOWN_AVAILABILITY_STATES:
            out[family] = state
        else:
            dropped += 1
    return out, dropped


@dataclass
class MultimodalFusionResult:
    """The engine output: qualifying windows (Phase 7 type) + an honest
    summary of everything the engine saw (budgets, quarantine,
    suppression, availability)."""

    windows: list[CandidateWindow]
    summary: dict[str, Any] = field(default_factory=dict)


def _family_weights(fusion_cfg: FusionConfig) -> dict[str, float]:
    return {
        "audio": fusion_cfg.audio_weight,
        "visual": fusion_cfg.visual_weight,
        "facecam": fusion_cfg.facecam_weight,
        "game_vision": fusion_cfg.game_vision_weight,
    }


def fuse_multimodal(
    events: list[SignalEvent],
    *,
    job_id: str,
    total_duration: float,
    fusion_cfg: FusionConfig,
    cfg: MultimodalConfig | None = None,
    availability: dict[str, str] | None = None,
) -> MultimodalFusionResult:
    """The full engine: normalize → correlate → interpret → evaluate →
    rank → adapt. Pure and deterministic (the wall-clock budget only
    REPORTS, it never changes which clusters were analyzed before the
    check). Never raises on malformed input (quarantine)."""
    cfg = cfg or MultimodalConfig()
    avail, avail_invalid = normalize_availability(availability)
    started = time.monotonic()

    # -- layer 1: normalization ------------------------------------------
    norm = normalize_events(events, job_id, cfg.max_events)
    if not norm.signals:
        summary = _summary(
            cfg, avail, avail_invalid, norm, clusters=0, clusters_dropped=0,
            unprocessed=0, patterns=0, contradictions=0, suppressed=(),
            cands=(), capped=0, started=started, status="no_events",
        )
        return MultimodalFusionResult(windows=[], summary=summary)

    # -- layer 2: temporal correlation (bounded sweep) ---------------------
    clusters, clusters_dropped = correlate(norm.signals, cfg)

    # -- layers 3+4: per-cluster interpretation + evaluation ---------------
    weights = _family_weights(fusion_cfg)
    analyzed: list[RankedCandidate] = []
    suppressed_records: list[dict[str, Any]] = []
    patterns_matched = 0
    contradictions_count = 0
    unprocessed = 0
    budget_exceeded = False

    for index, cluster in enumerate(clusters):
        if (time.monotonic() - started) > cfg.processing_budget_seconds:
            # Deterministic report: everything analyzed so far is kept;
            # the remaining clusters are counted, not silently dropped.
            unprocessed = len(clusters) - index
            budget_exceeded = True
            break

        originals = [s.original for s in (cluster.events + cluster.overflow)]
        score, _phase7_confidence, reasons = score_cluster(originals, fusion_cfg)
        graph = build_graph(cluster, cfg)
        sequences = extract_sequences(cluster, graph, cfg,
                                      cfg.correlation_window_seconds)
        pattern = match_patterns(cluster, cfg)
        if pattern is not None:
            patterns_matched += 1
        contradictions = detect_contradictions(cluster)
        contradictions_count += len(contradictions)
        confidence = compute_confidence(cluster, cfg, weights,
                                        fusion_cfg.diversity_bonus,
                                        pattern, contradictions)
        provenance = build_provenance(cluster, graph, sequences, pattern,
                                      contradictions, confidence, cfg, avail)

        rc = RankedCandidate(
            cluster=cluster, score=score, score_reasons=reasons,
            confidence=confidence, pattern=pattern,
            contradictions=contradictions, graph=graph,
            sequences=sequences, provenance=provenance,
        )
        reason = suppression_reason(
            cluster, score=score,
            heuristic_confidence=confidence.heuristic_confidence,
            fusion_cfg=fusion_cfg, cfg=cfg, total_duration=total_duration,
        )
        if reason is not None:
            rc.suppressed = reason
            suppressed_records.append(
                {"index": index, "start": cluster.start, "reason": reason})
        else:
            rc.start, rc.end = candidate_bounds(cluster, cfg, total_duration)
            analyzed.append(rc)

    # -- layer 5: cap + adapt to the existing selection contract -----------
    kept, capped = apply_candidate_cap(analyzed, cfg.max_candidates)
    windows = [to_candidate_window(rc, fusion_cfg) for rc in kept]
    windows.sort(key=lambda w: w.start)

    status = "budget_exceeded" if budget_exceeded else "ok"
    summary = _summary(
        cfg, avail, avail_invalid, norm, clusters=len(clusters),
        clusters_dropped=clusters_dropped, unprocessed=unprocessed,
        patterns=patterns_matched, contradictions=contradictions_count,
        suppressed=suppressed_records, cands=windows, capped=capped,
        started=started, status=status,
    )
    return MultimodalFusionResult(windows=windows, summary=summary)


def _summary(
    cfg: MultimodalConfig,
    avail: dict[str, str],
    avail_invalid: int,
    norm: NormalizationResult,
    *,
    clusters: int,
    clusters_dropped: int,
    unprocessed: int,
    patterns: int,
    contradictions: int,
    suppressed: list[dict[str, Any]],
    cands: list[CandidateWindow],
    capped: int,
    started: float,
    status: str,
) -> dict[str, Any]:
    """The engine summary: honest counts of everything seen/kept/dropped.
    Records are capped (first 8) — the counts are always exact."""
    return {
        "version": cfg.confidence_version,
        "status": status,
        "availability": avail,
        "availability_invalid_dropped": avail_invalid,
        "signals_in": len(norm.signals) + len(norm.quarantined),
        "signals_used": len(norm.signals),
        "quarantined": len(norm.quarantined),
        "quarantined_records": norm.quarantined[:8],
        "input_truncated": norm.truncated,
        "clusters": clusters,
        "clusters_dropped": clusters_dropped,
        "clusters_unprocessed": unprocessed,
        "candidates": len(cands),
        "suppressed": len(suppressed),
        "suppressed_records": suppressed[:8],
        "candidates_capped": capped,
        "patterns_matched": patterns,
        "contradictions": contradictions,
        "budget_exceeded": status == "budget_exceeded",
        "processing_seconds": round(time.monotonic() - started, 3),
    }


@dataclass
class MultimodalFusionEngine:
    """Thin named facade over :func:`fuse_multimodal` (the pipeline holds
    one per job: the validated config + the availability snapshot)."""

    config: MultimodalConfig
    availability: dict[str, str] = field(default_factory=dict)

    def fuse(
        self,
        events: list[SignalEvent],
        *,
        job_id: str,
        total_duration: float,
        fusion_cfg: FusionConfig,
    ) -> MultimodalFusionResult:
        return fuse_multimodal(
            events,
            job_id=job_id,
            total_duration=total_duration,
            fusion_cfg=fusion_cfg,
            cfg=self.config,
            availability=self.availability,
        )


def derive_availability(
    *,
    facecam_meta: dict[str, Any],
    game_vision_meta: dict[str, Any],
    facecam_enabled: bool,
    game_vision_enabled: bool,
    facecam_event_count: int,
    game_vision_event_count: int,
) -> dict[str, str]:
    """Builds the per-family availability snapshot from the MEASURED
    modality summaries (never from import success):

    - audio/visual: the core passes always run at completion → available;
    - facecam/game_vision: from the honest summarize_* metadata
      ({} = disabled, available=False = unavailable with the recorded
      reason; available=True + 0 events = available_no_events).
    """
    # audio/visual are the CORE passes: at completion they were measured
    # on every chunk, so their state is "available" by construction.
    avail: dict[str, str] = {"audio": AVAIL_AVAILABLE,
                             "visual": AVAIL_AVAILABLE}

    if not facecam_enabled:
        avail["facecam"] = AVAIL_DISABLED
    elif not facecam_meta or not facecam_meta.get("available"):
        avail["facecam"] = AVAIL_UNAVAILABLE
    elif facecam_event_count == 0:
        avail["facecam"] = AVAIL_AVAILABLE_NO_EVENTS
    else:
        avail["facecam"] = AVAIL_AVAILABLE

    if not game_vision_enabled:
        avail["game_vision"] = AVAIL_DISABLED
    elif not game_vision_meta or not game_vision_meta.get("available"):
        avail["game_vision"] = AVAIL_UNAVAILABLE
    elif game_vision_event_count == 0:
        avail["game_vision"] = AVAIL_AVAILABLE_NO_EVENTS
    else:
        avail["game_vision"] = AVAIL_AVAILABLE

    return avail
