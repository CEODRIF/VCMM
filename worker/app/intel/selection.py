"""Context-aware clip selection (TASK 007).

Turns qualifying candidate windows into ranked, context-preserving clip
windows on the ABSOLUTE source timeline:

- **context** — pre-roll before the event core + aftermath after it;
- **speech-safe boundaries** — start/end snap OUTWARD to the nearest
  silence gap so a shout or sentence is not cut mid-word;
- **merge** — overlapping / adjacent windows become one clip;
- **dedup** — near-duplicates (IoU ≥ threshold) keep the stronger;
- **categories** — a configurable, HEURISTIC mapping from the evidence
  (always labeled; `unclassified` when the evidence is mixed);
- **ranking** — (score desc, confidence desc, start asc), capped at
  ``requested_clips``.

Scores are "estimated engagement potential from measured evidence" — NOT
a virality prediction (the product never claims one).

Default durations match the product clip contract (60-180 s, enforced by
the DB on the clips table) so selected windows can flow into the existing
renderer/store without a schema change; the bounds are configurable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .fusion import CandidateWindow
from .signals import (
    KIND_ENERGY_SPIKE,
    KIND_FACE_BROW_ACTIVITY,
    KIND_FACE_HEAD_MOTION,
    KIND_FACE_MOUTH_BURST,
    KIND_FACE_SMILE_GEOMETRY,
    KIND_GF_KILL_FEED_ENTRY,
    KIND_GF_MATCH_RESULT_TEXT,
    KIND_GF_SCORE_CHANGE,
    KIND_GF_SCOREBOARD_APPEARANCE,
    KIND_MOTION_BURST,
    KIND_SCENE_CHANGE,
    KIND_TRANSIENT,
    KIND_VOICE_ACTIVITY_CHANGE,
)

# -- category heuristics (evidence → label) ----------------------------------------
CATEGORY_CRAZY_REACTION = "crazy_reaction"
CATEGORY_FUNNY = "funny_moment"
CATEGORY_UNEXPECTED = "unexpected_moment"
CATEGORY_CLUTCH = "clutch_moment"
CATEGORY_EPIC_KILL = "epic_kill"
CATEGORY_RAGE = "rage_moment"
CATEGORY_VICTORY = "victory_moment"
CATEGORY_FAILURE = "unexpected_failure"
CATEGORY_UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class SelectionConfig:
    pre_context_seconds: float = 4.0
    post_context_seconds: float = 8.0
    merge_gap_seconds: float = 3.0
    dedup_iou: float = 0.5
    min_duration_seconds: float = 60.0   # product clip contract lower bound
    max_duration_seconds: float = 180.0  # product clip contract upper bound
    snap_search_seconds: float = 2.5     # how far boundaries may snap to silence


@dataclass
class SelectedClip:
    start: float
    end: float
    score: float
    confidence: float
    category: str
    source_windows: list[CandidateWindow] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "score": round(self.score, 2),
            "confidence": round(self.confidence, 4),
            "category": self.category,
            "evidence": self.evidence,
        }


def _iou(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    inter = max(0.0, min(a_end, b_end) - max(a_start, b_start))
    union = max(a_end, b_end) - min(a_start, b_start)
    return inter / union if union > 0 else 0.0


def _snap_to_silence(boundary: float, direction: int, gaps: list[tuple[float, float]], search: float,
                     total_duration: float) -> float:
    """Moves ``boundary`` (direction: -1 = start snaps left, +1 = end snaps
    right) to the edge of the nearest silence gap within ``search`` seconds,
    so speech/generally important audio is not cut. No gap → unchanged."""
    if not gaps:
        return boundary
    for gap_start, gap_end in sorted(gaps):
        if direction < 0 and gap_end <= boundary <= gap_end + search:
            return gap_end          # start at the end of a silence that ends at/before it
        if direction > 0 and boundary <= gap_start <= boundary + search:
            return gap_start        # end at the start of a silence that begins at/after it
    # Fallback: nearest gap edge within the search radius.
    best = None
    for gap_start, gap_end in gaps:
        if direction < 0:
            cand = gap_end if gap_end <= boundary else None
            if cand is not None and boundary - cand <= search:
                if best is None or cand > best:
                    best = cand
        else:
            cand = gap_start if gap_start >= boundary else None
            if cand is not None and cand - boundary <= search:
                if best is None or cand < best:
                    best = cand
    if best is None:
        return boundary
    return max(0.0, min(best, total_duration))


def _categorize(window: CandidateWindow) -> tuple[str, str]:
    """HEURISTIC label from the strongest evidence. Returns (category, why).

    Facecam kinds (TASK 008) contribute only as HEURISTIC visual-pattern
    evidence — the "why" strings say exactly that. Facecam evidence never
    overrides stronger audio/visual structures below it; it refines them.
    """
    kinds = {e.kind for e in window.events}
    families = set(window.families)
    vocal = {KIND_ENERGY_SPIKE, KIND_VOICE_ACTIVITY_CHANGE} & kinds
    sfx = kinds & {KIND_TRANSIENT}
    scene = window.family_strengths.get("visual", 0.0) if "visual" in families else 0.0
    has_scene = KIND_SCENE_CHANGE in kinds
    has_smile = KIND_FACE_SMILE_GEOMETRY in kinds
    has_mouth_burst = KIND_FACE_MOUTH_BURST in kinds
    has_kill_feed = KIND_GF_KILL_FEED_ENTRY in kinds
    has_match_result = KIND_GF_MATCH_RESULT_TEXT in kinds
    has_score_change = KIND_GF_SCORE_CHANGE in kinds
    has_scoreboard = KIND_GF_SCOREBOARD_APPEARANCE in kinds
    game_ev = ({KIND_GF_KILL_FEED_ENTRY, KIND_GF_MATCH_RESULT_TEXT,
                KIND_GF_SCORE_CHANGE, KIND_GF_SCOREBOARD_APPEARANCE} & kinds)

    if vocal and has_smile:
        return (CATEGORY_FUNNY,
                "vocal activity co-occurring with sustained smile-like facecam geometry "
                "(heuristic visual pattern)")
    if vocal and has_mouth_burst:
        return (CATEGORY_CRAZY_REACTION,
                "vocal spike co-occurring with a measured mouth-opening burst on the facecam "
                "(heuristic visual pattern)")
    if vocal and len(kinds & (vocal | sfx)) >= 2:
        return CATEGORY_CRAZY_REACTION, "sustained vocal spike co-occurring with a transient burst"
    if vocal and has_scene:
        return CATEGORY_CRAZY_REACTION, "vocal spike around a measured cut"
    # -- game vision (TASK 009): OCR-observed in-game events --------------
    # The TEXT/VALUES were directly observed by OCR; the category is a
    # heuristic interpretation of that observed content (always labeled).
    if vocal and has_kill_feed:
        return (CATEGORY_EPIC_KILL,
                "vocal spike co-occurring with an OCR-observed kill-feed entry "
                "(heuristic interpretation of observed text)")
    if has_match_result:
        text = next((str(e.evidence.get("text", "")).casefold()
                     for e in window.events if e.kind == KIND_GF_MATCH_RESULT_TEXT), "")
        if text in {"victory", "win", "gg", "gg wp"}:
            return (CATEGORY_VICTORY,
                    "OCR-observed match-result text (win/victory/gg) — observed text, "
                    "heuristic interpretation")
        if text in {"defeat", "loss", "surrender", "forfeit", "game over"}:
            return (CATEGORY_FAILURE,
                    "OCR-observed match-result text (defeat/loss) — observed text, "
                    "heuristic interpretation")
        return (CATEGORY_UNEXPECTED,
                "OCR-observed match-result text (heuristic interpretation)")
    if has_kill_feed and has_scene:
        return (CATEGORY_EPIC_KILL,
                "OCR-observed kill-feed entry co-occurring with a measured cut "
                "(heuristic interpretation of observed text)")
    if has_kill_feed:
        if len(families) >= 2:
            return (CATEGORY_EPIC_KILL,
                    "OCR-observed kill-feed entry with corroborating evidence "
                    "(heuristic interpretation)")
        return (CATEGORY_UNEXPECTED,
                "OCR-observed kill-feed entry (heuristic interpretation of observed "
                "text; single family)")
    if has_score_change:
        return CATEGORY_UNCLASSIFIED, "OCR-observed score change (measured values; no confident category)"
    if has_scoreboard:
        return CATEGORY_UNCLASSIFIED, "scoreboard appearance (text-density heuristic)"
    if game_ev:
        return CATEGORY_UNCLASSIFIED, "game vision evidence (heuristic interpretation)"
    if vocal:
        return CATEGORY_CRAZY_REACTION, "strong vocal-family evidence (heuristic)"
    if sfx and has_scene:
        return CATEGORY_EPIC_KILL, "transient burst + measured cut (kill-style event, heuristic)"
    if sfx:
        return CATEGORY_UNEXPECTED, "audio transient burst (heuristic)"
    if has_scene and "visual" in families:
        return CATEGORY_UNEXPECTED, "measured scene change (heuristic)"
    if KIND_MOTION_BURST in kinds:
        return CATEGORY_CLUTCH, "sudden visual motion surge (heuristic)"
    if has_smile and len(families) >= 2:
        return (CATEGORY_FUNNY,
                "sustained smile-like facecam geometry with other-family evidence "
                "(heuristic visual pattern)")
    if has_mouth_burst or KIND_FACE_BROW_ACTIVITY in kinds or KIND_FACE_HEAD_MOTION in kinds:
        return (CATEGORY_UNCLASSIFIED,
                "facecam facial-activity pattern without corroborating evidence "
                "(heuristic visual pattern — not an emotion classification)")
    return CATEGORY_UNCLASSIFIED, "mixed or weak evidence — no confident category"


def _merge_windows(windows: list[CandidateWindow], cfg: SelectionConfig) -> list[CandidateWindow]:
    """Merges overlapping / adjacent windows (union of events)."""
    if not windows:
        return []
    ordered = sorted(windows, key=lambda w: w.start)
    merged: list[CandidateWindow] = [ordered[0]]
    for w in ordered[1:]:
        last = merged[-1]
        if w.start <= last.end + cfg.merge_gap_seconds:
            merged[-1] = CandidateWindow(
                start=min(last.start, w.start),
                end=max(last.end, w.end),
                events=sorted(last.events + w.events, key=lambda e: e.t),
                score=max(last.score, w.score),
                confidence=max(last.confidence, w.confidence),
                families=tuple(sorted(set(last.families) | set(w.families))),
                family_strengths={
                    f: max(last.family_strengths.get(f, 0.0), w.family_strengths.get(f, 0.0))
                    for f in set(last.family_strengths) | set(w.family_strengths)
                },
                reasons={**last.reasons, "merged_with": last.reasons.get("merged_with", 0) + 1},
            )
        else:
            merged.append(w)
    return merged


def select_clips(
    windows: list[CandidateWindow],
    *,
    total_duration: float,
    silence_gaps: list[tuple[float, float]],
    requested_clips: int,
    cfg: SelectionConfig | None = None,
) -> list[SelectedClip]:
    """Full selection: context → snap → merge → dedup → categorize → rank."""
    cfg = cfg or SelectionConfig()
    if not windows:
        return []

    # 1) Expand each window with pre/post context, clamped to the source.
    expanded: list[CandidateWindow] = []
    for w in windows:
        start = max(0.0, w.start - cfg.pre_context_seconds)
        end = min(total_duration, w.end + cfg.post_context_seconds)
        expanded.append(CandidateWindow(start=start, end=end, events=w.events, score=w.score,
                                        confidence=w.confidence, families=w.families,
                                        family_strengths=w.family_strengths, reasons=w.reasons))

    # 2) Merge overlaps produced by the expansion.
    merged = _merge_windows(expanded, cfg)

    # 3) Enforce product duration bounds (shrink to core, then grow evenly).
    bounded: list[CandidateWindow] = []
    for w in merged:
        duration = w.end - w.start
        if duration < cfg.min_duration_seconds:
            need = cfg.min_duration_seconds - duration
            pad_start = min(need / 2, w.start)
            pad_end = min(need - pad_start, total_duration - w.end)
            w = CandidateWindow(start=w.start - pad_start, end=w.end + pad_end, events=w.events,
                                score=w.score, confidence=w.confidence, families=w.families,
                                family_strengths=w.family_strengths, reasons=w.reasons)
        elif duration > cfg.max_duration_seconds:
            core = (w.start + w.end) / 2
            w = CandidateWindow(start=core - cfg.max_duration_seconds / 2,
                                end=core + cfg.max_duration_seconds / 2,
                                events=w.events, score=w.score, confidence=w.confidence,
                                families=w.families, family_strengths=w.family_strengths,
                                reasons=w.reasons)
        w.start = max(0.0, w.start)
        w.end = min(total_duration, w.end)
        bounded.append(w)

    # 4) Speech-safe boundary snap (only INWARD-safe direction per edge).
    snapped: list[CandidateWindow] = []
    for w in bounded:
        start = _snap_to_silence(w.start, -1, silence_gaps, cfg.snap_search_seconds, total_duration)
        end = _snap_to_silence(w.end, +1, silence_gaps, cfg.snap_search_seconds, total_duration)
        if end - start >= min(cfg.min_duration_seconds, w.end - w.start):
            w = CandidateWindow(start=start, end=end, events=w.events, score=w.score,
                                confidence=w.confidence, families=w.families,
                                family_strengths=w.family_strengths, reasons=w.reasons)
        snapped.append(w)

    # 5) Near-duplicate removal (IoU), keep the stronger.
    deduped: list[CandidateWindow] = []
    for w in sorted(snapped, key=lambda x: -x.score):
        if all(_iou(w.start, w.end, k.start, k.end) < cfg.dedup_iou for k in deduped):
            deduped.append(w)

    # 6) Categorize + evidence + ranking.
    clips: list[SelectedClip] = []
    for w in deduped:
        category, why = _categorize(w)
        evidence: dict[str, Any] = {
            "why": why,
            "heuristic_category": True,
            "families": list(w.families),
            "family_strengths": w.family_strengths,
            "event_kinds": sorted({e.kind for e in w.events}),
            "event_times": [round(e.t, 3) for e in w.events],
        }
        # TASK 010 (backward-compatible): when the multimodal engine produced
        # this window, its size-bounded provenance (independent modalities,
        # duplicates, pattern, heuristic-confidence calc) rides along so the
        # selected moment stays explainable. Absent in Phase 7 mode — the
        # evidence shape is otherwise byte-identical.
        multimodal_prov = w.reasons.get("multimodal")
        if multimodal_prov:
            evidence["multimodal"] = multimodal_prov
        clips.append(SelectedClip(
            start=w.start,
            end=w.end,
            score=w.score,
            confidence=w.confidence,
            category=category,
            source_windows=[w],
            evidence=evidence,
        ))
    clips.sort(key=lambda c: (-c.score, -c.confidence, c.start))
    return clips[: max(1, requested_clips)]
