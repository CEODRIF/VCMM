"""Direct video file URL adapter (TASK 014 — universal link ingestion).

The user pastes a PUBLIC http(s) URL that points directly at a video
FILE (``.mp4`` / ``.webm`` / ``.mov``) — e.g. a link from their own
storage, a CDN, or a file-sharing service. The worker (never the
browser, never Vercel serverless) downloads it and runs the shared
pipeline:

1. ``validate()`` — SSRF-guarded availability PROBE (HEAD, with a
   bounded ``Range: bytes=0-0`` GET fallback for servers that refuse
   HEAD). No payload is ever downloaded here; reachability, HTTP status
   and (when the server declares it) the content length are reported.
2. ``materialize()`` — the guarded transfer: the redirect chain is
   resolved and re-validated hop by hop (fresh public-IP DNS check at
   every hop, bounded), then the FINAL url is streamed in bounded
   chunks to temp disk with the operator's size cap, retry budget and
   a TOTAL wall-clock deadline. The bytes are probed with FFprobe
   BEFORE they are uploaded — a file that is not a real video (an HTML
   page wearing an ``.mp4`` extension) fails here, not later.
3. ingestion pins the object into the PRIVATE ``clipforge-sources``
   bucket (server-generated path) and runs the SAME download/probe/
   complete tail as direct uploads — the analysis/rendering pipeline
   below the provider is identical for every source type.

Security boundary (``http_guard``):

- http(s) only; no userinfo; standard ports (80/443) only; control
  characters rejected;
- the destination hostname is resolved and EVERY answer must be
  globally public — loopback, private, link-local (incl. the cloud
  metadata address), reserved, multicast and unspecified ranges are
  refused; decimal/octal/hex IP encodings and IPv6 variants are
  normalized first;
- the DNS check runs immediately before the probe, before EVERY
  redirect hop, and again before EVERY streaming attempt (rebinding
  posture identical to the platform providers);
- redirects are never followed implicitly — ``resolve_redirect_chain``
  re-validates each ``Location`` with the same rules and the hop count
  is bounded;
- an optional operator HOST ALLOWLIST (``WORKER_DIRECT_URL_HOST_ALLOWLIST``)
  restricts imports to named storage domains; the default (empty)
  permits any PUBLIC host — the allowlist is the only check that can
  be relaxed, and only in this mode.

Platform URLs (youtube.com / youtu.be / twitch.tv / twitch CDN hosts)
are NEVER claimed here — they belong to the platform adapters and their
honest capability gates (see ``docs/link-ingestion.md``).
"""

from __future__ import annotations

import logging
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
    ProviderCheck,
)
from .errors import InvalidUrlError, TransferInterruptedError
from .http_guard import ProbeResult, SafeHttpClient, _assert_url_shape
from .models import NormalizedSource

logger = logging.getLogger("clipforge.providers.direct_url")

#: The file extensions this adapter claims (case-insensitive, path only —
#: a query string is allowed: signed URLs carry their parameters there).
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov")

#: Storage content types for the private-bucket object (by extension).
CONTENT_TYPES = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}

#: Hosts that are PLATFORM endpoints, not file hosts. A URL on one of
#: these must go through its platform adapter (honest capability gate),
#: never through the generic file importer.
_PLATFORM_HOSTS = (
    "youtube.com",
    "youtu.be",
    "twitch.tv",
    "twitchcdn.net",
    "jtvnw.net",
)


def _is_platform_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == suffix or host.endswith("." + suffix) for suffix in _PLATFORM_HOSTS)


def classify_video_url(url: str) -> str | None:
    """Returns the file extension (``.mp4`` / ``.webm`` / ``.mov``) when
    ``url`` is a public http(s) link to a direct video file, else None.

    Pure shape classification — no network, no DNS, no guessing.
    Platform hosts and non-video extensions are never claimed.
    """
    value = str(url or "").strip()
    if not value:
        return None
    try:
        parts = urlparse(value)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None
    if parts.port is not None and parts.port not in (80, 443):
        # Non-standard ports are never claimed: the SSRF guard rejects them
        # anyway, and a claim here would only produce a confusing guard
        # error later. (Default ports parse as port=None.)
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or _is_platform_host(host):
        return None
    if parts.username is not None or parts.password is not None:
        # Credentials-in-URL are refused by the guard too; claiming such a
        # URL would only produce a confusing guard error later.
        return None
    path = (parts.path or "").lower()
    for ext in VIDEO_EXTENSIONS:
        if path.endswith(ext):
            return ext
    return None


@dataclass(frozen=True)
class DirectUrlConfig:
    """Operator settings for direct video file URL imports (TASK 014)."""

    #: Master switch. OFF → the adapter reports honest unavailability
    #: and direct_url jobs fail permanently with a clear message.
    enabled: bool = True
    #: Exact-host allowlist. None/empty = any PUBLIC host (the guard
    #: still applies in full).
    host_allowlist: tuple[str, ...] | None = None
    #: Hard cap on the materialized source (bytes).
    max_source_bytes: int = 2 * 1024 * 1024 * 1024
    #: Streaming chunk size — bounds transfer memory.
    chunk_bytes: int = 1024 * 1024
    #: Bounded retry budget for transient failures (statuses/transport).
    max_attempts: int = 1
    backoff_seconds: float = 1.0
    #: Cap on redirect hops (each hop is re-validated by the guard).
    max_redirects: int = 3
    #: TOTAL wall-clock budget for one transfer (all attempts included).
    transfer_deadline_seconds: float | None = 1800.0
    #: Per-request timeout for the availability probe.
    probe_timeout_seconds: float = 10.0
    #: Per-request timeout for the streaming transfer (read timeout is
    #: per chunk — a slow-but-alive source still fits the deadline).
    transfer_timeout_seconds: float = 30.0

    @property
    def any_public_host(self) -> bool:
        return not self.host_allowlist


class DirectUrlProvider:
    name = "direct_url"
    supports_validation = True
    # The URL itself is the authorized source: a public file the user
    # explicitly pointed at. There is no platform policy to satisfy —
    # only the SSRF guard, the size/time budgets, and FFprobe truth.
    supports_materialization = True

    def __init__(
        self,
        ffprobe_path: str,
        *,
        config: DirectUrlConfig | None = None,
        resolver: Any = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._ffprobe = ffprobe_path
        self._config = config or DirectUrlConfig()
        self._resolver = resolver
        self._external_client = http_client
        self._probe_timeout = self._config.probe_timeout_seconds
        self._transfer_timeout = self._config.transfer_timeout_seconds

    # -- capabilities / configuration -------------------------------------------

    @property
    def is_configured(self) -> bool:
        """The adapter is 'configured' iff the feature switch is ON
        (there are no credentials to configure — the guard IS the
        authorization boundary)."""
        return self._config.enabled

    def can_handle(self, url: str) -> bool:
        return classify_video_url(url) is not None

    def _client(
        self,
        timeout: float,
        *,
        transport: Any = None,
    ) -> SafeHttpClient:
        allowlist = self._config.host_allowlist
        return SafeHttpClient(
            list(allowlist) if allowlist else [],
            timeout=timeout,
            max_redirects=self._config.max_redirects,
            resolver=self._resolver,
            transport=transport,
            client=self._external_client,
            require_allowlist=not self._config.any_public_host,
        )

    # -- validation / probe --------------------------------------------------------

    async def validate(self, url: str) -> ProviderCheck:
        """Guarded availability probe (HEAD, bounded GET fallback).

        Never downloads the media. Destination-policy violations
        (private IP, non-standard port, credentials, allowlist miss)
        raise :class:`InvalidUrlError` — the input cannot change on
        retry, so the job fails permanently with an actionable message.
        Transient reachability problems report ``unavailable`` (retry).
        """
        # SECURITY (TASK 014): unsafe input is REJECTED, not merely
        # "unrecognized". Non-http(s) schemes, embedded credentials,
        # control characters and non-standard ports can never become
        # valid inputs — the check runs BEFORE the shape claim, so an
        # attacker-crafted URL always fails with a typed, permanent
        # InvalidUrlError instead of a soft "not a video URL" answer.
        try:
            _assert_url_shape(str(url or "").strip())
        except InvalidUrlError:
            raise
        ext = classify_video_url(url)
        if ext is None:
            return ProviderCheck(
                STATUS_UNAVAILABLE, self.name,
                reason="Not a direct video file URL — paste a public http(s) link "
                       "that ends in .mp4, .webm or .mov (platform links have "
                       "their own tabs).",
            )
        http = self._client(self._probe_timeout)
        try:
            try:
                result: ProbeResult = await http.probe(url)
            except InvalidUrlError:
                raise
            except TransferInterruptedError:
                return ProviderCheck(
                    STATUS_UNAVAILABLE, self.name,
                    reason="The file could not be reached right now — try again later.",
                )
        finally:
            # aclose() is a no-op for injected (externally owned) clients.
            await http.aclose()

        status = result.status_code
        if status in (200, 206):
            specific: dict[str, Any] = {"file_extension": ext.lstrip(".") or None}
            declared = result.headers.get("content-length")
            if (declared or "").isdigit() and int(declared) > 0:
                specific["declared_bytes"] = int(declared)
            metadata = NormalizedSource(
                provider=self.name,
                canonical_url=url,
                state="completed",
                provider_specific=specific,
            )
            return ProviderCheck(STATUS_AVAILABLE, self.name, metadata=metadata)
        if status in (404, 410):
            return ProviderCheck(STATUS_DELETED, self.name,
                                 reason="The file was not found at that URL (404/410).")
        if status in (401, 403):
            return ProviderCheck(STATUS_RESTRICTED, self.name,
                                 reason="The file URL requires access the worker does "
                                        "not have (private or restricted).")
        if status == 429:
            return ProviderCheck(STATUS_RATE_LIMITED, self.name,
                                 reason="The file host rate-limited the request — retry shortly.")
        return ProviderCheck(STATUS_UNAVAILABLE, self.name,
                             reason="The file host could not be checked right now.")

    # -- materialization -------------------------------------------------------------

    async def materialize(self, url: str, destination_dir: Any, **kwargs: Any) -> Path:
        """Guarded streaming transfer of the file, then a REAL FFprobe
        probe of the bytes before anything is uploaded.

        ``kwargs``: ``progress`` (bytes_written, total_or_None) and
        ``should_cancel`` (lease-loss/shutdown probe), exactly like the
        other materializing adapters.
        """
        ext = classify_video_url(url)
        if ext is None:
            raise InvalidUrlError("direct URL no longer matches a supported video file shape")
        progress = kwargs.get("progress")
        should_cancel = kwargs.get("should_cancel")
        destination_dir = Path(destination_dir)
        destination = destination_dir / f"source{ext}"
        http = self._client(self._transfer_timeout)
        try:
            # 1) Resolve the redirect chain FIRST (every hop re-validated
            #    with fresh public-IP DNS); the stream itself then
            #    refuses any surprise redirect.
            final_url = await http.resolve_redirect_chain(
                url,
                allow_public_redirects=self._config.any_public_host,
            )
            # 2) Bounded streaming transfer (size cap + total deadline).
            await http.stream_to_file(
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
            await http.aclose()
        if not destination.is_file() or destination.stat().st_size == 0:
            destination.unlink(missing_ok=True)
            raise TransferInterruptedError("provider transfer produced an empty file")
        # 3) Validate the ACTUAL bytes (never the extension). A page or a
        #    truncated file fails here, before it can reach storage — and
        #    the rejected bytes are removed (safe cleanup, no garbage).
        try:
            await probe_media(self._ffprobe, destination, self._probe_timeout)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return destination


__all__ = [
    "CONTENT_TYPES",
    "DirectUrlConfig",
    "DirectUrlProvider",
    "VIDEO_EXTENSIONS",
    "classify_video_url",
]
