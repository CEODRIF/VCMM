"""Multimodal fusion (TASK 007) — from signal events to scored candidates.

The core rule (and its unit test): **a loud sound alone must not
qualify**. A single-family group is heavily discounted, and a group whose
ONLY audio evidence is a transient (game-SFX family — not a vocal
reaction) can never reach the qualification bar on its own. Qualification
comes from INDEPENDENT families agreeing in time (or a very strong
vocal-family signal), which is exactly the "reaction + event" structure
the product wants.

Scoring (fully deterministic, every number explainable from evidence):

    weighted  = Σ weight_f · max_strength_f / Σ weight_f   (over PRESENT
                families only — an absent family is NOT counted as zero,
                it is simply not there, and lowers confidence instead)
    score     = 100 · weighted
                · (1 + diversity_bonus)            when ≥ 2 families agree
                · solo_factor (0.5, or 0.2 for SFX-only transients)
                when a single family is present
    confidence = 0.5 · (number of present families)   (1 family → 0.5,
                2+ → 1.0)

Temporal structure is preserved by construction: a candidate window keeps
its member events in time order (event → reaction → aftermath), and
selection expands context AROUND that core instead of replacing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .signals import KIND_TRANSIENT, SignalEvent

DEFAULT_FUSION_WINDOW_SECONDS = 2.5
DEFAULT_AUDIO_WEIGHT = 0.5
DEFAULT_VISUAL_WEIGHT = 0.5
DEFAULT_DIVERSITY_BONUS = 0.25
DEFAULT_SOLO_FACTOR = 0.5
DEFAULT_SFX_ONLY_SOLO_FACTOR = 0.2
DEFAULT_MIN_SCORE = 25.0


@dataclass(frozen=True)
class FusionConfig:
    window_seconds: float = DEFAULT_FUSION_WINDOW_SECONDS
    audio_weight: float = DEFAULT_AUDIO_WEIGHT
    visual_weight: float = DEFAULT_VISUAL_WEIGHT
    diversity_bonus: float = DEFAULT_DIVERSITY_BONUS
    solo_factor: float = DEFAULT_SOLO_FACTOR
    sfx_only_solo_factor: float = DEFAULT_SFX_ONLY_SOLO_FACTOR
    min_score: float = DEFAULT_MIN_SCORE

    def __post_init__(self) -> None:
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if not (0 < self.audio_weight and 0 < self.visual_weight):
            raise ValueError("family weights must be positive")
        if not 0 <= self.min_score <= 100:
            raise ValueError("min_score must be within [0, 100]")


@dataclass
class CandidateWindow:
    """A time-ordered cluster of signals that may become a clip."""

    start: float
    end: float
    events: list[SignalEvent] = field(default_factory=list)
    score: float = 0.0
    confidence: float = 0.0
    families: tuple[str, ...] = ()
    family_strengths: dict[str, float] = field(default_factory=dict)
    reasons: dict[str, Any] = field(default_factory=dict)
    min_score: float = DEFAULT_MIN_SCORE  # the bar the scorer applied

    @property
    def qualifies(self) -> bool:
        return self.score >= self.min_score


def _cluster(events: list[SignalEvent], window_seconds: float) -> list[list[SignalEvent]]:
    """Greedy time clustering: an event joins the open cluster when it starts
    within ``window_seconds`` of the cluster's last event END (union-chain)."""
    if not events:
        return []
    ordered = sorted(events, key=lambda e: (e.t, e.t_end))
    clusters: list[list[SignalEvent]] = [[ordered[0]]]
    for event in ordered[1:]:
        last = clusters[-1][-1]
        if event.t <= last.t_end + window_seconds:
            clusters[-1].append(event)
        else:
            clusters.append([event])
    return clusters


def score_cluster(cluster: list[SignalEvent], cfg: FusionConfig) -> tuple[float, float, dict[str, Any]]:
    """Scores one cluster; returns (score, confidence, reasons)."""
    families: set[str] = set()
    strengths: dict[str, float] = {}
    for e in cluster:
        families.add(e.family)
        strengths[e.family] = max(strengths.get(e.family, 0.0), e.strength)

    weights = {"audio": cfg.audio_weight, "visual": cfg.visual_weight}
    present = [f for f in families if f in weights]
    weight_sum = sum(weights[f] for f in present) or 1.0
    weighted = sum(weights[f] * strengths[f] for f in present) / weight_sum

    # SFX-only group: the ONLY audio kinds are transients (game sound).
    sfx_only = (
        len(present) == 1
        and "audio" in present
        and all(e.kind == KIND_TRANSIENT for e in cluster if e.family == "audio")
    )

    if len(present) >= 2:
        score = 100.0 * weighted * (1.0 + cfg.diversity_bonus)
        solo = None
    elif sfx_only:
        score = 100.0 * weighted * cfg.sfx_only_solo_factor
        solo = "sfx_only_transient"
    else:
        score = 100.0 * weighted * cfg.solo_factor
        solo = "single_family"

    confidence = 0.5 * len(present)
    reasons = {
        "families": sorted(present),
        "family_strengths": {f: round(strengths[f], 4) for f in sorted(strengths)},
        "weighted": round(weighted, 4),
        "solo_mode": solo,
        "event_kinds": sorted({e.kind for e in cluster}),
    }
    return round(min(100.0, score), 2), round(confidence, 4), reasons


def fuse(events: list[SignalEvent], cfg: FusionConfig | None = None) -> list[CandidateWindow]:
    """Clusters + scores ALL signal events; returns QUALIFYING windows only."""
    cfg = cfg or FusionConfig()
    windows: list[CandidateWindow] = []
    for cluster in _cluster(events, cfg.window_seconds):
        start = min(e.t for e in cluster)
        end = max(e.t_end for e in cluster)
        score, confidence, reasons = score_cluster(cluster, cfg)
        window = CandidateWindow(
            start=start,
            end=end,
            events=sorted(cluster, key=lambda e: e.t),
            score=score,
            confidence=confidence,
            families=tuple(sorted(reasons["families"])),
            family_strengths=reasons["family_strengths"],
            reasons=reasons,
            min_score=cfg.min_score,
        )
        if window.qualifies:
            windows.append(window)
    return sorted(windows, key=lambda w: w.start)
