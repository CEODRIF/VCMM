"""Advanced multimodal fusion configuration (TASK 010).

Frozen, validated per-job configuration for the fusion ENGINE. The engine
is an OPTIONAL layer over the existing Phase 7 fusion: a ``None``
MultimodalConfig (the default) means the pipeline runs
``fusion.fuse()`` exactly as before — zero overhead, zero behavioral
change (verified by an A/B pipeline test).

Terminology (spec-mandated): the engine's output is
``heuristic_confidence`` — a bounded, deterministic, explainable
aggregate of measured evidence. It is NOT a statistically calibrated
probability of correctness (no annotated real-VOD evaluation exists yet;
that is TASK 013's scope).

Every limit here is ENFORCED (deterministic truncation + a
``budget_exceeded`` record), not aspirational.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Bump when the confidence formula changes shape (inputs/penalties).
#: Exposed in every provenance record so results are reproducible/auditable.
CONFIDENCE_VERSION = "mv1"


@dataclass(frozen=True)
class PatternConfig:
    """One cross-modal pattern: anchored by required kinds, supported by
    optional kinds, matched as an ORDERED sequence with per-step tolerance
    (detector timing uncertainty — exact order is NOT required beyond the
    tolerance). A pattern is a CANDIDATE INTERPRETATION, never ground truth.

    ``score_bonus`` defaults to 0.0 on purpose: matching a pattern must not
    arbitrarily raise a score just because more events exist. It affects
    heuristic confidence + provenance; operators may opt into a small score
    bonus explicitly.
    """

    name: str
    anchor_kinds: frozenset[str]          # at least one must be present
    step_kinds: tuple[frozenset[str], ...]  # ordered stages (each optional EXCEPT anchor)
    step_window_seconds: float = 3.0      # tolerance between consecutive stages
    min_independent_modalities: int = 1   # independence floor for the match
    confidence_bonus: float = 0.10        # bounded heuristic contribution
    score_bonus: float = 0.0              # 0 = patterns never inflate scores


@dataclass(frozen=True)
class MultimodalConfig:
    # -- temporal correlation ------------------------------------------
    #: Correlation window (s): events within this gap of a cluster's last
    #: end are considered temporally related (matches the Phase 7 fusion
    #: default of 2.5 s).
    correlation_window_seconds: float = 2.5
    #: Exponential decay constant (s) for temporal-proximity weighting:
    #: weight = exp(-gap / tau). Deterministic, bounded.
    decay_tau_seconds: float = 2.5
    #: A same-family observation inside this gap (s) of a same-kind one is
    #: treated as a repeat of the SAME physical signal (corroboration, not
    #: independence).
    same_kind_merge_seconds: float = 1.0
    # -- confidence ------------------------------------------------------
    confidence_version: str = CONFIDENCE_VERSION
    #: Floor for a candidate's heuristic confidence (below → suppressed).
    confidence_threshold: float = 0.30
    #: Minimum independent modality count for ANY candidate (1 = allow
    #: strong solo-modality candidates; patterns raise their own floors).
    min_independent_modalities: int = 1
    #: Single strong audio-only anchor that justifies a solo candidate
    #: (spec 6E) — below it, audio-only candidates need another modality.
    strong_audio_only_threshold: float = 0.80
    #: Low-quality observation threshold + its multiplicative penalty.
    low_quality_threshold: float = 0.40
    low_quality_penalty: float = 0.25
    #: Multiplicative penalty per detected contradiction (conservative set).
    contradiction_penalty: float = 0.15
    # -- candidate hygiene -----------------------------------------------
    pre_padding_seconds: float = 1.0      # added to the measured cluster span
    post_padding_seconds: float = 2.0
    #: Candidates supported ONLY by repeated observations of ONE family
    #: below this strength are suppressed (single-source repetition).
    single_source_min_strength: float = 0.60
    # -- hard budgets (enforced; deterministic truncation) ----------------
    max_events: int = 5000                # input timeline cap (time-ordered)
    max_clusters: int = 500               # correlation clusters
    max_signals_per_cluster: int = 64     # graph node cap
    max_edges_per_node: int = 8           # graph edge cap
    max_sequences_per_cluster: int = 8    # sequence extraction cap
    max_sequence_length: int = 4          # longest multi-stage sequence
    max_candidates: int = 200             # output candidate cap
    processing_budget_seconds: float = 30.0  # wall budget (checked between clusters)
    # -- cross-modal patterns (spec section 6; all conservative) -----------
    patterns: tuple[PatternConfig, ...] = field(default_factory=lambda: (
        PatternConfig(
            name="gameplay_elimination",
            anchor_kinds=frozenset({"kill_feed_entry"}),
            step_kinds=(frozenset({"scene_change", "motion_burst"}),
                        frozenset({"kill_feed_entry"}),
                        frozenset({"energy_spike", "voice_activity_change"}),
                        frozenset({"face_mouth_burst", "face_head_motion",
                                    "face_smile_geometry", "face_brow_activity"})),
            min_independent_modalities=2,
        ),
        PatternConfig(
            name="high_impact_gameplay",
            anchor_kinds=frozenset({"scene_change", "motion_burst"}),
            step_kinds=(frozenset({"scene_change", "motion_burst"}),
                        frozenset({"transient", "energy_spike"}),
                        frozenset({"face_mouth_burst", "face_head_motion",
                                   "kill_feed_entry", "score_change"})),
            min_independent_modalities=2,
        ),
        PatternConfig(
            name="match_victory",
            anchor_kinds=frozenset({"match_result_text"}),
            step_kinds=(frozenset({"scoreboard_appearance"}),
                        frozenset({"score_change"}),
                        frozenset({"match_result_text"}),
                        frozenset({"energy_spike", "voice_activity_change"})),
            min_independent_modalities=1,
        ),
        PatternConfig(
            name="unexpected_reaction",
            anchor_kinds=frozenset({"face_mouth_burst", "face_head_motion",
                                    "face_smile_geometry", "face_brow_activity"}),
            step_kinds=(frozenset({"face_mouth_burst", "face_head_motion",
                                   "face_smile_geometry", "face_brow_activity"}),
                        frozenset({"energy_spike", "voice_activity_change"}),
                        frozenset({"scene_change", "motion_burst",
                                   "kill_feed_entry"})),
            min_independent_modalities=2,
        ),
        PatternConfig(
            name="strong_audio_only",
            anchor_kinds=frozenset({"energy_spike", "voice_activity_change"}),
            step_kinds=(frozenset({"energy_spike", "voice_activity_change",
                                   "transient"}),),
            min_independent_modalities=1,
        ),
    ))

    def __post_init__(self) -> None:
        if self.correlation_window_seconds <= 0 or self.decay_tau_seconds <= 0:
            raise ValueError("correlation window and decay tau must be positive")
        if self.same_kind_merge_seconds < 0:
            raise ValueError("same_kind_merge_seconds must be >= 0")
        if not 0.0 <= self.confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must be within [0, 1]")
        if self.min_independent_modalities < 1:
            raise ValueError("min_independent_modalities must be >= 1")
        if not 0.0 <= self.strong_audio_only_threshold <= 1.0:
            raise ValueError("strong_audio_only_threshold must be within [0, 1]")
        if not 0.0 <= self.low_quality_threshold <= 1.0:
            raise ValueError("low_quality_threshold must be within [0, 1]")
        if not 0.0 < self.low_quality_penalty <= 1.0:
            raise ValueError("low_quality_penalty must be within (0, 1]")
        if not 0.0 < self.contradiction_penalty <= 1.0:
            raise ValueError("contradiction_penalty must be within (0, 1]")
        if self.pre_padding_seconds < 0 or self.post_padding_seconds < 0:
            raise ValueError("padding must be >= 0")
        if not 0.0 <= self.single_source_min_strength <= 1.0:
            raise ValueError("single_source_min_strength must be within [0, 1]")
        for name in ("max_events", "max_clusters", "max_signals_per_cluster",
                     "max_edges_per_node", "max_sequences_per_cluster",
                     "max_sequence_length", "max_candidates"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")
        if self.processing_budget_seconds <= 0:
            raise ValueError("processing_budget_seconds must be positive")
        seen: set[str] = set()
        for p in self.patterns:
            if p.name in seen:
                raise ValueError(f"duplicate pattern name: {p.name}")
            seen.add(p.name)
            if not p.anchor_kinds:
                raise ValueError(f"pattern {p.name} needs anchor kinds")
            if p.step_window_seconds <= 0:
                raise ValueError(f"pattern {p.name} step window must be positive")
            if p.min_independent_modalities < 1:
                raise ValueError(f"pattern {p.name} modality floor must be >= 1")
            if not 0.0 <= p.confidence_bonus <= 0.5:
                raise ValueError(f"pattern {p.name} confidence_bonus within [0, 0.5]")
            if not 0.0 <= p.score_bonus <= 1.0:
                raise ValueError(f"pattern {p.name} score_bonus within [0, 1]")
