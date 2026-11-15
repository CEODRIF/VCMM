"""TASK 015 — full-timeline coverage engine (PURE unit tests, no I/O).

Covers: window planning + remainder handling, gap/overlap detection,
transcript/evidence extraction (measured only — never invented), and the
unified-row honesty rules (not_configured / failed / ok states).
"""

from __future__ import annotations

import pytest

from app.ai.timeline import (
    TimelineCoverageError,
    TimelineRow,
    TimelineWindow,
    build_timeline_rows,
    plan_windows,
    verify_coverage,
    window_evidence,
    window_text,
)


class TestPlanWindows:
    def test_exact_tiling(self):
        w = plan_windows(3600, 120, 240)
        assert len(w) == 30
        assert w[0].start == 0.0
        assert w[-1].end == 3600.0
        for a, b in zip(w, w[1:]):
            assert b.start == a.end  # no gaps, no overlaps, by construction

    def test_remainder_is_an_explicit_final_window(self):
        w = plan_windows(3700, 120, 240)
        assert len(w) == 31
        assert abs(w[-1].start - 3600.0) < 1e-9
        assert abs(w[-1].end - 3700.0) < 1e-9
        assert abs(w[-1].duration - 100.0) < 1e-9  # the remainder window

    def test_sub_window_source_yields_single_window(self):
        w = plan_windows(45, 120, 240)
        assert len(w) == 1
        assert (w[0].start, w[0].end) == (0.0, 45.0)

    def test_cost_bound_widens_windows_without_dropping_coverage(self):
        # 8h at 120s would be 288 windows > cap 240 → widen, still exact.
        w = plan_windows(8 * 3600, 120, 240)
        assert len(w) <= 240
        assert w[0].start == 0.0
        assert w[-1].end == 8 * 3600
        for a, b in zip(w, w[1:]):
            assert b.start == a.end

    def test_invalid_inputs_raise(self):
        with pytest.raises(ValueError):
            plan_windows(0, 120, 240)
        with pytest.raises(ValueError):
            plan_windows(-5, 120, 240)
        with pytest.raises(ValueError):
            plan_windows(100, 0, 240)
        with pytest.raises(ValueError):
            plan_windows(100, 120, 0)


class TestVerifyCoverage:
    def _rows(self, spans):
        return [
            TimelineRow(window_index=i, start_time=s, end_time=e)
            for i, (s, e) in enumerate(spans)
        ]

    def test_valid_tiling_passes(self):
        verify_coverage(self._rows([(0, 100), (100, 200), (200, 250)]), 250)

    def test_gap_detected(self):
        with pytest.raises(TimelineCoverageError, match="gap"):
            verify_coverage(self._rows([(0, 100), (150, 250)]), 250)

    def test_overlap_detected(self):
        with pytest.raises(TimelineCoverageError, match="overlap"):
            verify_coverage(self._rows([(0, 120), (100, 250)]), 250)

    def test_nonzero_start_detected(self):
        with pytest.raises(TimelineCoverageError, match="starts at"):
            verify_coverage(self._rows([(5, 100)]), 100)

    def test_short_end_detected(self):
        with pytest.raises(TimelineCoverageError, match="ends at"):
            verify_coverage(self._rows([(0, 100)]), 250)

    def test_overrun_detected(self):
        with pytest.raises(TimelineCoverageError, match="extends"):
            verify_coverage(self._rows([(0, 300)]), 250)

    def test_empty_rows_detected(self):
        with pytest.raises(TimelineCoverageError, match="no rows"):
            verify_coverage([], 100)


class TestWindowText:
    def test_only_overlapping_segments_included(self):
        segs = [
            {"text": "a", "start": 0.0, "end": 5.0},
            {"text": "b", "start": 4.0, "end": 14.0},
            {"text": "c", "start": 20.0, "end": 30.0},
        ]
        assert window_text(segs, 10, 20) == "b"
        assert window_text(segs, 0, 30) == "a b c"

    def test_boundary_exclusion(self):
        segs = [{"text": "x", "start": 10.0, "end": 20.0}]
        assert window_text(segs, 0, 10) == ""   # ends exactly at window start
        assert window_text(segs, 20, 30) == ""  # starts exactly at window end

    def test_empty_window_is_empty_never_invented(self):
        assert window_text([], 0, 100) == ""
        assert window_text(None, 0, 100) == ""

    def test_char_bound(self):
        segs = [{"text": "word " * 500, "start": 0, "end": 10}]
        out = window_text(segs, 0, 10, max_chars=100)
        assert len(out) <= 100


class TestWindowEvidence:
    def test_family_grouping_and_boundary_exclusion(self):
        evs = [
            {"family": "audio", "kind": "energy_peak", "t": 5.0, "t_end": 6.0,
             "strength": 0.8, "evidence": {"db": -12.5}},
            {"family": "visual", "kind": "scene_cut", "t": 15.0, "t_end": 15.0,
             "strength": 0.9, "evidence": {}},
            {"family": "visual", "kind": "ocr", "t": 50.0, "t_end": 51.0,
             "strength": 0.4, "evidence": {"text": "GO"}},
        ]
        out = window_evidence(evs, 0, 30)
        assert [e["kind"] for e in out["audio"]] == ["energy_peak"]
        assert [e["kind"] for e in out["visual"]] == ["scene_cut"]
        assert out["total"] == 2  # the t=50 event is outside the window
        assert out["audio"][0]["evidence"] == {"db": -12.5}

    def test_no_events_is_zero_never_invented(self):
        out = window_evidence([], 0, 100)
        assert out == {"audio": [], "visual": [], "total": 0}

    def test_per_family_cap(self):
        evs = [
            {"family": "audio", "kind": f"k{i}", "t": float(i), "t_end": float(i),
             "strength": 0.1, "evidence": {}}
            for i in range(30)
        ]
        out = window_evidence(evs, 0, 100, max_per_family=12)
        assert len(out["audio"]) == 12
        assert out["total"] == 30


class TestBuildTimelineRows:
    def test_unconfigured_state_is_honest_and_complete(self):
        windows = plan_windows(25, 10, 240)
        rows = build_timeline_rows(25, windows, [], [], None)
        assert len(rows) == 3  # [0,10) [10,20) [20,25) remainder
        assert [r.end_time for r in rows] == [10.0, 20.0, 25.0]
        for r in rows:
            assert r.gemini_assessment == {"status": "not_configured"}
            assert r.event_importance is None
            assert r.candidate_clip is False
            assert r.event_type == "unknown"
            assert r.scene_description is None

    def test_ok_assessment_sets_semantic_fields(self):
        windows = plan_windows(20, 10, 240)
        assessments = {
            0: {"status": "ok", "scene_description": "intro", "event_type": "intro",
                "event_importance": 0.3, "candidate_clip": False, "reasoning": "r"},
            1: {"status": "ok", "scene_description": "clutch play",
                "event_type": "combat", "event_importance": 0.91,
                "candidate_clip": True, "reasoning": "r2"},
        }
        rows = build_timeline_rows(20, windows, [], [], assessments)
        assert rows[1].event_type == "combat"
        assert rows[1].event_importance == 0.91
        assert rows[1].candidate_clip is True
        assert rows[1].scene_description == "clutch play"
        assert rows[0].candidate_clip is False

    def test_failed_assessment_never_claims_analysis(self):
        windows = plan_windows(20, 10, 240)
        rows = build_timeline_rows(
            20, windows, [], [],
            {0: {"status": "failed", "reason": "GeminiRateLimitedError: quota"}})
        assert rows[0].gemini_assessment["status"] == "failed"
        assert rows[0].event_importance is None
        assert rows[0].candidate_clip is False
        assert rows[0].event_type == "unknown"

    def test_confidence_only_from_measured_signals(self):
        evs = [{"family": "audio", "kind": "energy_peak", "t": 2.0, "t_end": 3.0,
                "strength": 0.77, "evidence": {}}]
        windows = plan_windows(10, 10, 240)
        rows = build_timeline_rows(10, windows, [], evs, None)
        assert rows[0].confidence == 0.77
        assert rows[0].audio_evidence["total"] == 1
        empty = build_timeline_rows(10, windows, [], [], None)
        assert empty[0].confidence is None

    def test_transcript_flows_into_rows(self):
        segs = [{"text": "hello world", "start": 1.0, "end": 3.0},
                {"text": "second window talk", "start": 12.0, "end": 14.0}]
        windows = plan_windows(20, 10, 240)
        rows = build_timeline_rows(20, windows, segs, [], None)
        assert rows[0].transcript == "hello world"
        assert rows[1].transcript == "second window talk"

    def test_to_db_row_shape(self):
        rows = build_timeline_rows(10, plan_windows(10, 10, 240), [], [], None)
        db = rows[0].to_db_row("j")
        assert set(db) == {
            "job_id", "window_index", "start_time", "end_time", "event_type",
            "transcript", "scene_description", "visual_evidence",
            "audio_evidence", "event_importance", "candidate_clip",
            "confidence", "gemini_assessment",
        }
        assert db["job_id"] == "j"
        assert db["window_index"] == 0
