"""TASK 015 P3 — clip generation planning (UNIT tests, no I/O).

Mode A (full coverage): clip-count calculation, the final remainder policy,
complete timeline coverage, no gaps / no unintended overlaps, chronological
order — and the 60-minute @ 60-second example from the product spec.

Mode B (viral highlights): the transparent scoring model (exact math),
ranking order, qualification thresholds, context expansion (event
beginnings/ends preserved), near-duplicate prevention, hard max_clips
bound, and the honesty rule that unmeasured components contribute 0.
"""

from __future__ import annotations

import pytest

from app.ai.clip_planning import (
    ClipSpan,
    expected_clip_count,
    plan_full_coverage,
    rank_highlights,
    spans_overlap_ratio,
    verify_clip_tiling,
)


# ---------------------------------------------------------------------------
# MODE A — count + tiling
# ---------------------------------------------------------------------------

def test_expected_clip_count_is_ceil():
    assert expected_clip_count(3600, 60) == 60     # exact division
    assert expected_clip_count(3645, 60) == 61     # 60.75 -> 61
    assert expected_clip_count(3705, 60) == 62     # 61.75 -> 62
    assert expected_clip_count(60, 60) == 1
    assert expected_clip_count(59, 60) == 1        # shorter than one clip
    assert expected_clip_count(1, 60) == 1


def test_sixty_minutes_at_sixty_seconds_yields_sixty_clips():
    """The spec example: 60 min source, 60 s clip duration -> 60 clips."""
    spans = plan_full_coverage(3600, 60)
    assert len(spans) == 60
    assert spans[0] == ClipSpan(0.0, 60.0)
    assert spans[-1] == ClipSpan(3540.0, 3600.0)
    verify_clip_tiling(spans, 3600)


def test_plan_full_coverage_exact_division_tiles_without_remainder():
    spans = plan_full_coverage(100, 10)
    assert [(s.start, s.end) for s in spans] == [
        (0.0, 10.0), (10.0, 20.0), (20.0, 30.0), (30.0, 40.0), (40.0, 50.0),
        (50.0, 60.0), (60.0, 70.0), (70.0, 80.0), (80.0, 90.0), (90.0, 100.0),
    ]


def test_short_remainder_is_appended_to_the_previous_clip():
    """25s @ 10s: remainder 5s < 10s -> merged; no padding, nothing skipped."""
    spans = plan_full_coverage(25, 10)
    assert [(s.start, s.end) for s in spans] == [(0.0, 10.0), (10.0, 25.0)]
    # The source end is covered exactly — the last clip extends to 25s.
    assert spans[-1].end == 25.0
    verify_clip_tiling(spans, 25)


def test_remainder_of_exactly_ten_seconds_is_kept_as_its_own_clip():
    spans = plan_full_coverage(250, 120)  # 2 full + 10s remainder
    assert [(s.start, s.end) for s in spans] == [
        (0.0, 120.0), (120.0, 240.0), (240.0, 250.0),
    ]


def test_source_shorter_than_one_clip_is_a_single_clip():
    spans = plan_full_coverage(25, 60)
    assert spans == [ClipSpan(0.0, 25.0)]  # output duration = source duration


def test_plan_full_coverage_covers_everything_exactly_once():
    """Property sweep: chronological, no gaps, no overlaps, no padding."""
    for duration, clip in [(45, 7), (100.5, 13), (371, 120), (1200, 60),
                           (25, 10), (65, 10), (92, 10)]:
        spans = plan_full_coverage(duration, clip)
        verify_clip_tiling(spans, duration)          # raises on any violation
        # Chronological, strictly forward.
        assert all(spans[i].end <= spans[i + 1].start for i in range(len(spans) - 1))
        # No invented footage: the total rendered length == the source length.
        assert sum(s.duration for s in spans) == pytest.approx(duration)
        # The count is the ceil contract (or ceil-1 via the remainder merge).
        assert len(spans) in (expected_clip_count(duration, clip),
                              expected_clip_count(duration, clip) - 1)


def test_verify_clip_tiling_rejects_gaps_overlaps_and_missing_end():
    good = [ClipSpan(0, 10), ClipSpan(10, 20)]
    verify_clip_tiling(good, 20)  # no error
    with pytest.raises(ValueError, match="gap"):
        verify_clip_tiling([ClipSpan(0, 10), ClipSpan(11, 20)], 20)
    with pytest.raises(ValueError, match="overlap"):
        verify_clip_tiling([ClipSpan(0, 10), ClipSpan(9, 20)], 20)
    with pytest.raises(ValueError, match="ends at"):
        verify_clip_tiling([ClipSpan(0, 10)], 20)
    with pytest.raises(ValueError, match="starts at"):
        verify_clip_tiling([ClipSpan(1, 20)], 20)
    with pytest.raises(ValueError, match="extends"):
        verify_clip_tiling([ClipSpan(0, 21)], 20)
    with pytest.raises(ValueError, match="at least one"):
        verify_clip_tiling([], 20)


def test_plan_full_coverage_rejects_bad_durations():
    with pytest.raises(ValueError):
        plan_full_coverage(0, 60)
    with pytest.raises(ValueError):
        plan_full_coverage(60, 0)


def test_spans_overlap_ratio():
    assert spans_overlap_ratio(0, 100, 90, 190) == pytest.approx(0.1)  # vs shorter
    assert spans_overlap_ratio(0, 10, 0, 10) == 1.0
    assert spans_overlap_ratio(0, 10, 10, 20) == 0.0
    assert spans_overlap_ratio(0, 20, 5, 15) == 1.0  # contained


# ---------------------------------------------------------------------------
# MODE B — transparent ranking
# ---------------------------------------------------------------------------

def _row(index: int, start: float, end: float, *, importance: float | None = None,
         confidence: float | None = None, candidate: bool = False,
         status: str | None = "ok", transcript: str = "") -> dict:
    """One timeline row in the exact shape the analysis stage builds."""
    assessment = None
    if status is not None:
        assessment = {"status": status, "event_importance": importance,
                      "candidate_clip": candidate, "reasoning": "measured"}
    return {
        "window_index": index,
        "start_time": start,
        "end_time": end,
        "transcript": transcript,
        "confidence": confidence,
        "event_importance": importance if status == "ok" else None,
        "gemini_assessment": assessment,
    }


def test_rank_highlights_transparent_score_math_is_exact():
    rows = [_row(0, 0, 60, importance=0.8, confidence=0.6, candidate=True)]
    cands = rank_highlights(rows, clip_duration=60, max_clips=3)
    assert len(cands) == 1
    # 100 * (0.5*0.8 + 0.3*0.6 + 0.2*1.0) = 40 + 18 + 20
    assert cands[0].score == pytest.approx(78.0)
    assert cands[0].importance == 0.8
    assert cands[0].confidence == 0.6
    assert cands[0].candidate_windows == 1


def test_rank_highlights_unmeasured_components_contribute_zero():
    # No assessment (status None): importance contributes 0; the measured
    # confidence alone drives the score — 100 * 0.3 * 0.7 = 21.
    rows = [_row(0, 0, 60, confidence=0.7, status=None)]
    cands = rank_highlights(rows, 60, 3)
    assert len(cands) == 1
    assert cands[0].score == pytest.approx(21.0)
    assert cands[0].importance is None


def test_rank_highlights_a_failed_assessment_never_counts_as_importance():
    # importance 0.95 but the assessment FAILED -> honest None -> no seed.
    rows = [_row(0, 0, 60, importance=0.95, status="failed")]
    assert rank_highlights(rows, 60, 3) == []
    # Same row persisted with the raw value but a failed status.
    rows[0]["gemini_assessment"] = {"status": "failed", "reason": "quota"}
    assert rank_highlights(rows, 60, 3) == []


def test_rank_highlights_qualification_thresholds():
    assert rank_highlights([_row(0, 0, 60, importance=0.49)], 60, 3) == []
    assert len(rank_highlights([_row(0, 0, 60, importance=0.5)], 60, 3)) == 1
    assert rank_highlights([_row(0, 0, 60, confidence=0.59, status=None)], 60, 3) == []
    assert len(rank_highlights([_row(0, 0, 60, confidence=0.6, status=None)], 60, 3)) == 1
    # The candidate flag alone qualifies a window (a completed assessment).
    assert len(rank_highlights([_row(0, 0, 60, candidate=True)], 60, 3)) == 1


def test_rank_highlights_ranks_by_score_descending():
    # Qualifying windows separated by dead windows -> independent groups.
    rows = [
        _row(0, 0, 60, importance=0.6),
        _row(1, 60, 120),
        _row(2, 120, 180, importance=0.9),
        _row(3, 180, 240),
        _row(4, 240, 300, importance=0.75),
    ]
    cands = rank_highlights(rows, 60, 5)
    assert len(cands) == 3
    assert [c.score for c in cands] == sorted([c.score for c in cands], reverse=True)
    assert [c.start for c in cands] == [120, 240, 0]
    # 100 * 0.5 * importance (no measured confidence, no candidate flags).
    assert [c.score for c in cands] == [pytest.approx(45.0),
                                        pytest.approx(37.5), pytest.approx(30.0)]


def test_rank_highlights_context_expansion_preserves_event_boundaries():
    """A high-importance window expands over its transcript-connected
    neighbour (the beginning of the event is not cut off) but stops at the
    empty window (no signal continuation)."""
    rows = [
        _row(0, 0, 60, transcript="intro talk building up to the play"),
        _row(1, 60, 120, importance=0.9, transcript="the kill happened here"),
        _row(2, 120, 180),  # empty: no transcript, no measured signal
    ]
    cands = rank_highlights(rows, 60, 3)
    assert len(cands) == 1
    assert cands[0].start == 0.0    # expanded left over the connected window
    assert cands[0].end == 120.0    # NOT expanded right into the dead window
    assert cands[0].window_count == 2
    # 100 * (0.5*0.9 + 0.3*0 + 0.2*0) = 45
    assert cands[0].score == pytest.approx(45.0)


def test_rank_highlights_never_returns_near_duplicate_candidates():
    """Whatever the grouping, the returned candidates must be pairwise
    near-duplicate-free (IoU <= 0.3 on the shorter window). A run of five
    adjacent qualifying windows exercises both the expansion bound and the
    cap, which can only shrink spans — never create overlaps."""
    rows = [_row(i, i * 100, i * 100 + 100, importance=0.9) for i in range(5)]
    cands = rank_highlights(rows, clip_duration=90, max_clips=5, duration_seconds=500.0)
    assert len(cands) >= 1
    for i in range(len(cands)):
        for j in range(i + 1, len(cands)):
            a, b = cands[i], cands[j]
            overlap = spans_overlap_ratio(a.start, a.end, b.start, b.end)
            assert overlap <= 0.3, f"candidates {i} and {j} are near-duplicates"


def test_rank_highlights_max_clips_is_a_hard_upper_bound():
    rows = []
    for i, imp in enumerate([0.9, 0.8, 0.7, 0.6]):
        # Qualifying windows separated by dead windows -> 4 independent
        # candidate groups.
        rows.append(_row(i * 2, i * 120, i * 120 + 60, importance=imp))
        rows.append(_row(i * 2 + 1, i * 120 + 60, i * 120 + 120))
    rows.sort(key=lambda r: r["window_index"])
    cands = rank_highlights(rows, 60, 2)
    assert len(cands) == 2
    # The two best (0.9 -> 45, 0.8 -> 40).
    assert cands[0].score == pytest.approx(45.0)
    assert cands[1].score == pytest.approx(40.0)


def test_rank_highlights_empty_when_nothing_qualifies():
    assert rank_highlights([], 60, 3) == []
    assert rank_highlights([_row(0, 0, 60)], 60, 3) == []
    assert rank_highlights([_row(0, 0, 60, importance=0.9)], 0, 3) == []
    assert rank_highlights([_row(0, 0, 60, importance=0.9)], 60, 0) == []


def test_rank_highlights_caps_absurd_groups_at_three_x_clip_duration():
    # Three adjacent qualifying 100s windows = 300s > 3*90 = 270 cap.
    rows = [
        _row(0, 0, 100, importance=0.9),
        _row(1, 100, 200, importance=0.9),
        _row(2, 200, 300, importance=0.9),
    ]
    cands = rank_highlights(rows, clip_duration=90, max_clips=3)
    assert len(cands) == 1
    assert cands[0].start == 0.0
    assert cands[0].end == 270.0  # capped, still chronological + bounded
    assert cands[0].duration <= 90 * 3


def test_rank_highlights_clamps_to_the_measured_source_duration():
    rows = [_row(0, 0, 100, importance=0.9)]
    cands = rank_highlights(rows, clip_duration=90, max_clips=3, duration_seconds=250.0)
    assert cands[0].end <= 250.0
    cands2 = rank_highlights(rows, clip_duration=90, max_clips=3, duration_seconds=50.0)
    assert cands2[0].start >= 0.0
    assert cands2[0].end <= 50.0
