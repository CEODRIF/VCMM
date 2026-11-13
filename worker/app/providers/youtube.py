"""YouTube adapter (TASK 007) — VALIDATION ONLY.

Supports completed live streams (VODs) and regular uploads at
youtube.com / youtu.be. Availability is checked through the official
KEYLESS oEmbed endpoint (no API key, no auth, no download, no DRM
interaction):

- HTTP 200  → available (title from oEmbed; ``type: "live"`` marks a live
  page — a live stream is NOT a completed recording → restricted);
- HTTP 404  → deleted / private (oEmbed returns 404 for both — mapped to
  ``deleted`` with a safe reason);
- HTTP 429 / 5xx → ``rate_limited`` / ``unavailable`` (safe retry).

``supports_materialization`` is **False**: downloading YouTube source
material requires an explicitly authorized provider (the Phase 2
integration point). The adapter refuses to materialize rather than
guess — no yt-dlp, no DRM bypass, ever.
"""

from __future__ import annotations

import logging
import re
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

logger = logging.getLogger("clipforge.providers.youtube")

OEMBED_URL = "https://www.youtube.com/oembed"
HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}

# Note: the bare ``/{videoId}`` path form is intentionally NOT claimed — it
# is ambiguous with channel pages. ``/watch?v=``, ``/shorts/``, ``/live/``,
# ``/embed/`` and ``youtu.be/{id}`` are the accepted shapes.


class YouTubeProvider:
    name = "youtube"
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
        match = re.search(r"/(shorts|embed|live|v)/([A-Za-z0-9_-]{6,})", parsed.path)
        if match:
            return match.group(2)
        return None

    async def validate(self, url: str) -> ProviderCheck:
        vid = self._video_id(url)
        if not vid or not re.fullmatch(r"[A-Za-z0-9_-]{6,15}", vid):
            return ProviderCheck(STATUS_UNAVAILABLE, self.name, reason="Not a YouTube video or VOD URL.")
        client = await self._request()
        try:
            response = await client.get(OEMBED_URL, params={"url": url, "format": "json"})
        except httpx.HTTPError:
            return ProviderCheck(STATUS_UNAVAILABLE, self.name,
                                 reason="Could not reach YouTube right now; try again later.")
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                return ProviderCheck(STATUS_UNAVAILABLE, self.name, reason="Unexpected YouTube response.")
            title = payload.get("title")
            if str(payload.get("type", "")).lower() == "live":
                return ProviderCheck(
                    STATUS_RESTRICTED, self.name, title=title,
                    reason="This is a live page, not a completed recording — wait for the VOD.",
                )
            return ProviderCheck(STATUS_AVAILABLE, self.name, title=title)
        if response.status_code == 404:
            return ProviderCheck(STATUS_DELETED, self.name,
                                 reason="This recording is not available (deleted, private or age-restricted).")
        if response.status_code == 429:
            return ProviderCheck(STATUS_RATE_LIMITED, self.name,
                                 reason="YouTube rate limit — retry shortly.")
        return ProviderCheck(STATUS_UNAVAILABLE, self.name,
                             reason="YouTube could not be checked right now.")

    async def materialize(self, url: str, destination_dir: Any, **kwargs: Any) -> Any:
        # Honest refusal: no authorized download path is shipped.
        raise MaterializationUnavailable(
            "YouTube source materialization requires an explicitly authorized provider; "
            "this build only validates YouTube URLs. Upload the file instead."
        )
