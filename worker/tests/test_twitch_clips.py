"""TASK 014 — Twitch CLIP import tests (universal link ingestion).

The ONLY authorized Twitch media path: the official Get Clips Download
endpoint under the creator's OAuth authorization (refresh_token grant).
Deterministic: httpx.MockTransport (no network), injected resolver
(deterministic offline DNS), real ffprobe for byte validation.

Contract: URL classification; successful authorized import (token
refresh → editor id → clip metadata → download URL → host-suffix guard
→ SSRF-guarded stream → FFprobe BEFORE storage); honest per-class
refusals (VOD/live); PERMANENT + actionable authorization failures;
Helix failure mapping; CDN hardening (https-only, media host suffixes,
redirect rejection, DNS rebinding, size cap, empty/corrupt bytes);
OAuth hygiene (refresh token + client secret ONLY in the token POST
body; no token/secret/signed-URL in URLs or log records).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.providers.base import MaterializationUnavailable, STATUS_AVAILABLE
from app.providers.errors import (
    InvalidUrlError,
    MetadataUnavailableError,
    RateLimitedError,
    SourceExpiredError,
    SourceRestrictedError,
    TransferInterruptedError,
    UnauthorizedError,
)
from app.providers.twitch import TwitchConfig, TwitchProvider
from app.validation.media import MediaValidationError

PUBLIC = "93.184.216.34"
API_HOST = "api.twitch.tv"
OAUTH_HOST = "id.twitch.tv"
CDN_HOST = "cdn.twitchcdn.net"

SLUG = "RewolvedKleinerPinguin"
REFRESH_SECRET = "refresh-sentinel-do-not-leak"
CLIENT_SECRET = "client-secret-sentinel"
ACCESS_SENTINEL = "creator-access-sentinel"
CDN_SIGNATURE = "CLIP-CDN-SIGNATURE-SECRET"
CDN_URL = f"https://{CDN_HOST}/clips/{SLUG}.mp4?token={CDN_SIGNATURE}"
CLIP_URLS = {
    "short-link": f"https://clips.twitch.tv/{SLUG}",
    "web-clips": f"https://www.twitch.tv/clips/{SLUG}",
    "channel-clip": f"https://twitch.tv/creatorname/clip/{SLUG}",
}


def run(coro: Any) -> Any:
    return asyncio.new_event_loop().run_until_complete(coro)


def _resolver(*answers_by_host: tuple[str, tuple[str, ...]]):
    table = dict(answers_by_host)

    def resolve(hostname: str):
        return table.get((hostname or "").lower(), ("NXDOMAIN",))

    return resolve


def _config(**over: Any) -> TwitchConfig:
    base: dict[str, Any] = dict(
        client_id="cid-sentinel",
        client_secret=CLIENT_SECRET,
        owner_refresh_token=REFRESH_SECRET,
        max_source_bytes=10 * 1024 * 1024,
        chunk_bytes=64 * 1024,
        max_attempts=1,
        backoff_seconds=0.0,
        transfer_timeout_seconds=5.0,
        transfer_deadline_seconds=None,
        ffprobe_timeout_seconds=15.0,
    )
    base.update(over)
    return TwitchConfig(**base)


def _public_resolver(*hosts: str) -> Any:
    return _resolver(*((h, (PUBLIC,)) for h in hosts))

def _token_ok(state: dict) -> httpx.Response:
    state["token_posts"] += 1
    return httpx.Response(200, json={"access_token": ACCESS_SENTINEL,
                                     "expires_in": 3600, "token_type": "bearer"})


def _token_fail(state: dict) -> httpx.Response:
    state["token_posts"] += 1
    # A realistic provider error body with a secret-looking payload: the
    # provider must NEVER echo it into messages or logs.
    return httpx.Response(400, json={"error": "expired_token",
                                     "error_description": f"refresh={REFRESH_SECRET} leak"})


def _clip_record(state: dict, params: dict, status: int = 200) -> httpx.Response:
    state["clip_gets"] += 1
    state["clip_params"] = dict(params)
    if status != 200:
        return httpx.Response(status)
    return httpx.Response(200, json={"data": [
        {"id": SLUG, "broadcaster_id": "888000", "broadcaster_name": "Creator",
         "title": "The Clip", "duration": "0.060",
         "url": f"https://clips.twitch.tv/{SLUG}"}]})


def _downloads_ok(state: dict, params: dict) -> httpx.Response:
    state["download_gets"] += 1
    state["download_params"] = dict(params)
    return httpx.Response(200, json={"data": [
        {"clip_id": SLUG, "landscape_download_url": CDN_URL,
         "portrait_download_url": None}]})


def make_handler(video: bytes = b"", state: dict | None = None,
                 downloads: str = "ok", clip_status: int = 200) -> Any:
    """Single MockTransport dispatch for the whole authorized flow."""
    state = state if state is not None else {}
    state.setdefault("token_posts", 0)
    state.setdefault("token_bodies", [])
    state.setdefault("token_urls", [])
    state.setdefault("auth_headers", [])
    state.setdefault("clip_gets", 0)
    state.setdefault("clip_params", {})
    state.setdefault("download_gets", 0)
    state.setdefault("download_params", {})
    state.setdefault("cdn_hits", 0)
    request_body = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_body
        request_body = request.content or b""
        host = request.url.host or ""
        path = request.url.path
        if host == OAUTH_HOST and path == "/oauth2/token":
            state["token_urls"].append(str(request.url))
            if isinstance(request_body, (bytes, bytearray)):
                request_body = request_body.decode("utf-8", "replace")
            state["token_bodies"].append(str(request_body))
            return _token_ok(state) if downloads != "token-400" else _token_fail(state)
        if host == API_HOST:
            state["auth_headers"].append(request.headers.get("authorization") or "")
            if path == "/helix/users":
                return httpx.Response(200, json={"data": [{"id": "999000"}]})
            if path == "/helix/clips":
                return _clip_record(state, dict(request.url.params), clip_status)
            if path == "/helix/clips/downloads":
                # "redirect-*" modes only alter the CDN host's behavior —
                # the helix flow itself is normal.
                if downloads == "ok" or downloads.startswith("redirect-"):
                    return _downloads_ok(state, dict(request.url.params))
                if downloads == "404":
                    return httpx.Response(404)
                if downloads == "403":
                    return httpx.Response(403)
                if downloads == "429":
                    return httpx.Response(429)
                if downloads == "500":
                    return httpx.Response(500)
                if downloads == "400":
                    return httpx.Response(400, json={"message": "editor mismatch"})
                if downloads == "empty":
                    return httpx.Response(200, json={"data": []})
                if downloads == "wrong-clip":
                    return httpx.Response(200, json={"data": [
                        {"clip_id": "OtherClip", "landscape_download_url": CDN_URL}]})
                if downloads == "no-url":
                    return httpx.Response(200, json={"data": [
                        {"clip_id": SLUG, "landscape_download_url": None,
                         "portrait_download_url": None}]})
                if downloads.startswith("url:"):
                    return httpx.Response(200, json={"data": [
                        {"clip_id": SLUG, "landscape_download_url": downloads[4:],
                         "portrait_download_url": None}]})
            return httpx.Response(404)
        if host == CDN_HOST:
            state["cdn_hits"] += 1
            # "redirect-public": the 302 answers EVERY request (chain-level
            # hop, rejected by the strict allowlist / public-IP guard).
            if downloads == "redirect-public":
                return httpx.Response(302, headers={"location": "https://other-cdn.example.net/x.mp4"})
            if request.method == "HEAD":
                return httpx.Response(200, headers={"content-length": str(len(video))})
            return httpx.Response(200, content=video)
        return httpx.Response(404)

    return handler


def _provider(ffprobe_bin: str, handler: Any, *, config: TwitchConfig | None = None,
              resolver: Any = None) -> TwitchProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TwitchProvider(
        http_client=client, timeout=5.0,
        config=config or _config(), resolver=resolver, ffprobe_path=ffprobe_bin,
    )


def _default_resolver(*extra: Any) -> Any:
    return _resolver((API_HOST, (PUBLIC,)), (OAUTH_HOST, (PUBLIC,)),
                     (CDN_HOST, (PUBLIC,)), *extra)


# =============================================================================
# URL classification
# =============================================================================

def test_clip_url_forms_are_recognized():
    p = TwitchProvider(config=_config())
    for url in CLIP_URLS.values():
        assert p.can_handle(url), url
        assert p._is_clip_url(url), url
        assert p._clip_slug(url) == SLUG, url


def test_non_clip_urls_are_not_clips():
    p = TwitchProvider(config=_config())
    for url in ("https://twitch.tv/videos/123456789",
                "https://twitch.tv/live/creatorname",
                "https://twitch.tv/creatorname",
                "https://twitch.tv/directory/browse"):
        assert p._is_clip_url(url) is False, url
        assert p._clip_slug(url) is None, url


def test_vod_id_wins_over_clips_path_shape():
    """twitch.tv/clips/videos/123 is the VOD of a channel NAMED 'clips' —
    it must be classified as a VOD, never as a clip slug."""
    p = TwitchProvider(config=_config())
    url = "https://twitch.tv/clips/videos/123456789"
    assert p._vod_id(url) == "123456789"
    assert p._is_clip_url(url) is False


def test_short_link_subdomain_is_handled():
    p = TwitchProvider(config=_config())
    assert p.can_handle(CLIP_URLS["short-link"])
    assert p.can_handle("https://clips.twitch.tv/abc-def123")


def test_vod_and_live_materialization_remain_refused():
    p = TwitchProvider(config=_config(owner_refresh_token=None))
    for url in ("https://twitch.tv/videos/123456789",
                "https://twitch.tv/live/creatorname"):
        with pytest.raises(MaterializationUnavailable) as exc:
            run(p.materialize(url, Path("/tmp/tc-vod")))
        assert "official" in str(exc.value).lower()
        assert "upload the file directly" in str(exc.value).lower()


# =============================================================================
# Successful authorized import (the happy path)
# =============================================================================

def test_clip_validate_reports_metadata_with_app_token():
    """Clip metadata works with the app token (client id + secret)."""
    state: dict = {}
    p = _provider("ffprobe", make_handler(state=state),
                  resolver=_default_resolver())
    check = run(p.validate(CLIP_URLS["web-clips"]))
    assert check.status == STATUS_AVAILABLE, check.reason
    assert check.metadata is not None
    assert check.metadata.provider_id == SLUG
    assert check.metadata.creator_name == "Creator"
    assert check.metadata.title == "The Clip"


def test_clip_import_happy_path_end_to_end(ffprobe_bin, synthetic_videos, tmp_path):
    video = synthetic_videos["valid"].read_bytes()
    state: dict = {}
    p = _provider(ffprobe_bin, make_handler(video=video, state=state),
                  resolver=_default_resolver())
    destination = run(p.materialize(CLIP_URLS["short-link"], tmp_path))
    # 1) The real bytes arrived.
    assert destination.is_file()
    assert destination.read_bytes() == video
    # 2) The official flow was used, in order, with the right identities.
    assert state["token_posts"] == 1
    body = state["token_bodies"][0]
    assert "grant_type=refresh_token" in body
    assert REFRESH_SECRET in body
    assert CLIENT_SECRET in body
    assert state["auth_headers"] and all(h == f"Bearer {ACCESS_SENTINEL}"
                                         for h in state["auth_headers"])
    assert state["clip_params"]["id"] == SLUG
    assert state["download_params"] == {"editor_id": "999000",
                                        "broadcaster_id": "888000",
                                        "clip_id": SLUG}
    assert state["cdn_hits"] >= 1
    # 3) The progress callback was exercised (worker reports 25..65).
    progress_calls: list[int] = []
    p2 = _provider(ffprobe_bin, make_handler(video=video, state={}),
                   resolver=_default_resolver())
    run(p2.materialize(CLIP_URLS["web-clips"], tmp_path / "second",
                       progress=lambda done, total: progress_calls.append(done)))
    assert progress_calls and progress_calls == sorted(progress_calls)


def test_clip_import_caches_token_and_editor_id(ffprobe_bin, synthetic_videos, tmp_path):
    """One provider instance: the token + /users calls happen ONCE."""
    video = synthetic_videos["valid"].read_bytes()
    state: dict = {}
    p = _provider(ffprobe_bin, make_handler(video=video, state=state),
                  resolver=_default_resolver())
    run(p.materialize(CLIP_URLS["short-link"], tmp_path / "a"))
    run(p.materialize(CLIP_URLS["web-clips"], tmp_path / "b"))
    assert state["token_posts"] == 1
    assert state["clip_gets"] == 2


# =============================================================================
# Unauthorized / expired creator authorization (PERMANENT + actionable)
# =============================================================================

def test_clip_import_without_creator_token_fails_permanently_no_network(tmp_path):
    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no network without creator authorization")

    p = _provider("ffprobe", boom,
                  config=_config(owner_refresh_token=None),
                  resolver=_default_resolver())
    with pytest.raises(UnauthorizedError) as exc:
        run(p.materialize(CLIP_URLS["short-link"], tmp_path))
    assert not exc.value.retryable
    message = str(exc.value)
    assert "WORKER_TWITCH_OAUTH_REFRESH_TOKEN" in message
    assert "channel:manage:clips" in message
    assert "upload the video file directly" in message
    # No secret may appear.
    assert CLIENT_SECRET not in message


def test_expired_or_invalid_refresh_token_fails_permanently(tmp_path):
    state: dict = {}
    p = _provider("ffprobe", make_handler(state=state, downloads="token-400"),
                  resolver=_default_resolver())
    with pytest.raises(UnauthorizedError) as exc:
        run(p.materialize(CLIP_URLS["short-link"], tmp_path))
    assert not exc.value.retryable
    message = str(exc.value)
    assert "400" in message
    # The provider's error body (echoing a secret) must NEVER leak.
    assert REFRESH_SECRET not in message
    assert "leak" not in message
    # And it must not be retried into a loop (one token attempt only).
    assert state["token_posts"] == 1


def test_creator_account_unidentifiable_is_permanent(tmp_path):
    """GET /users non-200 → the creator authorization is broken."""
    state: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/helix/users":
            return httpx.Response(401)
        return make_handler(state=state)(request)

    p = _provider("ffprobe", handler, resolver=_default_resolver())
    with pytest.raises(UnauthorizedError) as exc:
        run(p.materialize(CLIP_URLS["short-link"], tmp_path))
    assert not exc.value.retryable
    assert "creator" in str(exc.value).lower()


def test_editor_mismatch_400_is_a_deployment_error(tmp_path):
    """Official 400 on /clips/downloads: token user != editor_id."""
    state: dict = {}
    p = _provider("ffprobe", make_handler(state=state, downloads="400"),
                  resolver=_default_resolver())
    with pytest.raises(UnauthorizedError) as exc:
        run(p.materialize(CLIP_URLS["short-link"], tmp_path))
    assert not exc.value.retryable
    assert "editor" in str(exc.value).lower()


# =============================================================================
# Helix API failure mapping (clip metadata + downloads)
# =============================================================================

@pytest.mark.parametrize("status,exc_type,permanent", [
    (429, RateLimitedError, False),
    (403, SourceRestrictedError, True),
    (500, MetadataUnavailableError, False),
])
def test_clip_metadata_failures_are_mapped(tmp_path, status, exc_type, permanent):
    state: dict = {}
    p = _provider("ffprobe", make_handler(state=state, clip_status=status),
                  resolver=_default_resolver())
    with pytest.raises(exc_type) as exc:
        run(p.materialize(CLIP_URLS["short-link"], tmp_path))
    assert exc.value.retryable is (not permanent)


def test_clip_deleted_reports_expired(tmp_path):
    """GET /clips 200 with an empty/unknown data list → deleted."""
    state: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/helix/clips":
            return httpx.Response(200, json={"data": []})
        return make_handler(state=state)(request)

    p = _provider("ffprobe", handler, resolver=_default_resolver())
    with pytest.raises(SourceExpiredError) as exc:
        run(p.materialize(CLIP_URLS["short-link"], tmp_path))
    assert exc.value.retryable is False


@pytest.mark.parametrize("downloads,exc_type,permanent", [
    ("404", SourceExpiredError, True),
    ("403", SourceRestrictedError, True),
    ("429", RateLimitedError, False),
    ("500", MetadataUnavailableError, False),
    ("empty", MetadataUnavailableError, False),
    ("wrong-clip", MetadataUnavailableError, False),
])
def test_clip_download_failures_are_mapped(tmp_path, downloads, exc_type, permanent):
    state: dict = {}
    p = _provider("ffprobe", make_handler(state=state, downloads=downloads),
                  resolver=_default_resolver())
    with pytest.raises(exc_type) as exc:
        run(p.materialize(CLIP_URLS["short-link"], tmp_path))
    assert exc.value.retryable is (not permanent)


def test_download_url_null_is_an_honest_refusal(tmp_path):
    """Twitch 200 but no download URL: the media is gone — permanent."""
    state: dict = {}
    p = _provider("ffprobe", make_handler(state=state, downloads="no-url"),
                  resolver=_default_resolver())
    with pytest.raises(MaterializationUnavailable) as exc:
        run(p.materialize(CLIP_URLS["short-link"], tmp_path))
    assert "no longer available" in str(exc.value).lower()


# =============================================================================
# CDN hardening (host suffixes, https-only, redirects, SSRF, sizes)
# =============================================================================

def test_cdn_host_outside_configured_suffixes_is_rejected(tmp_path):
    state: dict = {}
    p = _provider("ffprobe", make_handler(state=state,
                                          downloads=f"url:https://evil.example.com/{SLUG}.mp4"),
                  resolver=_default_resolver(("evil.example.com", (PUBLIC,))))
    with pytest.raises(InvalidUrlError) as exc:
        run(p.materialize(CLIP_URLS["short-link"], tmp_path))
    assert "media host" in str(exc.value).lower()
    assert state["cdn_hits"] == 0


def test_cdn_http_scheme_is_rejected(tmp_path):
    state: dict = {}
    p = _provider("ffprobe", make_handler(state=state,
                                          downloads=f"url:http://{CDN_HOST}/{SLUG}.mp4"),
                  resolver=_default_resolver())
    with pytest.raises(InvalidUrlError):
        run(p.materialize(CLIP_URLS["short-link"], tmp_path))
    assert state["cdn_hits"] == 0


def test_cdn_subdomain_of_permitted_suffix_is_accepted(ffprobe_bin, synthetic_videos, tmp_path):
    video = synthetic_videos["valid"].read_bytes()
    state: dict = {}
    p = _provider(ffprobe_bin, make_handler(video=video, state=state),
                  resolver=_default_resolver())
    destination = run(p.materialize(CLIP_URLS["channel-clip"], tmp_path))
    assert destination.read_bytes() == video


def test_cdn_redirect_to_unlisted_host_is_rejected(ffprobe_bin, synthetic_videos, tmp_path):
    """The chain client is allowlist=[CDN host] (strict): a redirect to a
    different host fails closed — even when that host is public."""
    video = synthetic_videos["valid"].read_bytes()
    state: dict = {}
    p = _provider(ffprobe_bin,
                  make_handler(video=video, state=state, downloads="redirect-public"),
                  resolver=_default_resolver(("other-cdn.example.net", (PUBLIC,))))
    with pytest.raises(InvalidUrlError):
        run(p.materialize(CLIP_URLS["short-link"], tmp_path))


def test_cdn_redirect_to_private_ip_is_rejected(ffprobe_bin, synthetic_videos, tmp_path):
    video = synthetic_videos["valid"].read_bytes()
    state: dict = {}
    p = _provider(ffprobe_bin,
                  make_handler(video=video, state=state, downloads="redirect-public"),
                  resolver=_default_resolver(("other-cdn.example.net", ("10.0.0.1",))))
    with pytest.raises(InvalidUrlError):
        run(p.materialize(CLIP_URLS["short-link"], tmp_path))


def test_cdn_dns_rebinding_poisons_the_fetch(ffprobe_bin, synthetic_videos, tmp_path):
    video = synthetic_videos["valid"].read_bytes()
    state: dict = {}
    # ONE private answer among the CDN name's answers: refuse.
    p = _provider(ffprobe_bin, make_handler(video=video, state=state),
                  resolver=_resolver((API_HOST, (PUBLIC,)), (OAUTH_HOST, (PUBLIC,)),
                                     (CDN_HOST, (PUBLIC, "169.254.169.254"))))
    with pytest.raises(InvalidUrlError):
        run(p.materialize(CLIP_URLS["short-link"], tmp_path))
    assert state["cdn_hits"] == 0


def test_cdn_size_cap_aborts_and_cleans_up(ffprobe_bin, synthetic_videos, tmp_path):
    video = synthetic_videos["valid"].read_bytes()
    state: dict = {}
    p = _provider(ffprobe_bin, make_handler(video=video, state=state),
                  config=_config(max_source_bytes=1024),
                  resolver=_default_resolver())
    with pytest.raises(TransferInterruptedError) as exc:
        run(p.materialize(CLIP_URLS["short-link"], tmp_path))
    assert "maximum size" in str(exc.value).lower()
    assert not (tmp_path / "source.mp4").exists()


def test_cdn_empty_transfer_fails_cleanly(ffprobe_bin, tmp_path):
    state: dict = {}
    p = _provider(ffprobe_bin, make_handler(video=b"", state=state),
                  resolver=_default_resolver())
    with pytest.raises(TransferInterruptedError) as exc:
        run(p.materialize(CLIP_URLS["short-link"], tmp_path))
    assert "empty file" in str(exc.value).lower()
    assert not (tmp_path / "source.mp4").exists()


def test_cdn_bytes_that_are_not_video_fail_probe(ffprobe_bin, tmp_path):
    state: dict = {}
    page = b"<!doctype html><html><body>not a clip</body></html>"
    p = _provider(ffprobe_bin, make_handler(video=page, state=state),
                  resolver=_default_resolver())
    with pytest.raises(MediaValidationError):
        run(p.materialize(CLIP_URLS["short-link"], tmp_path))
    assert not (tmp_path / "source.mp4").exists()


def test_cdn_surprise_redirect_during_stream_fails(ffprobe_bin, synthetic_videos, tmp_path):
    """HEAD 200-final, GET 302: never follow an unvalidated hop."""
    video = synthetic_videos["valid"].read_bytes()
    state: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if (request.url.host or "") == CDN_HOST and request.method == "GET":
            return httpx.Response(302, headers={"location": "https://other-cdn.example.net/x.mp4"})
        return make_handler(video=video, state=state)(request)

    p = _provider(ffprobe_bin, handler,
                  resolver=_default_resolver(("other-cdn.example.net", (PUBLIC,))))
    with pytest.raises(TransferInterruptedError) as exc:
        run(p.materialize(CLIP_URLS["short-link"], tmp_path))
    assert "redirect" in str(exc.value).lower()


# =============================================================================
# OAuth token + signed-URL hygiene (URLs and logs)
# =============================================================================

def test_token_exchange_url_carries_no_secrets(ffprobe_bin, tmp_path):
    """The refresh token + client secret live ONLY in the POST body —
    the token URL must be parameter-free (no query, no fragment)."""
    state: dict = {}
    p = _provider(ffprobe_bin, make_handler(state=state),
                  resolver=_default_resolver())
    with pytest.raises(TransferInterruptedError):  # empty video → clean fail
        run(p.materialize(CLIP_URLS["short-link"], tmp_path))
    assert state["token_posts"] == 1
    for url in state["token_urls"]:
        # The endpoint path itself is /oauth2/token; the point is that NO
        # credential ever appears as a query parameter.
        assert "?" not in url and "#" not in url
        assert url.endswith("/oauth2/token")
        assert REFRESH_SECRET not in url and CLIENT_SECRET not in url
        assert ACCESS_SENTINEL not in url


def test_no_secret_in_any_error_message(ffprobe_bin, synthetic_videos, tmp_path):
    video = synthetic_videos["valid"].read_bytes()
    for downloads in ("403", "400", "500"):
        state: dict = {}
        p = _provider(ffprobe_bin, make_handler(video=video, state=state,
                                                downloads=downloads),
                      resolver=_default_resolver())
        with pytest.raises(Exception) as exc:  # noqa: B017 — any typed error
            run(p.materialize(CLIP_URLS["short-link"], tmp_path))
        message = str(exc.value)
        for secret in (REFRESH_SECRET, CLIENT_SECRET, ACCESS_SENTINEL, CDN_SIGNATURE):
            assert secret not in message, (downloads, secret)
        assert "token=" not in message


def test_no_secret_in_log_records(ffprobe_bin, synthetic_videos, tmp_path, caplog):
    """A failing import must not log the refresh token, client secret,
    access token, or the signed CDN URL."""
    video = synthetic_videos["valid"].read_bytes()
    state: dict = {}
    p = _provider(ffprobe_bin, make_handler(video=video, state=state,
                                            downloads="403"),
                  resolver=_default_resolver())
    with caplog.at_level("DEBUG"):
        with pytest.raises(SourceRestrictedError):
            run(p.materialize(CLIP_URLS["short-link"], tmp_path))
    for record in caplog.records:
        rendered = f"{record.message} {record.args!r}"
        for secret in (REFRESH_SECRET, CLIENT_SECRET, ACCESS_SENTINEL,
                       CDN_SIGNATURE, "token="):
            assert secret not in rendered, (record.name, record.levelname, secret)
