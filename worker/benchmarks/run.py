"""Reproducible benchmark runner (TASK 007).

Measures, on the deterministic synthetic fixture:
  - event detection precision / recall vs ground truth (core kinds;
    secondary heuristics reported separately — never hidden),
  - boundary accuracy (measured t vs ground-truth t),
  - duplicate events / duplicate clips,
  - chunk-boundary correctness (straddled events persisted exactly once),
  - interruption + recovery (zero reprocessing, same final state as clean),
  - wall time per source hour, peak memory (ru_maxrss delta),
  - render validity + A/V sync of the top candidate clip.

Output is a JSON report where ``measured`` (facts) and ``targets`` (labeled
aspirations, several of which are UNMEASURABLE on synthetic footage) never
blend. Run:  python -m benchmarks.run --workdir <dir> [--out report.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import resource
import shutil
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import app.intel.pipeline as pipeline_mod
from app.intel.checkpoints import IntelStore
from app.intel.pipeline import PipelineConfig, StreamIntelPipeline
from app.intel.signals import KIND_ENERGY_SPIKE, KIND_MOTION_BURST, KIND_SCENE_CHANGE, KIND_TRANSIENT, KIND_VOICE_ACTIVITY_CHANGE
from app.intel.testing import FakeSupabaseClient
from app.rendering.base import RenderSpec
from app.rendering.profiles import RenderProfile
from app.rendering.renderer import FFmpegRenderer
from app.rendering.validation import verify_render_output
from app.validation.media import probe_media

from .fixtures import BenchmarkFixture, build_stream_fixture

#: Kinds counted in precision/recall. Secondary heuristics (voice regime,
#: motion) are reported separately — including them would conflate a
#: heuristic's false positives with the core signal's accuracy.
CORE_KINDS = {KIND_ENERGY_SPIKE, KIND_TRANSIENT, KIND_SCENE_CHANGE}
SECONDARY_KINDS = {KIND_VOICE_ACTIVITY_CHANGE, KIND_MOTION_BURST}

JOB = str(UUID("42424242-4242-4242-8242-424242424242"))
MATCH_TOLERANCE_SEC = 1.0


def _rss_mb(which: int) -> float:
    # ru_maxrss: KB on Linux, bytes on macOS → normalize to MB.
    raw = resource.getrusage(which).ru_maxrss
    return raw / 1024.0 if platform.system() == "Linux" else raw / (1024.0 * 1024.0)


def _peak_rss_mb() -> float:
    # The pipeline's own RSS (where the PCM/feature buffers live).
    return _rss_mb(resource.RUSAGE_SELF)


def _match_events(detected: list[Any], gt: list[Any], tol: float):
    """Greedy 1:1 temporal match on (family, kind, |Δt| ≤ tol)."""
    used: set[int] = set()
    boundary_errors: list[float] = []
    true_positives = 0
    false_negatives: list[dict[str, Any]] = []
    for g in gt:
        hit = None
        for i, e in enumerate(detected):
            if i in used:
                continue
            if e.family == g.family and e.kind == g.kind and abs(e.t - g.t) <= tol:
                hit = i
                break
        if hit is None:
            false_negatives.append({"t": g.t, "family": g.family, "kind": g.kind, "note": g.note})
            continue
        used.add(hit)
        true_positives += 1
        boundary_errors.append(abs(detected[hit].t - g.t))
    false_positives = [
        {"t": round(e.t, 3), "family": e.family, "kind": e.kind}
        for i, e in enumerate(detected)
        if i not in used and e.kind in CORE_KINDS
    ]
    return true_positives, false_positives, false_negatives, boundary_errors


def _candidate_iou_duplicates(candidates: list[Any], threshold: float = 0.5) -> int:
    n = 0
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            a, b = candidates[i], candidates[j]
            inter = max(0.0, min(a.end, b.end) - max(a.start, b.start))
            union = max(a.end, b.end) - min(a.start, b.start)
            if union > 0 and inter / union >= threshold:
                n += 1
    return n


def _decode_counter():
    state = {"audio": 0, "visual": 0}
    real_audio = pipeline_mod.extract_chunk_wav
    real_visual = pipeline_mod.extract_visual_features

    async def audio(ffmpeg_path, source, offset, length, destination, timeout_seconds):
        state["audio"] += 1
        return await real_audio(ffmpeg_path, source, offset, length, destination, timeout_seconds)

    async def visual(ffmpeg_path, source, offset, length, timeout_seconds, scene_threshold=0.3):
        state["visual"] += 1
        return await real_visual(ffmpeg_path, source, offset, length, timeout_seconds, scene_threshold)

    def restore():
        pipeline_mod.extract_chunk_wav = real_audio
        pipeline_mod.extract_visual_features = real_visual

    def install():
        pipeline_mod.extract_chunk_wav = audio
        pipeline_mod.extract_visual_features = visual

    return state, install, restore


async def _render_top(ffmpeg: str, ffprobe: str, fixture: BenchmarkFixture,
                      candidates: list[Any], out_dir: Path) -> dict[str, Any]:
    if not candidates:
        return {"valid": False, "reason": "no candidates to render"}
    top = candidates[0]
    clip = out_dir / "top-candidate.mp4"
    renderer = FFmpegRenderer(ffmpeg, ffprobe, RenderProfile(), timeout_seconds=300.0)
    await renderer.render(str(fixture.source), top.start, max(1.0, top.end - top.start),
                          RenderSpec(), str(clip))
    info = await verify_render_output(ffprobe, clip, RenderProfile(),
                                      expected_duration=top.end - top.start,
                                      timeout_seconds=60.0)
    src_info = await probe_media(ffprobe, clip, 60.0)
    # A/V sync: the two streams of the rendered clip must match in duration.
    import json as _j
    import subprocess as _sp

    def _ffprobe_streams():
        out = _sp.run([ffprobe, "-v", "error", "-show_streams",
                       "-of", "json", str(clip)], capture_output=True, check=True)
        return _j.loads(out.stdout.decode())["streams"]

    streams = await asyncio.to_thread(_ffprobe_streams)
    vd = next((float(s.get("duration") or 0) for s in streams if s.get("codec_type") == "video"), None)
    ad = next((float(s.get("duration") or 0) for s in streams if s.get("codec_type") == "audio"), None)
    return {
        "valid": True,
        "width": info.width,
        "height": info.height,
        "fps_measured": src_info.avg_fps,
        "video_duration_sec": vd,
        "audio_duration_sec": ad,
        "av_delta_sec": round(abs((vd or 0) - (ad or 0)), 4) if vd is not None and ad is not None else None,
        "expected_contract": "1080x1920 H.264+AAC (existing Phase 5 contract)",
    }


def run_benchmark(ffmpeg: str, ffprobe: str, workdir: Path,
                  chunk_seconds: int = 10, overlap_seconds: int = 2,
                  render_top: bool = True) -> dict[str, Any]:
    workdir.mkdir(parents=True, exist_ok=True)
    fixture = build_stream_fixture(ffmpeg, workdir / "fixture")

    state, install, restore = _decode_counter()
    install()
    t_start = time.monotonic()
    rss_start = _peak_rss_mb()
    try:
        # ---- phase 1: interrupted run (budget 2 of 4 chunks) ----------------
        client = FakeSupabaseClient()
        config = PipelineConfig(chunk_seconds=chunk_seconds, overlap_seconds=overlap_seconds,
                                chunks_per_claim=2, chunk_decode_timeout_seconds=180.0,
                                visual_pass_timeout_seconds=180.0, requested_clips=5)
        pipe = StreamIntelPipeline(ffmpeg, IntelStore(client), config)
        r1 = asyncio.run(pipe.run(JOB, fixture.source, fixture.duration, workdir / "temp"))
        assert r1.status == "incomplete"
        decodes_after_run1 = dict(state)

        # ---- phase 2: resume → complete -------------------------------------
        r2 = asyncio.run(pipe.run(JOB, fixture.source, fixture.duration, workdir / "temp"))
        assert r2.status == "complete"
        decodes_after_resume = dict(state)

        # ---- phase 3: clean reference run (fresh store, full budget) ---------
        clean_pipe = StreamIntelPipeline(
            ffmpeg, IntelStore(FakeSupabaseClient()),
            PipelineConfig(chunk_seconds=chunk_seconds, overlap_seconds=overlap_seconds,
                           chunks_per_claim=100, chunk_decode_timeout_seconds=180.0,
                           visual_pass_timeout_seconds=180.0, requested_clips=5))
        clean = asyncio.run(clean_pipe.run(JOB, fixture.source, fixture.duration, workdir / "clean"))
        assert clean.status == "complete"
    finally:
        restore()
    t_end = time.monotonic()

    wall = t_end - t_start
    rss_peak = _peak_rss_mb()

    # ---- event metrics (resumed run vs ground truth) -------------------------
    detected = r2.events
    core_detected = [e for e in detected if e.kind in CORE_KINDS]
    tp, fp, fn, boundary_errors = _match_events(core_detected, fixture.events, MATCH_TOLERANCE_SEC)
    precision = tp / (tp + len(fp)) if (tp + len(fp)) else 1.0
    recall = tp / len(fixture.events) if fixture.events else 1.0
    secondary = [e for e in detected if e.kind in SECONDARY_KINDS]

    # ---- duplicates -----------------------------------------------------------
    event_keys = [e.key_seed for e in detected]
    event_duplicates = len(event_keys) - len(set(event_keys))
    candidate_duplicates = _candidate_iou_duplicates(r2.candidates)

    # ---- chunk-boundary correctness -------------------------------------------
    def _count_for(g):
        return sum(1 for e in detected
                   if e.family == g.family and e.kind == g.kind and abs(e.t - g.t) <= MATCH_TOLERANCE_SEC)

    straddle_results = [{"t": g.t, "kind": g.kind, "persisted": _count_for(g)} for g in fixture.straddled]
    boundary_ok = all(r["persisted"] == 1 for r in straddle_results)

    # ---- recovery correctness ---------------------------------------------------
    expected_remaining = r2.total_chunks - r1.processed_chunks
    reprocessed = r2.processed_chunks - expected_remaining
    sig = lambda res: sorted((round(e.t, 1), e.family, e.kind) for e in res.events)
    recovery_state_matches_clean = sig(r2) == sig(clean)

    # ---- render ------------------------------------------------------------------
    render_report: dict[str, Any] = {"enabled": render_top}
    if render_top:
        render_report.update(asyncio.run(_render_top(
            ffmpeg, ffprobe, fixture, r2.candidates, workdir / "render")))

    return {
        "fixture": {
            "type": "synthetic-deterministic",
            "human_annotated": False,
            "note": ("SYNTHETIC scripted fixture: measures pipeline correctness, "
                     "recovery and resources. It is NOT a human-annotated real-world "
                     "accuracy benchmark; targets below that require real footage "
                     "are marked unmeasurable."),
            "duration_seconds": fixture.duration,
            "scripted_events": [
                {"t": g.t, "family": g.family, "kind": g.kind, "note": g.note}
                for g in fixture.events
            ],
        },
        "config": {"chunk_seconds": chunk_seconds, "overlap_seconds": overlap_seconds,
                   "match_tolerance_sec": MATCH_TOLERANCE_SEC,
                   "ffmpeg": ffmpeg},
        "measured": {
            "event_detection": {
                "precision": round(precision, 4),
                "recall": round(recall, 4),
                "true_positives": tp,
                "false_positives": fp,
                "false_negatives": fn,
                "boundary_error_sec": {
                    "mean": round(sum(boundary_errors) / len(boundary_errors), 4) if boundary_errors else None,
                    "max": round(max(boundary_errors), 4) if boundary_errors else None,
                },
                "secondary_heuristic_events_excluded": [
                    {"t": round(e.t, 3), "kind": e.kind} for e in secondary
                ],
            },
            "duplicates": {
                "event_key_duplicates": event_duplicates,
                "candidate_pairs_iou_ge_0.5": candidate_duplicates,
            },
            "chunk_boundary": {
                "straddled_events": straddle_results,
                "all_persisted_exactly_once": boundary_ok,
            },
            "recovery": {
                "total_chunks": r2.total_chunks,
                "interrupted_after_chunks": r1.processed_chunks,
                "chunks_reprocessed_on_resume": reprocessed,
                "audio_decodes_run1": decodes_after_run1["audio"],
                "audio_decodes_resume_run": decodes_after_resume["audio"] - decodes_after_run1["audio"],
                "final_state_matches_clean_run": recovery_state_matches_clean,
            },
            "throughput": {
                "wall_seconds": round(wall, 3),
                "time_per_source_hour_seconds": round(wall / (fixture.duration / 3600.0), 3),
            },
            "memory": {
                "peak_rss_self_delta_mb": round(rss_peak - rss_start, 1),
                "peak_rss_children_max_mb": round(_rss_mb(resource.RUSAGE_CHILDREN), 1),
                "note": "self = the pipeline process (PCM/feature buffers); children = the largest single FFmpeg subprocess",
            },
            "render": render_report,
        },
        "targets": {
            "event_precision": ">= 0.9 on human-annotated real VODs (unmeasurable on synthetic fixture)",
            "event_recall": ">= 0.8 on human-annotated real VODs (unmeasurable on synthetic fixture)",
            "boundary_accuracy": "median |Δt| <= 1.0 s vs human annotation (unmeasurable on synthetic fixture)",
            "time_per_source_hour": "<= 60 s (target for 1080p60 sources; this fixture is 320x240@24)",
            "peak_memory": "<= 512 MB per worker process (no whole-recording buffering)",
            "recovery": "zero reprocessed chunks, identical final state (measurable — see measured.recovery)",
            "duplicates": "zero duplicate events / near-duplicate clips (measurable — see measured.duplicates)",
            "render": "valid 1080x1920 H.264+AAC with |A/V delta| < 150 ms (measurable — see measured.render)",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stream-intelligence benchmark (synthetic fixture)")
    parser.add_argument("--workdir", default="benchmarks-out", type=Path)
    parser.add_argument("--out", default=None, type=Path)
    parser.add_argument("--chunk", type=int, default=10)
    parser.add_argument("--overlap", type=int, default=2)
    parser.add_argument("--no-render", action="store_true")
    args = parser.parse_args(argv)

    def resolve(name: str) -> str:
        env = os.environ.get(f"{name.upper()}_BIN")
        if env:
            return env
        on_path = shutil.which(name)
        if on_path:
            return on_path
        repo = Path(__file__).resolve().parents[2]
        if name == "ffmpeg":
            candidates = [repo / "node_modules" / "ffmpeg-static" / "ffmpeg"]
        else:
            candidates = [
                repo / "node_modules" / "ffprobe-static" / "bin" / system_dir / machine_dir / "ffprobe"
                for system_dir in ("linux", "darwin", "win32")
                for machine_dir in ("x64", "arm64")
            ]
        for static in candidates:
            if static.is_file():
                return str(static)
        raise SystemExit(f"{name} binary not found (set {name.upper()}_BIN)")

    report = run_benchmark(resolve("ffmpeg"), resolve("ffprobe"), args.workdir,
                           chunk_seconds=args.chunk, overlap_seconds=args.overlap,
                           render_top=not args.no_render)
    out = args.out or (args.workdir / "report.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"benchmark report written to {out}")
    print(json.dumps(report["measured"]["event_detection"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
