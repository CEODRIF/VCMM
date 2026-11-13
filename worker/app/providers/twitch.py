"""Twitch adapter (TASK 007) — VALIDATION ONLY.

Supports COMPLETED VODs (``twitch.tv/videos/{id}``,
``twitch.tv/{channel}/videos/{id}`). Live URLs are rejected explicitly:
only completed recordings are analyzable.

Availability uses the KEYLESS Helix ``GET /videos`` listing (public VODs,
no auth required for public content, no download, no DRM interaction):

- 200 + the VOD id present → available (title from the payload);
- 200 + id absent          → deleted (the listing only returns live VODs);
- 403/401                   → restricted/private;
- 429                       → rate_limited (Twitch publishes Retry-After —
                              we surface the safe status, never retry-loop);
- other 4xx/5xx             → unavailable.

``supports_materialization`` is **False** (TTS/official API + user
authorization required; not shipped). No yt-dlp, no DRM bypass.
"""

from __future__ import annotations

import logging
import re
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

logger = logging.getLogger("clipforge.providers.twitch")

HELIX_VIDEOS_URL = "https://api.twitch.tv/helix/videos"
HOSTS = {"twitch.tv", "www.twitch.tv", "go.twitch.tv", "m.twitch.tv"}

#: twitch.tv/videos/{id} and twitch.tv/{channel}/videos/{id}
_VOD_PATH_RE = re.compile(r"/videos/([0-9]+)(?:/|$)")
#: twitch.tv/live/{channel} or a bare channel page
_LIVE_PATH_RE = re.compile(r"^/live/")
_BARE_CHANNEL_RE = re.compile(r"^/[^/]+/?$")


class TwitchProvider:
    name = "twitch"
    supports_validation = True
    supports_materialization = False  # authorized provider required (not shipped)

    def __init__(self, http_client: httpx.AsyncClient | None = None, timeout: float = 10.0) -> None:
        self._client = http_client
        self._owns_client = http_client is None
        self._timeout = timeout

    async def _request(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=True)
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def can_handle(self, url: str) -> bool:
        try:
            parsed = urlparse(url.strip())
        except ValueError:
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        return (parsed.hostname or "") in HOSTS

    def _vod_id(self, url: str) -> str | None:
        path = urlparse(url.strip()).path
        match = _VOD_PATH_RE.search(path)
        if match:
            return match.group(1)
        return None

    def _is_live_url(self, url: str) -> bool:
        path = urlparse(url.strip()).path
        if self._vod_id(url):
            return False  # a VOD id wins over everything
        return bool(_LIVE_PATH_RE.match(path) or _BARE_CHANNEL_RE.match(path))

    async def validate(self, url: str) -> ProviderCheck:
        if self._is_live_url(url):
            return ProviderCheck(STATUS_RESTRICTED, self.name,
                                 reason="This is a live stream, not a completed recording — wait for the VOD.")
        vod_id = self._vod_id(url)
        if not vod_id:
            return ProviderCheck(STATUS_UNAVAILABLE, self.name,
                                 reason="Not a Twitch VOD URL (use twitch.tv/videos/{id}).")
        client = await self._request()
        try:
            response = await client.get(HELIX_VIDEOS_URL, params={"id": vod_id, "first": 1})
        except httpx.HTTPError:
            return ProviderCheck(STATUS_UNAVAILABLE, self.name,
                                 reason="Could not reach Twitch right now; try again later.")
        if response.status_code == 200:
            try:
                data = response.json().get("data") or []
            except ValueError:
                return ProviderCheck(STATUS_UNAVAILABLE, self.name, reason="Unexpected Twitch response.")
            if data and str(data[0].get("id")) == vod_id:
                title = data[0].get("title")
                return ProviderCheck(STATUS_AVAILABLE, self.name, title=title)
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

    async def materialize(self, url: str, destination_dir: Any, **kwargs: Any) -> Any:
        # Honest refusal: no authorized download path is shipped.
        raise MaterializationUnavailable(
            "Twitch source materialization requires an explicitly authorized provider; "
            "this build only validates Twitch VOD URLs. Upload the file instead."
        )
