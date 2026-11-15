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
    """A source adapter. Provider-specific code stays behind this surface.

    TASK 015 — universal ingestion contract. The canonical names required
    by the product spec map onto this core surface; :class:`UniversalIngestionMixin`
    provides the mapping so every adapter exposes the same 8-method
    contract without a breaking refactor:

    =========================  ==================================================
    canonical name             implementation
    =========================  ==================================================
    ``classify_url(url)``       ``can_handle(url)`` — shape claim, no network
    ``validate_source(url)``    ``validate(url)`` — availability + status
    ``fetch_metadata(url)``     ``validate(url)`` → ``check.metadata``
    ``acquire_media(url, dir)`` ``materialize(url, dir, progress=, should_cancel=)``
    ``stream_or_download(...)`` bounded-chunk streaming transfer (the
                                 :mod:`app.providers.http_guard` guarantees:
                                 chunked reads, size cap, deadline, retry)
                                 returning the acquired file size
    ``report_progress(cb)``     progress flows through the ``progress``
                                 keyword of ``acquire_media`` into the job
                                 queue (the pipeline owns user-facing
                                 progress; providers never log it)
    ``cleanup(path)``           remove a locally acquired file (safe,
                                 best-effort — the pipeline also rmtrees
                                 its temp dir on every exit path)
    =========================  ==================================================
    """

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


class UniversalIngestionMixin:
    """TASK 015 — the canonical 8-method ingestion contract.

    Concrete providers implement the three core methods (``can_handle``,
    ``validate``, ``materialize``) and inherit this mixin to expose the
    canonical names. The aliases are thin and deterministic — the
    security guarantees live in the core implementations (SSRF guard,
    bounded streaming, typed errors), never in this layer.
    """

    def classify_url(self, url: str) -> bool:
        """Shape claim for the ingestion engine's auto-detection."""
        return self.can_handle(url)

    async def validate_source(self, url: str) -> ProviderCheck:
        return await self.validate(url)

    async def fetch_metadata(self, url: str) -> Any:
        """Normalized metadata for AVAILABLE sources; None otherwise
        (an unavailable source has no trustworthy metadata)."""
        check = await self.validate(url)
        return check.metadata if check.status == STATUS_AVAILABLE else None

    async def acquire_media(self, url: str, destination_dir: Any, **kwargs: Any) -> Any:
        """Authorized media acquisition → locally analyzable file."""
        return await self.materialize(url, destination_dir, **kwargs)

    async def stream_or_download(self, url: str, destination_dir: Any, **kwargs: Any) -> int:
        """Bounded-chunk streaming transfer (see the module contract
        table). Returns the acquired file size in bytes."""
        path = await self.materialize(url, destination_dir, **kwargs)
        return path.stat().st_size

    def report_progress(self, progress: float) -> None:
        """User-facing progress is pipeline-owned (queue writes); the
        progress CALLBACK passed to acquire_media is the provider's
        reporting channel. Kept as a no-op hook for contract symmetry."""
        return None

    def cleanup(self, path: Any) -> None:
        """Best-effort removal of a locally acquired file (never raises)."""
        try:
            from pathlib import Path as _Path
            _Path(str(path)).unlink(missing_ok=True)
        except OSError:
            pass
