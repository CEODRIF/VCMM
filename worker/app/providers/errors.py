"""Typed provider errors (TASK 011, spec section 13).

Every provider-layer failure surfaces as a typed :class:`ProviderError`
with a stable ``code``, a ``retryable`` flag, and a **safe message** that
is user/job-log friendly (no credentials, no signed URLs, no internals).
The message is what ends up in ``jobs.last_error`` — it must stand alone
as an explanation.

Two pre-existing Phase 7 exceptions stay where they are
(:class:`~app.providers.base.MaterializationUnavailable` and
:class:`~app.providers.base.ProviderNotSupported`) and are treated as
permanent; :class:`MaterializationUnsupported` wraps the former with a
stable code for queue categorization.
"""

from __future__ import annotations

import re
from typing import Any

#: Stable error codes (recorded in ``jobs.last_error`` as ``code: message``).
CODE_INVALID_URL = "provider_invalid_url"
CODE_UNSUPPORTED_PROVIDER = "provider_unsupported"
CODE_UNAUTHORIZED = "provider_unauthorized"
CODE_METADATA_UNAVAILABLE = "provider_metadata_unavailable"
CODE_MATERIALIZATION_UNSUPPORTED = "materialization_unsupported"
CODE_RATE_LIMITED = "provider_rate_limited"
CODE_PROVIDER_UNAVAILABLE = "provider_unavailable"
CODE_SOURCE_RESTRICTED = "source_restricted"
CODE_SOURCE_EXPIRED = "source_expired"
CODE_SIZE_DURATION_EXCEEDED = "source_size_or_duration_exceeded"
CODE_TRANSFER_INTERRUPTED = "transfer_interrupted"
CODE_STORAGE_FAILED = "storage_upload"
CODE_CANCELLED = "source_cancelled"

_ALL_CODES = (
    CODE_INVALID_URL, CODE_UNSUPPORTED_PROVIDER, CODE_UNAUTHORIZED,
    CODE_METADATA_UNAVAILABLE, CODE_MATERIALIZATION_UNSUPPORTED,
    CODE_RATE_LIMITED, CODE_PROVIDER_UNAVAILABLE, CODE_SOURCE_RESTRICTED,
    CODE_SOURCE_EXPIRED, CODE_SIZE_DURATION_EXCEEDED,
    CODE_TRANSFER_INTERRUPTED, CODE_STORAGE_FAILED, CODE_CANCELLED,
)


class ProviderError(Exception):
    """Base class for all typed provider failures."""

    code: str = CODE_METADATA_UNAVAILABLE
    #: Whether a retry of the SAME job could plausibly succeed (transient
    #: provider/network conditions). Permanent errors must never be
    #: retried — the input or the deployment will not change on its own.
    retryable: bool = False

    def __init__(self, safe_message: str, *, details: dict[str, Any] | None = None) -> None:
        self.safe_message = _redact(safe_message)
        #: Non-sensitive machine-readable context (status codes, host
        #: names). NEVER tokens, signed URLs, or request bodies.
        self.details = dict(details or {})
        super().__init__(self.safe_message)

    def __str__(self) -> str:  # the message is the whole record
        return self.safe_message


class InvalidUrlError(ProviderError):
    code = CODE_INVALID_URL
    retryable = False


class UnsupportedProviderError(ProviderError):
    code = CODE_UNSUPPORTED_PROVIDER
    retryable = False


class UnauthorizedError(ProviderError):
    """Credential problem (expired token, invalid grant, missing scope).
    A retry of the same job cannot fix a credential — the operator or the
    user must act, so this is permanent at job level."""
    code = CODE_UNAUTHORIZED
    retryable = False


class MetadataUnavailableError(ProviderError):
    """The provider could not be reached / answered malformed. Transient
    unless the source itself does not exist (that is SourceExpired/
    SourceRestricted)."""
    code = CODE_METADATA_UNAVAILABLE
    retryable = True


class MaterializationUnsupportedError(ProviderError):
    """Metadata is permitted, but NO authorized materialization path
    exists for this provider on this deployment. This is the honesty
    gate's typed form (spec section 7): it is reported, never worked
    around."""
    code = CODE_MATERIALIZATION_UNSUPPORTED
    retryable = False

    def __init__(self, safe_message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(safe_message, details=details)


class RateLimitedError(ProviderError):
    code = CODE_RATE_LIMITED
    retryable = True


class ProviderUnavailableError(ProviderError):
    """The provider is not configured for this deployment (or is
    disabled). Permanent for the job — retries cannot configure it."""
    code = CODE_PROVIDER_UNAVAILABLE
    retryable = False


class SourceRestrictedError(ProviderError):
    """The source exists but access is limited (private, age-restricted,
    region-locked, live, or platform policy)."""
    code = CODE_SOURCE_RESTRICTED
    retryable = False


class SourceExpiredError(ProviderError):
    """The source no longer exists (deleted, removed, expired VOD)."""
    code = CODE_SOURCE_EXPIRED
    retryable = False


class SizeOrDurationExceededError(ProviderError):
    code = CODE_SIZE_DURATION_EXCEEDED
    retryable = False


class TransferInterruptedError(ProviderError):
    """A permitted transfer broke (network drop, provider 5xx after
    retries, redirect violation). Retryable: re-claim resumes cleanly."""
    code = CODE_TRANSFER_INTERRUPTED
    retryable = True


class StorageFailedError(ProviderError):
    code = CODE_STORAGE_FAILED
    retryable = True


class SourceCancelledError(ProviderError):
    """The job was cancelled (lease loss / shutdown) mid-transfer."""
    code = CODE_CANCELLED
    retryable = True  # the job simply continues on its next claim


#: Mapping from Phase 7 ProviderCheck STATUS_* values to typed errors.
#: ``available`` is not a failure and has no entry here.
STATUS_TO_ERROR: dict[str, type[ProviderError]] = {
    "private": SourceRestrictedError,
    "deleted": SourceExpiredError,
    "restricted": SourceRestrictedError,
    "rate_limited": RateLimitedError,
    "unavailable": MetadataUnavailableError,
}


def error_for_status(status: str, provider: str, reason: str | None) -> ProviderError:
    """Builds the typed error for a non-available ProviderCheck status.
    The provider-supplied ``reason`` (safe by contract) becomes the
    message; a stable code keeps queue categorization deterministic."""
    cls = STATUS_TO_ERROR.get(status, MetadataUnavailableError)
    message = reason or f"{provider} source could not be checked."
    return cls(f"{provider}: {message}")


# -- secret redaction -------------------------------------------------------------

_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Authorization / Cookie headers: mask the ENTIRE value (to end of
    # line) — a header value may carry a "Scheme token" pair, so masking
    # only the first word would leak the rest.
    re.compile(r"(?i)(authorization\s*[:=]\s*)([^\r\n]*)"),
    re.compile(r"(?i)(\bcookie\s*[:=]\s*)([^\r\n]*)"),
    re.compile(r"(?i)(\bset-cookie\s*[:=]\s*)([^\r\n]*)"),
    # Bare bearer tokens (any context: "Bearer <jwt>", "bearer <hex>")
    re.compile(r"(?i)(bearer\s+)([a-z0-9._~+/=-]{16,})"),
    # Key=value credential shapes (JSON bodies, query strings, prose)
    re.compile(r"(?i)(\baccess_token[\"']?\s*[:=]\s*[\"']?)([a-z0-9._~+/=-]{16,})"),
    re.compile(r"(?i)(\brefresh_token[\"']?\s*[:=]\s*[\"']?)([a-z0-9._~+/=-]{16,})"),
    re.compile(r"(?i)(\bclient_secret[\"']?\s*[:=]\s*[\"']?)([^\s\"',;]{8,})"),
    re.compile(r"(?i)(\b(token|api_key|apikey|secret|password)\s*[:=]\s*)([^\s\"',;]{8,})"),
    # Signed supabase/storage URLs (query carries the signature): mask from
    # the FIRST `token=` parameter to the end of the URL — the signature IS
    # the token value, and short signatures must not slip past the
    # length-gated key=value pattern above.
    re.compile(r"(?i)(https?://[^\s]*?\btoken=)[^\s]+"),
    # Bare token= values (defense in depth). Own group, so it can never
    # crash the redactor if pattern ordering changes.
    re.compile(r"(?i)(\btoken=)[a-z0-9._~+/-]{16,}"),
)


def redact(value: str | None) -> str | None:
    """Removes credential-shaped substrings from a message before it can
    reach a log line or a job error record. Defensive only — the real
    rule is that secrets are never put into messages in the first place
    (tokens travel in headers, never URLs)."""
    if value is None:
        return None
    redacted = value
    for pattern in _SENSITIVE_PATTERNS:
        redacted = pattern.sub(lambda m: m.group(1) + "***", redacted)
    return redacted


def _redact(value: str) -> str:
    return redact(value) or ""
