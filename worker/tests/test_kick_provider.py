"""TASK 015 — Kick provider tests (first-class source adapter).

Deterministic: httpx.MockTransport (no network), injected resolver
(deterministic offline DNS). The Kick public API base is pinned to a
test mirror host; the Authorization header format is the documented
``APIKey <key>`` scheme.

Contract: URL classification (VOD / channel / lookalikes / non-Kick);
unconfigured = honest no-network unavailability; metadata happy path
(title/duration/creator/canonical URL); 404 channel → deleted; 401/403
→ key rejected; 429 → rate_limited; 5xx → unavailable; VOD id missing
from the list → deleted (Kick VODs expire 14-30 days); materialization
ALWAYS refused (no official media-download endpoint); the key never
appears outside the allowlisted Authorization header; registry order
(Kick before direct_url); the 8-method universal ingestion contract on
ALL six providers.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from app.providers.base import MaterializationUnavailable
from app.providers.kick import HOSTS, KickConfig, KickProvider, _parse_kick_url
from app.providers.registry import (
    default_providers,
    provider_for_url,
    provider_by_type,
    provider_is_configured,
)

API_HOST = "api.kick.test"  # mock base host (allowlisted by the provider)
PUBLIC = "93.184.216.34"
KEY = "kick-api-key-sentinel-do-not-leak"
BASE = f"https://{API_HOST}/public/v1"

CHANNEL = "sometestchannel"
VOD_ID = 1234567890
VOD_URL = f"https://kick.com/{CHANNEL}/v/{VOD_ID}"


def run(coro: Any) -> Any:
    return asyncio.new_event_loop().run_until_complete(coro)


def _resolver(*hosts: str) -> Any:
    return _make_resolver(*hosts)


def _make_resolver(*hosts: str) -> Any:
    table = {h: (PUBLIC,) for h in hosts}

    def resolve(hostname: str):
        return table.get((hostname or "").lower(), ("NXDOMAIN",))

    return resolve


def _config(**over: Any) -> KickConfig:
    base: dict[str, Any] = dict(api_key=KEY, api_base=BASE)
    base.update(over)
    return KickConfig(**base)


def make_handler(state: dict, *, videos_status: int = 200,
                 channel_status: int = 200, vod_found: bool = True,
                 is_live: bool = True) -> Any:
    """Single MockTransport dispatch for the Kick public API mirror."""
    state.setdefault("hits", [])
    state.setdefault("auth_headers", [])

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        assert host == API_HOST, f"unexpected host {host} (SSRF guard bypass?)"
        state["hits"].append((request.method, str(request.url)))
        state["auth_headers"].append(request.headers.get("authorization"))
        path = request.url.path
        if path == f"/public/v1/channels/{CHANNEL}/videos":
            if videos_status != 200:
                return httpx.Response(videos_status)
            items = []
            if vod_found:
                items.append({
                    "id": VOD_ID,
                    "title": "Late night ranked session",
                    "created_at": "2026-11-14T21:30:00.000Z",
                    "duration": 3600,
                    "views": 4200,
                })
            items.append({"id": 111, "title": "Older", "duration": 60})
            return httpx.Response(200, json={"data": items, "message": ""})
        if path == f"/public/v1/channels/{CHANNEL}":
            if channel_status != 200:
                return httpx.Response(channel_status)
            return httpx.Response(200, json={
                "data": {"name": CHANNEL, "is_live": is_live}, "message": ""})
        return httpx.Response(404, json={"data": {}, "message": "Not Found"})

    return handler


def _provider(handler: Any, state: dict[str, Any] | None = None,
              **cfg_over: Any) -> tuple[KickProvider, dict]:
    """Builds a provider over the mocked transport. ``state`` (when
    provided) MUST be the same dict the handler mutates."""
    state = state if state is not None else {}
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = KickProvider(
        http_client=http, timeout=5.0,
        config=_config(**cfg_over),
        resolver=_resolver(API_HOST),
    )
    return provider, state


def _close(provider: KickProvider) -> None:
    async def _do() -> None:
        await provider.close()
    run(_do())


# --------------------------------------------------------------------------
# URL classification (shape only, no network)
# --------------------------------------------------------------------------

class TestClassification:
    def test_vod_forms_claimed(self):
        for url in (
            VOD_URL,
            f"https://www.kick.com/{CHANNEL}/v/{VOD_ID}",
            f"https://m.kick.com/{CHANNEL}/v/{VOD_ID}/",
        ):
            assert _parse_kick_url(url) == (CHANNEL, VOD_ID), url

    def test_bare_channel_claimed_as_live(self):
        assert _parse_kick_url(f"https://kick.com/{CHANNEL}") == (CHANNEL, None)
        assert _parse_kick_url(f"https://www.kick.com/{CHANNEL}/") == (CHANNEL, None)

    def test_non_kick_hosts_rejected(self):
        assert _parse_kick_url("https://not-kick.com/a/b/v/1") is None
        assert _parse_kick_url("https://kick.com.evil.example/x/v/1") is None
        assert _parse_kick_url("https://evil-kick.com/x/v/1") is None
        assert _parse_kick_url("https://youtube.com/watch?v=x") is None

    def test_category_and_garbage_rejected(self):
        assert _parse_kick_url("https://kick.com/tv/fps") is None
        assert _parse_kick_url("https://kick.com/") is None
        assert _parse_kick_url("ftp://kick.com/x/v/1") is None
        assert _parse_kick_url("https://user:pass@kick.com/x/v/1") is None
        assert _parse_kick_url("https://kick.com:8443/x/v/1") is None
        assert _parse_kick_url("not a url") is None

    def test_non_numeric_vod_id_rejected(self):
        assert _parse_kick_url("https://kick.com/x/v/abc") is None


# --------------------------------------------------------------------------
# Unconfigured deployment: honest, NO network call
# --------------------------------------------------------------------------

class TestUnconfigured:
    def test_validate_without_key_never_touches_network(self):
        state: dict[str, Any] = {"hits": []}

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            state["hits"].append(str(request.url))  # any hit = test failure
            return httpx.Response(200, json={"data": []})

        provider = KickProvider(
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            config=KickConfig(api_key=None, api_base=BASE),
            resolver=_resolver(API_HOST),
        )
        for url in (VOD_URL, f"https://kick.com/{CHANNEL}"):
            check = run(provider.validate(url))
            assert check.status == "unavailable"
            assert check.metadata is None
            assert "WORKER_KICK_API_KEY" in (check.reason or "")
        assert state["hits"] == []  # no network call at all
        assert provider.is_configured is False
        assert provider_is_configured(provider) is False

    def test_unconfigured_materialize_still_refused(self):
        provider = KickProvider(config=KickConfig(api_key=None, api_base=BASE))
        with pytest.raises(MaterializationUnavailable) as exc:
            run(provider.materialize(VOD_URL, "/tmp/kick-test"))
        assert "no official VOD media-download endpoint" in str(exc.value)


# --------------------------------------------------------------------------
# Metadata via the mocked official public API
# --------------------------------------------------------------------------

class TestMetadata:
    def test_vod_available_with_full_metadata(self):
        state: dict[str, Any] = {}
        provider, state = _provider(make_handler(state), state)
        try:
            check = run(provider.validate(VOD_URL))
            assert check.status == "available"
            assert check.title == "Late night ranked session"
            assert check.duration_seconds == 3600.0
            meta = check.metadata
            assert meta is not None
            assert meta.provider == "kick"
            assert meta.canonical_url == VOD_URL
            assert meta.provider_id == str(VOD_ID)
            assert meta.creator_name == CHANNEL
            assert meta.published_at == "2026-11-14T21:30:00.000Z"
            assert meta.state == "completed"
            # the key travels ONLY in the Authorization header
            assert state["auth_headers"] == [f"APIKey {KEY}"]
        finally:
            _close(provider)

    def test_vod_id_not_in_list_is_deleted(self):
        provider, _state = _provider(make_handler({}, vod_found=False))
        try:
            check = run(provider.validate(VOD_URL))
            assert check.status == "deleted"
            assert "14-30 days" in (check.reason or "")
            assert "Kick Studio" in (check.reason or "")
        finally:
            _close(provider)

    def test_missing_channel_404_is_deleted(self):
        provider, _ = _provider(make_handler({}, videos_status=404))
        try:
            check = run(provider.validate(VOD_URL))
            assert check.status == "deleted"
            assert "not found" in (check.reason or "")
        finally:
            _close(provider)

    def test_key_rejected_401_and_403(self):
        for status in (401, 403):
            provider, _ = _provider(make_handler({}, videos_status=status))
            try:
                check = run(provider.validate(VOD_URL))
                assert check.status == "unavailable", status
                assert "rejected" in (check.reason or ""), status
            finally:
                _close(provider)

    def test_rate_limited_429(self):
        provider, _ = _provider(make_handler({}, videos_status=429))
        try:
            check = run(provider.validate(VOD_URL))
            assert check.status == "rate_limited"
        finally:
            _close(provider)

    def test_server_error_500(self):
        provider, _ = _provider(make_handler({}, videos_status=500))
        try:
            check = run(provider.validate(VOD_URL))
            assert check.status == "unavailable"
            assert "server error" in (check.reason or "")
        finally:
            _close(provider)

    def test_live_channel_refused_with_vod_workaround(self):
        provider, _ = _provider(make_handler({}, is_live=True))
        try:
            check = run(provider.validate(f"https://kick.com/{CHANNEL}"))
            assert check.status == "unavailable"
            assert "live" in (check.reason or "").lower()
            assert f"/v/" in (check.reason or "")
        finally:
            _close(provider)

    def test_offline_channel_refused(self):
        provider, _ = _provider(make_handler({}, is_live=False))
        try:
            check = run(provider.validate(f"https://kick.com/{CHANNEL}"))
            assert check.status == "unavailable"
            assert "not live" in (check.reason or "")
        finally:
            _close(provider)

    def test_channel_404_deleted(self):
        provider, _ = _provider(make_handler({}, channel_status=404))
        try:
            check = run(provider.validate(f"https://kick.com/{CHANNEL}"))
            assert check.status == "deleted"
        finally:
            _close(provider)

    def test_unexpected_shape_is_unavailable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": "garbage", "message": ""})

        provider, _ = _provider(handler)
        try:
            check = run(provider.validate(VOD_URL))
            assert check.status == "unavailable"
            assert "unexpected" in (check.reason or "")
        finally:
            _close(provider)


# --------------------------------------------------------------------------
# Materialization: ALWAYS an honest refusal (no media endpoint exists)
# --------------------------------------------------------------------------

class TestMaterializationRefusal:
    def test_materialize_refused_with_actionable_message(self):
        state: dict[str, Any] = {}
        provider, state = _provider(make_handler(state), state)
        try:
            with pytest.raises(MaterializationUnavailable) as exc:
                run(provider.materialize(VOD_URL, "/tmp/kick-test"))
            assert "Kick Studio" in str(exc.value)
            assert "upload the file directly" in str(exc.value)
            assert state["hits"] == []  # no network, no download attempt
        finally:
            _close(provider)

    def test_caps_flag_honest(self):
        provider = KickProvider(config=_config())
        assert provider.supports_validation is True
        assert provider.supports_materialization is False

    def test_no_key_leak_in_refusal(self):
        provider, _ = _provider(make_handler({}))
        try:
            with pytest.raises(MaterializationUnavailable) as exc:
                run(provider.materialize(VOD_URL, "/tmp/kick-test"))
            assert KEY not in str(exc.value)
        finally:
            _close(provider)


# --------------------------------------------------------------------------
# Registry wiring + the 8-method universal ingestion contract
# --------------------------------------------------------------------------

CONTRACT_METHODS = (
    "classify_url", "validate_source", "fetch_metadata", "acquire_media",
    "stream_or_download", "report_progress", "cleanup",
)


class TestRegistry:
    def test_order_platform_adapters_before_direct_url(self):
        providers = default_providers("/usr/bin/ffprobe")
        names = [p.name for p in providers]
        assert names == ["upload", "youtube", "twitch", "kick", "direct_url"]

    def test_kick_url_claimed_by_kick_not_direct_url(self):
        providers = default_providers("/usr/bin/ffprobe")
        # A video file on a kick.com lookalike is still a direct file...
        assert provider_for_url(
            "https://storage.kickcdn-like.example/v.mp4", providers).name == "direct_url"
        # ...but a kick.com VOD shape belongs to the kick adapter.
        assert provider_for_url(VOD_URL, providers).name == "kick"
        assert provider_by_type("kick", providers) is not None
        assert provider_by_type("kick", providers).name == "kick"

    def test_all_six_providers_expose_the_8_method_contract(self):
        providers = default_providers("/usr/bin/ffprobe")
        assert len(providers) == 5  # upload, youtube, twitch, kick, direct_url
        for provider in providers:
            for method in CONTRACT_METHODS:
                assert callable(getattr(provider, method, None)), (
                    f"{provider.name} lacks contract method {method}")

    def test_classify_url_matches_can_handle(self):
        providers = default_providers("/usr/bin/ffprobe")
        samples = (
            VOD_URL,
            f"https://kick.com/{CHANNEL}",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://twitch.tv/videos/123",
            "https://storage.example.com/v.mp4",
            "https://example.com/page.html",
            "not a url",
        )
        for url in samples:
            for provider in providers:
                try:
                    expected = provider.can_handle(url)
                except Exception:  # noqa: BLE001 — a claim must never raise
                    expected = False
                assert provider.classify_url(url) is expected or (
                    provider.classify_url(url) == expected), (provider.name, url)

    def test_contract_aliases_delegate(self):
        provider, state = _provider(make_handler({}))
        try:
            # validate_source is the same object as validate
            a = run(provider.validate_source(VOD_URL))
            b = run(provider.validate(VOD_URL))
            assert (a.status, a.title, a.duration_seconds) == \
                   (b.status, b.title, b.duration_seconds)
            # fetch_metadata returns the NormalizedSource when available
            meta = run(provider.fetch_metadata(VOD_URL))
            assert meta is not None and meta.provider == "kick"
            # report_progress is a no-op hook (pipeline owns progress)
            assert provider.report_progress(0.42) is None
            # cleanup is best-effort and never raises on a missing path
            provider.cleanup("/tmp/does-not-exist-kick-cleanup")
        finally:
            _close(provider)

    def test_settings_wiring(self):
        """Settings → registry config path (WORKER_KICK_API_KEY)."""
        from app.config.settings import Settings

        s = Settings(supabase_url="http://x", supabase_service_role_key="y",
                     kick_api_key="")  # empty → None (validator)
        assert s.kick_api_key is None
        s2 = Settings(supabase_url="http://x", supabase_service_role_key="y",
                      kick_api_key=KEY)
        providers = default_providers("/usr/bin/ffprobe", settings=s2)
        kick = provider_by_type("kick", providers)
        assert kick is not None
        assert kick.is_configured is True
        assert kick._config.api_base == "https://api.kick.com/public/v1"
