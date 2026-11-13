"""SSRF-safe HTTP for provider calls (TASK 011, spec sections 7 + 9).

The worker talks to provider endpoints ONLY through this layer. Threats
addressed and how:

- **scheme/host allowlist** — only ``http(s)`` to explicitly allowlisted
  hosts (per provider); lookalike domains (``not-twitch.com``,
  ``youtube-evil.com``, ``twitch.tv.attacker.example``) can never match;
- **no credentials in URLs** — user:pass@ URLs are rejected; auth tokens
  travel ONLY in request headers;
- **port restriction** — 80/443 only (no arbitrary service ports);
- **SSRF / private-IP rejection** — the hostname is resolved (every
  A/AAAA answer) and ALL resolved addresses must be globally public:
  loopback, private, link-local, reserved, multicast and unspecified
  ranges are rejected; decimal/octal/hex IP encodings and IPv6 variants
  are normalized before the check;
- **DNS-rebinding posture** — resolution + check runs IMMEDIATELY before
  every request AND again before every redirect hop (fresh resolution,
  not a cached answer); the residual TOCTOU window between our check and
  the underlying connector is documented in the module tests;
- **redirect policy** — redirects are NOT followed automatically; each
  ``Location`` is re-validated by the SAME rules (allowlist + private-IP
  check) and hops are bounded;
- **no user-supplied headers** — the caller may only set the documented
  header allowlist (Accept / Authorization / Client-Id); everything else
  is built here;
- **secret hygiene** — URLs never carry tokens; every log/error line is
  passed through :func:`~app.providers.errors.redact`.

Deterministic and fully testable: the resolver is injectable, and the
transport is the standard httpx one (tests use ``httpx.MockTransport``).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Any, Awaitable, Callable, Iterable, Sequence
from urllib.parse import urlsplit

import httpx

from .errors import (
    InvalidUrlError,
    RateLimitedError,
    SourceCancelledError,
    TransferInterruptedError,
    redact,
)

logger = logging.getLogger("clipforge.providers.http")

#: Hosts are compared lowercased; trailing-dot variants are normalized.
_ALLOWED_PORTS = {80, 443}
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
#: Header names callers may set (lowercase). Authorization carries the
#: provider token — it is NEVER written to logs or URLs.
#: (x-goog-api-key = the official header form of YouTube API keys, so the
#: key never appears in a URL/exception string.)
_HEADER_ALLOWLIST = frozenset({"accept", "authorization", "client-id",
                               "client_id", "x-goog-api-key"})

#: A resolver maps a hostname to the list of IP addresses (strings) it
#: resolves to. Injectable for tests (deterministic, offline).
Resolver = Callable[[str], Sequence[str]]


# -- URL + IP validation ---------------------------------------------------------

def _normalize_host(host: str) -> str:
    return host.lower().rstrip(".")


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for globally routable public addresses (fail closed).

    Note: ``is_site_local`` exists only on IPv6Address (fc00::/7); the
    IPv4 equivalents (10/8, 172.16/12, 192.168/16) are already covered by
    ``is_private``. Everything non-public is refused.
    """
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or getattr(ip, "is_site_local", False)
    )


def _parse_ip(text: str) -> ipaddress._BaseAddress | None:  # type: ignore[name-defined]
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def resolve_and_check(
    hostname: str, resolver: Resolver | None = None
) -> list[str]:
    """Resolves ``hostname`` and verifies EVERY answer is public.

    Raises :class:`InvalidUrlError` for an unresolvable name or any
    disallowed address. Returns the public addresses (for logging/tests).
    """
    host = _normalize_host(hostname or "")
    if not host:
        raise InvalidUrlError("empty hostname")

    # A literal IP in the URL is checked directly (decimal/octal/hex
    # encodings are normalized by ipaddress before the class check).
    literal = _parse_ip(host)
    if literal is not None:
        if not _is_public_ip(literal):
            raise InvalidUrlError(f"refusing non-public destination ({literal.version} address class)")
        return [str(literal)]

    resolve = resolver or _system_resolver
    try:
        answers = list(resolve(host))
    except (socket.gaierror, OSError, UnicodeError) as exc:
        raise InvalidUrlError(f"could not resolve host: {exc.__class__.__name__}") from None
    if not answers:
        raise InvalidUrlError("host resolved to no addresses")
    for answer in answers:
        ip = _parse_ip(answer)
        if ip is None:
            # A resolver that returns non-IP strings is a misconfiguration —
            # fail closed.
            raise InvalidUrlError("resolver returned a non-IP address")
        if not _is_public_ip(ip):
            raise InvalidUrlError(f"refusing non-public destination address")
    return [str(_parse_ip(a)) for a in answers]  # type: ignore[arg-type]


def _system_resolver(hostname: str) -> Sequence[str]:
    infos = socket.getaddrinfo(hostname, None)
    return {info[4][0] for info in infos}


def assert_safe_url(url: str, allowed_hosts: Iterable[str]) -> str:
    """Strict URL validation against the provider's host allowlist.

    Returns the normalized URL (trimmed). Raises
    :class:`InvalidUrlError` with a safe message on any violation.
    """
    value = (url or "").strip()
    if not value:
        raise InvalidUrlError("empty URL")
    if "\r" in value or "\n" in value or "\x00" in value:
        raise InvalidUrlError("URL contains control characters")
    try:
        parts = urlsplit(value)
    except ValueError:
        raise InvalidUrlError("malformed URL") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise InvalidUrlError("only http(s) URLs are permitted")
    host = parts.hostname
    if not host:
        raise InvalidUrlError("URL has no host")
    host = _normalize_host(host)
    if parts.username is not None or parts.password is not None:
        raise InvalidUrlError("credentials in URLs are not permitted")
    port = parts.port
    if port is None:
        port = 443 if parts.scheme.lower() == "https" else 80
    if port not in _ALLOWED_PORTS:
        raise InvalidUrlError("only standard http/https ports are permitted")
    allowed = {_normalize_host(h) for h in allowed_hosts}
    if host not in allowed:
        raise InvalidUrlError("host is not on the provider allowlist")
    return value


def validate_url_and_destination(url: str, allowed_hosts: Iterable[str],
                                 resolver: Resolver | None = None) -> str:
    """assert_safe_url + DNS revalidation (the two halves of the SSRF
    guard, always applied together before a request)."""
    normalized = assert_safe_url(url, allowed_hosts)
    host = _normalize_host(urlsplit(normalized).hostname or "")
    resolve_and_check(host, resolver)
    return normalized


# -- safe client ---------------------------------------------------------------

class SafeHttpClient:
    """httpx wrapper enforcing the guard on EVERY request and EVERY
    redirect hop. The transport is injectable (httpx.MockTransport in
    tests); the resolver is injectable (deterministic offline DNS)."""

    def __init__(
        self,
        allowed_hosts: Sequence[str],
        *,
        timeout: float = 10.0,
        max_redirects: int = 3,
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
        base_headers: dict[str, str] | None = None,
    ) -> None:
        self._allowed_hosts = tuple(_normalize_host(h) for h in allowed_hosts)
        self._timeout = timeout
        self._max_redirects = max(0, int(max_redirects))
        self._resolver = resolver
        self._base_headers = {k.lower(): v for k, v in (base_headers or {}).items()}
        if client is not None:
            # Dependency-injected client (tests / composition root): we
            # still own the REDIRECT POLICY — force no auto-follow so
            # every hop passes the guard.
            client.follow_redirects = False
            self._client = client
            self._owns_client = False
        else:
            # follow_redirects=False: we validate every hop ourselves.
            self._client = httpx.AsyncClient(
                timeout=timeout, follow_redirects=False, transport=transport
            )
            self._owns_client = True

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers_for(self, extra: dict[str, str] | None) -> dict[str, str]:
        headers = dict(self._base_headers)
        for key, value in (extra or {}).items():
            lowered = key.lower()
            if lowered not in _HEADER_ALLOWLIST:
                # Never forward arbitrary caller headers to the wire.
                raise InvalidUrlError(f"header {key!r} is not permitted")
            headers[lowered] = value
        return headers

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
    ) -> httpx.Response:
        """One HTTP call with bounded, re-validated redirects.

        ``data`` is a form-encoded body (provider-defined forms only,
        e.g. OAuth token exchanges) — never free-form caller payloads.
        Raises typed provider errors (InvalidUrlError /
        TransferInterruptedError) — raw transport exceptions are mapped,
        never leaked.
        """
        current = url
        for _hop in range(self._max_redirects + 1):
            try:
                normalized = validate_url_and_destination(
                    current, self._allowed_hosts, self._resolver
                )
            except InvalidUrlError as exc:
                raise exc
            try:
                response = await self._client.request(
                    method, normalized, headers=self._headers_for(headers),
                    params=params, data=data,
                )
            except httpx.TimeoutException as exc:
                raise TransferInterruptedError(
                    "provider request timed out",
                    details={"stage": "request", "error": exc.__class__.__name__},
                ) from None
            except httpx.HTTPError as exc:
                raise TransferInterruptedError(
                    "provider request failed",
                    details={"stage": "request", "error": exc.__class__.__name__},
                ) from None
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise TransferInterruptedError(
                        "provider redirect without a Location header"
                    )
                # Absolute or relative-to-current — resolve safely.
                # (httpx URL is rendered with plain str() in 0.28.x.)
                current = str(httpx.URL(normalized).join(location))
                params = None  # the Location is authoritative
                data = None
                # 301/302/303 switch non-GET requests to GET (RFC 9110).
                if method.upper() not in ("GET", "HEAD") \
                        and response.status_code in (301, 302, 303):
                    method = "GET"
                continue
            return response
        raise TransferInterruptedError(
            f"too many redirects (>{self._max_redirects})",
            details={"stage": "redirect"},
        )

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    # -- streaming -----------------------------------------------------------

    async def stream_to_file(
        self,
        url: str,
        destination: Any,  # pathlib.Path
        *,
        max_bytes: int,
        chunk_size: int = 1024 * 1024,
        headers: dict[str, str] | None = None,
        progress: Callable[[int, int | None], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        max_attempts: int = 1,
        backoff_seconds: float = 1.0,
        retryable_statuses: Iterable[int] = _RETRYABLE_STATUS,
    ) -> int:
        """Streams ``url`` to ``destination`` in bounded chunks.

        Guarantees (all tested): never reads more than ``chunk_size``
        bytes of payload into memory at once; aborts + removes the
        partial file when the size cap is exceeded, when
        ``should_cancel()`` turns true, or on any failure; retries ONLY
        retryable statuses/transport errors, bounded by ``max_attempts``
        with linear backoff (honoring Retry-After when present).
        Returns total bytes written.
        """
        retryable = set(retryable_statuses)
        attempt = 0
        while True:
            attempt += 1
            destination.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            try:
                # The guard runs per attempt (fresh DNS each time).
                normalized = validate_url_and_destination(
                    url, self._allowed_hosts, self._resolver
                )
                handle = destination.open("wb")  # fresh file per attempt
                try:
                    async with self._client.stream(
                        "GET", normalized, headers=self._headers_for(headers)
                    ) as response:
                        if response.status_code in retryable:
                            retry_after = _retry_after_seconds(response.headers)
                            if attempt < max_attempts:
                                await _sleep(backoff_seconds, retry_after)
                                continue
                            if response.status_code == 429:
                                raise RateLimitedError(
                                    "provider rate limit (after retries)",
                                    details={"status": 429},
                                )
                            raise TransferInterruptedError(
                                "provider returned an error (after retries)",
                                details={"status": response.status_code},
                            )
                        if response.status_code >= 400:
                            raise TransferInterruptedError(
                                f"provider denied the transfer (status {response.status_code})",
                                details={"status": response.status_code},
                            )
                        total_header = response.headers.get("content-length")
                        total = int(total_header) if (total_header or "").isdigit() else None
                        async for chunk in response.aiter_bytes(chunk_size):
                            if should_cancel is not None and should_cancel():
                                raise SourceCancelledError(
                                    "transfer cancelled (lease loss or shutdown)"
                                )
                            if not chunk:
                                continue
                            written += len(chunk)
                            if written > max_bytes:
                                raise _SizeCapExceeded(max_bytes)
                            handle.write(chunk)
                            if progress is not None:
                                progress(written, total)
                finally:
                    handle.close()
                return written
            except _SizeCapExceeded:
                _remove_quietly(destination)
                raise TransferInterruptedError(
                    f"source exceeds the maximum size ({max_bytes} bytes)",
                    details={"max_bytes": max_bytes},
                ) from None
            except (SourceCancelledError, RateLimitedError, InvalidUrlError):
                _remove_quietly(destination)
                raise
            except httpx.TimeoutException:
                _remove_quietly(destination)
                if attempt < max_attempts:
                    await _sleep(backoff_seconds, None)
                    continue
                raise TransferInterruptedError(
                    "provider transfer timed out (after retries)",
                    details={"stage": "stream"},
                ) from None
            except httpx.HTTPError:
                _remove_quietly(destination)
                if attempt < max_attempts:
                    await _sleep(backoff_seconds, None)
                    continue
                raise TransferInterruptedError(
                    "provider transfer failed (after retries)",
                    details={"stage": "stream"},
                ) from None
            finally:
                if written == 0:
                    _remove_quietly(destination)


class _SizeCapExceeded(Exception):
    def __init__(self, max_bytes: int) -> None:
        super().__init__(f"size cap {max_bytes}")
        self.max_bytes = max_bytes


def _remove_quietly(destination: Any) -> None:
    try:
        destination.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 — cleanup must never mask the error
        logger.warning("could not remove partial file %s", redact(str(destination)))


def _retry_after_seconds(headers: httpx.Headers) -> float | None:
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, min(30.0, float(raw)))
    except ValueError:
        return None


async def _sleep(backoff: float, retry_after: float | None) -> None:
    delay = max(backoff, retry_after or 0.0)
    if delay > 0:
        await asyncio.sleep(delay)
