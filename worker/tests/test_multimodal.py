"""TASK 010 — advanced multimodal fusion engine tests.

Deterministic, synthetic-only coverage of the five engine layers
(normalization → correlation → interpretation → confidence/provenance →
ranking) plus the pipeline integration:

- disabled mode must preserve the Phase 7 behavior EXACTLY (A/B test
  against an independent ``fuse()`` + ``select_clips()`` recomputation);
- enabled mode upgrades the SAME qualifying candidates (identical
  ``score_cluster`` bar) with heuristic confidence + provenance;
- evidence independence (repeated OCR / repeated landmarks / same-family
  siblings never count as extra modalities);
- budgets are ENFORCED (deterministic truncation + honest reporting).

No detector is run here (the engine is a pure function of stored
``SignalEvent`` rows) — real-detector regression is covered by the
TASK 007/008/009 suites, which must stay green.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

import app.intel.pipeline as pipeline_mod
from app.intel.checkpoints import IntelStore
from app.intel.fusion import FusionConfig, fuse
from app.intel.multimodal import (
    AVAIL_AVAILABLE,
    AVAIL_AVAILABLE_NO_EVENTS,
    AVAIL_DISABLED,
    AVAIL_UNAVAILABLE,
    CONFIDENCE_VERSION,
    MultimodalConfig,
    derive_availability,
    fuse_multimodal,
    normalize_availability,
)
from app.intel.multimodal.confidence import compute_confidence, detect_contradictions
from app.intel.multimodal.correlation import correlate
from app.intel.multimodal.evidence import (
    PROVENANCE_MAX_BYTES,
    build_provenance,
)
from app.intel.multimodal.event_graph import build_graph, extract_sequences
from app.intel.multimodal.ranking import (
    apply_candidate_cap,
    candidate_bounds,
    suppression_reason,
)
from app.intel.multimodal.sequences import match_patterns
from app.intel.multimodal.timeline import normalize_events
from app.intel.pipeline import PipelineConfig, StreamIntelPipeline
from app.intel.selection import select_clips
from app.intel.signals import SignalEvent, event_key
from conftest import _run_ffmpeg
from intel_fixtures import FakeSupabaseClient

JOB = str(UUID("01020304-0506-4708-990a-0b0c0d0e0f10"))
FC = FusionConfig()
MM = MultimodalConfig()


def ev(t: float, t_end: float, family: str, kind: str,
       strength: float = 0.8, **evd) -> SignalEvent:
    """A SignalEvent with the persistence-contract evidence embedded
    (family/kind/strength inside evidence — exactly what the detectors
    store, so from_row round-trips)."""
    evidence = {"family": family, "kind": kind, "strength": strength, **evd}
    return SignalEvent(t=t, t_end=t_end, family=family, kind=kind,
                       strength=strength, evidence=evidence)


def _norm(events, cfg=None):
    return normalize_events(events, JOB, (cfg or MM).max_events)


def _cluster(events, cfg=None):
    """The first temporal cluster of ``events`` (deterministic)."""
    cfg = cfg or MM
    clusters, _ = correlate(_norm(events, cfg).signals, cfg)
    return clusters[0]


def _analyze(cluster, cfg=None, availability=None, pattern=None):
    """Runs layers 3+4 for one cluster (the engine's per-cluster core)."""
    cfg = cfg or MM
    graph = build_graph(cluster, cfg)
    sequences = extract_sequences(cluster, graph, cfg, cfg.correlation_window_seconds)
    pattern = pattern if pattern is not None else match_patterns(cluster, cfg)
    contradictions = detect_contradictions(cluster)
    weights = {"audio": FC.audio_weight, "visual": FC.visual_weight,
               "facecam": FC.facecam_weight, "game_vision": FC.game_vision_weight}
    confidence = compute_confidence(cluster, cfg, weights, FC.diversity_bonus,
                                    pattern, contradictions)
    return graph, sequences, pattern, contradictions, confidence


# =============================================================================
# Layer 1 — signal normalization
# =============================================================================

def test_normalize_preserves_contract_and_order():
    e1 = ev(10.0, 10.5, "audio", "energy_spike", 0.7, engine="audio-features")
    e2 = ev(9.0, 9.3, "visual", "scene_change", 0.9, engine="ffmpeg-scene")
    result = normalize_events([e1, e2], JOB, MM.max_events)
    assert result.ok
    # deterministic time order (e2 first)
    assert [s.key for s in result.signals] == [event_key(JOB, e2), event_key(JOB, e1)]
    s = result.signals[1]
    # original values preserved, stable key, detector identity extracted
    assert s.t == 10.0 and s.t_end == 10.5 and s.strength == 0.7
    assert s.family == "audio" and s.kind == "energy_spike"
    assert s.key == event_key(JOB, e1)
    assert s.detector == "audio-features"
    # original evidence referenced, never mutated
    assert s.original is e1
    assert e1.evidence["engine"] == "audio-features"


def test_normalize_quarantines_invalid_timestamps():
    # SignalEvent's constructor validates, so malformed rows can only
    # reach the engine boundary via rehydration bypassing __post_init__ —
    # construct valid, then corrupt (simulating untrusted stored data).
    good = ev(5.0, 5.5, "audio", "energy_spike")
    neg = ev(0.0, 0.5, "audio", "energy_spike")
    object.__setattr__(neg, "t", -1.0)
    inv = ev(1.0, 1.5, "audio", "energy_spike")
    object.__setattr__(inv, "t_end", 0.5)
    nan = ev(3.0, 3.5, "audio", "energy_spike")
    object.__setattr__(nan, "t", float("nan"))
    result = normalize_events([good, neg, inv, nan], JOB, MM.max_events)
    assert len(result.signals) == 1 and result.signals[0].key == event_key(JOB, good)
    assert len(result.quarantined) == 3
    assert sorted(q["reason"] for q in result.quarantined) == sorted([
        "timestamps violate 0 <= t <= t_end",
        "timestamps violate 0 <= t <= t_end",
        "non-finite timestamps",
    ])
    # the quarantine record must be JSON-safe (non-finite t → label)
    json.dumps(result.quarantined, allow_nan=False)


def test_normalize_quarantines_malformed_strength():
    good = ev(5.0, 5.5, "audio", "energy_spike", 0.5)
    high = ev(6.0, 6.2, "audio", "energy_spike", 0.9)
    object.__setattr__(high, "strength", 1.5)
    low = ev(7.0, 7.2, "audio", "energy_spike", 0.9)
    object.__setattr__(low, "strength", -0.1)
    nan = ev(8.0, 8.2, "audio", "energy_spike")
    object.__setattr__(nan, "strength", float("nan"))
    result = normalize_events([good, high, low, nan], JOB, MM.max_events)
    assert len(result.signals) == 1
    assert sorted(q["reason"] for q in result.quarantined) == sorted([
        "strength outside [0, 1]",
        "strength outside [0, 1]",
        "non-finite strength",
    ])


def test_normalize_quarantines_unknown_family_and_missing_kind():
    bad_family = ev(5.0, 5.5, "audio", "x")
    object.__setattr__(bad_family, "family", "not_a_family")
    missing_kind = ev(6.0, 6.2, "audio", "energy_spike")
    object.__setattr__(missing_kind, "kind", "")
    good = ev(7.0, 7.2, "audio", "energy_spike")
    result = normalize_events([bad_family, missing_kind, good], JOB, MM.max_events)
    assert len(result.signals) == 1
    assert "unknown family" in result.quarantined[0]["reason"]
    assert "missing kind" in result.quarantined[1]["reason"]


def test_normalize_deterministic_under_shuffled_input():
    events = [ev(i * 0.7, i * 0.7 + 0.2, fam, "energy_spike", 0.5 + (i % 5) * 0.1)
              for i, fam in enumerate(["audio", "visual", "facecam", "game_vision"] * 10)]
    shuffled = list(reversed(events))
    a = normalize_events(events, JOB, MM.max_events)
    b = normalize_events(shuffled, JOB, MM.max_events)
    assert [s.key for s in a.signals] == [s.key for s in b.signals]
    assert [s.t for s in a.signals] == sorted(s.t for s in a.signals)


def test_normalize_input_truncation_budget():
    cfg = replace(MM, max_events=100)
    events = [ev(i * 1.0, i * 1.0 + 0.1, "audio", "energy_spike", 0.5)
              for i in range(150)]
    result = normalize_events(events, JOB, cfg.max_events)
    assert result.truncated
    assert len(result.signals) == 100
    # deterministic: the EARLIEST 100 survive
    assert [s.t for s in result.signals] == [float(i) for i in range(100)]


def test_normalize_derives_quality_from_detector_measurements():
    ocr = ev(5.0, 5.3, "game_vision", "kill_feed_entry", 0.9,
             engine="tesseract", ocr_confidence=70, directly_observed=True,
             text="A > B")
    face = ev(6.0, 6.4, "facecam", "face_mouth_burst", 0.6, engine="mediapipe")
    result = normalize_events([ocr, face], JOB, MM.max_events)
    by_kind = {s.kind: s for s in result.signals}
    # OCR: measured detector confidence (0..100) → quality 0.70 (NOT the strength)
    assert by_kind["kill_feed_entry"].quality == 0.70
    assert by_kind["kill_feed_entry"].directly_observed is True
    # facecam: no separate detector confidence measured → quality = strength
    assert by_kind["face_mouth_burst"].quality == 0.6
    assert by_kind["face_mouth_burst"].directly_observed is None
    # original strength is NEVER overridden
    assert by_kind["kill_feed_entry"].strength == 0.9


def test_normalize_never_crashes_on_garbage():
    garbage = [None, object(), 42, ev(1.0, 2.0, "audio", "energy_spike", 0.5)]
    result = normalize_events(garbage, JOB, MM.max_events)  # must not raise
    assert len(result.signals) == 1
    assert len(result.quarantined) == 3
    # the whole summary must serialize (stored as jsonb)
    json.dumps(result.quarantined, allow_nan=False)


# =============================================================================
# Layer 2 — temporal correlation + evidence independence
# =============================================================================

def test_correlation_overlap_and_window_chaining():
    a = ev(100.0, 100.2, "visual", "scene_change")
    b = ev(100.1, 100.3, "game_vision", "kill_feed_entry", text="A > B")
    c = ev(102.4, 103.0, "audio", "energy_spike")          # chained within 2.5 s
    d = ev(120.0, 120.5, "audio", "transient")              # far away
    clusters, dropped = correlate(_norm([a, b, c, d]).signals, MM)
    assert dropped == 0 and len(clusters) == 2
    expected = {event_key(JOB, a), event_key(JOB, b), event_key(JOB, c)}
    assert {s.key for s in clusters[0].events} == expected
    assert clusters[0].start == 100.0 and clusters[1].start == 120.0


def test_correlation_non_overlapping_events_separate_clusters():
    a = ev(100.0, 100.2, "audio", "energy_spike")
    b = ev(103.0, 103.2, "visual", "scene_change")  # gap 2.8 > 2.5 window
    clusters, _ = correlate(_norm([a, b]).signals, MM)
    assert len(clusters) == 2


def test_correlation_absolute_chunk_offsets():
    # events straddling a chunk boundary keep ABSOLUTE times and cluster
    # across the boundary (ownership/dedup already ran upstream)
    a = ev(598.0, 598.5, "audio", "energy_spike")
    b = ev(600.5, 601.0, "visual", "scene_change")   # 2.0 s gap → same cluster
    c = ev(609.0, 609.5, "audio", "transient")       # far → new cluster
    clusters, _ = correlate(_norm([a, b, c]).signals, MM)
    assert len(clusters) == 2
    assert clusters[0].start == 598.0 and clusters[0].end >= 601.0


def test_exact_duplicate_key_suppressed():
    e = ev(100.0, 100.3, "game_vision", "kill_feed_entry", 0.85, text="A > B",
           ocr_confidence=88)
    e_dup = ev(100.0, 100.3, "game_vision", "kill_feed_entry", 0.85, text="A > B",
               ocr_confidence=88)  # same 0.1 s bucket + family + kind → same key
    assert event_key(JOB, e) == event_key(JOB, e_dup)
    c = _cluster([e, e_dup])
    assert c.duplicates_exact == 1
    assert c.duplicates_repeated == 0
    assert len(c.observations) == 1
    assert c.independent_modalities == {"game_vision"}


def test_same_kind_repeat_is_corroboration_not_independence():
    # two OCR samples of the SAME persistent kill-feed line (same content)
    a = ev(100.0, 100.4, "game_vision", "kill_feed_entry", 0.85, text="A > B",
           ocr_confidence=88, engine="tesseract")
    b = ev(100.2, 100.6, "game_vision", "kill_feed_entry", 0.80, text="A > B",
           ocr_confidence=82, engine="tesseract")
    c = _cluster([a, b])
    assert c.duplicates_repeated == 1
    assert len(c.observations) == 1
    # max strength kept
    assert c.observations[0].signal.strength == 0.85
    assert c.independent_modalities == {"game_vision"}
    assert c.independent_observations == {("game_vision", "kill_feed_entry")}


def test_different_ocr_text_is_not_corroboration():
    # two DIFFERENT readings within the merge gap = two observations
    a = ev(100.0, 100.4, "game_vision", "match_result_text", 0.8,
           text="Victory", ocr_confidence=90, engine="tesseract")
    b = ev(100.4, 100.8, "game_vision", "match_result_text", 0.7,
           text="Defeat", ocr_confidence=70, engine="tesseract")
    c = _cluster([a, b])
    assert c.duplicates_repeated == 0
    assert len(c.observations) == 2
    assert c.independent_modalities == {"game_vision"}  # still ONE modality
    assert len(c.independent_observations) == 1          # same (family, kind)
    # and they ARE visible to contradiction detection
    assert detect_contradictions(c)


def test_same_family_different_kind_is_one_modality():
    # loud burst + audio-derived excitement from the SAME detector family
    a = ev(100.0, 100.5, "audio", "transient", 0.9)
    b = ev(100.3, 102.0, "audio", "energy_spike", 0.8)
    c = _cluster([a, b])
    assert c.independent_modalities == {"audio"}
    assert c.independent_observations == {("audio", "transient"),
                                          ("audio", "energy_spike")}


def test_repeated_facecam_landmarks_never_inflate():
    # one continuous reaction: three mouth bursts + one head motion
    events = [ev(100.0 + i * 0.3, 100.0 + i * 0.3 + 0.2, "facecam",
                 "face_mouth_burst", 0.6, engine="mediapipe") for i in range(3)]
    events.append(ev(101.0, 101.4, "facecam", "face_head_motion", 0.5,
                     engine="mediapipe"))
    c = _cluster(events)
    assert c.independent_modalities == {"facecam"}
    # three mouth bursts within the merge gap → ONE observation
    assert c.duplicates_repeated == 2
    assert len(c.observations) == 2
    assert c.independent_observations == {("facecam", "face_mouth_burst"),
                                          ("facecam", "face_head_motion")}


def test_cross_family_independent_modalities():
    events = [ev(100.0, 100.2, "visual", "scene_change", 0.7),
              ev(100.1, 100.3, "game_vision", "kill_feed_entry", 0.85, text="A > B"),
              ev(100.5, 102.5, "audio", "energy_spike", 0.9),
              ev(101.0, 101.5, "facecam", "face_mouth_burst", 0.6)]
    c = _cluster(events)
    assert c.independent_modalities == {"visual", "game_vision", "audio", "facecam"}
    assert len(c.independent_observations) == 4


def test_cluster_node_cap_overflow_is_not_independent():
    cfg = replace(MM, max_signals_per_cluster=5)
    # 5 distinct audio observations (alternating kinds, > merge gap apart)
    # hit the cap; a 6th signal of a NEW family must become overflow —
    # kept for scoring, but never independent.
    events = [ev(100.0 + i * 1.5, 100.0 + i * 1.5 + 0.2, "audio",
                 "energy_spike" if i % 2 else "transient", 0.5)
              for i in range(5)]
    events.append(ev(107.5, 107.7, "facecam", "face_mouth_burst", 0.5))
    clusters, _ = correlate(_norm(events, cfg).signals, cfg)
    c = clusters[0]
    assert len(c.observations) == 5
    assert len(c.overflow) == 1 and c.overflow[0].family == "facecam"
    # overflow never grows independence
    assert c.independent_modalities == {"audio"}
    # ... but it stays part of the scoring multiset (events + overflow)
    assert len(c.events + c.overflow) == 6


def test_max_clusters_truncation_reports_dropped():
    cfg = replace(MM, max_clusters=3)
    events = [ev(float(i * 30), float(i * 30) + 0.5, "audio", "energy_spike", 0.5)
              for i in range(10)]
    clusters, dropped = correlate(_norm(events, cfg).signals, cfg)
    assert len(clusters) == 3
    assert dropped == 7


def test_proximity_weights_bounded_and_deterministic():
    near = _cluster([ev(100.0, 100.2, "audio", "energy_spike", 0.8),
                     ev(100.1, 100.3, "visual", "scene_change", 0.8)])
    far = _cluster([ev(100.0, 100.1, "audio", "energy_spike", 0.8),
                    ev(102.4, 102.6, "visual", "scene_change", 0.8)])
    assert all(0.0 < w <= 1.0 for w in near.proximity_weights)
    assert all(0.0 < w <= 1.0 for w in far.proximity_weights)
    assert near.proximity_weights[0] > far.proximity_weights[0]


# =============================================================================
# Layer 3 — bounded event graph + sequences + patterns
# =============================================================================

def test_graph_is_bounded():
    cfg = replace(MM, max_signals_per_cluster=30, max_edges_per_node=4)
    # 35 distinct observations (alternating kinds, 2.0 s apart → always
    # beyond the same-kind merge gap) → the node cap must bite.
    events = [ev(100.0 + i * 2.0, 100.0 + i * 2.0 + 0.2,
                 "audio" if i % 2 else "visual",
                 "energy_spike" if i % 2 else "scene_change", 0.6)
              for i in range(35)]
    c = _cluster(events, cfg)
    graph = build_graph(c, cfg)
    assert len(graph.nodes) == cfg.max_signals_per_cluster  # cap enforced
    from collections import Counter
    per_node = Counter()
    for e in graph.edges:
        per_node[e.a] += 1
        per_node[e.b] += 1
    assert max(per_node.values()) <= cfg.max_edges_per_node * 2
    # relations are classified
    assert all(e.relation in ("overlap", "precedes", "follows") for e in graph.edges)


def test_sequences_distinct_kind_chain_with_tolerance():
    events = [ev(100.0, 100.2, "visual", "scene_change", 0.7),
              ev(100.5, 100.7, "game_vision", "kill_feed_entry", 0.85, text="A > B"),
              ev(101.2, 102.5, "audio", "energy_spike", 0.9),
              ev(102.0, 102.4, "facecam", "face_mouth_burst", 0.6)]
    c = _cluster(events)
    graph = build_graph(c, MM)
    seqs = extract_sequences(c, graph, MM, MM.correlation_window_seconds)
    assert ["scene_change", "kill_feed_entry", "energy_spike",
            "face_mouth_burst"] in seqs


def test_sequences_same_kind_extends_not_repeats():
    # a persistent kill-feed line observed over several samples = ONE stage
    events = [ev(100.0 + i * 0.3, 100.0 + i * 0.3 + 0.2, "game_vision",
                 "kill_feed_entry", 0.85, text="A > B") for i in range(3)]
    events.append(ev(101.5, 103.0, "audio", "energy_spike", 0.9))
    c = _cluster(events)
    graph = build_graph(c, MM)
    seqs = extract_sequences(c, graph, MM, MM.correlation_window_seconds)
    flat = [k for seq in seqs for k in seq]
    assert flat.count("kill_feed_entry") == 1
    assert flat.count("energy_spike") == 1


def test_sequences_caps():
    cfg = replace(MM, max_sequence_length=3, max_sequences_per_cluster=2)
    events = [ev(100.0 + i * 0.4, 100.0 + i * 0.4 + 0.1,
                 "audio" if i % 2 else "visual",
                 "energy_spike" if i % 2 else "scene_change", 0.6)
              for i in range(20)]
    c = _cluster(events, cfg)
    graph = build_graph(c, cfg)
    seqs = extract_sequences(c, graph, cfg, cfg.correlation_window_seconds)
    assert len(seqs) <= cfg.max_sequences_per_cluster
    assert all(len(s) <= cfg.max_sequence_length for s in seqs)


def test_elimination_pattern_full_chain():
    events = [ev(100.0, 100.4, "game_vision", "kill_feed_entry", 0.85,
                 text="A > B", ocr_confidence=90),
              ev(101.0, 103.0, "audio", "energy_spike", 0.9),
              ev(102.0, 102.5, "facecam", "face_mouth_burst", 0.6)]
    c = _cluster(events)
    m = match_patterns(c, MM)
    assert m is not None and m.pattern == "gameplay_elimination"
    assert [s.kind for s in m.stages] == ["kill_feed_entry", "energy_spike",
                                          "face_mouth_burst"]
    assert m.independent_modalities == 3


def test_high_impact_pattern():
    events = [ev(100.0, 100.2, "visual", "scene_change", 0.7),
              ev(100.5, 102.5, "audio", "energy_spike", 0.9),
              ev(101.0, 101.5, "facecam", "face_mouth_burst", 0.6)]
    c = _cluster(events)
    m = match_patterns(c, MM)
    assert m is not None and m.pattern == "high_impact_gameplay"
    assert m.independent_modalities >= 2


def test_match_victory_pattern():
    events = [ev(300.0, 300.5, "game_vision", "scoreboard_appearance", 0.5),
              ev(300.5, 300.9, "game_vision", "score_change", 0.8,
                 label="score", before="2", after="3", ocr_confidence=85),
              ev(301.0, 301.5, "game_vision", "match_result_text", 0.8,
                 text="Victory", ocr_confidence=92),
              ev(301.5, 303.0, "audio", "energy_spike", 0.9)]
    c = _cluster(events)
    m = match_patterns(c, MM)
    assert m is not None and m.pattern == "match_victory"
    assert m.independent_modalities >= 1


def test_unexpected_reaction_pattern():
    events = [ev(400.0, 400.6, "facecam", "face_mouth_burst", 0.8, engine="mediapipe"),
              ev(400.5, 402.0, "audio", "energy_spike", 0.9),
              ev(401.0, 401.2, "visual", "scene_change", 0.7)]
    c = _cluster(events)
    m = match_patterns(c, MM)
    assert m is not None and m.pattern == "unexpected_reaction"
    assert m.independent_modalities >= 2


def test_strong_audio_only_pattern():
    events = [ev(500.0, 502.0, "audio", "energy_spike", 0.9)]
    c = _cluster(events)
    m = match_patterns(c, MM)
    assert m is not None and m.pattern == "strong_audio_only"


def test_strong_audio_only_weak_is_rejected():
    events = [ev(500.0, 502.0, "audio", "energy_spike", 0.5)]
    c = _cluster(events)
    assert match_patterns(c, MM) is None


def test_pattern_requires_anchor():
    # no kill_feed / scene / result / face anchor → no elimination pattern
    events = [ev(500.0, 502.0, "audio", "energy_spike", 0.9)]
    c = _cluster(events)
    m = match_patterns(c, replace(MM, patterns=MM.patterns[:1]))  # elimination only
    assert m is None


def test_pattern_min_modality_floor():
    # kill feed ALONE: gameplay_elimination requires 2 independent modalities
    cfg = replace(MM, patterns=MM.patterns[:1])  # elimination only
    events = [ev(500.0, 500.4, "game_vision", "kill_feed_entry", 0.9,
                 text="A > B", ocr_confidence=95)]
    c = _cluster(events)
    assert match_patterns(c, cfg) is None


def test_one_best_pattern_deterministic():
    events = [ev(100.0, 100.2, "visual", "scene_change", 0.7),
              ev(100.1, 100.3, "game_vision", "kill_feed_entry", 0.85, text="A > B"),
              ev(100.5, 102.5, "audio", "energy_spike", 0.9),
              ev(101.0, 101.5, "facecam", "face_mouth_burst", 0.6)]
    c = _cluster(events)
    first = match_patterns(c, MM)
    second = match_patterns(c, MM)
    assert first is not None and first == second  # deterministic tie-break
