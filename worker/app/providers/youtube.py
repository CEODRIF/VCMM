"""YouTube adapter (Phase 7 foundation, TASK 011 — authorized metadata).

**Official, policy-compliant capabilities (exactly what this adapter
implements — nothing else is claimed):**

METADATA (authorized, official endpoints):
- KEYLESS (default, no credentials): the official YouTube oEmbed
  endpoint — reports title, creator, thumbnail and the page type
  (``video`` vs ``live``). Duration/state are NOT reported by oEmbed,
  so they stay ``None``/``unknown`` in the normalized metadata (never
  guessed).
- CONFIGURED (operator-set ``WORKER_YT_API_KEY`` or OAuth user
  credentials): the official YouTube Data API v3 ``videos.list`` —
  duration (ISO-8601), publication time, channel, thumbnail, definition
  (quality class), live-stream state (live / upcoming), privacy status
  (private → restricted), upload status (rejected → restricted,
  processing → unavailable). User OAuth additionally lets the USER's
  own authorized context see unlisted/private videos in metadata.

MATERIALIZATION (download of the media): **NOT SUPPORTED** — neither
platform exposes an official API for downloading video media of a
channel/creator's public content. Per the security boundary (TASK 011
spec section 7), this adapter EXPOSES that as unsupported
(:class:`MaterializationUnavailable`) instead of implementing any
workaround. No yt-dlp, no browser cookies, no DRM/geo bypass — ever.

URL claims (host-allowlisted, scheme-restricted, SSRF-guarded):
``youtube.com``/``youtu.be`` — ``/watch?v=``, ``/shorts/``, ``/live/``,
``/embed/``, ``youtu.be/{id}``. The bare ``/{id}`` form is NOT claimed
(ambiguous with channel pages).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

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

logger = logging.getLogger("clipforge.providers.youtube")

OEMBED_URL = "https://www.youtube.com/oembed"
DATA_API_PATH = "/youtube/v3/videos"
HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}

#: Note: the bare ``/{videoId}`` path form is intentionally NOT claimed — it
#: is ambiguous with channel pages. ``/watch?v=``, ``/shorts/``, ``/live/``,
#: ``/embed/`` and ``youtu.be/{id}`` are the accepted shapes.
_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,15}")
_PATH_ID_RE = re.compile(r"/(shorts|embed|live|v)/([A-Za-z0-9_-]{6,})")


@dataclass(frozen=True)
class YouTubeConfig:
    """Operator-provided YouTube authorization (server-side only).

    Nothing here is required: with an EMPTY config the adapter still
    works for metadata via the keyless oEmbed endpoint (partial, honest).
    """

    api_key: str | None = None
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None
    oauth_refresh_token: str | None = None
    oauth_scopes: str = "openid"
    api_base: str = "https://www.googleapis.com"
    token_endpoint: str = "https://oauth2.googleapis.com/token"

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    @property
    def has_oauth(self) -> bool:
        return bool(self.oauth_client_id and self.oauth_client_secret
                    and self.oauth_refresh_token)

    @property
    def data_api_configured(self) -> bool:
        return self.has_api_key or self.has_oauth


class _OAuthTokenCache:
    """Server-side refresh-token exchange with caching + single-flight.

    The refresh token and client secret travel ONLY in the POST body of
    the token exchange — never in URLs, never in logs (all error paths
    go through :func:`redact`).
    """

    def __init__(self, config: YouTubeConfig, token_http: SafeHttpClient) -> None:
        self._config = config
        self._http = token_http
        self._lock = asyncio.Lock()
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    async def access_token(self) -> str:
        if self._access_token is not None and time.time() < self._expires_at - 60:
            return self._access_token
        async with self._lock:
            if self._access_token is not None and time.time() < self._expires_at - 60:
                return self._access_token
            return await self._refresh()

    async def _refresh(self) -> str:
        response = await self._http.request(
            "POST", self._config.token_endpoint,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._config.oauth_refresh_token or "",
                "client_id": self._config.oauth_client_id or "",
                "client_secret": self._config.oauth_client_secret or "",
            },
        )
        if response.status_code != 200:
            # The provider's error body is NOT echoed (it may contain
            # request context) — only a stable, redacted explanation.
            raise UnauthorizedError(
                "YouTube OAuth token refresh failed "
                f"(provider status {response.status_code}) — check the provider "
                "credentials on this deployment.",
                details={"status": response.status_code},
            )
        try:
            payload = response.json()
        except ValueError:
            raise UnauthorizedError(
                "YouTube OAuth token endpoint returned an unexpected response."
            ) from None
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise UnauthorizedError(
                "YouTube OAuth token endpoint returned no access token."
            )
        try:
            lifetime = int(payload.get("expires_in", 3600))
        except (TypeError, ValueError):
            lifetime = 3600
        self._access_token = token
        self._expires_at = time.time() + max(60, lifetime)
        return token

    async def aclose(self) -> None:
        await self._http.aclose()


class YouTubeProvider:
    name = "youtube"
    supports_validation = True
    supports_materialization = False  # no official media-download API (honest)

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        *,
        config: YouTubeConfig | None = None,
        resolver: Any = None,
        max_redirects: int = 3,
    ) -> None:
        self._config = config or YouTubeConfig()
        self._external_client = http_client
        self._timeout = timeout
        self._resolver = resolver
        self._max_redirects = max_redirects
        self._token_cache: _OAuthTokenCache | None = None

    # -- capabilities / configuration -------------------------------------------

    @property
    def data_api_configured(self) -> bool:
        """True when metadata can come from the Data API (richer);
        otherwise the keyless oEmbed path is used (partial, honest)."""
        return self._config.data_api_configured

    async def close(self) -> None:
        if self._external_client is not None:
            await self._external_client.aclose()
        if self._token_cache is not None:
            await self._token_cache.aclose()

    def _metadata_http(self) -> SafeHttpClient:
        """Safe client for the endpoint host(s) of the active path."""
        if self.data_api_configured:
            allowed = [h for h in (_host_of(self._config.api_base),
                                   _host_of(self._config.token_endpoint)) if h]
        else:
            allowed = list(HOSTS)
        return SafeHttpClient(
            allowed,
            timeout=self._timeout,
            max_redirects=self._max_redirects,
            resolver=self._resolver,
            client=self._external_client,
        )

    # -- URL handling (Phase 7 behavior, unchanged) --------------------------------

    def can_handle(self, url: str) -> bool:
        try:
            parsed = urlparse(url.strip())
        except ValueError:
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname or ""
        return host in HOSTS or host.endswith(".youtube.com")

    def _video_id(self, url: str) -> str | None:
        parsed = urlparse(url.strip())
        host = parsed.hostname or ""
        if host in ("youtu.be",):
            vid = parsed.path.lstrip("/").split("/")[0]
            return vid or None
        query = parse_qs(parsed.query)
        if "v" in query and query["v"][0]:
            return query["v"][0]
        match = _PATH_ID_RE.search(parsed.path)
        if match:
            return match.group(2)
        return None

    def _canonical_url(self, url: str, vid: str) -> str:
        return f"https://www.youtube.com/watch?v={vid}"

    # -- validation / metadata -----------------------------------------------------

    async def validate(self, url: str) -> ProviderCheck:
        vid = self._video_id(url)
        if not vid or not _VIDEO_ID_RE.fullmatch(vid):
            return ProviderCheck(STATUS_UNAVAILABLE, self.name,
                                 reason="Not a YouTube video or VOD URL.")
        if self.data_api_configured:
            return await self._validate_data_api(url, vid)
        return await self._validate_oembed(url, vid)

    async def _validate_oembed(self, url: str, vid: str) -> ProviderCheck:
        """Keyless official oEmbed — partial metadata (no duration/state
        from this endpoint; those stay unknown, never guessed)."""
        http = self._metadata_http()
        try:
            try:
                response = await http.get(OEMBED_URL, params={"url": url, "format": "json"})
            except Exception as exc:  # noqa: BLE001 — guard/transport boundary
                return _check_from_error(self.name, exc)
        finally:
            # aclose() is a no-op for injected (externally owned) clients.
            await http.aclose()
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                return ProviderCheck(STATUS_UNAVAILABLE, self.name,
                                     reason="Unexpected YouTube response.")
            title = payload.get("title")
            is_live = str(payload.get("type", "")).lower() == "live"
            metadata = NormalizedSource(
                provider=self.name,
                canonical_url=self._canonical_url(url, vid),
                provider_id=vid,
                title=_as_str(title),
                creator_name=_as_str(payload.get("author_name")),
                thumbnail_url=_as_str(payload.get("thumbnail_url")),
                state="live" if is_live else "unknown",
                provider_specific={"metadata_source": "oembed_keyless"},
            )
            if is_live:
                return ProviderCheck(
                    STATUS_RESTRICTED, self.name, title=_as_str(title),
                    reason="This is a live page, not a completed recording — wait for the VOD.",
                    metadata=metadata,
                )
            return ProviderCheck(STATUS_AVAILABLE, self.name, title=_as_str(title),
                                 metadata=metadata)
        if response.status_code == 404:
            return ProviderCheck(STATUS_DELETED, self.name,
                                 reason="This recording is not available (deleted, private or age-restricted).")
        if response.status_code == 429:
            return ProviderCheck(STATUS_RATE_LIMITED, self.name,
                                 reason="YouTube rate limit — retry shortly.")
        return ProviderCheck(STATUS_UNAVAILABLE, self.name,
                             reason="YouTube could not be checked right now.")

    async def _validate_data_api(self, url: str, vid: str) -> ProviderCheck:
        params = {"part": "snippet,contentDetails,liveStreamingDetails", "id": vid}
        headers: dict[str, str] = {}
        if self._config.has_api_key:
            # Official header form (X-Goog-Api-Key): the key NEVER appears
            # in the URL — so it cannot leak into exception strings/logs.
            headers["X-Goog-Api-Key"] = self._config.api_key
        if not self._config.has_api_key:
            # Token exchange uses its own client; do it BEFORE the
            # metadata client exists (an early return must not leak it).
            # A credential failure PROPAGATES (UnauthorizedError): it is a
            # deployment/operator problem that a retry cannot fix — the
            # ingestion layer maps it to the PERMANENT
            # provider_unauthorized category with a safe message.
            token = await self._token()
            headers = {"Authorization": f"Bearer {token}"}
        http = self._metadata_http()
        try:
            try:
                response = await http.request(
                    "GET", f"{self._config.api_base}{DATA_API_PATH}",
                    params=params, headers=headers,
                )
            except Exception as exc:  # noqa: BLE001
                return _check_from_error(self.name, exc)
        finally:
            await http.aclose()
        if response.status_code == 200:
            try:
                items = (response.json().get("items") or [])
            except ValueError:
                return ProviderCheck(STATUS_UNAVAILABLE, self.name,
                                     reason="Unexpected YouTube response.")
            if not items:
                return ProviderCheck(STATUS_DELETED, self.name,
                                     reason="This recording is not available (deleted, private or age-restricted).")
            metadata = self._metadata_from_item(items[0], vid, url)
            status = _state_to_check_status(metadata.state)
            if status == STATUS_AVAILABLE:
                return ProviderCheck(STATUS_AVAILABLE, self.name,
                                     title=metadata.title,
                                     duration_seconds=metadata.duration_seconds,
                                     metadata=metadata)
            return ProviderCheck(status, self.name, title=metadata.title,
                                 reason=_state_reason(metadata.state),
                                 metadata=metadata)
        if response.status_code == 404:
            return ProviderCheck(STATUS_DELETED, self.name,
                                 reason="This recording is not available (deleted, private or age-restricted).")
        if response.status_code == 403:
            return ProviderCheck(STATUS_RESTRICTED, self.name,
                                 reason="This recording is restricted (private, age or region restriction).")
        if response.status_code == 429:
            return ProviderCheck(STATUS_RATE_LIMITED, self.name,
                                 reason="YouTube rate limit — retry shortly.")
        return ProviderCheck(STATUS_UNAVAILABLE, self.name,
                             reason="YouTube could not be checked right now.")

    async def _token(self) -> str:
        if self._token_cache is None:
            # An injected client (tests/DI) is REUSED for the token
            # exchange too — never a surprise second network path.
            token_http = SafeHttpClient(
                [_host_of(self._config.token_endpoint)],
                timeout=self._timeout,
                max_redirects=0,
                resolver=self._resolver,
                client=self._external_client,
            )
            self._token_cache = _OAuthTokenCache(self._config, token_http)
        return await self._token_cache.access_token()

    def _metadata_from_item(self, item: dict[str, Any], vid: str, url: str) -> NormalizedSource:
        snippet = item.get("snippet") or {}
        details = item.get("contentDetails") or {}
        live = item.get("liveStreamingDetails") or {}
        upload_status = str(item.get("status", {}).get("uploadStatus", "")).lower()
        privacy = str(snippet.get("privacyStatus", "")).lower()

        # State classification (conservative): live > scheduled >
        # rejected/processing > private > completed.
        live_state = str(live.get("actualState") or "").lower()
        if live_state in ("livestreamlive", "live"):
            state: str = "live"
        elif live_state in ("livestreamupcoming", "upcoming"):
            state = "scheduled"
        elif upload_status in ("rejected",):
            state = "restricted"
        elif upload_status in ("failed", "processing"):
            state = "unavailable"
        elif privacy == "private":
            state = "restricted"
        else:
            state = "completed"

        return NormalizedSource(
            provider=self.name,
            canonical_url=self._canonical_url(url, vid),
            provider_id=vid,
            title=_as_str(snippet.get("title")),
            duration_seconds=_parse_iso8601_duration(details.get("duration")),
            published_at=_as_str(snippet.get("publishedAt")),
            creator_name=_as_str(snippet.get("channelTitle")),
            thumbnail_url=_thumbnail_url(snippet.get("thumbnails")),
            quality=_as_str(details.get("definition")),
            state=state,  # type: ignore[arg-type]
            provider_specific={
                "privacy_status": privacy or None,
                "upload_status": upload_status or None,
                "metadata_source": "youtube_data_api_v3",
            },
        )

    async def materialize(self, url: str, destination_dir: Any, **kwargs: Any) -> Any:
        # Honest refusal: NO official YouTube API provides media bytes for
        # a public video. This deployment therefore cannot materialize
        # YouTube sources — upload the file directly (or configure an
        # explicitly authorized ingestion service).
        raise MaterializationUnavailable(
            "YouTube source materialization is not available: YouTube exposes no "
            "official API for downloading video media. Metadata works, but the "
            "media cannot be retrieved through an authorized mechanism on this "
            "deployment. Upload the file directly instead."
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


def _parse_iso8601_duration(value: Any) -> float | None:
    """Parses ISO-8601 durations like ``PT2H30M12S`` (YouTube's format).
    Unknown/missing → None (never guessed)."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?",
                         value.strip())
    if not match:
        return None
    days, hours, minutes, seconds = (float(g or 0) for g in match.groups())
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    return total if total > 0 else None


def _thumbnail_url(thumbnails: Any) -> str | None:
    if not isinstance(thumbnails, dict):
        return None
    for tier in ("medium", "high", "default"):
        info = thumbnails.get(tier)
        if isinstance(info, dict):
            url = _as_str(info.get("url"))
            if url:
                return url
    return None


def _state_to_check_status(state: str) -> str:
    if state == "completed":
        return STATUS_AVAILABLE
    if state in ("live", "scheduled", "restricted"):
        return STATUS_RESTRICTED
    return STATUS_UNAVAILABLE


def _state_reason(state: str) -> str:
    return {
        "live": "This is a live stream, not a completed recording — wait for the VOD.",
        "scheduled": "This stream is scheduled but has not started yet.",
        "restricted": "This recording is restricted (private, age or region restriction).",
        "unavailable": "This recording is not available (deleted, private or age-restricted).",
    }.get(state, "This recording could not be checked right now.")


def _check_from_error(provider: str, exc: Exception) -> ProviderCheck:
    """Maps guard/transport errors to the safe ProviderCheck vocabulary."""
    message = redact(str(exc))
    if "rate limit" in message or "status 429" in message:
        return ProviderCheck(STATUS_RATE_LIMITED, provider,
                             reason=f"{provider} rate limit — retry shortly.")
    return ProviderCheck(STATUS_UNAVAILABLE, provider,
                         reason=f"Could not reach {provider} right now; try again later.")
