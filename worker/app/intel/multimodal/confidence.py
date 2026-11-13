"""Explainable heuristic confidence (TASK 010, layer 4).

Terminology (spec-mandated): this is ``heuristic_confidence`` — a
bounded, deterministic aggregate of measured evidence. It is NOT a
statistically calibrated probability of correctness (no annotated
real-VOD evaluation exists; calibration is TASK 013's scope). Every
result exposes its full calculation inputs and penalties.

Formula (version mv1, all factors bounded in [0, 1] unless noted):

    base            = Σ w_f · max_strength_f / Σ w_f   over PRESENT
                      families (w_f = the existing FusionConfig weights —
                      detector strengths are PRESERVED, never rewritten)
    independence    = 1 + diversity_bonus · max(0, min(n_mods, 3) − 1) / 2
                      (n_mods = INDEPENDENT modalities after dedup;
                      1 mod → 1.0 — a single modality gets NO credit —
                      2 → 1 + bonus/2, 3+ → 1 + bonus — bounded, so
                      "more events" never auto-boosts)
    proximity       = mean of the cluster's cross-family decay weights
                      (exp(-gap/tau); 1.0 when nothing measured → the
                      factor is 1.0, i.e. absence of data is NOT a
                      penalty)
    quality         = mean of kept-observation qualities (detector
                      confidence where measured; strength elsewhere)
    pattern         = 1 + pattern.confidence_bonus when a pattern matched
    penalties       = Π (1 - contradiction_penalty) per contradiction
                      · (1 - low_quality_penalty) if quality < threshold

    heuristic_confidence = clamp(base · independence · proximity ·
                                 quality · pattern · penalties, 0, 1)

Missing data is never negative evidence: a detector that was
unavailable simply contributes no modality (the availability summary is
reported alongside the number, so it stays explainable).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import CONFIDENCE_VERSION, MultimodalConfig
from .correlation import TemporalCluster
from .sequences import PatternMatch

FAMILY_WEIGHT_DEFAULT = 0.5  # fallback when a family is absent from FusionConfig


@dataclass
class ConfidenceResult:
    """The number + its ENTIRE explanation (deterministic)."""

    heuristic_confidence: float
    version: str
    calc: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"heuristic_confidence": self.heuristic_confidence,
                "version": self.version, "calc": self.calc}


def compute_confidence(
    cluster: TemporalCluster,
    cfg: MultimodalConfig,
    family_weights: dict[str, float],
    diversity_bonus: float,
    pattern: PatternMatch | None,
    contradictions: list[str],
) -> ConfidenceResult:
    obs = cluster.observations
    if not obs:
        return ConfidenceResult(0.0, cfg.confidence_version,
                                {"note": "no observations"})

    # base: weighted max strength over PRESENT families (originals kept)
    def _q(s) -> float:
        return s.quality if s.quality > 0 else s.strength

    strengths: dict[str, float] = {}
    qualities: list[float] = []
    for o in obs:
        strengths[o.signal.family] = max(strengths.get(o.signal.family, 0.0), _q(o.signal))
        # every KEPT signal contributes to the quality mean (duplicates
        # included on purpose — a repeatedly low-quality read stays low
        # quality); only primaries count toward independence.
        qualities.append(_q(o.signal))
        for extra in (*o.corroborations, *o.related):
            qualities.append(_q(extra))

    present = list(strengths)  # deterministic: first-observation order
    weight_sum = sum(family_weights.get(f, FAMILY_WEIGHT_DEFAULT) for f in present) or 1.0
    base = sum(family_weights.get(f, FAMILY_WEIGHT_DEFAULT) * strengths[f]
               for f in present) / weight_sum

    # independence: credit per INDEPENDENT modality beyond the first
    # (1 mod → 1.0; 2 → 1 + bonus/2; 3+ → 1 + bonus — bounded)
    n_mods = len(cluster.independent_modalities)
    independence = 1.0 + diversity_bonus * max(0, min(n_mods, 3) - 1) / 2.0

    # proximity: mean cross-family decay weight (1.0 when none measured)
    if cluster.proximity_weights:
        proximity = sum(cluster.proximity_weights) / len(cluster.proximity_weights)
    else:
        proximity = 1.0

    # quality: mean over ALL kept evidence (duplicates included on purpose —
    # a repeatedly LOW-quality read stays low quality)
    quality = sum(qualities) / len(qualities) if qualities else 0.0

    # pattern contribution
    pattern_factor = 1.0
    pattern_info: dict[str, Any] | None = None
    if pattern is not None:
        p = next(p for p in cfg.patterns if p.name == pattern.pattern)
        pattern_factor = 1.0 + p.confidence_bonus
        pattern_info = {
            "name": pattern.pattern,
            "independent_modalities": pattern.independent_modalities,
            "stages": [s.kind for s in pattern.stages],
            "explanation": pattern.explanation,
        }

    # penalties
    penalties: list[dict[str, Any]] = []
    factor = 1.0
    for c in contradictions:
        factor *= (1.0 - cfg.contradiction_penalty)
        penalties.append({"type": "contradiction", "detail": c,
                          "multiplier": round(1.0 - cfg.contradiction_penalty, 4)})
    if quality < cfg.low_quality_threshold:
        factor *= (1.0 - cfg.low_quality_penalty)
        penalties.append({"type": "low_quality",
                          "detail": f"mean quality {round(quality, 3)} < {cfg.low_quality_threshold}",
                          "multiplier": round(1.0 - cfg.low_quality_penalty, 4)})

    value = base * independence * proximity * quality * pattern_factor * factor
    value = round(min(1.0, max(0.0, value)), 4)

    calc = {
        "base": round(base, 4),
        "family_max_strengths": {f: round(v, 4) for f, v in sorted(strengths.items())},
        "independence": {"modalities": n_mods,
                         "factor": round(independence, 4)},
        "proximity": {"weights": cluster.proximity_weights[:8],
                      "factor": round(proximity, 4)},
        "quality": {"mean": round(quality, 4),
                    "observations_scored": len(qualities)},
        "pattern": pattern_info,
        "penalties": penalties,
        "note": ("heuristic_confidence: bounded deterministic aggregate of "
                 "measured evidence; NOT a statistically calibrated "
                 "probability (no annotated real-VOD evaluation yet)"),
    }
    return ConfidenceResult(value, cfg.confidence_version or CONFIDENCE_VERSION, calc)


def detect_contradictions(cluster: TemporalCluster) -> list[str]:
    """Conservative, deterministic contradiction set (limited by design):

    - conflicting MATCH-RESULT readings in one cluster (a win-family token
      AND a loss-family token both OCR-observed) — genuinely opposing
      evidence about the same moment.

    Missing data / unavailable detectors are NOT contradictions (never
    negative evidence). No other "contradiction" rules are invented: the
    mechanism is open, the rule set is deliberately small.
    """
    WIN = {"victory", "win", "gg", "gg wp"}
    LOSS = {"defeat", "loss", "surrender", "forfeit", "game over"}
    win_seen: str | None = None
    loss_seen: str | None = None
    for o in cluster.observations:
        s = o.signal
        if s.kind != "match_result_text":
            continue
        text = str(s.original.evidence.get("text", "")).casefold()
        if text in WIN and win_seen is None:
            win_seen = text
        if text in LOSS and loss_seen is None:
            loss_seen = text
    if win_seen is not None and loss_seen is not None:
        return [f"conflicting match-result readings: {win_seen!r} vs {loss_seen!r}"]
    return []
