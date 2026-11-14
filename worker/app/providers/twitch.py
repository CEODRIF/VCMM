"""Twitch adapter (Phase 7 foundation, TASK 011 — authorized metadata).

**Official, policy-compliant capabilities (exactly what this adapter
implements — nothing else is claimed):**

METADATA (authorized, official Helix API):
- UNCONFIGURED (no ``WORKER_TWITCH_CLIENT_ID``): the adapter reports
  ``unavailable`` with a clear "not configured" reason. (Phase 7 called
  Helix keyless, which always 401'd and was misreported as "restricted" —
  TASK 011 makes the unconfigured state honest. Injected test clients
  without credentials keep working for the URL/shape matrix.)
- CLIENT-ID ONLY: public VOD metadata via ``GET /videos?ids=`` with the
  ``Client-Id`` header (the documented public-data mode) — title,
  duration, created time, creator, thumbnail, archive/highlight kind.
- CLIENT-ID + SECRET: an app access token via the official
  ``client_credentials`` grant (cached, auto-refreshed) is used instead
  of the bare header.
- Live URLs (``/live/{channel}``, bare ``/{channel}``): classified via
  the official ``GET /streams`` — online → ``live`` (not materializable),
  offline → unavailable.

MATERIALIZATION (download of the media), TASK 014:

- **VOD URLs — NOT SUPPORTED.** The official Twitch APIs expose no
  endpoint that returns FULL-VOD media bytes to a third-party
  application. The historical ``GET /videos/{id}/clips`` endpoint is
  DEFUNCT (verified 2026-11: live 404), and "GQL download" paths are
  undocumented/unapproved and are NOT used. Materialization of VOD URLs
  is EXPOSED as unsupported (:class:`MaterializationUnavailable`) — no
  unauthorized session cookies, no private endpoints, no circumvention.
- **CLIP URLs — SUPPORTED through the official
  ``GET /helix/clips/downloads`` endpoint** (the only authorized
  media-access path Twitch exposes for creator-owned content): the
  endpoint serves temporary CDN download URLs ONLY to the clip's
  broadcaster or an authorized editor — it requires a USER access token
  with the ``channel:manage:clips`` (or ``editor:manage:clips``) scope.
  This deployment obtains that token the official way: the operator
  configures the creator's OAuth REFRESH token (``WORKER_TWITCH_OAUTH_
  REFRESH_TOKEN``) for the deployment's Twitch application, and the
  worker exchanges it via the official ``refresh_token`` grant (cached,
  auto-refreshing, single-flight — the same pattern as the YouTube
  OAuth flow). The returned download URL is https-only, must match the
  configured Twitch CDN host suffixes, and is then fetched through the
  full SSRF guard (public-IP DNS revalidation, size cap, deadline,
  bounded redirects) and FFprobe-verified before storage. When the
  creator authorization is not configured, clip materialization fails
  with a clear, actionable error — never guessed, never worked around.

URL claims (host-allowlisted, scheme-restricted, SSRF-guarded):
``twitch.tv`` / ``clips.twitch.tv`` — ``/videos/{id}``,
``/{channel}/videos/{id}`` (VODs), ``/clips/{slug}``,
``/{channel}/clip/{slug}`` (clips), ``/live/{channel}``, bare
``/{channel}`` (live check).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ..validation.media import probe_media
from .base import (
    STATUS_AVAILABLE,
    STATUS_DELETED,
    STATUS_RATE_LIMITED,
    STATUS_RESTRICTED,
    STATUS_UNAVAILABLE,
    MaterializationUnavailable,
    ProviderCheck,
)
from .errors import (
    InvalidUrlError,
    MetadataUnavailableError,
    ProviderError,
    RateLimitedError,
    SourceExpiredError,
    SourceRestrictedError,
    TransferInterruptedError,
    UnauthorizedError,
    redact,
)
from .http_guard import SafeHttpClient
from .models import NormalizedSource

logger = logging.getLogger("clipforge.providers.twitch")

#: twitch.tv/videos/{id} and twitch.tv/{channel}/videos/{id}
_VOD_PATH_RE = re.compile(r"/videos/([0-9]+)(?:/|$)")
#: twitch.tv/live/{channel} or a bare channel page
_LIVE_PATH_RE = re.compile(r"^/live/([a-z0-9_]{1,25})(?:/|$)", re.IGNORECASE)
_BARE_CHANNEL_RE = re.compile(r"^/([a-z0-9_]{1,25})/?$", re.IGNORECASE)
#: twitch.tv/clips/{slug} and twitch.tv/{channel}/clip/{slug}
#: (Twitch clip ids are alphanumeric; observed slugs also carry '-').
_CLIP_PATH_RE = re.compile(
    r"^/(?:[a-z0-9_]{1,25}/)?clips?/([A-Za-z0-9][A-Za-z0-9-]{0,99})(?:/.*)?$",
    re.IGNORECASE,
)
#: clips.twitch.tv/{slug} — the canonical clip short-link domain.
_CLIPS_SUBDOMAIN_RE = re.compile(
    r"^/([A-Za-z0-9][A-Za-z0-9-]{0,99})/?$", re.IGNORECASE
)
#: Hosts the adapter claims (all official Twitch properties).
_CLAIMED_HOSTS = {"twitch.tv", "www.twitch.tv", "go.twitch.tv", "m.twitch.tv",
                  "clips.twitch.tv"}


@dataclass(frozen=True)
class TwitchConfig:
    """Operator-provided Twitch authorization (server-side only).

    Minimum official requirement for public VOD metadata: a Twitch
    application's CLIENT ID. A client SECRET enables the app-access-token
    flow (the recommended authenticated mode).

    TASK 014: ``owner_refresh_token`` is the CREATOR'S OAuth refresh
    token (granted by the clip's broadcaster/editor with the
    ``channel:manage:clips`` scope, authorized against THIS deployment's
    client id) — the authorization that makes CLIP materialization
    possible through the official Get Clips Download endpoint. It
    requires ``client_id`` + ``client_secret`` (official refresh_token
    grant) and is validated as such at startup.
    """

    client_id: str | None = None
    client_secret: str | None = None
    owner_refresh_token: str | None = None
    api_base: str = "https://api.twitch.tv/helix"
    oauth_base: str = "https://id.twitch.tv"
    #: Suffixes permitted for the CDN download URLs returned by the
    #: official Get Clips Download endpoint (Twitch-owned media hosts).
    media_host_suffixes: tuple[str, ...] = ("twitchcdn.net", "twitch.tv",
                                            "jtvnw.net")
    # -- transfer budgets (clip materialization; filled from Settings) ----
    max_source_bytes: int = 2 * 1024 * 1024 * 1024
    chunk_bytes: int = 1024 * 1024
    max_attempts: int = 1
    backoff_seconds: float = 1.0
    max_redirects: int = 3
    transfer_deadline_seconds: float | None = 1800.0
    transfer_timeout_seconds: float = 30.0
    #: FFprobe timeout for validating the materialized clip bytes.
    ffprobe_timeout_seconds: float = 60.0

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id)

    @property
    def app_token_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def creator_token_configured(self) -> bool:
        """The full authorization set for CLIP materialization: the
        refresh_token grant needs client id + secret + the creator's
        refresh token (enforced at settings startup too)."""
        return bool(self.client_id and self.client_secret
                    and self.owner_refresh_token)


class _AppTokenCache:
    """Official ``client_credentials`` grant, cached, single-flight.

    The client secret travels ONLY in the POST body of the token
    exchange — never in URLs, never in logs.
    """

    def __init__(self, config: TwitchConfig, token_http: SafeHttpClient) -> None:
        self._config = config
        self._http = token_http
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

    async def token(self) -> str:
        if self._token is not None and time.time() < self._expires_at - 60:
            return self._token
        async with self._lock:
            if self._token is not None and time.time() < self._expires_at - 60:
                return self._token
            return await self._refresh()

    async def _refresh(self) -> str:
        response = await self._http.request(
            "POST", f"{self._config.oauth_base}/oauth2/token",
            data={"grant_type": "client_credentials"},
            headers={"Client-Id": self._config.client_id or ""},
        )
        if response.status_code != 200:
            raise UnauthorizedError(
                "Twitch app-token exchange failed "
                f"(provider status {response.status_code}) — check the client "
                "credentials on this deployment.",
                details={"status": response.status_code},
            )
        try:
            payload = response.json()
        except ValueError:
            raise UnauthorizedError(
                "Twitch token endpoint returned an unexpected response."
            ) from None
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise UnauthorizedError(
                "Twitch token endpoint returned no access token."
            )
        try:
            lifetime = int(payload.get("expires_in", 86400))
        except (TypeError, ValueError):
            lifetime = 86400
        self._token = token
        self._expires_at = time.time() + max(60, lifetime)
        return token

    async def aclose(self) -> None:
        await self._http.aclose()


class _CreatorTokenCache:
    """Official ``refresh_token`` grant for the CREATOR's authorization
    (TASK 014 — the only path that makes Get Clips Download work).

    The refresh token and client secret travel ONLY in the POST body of
    the token exchange — never in URLs, never in logs (all error paths
    go through :func:`redact`). The resulting access token is cached
    with a safe expiry margin and refreshed single-flight.
    """

    def __init__(self, config: TwitchConfig, token_http: SafeHttpClient) -> None:
        self._config = config
        self._http = token_http
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

    async def token(self) -> str:
        if self._token is not None and time.time() < self._expires_at - 60:
            return self._token
        async with self._lock:
            if self._token is not None and time.time() < self._expires_at - 60:
                return self._token
            return await self._refresh()

    async def _refresh(self) -> str:
        response = await self._http.request(
            "POST", f"{self._config.oauth_base}/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._config.owner_refresh_token or "",
                "client_id": self._config.client_id or "",
                "client_secret": self._config.client_secret or "",
            },
        )
        if response.status_code != 200:
            # The provider's error body is NOT echoed (it may contain
            # request context) — only a stable, redacted explanation.
            raise UnauthorizedError(
                "Twitch creator-token refresh failed "
                f"(provider status {response.status_code}) — check "
                "WORKER_TWITCH_OAUTH_REFRESH_TOKEN and the Twitch app "
                "credentials on this deployment.",
                details={"status": response.status_code},
            )
        try:
            payload = response.json()
        except ValueError:
            raise UnauthorizedError(
                "Twitch token endpoint returned an unexpected response."
            ) from None
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise UnauthorizedError(
                "Twitch token endpoint returned no access token."
            )
        try:
            lifetime = int(payload.get("expires_in", 86400))
        except (TypeError, ValueError):
            lifetime = 86400
        self._token = token
        self._expires_at = time.time() + max(60, lifetime)
        return token

    async def aclose(self) -> None:
        await self._http.aclose()


class TwitchProvider:
    name = "twitch"
    supports_validation = True
    # TASK 014: clip URLs have an AUTHORIZED materialization path (the
    # official Get Clips Download endpoint under the creator's
    # authorization). VOD/live URLs have NO official media endpoint and
    # are refused per-URL-class inside materialize() — the flag says
    # "this adapter can materialize (some) Twitch URLs", and the
    # materialization gate stays honest in the error messages.
    supports_materialization = True

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        *,
        config: TwitchConfig | None = None,
        resolver: Any = None,
        max_redirects: int = 3,
        ffprobe_path: str = "ffprobe",
    ) -> None:
        self._config = config or TwitchConfig()
        self._external_client = http_client
        self._timeout = timeout
        self._resolver = resolver
        self._max_redirects = max_redirects
        self._ffprobe = ffprobe_path
        self._token_cache: _AppTokenCache | None = None
        self._creator_token_cache: _CreatorTokenCache | None = None
        self._creator_user_id_cache: str | None = None

    # -- capabilities / configuration -------------------------------------------

    @property
    def is_configured(self) -> bool:
        """False when no client id is configured — the adapter then
        refuses to call Helix and reports honest unavailability instead
        of misreporting 401s as 'restricted' (the Phase 7 wart)."""
        return self._config.is_configured

    async def close(self) -> None:
        if self._external_client is not None:
            await self._external_client.aclose()
        if self._token_cache is not None:
            await self._token_cache.aclose()
        if self._creator_token_cache is not None:
            await self._creator_token_cache.aclose()

    def _http(self) -> SafeHttpClient:
        hosts = [_host_of(self._config.api_base)]
        if self._config.app_token_configured:
            hosts.append(_host_of(self._config.oauth_base))
        return SafeHttpClient(
            [h for h in hosts if h],
            timeout=self._timeout,
            max_redirects=self._max_redirects,
            resolver=self._resolver,
            client=self._external_client,
        )

    async def _auth_headers(self) -> dict[str, str]:
        if not self._config.is_configured:
            return {}
        if self._config.app_token_configured:
            if self._token_cache is None:
                # An injected client (tests/DI) is REUSED for the token
                # exchange too — never a surprise second network path.
                token_http = SafeHttpClient(
                    [_host_of(self._config.oauth_base)],
                    timeout=self._timeout, max_redirects=0,
                    resolver=self._resolver,
                    client=self._external_client,
                )
                self._token_cache = _AppTokenCache(self._config, token_http)
            token = await self._token_cache.token()
            return {"Client-Id": self._config.client_id or "",
                    "Authorization": f"Bearer {token}"}
        return {"Client-Id": self._config.client_id or ""}

    # -- URL handling (Phase 7 behavior, unchanged) --------------------------------

    def can_handle(self, url: str) -> bool:
        try:
            parsed = urlparse(url.strip())
        except ValueError:
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        return (parsed.hostname or "").lower() in _CLAIMED_HOSTS

    def _vod_id(self, url: str) -> str | None:
        path = urlparse(url.strip()).path
        match = _VOD_PATH_RE.search(path)
        if match:
            return match.group(1)
        return None

    def _clip_slug(self, url: str) -> str | None:
        """TASK 014: clip slug for ``/clips/{slug}``,
        ``/{channel}/clip/{slug}`` and ``clips.twitch.tv/{slug}`` URLs;
        None for every other path."""
        try:
            parsed = urlparse(url.strip())
        except ValueError:
            return None
        path = parsed.path
        if (parsed.hostname or "").lower() == "clips.twitch.tv":
            match = _CLIPS_SUBDOMAIN_RE.match(path)
            if match:
                return match.group(1)
            return None
        match = _CLIP_PATH_RE.match(path)
        if match:
            return match.group(1)
        return None

    def _is_clip_url(self, url: str) -> bool:
        # A VOD id wins over everything (guards against a channel that is
        # literally named "clips": /clips/videos/{id} is a VOD URL).
        if self._vod_id(url):
            return False
        return self._clip_slug(url) is not None

    def _channel(self, url: str) -> str | None:
        """Channel for live-style URLs; None when the URL is not a
        live/channel form."""
        path = urlparse(url.strip()).path
        match = _LIVE_PATH_RE.match(path) or _BARE_CHANNEL_RE.match(path)
        return match.group(1).lower() if match else None

    def _is_live_url(self, url: str) -> bool:
        path = urlparse(url.strip()).path
        if self._vod_id(url):
            return False  # a VOD id wins over everything
        return bool(_LIVE_PATH_RE.match(path) or _BARE_CHANNEL_RE.match(path))

    # -- validation / metadata -----------------------------------------------------

    async def validate(self, url: str) -> ProviderCheck:
        if not self.is_configured:
            # Honest: unconfigured deployments do not guess at Helix.
            return ProviderCheck(
                STATUS_UNAVAILABLE, self.name,
                reason="The Twitch source provider is not configured on this "
                       "deployment (no client id). Direct video file upload is "
                       "available.",
            )
        if self._is_live_url(url):
            return await self._check_live(url)
        if self._is_clip_url(url):
            return await self._check_clip(url, self._clip_slug(url))  # type: ignore[arg-type]
        vod_id = self._vod_id(url)
        if not vod_id:
            return ProviderCheck(
                STATUS_UNAVAILABLE, self.name,
                reason="Not a Twitch VOD or clip URL (use twitch.tv/videos/{id} "
                       "or twitch.tv/clips/{slug}).",
            )
        return await self._check_vod(url, vod_id)

    # -- creator authorization (TASK 014: clip materialization) -----------------

    async def _creator_headers(self) -> dict[str, str]:
        """The CREATOR's user-access-token headers (refresh_token grant,
        cached). Raises UnauthorizedError (permanent) on grant failure."""
        if not self._config.creator_token_configured:
            raise UnauthorizedError(
                "Twitch clip import is not authorized on this deployment: "
                "configure WORKER_TWITCH_OAUTH_REFRESH_TOKEN (the clip "
                "owner's refresh token, scope channel:manage:clips) together "
                "with WORKER_TWITCH_CLIENT_ID and WORKER_TWITCH_CLIENT_SECRET "
                "— or upload the video file directly."
            )
        if self._creator_token_cache is None:
            token_http = SafeHttpClient(
                [_host_of(self._config.oauth_base)],
                timeout=self._timeout, max_redirects=0,
                resolver=self._resolver,
                client=self._external_client,
            )
            self._creator_token_cache = _CreatorTokenCache(self._config, token_http)
        token = await self._creator_token_cache.token()
        return {"Client-Id": self._config.client_id or "",
                "Authorization": f"Bearer {token}"}

    async def _creator_user_id(self, headers: dict[str, str]) -> str:
        """The user id behind the creator token (official ``GET /users``
        with no parameters) — needed as ``editor_id`` for Get Clips
        Download. Cached for the provider's lifetime."""
        if self._creator_user_id_cache is not None:
            return self._creator_user_id_cache
        http = self._http()
        try:
            try:
                response = await http.request(
                    "GET", f"{self._config.api_base}/users", headers=headers,
                )
            except Exception as exc:  # noqa: BLE001 — guard/transport boundary
                raise _guard_to_provider_error(exc) from None
        finally:
            await http.aclose()
        if response.status_code != 200:
            raise UnauthorizedError(
                "Twitch could not identify the configured creator account "
                f"(provider status {response.status_code}) — check the "
                "creator OAuth authorization on this deployment.",
                details={"status": response.status_code},
            )
        try:
            data = response.json().get("data") or []
        except ValueError:
            raise MetadataUnavailableError("Unexpected Twitch response.") from None
        if not data or not isinstance(data[0].get("id"), (str, int)):
            raise MetadataUnavailableError("Unexpected Twitch response.")
        self._creator_user_id_cache = str(data[0]["id"])
        return self._creator_user_id_cache

    async def _fetch_clip(self, slug: str, headers: dict[str, str]) -> dict[str, Any] | None:
        """Official ``GET /clips?id={slug}`` — the clip's metadata, or
        None when Twitch does not report it (deleted/removed)."""
        http = self._http()
        try:
            try:
                response = await http.request(
                    "GET", f"{self._config.api_base}/clips",
                    params={"id": slug}, headers=headers,
                )
            except Exception as exc:  # noqa: BLE001 — guard/transport boundary
                raise _guard_to_provider_error(exc) from None
        finally:
            await http.aclose()
        if response.status_code != 200:
            if response.status_code == 429:
                raise RateLimitedError("Twitch rate limit — retry shortly.")
            if response.status_code in (401, 403):
                raise SourceRestrictedError("This clip is private or restricted.")
            raise MetadataUnavailableError("Twitch could not be checked right now.")
        try:
            data = response.json().get("data") or []
        except ValueError:
            raise MetadataUnavailableError("Unexpected Twitch response.") from None
        for clip in data:
            if isinstance(clip, dict) and str(clip.get("id")) == slug:
                return clip
        return None

    async def _check_clip(self, url: str, slug: str) -> ProviderCheck:
        """Official metadata for a clip URL (TASK 014).

        ``GET /clips`` requires a real token (user or app) — a
        client-id-only deployment cannot even read clip metadata, and
        that is reported honestly instead of misreading a 401.
        """
        if self._config.creator_token_configured:
            headers = await self._creator_headers()
        elif self._config.app_token_configured:
            headers = await self._auth_headers()
        else:
            return ProviderCheck(
                STATUS_UNAVAILABLE, self.name,
                reason="Twitch clip metadata needs an authorized token; this "
                       "deployment runs VOD-metadata mode (client id only).",
            )
        # Typed errors (restricted / rate-limited / metadata) propagate to
        # the ingestion categorizer; only an absent clip is a deleted state.
        clip = await self._fetch_clip(slug, headers)
        if clip is None:
            return ProviderCheck(STATUS_DELETED, self.name,
                                 reason="This clip is not available (deleted or removed).")
        metadata = NormalizedSource(
            provider=self.name,
            canonical_url=f"https://clips.twitch.tv/{slug}",
            provider_id=slug,
            title=_as_str(clip.get("title")),
            duration_seconds=_seconds(clip.get("duration")),
            published_at=_as_str(clip.get("created_at")),
            creator_name=_as_str(clip.get("broadcaster_name")),
            state="completed",
            provider_specific={
                "broadcaster_id": _as_str(clip.get("broadcaster_id")),
                "video_id": _as_str(clip.get("video_id")),
                "clip_source": "helix_get_clips",
            },
        )
        return ProviderCheck(STATUS_AVAILABLE, self.name,
                             title=metadata.title,
                             duration_seconds=metadata.duration_seconds,
                             metadata=metadata)

    async def _check_vod(self, url: str, vod_id: str) -> ProviderCheck:
        # Token exchange (if configured) runs BEFORE the metadata client
        # exists — an early return must not leak a client.
        headers = await self._auth_headers()
        http = self._http()
        try:
            try:
                response = await http.request(
                    "GET", f"{self._config.api_base}/videos",
                    params={"id": vod_id, "first": 1}, headers=headers,
                )
            except Exception as exc:  # noqa: BLE001 — guard/transport boundary
                return _check_from_error(self.name, exc)
        finally:
            # aclose() is a no-op for injected (externally owned) clients.
            await http.aclose()
        if response.status_code == 200:
            try:
                data = response.json().get("data") or []
            except ValueError:
                return ProviderCheck(STATUS_UNAVAILABLE, self.name,
                                     reason="Unexpected Twitch response.")
            if data and str(data[0].get("id")) == vod_id:
                vod = data[0]
                metadata = NormalizedSource(
                    provider=self.name,
                    canonical_url=f"https://www.twitch.tv/videos/{vod_id}",
                    provider_id=vod_id,
                    title=_as_str(vod.get("title")),
                    duration_seconds=_seconds(vod.get("duration")),
                    published_at=_as_str(vod.get("created_at")),
                    creator_name=_as_str(vod.get("user_name")),
                    thumbnail_url=_as_str(vod.get("thumbnail_url")),
                    state="completed",
                    provider_specific={"vod_type": _as_str(vod.get("type"))},
                )
                return ProviderCheck(STATUS_AVAILABLE, self.name,
                                     title=metadata.title,
                                     duration_seconds=metadata.duration_seconds,
                                     metadata=metadata)
            return ProviderCheck(STATUS_DELETED, self.name,
                                 reason="This VOD is not available (deleted or removed).")
        if response.status_code in (401, 403):
            return ProviderCheck(STATUS_RESTRICTED, self.name,
                                 reason="This VOD is private or restricted.")
        if response.status_code == 429:
            return ProviderCheck(STATUS_RATE_LIMITED, self.name,
                                 reason="Twitch rate limit — retry shortly.")
        return ProviderCheck(STATUS_UNAVAILABLE, self.name,
                             reason="Twitch could not be checked right now.")

    async def _check_live(self, url: str) -> ProviderCheck:
        channel = self._channel(url)
        if not channel:
            return ProviderCheck(STATUS_UNAVAILABLE, self.name,
                                  reason="Not a recognizable Twitch stream URL.")
        headers = await self._auth_headers()
        http = self._http()
        try:
            try:
                response = await http.request(
                    "GET", f"{self._config.api_base}/streams",
                    params={"user_login": channel, "first": 1}, headers=headers,
                )
            except Exception as exc:  # noqa: BLE001
                return _check_from_error(self.name, exc)
        finally:
            await http.aclose()
        if response.status_code == 200:
            try:
                data = response.json().get("data") or []
            except ValueError:
                return ProviderCheck(STATUS_UNAVAILABLE, self.name,
                                     reason="Unexpected Twitch response.")
            if data:
                stream = data[0]
                metadata = NormalizedSource(
                    provider=self.name,
                    canonical_url=f"https://www.twitch.tv/{channel}",
                    provider_id=None,
                    title=_as_str(stream.get("title")),
                    creator_name=_as_str(stream.get("user_name")),
                    state="live",
                    provider_specific={"live": True},
                )
                return ProviderCheck(STATUS_RESTRICTED, self.name,
                                     title=metadata.title,
                                     reason="This is a live stream, not a completed recording — wait for the VOD.",
                                     metadata=metadata)
            return ProviderCheck(STATUS_UNAVAILABLE, self.name,
                                 reason="That channel is offline; no VOD at that URL.")
        if response.status_code == 429:
            return ProviderCheck(STATUS_RATE_LIMITED, self.name,
                                 reason="Twitch rate limit — retry shortly.")
        if response.status_code in (401, 403):
            return ProviderCheck(STATUS_UNAVAILABLE, self.name,
                                 reason=redact("Twitch authorization failed — check the provider credentials."))
        return ProviderCheck(STATUS_UNAVAILABLE, self.name,
                             reason="Twitch could not be checked right now.")

    async def materialize(self, url: str, destination_dir: Any, **kwargs: Any) -> Any:
        """TASK 014 — per-URL-class, honest materialization.

        - CLIP URL: the official Get Clips Download flow under the
          creator's authorization (module docstring). The returned CDN
          URL is https-only, must match the configured Twitch media host
          suffixes, and is fetched through the full SSRF guard, then
          FFprobe-verified BEFORE storage.
        - VOD / live / other: NO authorized media endpoint exists — a
          clear, actionable refusal (never a workaround).
        """
        if not self._is_clip_url(url):
            raise MaterializationUnavailable(
                "Twitch VOD materialization is not available: the official Twitch "
                "APIs expose no authorized endpoint for downloading VOD media to a "
                "third-party application (Twitch CLIP URLs are supported with "
                "creator authorization). Export the VOD from the Twitch dashboard, "
                "or upload the file directly."
            )
        slug = self._clip_slug(url)
        progress = kwargs.get("progress")
        should_cancel = kwargs.get("should_cancel")
        destination = Path(destination_dir) / "source.mp4"

        # 1) Creator authorization (refresh_token grant, cached). Raises
        #    UnauthorizedError (permanent, actionable) when not configured.
        headers = await self._creator_headers()
        editor_id = await self._creator_user_id(headers)

        # 2) Clip metadata (broadcaster id for the download request).
        clip = await self._fetch_clip(slug, headers)
        if clip is None:
            raise SourceExpiredError("This clip is not available (deleted or removed).")
        broadcaster_id = _as_str(clip.get("broadcaster_id"))
        if not broadcaster_id:
            raise MetadataUnavailableError("Twitch did not report the clip's broadcaster.")

        # 3) Official Get Clips Download — the ONLY authorized media
        #    endpoint (serves CDN URLs to the broadcaster/editor).
        http = self._http()
        try:
            try:
                response = await http.request(
                    "GET", f"{self._config.api_base}/clips/downloads",
                    params={"editor_id": editor_id,
                            "broadcaster_id": broadcaster_id,
                            "clip_id": slug},
                    headers=headers,
                )
            except Exception as exc:  # noqa: BLE001 — guard/transport boundary
                raise _guard_to_provider_error(exc) from None
        finally:
            await http.aclose()
        if response.status_code == 404:
            raise SourceExpiredError("This clip is not available (deleted or removed).")
        if response.status_code == 400:
            # Official 400 cause: the editor_id does not match the user id
            # in the access token — a deployment misconfiguration (a retry
            # cannot fix it).
            raise UnauthorizedError(
                "Twitch rejected the clip-download request (the configured "
                "creator token does not match the requested editor) — check "
                "the creator OAuth authorization on this deployment."
            )
        if response.status_code == 429:
            raise RateLimitedError("Twitch rate limit — retry shortly.")
        if response.status_code in (401, 403):
            raise SourceRestrictedError(
                "Twitch refused the clip download: the configured creator "
                "account is not the clip's broadcaster or an authorized "
                "editor for it (official Get Clips Download authorization)."
            )
        if response.status_code != 200:
            raise MetadataUnavailableError("Twitch could not provide the clip download.")
        try:
            data = response.json().get("data") or []
        except ValueError:
            raise MetadataUnavailableError("Unexpected Twitch response.") from None
        entry = next(
            (d for d in data
             if isinstance(d, dict) and str(d.get("clip_id")) == slug),
            None,
        )
        if entry is None:
            raise MetadataUnavailableError("Twitch did not return a download entry for this clip.")
        download_url = (_as_str(entry.get("landscape_download_url"))
                        or _as_str(entry.get("portrait_download_url")))
        if not download_url:
            raise MaterializationUnavailable(
                "Twitch did not provide a download URL for this clip — the "
                "clip's media is no longer available for download."
            )

        # 4) Guarded fetch: https + configured media host suffix, then the
        #    full public-IP SSRF guard on the transfer itself.
        media_host = _clip_media_host(download_url, self._config.media_host_suffixes)
        media_http = SafeHttpClient(
            [media_host],
            timeout=self._config.transfer_timeout_seconds,
            max_redirects=self._config.max_redirects,
            resolver=self._resolver,
            client=self._external_client,
        )
        try:
            final_url = await media_http.resolve_redirect_chain(download_url)
            await media_http.stream_to_file(
                final_url,
                destination,
                max_bytes=self._config.max_source_bytes,
                chunk_size=self._config.chunk_bytes,
                progress=progress,
                should_cancel=should_cancel,
                max_attempts=self._config.max_attempts,
                backoff_seconds=self._config.backoff_seconds,
                deadline_seconds=self._config.transfer_deadline_seconds,
            )
        finally:
            await media_http.aclose()
        if not destination.is_file() or destination.stat().st_size == 0:
            destination.unlink(missing_ok=True)
            raise TransferInterruptedError("provider transfer produced an empty file")
        # 5) Validate the ACTUAL bytes (never the URL) before storage —
        #    rejected bytes are removed (safe cleanup, no garbage left).
        try:
            await probe_media(self._ffprobe, destination,
                              self._config.ffprobe_timeout_seconds)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return destination


# -- helpers ---------------------------------------------------------------------

def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _as_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _seconds(value: Any) -> float | None:
    """Twitch durations are numeric (seconds) strings or numbers; unknown
    shapes stay None (never guessed)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    return number if number > 0 else None


def _check_from_error(provider: str, exc: Exception) -> ProviderCheck:
    message = redact(str(exc))
    if "rate limit" in message or "status 429" in message:
        return ProviderCheck(STATUS_RATE_LIMITED, provider,
                             reason=f"{provider} rate limit — retry shortly.")
    return ProviderCheck(STATUS_UNAVAILABLE, provider,
                         reason=f"Could not reach {provider} right now; try again later.")


def _guard_to_provider_error(exc: Exception) -> ProviderError:
    """Maps a guard/transport failure from a metadata or download call to
    a typed provider error (raw exceptions never reach the queue layer)."""
    if isinstance(exc, ProviderError):
        return exc
    return MetadataUnavailableError(
        "Twitch could not be checked right now.",
        details={"error": exc.__class__.__name__},
    )


def _clip_media_host(download_url: str,
                     suffixes: tuple[str, ...]) -> str:
    """Validates an official CDN download URL (https + a configured
    Twitch media host suffix) and returns its normalized host for the
    fetch allowlist. Raises InvalidUrlError on any violation."""
    try:
        parts = urlparse(download_url)
    except ValueError:
        raise InvalidUrlError("malformed clip download URL") from None
    if parts.scheme != "https":
        raise InvalidUrlError("clip download URL is not https")
    if parts.username is not None or parts.password is not None:
        raise InvalidUrlError("credentials in URLs are not permitted")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise InvalidUrlError("clip download URL has no host")
    normalized = tuple(s.lower().rstrip(".") for s in suffixes if s)
    if not any(host == s or host.endswith("." + s) for s in normalized):
        raise InvalidUrlError("clip download host is not a permitted Twitch media host")
    return host
