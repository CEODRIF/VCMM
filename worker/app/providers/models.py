"""Normalized provider source metadata (TASK 011, spec section 4).

A stable, provider-independent metadata contract. The rules:

- **Unknown fields stay unknown** — every field is ``None`` until a
  provider officially reports a value; nothing is guessed, defaulted, or
  fabricated (e.g. a keyless oEmbed check reports title/creator but NOT
  duration — duration stays ``None``);
- **strict types** — values are validated on construction;
- **no secrets, ever** — ``provider_specific`` is a small whitelist of
  safe, displayable facts (see :meth:`NormalizedSource.to_public_dict`);
  tokens, signed URLs and internal storage paths never appear in this
  model;
- the model is a value object (frozen) so it can be stored in
  ``jobs.source_metadata`` (jsonb) without mutation surprises.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

#: Source states. ``completed`` = a finished recording; ``live`` = a
#: broadcast in progress (NOT materializable); ``scheduled`` = announced,
#: not started; ``unavailable`` = not found / deleted / expired;
#: ``restricted`` = exists but access is limited (private, age-restricted,
#: region-locked, or a provider policy the deployment cannot authorize);
#: ``unknown`` = the provider did not report a state (never guessed).
SourceState = Literal[
    "completed", "live", "scheduled", "unavailable", "restricted", "unknown"
]
SOURCE_STATES = frozenset(
    {"completed", "live", "scheduled", "unavailable", "restricted", "unknown"}
)


def _validate_non_negative(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number or None")
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        raise ValueError(f"{name} must be finite and >= 0")
    return float(value)


def _validate_positive_int(value: int | None, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer or None")
    return value


@dataclass(frozen=True)
class NormalizedSource:
    """Normalized, provider-independent metadata for ONE source."""

    provider: str
    canonical_url: str
    provider_id: str | None = None
    title: str | None = None
    duration_seconds: float | None = None
    published_at: str | None = None       # ISO-8601 UTC, when officially reported
    creator_name: str | None = None
    thumbnail_url: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    quality: str | None = None
    state: SourceState = "unknown"
    #: Small, SAFE, displayable provider facts (e.g. twitch "archive" vs
    #: "highlight", youtube privacy status). No tokens, no signed URLs,
    #: no internal paths. Bounded at construction (see _check_specific).
    provider_specific: dict[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if not self.provider or not isinstance(self.provider, str):
            raise ValueError("provider must be a non-empty string")
        if not self.canonical_url or not isinstance(self.canonical_url, str):
            raise ValueError("canonical_url must be a non-empty string")
        if self.state not in SOURCE_STATES:
            raise ValueError(f"state must be one of {sorted(SOURCE_STATES)}")
        object.__setattr__(
            self, "duration_seconds", _validate_non_negative(self.duration_seconds,
                                                             "duration_seconds"))
        object.__setattr__(self, "width", _validate_positive_int(self.width, "width"))
        object.__setattr__(self, "height", _validate_positive_int(self.height, "height"))
        fps = self.fps
        if fps is not None:
            if isinstance(fps, bool) or not isinstance(fps, (int, float)) \
                    or fps != fps or fps <= 0:
                raise ValueError("fps must be a positive finite number or None")
            object.__setattr__(self, "fps", float(fps))
        if self.published_at is not None and not isinstance(self.published_at, str):
            raise ValueError("published_at must be an ISO-8601 string or None")
        _check_specific(self.provider_specific)
        # Defensive copy so the frozen model's nested dict can't be mutated
        # through an alias held by the caller.
        object.__setattr__(self, "provider_specific", dict(self.provider_specific))

    # -- serialization -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict for ``jobs.source_metadata`` (no secrets by
        construction — see :meth:`to_public_dict` for the frontend subset)."""
        return {
            "provider": self.provider,
            "provider_id": self.provider_id,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "duration_seconds": self.duration_seconds,
            "published_at": self.published_at,
            "creator_name": self.creator_name,
            "thumbnail_url": self.thumbnail_url,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "quality": self.quality,
            "state": self.state,
            "provider_specific": dict(self.provider_specific),
        }

    #: Whitelist of keys safe to show in API responses (job lists/details).
    PUBLIC_FIELDS = ("provider", "provider_id", "title", "duration_seconds",
                     "published_at", "creator_name", "state")

    def to_public_dict(self) -> dict[str, Any]:
        """The frontend-visible subset: NO URLs (canonical/thumbnail), no
        internal paths, no provider internals. Only display facts."""
        return {
            "provider": self.provider,
            "providerId": self.provider_id,
            "title": self.title,
            "durationSeconds": self.duration_seconds,
            "publishedAt": self.published_at,
            "creatorName": self.creator_name,
            "state": self.state,
        }


def _check_specific(specific: dict[str, Any]) -> None:
    """``provider_specific`` must be small, flat-ish and secret-free."""
    if not isinstance(specific, dict):
        raise ValueError("provider_specific must be a dict")
    if len(specific) > 12:
        raise ValueError("provider_specific is limited to 12 entries")
    for key, value in specific.items():
        if not isinstance(key, str) or not key:
            raise ValueError("provider_specific keys must be non-empty strings")
        lowered = key.lower()
        # Hard block on anything that could carry credentials/URLs.
        if any(marker in lowered for marker in
               ("token", "secret", "key", "password", "cookie", "signature",
                "signed_url", "upload_url", "authorization")):
            raise ValueError(f"provider_specific key {key!r} is not allowed")
        if value is None or isinstance(value, (str, int, float, bool)):
            if isinstance(value, str) and len(value) > 200:
                raise ValueError("provider_specific string values are capped at 200 chars")
            continue
        raise ValueError("provider_specific values must be scalars or None")
