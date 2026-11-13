"""RenderProcessor caption wiring tests (Phase 5B).

Fake queue/storage/clip-store (reused from test_render_processor) + REAL
FFmpeg for the render path. Asserts the SPEC the processor builds per clip:
job-level toggle, persisted-payload parsing, clip-relative conversion,
style propagation, and that the whole Phase 5A lifecycle (upload, mark,
complete, resume, failure) is preserved with captions enabled.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
from uuid import UUID

from app.config.settings import Settings
from app.processors.rendering import RenderProcessor
from app.queue.models import ERROR_RENDER_FAILED
from app.rendering.base import RenderSpec
from app.rendering.ffmpeg import FFmpegError
from app.rendering.renderer import FFmpegRenderer
from tests.test_render_processor import (
    CLIP_A,
    CLIP_B,
    JOB_ID,
    PATH_A,
    PATH_B,
    WORKER,
    FakeClipStore,
    FakeQueue,
    FakeStorage,
    clip,
    make_settings,
    run,
)

CLIP_2_START = 1.0


def make_job(captions_enabled: bool = True) -> dict[str, Any]:
    job: dict[str, Any] = {
        "id": JOB_ID,
        "status": "rendering",
        "_worker_id": WORKER,
        "source_path": f"sources/{JOB_ID}/original/video.mp4",
        "captions_enabled": captions_enabled,
    }
    return job


class SpecRecordingRenderer(FFmpegRenderer):
    """Real renderer + spec log."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.specs: list[RenderSpec] = []
        self.raise_error: Optional[Exception] = None

    async def render(self, source_path, start, end, spec, output_path):
        self.specs.append(spec)
        if self.raise_error is not None:
            raise self.raise_error
        return await super().render(source_path, start, end, spec, output_path)


def payload(words: list[tuple[str, float, float]]) -> dict[str, Any]:
    return {"words": [{"w": w, "s": s, "e": e} for w, s, e in words]}


def make_processor(settings, queue, storage, store, renderer) -> RenderProcessor:
    return RenderProcessor(settings, queue, storage, store, renderer)


# ---------------------------------------------------------------------------

def test_spec_carries_clip_relative_words_and_style(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    """The processor converts the persisted ABSOLUTE payload to clip-relative
    words and passes them (with the configured style) to the renderer."""
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin, caption_highlight_color="00FF00")
    queue = FakeQueue()
    storage = FakeStorage(synthetic_videos["valid"].read_bytes())
    store = FakeClipStore([
        {**clip(CLIP_A, 0.0, 1.0, order=0), "captions": payload([("hallo", 0.2, 0.8), ("welt", 0.9, 1.5)])},
        {**clip(CLIP_B, CLIP_2_START, 2.0, order=1), "captions": payload([("zwei", 1.3, 1.6), ("drei", 1.8, 2.4)])},
    ])
    renderer = SpecRecordingRenderer(ffmpeg_bin, ffprobe_bin, timeout_seconds=120)

    run(make_processor(settings, queue, storage, store, renderer).process_job(make_job()))

    assert len(renderer.specs) == 2
    # find the specs by their first word (clip order may vary)
    specs = renderer.specs
    spec_a = next(s for s in specs if s.caption_words and s.caption_words[0].word == "hallo")
    spec_b = next(s for s in specs if s.caption_words and s.caption_words[0].word == "zwei")
    assert spec_a.captions is True
    assert [(w.word, w.start, w.end) for w in spec_a.caption_words] == [
        ("hallo", 0.2, 0.8), ("welt", 0.9, 1.0),  # 1.5 clamped to the clip end
    ]
    # absolute 1.3/1.6 & 1.8/2.4 minus clip start 1.0 -> 0.3/0.6 & 0.8/1.0 (clamped)
    assert [(w.word, w.start, w.end) for w in spec_b.caption_words] == [
        ("zwei", 0.3, 0.6), ("drei", 0.8, 1.0),
    ]
    # style propagated from settings (validated RRGGBB)
    assert spec_a.caption_style is not None
    assert spec_a.caption_style.highlight_color == "00FF00"
    # lifecycle intact: both uploaded, ready, exactly one completion
    assert sorted(p for p, _ in storage.uploads) == sorted([PATH_A, PATH_B])
    assert queue.complete_calls == [(JOB_ID, WORKER)]
    assert queue.fail_calls == []


def test_captions_disabled_is_phase5a_behavior(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    """captions_enabled=false -> spec.captions False, no words, same lifecycle."""
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin)
    queue = FakeQueue()
    storage = FakeStorage(synthetic_videos["valid"].read_bytes())
    store = FakeClipStore([
        {**clip(CLIP_A, 0.0, 1.0, order=0), "captions": payload([("hallo", 0.2, 0.8)])},
    ])
    renderer = SpecRecordingRenderer(ffmpeg_bin, ffprobe_bin, timeout_seconds=120)

    run(make_processor(settings, queue, storage, store, renderer).process_job(make_job(captions_enabled=False)))

    assert renderer.specs[0].captions is False
    assert renderer.specs[0].caption_words == ()
    assert storage.uploads and storage.uploads[0][0] == PATH_A
    assert queue.complete_calls == [(JOB_ID, WORKER)]


def test_clip_without_captions_renders_plain(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    """No captions key (pre-5B rows) -> captions enabled but empty words:
    the exact Phase 5A render, still successful."""
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin)
    queue = FakeQueue()
    storage = FakeStorage(synthetic_videos["valid"].read_bytes())
    store = FakeClipStore([clip(CLIP_A, 0.0, 1.0, order=0)])
    renderer = SpecRecordingRenderer(ffmpeg_bin, ffprobe_bin, timeout_seconds=120)

    run(make_processor(settings, queue, storage, store, renderer).process_job(make_job()))

    assert renderer.specs[0].captions is True
    assert renderer.specs[0].caption_words == ()
    assert queue.complete_calls == [(JOB_ID, WORKER)]


def test_malformed_payload_degrades_to_plain_render(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    """A corrupt persisted payload never crashes the job and never
    fabricates words: the clip renders without subtitles."""
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin)
    queue = FakeQueue()
    storage = FakeStorage(synthetic_videos["valid"].read_bytes())
    store = FakeClipStore([
        {**clip(CLIP_A, 0.0, 1.0, order=0), "captions": ["not", "a", "dict"]},
        {**clip(CLIP_B, CLIP_2_START, 2.0, order=1), "captions": "garbage"},
    ])
    renderer = SpecRecordingRenderer(ffmpeg_bin, ffprobe_bin, timeout_seconds=120)

    run(make_processor(settings, queue, storage, store, renderer).process_job(make_job()))

    for spec in renderer.specs:
        assert spec.captions is True
        assert spec.caption_words == ()
    assert queue.complete_calls == [(JOB_ID, WORKER)]
    assert queue.fail_calls == []


def test_words_outside_window_yield_plain_render(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    """Payload words entirely outside [start, end] clamp to nothing -> plain
    render (nothing is invented)."""
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin)
    queue = FakeQueue()
    storage = FakeStorage(synthetic_videos["valid"].read_bytes())
    store = FakeClipStore([
        {**clip(CLIP_A, 0.0, 1.0, order=0), "captions": payload([("late", 5.0, 6.0), ("early", -3.0, -1.0)])},
    ])
    renderer = SpecRecordingRenderer(ffmpeg_bin, ffprobe_bin, timeout_seconds=120)

    run(make_processor(settings, queue, storage, store, renderer).process_job(make_job()))

    assert renderer.specs[0].caption_words == ()
    assert queue.complete_calls == [(JOB_ID, WORKER)]


def test_resume_with_captions_skips_ready_and_renders_rest(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    """Resume semantics with captions: the ready clip is NOT re-rendered
    (no duplicate upload), the pending clip gets its words."""
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin)
    queue = FakeQueue()
    storage = FakeStorage(synthetic_videos["valid"].read_bytes())
    store = FakeClipStore([
        {**clip(CLIP_A, 0.0, 1.0, status="ready", storage_path=PATH_A, order=0),
         "captions": payload([("skip", 0.1, 0.5)])},
        {**clip(CLIP_B, CLIP_2_START, 2.0, order=1), "captions": payload([("render", 1.2, 1.8)])},
    ])
    renderer = SpecRecordingRenderer(ffmpeg_bin, ffprobe_bin, timeout_seconds=120)

    run(make_processor(settings, queue, storage, store, renderer).process_job(make_job()))

    assert len(renderer.specs) == 1  # only clip B rendered
    assert [(w.word, w.start, w.end) for w in renderer.specs[0].caption_words] == [("render", 0.2, 0.8)]
    assert [p for p, _ in storage.uploads] == [PATH_B]
    assert queue.complete_calls == [(JOB_ID, WORKER)]


def test_renderer_failure_with_captions_is_transient_and_no_completion(ffmpeg_bin, ffprobe_bin, synthetic_videos, tmp_path):
    """A render failure while captions are attached follows the Phase 5A
    failure path: transient fail_job, no completion, job never marked done."""
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin)
    queue = FakeQueue()
    storage = FakeStorage(synthetic_videos["valid"].read_bytes())
    store = FakeClipStore([
        {**clip(CLIP_A, 0.0, 1.0, order=0), "captions": payload([("hallo", 0.2, 0.8)])},
    ])
    renderer = SpecRecordingRenderer(ffmpeg_bin, ffprobe_bin, timeout_seconds=120)
    renderer.raise_error = FFmpegError(ERROR_RENDER_FAILED, "scripted render failure")

    run(make_processor(settings, queue, storage, store, renderer).process_job(make_job()))

    assert len(queue.fail_calls) == 1
    job_id, worker_id, category, message, permanent = queue.fail_calls[0]
    assert (job_id, worker_id, category, permanent) == (JOB_ID, WORKER, ERROR_RENDER_FAILED, False)
    assert queue.complete_calls == []
    assert storage.uploads == []  # nothing uploaded on failure
