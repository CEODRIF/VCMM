"""Provider registry (TASK 007 foundation, TASK 011 — configuration).

Dispatch without guessing:

- :func:`provider_for_url` — dispatch by URL claim (can_handle);
- :func:`provider_by_type` — dispatch by ``jobs.source_type`` (the
  ingestion path; every provider source type MUST have an adapter, else
  the job fails permanently with a clear error).

``default_providers`` takes an optional :class:`Settings` (TASK 011):
without it, the Phase 7 defaults apply exactly (keyless adapters, no
materialization) — backward compatible for existing callers/tests.
"""

from __future__ import annotations

from typing import Sequence

from .base import ProviderNotSupported, StreamProvider
from .direct_url import DirectUrlConfig, DirectUrlProvider
from .twitch import TwitchConfig, TwitchProvider
from .upload import UploadProvider
from .youtube import YouTubeConfig, YouTubeProvider


def _youtube_config_from(settings) -> YouTubeConfig | None:
    if settings is None:
        return None
    return YouTubeConfig(
        api_key=settings.youtube_api_key,
        oauth_client_id=settings.youtube_oauth_client_id,
        oauth_client_secret=settings.youtube_oauth_client_secret,
        oauth_refresh_token=settings.youtube_oauth_refresh_token,
        oauth_scopes=settings.youtube_oauth_scopes,
    )


def _twitch_config_from(settings) -> TwitchConfig | None:
    if settings is None:
        return None
    suffixes = tuple(
        s.strip() for s in (settings.twitch_media_host_suffixes or "").split(",")
        if s.strip()
    )
    return TwitchConfig(
        client_id=settings.twitch_client_id,
        client_secret=settings.twitch_client_secret,
        owner_refresh_token=settings.twitch_oauth_refresh_token,
        api_base=settings.twitch_api_base,
        oauth_base=settings.twitch_oauth_base,
        media_host_suffixes=suffixes or TwitchConfig.media_host_suffixes,
        # Transfer budgets for clip materialization (operator-controlled).
        max_source_bytes=settings.materialize_max_source_bytes,
        chunk_bytes=settings.materialize_chunk_bytes,
        max_attempts=settings.provider_max_retries + 1,
        backoff_seconds=settings.provider_retry_backoff_seconds,
        max_redirects=settings.materialize_max_redirects,
        transfer_deadline_seconds=settings.direct_url_transfer_deadline_seconds,
        transfer_timeout_seconds=settings.provider_request_timeout_seconds,
    )


def _direct_url_config_from(settings) -> DirectUrlConfig | None:
    if settings is None:
        return None
    allowlist = tuple(
        s.strip() for s in (settings.direct_url_host_allowlist or "").split(",")
        if s.strip()
    )
    return DirectUrlConfig(
        enabled=settings.direct_urls_enabled,
        host_allowlist=allowlist or None,
        max_source_bytes=settings.materialize_max_source_bytes,
        chunk_bytes=settings.materialize_chunk_bytes,
        max_attempts=settings.provider_max_retries + 1,
        backoff_seconds=settings.provider_retry_backoff_seconds,
        max_redirects=settings.materialize_max_redirects,
        transfer_deadline_seconds=settings.direct_url_transfer_deadline_seconds,
        probe_timeout_seconds=settings.provider_request_timeout_seconds,
        transfer_timeout_seconds=settings.provider_request_timeout_seconds,
    )


def default_providers(
    ffprobe_path: str, settings=None
) -> tuple[StreamProvider, ...]:
    """The adapters that ship in this build.

    - upload: validation + materialization (fully implemented);
    - youtube: OFFICIAL METADATA only — materialization refused (no
      official media-download API; see the adapter's docstring);
    - twitch: OFFICIAL METADATA + CLIP materialization through the
      official Get Clips Download endpoint under the creator's OAuth
      authorization (TASK 014). VOD/live media has no authorized
      endpoint and is refused per-URL-class;
    - direct_url (TASK 014): public http(s) links to video files
      (.mp4/.webm/.mov) — SSRF-guarded streaming import; LAST in the
      tuple so platform URLs are always claimed by their platform
      adapter first.
    """
    return (
        UploadProvider(ffprobe_path),
        YouTubeProvider(config=_youtube_config_from(settings)),
        TwitchProvider(
            config=_twitch_config_from(settings),
            ffprobe_path=ffprobe_path,
        ),
        DirectUrlProvider(ffprobe_path,
                          config=_direct_url_config_from(settings)),
    )


def provider_for_url(url: str, providers: Sequence[StreamProvider]) -> StreamProvider:
    """Returns the adapter claiming ``url``; raises ProviderNotSupported
    when none does (never guessed, never a default)."""
    value = str(url or "")
    for provider in providers:
        try:
            if provider.can_handle(value):
                return provider
        except Exception:  # noqa: BLE001 — a broken can_handle must not leak
            continue
    raise ProviderNotSupported("No provider adapter claims this URL.")


def provider_by_type(source_type: str, providers: Sequence[StreamProvider]) -> StreamProvider | None:
    """Returns the adapter whose ``name`` matches the job's
    ``source_type`` (or None when no adapter is registered for it)."""
    value = str(source_type or "")
    for provider in providers:
        if getattr(provider, "name", None) == value:
            return provider
    return None


def provider_is_configured(provider: StreamProvider) -> bool:
    """Whether the adapter can actually call its platform's official
    metadata API on THIS deployment.

    - upload: always configured (local storage, no platform calls);
    - youtube: always able to do keyless oEmbed metadata (config enriches
      it), so configured = True;
    - twitch: requires at least a client id (official minimum) — without
      it the adapter reports honest unavailability instead of guessing.
    """
    flag = getattr(provider, "is_configured", None)
    if isinstance(flag, bool):
        return flag
    return True
