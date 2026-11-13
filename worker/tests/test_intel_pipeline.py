"""Pipeline recovery / resume tests (TASK 007) — the DEFINITION-OF-DONE test.

A long recording (scripted, deterministic, real FFmpeg decode) is processed
incrementally in bounded chunks. The test proves:

1. a run stops cleanly at its per-claim budget (``incomplete``, not an error);
2. a restart resumes from checkpoints and processes ONLY the remaining
   chunks — ZERO reprocessing (measured by decoding-offset instrumentation);
3. a HARD CRASH between the events-upsert and the checkpoint is safe: the
   resumed run re-detects the in-flight chunk and the upsert exact-dedups,
   yielding the SAME final events and SAME candidates as a clean run;
4. a boundary-straddling event (detected by two neighbouring chunks) is
   persisted EXACTLY ONCE (midpoint ownership);
5. cancellation keeps completed chunks checkpointed and cleans temp files;
6. a completed job re-run is a no-op (idempotent) — zero decodes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

import pytest

import app.intel.pipeline as pipeline_mod
from app.intel.checkpoints import IntelStore
from app.intel.pipeline import (
    PipelineCancelled,
    PipelineConfig,
    StreamIntelPipeline,
)
from app.intel.signals import KIND_ENERGY_SPIKE, KIND_SCENE_CHANGE, KIND_TRANSIENT
from conftest import _run_ffmpeg
from intel_fixtures import FakeSupabaseClient

JOB = str(UUID("11111111-1111-4111-8111-111111111111"))


# ---- scripted source: 8 s, burst + hard cut at the chunk 2/3 boundary -------------
#
# chunk=3 s, overlap=1 s → spans: [0,3) [2,5) [4,7) [6,8)
# ownership: span0 [0,2.5) span1 [2.5,4.5) span2 [4.5,6.5) span3 [6.5,∞)
# Scripted: audio noise burst 6.0–6.3 + hard video cut at 6.2 → both inside
# span2 AND span3 (boundary straddle), owned by span2.

TOTAL = 8.0
BURST_START, BURST_END = 6.0, 6.3
CUT_AT = 6.2


@pytest.fixture(scope="module")
def intel_source(ffmpeg_bin, tmp_path_factory) -> Path:
    out_dir = tmp_path_factory.mktemp("intel-source")
    src = out_dir / "stream.mp4"
    # audio = quiet 440 Hz bed (vol 0.05) + white-noise burst at 6.0–6.3 (vol 0.4)
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


class DecodeCounter:
    """Instruments the pipeline's two bounded decoders (monkeypatched)."""

    def __init__(self):
        self.audio_offsets: list[float] = []
        self.visual_offsets: list[float] = []
        self._real_audio = pipeline_mod.extract_chunk_wav
        self._real_visual = pipeline_mod.extract_visual_features

    def install(self, monkeypatch):
        async def audio(ffmpeg_path, source, offset, length, destination, timeout_seconds):
            self.audio_offsets.append(offset)
            return await self._real_audio(ffmpeg_path, source, offset, length, destination, timeout_seconds)

        async def visual(ffmpeg_path, source, offset, length, timeout_seconds, scene_threshold=0.3):
            self.visual_offsets.append(offset)
            return await self._real_visual(ffmpeg_path, source, offset, length, timeout_seconds, scene_threshold)

        monkeypatch.setattr(pipeline_mod, "extract_chunk_wav", audio)
        monkeypatch.setattr(pipeline_mod, "extract_visual_features", visual)

    def uninstall(self, monkeypatch):
        monkeypatch.undo()


def _config(chunks_per_claim: int = 2) -> PipelineConfig:
    return PipelineConfig(chunk_seconds=3, overlap_seconds=1, chunks_per_claim=chunks_per_claim,
                          chunk_decode_timeout_seconds=120.0, visual_pass_timeout_seconds=120.0,
                          requested_clips=5)


def _events_signature(result) -> list[tuple]:
    return sorted((round(e.t, 1), e.family, e.kind) for e in result.events)


def _candidates_signature(result) -> list[tuple]:
    return [(round(c.start, 1), round(c.end, 1), round(c.score, 1), c.category,
            round(c.confidence, 2)) for c in result.candidates]


# =============================================================================
# 1 + 2: budget → incomplete → resume with ZERO reprocessing
# =============================================================================

def test_resume_after_interruption_zero_reprocessing(ffmpeg_bin, intel_source, tmp_path, monkeypatch):
    counter = DecodeCounter()
    counter.install(monkeypatch)
    client = FakeSupabaseClient()
    store = IntelStore(client)
    pipe = StreamIntelPipeline(ffmpeg_bin, store, _config(chunks_per_claim=2))
    temp = tmp_path / "temp"

    # Run 1: the claim budget (2 chunks) is exhausted → incomplete, no error.
    r1 = asyncio.run(pipe.run(JOB, intel_source, TOTAL, temp))
    assert r1.status == "incomplete"
    assert r1.total_chunks == 4
    assert r1.processed_chunks == 2
    assert r1.remaining_chunks == 2
    assert r1.events == [] and r1.candidates == []  # nothing concluded early
    assert counter.audio_offsets == [0.0, 2.0]
    assert counter.visual_offsets == [0.0, 2.0]

    # Run 2 (after the "interruption"): resumes from checkpoints.
    r2 = asyncio.run(pipe.run(JOB, intel_source, TOTAL, temp))
    assert r2.status == "complete"
    assert r2.processed_chunks == 2          # ONLY the two remaining chunks
    assert r2.remaining_chunks == 0

    # ZERO reprocessing: every offset decoded exactly once, in order.
    assert counter.audio_offsets == [0.0, 2.0, 4.0, 6.0]
    assert counter.visual_offsets == [0.0, 2.0, 4.0, 6.0]

    # Temp files cleaned on every path.
    assert list(temp.glob("intel-chunk-*.wav")) == []

    # The complete result carries the full event set + ranked candidates.
    assert r2.events, "the scripted burst+cut must produce persisted events"
    assert r2.candidates, "evidence must yield at least one ranked candidate"
    top = r2.candidates[0]
    assert top.start <= BURST_START < top.end  # the event core is inside clip 1
    for c in r2.candidates:
        assert 0.0 <= c.start < c.end <= TOTAL
    scores = [c.score for c in r2.candidates]
    assert scores == sorted(scores, reverse=True)


# =============================================================================
# 3: hard crash between events-upsert and checkpoint → safe resume
# =============================================================================

def test_hard_crash_between_events_and_checkpoint_is_safe(
    ffmpeg_bin, intel_source, tmp_path, monkeypatch
):
    # Reference: clean full run on a fresh store.
    counter = DecodeCounter()
    counter.install(monkeypatch)
    clean_client = FakeSupabaseClient()
    clean_pipe = StreamIntelPipeline(ffmpeg_bin, IntelStore(clean_client), _config(10))
    clean = asyncio.run(clean_pipe.run(JOB, intel_source, TOTAL, tmp_path / "t1"))
    assert clean.status == "complete"

    # Crash run: same source, fresh store, full budget, but the store FAILS
    # on the 3rd checkpoint write — i.e. the process dies right AFTER the
    # events-upsert of chunk 2, BEFORE chunk 2's checkpoint is stored.
    import app.intel.checkpoints as checkpoints_mod

    crash_client = FakeSupabaseClient()

    class CrashStore(IntelStore):
        _marks = {"n": 0}

        async def mark_chunk_done(self, job_id, chunk_index, start, end, features):
            type(self)._marks["n"] += 1
            if type(self)._marks["n"] == 3:  # chunks 0,1 checkpointed; chunk 2 dies
                raise checkpoints_mod.IntelStoreError("checkpoint could not be stored")
            return await super().mark_chunk_done(job_id, chunk_index, start, end, features)

    crash_pipe = StreamIntelPipeline(ffmpeg_bin, CrashStore(crash_client), _config(10))
    with pytest.raises(checkpoints_mod.IntelStoreError):
        asyncio.run(crash_pipe.run(JOB, intel_source, TOTAL, tmp_path / "t2"))

    real_store = IntelStore(crash_client)

    # State after the crash: chunks 0,1 checkpointed; chunk 2's EVENTS were
    # already persisted (the crash happened after the events-upsert).
    done_after_crash = asyncio.run(real_store.load_done_chunks(JOB))
    assert set(done_after_crash) == {0, 1}
    events_after_crash = asyncio.run(real_store.load_events(JOB))
    assert any(BURST_START - 0.2 <= e.t <= BURST_END + 0.2 for e in events_after_crash), \
        "chunk 2's events must already be persisted before the crash"

    # Resume: chunk 2 is re-detected (not checkpointed) → events upserted
    # AGAIN → exact-dedup. The final state must equal the clean run.
    resumed = asyncio.run(
        StreamIntelPipeline(ffmpeg_bin, real_store, _config(10)).run(
            JOB, intel_source, TOTAL, tmp_path / "t3"))
    assert resumed.status == "complete"

    assert _events_signature(resumed) == _events_signature(clean)
    assert _candidates_signature(resumed) == _candidates_signature(clean)
    # No duplicate event rows were created by the re-detection.
    final_events = asyncio.run(real_store.load_events(JOB))
    keys = [e.key_seed for e in final_events]
    assert len(keys) == len(set(keys))


# =============================================================================
# 4: boundary-straddling event persisted exactly once
# =============================================================================

def test_boundary_event_persisted_exactly_once(ffmpeg_bin, intel_source, tmp_path):
    client = FakeSupabaseClient()
    pipe = StreamIntelPipeline(ffmpeg_bin, IntelStore(client), _config(10))
    result = asyncio.run(pipe.run(JOB, intel_source, TOTAL, tmp_path))
    assert result.status == "complete"

    def _count(kind, lo=BURST_START - 0.4, hi=BURST_END + 0.6):
        return sum(1 for e in result.events if e.kind == kind and lo <= e.t <= hi)

    # The burst straddles the span2/span3 boundary: both chunks decode it,
    # exactly ONE owner persists it.
    assert _count(KIND_ENERGY_SPIKE) == 1
    assert _count(KIND_TRANSIENT) == 1
    # The cut at 6.2 also straddles the boundary → one scene_change.
    assert sum(1 for e in result.events
               if e.kind == KIND_SCENE_CHANGE and abs(e.t - CUT_AT) <= 0.4) == 1
    # Nothing is lost: every expected family is present.
    families = {e.family for e in result.events}
    assert families == {"audio", "visual"}


# =============================================================================
# 5: cancellation keeps completed chunks, cleans temp
# =============================================================================

def test_cancellation_keeps_progress_and_cleans(ffmpeg_bin, intel_source, tmp_path, monkeypatch):
    counter = DecodeCounter()
    counter.install(monkeypatch)
    client = FakeSupabaseClient()
    pipe = StreamIntelPipeline(ffmpeg_bin, IntelStore(client), _config(10))
    temp = tmp_path / "temp"

    stop_after = {"n": 0}

    def should_stop():
        stop_after["n"] += 1
        return stop_after["n"] > 1  # allow chunk 0, refuse before chunk 1

    with pytest.raises(PipelineCancelled):
        asyncio.run(pipe.run(JOB, intel_source, TOTAL, temp, should_stop=should_stop))

    done = asyncio.run(IntelStore(client).load_done_chunks(JOB))
    assert set(done) == {0}                      # chunk 0 checkpointed
    assert counter.audio_offsets == [0.0]        # chunk 1 never decoded

    # Resume after the cancellation → completes, chunk 0 NOT reprocessed.
    result = asyncio.run(pipe.run(JOB, intel_source, TOTAL, temp))
    assert result.status == "complete"
    assert counter.audio_offsets == [0.0, 2.0, 4.0, 6.0]
    assert list(temp.glob("intel-chunk-*.wav")) == []


# =============================================================================
# 6: idempotent re-run of a completed job
# =============================================================================

def test_completed_job_rerun_is_noop(ffmpeg_bin, intel_source, tmp_path, monkeypatch):
    counter = DecodeCounter()
    counter.install(monkeypatch)
    client = FakeSupabaseClient()
    pipe = StreamIntelPipeline(ffmpeg_bin, IntelStore(client), _config(10))
    r1 = asyncio.run(pipe.run(JOB, intel_source, TOTAL, tmp_path))
    assert r1.status == "complete"

    r2 = asyncio.run(pipe.run(JOB, intel_source, TOTAL, tmp_path))
    assert r2.status == "complete"
    assert r2.processed_chunks == 0              # nothing to do
    assert counter.audio_offsets == [0.0, 2.0, 4.0, 6.0]  # no new decodes at all
    assert _events_signature(r2) == _events_signature(r1)
    assert _candidates_signature(r2) == _candidates_signature(r1)


# =============================================================================
# resource bounds: sequential decodes, bounded temp, O(chunks) fusion input
# =============================================================================

def test_decodes_are_sequential_and_bounded(ffmpeg_bin, intel_source, tmp_path, monkeypatch):
    seen = {"now": 0, "max_concurrent": 0, "max_temp_files": 0, "lengths": []}
    real_audio = pipeline_mod.extract_chunk_wav

    async def tracking_audio(ffmpeg_path, source, offset, length, destination, timeout_seconds):
        seen["now"] += 1
        seen["max_concurrent"] = max(seen["max_concurrent"], seen["now"])
        seen["lengths"].append(length)
        # bounded temp: at most the current chunk's wav may exist
        existing = list(destination.parent.glob("intel-chunk-*.wav"))
        seen["max_temp_files"] = max(seen["max_temp_files"], len(existing))
        try:
            return await real_audio(ffmpeg_path, source, offset, length, destination, timeout_seconds)
        finally:
            seen["now"] -= 1

    monkeypatch.setattr(pipeline_mod, "extract_chunk_wav", tracking_audio)
    pipe = StreamIntelPipeline(ffmpeg_bin, IntelStore(FakeSupabaseClient()), _config(10))
    result = asyncio.run(pipe.run(JOB, intel_source, TOTAL, tmp_path))
    assert result.status == "complete"
    assert seen["max_concurrent"] == 1                    # strictly sequential
    assert all(0 < length <= 3.0 for length in seen["lengths"])  # per-chunk bounded
    assert seen["max_temp_files"] <= 1                     # one temp wav at a time
    assert list(tmp_path.glob("intel-chunk-*.wav")) == []  # cleaned after the run
