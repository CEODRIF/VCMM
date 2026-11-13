"""TASK 011 security tests: SSRF guard, URL allowlist, redirects, headers,
secret redaction (spec sections 7 + 9 + 13).

Everything is deterministic and offline:
- DNS is an injected resolver stub (deterministic answers, incl. rebinding);
- the network is an httpx.MockTransport (requests recorded, no sockets);
- the ONE exception: encoded-IP tests use the system resolver, but only for
  strings that fail locally as malformed numeric hosts (no DNS query is
  ever emitted for them).
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import httpx
import pytest

from app.providers.errors import (
    InvalidUrlError,
    RateLimitedError,
    SourceCancelledError,
    TransferInterruptedError,
    redact,
)
from app.providers.http_guard import (
    SafeHttpClient,
    assert_safe_url,
    resolve_and_check,
)


def run(coro):
    return asyncio.run(coro)


def _resolver(*answers):
    def _resolve(hostname: str):
        return answers
    return _resolve


PUBLIC = "93.184.216.34"
ALLOWED = ["api.example.com"]


# =============================================================================
# URL allowlist + strict parsing (assert_safe_url)
# =============================================================================

def test_allowlist_accepts_canonical_forms():
    assert assert_safe_url("https://api.example.com/v", ALLOWED)
    assert assert_safe_url("http://api.example.com/v", ALLOWED)
    # case-normalized host, explicit standard port
    assert assert_safe_url("https://API.EXAMPLE.COM/v", ALLOWED)
    assert assert_safe_url("https://api.example.com:443/v", ALLOWED)
    assert assert_safe_url("http://api.example.com:80/v", ALLOWED)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://api.example.com/x",
        "javascript:alert(1)",
        "https://api.example.com.evil.com/x",      # suffix lookalike
        "https://evil-api.example.com/x",          # prefix lookalike
        "https://notapi.example.com/x",
        "https://api.example.com.attacker.test/x",
        "https://user:pass@api.example.com/x",     # userinfo
        "https://user@api.example.com/x",
        "https://api.example.com:8080/x",          # non-standard port
        "https://api.example.com:8443/x",
        "https://api.example.com/\x00evil",        # NUL
        "https://api.example.com/\r\nHost: evil",  # CRLF injection
        "https://api.example.com/a\nb",            # embedded newline
        "https://",
        "",
    ],
)
def test_allowlist_rejects_attacker_forms(url):
    with pytest.raises(InvalidUrlError):
        assert_safe_url(url, ALLOWED)


# =============================================================================
# Private / loopback / reserved destinations (SSRF)
# =============================================================================

@pytest.mark.parametrize(
    "host",
    [
        # loopback (v4 + v6)
        "127.0.0.1", "127.5.5.5", "::1",
        # RFC1918
        "10.0.0.1", "192.168.1.1", "172.16.0.1", "172.31.255.255",
        # link-local (v4 incl. the cloud-metadata address, + v6)
        "169.254.169.254", "169.254.0.1", "fe80::1",
        # unspecified
        "0.0.0.0", "::",
        # multicast / broadcast
        "224.0.0.1", "255.255.255.255", "ff02::1",
        # IPv6 unique-local (site-local)
        "fc00::1", "fd12:3456:789a::1",
    ],
)
def test_private_destinations_rejected(host):
    with pytest.raises(InvalidUrlError):
        resolve_and_check(host)  # literal IP: no resolver involved


@pytest.mark.parametrize("host", ["8.8.8.8", PUBLIC, "2001:4860:4860::8888"])
def test_public_destinations_accepted(host):
    assert resolve_and_check(host) == [host]


# Outside RFC1918: 172.32.0.1 is public — must NOT be rejected.
def test_public_range_edge_accepted():
    assert resolve_and_check("172.32.0.1") == ["172.32.0.1"]


def test_encoded_ip_forms_fail_closed():
    """Decimal/octal/hex encodings of loopback: ipaddress refuses to parse
    them (since the 3.9.5 CVE fix), so they fall to the resolver — and the
    system resolver rejects malformed numeric hosts WITHOUT any DNS query
    (EAI_NONAME, local). Fail closed either way."""
    for encoded in ("2130706433", "0177.0.0.1", "0x7f.0.0.1"):
        with pytest.raises(InvalidUrlError):
            resolve_and_check(encoded)


def test_unresolvable_host_fails_closed():
    def no_answers(hostname):
        return []

    with pytest.raises(InvalidUrlError):
        resolve_and_check("api.example.com", no_answers)


def test_resolver_gaierror_fails_closed():
    def broken(hostname):
        raise socket.gaierror("no such host")

    with pytest.raises(InvalidUrlError):
        resolve_and_check("api.example.com", broken)


def test_resolver_non_ip_answer_fails_closed():
    with pytest.raises(InvalidUrlError):
        resolve_and_check("api.example.com", _resolver("CNAME-ISH-ANSWER"))


# -- DNS rebinding posture ------------------------------------------------------

def test_dns_rebinding_detected_between_checks():
    """First answer public, second answer loopback: a fresh resolve must
    reject (the guard re-resolves before EVERY request, not once)."""
    answers = iter([[PUBLIC], ["127.0.0.1"]])

    def rebinding(hostname):
        return next(answers)

    assert resolve_and_check("api.example.com", rebinding) == [PUBLIC]
    with pytest.raises(InvalidUrlError):
        resolve_and_check("api.example.com", rebinding)


def test_mixed_public_and_private_answers_rejected():
    """ANY private answer poisons the set (partial public ≠ safe)."""
    with pytest.raises(InvalidUrlError):
        resolve_and_check("api.example.com", _resolver(PUBLIC, "127.0.0.1"))


def test_rebinding_between_http_attempts(tmp_path):
    """A stream retry re-resolves: attempt 1 passes the guard (public) but
    the transport drops; attempt 2's fresh resolution is loopback → the
    transfer must ABORT (InvalidUrlError), never reach the private IP."""
    state = {"n": 0}
    answers = iter([[PUBLIC], ["127.0.0.1"]])

    def rebinding(hostname):
        state["resolved"] = next(answers)
        return state["resolved"]

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        raise httpx.ReadError("connection dropped")

    client = SafeHttpClient(ALLOWED, resolver=rebinding,
                            transport=httpx.MockTransport(handler))
    dest = tmp_path / "source.bin"
    with pytest.raises(InvalidUrlError):
        run(client.stream_to_file("https://api.example.com/media", dest,
                                  max_bytes=1024, chunk_size=64,
                                  max_attempts=2, backoff_seconds=0))
    # Both attempts were guard-checked; only attempt 1 hit the transport.
    assert state["n"] == 1
    assert not dest.exists()


# =============================================================================
# Redirect policy (manual, re-validated, bounded)
# =============================================================================

def _client(handler, **kw) -> SafeHttpClient:
    return SafeHttpClient(ALLOWED, transport=httpx.MockTransport(handler), **kw)


def test_redirect_to_allowed_host_followed():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, content=b"ok")

    client = _client(handler, resolver=_resolver(PUBLIC))
    response = run(client.get("https://api.example.com/redirect"))
    assert response.status_code == 200
    assert len(seen) == 2


def test_redirect_to_lookalike_host_rejected():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://api.example.com.evil.test/x"})

    client = _client(handler, resolver=_resolver(PUBLIC))
    with pytest.raises(InvalidUrlError):
        run(client.get("https://api.example.com/redirect"))
    assert len(seen) == 1  # the evil hop was never contacted


def test_redirect_to_private_ip_rejected():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    client = _client(handler, resolver=_resolver(PUBLIC))
    with pytest.raises(InvalidUrlError):
        run(client.get("https://api.example.com/meta"))
    assert len(seen) == 1


def test_redirect_to_private_host_rejected_at_destination_check():
    """First hop resolves public; the redirect TARGET resolves loopback."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "hop" in str(request.url):
            return httpx.Response(302, headers={"location": "https://target.example.test/x"})
        return httpx.Response(200, content=b"never")

    def resolver(hostname):
        if hostname == "api.example.com":
            return [PUBLIC]
        if hostname == "target.example.test":
            return ["127.0.0.1"]
        raise AssertionError(f"unexpected host {hostname}")

    client = SafeHttpClient(["api.example.com", "target.example.test"],
                             resolver=resolver,
                             transport=httpx.MockTransport(handler))
    with pytest.raises(InvalidUrlError):
        run(client.get("https://api.example.com/hop"))


def test_redirect_loop_bounded():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/again"})

    client = _client(handler, resolver=_resolver(PUBLIC), max_redirects=3)
    with pytest.raises(TransferInterruptedError) as exc:
        run(client.get("https://api.example.com/loop"))
    assert "too many redirects" in str(exc.value)


def test_post_303_switches_to_get():
    """RFC 9110: 301/302/303 turn non-GET into GET on follow."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        if len(seen) == 1:
            return httpx.Response(303, headers={"location": "/result"})
        return httpx.Response(200, content=b"done")

    client = _client(handler, resolver=_resolver(PUBLIC))
    response = run(client.request("POST", "https://api.example.com/submit"))
    assert response.status_code == 200
    assert seen[0][0] == "POST"
    assert seen[1][0] == "GET"


# =============================================================================
# Header policy (no arbitrary user-supplied headers)
# =============================================================================

def test_arbitrary_headers_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x")

    client = _client(handler, resolver=_resolver(PUBLIC))
    with pytest.raises(InvalidUrlError):
        run(client.get("https://api.example.com/x", headers={"X-Api-Key": "nope"}))
    with pytest.raises(InvalidUrlError):
        run(client.get("https://api.example.com/x", headers={"Cookie": "a=b"}))
    with pytest.raises(InvalidUrlError):
        run(client.get("https://api.example.com/x", headers={"Host": "evil"}))


def test_allowlisted_headers_pass_through():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["client_id"] = request.headers.get("client-id")
        seen["goog_key"] = request.headers.get("x-goog-api-key")
        return httpx.Response(200, content=b"x")

    client = _client(handler, resolver=_resolver(PUBLIC))
    run(client.get("https://api.example.com/x", headers={
        "Authorization": "Bearer abc", "Client-Id": "cid", "X-Goog-Api-Key": "k",
    }))
    assert seen["auth"] == "Bearer abc"
    assert seen["client_id"] == "cid"
    assert seen["goog_key"] == "k"


def test_base_headers_always_sent():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["accept"] = request.headers.get("accept")
        return httpx.Response(200, content=b"x")

    client = _client(handler, resolver=_resolver(PUBLIC),
                     base_headers={"Accept": "application/json"})
    run(client.get("https://api.example.com/x"))
    assert seen["accept"] == "application/json"


# =============================================================================
# Streaming transfer: bounded, cancellable, retryable, size-capped
# =============================================================================

def test_stream_chunks_and_reports_progress(tmp_path):
    body = b"A" * 300
    progress = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body,
                              headers={"content-length": str(len(body))})

    client = _client(handler, resolver=_resolver(PUBLIC))
    written = run(client.stream_to_file(
        "https://api.example.com/media", tmp_path / "s.bin",
        max_bytes=1024, chunk_size=64,
        progress=lambda w, t: progress.append((w, t)),
    ))
    assert written == 300
    assert (tmp_path / "s.bin").read_bytes() == body
    # Chunked: many progress points, each step bounded by chunk_size (64)
    # — observable proof the transfer never held the whole file in memory.
    assert len(progress) >= 4
    deltas = [progress[i][0] - progress[i - 1][0] for i in range(1, len(progress))]
    assert all(0 < d <= 64 for d in deltas)
    assert progress[-1] == (300, 300)


def test_stream_size_cap_aborts_and_cleans_up(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"B" * 500,
                              headers={"content-length": "500"})

    client = _client(handler, resolver=_resolver(PUBLIC))
    dest = tmp_path / "big.bin"
    with pytest.raises(TransferInterruptedError) as exc:
        run(client.stream_to_file("https://api.example.com/big", dest,
                                  max_bytes=100, chunk_size=64))
    assert "maximum size" in str(exc.value)
    assert not dest.exists()  # partial removed


def test_stream_cancellation_mid_transfer(tmp_path):
    calls = {"n": 0}
    state = {"cancel": False}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"C" * 1000,
                              headers={"content-length": "1000"})

    client = _client(handler, resolver=_resolver(PUBLIC))
    dest = tmp_path / "c.bin"

    def progress(written, total):
        calls["n"] += 1
        if calls["n"] == 3:
            state["cancel"] = True  # cancel after the 2nd chunk

    with pytest.raises(SourceCancelledError):
        run(client.stream_to_file("https://api.example.com/c", dest,
                                  max_bytes=1024, chunk_size=64,
                                  progress=progress,
                                  should_cancel=lambda: state["cancel"]))
    assert not dest.exists()


def test_stream_retryable_429_then_success(tmp_path):
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        if hits["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, content=b"good", headers={"content-length": "4"})

    client = _client(handler, resolver=_resolver(PUBLIC))
    written = run(client.stream_to_file("https://api.example.com/r", tmp_path / "r.bin",
                                        max_bytes=100, chunk_size=16,
                                        max_attempts=2, backoff_seconds=0))
    assert written == 4
    assert hits["n"] == 2


def test_stream_429_after_retries_is_rate_limited(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    client = _client(handler, resolver=_resolver(PUBLIC))
    with pytest.raises(RateLimitedError):
        run(client.stream_to_file("https://api.example.com/rl", tmp_path / "rl.bin",
                                  max_bytes=100, chunk_size=16,
                                  max_attempts=3, backoff_seconds=0))


def test_stream_non_retryable_404_immediate(tmp_path):
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return httpx.Response(404)

    client = _client(handler, resolver=_resolver(PUBLIC))
    with pytest.raises(TransferInterruptedError):
        run(client.stream_to_file("https://api.example.com/404", tmp_path / "n.bin",
                                  max_bytes=100, chunk_size=16,
                                  max_attempts=3, backoff_seconds=0))
    assert hits["n"] == 1  # no retry loop for permanent errors


def test_stream_500_retries_then_gives_up(tmp_path):
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return httpx.Response(500)

    client = _client(handler, resolver=_resolver(PUBLIC))
    with pytest.raises(TransferInterruptedError):
        run(client.stream_to_file("https://api.example.com/500", tmp_path / "e.bin",
                                  max_bytes=100, chunk_size=16,
                                  max_attempts=2, backoff_seconds=0))
    assert hits["n"] == 2


def test_stream_empty_body_produces_no_file(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    client = _client(handler, resolver=_resolver(PUBLIC))
    dest = tmp_path / "empty.bin"
    written = run(client.stream_to_file("https://api.example.com/empty", dest,
                                        max_bytes=100, chunk_size=16))
    assert written == 0
    assert not dest.exists()


def test_stream_retries_transport_errors(tmp_path):
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        if hits["n"] == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, content=b"ok", headers={"content-length": "2"})

    client = _client(handler, resolver=_resolver(PUBLIC))
    written = run(client.stream_to_file("https://api.example.com/t", tmp_path / "t.bin",
                                        max_bytes=100, chunk_size=16,
                                        max_attempts=2, backoff_seconds=0))
    assert written == 2
    assert hits["n"] == 2


# =============================================================================
# Secret redaction (spec section 9: never log tokens/signed URLs)
# =============================================================================

def test_redact_bearer_and_authorization():
    out = redact("request failed: authorization: Bearer sk_live_abcdef1234567890")
    assert "sk_live_abcdef1234567890" not in out
    assert "***" in out


def test_redact_token_shaped_fields():
    for message in (
        "exchange failed access_token=abcdef1234567890abcdef",
        "refresh_token=abcdef1234567890abcdef in body",
        "client_secret=0123456789abcdef leaked",
        "Cookie: sessionid=abcdef123456",
        "Set-Cookie: x=abcdef123456",
        "password=hunter2secret123 in url",
    ):
        out = redact(message)
        assert "abcdef1234567890abcdef" not in (out or "")
        assert "0123456789abcdef" not in (out or "")


def test_redact_signed_storage_urls():
    url = "https://sb.example.com/storage/v1/object/sign?token=abcdef1234567890"
    out = redact(f"download {url}")
    assert "abcdef1234567890" not in (out or "")


def test_redact_leaves_plain_messages_alone():
    assert redact("provider returned 429") == "provider returned 429"
    assert redact(None) is None
