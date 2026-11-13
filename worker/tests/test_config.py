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
