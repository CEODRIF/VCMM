"""Kick source adapter (TASK 015) — first-class Kick provider.

Honest capability contract:

- **METADATA**: the Kick public API (``https://api.kick.com/public/v1``)
  is the official read surface; it requires an API key sent as
  ``Authorization: APIKey <key>``. Without a configured key the adapter
  reports unavailability WITHOUT touching the network (never a fake
  success, never a guess).
- **MEDIA**: Kick exposes NO official VOD media-download endpoint (VODs
  also expire 14-30 days after the broadcast ends; the platform itself
  directs creators to export from Kick Studio). ``materialize`` therefore
  raises :class:`MaterializationUnavailable` with an actionable message
  — the same honesty class as YouTube and Twitch VOD.

The 8-method universal ingestion contract (``classify_url`` /
``validate_source`` / ``fetch_metadata`` / ``acquire_media`` /
``stream_or_download`` / ``report_progress`` / ``cleanup``) is inherited
from :class:`UniversalIngestionMixin`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from .base import (
    STATUS_AVAILABLE,
    STATUS_DELETED,
    STATUS_RATE_LIMITED,
    STATUS_UNAVAILABLE,
    MaterializationUnavailable,
    ProviderCheck,
    UniversalIngestionMixin,
)
from .http_guard import SafeHttpClient
from .models import NormalizedSource

logger = logging.getLogger("clipforge.providers.kick")

#: Kick hosts (dot-boundary set — never suffix matching).
HOSTS = frozenset({"kick.com", "www.kick.com", "m.kick.com"})

#: VOD URL: ``https://kick.com/{channel}/v/{id}`` (canonical form).
_VOD_RE = re.compile(r"^/([A-Za-z0-9_]{2,32})/v/(\d{1,15})$")
#: Live / profile URL: ``https://kick.com/{channel}`` (bare channel).
_CHANNEL_RE = re.compile(r"^/([A-Za-z0-9_]{2,32})$")

_NOT_CONFIGURED_REASON = (
    "Kick API key is not configured on this deployment "
    "(WORKER_KICK_API_KEY). Direct file upload is available."
)

KEY_REJECTED_REASON = (
    "Kick API key was rejected by the platform (401/403) — "
    "check WORKER_KICK_API_KEY."
)
NETWORK_REASON = "Kick API request failed (network/timeout) — retry later."
SERVER_ERROR_REASON = "Kick API returned a server error — retry later."


@dataclass(frozen=True)
class KickConfig:
    """Operator configuration (server-side only; the key is never
    logged, never sent to clients, never embedded in error messages)."""

    api_key: str | None = None
    #: Official public API base (overridable for tests/private mirrors).
    api_base: str = "https://api.kick.com/public/v1"


def _parse_kick_url(url: str) -> tuple[str, int | None] | None:
    """Parse a Kick URL by shape only (no network).

    Returns ``(channel, vod_id)`` for a VOD URL, ``(channel, None)`` for
    a bare-channel (live/profile) URL, or ``None`` when the URL is not a
    usable Kick URL (wrong scheme/host, category page, junk).
    """
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    if host not in HOSTS:
        return None
    path = parsed.path.rstrip("/") or "/"
    vod = _VOD_RE.match(path)
    if vod:
        return (vod.group(1), int(vod.group(2)))
    channel = _CHANNEL_RE.match(path)
    if channel:
        return (channel.group(1), None)
    return None


def _host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 and result == result else None


def _opt_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


class KickProvider(UniversalIngestionMixin):
    """Kick adapter: official public-API metadata + honest media refusal.

    Capability honesty (mirrors YouTube / Twitch VOD):

    - VOD URL  → availability + title/duration metadata via the official
      public API (``GET /channels/{channel}/videos``);
    - live / bare-channel URL → honest "live streams are not importable"
      with the actionable workaround (wait for the VOD, or upload);
    - materialization → ALWAYS refused: Kick exposes no official VOD
      media-download endpoint, and no scraping/DRM-bypass path may be
      added.
    """

    name = "kick"
    supports_validation = True
    supports_materialization = False  # no official media-download API (honest)

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        *,
        config: KickConfig | None = None,
        resolver: Any = None,
        max_redirects: int = 3,
    ) -> None:
        self._config = config or KickConfig()
        self._external_client = http_client
        self._timeout = timeout
        self._resolver = resolver
        self._max_redirects = max_redirects

    # -- capabilities / configuration -------------------------------------------

    @property
    def is_configured(self) -> bool:
        """Whether the official public API can be called at all."""
        return bool(self._config.api_key)

    async def close(self) -> None:
        if self._external_client is not None:
            await self._external_client.aclose()

    def _api_http(self) -> SafeHttpClient:
        """Safe client pinned to the API host; the key travels only in
        the Authorization header of allowlisted requests (SSRF guard
        applies to every request and redirect hop)."""
        return SafeHttpClient(
            [_host_of(self._config.api_base)],
            timeout=self._timeout,
            max_redirects=self._max_redirects,
            resolver=self._resolver,
            client=self._external_client,
            base_headers={"Authorization": f"APIKey {self._config.api_key}"},
        )

    # -- URL handling -------------------------------------------------------------

    def can_handle(self, url: str) -> bool:
        """Shape claim: kick.com hosts with a VOD or bare-channel path."""
        return _parse_kick_url(url) is not None

    # -- validation / metadata -----------------------------------------------------

    async def validate(self, url: str) -> ProviderCheck:
        parsed = _parse_kick_url(url)
        if parsed is None:
            return ProviderCheck(
                STATUS_UNAVAILABLE, self.name,
                reason="Not a Kick VOD or channel URL.",
            )
        channel, vod_id = parsed
        if not self.is_configured:
            # No network call at all: an unconfigured deployment reports
            # honest unavailability instead of a guaranteed-401 probe.
            return ProviderCheck(STATUS_UNAVAILABLE, self.name,
                                 reason=_NOT_CONFIGURED_REASON)
        if vod_id is None:
            return await self._validate_channel(channel)
        return await self._validate_vod(channel, vod_id)

    async def _api_get(self, path: str) -> tuple[int, Any]:
        """One allowlisted GET. Returns ``(status_code, payload)``;
        status 0 = network failure (no HTTP status at all)."""
        http = self._api_http()
        try:
            response = await http.get(f"{self._config.api_base.rstrip('/')}{path}")
        except httpx.TimeoutException:
            logger.warning("kick api timeout path=%s", path)
            return (0, None)
        except httpx.HTTPError as exc:
            logger.warning("kick api network error path=%s err=%s",
                           path, exc.__class__.__name__)
            return (0, None)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return (response.status_code, payload)


    async def _validate_vod(self, channel: str, vod_id: int) -> ProviderCheck:
        status, payload = await self._api_get(f"/channels/{channel}/videos")
        if status == 404:
            # The /videos endpoint 404s when the CHANNEL does not exist.
            return ProviderCheck(
                STATUS_DELETED, self.name,
                reason=f"Kick channel '{channel}' not found.",
            )
        check = _status_check(status, payload)
        if check is not None:
            return check
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return ProviderCheck(
                STATUS_UNAVAILABLE, self.name,
                reason="Kick API returned an unexpected response shape.",
            )
        video = next(
            (v for v in items if isinstance(v, dict) and _to_int(v.get("id")) == vod_id),
            None,
        )
        if video is None:
            return ProviderCheck(
                STATUS_DELETED, self.name,
                reason=(
                    f"Kick VOD {vod_id} not found on channel '{channel}'. "
                    "Kick removes VODs 14-30 days after the broadcast ends — "
                    "export the recording from Kick Studio or upload the "
                    "file directly instead."
                ),
            )
        metadata = NormalizedSource(
            provider=self.name,
            canonical_url=f"https://kick.com/{channel}/v/{vod_id}",
            provider_id=str(vod_id),
            title=_opt_str(video.get("title")) or None,
            duration_seconds=_to_float(video.get("duration")),
            published_at=_opt_str(video.get("created_at")) or None,
            creator_name=channel,
            state="completed",
        )
        return ProviderCheck(
            STATUS_AVAILABLE, self.name,
            title=metadata.title,
            duration_seconds=metadata.duration_seconds,
            metadata=metadata,
        )

    async def _validate_channel(self, channel: str) -> ProviderCheck:
        status, payload = await self._api_get(f"/channels/{channel}")
        if status == 404:
            return ProviderCheck(
                STATUS_DELETED, self.name,
                reason=f"Kick channel '{channel}' not found.",
            )
        check = _status_check(status, payload)
        if check is not None:
            return check
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not data:
            return ProviderCheck(
                STATUS_DELETED, self.name,
                reason=f"Kick channel '{channel}' not found.",
            )
        if bool(data.get("is_live")):
            return ProviderCheck(
                STATUS_UNAVAILABLE, self.name,
                title=_opt_str(data.get("name")) or channel,
                reason=(
                    f"'{channel}' is live right now — Kick live streams "
                    "cannot be imported (no official endpoint). Wait for "
                    f"the broadcast to be published as a VOD "
                    f"(kick.com/{channel}/v/<id>) and paste that URL, or "
                    "upload the recording directly."
                ),
            )
        return ProviderCheck(
            STATUS_UNAVAILABLE, self.name,
            title=_opt_str(data.get("name")) or channel,
            reason=(
                f"'{channel}' is not live right now — a channel page has "
                f"no video to import. Paste the VOD URL "
                f"(kick.com/{channel}/v/<id>) or upload the file directly."
            ),
        )

    # -- materialization (honest refusal) -------------------------------------------

    async def materialize(self, url: str, destination_dir: Any, **kwargs: Any) -> Any:
        """Kick has NO official VOD media-download endpoint — this path
        is the honesty gate, never a download attempt."""
        raise MaterializationUnavailable(
            "Kick exposes no official VOD media-download endpoint (and "
            "VODs expire 14-30 days after the broadcast ends). Export "
            "the recording from Kick Studio and upload the file directly "
            "— or paste a public link to the video file "
            "(.mp4/.webm/.mov)."
        )


def _status_check(status: int, payload: Any) -> ProviderCheck | None:
    """Shared status→ProviderCheck mapping; None = '2xx, parse the body'."""
    if status == 0:
        return ProviderCheck(STATUS_UNAVAILABLE, "kick", reason=NETWORK_REASON)
    if status in (401, 403):
        return ProviderCheck(STATUS_UNAVAILABLE, "kick", reason=KEY_REJECTED_REASON)
    if status == 429:
        return ProviderCheck(STATUS_RATE_LIMITED, "kick",
                             reason="Kick API rate limit reached — retry later.")
    if status >= 500:
        return ProviderCheck(STATUS_UNAVAILABLE, "kick",
                             reason=SERVER_ERROR_REASON)
    if 200 <= status < 300:
        return None
    return ProviderCheck(
        STATUS_UNAVAILABLE, "kick",
        reason=f"Kick API returned HTTP {status} — retry later.",
    )
