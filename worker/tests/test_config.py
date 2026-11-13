"""Tests for worker environment configuration validation."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config.settings import Settings


def test_defaults_are_safe():
    settings = Settings()
    assert settings.environment == "development"
    assert settings.log_level == "INFO"
    assert settings.service_name == "clipforge-worker"
    assert settings.request_timeout_seconds > 0


def test_env_prefix_is_respected(monkeypatch):
    monkeypatch.setenv("WORKER_ENVIRONMENT", "production")
    monkeypatch.setenv("WORKER_LOG_LEVEL", "DEBUG")
    settings = Settings()
    assert settings.environment == "production"
    assert settings.log_level == "DEBUG"


def test_invalid_log_level_is_rejected():
    with pytest.raises(ValidationError):
        Settings(log_level="VERBOSE")  # type: ignore[arg-type]


def test_invalid_environment_is_rejected():
    with pytest.raises(ValidationError):
        Settings(environment="qa")  # type: ignore[arg-type]


def test_non_positive_timeout_is_rejected():
    with pytest.raises(ValidationError):
        Settings(request_timeout_seconds=0)


def test_storage_dirs_derive_from_root():
    settings = Settings(storage_root=Path("/data"))
    assert settings.upload_dir == Path("/data/uploads")
    assert settings.output_dir == Path("/data/outputs")
    # Phase 3: the temp directory is worker scratch space only.
    assert settings.tmp_dir == Path("/data/tmp")


def test_phase3_queue_defaults():
    settings = Settings()
    assert settings.worker_enabled is True
    assert settings.poll_interval == 2.0
    assert settings.concurrency == 2
    assert settings.job_lease_seconds == 300
    assert settings.max_attempts == 3
    assert settings.max_upload_size_bytes == 2 * 1024**3
    assert settings.max_source_duration_seconds == 8 * 3600
    assert settings.ffmpeg_path == "ffmpeg"
    assert settings.ffprobe_path == "ffprobe"
    assert settings.supabase_configured is False


def test_lease_seconds_bounds_match_rpc_rules():
    # The RPC rejects leases outside [30, 3600] — settings must enforce the
    # same bounds at startup.
    with pytest.raises(ValidationError):
        Settings(job_lease_seconds=29)
    with pytest.raises(ValidationError):
        Settings(job_lease_seconds=3601)
    assert Settings(job_lease_seconds=30).job_lease_seconds == 30
    assert Settings(job_lease_seconds=3600).job_lease_seconds == 3600


def test_concurrency_bounds():
    with pytest.raises(ValidationError):
        Settings(concurrency=0)
    with pytest.raises(ValidationError):
        Settings(concurrency=17)
    assert Settings(concurrency=1).concurrency == 1
    assert Settings(concurrency=16).concurrency == 16


def test_poll_interval_bounds():
    with pytest.raises(ValidationError):
        Settings(poll_interval=0)
    with pytest.raises(ValidationError):
        Settings(poll_interval=61)
    assert Settings(poll_interval=60).poll_interval == 60


def test_max_attempts_bounds():
    with pytest.raises(ValidationError):
        Settings(max_attempts=0)
    with pytest.raises(ValidationError):
        Settings(max_attempts=11)


def test_supabase_configured_requires_both_credentials():
    assert Settings(supabase_url="https://x.supabase.co").supabase_configured is False
    assert Settings(supabase_service_role_key="k").supabase_configured is False
    assert (
        Settings(supabase_url="https://x.supabase.co", supabase_service_role_key="k").supabase_configured
        is True
    )


def test_phase3_env_vars_are_picked_up(monkeypatch):
    monkeypatch.setenv("WORKER_CONCURRENCY", "4")
    monkeypatch.setenv("WORKER_POLL_INTERVAL", "5")
    monkeypatch.setenv("WORKER_MAX_ATTEMPTS", "5")
    settings = Settings()
    assert settings.concurrency == 4
    assert settings.poll_interval == 5
    assert settings.max_attempts == 5
