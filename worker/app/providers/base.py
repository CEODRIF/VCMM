"""Provider protocol + shared types (TASK 007)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

#: Availability outcomes for a URL check.
STATUS_AVAILABLE = "available"
STATUS_PRIVATE = "private"
STATUS_DELETED = "deleted"
STATUS_RESTRICTED = "restricted"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_UNAVAILABLE = "unavailable"


class MaterializationUnavailable(RuntimeError):
    """The adapter does NOT implement an authorized download path.

    Raised (never swallowed) whenever a caller tries to materialize a
    provider that only supports validation. This is the honesty gate: a
    provider is never claimed as downloadable until an explicitly
    authorized adapter implements it.
    """


class ProviderNotSupported(ValueError):
    """No adapter claims this URL (or the URL is not a valid URL)."""


@dataclass(frozen=True)
class ProviderCheck:
    """Result of a URL validation / availability check (no download)."""

    status: str                      # one of the STATUS_* constants
    provider: str                    # adapter name
    title: str | None = None
    duration_seconds: float | None = None
    reason: str | None = None        # safe, user-facing explanation
    #: TASK 011 (additive): the normalized metadata the official check
    #: produced — ``None`` when the provider was not configured, was
    #: rate-limited, or the source is not in a state that can carry
    #: metadata. Unknown fields stay ``None`` inside it (never guessed).
    metadata: Any = None             # NormalizedSource | None (Any: no import cycle)


@runtime_checkable
class StreamProvider(Protocol):
    """A source adapter. Provider-specific code stays behind this surface."""

    name: str
    supports_validation: bool
    supports_materialization: bool

    def can_handle(self, url: str) -> bool:
        """True when this adapter claims the URL (scheme + host shape)."""
        raise NotImplementedError

    async def validate(self, url: str) -> ProviderCheck:
        """Validates the URL and checks availability (official/keyless API
        or local probe). Never downloads media. Maps provider errors to
        the safe STATUS_* outcomes (private/deleted/restricted/
        rate_limited/unavailable)."""
        raise NotImplementedError

    async def materialize(self, url: str, destination_dir: Any, **kwargs: Any) -> Any:
        """Produces a locally analyzable file. Adapters without an
        AUTHORIZED download path raise :class:`MaterializationUnavailable`."""
        raise NotImplementedError
