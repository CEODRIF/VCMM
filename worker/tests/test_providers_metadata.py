"""TASK 011 metadata tests: official API paths, state classification,
OAuth token handling, and the normalized metadata contract
(spec sections 4-7, 13).

Deterministic and fully offline: httpx.MockTransport + injected resolvers.
NO live credentials anywhere (values are test-only sentinels).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from app.providers.base import (
    STATUS_AVAILABLE,
    STATUS_DELETED,
    STATUS_RATE_LIMITED,
    STATUS_RESTRICTED,
    STATUS_UNAVAILABLE,
    MaterializationUnavailable,
)
from app.providers.errors import (
    CODE_MATERIALIZATION_UNSUPPORTED,
    CODE_METADATA_UNAVAILABLE,
    CODE_RATE_LIMITED,
    CODE_SOURCE_EXPIRED,
    CODE_SOURCE_RESTRICTED,
    CODE_UNAUTHORIZED,
    MaterializationUnsupportedError,
    MetadataUnavailableError,
    RateLimitedError,
    SourceExpiredError,
    SourceRestrictedError,
    UnauthorizedError,
    error_for_status,
)
from app.providers.models import NormalizedSource
from app.providers.twitch import TwitchConfig, TwitchProvider
from app.providers.youtube import YouTubeConfig, YouTubeProvider


def run(coro):
    return asyncio.run(coro)


def _resolver(*answers):
    return lambda hostname: answers


PUBLIC = "93.184.216.34"


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# =============================================================================
# NormalizedSource contract (spec section 4)
# =============================================================================

def test_normalized_source_unknown_fields_stay_unknown():
    src = NormalizedSource(provider="youtube", canonical_url="https://www.youtube.com/watch?v=abc")
    assert src.title is None
    assert src.duration_seconds is None
    assert src.published_at is None
    assert src.state == "unknown"
    assert src.width is None and src.fps is None


def test_normalized_source_strict_types():
    with pytest.raises(ValueError):
        NormalizedSource(provider="", canonical_url="x")
    with pytest.raises(ValueError):
        NormalizedSource(provider="p", canonical_url="")
    with pytest.raises(ValueError):
        NormalizedSource(provider="p", canonical_url="x", state="bogus")
    with pytest.raises(ValueError):
        NormalizedSource(provider="p", canonical_url="x", duration_seconds=-1)
    with pytest.raises(ValueError):
        NormalizedSource(provider="p", canonical_url="x", duration_seconds=float("nan"))
    with pytest.raises(ValueError):
        NormalizedSource(provider="p", canonical_url="x", width=0)
    with pytest.raises(ValueError):
        NormalizedSource(provider="p", canonical_url="x", fps=0)
    with pytest.raises(ValueError):
        NormalizedSource(provider="p", canonical_url="x", fps=True)


def test_normalized_source_rejects_secret_shaped_keys():
    for bad_key in ("token", "access_token", "client_secret", "password",
                    "cookie", "signature", "signed_url", "upload_url",
                    "authorization", "api_key"):
        with pytest.raises(ValueError):
            NormalizedSource(provider="p", canonical_url="x",
                             provider_specific={bad_key: "value"})


def test_normalized_source_bounded_specific():
    with pytest.raises(ValueError):
        NormalizedSource(provider="p", canonical_url="x",
                         provider_specific={f"n{i}": i for i in range(13)})
    with pytest.raises(ValueError):
        NormalizedSource(provider="p", canonical_url="x",
                         provider_specific={"long": "x" * 201})
    with pytest.raises(ValueError):
        NormalizedSource(provider="p", canonical_url="x",
                         provider_specific={"nested": {"a": 1}})


def test_normalized_source_public_dict_has_no_urls():
    src = NormalizedSource(
        provider="youtube",
        canonical_url="https://www.youtube.com/watch?v=abc",
        provider_id="abc",
        title="T",
        duration_seconds=600.0,
        published_at="2026-01-01T00:00:00Z",
        creator_name="C",
        thumbnail_url="https://i.ytimg.com/vi/abc/dqdefault.jpg",
        state="completed",
    )
    public = src.to_public_dict()
    assert public == {
        "provider": "youtube", "providerId": "abc", "title": "T",
        "durationSeconds": 600.0, "publishedAt": "2026-01-01T00:00:00Z",
        "creatorName": "C", "state": "completed",
    }
    assert "canonical_url" not in public and "thumbnail_url" not in public
    # the full dict keeps URLs (for the worker's own records)
    assert src.to_dict()["canonical_url"] == "https://www.youtube.com/watch?v=abc"


# =============================================================================
# YouTube — Data API v3 (api-key mode)
# =============================================================================

def _item(duration: str | None = "PT2H30M12S", upload: str = "processed",
          privacy: str = "public", live_state: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": "abc123XYZ",
        "snippet": {
            "title": "My Recording",
            "channelTitle": "The Creator",
            "publishedAt": "2026-11-01T10:00:00Z",
            "privacyStatus": privacy,
            "thumbnails": {"medium": {"url": "https://i.ytimg.com/vi/abc123XYZ/mqdefault.jpg"}},
        },
        "contentDetails": {"duration": duration, "definition": "hd"},
        "status": {"uploadStatus": upload, "privacyStatus": privacy},
    }
    if live_state is not None:
        item["liveStreamingDetails"] = {"actualState": live_state}
    return item


def _yt_key_provider(handler, key: str = "yt-key-sentinel-000") -> YouTubeProvider:
    return YouTubeProvider(
        http_client=_client(handler),
        config=YouTubeConfig(api_key=key),
        resolver=_resolver(PUBLIC),
    )


def test_yt_data_api_completed_maps_fully():
    def handler(request: httpx.Request) -> httpx.Response:
        # the key must arrive in the OFFICIAL HEADER, never in the URL
        assert request.headers.get("x-goog-api-key") == "yt-key-sentinel-000"
        assert "key=" not in str(request.url)
        assert "/youtube/v3/videos" in str(request.url)
        return httpx.Response(200, json={"items": [_item()]})

    p = _yt_key_provider(handler)
    check = run(p.validate("https://www.youtube.com/watch?v=abc123XYZ"))
    assert check.status == STATUS_AVAILABLE
    meta = check.metadata
    assert meta.duration_seconds == 9012.0  # PT2H30M12S
    assert meta.title == "My Recording"
    assert meta.creator_name == "The Creator"
    assert meta.published_at == "2026-11-01T10:00:00Z"
    assert meta.thumbnail_url == "https://i.ytimg.com/vi/abc123XYZ/mqdefault.jpg"
    assert meta.quality == "hd"
    assert meta.state == "completed"
    assert check.duration_seconds == 9012.0


@pytest.mark.parametrize(
    "item,status,state",
    [
        (_item(live_state="livestreamLive"), STATUS_RESTRICTED, "live"),
        (_item(live_state="livestreamUpcoming"), STATUS_RESTRICTED, "scheduled"),
        (_item(privacy="private"), STATUS_RESTRICTED, "restricted"),
        (_item(upload="rejected"), STATUS_RESTRICTED, "restricted"),
        (_item(upload="processing"), STATUS_UNAVAILABLE, "unavailable"),
        (_item(upload="failed"), STATUS_UNAVAILABLE, "unavailable"),
    ],
)
def test_yt_data_api_state_classification(item, status, state):
    p = _yt_key_provider(lambda r: httpx.Response(200, json={"items": [item]}))
    check = run(p.validate("https://youtu.be/abc123XYZ"))
    assert check.status == status
    assert check.metadata.state == state
    assert check.reason  # a non-available state always carries an explanation


def test_yt_data_api_empty_items_is_deleted():
    p = _yt_key_provider(lambda r: httpx.Response(200, json={"items": []}))
    check = run(p.validate("https://youtu.be/abc123XYZ"))
    assert check.status == STATUS_DELETED
    assert check.reason


def test_yt_data_api_http_mapping():
    for code, status in ((403, STATUS_RESTRICTED), (404, STATUS_DELETED),
                         (429, STATUS_RATE_LIMITED), (500, STATUS_UNAVAILABLE)):
        p = _yt_key_provider(lambda r, c=code: httpx.Response(c))
        assert run(p.validate("https://youtu.be/abc123XYZ")).status == status


def test_yt_data_api_malformed_json():
    p = _yt_key_provider(lambda r: httpx.Response(200, content=b"not json"))
    assert run(p.validate("https://youtu.be/abc123XYZ")).status == STATUS_UNAVAILABLE


def test_yt_duration_parser_unknown_shapes_stay_none():
    for value in (None, "", "garbage", "P", "PT0S", 123, "PT2H30M12S ", "P1DT2H"):
        item = _item(duration=value if isinstance(value, str) or value is None else None)
        p = _yt_key_provider(lambda r, it=item: httpx.Response(200, json={"items": [it]}))
        meta = run(p.validate("https://youtu.be/abc123XYZ")).metadata
        expected = {"P1DT2H": 93600.0, "PT2H30M12S ": 9012.0}.get(value)
        assert meta.duration_seconds == expected


# --- YouTube OAuth (refresh-token) mode ---------------------------------------

_OAUTH_CONFIG = YouTubeConfig(
    oauth_client_id="yt-client-id-sentinel",
    oauth_client_secret="yt-client-secret-sentinel-000000",
    oauth_refresh_token="yt-refresh-token-sentinel-000000",
)


def _yt_oauth_provider(handler) -> YouTubeProvider:
    return YouTubeProvider(http_client=_client(handler), config=_OAUTH_CONFIG,
                           resolver=_resolver(PUBLIC))


def test_yt_oauth_token_flow_and_caching():
    calls = {"token": 0, "data": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/token"):
            calls["token"] += 1
            # the secret travels ONLY in the POST form body — never the URL
            assert "client_secret" not in str(request.url)
            return httpx.Response(200, json={"access_token": "yt-access-sentinel",
                                             "expires_in": 3600})
        calls["data"] += 1
        assert request.headers.get("authorization") == "Bearer yt-access-sentinel"
        return httpx.Response(200, json={"items": [_item()]})

    p = _yt_oauth_provider(handler)
    run(p.validate("https://youtu.be/abc123XYZ"))
    run(p.validate("https://youtu.be/abc123XYZ"))
    assert calls["token"] == 1  # cached across validates
    assert calls["data"] == 2


def test_yt_oauth_token_expiry_triggers_refresh():
    calls = {"token": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/token"):
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": f"tok{calls['token']}",
                                             "expires_in": 3600})
        return httpx.Response(200, json={"items": [_item()]})

    p = _yt_oauth_provider(handler)
    run(p.validate("https://youtu.be/abc123XYZ"))
    assert calls["token"] == 1
    # Force expiry (deterministic — no sleeps): the cache is now stale.
    p._token_cache._expires_at = 0.0
    run(p.validate("https://youtu.be/abc123XYZ"))
    assert calls["token"] == 2


def test_yt_oauth_invalid_grant_raises_typed_unauthorized():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    p = _yt_oauth_provider(handler)
    with pytest.raises(UnauthorizedError) as exc:
        run(p.validate("https://youtu.be/abc123XYZ"))
    assert exc.value.code == CODE_UNAUTHORIZED
    # credentials must NOT leak into the message
    assert "yt-refresh-token-sentinel-000000" not in exc.value.safe_message
    assert "yt-client-secret-sentinel-000000" not in exc.value.safe_message


def test_yt_oauth_token_endpoint_malformed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"oops")

    p = _yt_oauth_provider(handler)
    with pytest.raises(UnauthorizedError):
        run(p.validate("https://youtu.be/abc123XYZ"))


def test_yt_oauth_partial_trio_is_not_oauth():
    # Two of three OAuth values = NOT configured (the settings validator
    # enforces this too; the config itself stays honest).
    cfg = YouTubeConfig(oauth_client_id="x", oauth_client_secret="y")
    assert not cfg.has_oauth
    assert not cfg.data_api_configured


# =============================================================================
# Twitch — Helix (client-id + app-token modes)
# =============================================================================

def _twitch_provider(handler, *, client_secret: str | None = None,
                     api_key_mode: bool = True) -> TwitchProvider:
    config = TwitchConfig(client_id="tw-client-id-sentinel",
                          client_secret=client_secret)
    return TwitchProvider(http_client=_client(handler), config=config,
                          resolver=_resolver(PUBLIC))


def test_twitch_client_id_mode_no_token_exchange():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.headers.get("client-id") == "tw-client-id-sentinel"
        assert request.headers.get("authorization") is None
        return httpx.Response(200, json={"data": [
            {"id": "123", "title": "V", "duration": "90.5", "type": "archive",
             "created_at": "2026-10-01T00:00:00Z", "user_name": "chan",
             "thumbnail_url": "https://static-cdn.jtvnw.net/thumb.jpg"},
        ]})

    p = _twitch_provider(handler, client_secret=None)
    check = run(p.validate("https://twitch.tv/videos/123"))
    assert check.status == STATUS_AVAILABLE
    meta = check.metadata
    assert meta.duration_seconds == 90.5
    assert meta.state == "completed"
    assert meta.provider_specific.get("vod_type") == "archive"
    assert all("/helix/videos" in u for u in seen)
    assert not any("id.twitch.tv" in u for u in seen)  # NO token exchange


def test_twitch_app_token_mode_exchanges_and_uses_bearer():
    calls = {"token": 0, "helix": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/oauth2/token"):
            calls["token"] += 1
            assert "client_secret" not in str(request.url)
            return httpx.Response(200, json={"access_token": "tw-app-sentinel",
                                             "expires_in": 86400})
        calls["helix"] += 1
        assert request.headers.get("authorization") == "Bearer tw-app-sentinel"
        assert request.headers.get("client-id") == "tw-client-id-sentinel"
        return httpx.Response(200, json={"data": [
            {"id": "999", "title": "H", "duration": 120, "type": "highlight"}]})

    p = _twitch_provider(handler, client_secret="tw-secret-sentinel-000000")
    check = run(p.validate("https://twitch.tv/videos/999"))
    assert check.status == STATUS_AVAILABLE
    assert calls["token"] == 1
    assert check.metadata.provider_specific.get("vod_type") == "highlight"


def test_twitch_app_token_failure_raises_typed_unauthorized():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    p = _twitch_provider(handler, client_secret="tw-secret-sentinel-000000")
    with pytest.raises(UnauthorizedError) as exc:
        run(p.validate("https://twitch.tv/videos/999"))
    assert exc.value.code == CODE_UNAUTHORIZED
    assert "tw-secret-sentinel-000000" not in exc.value.safe_message


def test_twitch_vod_id_mismatch_is_deleted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "OTHER", "title": "x"}]})

    p = _twitch_provider(handler)
    check = run(p.validate("https://twitch.tv/videos/123"))
    assert check.status == STATUS_DELETED


def test_twitch_live_online_vs_offline():
    def online(request: httpx.Request) -> httpx.Response:
        assert "/helix/streams" in str(request.url)
        assert request.url.params.get("user_login") == "somechannel"
        return httpx.Response(200, json={"data": [{"user_name": "somechannel"}]})

    p = _twitch_provider(online)
    check = run(p.validate("https://twitch.tv/live/somechannel"))
    assert check.status == STATUS_RESTRICTED
    assert check.metadata.state == "live"

    p_offline = _twitch_provider(lambda r: httpx.Response(200, json={"data": []}))
    check2 = run(p_offline.validate("https://twitch.tv/somechannel"))
    assert check2.status == STATUS_UNAVAILABLE


def test_twitch_duration_unknown_shapes_stay_none():
    for duration in ("garbage", None, "", "0", -5, True):
        def handler(r: httpx.Request, d=duration) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"id": "1", "duration": d}]})

        p = _twitch_provider(handler)
        meta = run(p.validate("https://twitch.tv/videos/1")).metadata
        assert meta is not None
        assert meta.duration_seconds is None


# =============================================================================
# Status → typed error mapping (spec section 13)
# =============================================================================

def test_status_to_error_mapping():
    cases = {
        "private": (SourceRestrictedError, CODE_SOURCE_RESTRICTED, False),
        "deleted": (SourceExpiredError, CODE_SOURCE_EXPIRED, False),
        "restricted": (SourceRestrictedError, CODE_SOURCE_RESTRICTED, False),
        "rate_limited": (RateLimitedError, CODE_RATE_LIMITED, True),
        "unavailable": (MetadataUnavailableError, CODE_METADATA_UNAVAILABLE, True),
    }
    for status, (cls, code, retryable) in cases.items():
        err = error_for_status(status, "youtube", "some reason")
        assert isinstance(err, cls)
        assert err.code == code
        assert err.retryable is retryable
        assert "some reason" in err.safe_message
    # unknown status → conservative transient error (never fabricated cause)
    err = error_for_status("something-new", "youtube", None)
    assert err.code == CODE_METADATA_UNAVAILABLE
    assert err.retryable is True


def test_materialization_unsupported_typed():
    err = MaterializationUnsupportedError("metadata OK but no authorized download")
    assert err.code == CODE_MATERIALIZATION_UNSUPPORTED
    assert err.retryable is False
    # the Phase 7 runtime exception is the same honesty gate
    try:
        raise MaterializationUnavailable("nope")
    except MaterializationUnavailable as exc:
        assert "nope" in str(exc)


# =============================================================================
# Metadata-permitted-but-materialization-denied (spec section 7)
# =============================================================================

def test_metadata_ok_materialization_denied_clear_message():
    p = YouTubeProvider(config=YouTubeConfig(api_key="yt-key-sentinel-000"),
                        resolver=_resolver(PUBLIC))
    # supports flags are the contract the pipeline consults
    assert p.supports_validation is True
    assert p.supports_materialization is False
    with pytest.raises(MaterializationUnavailable) as exc:
        run(p.materialize("https://youtu.be/abc123XYZ", "/tmp/anywhere"))
    message = str(exc.value).lower()
    assert "metadata" in message
    assert "upload the file directly" in message


def test_twitch_metadata_ok_materialization_denied_clear_message():
    p = TwitchProvider(config=TwitchConfig(client_id="tw-client-id-sentinel"))
    assert p.supports_validation is True
    assert p.supports_materialization is False
    with pytest.raises(MaterializationUnavailable) as exc:
        run(p.materialize("https://twitch.tv/videos/1", "/tmp/anywhere"))
    message = str(exc.value).lower()
    assert "official" in message
    assert "upload the file directly" in message
