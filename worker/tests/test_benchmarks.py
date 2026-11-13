"""Benchmark framework tests (TASK 007).

Runs the deterministic synthetic benchmark end-to-end (real FFmpeg) and
asserts the report HONESTY contract:
  - measured values are facts from the run; targets are labeled separately;
  - the fixture is explicitly marked SYNTHETIC / not human-annotated;
  - on the scripted fixture the measurable targets hold: full recall,
    zero duplicates, boundary events persisted exactly once, zero
    reprocessed chunks on resume, a valid rendered clip with A/V sync.

These are synthetic-fixture guarantees (correctness + resources), NOT a
claim of production-grade real-world accuracy.
"""

from __future__ import annotations

from benchmarks.fixtures import DURATION, build_stream_fixture
from benchmarks.run import run_benchmark


def test_fixture_is_deterministic_and_annotated(ffmpeg_bin, tmp_path):
    f1 = build_stream_fixture(ffmpeg_bin, tmp_path / "a")
    f2 = build_stream_fixture(ffmpeg_bin, tmp_path / "b")
    assert f1.duration == DURATION == 30.0
    assert len(f1.events) == 6
    assert {e.kind for e in f1.events} == {"energy_spike", "transient", "scene_change"}
    # 4 of the 6 events straddle chunk boundaries (3 @ 8.5 s, 1 @ 25.0 s)
    assert len(f1.straddled) == 4
    # regeneration produces the same ground truth
    assert [(e.t, e.family, e.kind) for e in f1.events] == [(e.t, e.family, e.kind) for e in f2.events]


def test_benchmark_report_honesty_and_measurable_targets(ffmpeg_bin, ffprobe_bin, tmp_path):
    report = run_benchmark(ffmpeg_bin, ffprobe_bin, tmp_path, chunk_seconds=10,
                           overlap_seconds=2, render_top=True)

    # ---- honesty: measured vs targets clearly separated -------------------
    assert "measured" in report and "targets" in report
    assert report["fixture"]["type"] == "synthetic-deterministic"
    assert report["fixture"]["human_annotated"] is False
    assert "SYNTHETIC" in report["fixture"]["note"]
    for target in report["targets"].values():
        assert isinstance(target, str)  # targets are labeled aspirations, never fake numbers

    m = report["measured"]

    # ---- event detection (scripted ground truth) ---------------------------
    det = m["event_detection"]
    assert det["recall"] == 1.0, f"scripted events must all be detected: {det['false_negatives']}"
    assert det["true_positives"] == 6
    assert det["precision"] >= 0.8, f"unexpected false positives: {det['false_positives']}"
    # boundary accuracy: every matched event within the 1 s tolerance
    assert det["boundary_error_sec"]["max"] <= 1.0

    # ---- duplicates -----------------------------------------------------------
    assert m["duplicates"]["event_key_duplicates"] == 0
    assert m["duplicates"]["candidate_pairs_iou_ge_0.5"] == 0

    # ---- chunk-boundary correctness --------------------------------------------
    assert m["chunk_boundary"]["all_persisted_exactly_once"] is True
    for entry in m["chunk_boundary"]["straddled_events"]:
        assert entry["persisted"] == 1, entry

    # ---- recovery: interruption + resume with ZERO reprocessing ----------------
    rec = m["recovery"]
    assert rec["total_chunks"] == 4
    assert rec["interrupted_after_chunks"] == 2
    assert rec["chunks_reprocessed_on_resume"] == 0
    assert rec["audio_decodes_run1"] == 2
    assert rec["audio_decodes_resume_run"] == 2
    assert rec["final_state_matches_clean_run"] is True

    # ---- resources ---------------------------------------------------------------
    assert m["throughput"]["time_per_source_hour_seconds"] > 0
    assert m["memory"]["peak_rss_self_delta_mb"] >= 0

    # ---- render validity + A/V sync -------------------------------------------------
    r = m["render"]
    assert r["enabled"] is True and r["valid"] is True
    assert (r["width"], r["height"]) == (1080, 1920)
    assert r["av_delta_sec"] is not None and r["av_delta_sec"] < 0.15
    assert r["fps_measured"] is not None
