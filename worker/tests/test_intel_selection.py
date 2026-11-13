"""Clip selection tests (TASK 007) — context, boundaries, merge, dedup,
categories, ranking. All pure math over CandidateWindows.
"""

from __future__ import annotations

import pytest

from app.intel.fusion import CandidateWindow
from app.intel.selection import (
    CATEGORY_CLUTCH,
    CATEGORY_CRAZY_REACTION,
    CATEGORY_EPIC_KILL,
    CATEGORY_UNCLASSIFIED,
    CATEGORY_UNEXPECTED,
    SelectionConfig,
    select_clips,
)
from app.intel.signals import (
    KIND_ENERGY_SPIKE,
    KIND_MOTION_BURST,
    KIND_SCENE_CHANGE,
    KIND_TRANSIENT,
    SignalEvent,
)


def ev(t, family, kind, strength=1.0):
    return SignalEvent(t=t, t_end=t + 0.3, family=family, kind=kind, strength=strength,
                       evidence={"family": family, "kind": kind, "strength": strength})


def window(start, end, score, events, confidence=1.0):
    families = tuple(sorted({e.family for e in events})) if events else ()
    strengths = {}
    for e in events:
        strengths[e.family] = max(strengths.get(e.family, 0.0), e.strength)
    return CandidateWindow(start=start, end=end, events=events, score=score,
                           confidence=confidence, families=families,
                           family_strengths=strengths, reasons={"families": list(families)})


def _sel(windows, total=1500.0, gaps=(), requested=5, cfg=None):
    return select_clips(windows, total_duration=total, silence_gaps=list(gaps),
                        requested_clips=requested, cfg=cfg)


# ---- context + product duration bounds -------------------------------------------

def test_context_expansion_and_min_duration_padding():
    clips = _sel([window(100.0, 102.0, 80.0, [ev(100.0, "audio", KIND_ENERGY_SPIKE)])])
    assert len(clips) == 1
    c = clips[0]
    # 100-102 + pre 4/post 8 → 96-110 → padded to the 60 s product floor:
    assert c.start == pytest.approx(73.0)
    assert c.end == pytest.approx(133.0)
    assert c.duration == pytest.approx(60.0)
    assert c.start <= 100.0 < c.end <= 1500.0    # pre-event context included, core preserved


def test_clip_is_clamped_to_source_edges():
    clips = _sel([window(2.0, 4.0, 80.0, [ev(2.0, "audio", KIND_ENERGY_SPIKE)])], total=500.0)
    assert clips[0].start == 0.0
    assert clips[0].duration >= 59.0
    clips_end = _sel([window(496.0, 498.0, 80.0, [ev(496.0, "audio", KIND_ENERGY_SPIKE)])],
                     total=500.0)
    assert clips_end[0].end <= 500.0


def test_oversized_window_shrinks_to_max_duration_centered():
    # A wide merged window (100-300) must be capped at the 180 s product max,
    # centered on its core.
    clips = _sel([window(100.0, 300.0, 80.0, [ev(200.0, "audio", KIND_ENERGY_SPIKE)])])
    assert len(clips) == 1
    assert clips[0].duration == pytest.approx(180.0)
    assert clips[0].start == pytest.approx(112.0)
    assert clips[0].end == pytest.approx(292.0)


def test_custom_bounds_are_respected():
    cfg = SelectionConfig(min_duration_seconds=30.0, max_duration_seconds=45.0)
    clips = _sel([window(100.0, 102.0, 80.0, [ev(100.0, "audio", KIND_ENERGY_SPIKE)])], cfg=cfg)
    assert clips[0].duration == pytest.approx(30.0)


# ---- silence snapping (speech-safe boundaries) -------------------------------------

def test_boundaries_snap_outward_to_silence():
    # Bounded span 73-133; silence (70-72) ends just before the start → the
    # start moves to the END of the silence; silence (134-140) starts just
    # after the end → the end moves to the START of the silence.
    clips = _sel(
        [window(100.0, 102.0, 80.0, [ev(100.0, "audio", KIND_ENERGY_SPIKE)])],
        total=500.0, gaps=[(70.0, 72.0), (134.0, 140.0)],
    )
    assert clips[0].start == pytest.approx(72.0)
    assert clips[0].end == pytest.approx(134.0)


def test_snap_never_expands_beyond_search_radius():
    clips = _sel(
        [window(100.0, 102.0, 80.0, [ev(100.0, "audio", KIND_ENERGY_SPIKE)])],
        total=500.0, gaps=[(60.0, 62.0), (200.0, 210.0)],  # far away
    )
    assert clips[0].start == pytest.approx(73.0)  # unchanged
    assert clips[0].end == pytest.approx(133.0)


# ---- merge + dedup -------------------------------------------------------------------

def test_nearby_windows_merge_into_one_clip():
    clips = _sel([
        window(100.0, 102.0, 80.0, [ev(100.0, "audio", KIND_ENERGY_SPIKE)]),
        window(106.0, 108.0, 60.0, [ev(107.0, "visual", KIND_SCENE_CHANGE)]),
    ])
    assert len(clips) == 1  # merged (gap < merge_gap after context)
    assert clips[0].score == 80.0  # strongest survives
    assert len(clips[0].source_windows[0].events) == 2  # evidence unioned


def test_far_windows_stay_separate():
    clips = _sel([
        window(100.0, 102.0, 80.0, [ev(100.0, "audio", KIND_ENERGY_SPIKE)]),
        window(400.0, 402.0, 70.0, [ev(400.0, "audio", KIND_ENERGY_SPIKE)]),
    ])
    assert len(clips) == 2


def test_near_duplicates_are_removed_keeps_stronger():
    # Cores 20 s apart → expanded spans overlap ≥ 50 % IoU → one clip.
    clips = _sel([
        window(100.0, 102.0, 90.0, [ev(100.0, "audio", KIND_ENERGY_SPIKE)]),
        window(120.0, 122.0, 40.0, [ev(120.0, "audio", KIND_ENERGY_SPIKE)]),
    ])
    assert len(clips) == 1
    assert clips[0].score == 90.0


def test_distinct_candidates_are_not_removed():
    # Cores 25 s apart → IoU below the 0.5 dedup bar → both kept.
    clips = _sel([
        window(100.0, 102.0, 90.0, [ev(100.0, "audio", KIND_ENERGY_SPIKE)]),
        window(125.0, 127.0, 40.0, [ev(125.0, "audio", KIND_ENERGY_SPIKE)]),
    ])
    assert len(clips) == 2


# ---- categories (heuristic, always labeled) -------------------------------------------

def test_category_crazy_reaction_vocal_plus_transient():
    clips = _sel([window(100.0, 102.0, 80.0,
                         [ev(100.0, "audio", KIND_ENERGY_SPIKE), ev(100.4, "audio", KIND_TRANSIENT)])])
    assert clips[0].category == CATEGORY_CRAZY_REACTION


def test_category_crazy_reaction_vocal_around_cut():
    clips = _sel([window(100.0, 102.0, 80.0,
                         [ev(100.0, "audio", KIND_ENERGY_SPIKE), ev(100.1, "visual", KIND_SCENE_CHANGE)])])
    assert clips[0].category == CATEGORY_CRAZY_REACTION


def test_category_epic_kill_transient_plus_cut():
    clips = _sel([window(100.0, 102.0, 80.0,
                         [ev(100.0, "audio", KIND_TRANSIENT), ev(100.1, "visual", KIND_SCENE_CHANGE)])])
    assert clips[0].category == CATEGORY_EPIC_KILL


def test_category_unexpected_transient_only():
    # NOTE: transient-only windows never QUALIFY in fusion (loud-alone rule);
    # this tests the category mapping itself given such a window.
    clips = _sel([window(100.0, 102.0, 80.0, [ev(100.0, "audio", KIND_TRANSIENT)])])
    assert clips[0].category == CATEGORY_UNEXPECTED


def test_category_clutch_motion_only():
    clips = _sel([window(100.0, 102.0, 80.0, [ev(100.0, "visual", KIND_MOTION_BURST)])])
    assert clips[0].category == CATEGORY_CLUTCH


def test_category_unclassified_without_events():
    clips = _sel([window(100.0, 102.0, 80.0, [])])
    assert clips[0].category == CATEGORY_UNCLASSIFIED
    assert clips[0].evidence["heuristic_category"] is True


def test_evidence_is_explained_and_event_based():
    clips = _sel([window(100.0, 102.0, 80.0, [ev(100.0, "audio", KIND_ENERGY_SPIKE)])])
    evd = clips[0].evidence
    assert "why" in evd and evd["why"]
    assert evd["event_times"] == [100.0]
    assert evd["families"] == ["audio"]
    assert evd["family_strengths"] == {"audio": 1.0}


# ---- ranking ---------------------------------------------------------------------------

def test_ranking_score_desc_confidence_desc_start_asc():
    clips = _sel([
        window(100.0, 102.0, 50.0, [ev(100.0, "audio", KIND_ENERGY_SPIKE)], confidence=1.0),
        window(500.0, 502.0, 80.0, [ev(500.0, "audio", KIND_ENERGY_SPIKE)], confidence=0.5),
        window(900.0, 902.0, 65.0, [ev(900.0, "audio", KIND_ENERGY_SPIKE)], confidence=1.0),
    ])
    assert [c.score for c in clips] == [80.0, 65.0, 50.0]


def test_requested_clips_cap():
    clips = _sel(
        [window(100.0 + i * 300, 102.0 + i * 300, 50.0 + i,
                [ev(100.0 + i * 300, "audio", KIND_ENERGY_SPIKE)]) for i in range(4)],
        requested=2,
    )
    assert len(clips) == 2
    assert clips[0].score >= clips[1].score


def test_timestamps_preserved_and_valid():
    clips = _sel([window(100.0, 102.0, 80.0, [ev(100.0, "audio", KIND_ENERGY_SPIKE)]),
                  window(500.0, 502.0, 70.0, [ev(500.0, "audio", KIND_ENERGY_SPIKE)])])
    for c in clips:
        d = c.to_dict()
        assert d["start"] >= 0.0 and d["end"] <= 1500.0
        assert d["duration"] == pytest.approx(d["end"] - d["start"], abs=1e-6)
        assert 0 <= d["confidence"] <= 1


def test_empty_input():
    assert _sel([]) == []
