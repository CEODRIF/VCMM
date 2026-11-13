"""TASK 010 — confidence, provenance, ranking, engine budgets, pipeline A/B.

Companion to ``test_multimodal.py`` (layers 1-3 live there).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from app.intel.checkpoints import IntelStore
from app.intel.fusion import FusionConfig, fuse, score_cluster
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
from app.intel.signals import SignalEvent
from conftest import _run_ffmpeg
from intel_fixtures import FakeSupabaseClient

JOB = str(UUID("01020304-0506-4708-990a-0b0c0d0e0f10"))
FC = FusionConfig()
MM = MultimodalConfig()


def ev(t: float, t_end: float, family: str, kind: str,
       strength: float = 0.8, **evd) -> SignalEvent:
    evidence = {"family": family, "kind": kind, "strength": strength, **evd}
    return SignalEvent(t=t, t_end=t_end, family=family, kind=kind,
                       strength=strength, evidence=evidence)


def _norm(events, cfg=None):
    return normalize_events(events, JOB, (cfg or MM).max_events)


def _cluster(events, cfg=None):
    cfg = cfg or MM
    clusters, _ = correlate(_norm(events, cfg).signals, cfg)
    return clusters[0]


def _confidence(cluster, cfg=None, pattern=None):
    cfg = cfg or MM
    graph = build_graph(cluster, cfg)
    seqs = extract_sequences(cluster, graph, cfg, cfg.correlation_window_seconds)
    pattern = pattern if pattern is not None else match_patterns(cluster, cfg)
    contradictions = detect_contradictions(cluster)
    weights = {"audio": FC.audio_weight, "visual": FC.visual_weight,
               "facecam": FC.facecam_weight, "game_vision": FC.game_vision_weight}
    confidence = compute_confidence(cluster, cfg, weights, FC.diversity_bonus,
                                    pattern, contradictions)
    return confidence, pattern, contradictions


# =============================================================================
# Layer 4 — heuristic confidence
# =============================================================================

def test_confidence_bounded_and_deterministic():
    events = [ev(100.0, 100.2, "visual", "scene_change", 0.7),
              ev(100.1, 100.3, "game_vision", "kill_feed_entry", 0.85, text="A > B"),
              ev(100.5, 102.5, "audio", "energy_spike", 0.9),
              ev(101.0, 101.5, "facecam", "face_mouth_burst", 0.6)]
    c1, c2 = _cluster(events), _cluster(list(reversed(events)))
    a, _, _ = _confidence(c1)
    b, _, _ = _confidence(c2)
    assert 0.0 <= a.heuristic_confidence <= 1.0
    assert a.heuristic_confidence == b.heuristic_confidence
    assert a.to_dict() == b.to_dict()          # full calc identical
    assert a.version == CONFIDENCE_VERSION == "mv1"


def test_confidence_independence_levels():
    # 1 modality → factor 1.0 (no free credit)
    solo = _cluster([ev(100.0, 101.0, "audio", "energy_spike", 0.8)])
    c_solo, _, _ = _confidence(solo)
    assert c_solo.calc["independence"] == {"modalities": 1, "factor": 1.0}
    # 2 modalities → 1 + 0.25/2
    two = _cluster([ev(100.0, 101.0, "audio", "energy_spike", 0.8),
                    ev(100.1, 100.3, "visual", "scene_change", 0.8)])
    c_two, _, _ = _confidence(two)
    assert c_two.calc["independence"] == {"modalities": 2, "factor": 1.125}
    # 3 modalities → 1 + 0.25 (bounded)
    three = _cluster([ev(100.0, 101.0, "audio", "energy_spike", 0.8),
                      ev(100.1, 100.3, "visual", "scene_change", 0.8),
                      ev(100.2, 100.5, "facecam", "face_mouth_burst", 0.8)])
    c_three, _, _ = _confidence(three)
    assert c_three.calc["independence"] == {"modalities": 3, "factor": 1.25}
    assert c_solo.heuristic_confidence < c_two.heuristic_confidence < c_three.heuristic_confidence


def test_confidence_repeated_evidence_never_inflates():
    base_events = [ev(100.0, 100.4, "game_vision", "kill_feed_entry", 0.85,
                      text="A > B", ocr_confidence=88, engine="tesseract"),
                   ev(100.5, 102.5, "audio", "energy_spike", 0.9)]
    repeated = base_events + [
        ev(100.2, 100.6, "game_vision", "kill_feed_entry", 0.85,
           text="A > B", ocr_confidence=88, engine="tesseract"),
        ev(100.4, 100.8, "game_vision", "kill_feed_entry", 0.85,
           text="A > B", ocr_confidence=88, engine="tesseract"),
    ]
    a, _, _ = _confidence(_cluster(base_events))
    b, _, _ = _confidence(_cluster(repeated))
    # repeats must NEVER inflate confidence (a repeated low-quality read
    # can only hold the mean quality steady or pull it down)
    assert b.heuristic_confidence <= a.heuristic_confidence + 1e-9
    assert a.calc["independence"] == b.calc["independence"]
    # the repeats ARE recorded (nothing silently discarded)
    assert _cluster(repeated).duplicates_repeated == 2


def test_confidence_contradiction_penalty_exposed():
    events = [ev(200.0, 200.5, "game_vision", "match_result_text", 0.8,
                 text="Victory", ocr_confidence=90),
              ev(200.4, 200.9, "game_vision", "match_result_text", 0.7,
                 text="Defeat", ocr_confidence=70),
              ev(200.5, 202.5, "audio", "energy_spike", 0.8)]
    c = _cluster(events)
    with_pen, _, contradictions = _confidence(c)
    assert len(contradictions) == 1
    penalties = {p["type"]: p for p in with_pen.calc["penalties"]}
    assert "contradiction" in penalties
    assert penalties["contradiction"]["multiplier"] == 1.0 - MM.contradiction_penalty
    # the same cluster without the conflicting reading scores higher
    clean = _cluster([e for e in events if "Victory" in str(e.evidence.get("text", "")) or e.family == "audio"])
    clean_conf, _, clean_contra = _confidence(clean)
    assert clean_contra == []
    assert clean_conf.heuristic_confidence > with_pen.heuristic_confidence


def test_confidence_low_quality_penalty_exposed():
    low = _cluster([ev(100.0, 100.3, "game_vision", "kill_feed_entry", 0.9,
                       text="A > B", ocr_confidence=20),
                    ev(100.5, 102.0, "audio", "energy_spike", 0.3)])
    c_low, _, _ = _confidence(low)
    types = {p["type"] for p in c_low.calc["penalties"]}
    assert "low_quality" in types
    assert c_low.calc["quality"]["mean"] < MM.low_quality_threshold


def test_confidence_preserves_detector_strengths():
    events = [ev(100.0, 100.2, "visual", "scene_change", 0.71),
              ev(100.5, 102.5, "audio", "energy_spike", 0.88)]
    c = _cluster(events)
    conf, _, _ = _confidence(c)
    assert conf.calc["family_max_strengths"] == {"audio": 0.88, "visual": 0.71}
    # base = weighted mean over present families (weights equal → mean)
    assert conf.calc["base"] == pytest.approx(0.795, abs=1e-9)


def test_confidence_version_exposed_everywhere():
    events = [ev(100.0, 102.0, "audio", "energy_spike", 0.9)]
    result = fuse_multimodal(events, job_id=JOB, total_duration=600.0, fusion_cfg=FC)
    assert result.summary["version"] == CONFIDENCE_VERSION
    w = result.windows[0]
    assert w.reasons["multimodal"]["version"] == CONFIDENCE_VERSION
    assert w.reasons["multimodal"]["confidence"]["version"] == CONFIDENCE_VERSION


def test_confidence_availability_never_negative_evidence():
    events = [ev(100.0, 100.2, "visual", "scene_change", 0.8),
              ev(100.5, 102.5, "audio", "energy_spike", 0.9)]
    a = fuse_multimodal(events, job_id=JOB, total_duration=600.0, fusion_cfg=FC,
                        availability={"facecam": AVAIL_UNAVAILABLE,
                                      "game_vision": AVAIL_DISABLED})
    b = fuse_multimodal(events, job_id=JOB, total_duration=600.0, fusion_cfg=FC)
    assert a.windows[0].confidence == b.windows[0].confidence
    # the availability is RECORDED (explainable), not penalized
    assert a.windows[0].reasons["multimodal"]["availability"]["facecam"] == AVAIL_UNAVAILABLE
    assert b.windows[0].reasons["multimodal"]["availability"] == {}


def test_confidence_proximity_decay():
    near = _cluster([ev(100.0, 100.2, "audio", "energy_spike", 0.8),
                     ev(100.1, 100.3, "visual", "scene_change", 0.8)])
    far = _cluster([ev(100.0, 100.1, "audio", "energy_spike", 0.8),
                    ev(102.4, 102.6, "visual", "scene_change", 0.8)])
    c_near, _, _ = _confidence(near)
    c_far, _, _ = _confidence(far)
    assert c_near.calc["proximity"]["factor"] > c_far.calc["proximity"]["factor"]
    assert c_near.heuristic_confidence > c_far.heuristic_confidence


# =============================================================================
# Layer 4b — evidence provenance
# =============================================================================

def _provenance(events, cfg=None, availability=None):
    cfg = cfg or MM
    c = _cluster(events, cfg)
    graph = build_graph(c, cfg)
    seqs = extract_sequences(c, graph, cfg, cfg.correlation_window_seconds)
    pattern = match_patterns(c, cfg)
    contradictions = detect_contradictions(c)
    weights = {"audio": FC.audio_weight, "visual": FC.visual_weight,
               "facecam": FC.facecam_weight, "game_vision": FC.game_vision_weight}
    confidence = compute_confidence(c, cfg, weights, FC.diversity_bonus,
                                    pattern, contradictions)
    return c, build_provenance(c, graph, seqs, pattern, contradictions,
                               confidence, cfg, availability or {})


def test_provenance_traceable_supporting():
    events = [ev(100.0, 100.2, "visual", "scene_change", 0.7, engine="ffmpeg-scene"),
              ev(100.1, 100.3, "game_vision", "kill_feed_entry", 0.85, text="A > B",
                 engine="tesseract", ocr_confidence=88),
              ev(100.5, 102.5, "audio", "energy_spike", 0.9)]
    c, prov = _provenance(events, availability={"facecam": AVAIL_UNAVAILABLE})
    keys = {s["key"] for s in prov["supporting"]}
    assert keys == {s.key for s in c.events}
    assert prov["supporting_total"] == len(prov["supporting"])
    entry = next(s for s in prov["supporting"] if s["kind"] == "kill_feed_entry")
    assert entry["strength"] == 0.85 and entry["t"] == 100.1 and entry["t_end"] == 100.3
    assert entry["detector"] == "tesseract" and entry["quality"] == 0.88
    assert prov["families"]["game_vision"]["detectors"] == ["tesseract"]
    assert prov["independent_modalities"] == ["audio", "game_vision", "visual"]
    assert prov["availability"]["facecam"] == AVAIL_UNAVAILABLE
    assert prov["confidence"]["version"] == CONFIDENCE_VERSION


def test_provenance_records_excluded_evidence():
    events = [ev(100.0, 100.4, "game_vision", "kill_feed_entry", 0.85, text="A > B",
                 ocr_confidence=88),
              ev(100.2, 100.6, "game_vision", "kill_feed_entry", 0.80, text="A > B",
                 ocr_confidence=82),
              ev(100.5, 102.0, "audio", "energy_spike", 0.9)]
    c, prov = _provenance(events)
    assert prov["duplicates"]["repeated"] == 1
    assert prov["duplicates"]["exact"] == 0
    assert prov["duplicates"]["overflow"] == 0
    # the corroborated observation is still in the supporting list
    assert prov["supporting_total"] == 3


def test_provenance_size_bound():
    # 200 distinct observations in one cluster → the cascade keeps the
    # serialized record under the hard byte cap.
    cfg = replace(MM, max_signals_per_cluster=200)
    events = [ev(100.0 + i * 0.2, 100.0 + i * 0.2 + 0.1,
                 ("audio", "visual", "facecam", "game_vision")[i % 4],
                 ("energy_spike", "scene_change", "face_mouth_burst",
                  "kill_feed_entry")[i % 4], 0.6)
              for i in range(200)]
    c, prov = _provenance(events, cfg=cfg)
    blob = json.dumps(prov).encode("utf-8")
    assert len(blob) <= PROVENANCE_MAX_BYTES
    # counts stay EXACT even when the list is truncated
    assert prov["supporting_total"] == len(c.events)
    assert prov["confidence"]["version"] == CONFIDENCE_VERSION


def test_provenance_json_serializable_strict():
    events = [ev(100.0, 100.2, "visual", "scene_change", 0.7),
              ev(100.5, 102.5, "audio", "energy_spike", 0.9)]
    c, prov = _provenance(events)
    json.dumps(prov, allow_nan=False, sort_keys=True)  # must not raise


def test_provenance_contradiction_recorded():
    events = [ev(200.0, 200.5, "game_vision", "match_result_text", 0.8,
                 text="Victory", ocr_confidence=90),
              ev(200.4, 200.9, "game_vision", "match_result_text", 0.7,
                 text="Defeat", ocr_confidence=70),
              ev(200.5, 202.5, "audio", "energy_spike", 0.8)]
    c, prov = _provenance(events)
    assert prov["contradictions"]
    assert prov["confidence"]["calc"]["penalties"]


# =============================================================================
# Layer 5 — candidate ranking hygiene
# =============================================================================

def test_candidate_padding_and_clamp_start():
    c = _cluster([ev(0.0, 0.5, "visual", "scene_change", 0.9)])
    start, end = candidate_bounds(c, MM, total_duration=100.0)
    assert start == 0.0                      # clamped (0 - 1.0 → 0)
    assert end == pytest.approx(2.5)         # 0.5 + 2.0 padding


def test_candidate_padding_and_clamp_end():
    c = _cluster([ev(99.5, 100.0, "visual", "scene_change", 0.9)])
    start, end = candidate_bounds(c, MM, total_duration=100.0)
    assert end == 100.0                      # clamped to source duration
    assert start == pytest.approx(98.5)


def test_single_source_repetition_suppressed():
    # one family, max strength 0.56 (< 0.60 floor) but score 28 (≥ 25)
    # and confidence 0.3136 (≥ 0.30) → only the single-source rule fires
    events = [ev(100.0, 101.0, "audio", "energy_spike", 0.56)]
    c = _cluster(events)
    score, _, _ = score_cluster([s.original for s in c.events + c.overflow], FC)
    assert score >= FC.min_score
    reason = suppression_reason(c, score=score, heuristic_confidence=0.56 ** 2,
                                fusion_cfg=FC, cfg=MM, total_duration=600.0)
    assert reason == "single_source_repetition"


def test_strong_single_source_allowed():
    events = [ev(100.0, 101.0, "audio", "energy_spike", 0.9)]
    result = fuse_multimodal(events, job_id=JOB, total_duration=600.0, fusion_cfg=FC)
    assert len(result.windows) == 1
    assert result.summary["suppressed"] == 0


def test_below_min_score_suppressed():
    events = [ev(100.0, 100.5, "audio", "energy_spike", 0.15),
              ev(100.1, 100.3, "visual", "scene_change", 0.15)]
    c = _cluster(events)
    reason = suppression_reason(c, score=18.75, heuristic_confidence=0.9,
                                fusion_cfg=FC, cfg=MM, total_duration=600.0)
    assert reason == "below_min_score"


def test_confidence_threshold_suppression():
    events = [ev(100.0, 100.5, "audio", "energy_spike", 0.5),
              ev(100.1, 100.3, "visual", "scene_change", 0.5)]
    result = fuse_multimodal(events, job_id=JOB, total_duration=600.0, fusion_cfg=FC,
                             cfg=replace(MM, confidence_threshold=0.99))
    assert result.windows == []
    assert result.summary["suppressed"] == 1
    assert result.summary["suppressed_records"][0]["reason"] == "below_confidence_threshold"


def test_min_independent_modalities_floor():
    cfg = replace(MM, min_independent_modalities=2)
    events = [ev(100.0, 101.0, "audio", "energy_spike", 0.9)]
    result = fuse_multimodal(events, job_id=JOB, total_duration=600.0, fusion_cfg=FC, cfg=cfg)
    assert result.windows == []
    assert result.summary["suppressed_records"][0]["reason"] == \
        "insufficient_independent_modalities"


def test_max_candidates_cap_deterministic():
    cfg = replace(MM, max_candidates=3)
    events = []
    for i in range(10):
        base = 100.0 + i * 30.0
        m = 0.6 + i * 0.03  # every cluster passes score + confidence floors
        events.append(ev(base, base + 0.2, "visual", "scene_change", m))
        events.append(ev(base + 0.5, base + 2.0, "audio", "energy_spike", m))
    result = fuse_multimodal(events, job_id=JOB, total_duration=600.0,
                             fusion_cfg=FC, cfg=cfg)
    assert len(result.windows) == 3
    assert result.summary["candidates_capped"] == 7
    # kept = the three STRONGEST (highest score, then earliest start);
    # output is re-sorted in time order for downstream selection
    scores = [w.score for w in result.windows]
    assert max(scores) == pytest.approx(100.0)  # strong clusters hit the cap
    starts = [w.start for w in result.windows]
    assert starts == sorted(starts)
    # the strongest cluster (last one, start 370) must survive the cap
    assert any(w.start >= 369.0 for w in result.windows)


def test_max_candidates_cap_keeps_strongest():
    cfg = replace(MM, max_candidates=1)
    events = [ev(100.0, 100.2, "visual", "scene_change", 0.7),
              ev(100.5, 102.0, "audio", "energy_spike", 0.7),
              ev(200.0, 200.2, "visual", "scene_change", 0.95),
              ev(200.5, 202.0, "audio", "energy_spike", 0.95)]
    result = fuse_multimodal(events, job_id=JOB, total_duration=600.0,
                             fusion_cfg=FC, cfg=cfg)
    assert len(result.windows) == 1
    assert result.summary["candidates_capped"] == 1
    assert result.windows[0].start >= 199.0  # the strong cluster survived


# =============================================================================
# Engine-level: budgets, availability, robustness, determinism
# =============================================================================

def test_engine_large_synthetic_timeline():
    # 3000 isolated events over 2.5 hours (deterministic, no randomness):
    # each its own cluster (3.0 s apart > 2.5 s window) → the cluster cap
    # (500) MUST bite, and the bookkeeping must stay exact:
    # analyzed clusters == kept candidates + suppressed + capped.
    events = [ev(i * 3.0, i * 3.0 + 0.2,
                 "audio" if i % 2 else "visual",
                 "energy_spike" if i % 2 else "scene_change",
                 0.4 + (i % 10) * 0.05)
              for i in range(3000)]
    import time
    t0 = time.monotonic()
    result = fuse_multimodal(events, job_id=JOB, total_duration=9000.0,
                             fusion_cfg=FC, cfg=MM)
    elapsed = time.monotonic() - t0
    assert elapsed < 15.0
    s = result.summary
    assert s["signals_in"] == 3000 and s["signals_used"] == 3000
    assert s["clusters"] == MM.max_clusters                      # enforced cap
    assert s["clusters_dropped"] == 3000 - MM.max_clusters       # honest report
    assert s["candidates"] <= MM.max_candidates
    assert len(result.windows) == s["candidates"]
    # exact bookkeeping over the analyzed clusters
    assert (s["candidates"] + s["suppressed"] + s["candidates_capped"]
            == s["clusters"])


def test_engine_input_truncation_reported():
    cfg = replace(MM, max_events=500)
    events = [ev(float(i), float(i) + 0.1, "audio", "energy_spike", 0.5)
              for i in range(600)]
    result = fuse_multimodal(events, job_id=JOB, total_duration=600.0,
                             fusion_cfg=FC, cfg=cfg)
    assert result.summary["input_truncated"] is True
    assert result.summary["signals_used"] == 500


def test_engine_processing_budget_reported(monkeypatch):
    """The wall budget must make the engine STOP partway, keep what it
    analyzed, and report the rest — verified DETERMINISTICALLY with an
    injected clock that jumps 100 s after the first cluster."""
    import app.intel.multimodal as mm_mod
    import time as _time

    cfg = replace(MM, processing_budget_seconds=30.0)
    events = [ev(float(i * 30), float(i * 30) + 0.5, "audio", "energy_spike", 0.9)
              for i in range(10)]
    real = _time.monotonic
    state = {"n": 0}

    def fake_clock():
        state["n"] += 1
        # first two reads (engine start + first pre-cluster check): real time
        return real() + max(0, state["n"] - 2) * 100.0

    monkeypatch.setattr(mm_mod.time, "monotonic", fake_clock)
    result = fuse_multimodal(events, job_id=JOB, total_duration=9000.0,
                             fusion_cfg=FC, cfg=cfg)
    s = result.summary
    assert s["status"] == "budget_exceeded"
    assert s["budget_exceeded"] is True
    assert s["clusters"] == 10
    assert s["clusters_unprocessed"] == 9  # exactly one cluster analyzed
    # whatever WAS analyzed stays valid: exact bookkeeping, time-ordered
    assert (s["candidates"] + s["suppressed"] + s["candidates_capped"]
            == s["clusters"] - s["clusters_unprocessed"])
    starts = [w.start for w in result.windows]
    assert starts == sorted(starts)


def test_engine_no_events():
    result = fuse_multimodal([], job_id=JOB, total_duration=600.0, fusion_cfg=FC)
    assert result.windows == []
    assert result.summary["status"] == "no_events"
    assert result.summary["candidates"] == 0


def test_engine_all_modalities_unavailable():
    availability = {f: AVAIL_UNAVAILABLE for f in ("audio", "visual", "facecam", "game_vision")}
    result = fuse_multimodal([], job_id=JOB, total_duration=600.0, fusion_cfg=FC,
                             cfg=MM, availability=availability)
    assert result.windows == []
    assert result.summary["availability"] == availability
    # honest: availability is reported, nothing is fabricated
    json.dumps(result.summary, allow_nan=False)


def test_engine_availability_subsets():
    subsets = [
        {"audio": AVAIL_AVAILABLE, "visual": AVAIL_AVAILABLE,
         "facecam": AVAIL_DISABLED, "game_vision": AVAIL_DISABLED},
        {"audio": AVAIL_AVAILABLE_NO_EVENTS, "visual": AVAIL_AVAILABLE_NO_EVENTS,
         "facecam": AVAIL_UNAVAILABLE, "game_vision": AVAIL_AVAILABLE},
        {"audio": AVAIL_AVAILABLE, "visual": AVAIL_AVAILABLE,
         "facecam": AVAIL_AVAILABLE, "game_vision": AVAIL_UNAVAILABLE},
    ]
    events = [ev(100.0, 100.2, "visual", "scene_change", 0.7),
              ev(100.5, 102.5, "audio", "energy_spike", 0.9),
              ev(101.0, 101.5, "facecam", "face_mouth_burst", 0.6)]
    for avail in subsets:
        result = fuse_multimodal(events, job_id=JOB, total_duration=600.0,
                                 fusion_cfg=FC, cfg=MM, availability=avail)
        assert result.summary["availability"] == avail
        assert len(result.windows) == 1
        assert result.windows[0].reasons["multimodal"]["availability"] == avail


def test_normalize_availability_rejects_unknown():
    out, dropped = normalize_availability(
        {"audio": AVAIL_AVAILABLE, "hologram": "available",
         "facecam": "vibes"})
    assert out == {"audio": AVAIL_AVAILABLE}
    assert dropped == 2


def test_derive_availability_states():
    # disabled
    a = derive_availability(facecam_meta={}, game_vision_meta={},
                            facecam_enabled=False, game_vision_enabled=False,
                            facecam_event_count=0, game_vision_event_count=0)
    assert a["facecam"] == AVAIL_DISABLED and a["game_vision"] == AVAIL_DISABLED
    # unavailable (configured but measured unavailable)
    b = derive_availability(facecam_meta={"available": False, "reason": "no model"},
                           game_vision_meta={}, facecam_enabled=True,
                           game_vision_enabled=False,
                           facecam_event_count=0, game_vision_event_count=0)
    assert b["facecam"] == AVAIL_UNAVAILABLE
    # available with no events (ran, measured, nothing detected)
    c = derive_availability(facecam_meta={"available": True, "chunks_analyzed": 2},
                           game_vision_meta={"available": True},
                           facecam_enabled=True, game_vision_enabled=True,
                           facecam_event_count=0, game_vision_event_count=0)
    assert c["facecam"] == AVAIL_AVAILABLE_NO_EVENTS
    assert c["game_vision"] == AVAIL_AVAILABLE_NO_EVENTS
    # available with events
    d = derive_availability(facecam_meta={"available": True}, game_vision_meta={},
                           facecam_enabled=True, game_vision_enabled=True,
                           facecam_event_count=3, game_vision_event_count=0)
    assert d["facecam"] == AVAIL_AVAILABLE
    assert d["audio"] == AVAIL_AVAILABLE and d["visual"] == AVAIL_AVAILABLE


def test_engine_determinism_across_runs():
    events = [ev(100.0 + i * 0.3, 100.0 + i * 0.3 + 0.2,
                 ("audio", "visual", "facecam", "game_vision")[i % 4],
                 ("energy_spike", "scene_change", "face_mouth_burst",
                  "kill_feed_entry")[i % 4], 0.5 + (i % 3) * 0.1)
              for i in range(40)]
    r1 = fuse_multimodal(events, job_id=JOB, total_duration=600.0, fusion_cfg=FC, cfg=MM)
    r2 = fuse_multimodal(list(reversed(events)), job_id=JOB, total_duration=600.0,
                         fusion_cfg=FC, cfg=MM)

    def sig(r):
        return [
            (w.start, w.end, w.score, w.confidence, w.families,
             tuple(sorted(json.dumps(w.reasons, sort_keys=True).encode())))
            for w in r.windows
        ]

    assert sig(r1) == sig(r2)
    s1, s2 = dict(r1.summary), dict(r2.summary)
    s1.pop("processing_seconds"); s2.pop("processing_seconds")
    assert s1 == s2


def test_engine_never_raises_on_malformed_inputs():
    bad = ev(1.0, 2.0, "audio", "energy_spike", 0.5)
    object.__setattr__(bad, "strength", float("inf"))
    events = [None, object(), 42, bad,
              ev(100.0, 100.2, "visual", "scene_change", 0.7),
              ev(100.5, 102.5, "audio", "energy_spike", 0.9)]
    result = fuse_multimodal(events, job_id=JOB, total_duration=600.0,
                             fusion_cfg=FC, cfg=MM)  # must not raise
    assert result.summary["quarantined"] == 4
    assert len(result.windows) == 1
    json.dumps(result.summary, allow_nan=False)  # jsonb-safe


# =============================================================================
# Pipeline integration: disabled = EXACTLY Phase 7; enabled = provenance on
# the same qualifying candidates; resume determinism; shape compatibility.
#
# Scripted deterministic source (real FFmpeg decode, same recipe as the
# TASK 007 pipeline tests): 8 s; quiet 440 Hz bed + noise burst at 6.0-6.3
# + hard video cut at 6.2 → audio + visual events around t≈6.
# =============================================================================

PIP_JOB = str(UUID("11111111-1111-4111-8111-111111111111"))
TOTAL = 8.0
CUT_AT = 6.2


@pytest.fixture(scope="module")
def mm_source(ffmpeg_bin, tmp_path_factory) -> Path:
    out_dir = tmp_path_factory.mktemp("mm-source")
    src = out_dir / "stream.mp4"
    _run_ffmpeg(
        ffmpeg_bin,
        ["-f", "lavfi", "-i", f"testsrc=duration={CUT_AT}:size=320x240:rate=10",
         "-f", "lavfi", "-i", f"color=c=red:d={TOTAL - CUT_AT}:s=320x240:r=10",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={TOTAL}",
         "-f", "lavfi", "-i", "anoisesrc=d=0.3:color=white:amplitude=0.4",
         "-filter_complex",
         f"[0:v][1:v]concat=n=2:v=1:a=0[vid];"
         f"[2:a]volume=0.05[a0];[3:a]adelay=6000:all=1[a1];"
         f"[a0][a1]amix=inputs=2:normalize=0[aout]",
         "-map", "[vid]", "-map", "[aout]",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(src)],
    )
    return src


def _pip_config(chunks_per_claim: int = 10, multimodal=None) -> PipelineConfig:
    return PipelineConfig(chunk_seconds=3, overlap_seconds=1,
                          chunks_per_claim=chunks_per_claim,
                          chunk_decode_timeout_seconds=120.0,
                          visual_pass_timeout_seconds=120.0,
                          requested_clips=5, multimodal=multimodal)


def _candidates_sig(cands) -> list[tuple]:
    return [(round(c.start, 3), round(c.end, 3), round(c.score, 2),
             c.category, round(c.confidence, 4)) for c in cands]


def test_pipeline_disabled_is_exact_phase7_reference(ffmpeg_bin, mm_source, tmp_path):
    """Disabled mode (the DEFAULT) must reproduce Phase 7 behavior EXACTLY:
    the candidates must equal an INDEPENDENT recomputation (fuse() +
    select_clips()) from the same stored events, and no multimodal traces
    may exist anywhere (evidence shape, result metadata)."""
    client = FakeSupabaseClient()
    pipe = StreamIntelPipeline(ffmpeg_bin, IntelStore(client), _pip_config())
    result = asyncio.run(pipe.run(PIP_JOB, mm_source, TOTAL, tmp_path / "off"))
    assert result.status == "complete"
    assert result.candidates, "the scripted burst+cut must yield candidates"
    assert result.multimodal == {}                      # engine never ran

    # independent recomputation from the same stored evidence
    silence_gaps = []
    for feats in result.features_summaries:
        for g in feats.get("silence_gaps") or []:
            silence_gaps.append((float(g[0]), float(g[1])))
    silence_gaps.sort()
    reference = select_clips(
        fuse(result.events, _pip_config().fusion),
        total_duration=TOTAL, silence_gaps=silence_gaps,
        requested_clips=5, cfg=_pip_config().selection,
    )
    assert _candidates_sig(reference) == _candidates_sig(result.candidates)
    # evidence shape: NO multimodal key (byte-compatible with Phase 7)
    for c in result.candidates:
        assert "multimodal" not in c.evidence
        assert set(c.to_dict()) == {"start", "end", "duration", "score",
                                    "confidence", "category", "evidence"}


def test_pipeline_enabled_provenance_and_summary(ffmpeg_bin, mm_source, tmp_path):
    """Enabled mode: the SAME qualifying candidates (unchanged score bar)
    carry heuristic confidence + size-bounded provenance; the job-level
    summary reports availability and honest counts."""
    client = FakeSupabaseClient()
    pipe = StreamIntelPipeline(
        ffmpeg_bin, IntelStore(client), _pip_config(multimodal=MultimodalConfig()))
    result = asyncio.run(pipe.run(PIP_JOB, mm_source, TOTAL, tmp_path / "on"))
    assert result.status == "complete"
    assert result.candidates, "same source → same qualifying candidates"

    # job-level engine summary: honest availability (facecam/game_vision
    # were disabled for this job) + exact counts
    s = result.multimodal
    assert s["version"] == CONFIDENCE_VERSION
    assert s["status"] in ("ok", "budget_exceeded")
    assert s["availability"] == {"audio": AVAIL_AVAILABLE,
                                 "visual": AVAIL_AVAILABLE,
                                 "facecam": AVAIL_DISABLED,
                                 "game_vision": AVAIL_DISABLED}
    assert s["signals_in"] == len(result.events)
    assert s["signals_used"] + s["quarantined"] == len(result.events)
    assert s["candidates"] + s["suppressed"] + s["candidates_capped"] == s["clusters"]
    # per-candidate provenance: traceable + versioned + bounded
    for c in result.candidates:
        prov = c.evidence.get("multimodal")
        assert prov, "enabled mode must attach provenance to every candidate"
        assert prov["version"] == CONFIDENCE_VERSION
        assert prov["confidence"]["version"] == CONFIDENCE_VERSION
        assert prov["independent_modalities"]
        assert prov["supporting_total"] >= 1
        assert len(json.dumps(prov).encode()) <= PROVENANCE_MAX_BYTES
        # every supporting record carries the traceability fields
        for p in prov["supporting"]:
            assert {"key", "family", "kind", "t", "t_end", "strength"} <= set(p)
    # score parity with the unchanged Phase 7 scorer on the same events
    ph7_scores = sorted(w.score for w in fuse(result.events, FC) if w.qualifies)
    for c in result.candidates:
        assert c.score in ph7_scores or \
            any(abs(c.score - s) < 1e-9 for s in ph7_scores)


def test_pipeline_enabled_resume_determinism(ffmpeg_bin, mm_source, tmp_path):
    """Enabled mode is a pure function of the stored evidence: a run
    interrupted by the per-claim budget and resumed must produce the
    EXACT same candidates (incl. confidence) as one clean run."""
    clean_client = FakeSupabaseClient()
    clean = asyncio.run(
        StreamIntelPipeline(ffmpeg_bin, IntelStore(clean_client),
                            _pip_config(multimodal=MultimodalConfig())
                            ).run(PIP_JOB, mm_source, TOTAL, tmp_path / "clean"))
    assert clean.status == "complete"

    resume_client = FakeSupabaseClient()
    pipe = StreamIntelPipeline(ffmpeg_bin, IntelStore(resume_client),
                               _pip_config(chunks_per_claim=2,
                                           multimodal=MultimodalConfig()))
    r1 = asyncio.run(pipe.run(PIP_JOB, mm_source, TOTAL, tmp_path / "res"))
    assert r1.status == "incomplete" and r1.processed_chunks == 2
    r2 = asyncio.run(pipe.run(PIP_JOB, mm_source, TOTAL, tmp_path / "res"))
    assert r2.status == "complete" and r2.processed_chunks == 2

    assert _candidates_sig(r2.candidates) == _candidates_sig(clean.candidates)
    assert (r2.multimodal["version"], r2.multimodal["status"]) == \
           (clean.multimodal["version"], clean.multimodal["status"])
    # provenance fingerprint identical (the whole explainability payload)
    fp = lambda r: [
        json.dumps(c.evidence.get("multimodal"), sort_keys=True)
        for c in r.candidates]
    assert fp(r2) == fp(clean)


def test_selected_clip_to_dict_shape_compat(ffmpeg_bin, mm_source, tmp_path):
    """The frontend contract: SelectedClip.to_dict() keeps its exact key
    set; multimodal data only ever appears as a nested evidence key."""
    client = FakeSupabaseClient()
    pipe = StreamIntelPipeline(ffmpeg_bin, IntelStore(client),
                               _pip_config(multimodal=MultimodalConfig()))
    result = asyncio.run(pipe.run(PIP_JOB, mm_source, TOTAL, tmp_path / "shape"))
    assert result.status == "complete" and result.candidates
    for c in result.candidates:
        d = c.to_dict()
        assert set(d) == {"start", "end", "duration", "score", "confidence",
                          "category", "evidence"}
        # Phase 7 evidence keys all present, unchanged
        for key in ("why", "heuristic_category", "families",
                    "family_strengths", "event_kinds", "event_times"):
            assert key in c.evidence
        # the engine summary must not leak into the clip record itself
        assert isinstance(d["evidence"]["multimodal"], dict)
        assert "calc" not in d  # nothing engine-internal at the top level
