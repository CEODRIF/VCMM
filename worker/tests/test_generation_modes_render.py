"""TASK 015 P3 — generation-mode RENDERING tests (REAL FFmpeg integration).

The renderer, the ffprobe output-contract verification, and the
RenderProcessor lifecycle run here with REAL FFmpeg/ffprobe binaries over
small synthetic lavfi fixtures (session-scoped, 6s @30fps H.264+AAC).
Fake queue/storage/clip-store stand in only for the transport — every
output file that "uploads" is a genuine, ffprobe-validated render.

Covers: explicit 30 FPS render, explicit 60 FPS render from a 30 FPS
source (genuine 60fps stream via frame duplication — verified, not
claimed native), legacy source-rate-preserving render, fps-mismatch
rejection, A/V desync rejection, 1080x1920 validation from a square
source, the full-coverage (Mode A) end-to-end job, the highlights (Mode B)
end-to-end job, and failed-render retry without duplicate uploads.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from app.ai.clip_planning import plan_full_coverage
from app.processors.rendering import RenderProcessor
from app.queue.models import ERROR_RENDER_FAILED
from app.rendering.base import RenderSpec
from app.rendering.ffmpeg import FFmpegError
from app.rendering.renderer import FFmpegRenderer
from app.rendering.profiles import RenderProfile
from app.rendering.validation import RenderValidationError, verify_render_output
from app.storage.supabase_storage import StorageUploadError
from test_render_processor import (
    FakeClipStore,
    FakeQueue,
    FakeStorage,
    RecordingFFmpegRenderer,
    WORKER,
    JOB_ID,
    clip,
    make_settings,
)


def run(coro):
    return asyncio.run(coro)


def make_job(**overrides) -> dict:
    job = {
        "id": JOB_ID,
        "status": "rendering",
        "_worker_id": WORKER,
        "source_path": f"sources/{JOB_ID}/original/video.mp4",
    }
    job.update(overrides)
    return job


class UploadRecordingStorage(FakeStorage):
    """Keeps the exact bytes of every "uploaded" object so the tests can
    ffprobe the REAL produced files (not just the lifecycle)."""

    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.uploaded: dict[str, bytes] = {}

    async def upload_from(self, object_path, source_file, max_bytes=None):
        if object_path in self.upload_error_paths or self.upload_error:
            raise StorageUploadError("upload failed")
        data = Path(source_file).read_bytes()
        self.uploaded[object_path] = data
        self.uploads.append((object_path, len(data)))
        return len(data)


def ffprobe_json(ffprobe: str, path: Path) -> dict:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()[:500]
    return json.loads(result.stdout.decode())


def assert_vertical_h264_aac(ffprobe: str, data: bytes, tmp_path: Path, name: str,
                             fps: float, duration: float) -> dict:
    """The full output contract on a real file: 1080x1920 9:16, H.264,
    AAC where present, the expected fps, the expected duration."""
    out = tmp_path / name
    out.write_bytes(data)
    meta = ffprobe_json(ffprobe, out)
    streams = {s["codec_type"]: s for s in meta["streams"]}
    assert streams["video"]["codec_name"] == "h264", streams["video"].get("codec_name")
    assert (streams["video"]["width"], streams["video"]["height"]) == (1080, 1920)
    assert "aac" == streams["audio"]["codec_name"]
    num, den = streams["video"]["avg_frame_rate"].split("/")
    measured_fps = int(num) / int(den)
    assert measured_fps == pytest.approx(fps, rel=0.05), measured_fps
    measured_duration = float(meta["format"]["duration"])
    assert measured_duration == pytest.approx(duration, abs=0.35), measured_duration
    return meta


# ---- explicit FPS renders (real FFmpeg, real ffprobe verification) -------------

def test_explicit_30fps_render_1080x1920_h264_aac(ffmpeg_bin, ffprobe_bin, render_media, tmp_path):
    renderer = FFmpegRenderer(ffmpeg_bin, ffprobe_bin, timeout_seconds=120)
    out = tmp_path / "c30.mp4"
    run(renderer.render(str(render_media["landscape"]), 0.0, 3.0,
                        RenderSpec(target_fps=30.0), str(out)))
    info = run(verify_render_output(ffprobe_bin, out, RenderProfile(), expected_duration=3.0,
                                    timeout_seconds=60, expected_fps=30))
    assert (info.width, info.height) == (1080, 1920)
    assert info.fps == pytest.approx(30.0, rel=0.05)
    assert info.audio_duration_seconds is not None
    assert abs(info.audio_duration_seconds - info.duration_seconds) <= 0.2  # A/V sync
    meta = ffprobe_json(ffprobe_bin, out)
    streams = {s["codec_type"]: s for s in meta["streams"]}
    assert streams["video"]["codec_name"] == "h264"
    assert streams["audio"]["codec_name"] == "aac"


def test_explicit_60fps_render_from_30fps_source_is_genuine_60fps(ffmpeg_bin, ffprobe_bin,
                                                                  render_media, tmp_path):
    """The honest 60fps contract: a 30fps source rendered at the user's
    explicit 60 FPS choice produces a GENUINE 60fps stream (FFmpeg
    duplicates frames — the frame count doubles; it is never claimed to be
    native-60fps material)."""
    renderer = FFmpegRenderer(ffmpeg_bin, ffprobe_bin, timeout_seconds=120)
    out30 = tmp_path / "c30.mp4"
    out60 = tmp_path / "c60.mp4"
    run(renderer.render(str(render_media["landscape"]), 0.0, 3.0,
                        RenderSpec(target_fps=30.0), str(out30)))
    run(renderer.render(str(render_media["landscape"]), 0.0, 3.0,
                        RenderSpec(target_fps=60.0), str(out60)))
    # The 60fps output passes the 60fps verification (5% tolerance).
    info = run(verify_render_output(ffprobe_bin, out60, RenderProfile(), expected_duration=3.0,
                                    timeout_seconds=60, expected_fps=60))
    assert info.fps == pytest.approx(60.0, rel=0.05)
    # Frame-count evidence of the duplication (honesty fact).
    n30 = int(ffprobe_json(ffprobe_bin, out30)["streams"][0].get("nb_frames") or 0)
    n60 = int(ffprobe_json(ffprobe_bin, out60)["streams"][0].get("nb_frames") or 0)
    assert n30 > 0 and n60 > 0
    assert n60 == pytest.approx(2 * n30, rel=0.1)  # 30 -> 60 doubles the frames


def test_legacy_target_fps_none_preserves_the_measured_source_rate(ffmpeg_bin, ffprobe_bin,
                                                                   render_media, tmp_path):
    renderer = FFmpegRenderer(ffmpeg_bin, ffprobe_bin, timeout_seconds=120)
    out = tmp_path / "c_legacy.mp4"
    run(renderer.render(str(render_media["landscape"]), 0.0, 3.0,
                        RenderSpec(target_fps=None), str(out)))
    info = run(verify_render_output(ffprobe_bin, out, RenderProfile(), expected_duration=3.0,
                                    timeout_seconds=60))
    # 30fps source, no explicit choice -> the output stays at the source rate.
    assert info.fps == pytest.approx(30.0, rel=0.05)


def test_verify_rejects_a_30fps_output_claimed_as_60fps(ffmpeg_bin, ffprobe_bin,
                                                        render_media, tmp_path):
    renderer = FFmpegRenderer(ffmpeg_bin, ffprobe_bin, timeout_seconds=120)
    out = tmp_path / "c30.mp4"
    run(renderer.render(str(render_media["landscape"]), 0.0, 3.0,
                        RenderSpec(target_fps=30.0), str(out)))
    with pytest.raises(RenderValidationError, match="frame rate"):
        run(verify_render_output(ffprobe_bin, out, RenderProfile(), expected_duration=3.0,
                                 timeout_seconds=60, expected_fps=60))


def test_verify_rejects_av_duration_drift(ffmpeg_bin, ffprobe_bin, render_media, tmp_path,
                                          monkeypatch):
    import app.rendering.validation as validation_mod

    renderer = FFmpegRenderer(ffmpeg_bin, ffprobe_bin, timeout_seconds=120)
    out = tmp_path / "c.mp4"
    run(renderer.render(str(render_media["landscape"]), 0.0, 3.0,
                        RenderSpec(target_fps=30.0), str(out)))
    real = run(validation_mod.probe_render_output(ffprobe_bin, out, 60))
    drifted = dataclasses.replace(real,
                                  audio_duration_seconds=real.duration_seconds + 0.5)

    async def fake_probe(*_a, **_k):
        return drifted

    monkeypatch.setattr(validation_mod, "probe_render_output", fake_probe)
    with pytest.raises(RenderValidationError, match="desynchronized"):
        run(validation_mod.verify_render_output(ffprobe_bin, out, RenderProfile(), expected_duration=3.0,
                                                timeout_seconds=60, expected_fps=30))


def test_1080p_vertical_output_from_square_source(ffmpeg_bin, ffprobe_bin, render_media,
                                                  tmp_path):
    """The 9:16 1080x1920 contract holds even from a non-landscape source
    (safe vertical framing, no distortion)."""
    renderer = FFmpegRenderer(ffmpeg_bin, ffprobe_bin, timeout_seconds=120)
    out = tmp_path / "square.mp4"
    run(renderer.render(str(render_media["square"]), 0.0, 3.0,
                        RenderSpec(target_fps=30.0), str(out)))
    info = run(verify_render_output(ffprobe_bin, out, RenderProfile(), expected_duration=3.0,
                                    timeout_seconds=60, expected_fps=30))
    assert (info.width, info.height) == (1080, 1920)


# ---- end-to-end jobs (real renders through the RenderProcessor) ----------------

def test_full_coverage_job_renders_real_playable_clips_at_60fps(
        ffmpeg_bin, ffprobe_bin, render_media, tmp_path):
    """MODE A proof: the 6s source tiled into 3x2s clips (plan_full_coverage)
    renders through the real processor at the job's explicit 60 FPS — every
    uploaded object is a genuine, ffprobe-validated 1080x1920 H.264+AAC file."""
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin, render_concurrency=1)
    queue = FakeQueue()
    storage = UploadRecordingStorage(render_media["landscape"].read_bytes())
    spans = plan_full_coverage(6.0, 2.0)
    assert [(s.start, s.end) for s in spans] == [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0)]
    c1, c2, c3 = str(uuid4()), str(uuid4()), str(uuid4())
    paths = {c1: f"clips/{JOB_ID}/{c1}.mp4", c2: f"clips/{JOB_ID}/{c2}.mp4",
             c3: f"clips/{JOB_ID}/{c3}.mp4"}
    store = FakeClipStore([
        clip(c1, spans[0].start, spans[0].end, order=0),
        clip(c2, spans[1].start, spans[1].end, order=1),
        clip(c3, spans[2].start, spans[2].end, order=2),
    ])
    renderer = RecordingFFmpegRenderer(ffmpeg_bin, ffprobe_bin, timeout_seconds=120)
    processor = RenderProcessor(settings, queue, storage, store, renderer)

    job = make_job(output_fps=60, generation_mode="full_coverage", requested_clips=None)
    run(processor.process_job(job))

    assert queue.complete_calls == [(JOB_ID, WORKER)]
    assert queue.fail_calls == []
    assert renderer.windows == [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0)]
    assert sorted(storage.uploaded) == sorted(paths.values())
    # The REAL produced files meet the full output contract at 60fps.
    assert_vertical_h264_aac(ffprobe_bin, storage.uploaded[paths[c1]], tmp_path,
                             "fc1.mp4", fps=60, duration=2.0)
    assert_vertical_h264_aac(ffprobe_bin, storage.uploaded[paths[c2]], tmp_path,
                             "fc2.mp4", fps=60, duration=2.0)
    assert_vertical_h264_aac(ffprobe_bin, storage.uploaded[paths[c3]], tmp_path,
                             "fc3.mp4", fps=60, duration=2.0)
    for cid in (c1, c2, c3):
        statuses = [m[2] for m in store.marks if m[1] == cid]
        assert statuses == ["processing", "ready"], statuses


def test_highlights_job_renders_real_playable_clips_at_30fps(
        ffmpeg_bin, ffprobe_bin, render_media, tmp_path):
    """MODE B proof: two independent highlight windows (context-preserving
    start/end, no tiling) render at the job's explicit 30 FPS."""
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin, render_concurrency=1)
    queue = FakeQueue()
    storage = UploadRecordingStorage(render_media["landscape"].read_bytes())
    h1, h2 = str(uuid4()), str(uuid4())
    p1, p2 = f"clips/{JOB_ID}/{h1}.mp4", f"clips/{JOB_ID}/{h2}.mp4"
    store = FakeClipStore([clip(h1, 0.5, 3.0, order=0), clip(h2, 3.5, 5.8, order=1)])
    renderer = RecordingFFmpegRenderer(ffmpeg_bin, ffprobe_bin, timeout_seconds=120)
    processor = RenderProcessor(settings, queue, storage, store, renderer)

    job = make_job(output_fps=30, generation_mode="highlights", requested_clips=2)
    run(processor.process_job(job))

    assert queue.complete_calls == [(JOB_ID, WORKER)]
    assert queue.fail_calls == []
    assert sorted(storage.uploaded) == sorted([p1, p2])
    assert_vertical_h264_aac(ffprobe_bin, storage.uploaded[p1], tmp_path,
                             "hl1.mp4", fps=30, duration=2.5)
    assert_vertical_h264_aac(ffprobe_bin, storage.uploaded[p2], tmp_path,
                             "hl2.mp4", fps=30, duration=2.3)


class FailFirstRenderer(RecordingFFmpegRenderer):
    """Fails exactly the first render (encoder crash), then renders for real."""

    async def render(self, source_path, start, end, spec, output_path):
        if not self.windows:
            raise FFmpegError(ERROR_RENDER_FAILED, "simulated encoder crash")
        return await super().render(source_path, start, end, spec, output_path)


def test_failed_render_retry_produces_each_clip_exactly_once(
        ffmpeg_bin, ffprobe_bin, render_media, tmp_path):
    """Attempt 1: the first render crashes -> the job fails TRANSIENTLY
    (re-claimable) and never completes. Attempt 2 (fresh claim): only the
    non-ready clips are rendered; every object uploads EXACTLY once across
    both attempts (no duplicates), and the job completes."""
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin, render_concurrency=1)
    content = render_media["landscape"].read_bytes()
    a, b = str(uuid4()), str(uuid4())
    pa, pb = f"clips/{JOB_ID}/{a}.mp4", f"clips/{JOB_ID}/{b}.mp4"
    store = FakeClipStore([clip(a, 0.0, 3.0, order=0), clip(b, 3.0, 6.0, order=1)])
    job = make_job(output_fps=30, generation_mode="full_coverage", requested_clips=None)

    # Attempt 1 — the encoder crashes on the first clip.
    queue1 = FakeQueue()
    storage1 = UploadRecordingStorage(content)
    renderer1 = FailFirstRenderer(ffmpeg_bin, ffprobe_bin, timeout_seconds=120)
    run(RenderProcessor(settings, queue1, storage1, store, renderer1).process_job(job))
    assert queue1.complete_calls == []
    assert len(queue1.fail_calls) == 1
    assert queue1.fail_calls[0][2] == ERROR_RENDER_FAILED
    assert queue1.fail_calls[0][4] is False  # transient: re-claimable
    assert storage1.uploads == []            # nothing unverified was uploaded

    # Attempt 2 — a fresh worker re-claims the same job.
    queue2 = FakeQueue()
    storage2 = UploadRecordingStorage(content)
    renderer2 = RecordingFFmpegRenderer(ffmpeg_bin, ffprobe_bin, timeout_seconds=120)
    run(RenderProcessor(settings, queue2, storage2, store, renderer2).process_job(job))
    assert queue2.complete_calls == [(JOB_ID, WORKER)]
    assert queue2.fail_calls == []

    # No duplicates: each object path was uploaded exactly once in total.
    all_uploads = [p for p, _ in storage1.uploads] + [p for p, _ in storage2.uploads]
    assert sorted(all_uploads) == sorted([pa, pb])
    # The produced files are genuine.
    assert_vertical_h264_aac(ffprobe_bin, storage2.uploaded[pa], tmp_path,
                             "retry_a.mp4", fps=30, duration=3.0)
    assert_vertical_h264_aac(ffprobe_bin, storage2.uploaded[pb], tmp_path,
                             "retry_b.mp4", fps=30, duration=3.0)
