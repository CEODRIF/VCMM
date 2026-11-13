"""Provider registry (TASK 007) — dispatch without guessing."""

from __future__ import annotations

from typing import Sequence

from .base import ProviderNotSupported, StreamProvider
from .twitch import TwitchProvider
from .upload import UploadProvider
from .youtube import YouTubeProvider


def default_providers(ffprobe_path: str) -> tuple[StreamProvider, ...]:
    """The adapters that ship in this build.

    - upload: validation + materialization (fully implemented);
    - youtube / twitch: VALIDATION ONLY (materialization requires an
      explicitly authorized provider and is refused until then).
    """
    return (
        UploadProvider(ffprobe_path),
        YouTubeProvider(),
        TwitchProvider(),
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
