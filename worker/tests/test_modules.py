"""Import and contract tests for the Phase 1 worker scaffolding."""

import importlib
from pathlib import Path
from uuid import uuid4

WORKER_MODULES = [
    "app.main",
    "app.api.health",
    "app.config.settings",
    "app.config.logging",
    "app.queue.models",
    "app.processors.pipeline",
    "app.transcription.base",
    "app.ai.clip_detection",
    "app.rendering.base",
    "app.storage.paths",
]


def test_all_phase1_modules_import():
    for name in WORKER_MODULES:
        module = importlib.import_module(name)
        assert module is not None


def test_pipeline_stage_order():
    from app.processors.pipeline import stage_order

    assert stage_order() == ["source", "transcription", "analysis", "rendering"]


def test_job_status_matches_frontend_states():
    from app.queue.models import JobStatus

    # Must stay in sync with src/config/app.ts -> processingStates.
    assert [status.value for status in JobStatus] == [
        "queued",
        "uploading",
        "transcribing",
        "analyzing",
        "rendering",
        "completed",
        "failed",
    ]


def test_render_spec_is_fixed_vertical_1080p():
    from app.rendering.base import RenderSpec

    spec = RenderSpec()
    assert spec.width == 1080
    assert spec.height == 1920
    assert spec.aspect_ratio == "9:16"


def test_upload_path_strips_traversal():
    from app.storage.paths import upload_path

    job_id = uuid4()
    result = upload_path(Path("/data"), job_id, "../../etc/passwd")
    assert result.name == "passwd"
    assert "/data/jobs" in str(result)
