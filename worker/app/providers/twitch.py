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

MATERIALIZATION (download of the media): **NOT SUPPORTED** — the official
Twitch APIs expose no endpoint that returns VOD media bytes to a
third-party application (historical "GQL download" paths are
undocumented/unapproved and are NOT used). Per the security boundary
(TASK 011 spec section 7), materialization is EXPOSED as unsupported
(:class:`MaterializationUnavailable`) — no unauthorized session cookies,
no private endpoints, no circumvention.

URL claims (host-allowlisted, scheme-restricted, SSRF-guarded):
``twitch.tv`` — ``/videos/{id}``, ``/{channel}/videos/{id}`` (VODs),
``/live/{channel}``, bare ``/{channel}`` (live check).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from .base import (
    STATUS_AVAILABLE,
    STATUS_DELETED,
    STATUS_RATE_LIMITED,
    STATUS_RESTRICTED,
    STATUS_UNAVAILABLE,
    MaterializationUnavailable,
    ProviderCheck,
)
from .errors import UnauthorizedError, redact
from .http_guard import SafeHttpClient
from .models import NormalizedSource

logger = logging.getLogger("clipforge.providers.twitch")

#: twitch.tv/videos/{id} and twitch.tv/{channel}/videos/{id}
_VOD_PATH_RE = re.compile(r"/videos/([0-9]+)(?:/|$)")
#: twitch.tv/live/{channel} or a bare channel page
_LIVE_PATH_RE = re.compile(r"^/live/([a-z0-9_]{1,25})(?:/|$)", re.IGNORECASE)
_BARE_CHANNEL_RE = re.compile(r"^/([a-z0-9_]{1,25})/?$", re.IGNORECASE)


@dataclass(frozen=True)
class TwitchConfig:
    """Operator-provided Twitch authorization (server-side only).

    Minimum official requirement for public VOD metadata: a Twitch
    application's CLIENT ID. A client SECRET enables the app-access-token
    flow (the recommended authenticated mode).
    """

    client_id: str | None = None
    client_secret: str | None = None
    api_base: str = "https://api.twitch.tv/helix"
    oauth_base: str = "https://id.twitch.tv"

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id)

    @property
    def app_token_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


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


class TwitchProvider:
    name = "twitch"
    supports_validation = True
    supports_materialization = False  # no official VOD media API (honest)

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        *,
        config: TwitchConfig | None = None,
        resolver: Any = None,
        max_redirects: int = 3,
    ) -> None:
        self._config = config or TwitchConfig()
        self._external_client = http_client
        self._timeout = timeout
        self._resolver = resolver
        self._max_redirects = max_redirects
        self._token_cache: _AppTokenCache | None = None

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
        return (parsed.hostname or "").lower() in {"twitch.tv", "www.twitch.tv",
                                                   "go.twitch.tv", "m.twitch.tv"}

    def _vod_id(self, url: str) -> str | None:
        path = urlparse(url.strip()).path
        match = _VOD_PATH_RE.search(path)
        if match:
            return match.group(1)
        return None

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
        vod_id = self._vod_id(url)
        if not vod_id:
            return ProviderCheck(STATUS_UNAVAILABLE, self.name,
                                 reason="Not a Twitch VOD URL (use twitch.tv/videos/{id}).")
        return await self._check_vod(url, vod_id)

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
        # Honest refusal: the official Twitch APIs do not expose VOD media
        # bytes to third-party apps. No unofficial "GQL download" paths,
        # no session cookies, no circumvention.
        raise MaterializationUnavailable(
            "Twitch source materialization is not available: the official Twitch "
            "APIs expose no authorized endpoint for downloading VOD media to a "
            "third-party application. Metadata works, but the media cannot be "
            "retrieved through an authorized mechanism on this deployment. "
            "Upload the file directly instead."
        )


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
