"""FFprobe media validation tests (real binaries, synthetic media).

The browser's extension/MIME is never trusted — these tests probe the actual
bytes: container whitelist, video-stream presence, audio availability,
duration (SOURCE limit, not the 60-180s clip setting), resolution and size.
"""

from __future__ import annotations

import asyncio

import pytest

from app.queue.models import (
    ERROR_CORRUPT_MEDIA,
    ERROR_FFPROBE,
    ERROR_INVALID_FILE,
    ERROR_NO_VIDEO_STREAM,
    ERROR_SOURCE_TOO_LARGE,
    ERROR_SOURCE_TOO_LONG,
    ERROR_STORAGE_DOWNLOAD,
    ERROR_UNSUPPORTED_FORMAT,
    ERROR_UNKNOWN,
    PERMANENT_ERROR_CATEGORIES,
)
from app.validation.media import (
    MediaInfo,
    MediaValidationError,
    probe_media,
    validate_media,
)


def run(coro):
    return asyncio.run(coro)


def probe(ffprobe_bin: str, path) -> MediaInfo:
    return run(probe_media(ffprobe_bin, path, timeout_seconds=30.0))


def test_probe_valid_video(ffprobe_bin, synthetic_videos):
    info = probe(ffprobe_bin, synthetic_videos["valid"])
    assert info.has_video is True
    assert info.has_audio is True
    assert info.video_codec == "h264"
    assert info.audio_codec == "aac"
    assert info.width == 320
    assert info.height == 240
    assert 1.8 <= info.duration_seconds <= 2.4
    assert info.file_size_bytes > 0
    assert "mp4" in info.container


def test_probe_video_without_audio_is_accepted(ffprobe_bin, synthetic_videos):
    # Audio is checked and reported, but its absence must not reject a source.
    info = probe(ffprobe_bin, synthetic_videos["video_only"])
    assert info.has_video is True
    assert info.has_audio is False
    assert info.audio_codec is None


def test_probe_audio_only_file_raises_no_video_stream(ffprobe_bin, synthetic_videos):
    with pytest.raises(MediaValidationError) as exc:
        probe(ffprobe_bin, synthetic_videos["audio_only"])
    assert exc.value.category == ERROR_NO_VIDEO_STREAM
    assert exc.value.category in PERMANENT_ERROR_CATEGORIES


def test_probe_corrupt_file_raises_corrupt_media(ffprobe_bin, synthetic_videos):
    with pytest.raises(MediaValidationError) as exc:
        probe(ffprobe_bin, synthetic_videos["corrupt"])
    assert exc.value.category == ERROR_CORRUPT_MEDIA


def test_probe_unsupported_container_raises(ffprobe_bin, synthetic_videos):
    with pytest.raises(MediaValidationError) as exc:
        probe(ffprobe_bin, synthetic_videos["unsupported"])
    assert exc.value.category == ERROR_UNSUPPORTED_FORMAT


def test_probe_empty_file_raises_invalid_file(ffprobe_bin, synthetic_videos):
    with pytest.raises(MediaValidationError) as exc:
        probe(ffprobe_bin, synthetic_videos["empty"])
    assert exc.value.category == ERROR_INVALID_FILE


def test_probe_missing_file_raises_invalid_file(ffprobe_bin, tmp_path):
    with pytest.raises(MediaValidationError) as exc:
        probe(ffprobe_bin, tmp_path / "does-not-exist.mp4")
    assert exc.value.category == ERROR_INVALID_FILE


def test_validate_media_returns_safe_metadata(ffprobe_bin, synthetic_videos):
    info = probe(ffprobe_bin, synthetic_videos["valid"])
    metadata = validate_media(info, max_source_duration_seconds=8 * 3600, max_upload_size_bytes=2 * 1024**3)
    assert set(metadata) == {
        "container",
        "duration_seconds",
        "width",
        "height",
        "video_codec",
        "has_audio",
        "audio_codec",
        "file_size_bytes",
    }
    assert metadata["width"] == 320
    assert metadata["height"] == 240


def _info(**overrides) -> MediaInfo:
    base = dict(
        container="mov,mp4,m4a,3gp,3g2,mj2",
        duration_seconds=120.0,
        width=1920,
        height=1080,
        video_codec="h264",
        has_video=True,
        has_audio=True,
        audio_codec="aac",
        file_size_bytes=1_000_000,
    )
    base.update(overrides)
    return MediaInfo(**base)


def test_validate_media_enforces_source_duration_limit():
    # 120s source vs a 60s infrastructure limit → rejected. The 60-180s clip
    # setting is deliberately NOT involved here.
    with pytest.raises(MediaValidationError) as exc:
        validate_media(_info(), max_source_duration_seconds=60, max_upload_size_bytes=2 * 1024**3)
    assert exc.value.category == ERROR_SOURCE_TOO_LONG


def test_validate_media_enforces_file_size_limit():
    with pytest.raises(MediaValidationError) as exc:
        validate_media(
            _info(file_size_bytes=10),
            max_source_duration_seconds=8 * 3600,
            max_upload_size_bytes=5,
        )
    assert exc.value.category == ERROR_SOURCE_TOO_LARGE


def test_error_category_split_permanent_vs_transient():
    # Unrecoverable input errors must never be retried...
    assert PERMANENT_ERROR_CATEGORIES == {
        ERROR_INVALID_FILE,
        ERROR_CORRUPT_MEDIA,
        ERROR_UNSUPPORTED_FORMAT,
        ERROR_NO_VIDEO_STREAM,
        ERROR_SOURCE_TOO_LARGE,
        ERROR_SOURCE_TOO_LONG,
    }
    # ...while infrastructure errors are retryable.
    for transient in (ERROR_STORAGE_DOWNLOAD, ERROR_FFPROBE, ERROR_UNKNOWN):
        assert transient not in PERMANENT_ERROR_CATEGORIES
