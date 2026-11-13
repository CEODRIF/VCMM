"""Provider adapter tests (TASK 007, adapted by TASK 011) — deterministic,
NO real network.

httpx.MockTransport drives the YouTube (oEmbed) and Twitch (Helix) adapters
through every documented outcome: available, deleted/private, restricted
(live pages), rate-limited, HTTP errors, malformed metadata, timeouts and
network failures. The upload adapter is tested with a fake download
callable (no real downloads anywhere). The materialization honesty gate is
asserted for every validation-only adapter.

TASK 011 additions (adaptations, not weakenings):
- every provider under test gets an OFFLINE resolver stub, so the SSRF
  guard's DNS revalidation never touches the real network;
- Twitch tests carry a minimal ``TwitchConfig(client_id=...)`` — the
  official minimum for public VOD metadata (Client-Id header mode);
- Twitch live/channel URLs now query the official ``/streams`` endpoint
  (online → restricted "wait for the VOD", offline → unavailable);
- a new unconfigured-Twitch test asserts the honest "not configured"
  status with NO network call.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from app.providers.base import (
    STATUS_AVAILABLE,
    STATUS_DELETED,
    STATUS_RATE_LIMITED,
    STATUS_RESTRICTED,
    STATUS_UNAVAILABLE,
    MaterializationUnavailable,
    ProviderNotSupported,
)
from app.providers.registry import default_providers, provider_for_url
from app.providers.twitch import TwitchConfig, TwitchProvider
from app.providers.upload import UploadProvider
from app.providers.youtube import YouTubeProvider
from app.queue.models import ERROR_STORAGE_DOWNLOAD
from app.validation.media import MediaValidationError


def run(coro):
    return asyncio.run(coro)


def client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _public_resolver(hostname: str):
    """Deterministic offline DNS: every name resolves to one public IP."""
    return ["93.184.216.34"]


#: The official minimum Twitch credential (client id → Client-Id header).
_TEST_TWITCH_CONFIG = TwitchConfig(client_id="test-client-id")


# =============================================================================
# YouTube
# =============================================================================

def test_youtube_can_handle_shapes():
    p = YouTubeProvider()
    assert p.can_handle("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert p.can_handle("https://youtu.be/dQw4w9WgXcQ")
    assert p.can_handle("https://youtube.com/shorts/abc123XYZ")
    assert p.can_handle("https://m.youtube.com/watch?v=abc123XYZ")
    assert p.can_handle("https://music.youtube.com/watch?v=abc123XYZ")
    assert p.can_handle("https://www.youtube.com/embed/abc123XYZ")
    # NOT youtube:
    assert not p.can_handle("https://twitch.tv/videos/123")
    assert not p.can_handle("ftp://youtube.com/watch?v=abc123XYZ")
    assert not p.can_handle("https://notyoutube.com/watch?v=abc123XYZ")
    assert not p.can_handle("https://evil-youtube.com.evil.io/x")  # suffix trick


def test_youtube_video_id_extraction():
    p = YouTubeProvider()
    assert p._video_id("https://www.youtube.com/watch?v=abc123XYZ&t=10") == "abc123XYZ"
    assert p._video_id("https://youtu.be/abc123XYZ") == "abc123XYZ"
    assert p._video_id("https://www.youtube.com/shorts/abc123XYZ") == "abc123XYZ"
    assert p._video_id("https://www.youtube.com/live/abc123XYZ") == "abc123XYZ"
    assert p._video_id("https://www.youtube.com/embed/abc123XYZ") == "abc123XYZ"
    # channel pages / bare paths are NOT claimed
    assert p._video_id("https://www.youtube.com/@somechannel") is None
    assert p._video_id("https://www.youtube.com/") is None


def test_youtube_available():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"title": "My VOD", "type": "video"})

    p = YouTubeProvider(http_client=client_with(handler), resolver=_public_resolver)
    check = run(p.validate("https://youtu.be/abc123XYZ"))
    assert check.status == STATUS_AVAILABLE
    assert check.title == "My VOD"
    assert check.reason is None
    # TASK 011: keyless oEmbed carries partial, HONEST metadata
    assert check.metadata is not None
    assert check.metadata.title == "My VOD"
    assert check.metadata.duration_seconds is None  # unknown stays unknown
    assert check.metadata.state in ("unknown", "live")


def test_youtube_live_page_is_restricted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"title": "Live", "type": "live"})

    p = YouTubeProvider(http_client=client_with(handler), resolver=_public_resolver)
    check = run(p.validate("https://www.youtube.com/watch?v=abc123XYZ"))
    assert check.status == STATUS_RESTRICTED
    assert check.reason
    assert check.metadata is not None and check.metadata.state == "live"


def test_youtube_deleted_private_404():
    p = YouTubeProvider(http_client=client_with(lambda r: httpx.Response(404)),
                        resolver=_public_resolver)
    check = run(p.validate("https://youtu.be/abc123XYZ"))
    assert check.status == STATUS_DELETED
    assert check.reason


def test_youtube_rate_limited_429():
    p = YouTubeProvider(http_client=client_with(lambda r: httpx.Response(429)),
                        resolver=_public_resolver)
    assert run(p.validate("https://youtu.be/abc123XYZ")).status == STATUS_RATE_LIMITED


def test_youtube_server_error_unavailable():
    p = YouTubeProvider(http_client=client_with(lambda r: httpx.Response(500)),
                        resolver=_public_resolver)
    check = run(p.validate("https://youtu.be/abc123XYZ"))
    assert check.status == STATUS_UNAVAILABLE
    assert check.reason


def test_youtube_malformed_metadata():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"this is not json")

    p = YouTubeProvider(http_client=client_with(handler), resolver=_public_resolver)
    assert run(p.validate("https://youtu.be/abc123XYZ")).status == STATUS_UNAVAILABLE


def test_youtube_network_error_and_timeout():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    for handler in (boom, slow):
        p = YouTubeProvider(http_client=client_with(handler), resolver=_public_resolver)
        check = run(p.validate("https://youtu.be/abc123XYZ"))
        assert check.status == STATUS_UNAVAILABLE
        # safe message — no internals leaked
        assert "no route" not in (check.reason or "")


def test_youtube_invalid_video_id_never_calls_network():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"title": "x", "type": "video"})

    p = YouTubeProvider(http_client=client_with(handler), resolver=_public_resolver)
    # too-short id → rejected before any request
    check = run(p.validate("https://www.youtube.com/watch?v=ab"))
    assert check.status == STATUS_UNAVAILABLE
    assert calls == []
    # channel page → no id → rejected before any request
    check2 = run(p.validate("https://www.youtube.com/@somechannel"))
    assert check2.status == STATUS_UNAVAILABLE
    assert calls == []


def test_youtube_materialization_refused():
    p = YouTubeProvider()
    with pytest.raises(MaterializationUnavailable):
        run(p.materialize("https://youtu.be/abc123XYZ", Path("/tmp/x")))
    assert p.supports_materialization is False


# =============================================================================
# Twitch
# =============================================================================

def test_twitch_can_handle_shapes():
    p = TwitchProvider()
    assert p.can_handle("https://twitch.tv/videos/123456789")
    assert p.can_handle("https://www.twitch.tv/somechannel/videos/123456789")
    assert p.can_handle("https://m.twitch.tv/videos/123")
    assert not p.can_handle("https://youtube.com/watch?v=x")
    assert not p.can_handle("http://not-twitch.tv/videos/1")


def _helix(vod_id: str | None) -> httpx.Response:
    data = [{"id": vod_id, "title": "Twitch VOD", "duration": 3600}] if vod_id else []
    return httpx.Response(200, json={"data": data})


def test_twitch_vod_available():
    p = TwitchProvider(http_client=client_with(lambda r: _helix("123456789")),
                       config=_TEST_TWITCH_CONFIG, resolver=_public_resolver)
    check = run(p.validate("https://twitch.tv/videos/123456789"))
    assert check.status == STATUS_AVAILABLE
    assert check.title == "Twitch VOD"
    # TASK 011: the official Helix check carries typed metadata
    assert check.metadata is not None
    assert check.metadata.provider_id == "123456789"
    assert check.metadata.duration_seconds == 3600.0
    assert check.metadata.state == "completed"


def test_twitch_channel_style_vod_available():
    p = TwitchProvider(http_client=client_with(lambda r: _helix("42")),
                       config=_TEST_TWITCH_CONFIG, resolver=_public_resolver)
    check = run(p.validate("https://twitch.tv/somechannel/videos/42"))
    assert check.status == STATUS_AVAILABLE


def test_twitch_deleted_when_absent_from_listing():
    p = TwitchProvider(http_client=client_with(lambda r: httpx.Response(200, json={"data": []})),
                       config=_TEST_TWITCH_CONFIG, resolver=_public_resolver)
    check = run(p.validate("https://twitch.tv/videos/123456789"))
    assert check.status == STATUS_DELETED
    assert check.reason


def test_twitch_private_403_and_401():
    for code in (401, 403):
        p = TwitchProvider(http_client=client_with(lambda r, c=code: httpx.Response(c)),
                           config=_TEST_TWITCH_CONFIG, resolver=_public_resolver)
        check = run(p.validate("https://twitch.tv/videos/123456789"))
        assert check.status == STATUS_RESTRICTED


def test_twitch_rate_limited_429():
    p = TwitchProvider(http_client=client_with(lambda r: httpx.Response(429)),
                       config=_TEST_TWITCH_CONFIG, resolver=_public_resolver)
    assert run(p.validate("https://twitch.tv/videos/123")).status == STATUS_RATE_LIMITED


def test_twitch_server_error_unavailable():
    p = TwitchProvider(http_client=client_with(lambda r: httpx.Response(503)),
                       config=_TEST_TWITCH_CONFIG, resolver=_public_resolver)
    check = run(p.validate("https://twitch.tv/videos/123"))
    assert check.status == STATUS_UNAVAILABLE


def test_twitch_malformed_metadata():
    p = TwitchProvider(http_client=client_with(lambda r: httpx.Response(200, content=b"oops")),
                       config=_TEST_TWITCH_CONFIG, resolver=_public_resolver)
    assert run(p.validate("https://twitch.tv/videos/123")).status == STATUS_UNAVAILABLE


def test_twitch_network_error():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    p = TwitchProvider(http_client=client_with(boom),
                       config=_TEST_TWITCH_CONFIG, resolver=_public_resolver)
    check = run(p.validate("https://twitch.tv/videos/123"))
    assert check.status == STATUS_UNAVAILABLE
    assert "timed out" not in (check.reason or "")


def test_twitch_live_url_online_is_restricted():
    """TASK 011: live/channel URLs query the official /streams endpoint.
    Channel ONLINE → restricted (a live stream is not a downloadable VOD)."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        # /streams?user_login=somechannel → the channel is live
        return httpx.Response(200, json={"data": [{"id": 1, "user_name": "somechannel"}]})

    p = TwitchProvider(http_client=client_with(handler),
                       config=_TEST_TWITCH_CONFIG, resolver=_public_resolver)
    for url in ("https://twitch.tv/live/somechannel", "https://twitch.tv/somechannel"):
        check = run(p.validate(url))
        assert check.status == STATUS_RESTRICTED
        assert "live" in (check.reason or "").lower() or "vod" in (check.reason or "").lower()
    assert len(calls) == 2  # one official /streams call per URL
    assert all("/streams" in str(c.url) for c in calls)


def test_twitch_live_url_offline_is_unavailable():
    """Channel OFFLINE → the URL has no VOD → unavailable (not restricted)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    p = TwitchProvider(http_client=client_with(handler),
                       config=_TEST_TWITCH_CONFIG, resolver=_public_resolver)
    check = run(p.validate("https://twitch.tv/somechannel"))
    assert check.status == STATUS_UNAVAILABLE
    assert check.reason


def test_twitch_non_vod_paths_unavailable():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _helix("1")

    p = TwitchProvider(http_client=client_with(handler),
                       config=_TEST_TWITCH_CONFIG, resolver=_public_resolver)
    # a multi-segment path that is neither a VOD nor a channel page
    check = run(p.validate("https://twitch.tv/directory/browse"))
    assert check.status == STATUS_UNAVAILABLE
    assert calls == []


def test_twitch_unconfigured_reports_honestly_without_network():
    """TASK 011: an unconfigured deployment does NOT guess at Helix — it
    reports unavailability and makes zero network calls."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _helix("1")

    p = TwitchProvider(http_client=client_with(handler), resolver=_public_resolver)
    assert p.is_configured is False
    check = run(p.validate("https://twitch.tv/videos/123456789"))
    assert check.status == STATUS_UNAVAILABLE
    assert "not configured" in (check.reason or "").lower()
    assert calls == []


def test_twitch_materialization_refused():
    p = TwitchProvider()
    with pytest.raises(MaterializationUnavailable):
        run(p.materialize("https://twitch.tv/videos/1", Path("/tmp/x")))
    assert p.supports_materialization is False


# =============================================================================
# Upload (authorized local files only — fake download, no network)
# =============================================================================

def test_upload_can_handle_object_paths_only():
    p = UploadProvider(ffprobe_path="ffprobe")
    assert p.can_handle("sources/11111111-1111-4111-8111-111111111111/original/v.mp4")
    assert not p.can_handle("https://example.com/v.mp4")
    assert not p.can_handle("")
    assert not p.can_handle("/etc/passwd")


def test_upload_validate_is_local_only():
    p = UploadProvider(ffprobe_path="ffprobe")
    check = run(p.validate("sources/j/v.mp4"))
    assert check.status == STATUS_AVAILABLE


def test_upload_materialize_downloads_and_probes(ffprobe_bin, render_media, tmp_path):
    calls = []

    async def fake_download(object_path: str, destination: Path) -> None:
        calls.append(object_path)
        destination.write_bytes(render_media["landscape"].read_bytes())

    p = UploadProvider(ffprobe_path=ffprobe_bin)
    out = run(p.materialize("sources/j/v.mp4", tmp_path, download=fake_download))
    assert calls == ["sources/j/v.mp4"]
    assert out.is_file() and out.stat().st_size > 0
    assert p.supports_materialization is True


def test_upload_materialize_missing_file_raises_safe_error(tmp_path):
    async def missing_download(object_path, destination):
        pass  # the "download" produced nothing

    p = UploadProvider(ffprobe_path="ffprobe")
    with pytest.raises(MediaValidationError) as exc:
        run(p.materialize("sources/j/v.mp4", tmp_path, download=missing_download))
    assert exc.value.category == ERROR_STORAGE_DOWNLOAD
    assert not (tmp_path / "source.bin").exists()  # cleaned up


def test_upload_materialize_corrupt_file_raises_invalid_file(ffprobe_bin, tmp_path):
    async def corrupt_download(object_path, destination):
        destination.write_bytes(b"definitely not an mp4" * 32)

    p = UploadProvider(ffprobe_path=ffprobe_bin)
    with pytest.raises(MediaValidationError) as exc:
        run(p.materialize("sources/j/v.mp4", tmp_path, download=corrupt_download))
    assert exc.value.category in ("invalid_file", "corrupt_media")


def test_upload_materialize_requires_download_callable(tmp_path):
    p = UploadProvider(ffprobe_path="ffprobe")
    with pytest.raises(ValueError):
        run(p.materialize("sources/j/v.mp4", tmp_path))


# =============================================================================
# Registry: dispatch without guessing
# =============================================================================

def test_registry_dispatches_to_claiming_adapter(ffprobe_bin):
    providers = default_providers(ffprobe_bin)
    assert isinstance(provider_for_url("https://youtu.be/abc123XYZ", providers), YouTubeProvider)
    assert isinstance(provider_for_url("https://twitch.tv/videos/123", providers), TwitchProvider)
    assert isinstance(
        provider_for_url("sources/j/v.mp4", providers), UploadProvider)


def test_registry_rejects_unknown_urls():
    providers = default_providers("ffprobe")
    for url in ("https://example.com/watch?v=x", "", None, "notaurl",
                "https://kick.com/channel/x/videos/1"):
        with pytest.raises(ProviderNotSupported):
            provider_for_url(url, providers)  # type: ignore[arg-type]


def test_default_provider_capabilities_are_honest(ffprobe_bin):
    providers = default_providers(ffprobe_bin)
    by_name = {p.name: p for p in providers}
    assert by_name["upload"].supports_validation and by_name["upload"].supports_materialization
    assert by_name["youtube"].supports_validation and not by_name["youtube"].supports_materialization
    assert by_name["twitch"].supports_validation and not by_name["twitch"].supports_materialization


def test_no_ytdlp_or_unauthorized_download_tooling():
    # The adapters must never pull in unauthorized download tooling.
    import app.providers as providers_pkg

    base_dir = Path(providers_pkg.__path__[0])
    for module_name in ("youtube", "twitch", "upload", "registry", "base"):
        source = (base_dir / f"{module_name}.py").read_text()
        assert "yt_dlp" not in source
        assert "ytdlp" not in source.lower()
        assert "requests.get" not in source  # only httpx, with injected clients in tests
