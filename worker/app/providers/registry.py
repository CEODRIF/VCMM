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
    return TwitchConfig(
        client_id=settings.twitch_client_id,
        client_secret=settings.twitch_client_secret,
        api_base=settings.twitch_api_base,
        oauth_base=settings.twitch_oauth_base,
    )


def default_providers(
    ffprobe_path: str, settings=None
) -> tuple[StreamProvider, ...]:
    """The adapters that ship in this build.

    - upload: validation + materialization (fully implemented);
    - youtube / twitch: OFFICIAL METADATA (keyless oEmbed / Helix with
      operator credentials when configured). Materialization is refused
      (no official media-download API exists on either platform — see
      the adapters' docstrings and docs/ARCHITECTURE.md).
    """
    return (
        UploadProvider(ffprobe_path),
        YouTubeProvider(config=_youtube_config_from(settings)),
        TwitchProvider(config=_twitch_config_from(settings)),
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
