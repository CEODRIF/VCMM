"""TASK 014 — direct video file URL provider tests (universal link
ingestion, the direct-URL class).

All deterministic: httpx.MockTransport (no network), injected resolver
(deterministic offline DNS), real ffprobe for the byte-validation step.

Covers the security contract of the class:
  - shape claim (public http(s) + .mp4/.webm/.mov; platform hosts never);
  - SSRF matrix: loopback / private / link-local / literal IPs refused;
  - DNS rebinding: every answer must be public, rechecked per request/hop;
  - redirects: chain re-validated per hop; private destinations refused at
    EVERY hop; public CDN hops permitted; bounded; stream rejects a
    surprise redirect;
  - size cap enforced DURING the transfer (partial file removed);
  - total wall-clock deadline bounds slow-drip sources;
  - HEAD probe with bounded Range-GET fallback (405/501);
  - status mapping (404/401/403/429/5xx) to typed ProviderCheck outcomes;
  - real media validation: bytes that are not a video NEVER reach storage;
  - no secret/signed-URL leakage in error messages;
  - optional operator host allowlist.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.providers.direct_url import DirectUrlConfig, DirectUrlProvider, classify_video_url
from app.providers.errors import InvalidUrlError, TransferInterruptedError
from app.validation.media import MediaValidationError

PUBLIC = "93.184.216.34"
PUBLIC_2 = "93.184.216.35"
LOOPBACK = "127.0.0.1"
PRIVATE = "10.1.2.3"
LINK_LOCAL = "169.254.169.254"


def run(coro):
    return asyncio.run(coro)


def _resolver(*answers_by_host: tuple[str, tuple[str, ...]]):
    """hostname → addresses map resolver (deterministic offline DNS)."""
    table = {host: ips for host, ips in answers_by_host}

    def resolve(hostname: str):
        return table.get(hostname, ("NXDOMAIN",))

    return resolve


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _provider(
    ffprobe_bin: str,
    handler,
    resolver=None,
    config: DirectUrlConfig | None = None,
) -> DirectUrlProvider:
    return DirectUrlProvider(
        ffprobe_bin,
        config=config or DirectUrlConfig(),
        resolver=resolver,
        http_client=_client(handler),
    )


FILE_URL = "https://cdn.example.com/streams/vod.mp4"
FILE_BYTES_KEY = b"MP4-BYTES"


def _video_handler(payload: bytes, *, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(status, headers={"content-length": str(len(payload))})
        return httpx.Response(status, content=payload,
                              headers={"content-type": "video/mp4"})

    return handler


# =============================================================================
# claim / classification (no network)
# =============================================================================

def test_claim_matrix_is_shape_only():
    assert classify_video_url("https://a.example.com/v.mp4") == ".mp4"
    assert classify_video_url("https://a.example.com/deep/path/V.WEBM") == ".webm"
    assert classify_video_url("https://a.example.com/rec.mov?sig=signed-token") == ".mov"
    assert classify_video_url("https://a.example.com/v.mp4?e=123#frag") == ".mp4"
    # platform hosts always go to their platform adapters
    assert classify_video_url("https://www.youtube.com/watch?v=x") is None
    assert classify_video_url("https://youtu.be/x") is None
    assert classify_video_url("https://twitch.tv/videos/1") is None
    assert classify_video_url("https://clips.twitch.tv/SomeClip") is None
    # non-video / non-http / bad shapes
    assert classify_video_url("https://a.example.com/v.m3u8") is None
    assert classify_video_url("https://a.example.com/page.html") is None
    assert classify_video_url("https://a.example.com:8443/v.mp4") is None
    assert classify_video_url("ftp://a.example.com/v.mp4") is None
    assert classify_video_url("https://user:pw@a.example.com/v.mp4") is None
    assert classify_video_url("not a url") is None


# =============================================================================
# validate — SSRF matrix (permanent, typed — never a ProviderCheck)
# =============================================================================

@pytest.mark.parametrize(
    "url,answers",
    [
        ("https://127.0.0.1/v.mp4", [(LOOPBACK, (LOOPBACK,))]),          # loopback literal
        ("https://10.0.0.1/v.mp4", [(PRIVATE, (PRIVATE,))]),              # private literal
        ("https://169.254.169.254/v.mp4", [(LINK_LOCAL, (LINK_LOCAL,))]), # metadata svc
        ("https://[::1]/v.mp4", [(None, ())]),                            # ipv6 loopback
        ("https://localhost/v.mp4", [("localhost", (LOOPBACK,))]),        # loopback name
        # rebinding: ONE private answer poisons the whole name
        ("https://mixed.example.com/v.mp4",
         [("mixed.example.com", (PUBLIC, PRIVATE))]),
        # decimal/octal IP encodings
        ("https://2130706433/v.mp4", [(None, ())]),                       # 127.0.0.1 decimal
        ("https://0177.0.0.1/v.mp4", [(None, ())]),                       # 127.0.0.1 octal
        ("https://a.example.com:8443/v.mp4", [("a.example.com", (PUBLIC,))]),  # port
        ("https://user:pw@a.example.com/v.mp4", [("a.example.com", (PUBLIC,))]),  # creds
        ("gopher://a.example.com/v.mp4", [("a.example.com", (PUBLIC,))]),  # scheme
        ("https://a.example.com/v.mp4\r\nx", [("a.example.com", (PUBLIC,))]),     # control chars
    ],
    ids=[
        "loopback-literal", "private-literal", "link-local", "ipv6-loopback",
        "loopback-name", "rebinding-private-answer", "decimal-ip", "octal-ip",
        "non-standard-port", "credentials", "scheme", "control-chars",
    ],
)
def test_validate_refuses_unsafe_destinations_permanently(
    ffprobe_bin, url, answers
):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must NOT be reached for an unsafe URL")

    p = _provider(ffprobe_bin, handler, resolver=_resolver(*answers))
    with pytest.raises(InvalidUrlError):
        run(p.validate(url))


def test_validate_dns_failure_reports_unavailable_not_crash(ffprobe_bin):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must NOT be reached for an unresolvable host")

    p = _provider(ffprobe_bin, handler, resolver=_resolver())  # nothing resolvable
    with pytest.raises(InvalidUrlError):
        run(p.validate(FILE_URL))


# =============================================================================
# validate — status mapping (real HTTP, mocked transport)
# =============================================================================

def test_validate_reports_available_with_declared_size(ffprobe_bin, synthetic_videos):
    payload = synthetic_videos["valid"].read_bytes()
    p = _provider(ffprobe_bin, _video_handler(payload),
                  resolver=_resolver(("cdn.example.com", (PUBLIC,))))
    check = run(p.validate(FILE_URL))
    assert check.status == "available"
    assert check.metadata is not None
    assert check.metadata.provider_specific["declared_bytes"] == len(payload)
    assert check.metadata.provider_specific["file_extension"] == "mp4"


def test_validate_status_mapping(ffprobe_bin):
    payload = b"x" * 1024
    resolver = _resolver(("cdn.example.com", (PUBLIC,)))
    for status, expected in [
        (404, "deleted"),
        (410, "deleted"),
        (401, "restricted"),
        (403, "restricted"),
        (429, "rate_limited"),
        (503, "unavailable"),
    ]:
        p = _provider(ffprobe_bin, _video_handler(payload, status=status), resolver=resolver)
        check = run(p.validate(FILE_URL))
        assert check.status == expected, (status, check)


def test_validate_head_405_falls_back_to_bounded_range_get(ffprobe_bin, synthetic_videos):
    """HEAD refused (405) → exactly one bounded GET with Range: bytes=0-0."""
    payload = synthetic_videos["valid"].read_bytes()
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.headers.get("range", "")))
        if request.method == "HEAD":
            return httpx.Response(405)
        return httpx.Response(206, content=payload[:1],
                              headers={"content-range": f"bytes 0-0/{len(payload)}"})

    p = _provider(ffprobe_bin, handler, resolver=_resolver(("cdn.example.com", (PUBLIC,))))
    check = run(p.validate(FILE_URL))
    assert check.status == "available"
    assert [m for m, _ in seen] == ["HEAD", "GET"]
    assert seen[1][1] == "bytes=0-0"


def test_validate_is_reachability_only_byte_truth_is_materialize(ffprobe_bin):
    """Design: the probe checks reachability/status; an HTML page that
    answers 200 is reported reachable — and is REJECTED by the FFprobe
    byte validation in materialize (covered separately). The pipeline
    never claims a download that is not a real video."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-type": "text/html"})
        return httpx.Response(200, content=b"<html></html>",
                              headers={"content-type": "text/html"})

    p = _provider(ffprobe_bin, handler, resolver=_resolver(("cdn.example.com", (PUBLIC,))))
    check = run(p.validate(FILE_URL))
    assert check.status == "available"  # reachable; bytes validated later


# =============================================================================
# materialize — redirects (re-validated per hop)
# =============================================================================

def test_materialize_follows_public_redirect_chain(ffprobe_bin, synthetic_videos):
    """user host → CDN host (public): allowed; the stream hits the FINAL host."""
    payload = synthetic_videos["valid"].read_bytes()
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", "")
        hits.append(host)
        if request.url.host == "user.example.com":
            return httpx.Response(302, headers={"location": "https://cdn.example.com/v.mp4"})
        return httpx.Response(200, content=payload, headers={"content-length": str(len(payload))})

    resolver = _resolver(
        ("user.example.com", (PUBLIC,)), ("cdn.example.com", (PUBLIC_2,))
    )
    p = _provider(ffprobe_bin, handler, resolver=resolver)
    destination = run(p.materialize("https://user.example.com/start.mp4", Path("/tmp/t14-a")))
    assert destination.is_file()
    assert destination.read_bytes() == payload
    # both hosts were actually contacted, in order
    assert hits and hits[0] == "user.example.com"
    assert "cdn.example.com" in hits
    destination.unlink(missing_ok=True)


def test_materialize_refuses_redirect_to_private_destination(ffprobe_bin):
    """A public first hop that redirects to loopback = SSRF attempt → refused."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "user.example.com":
            return httpx.Response(302, headers={"location": "https://127.0.0.1/secret.mp4"})
        raise AssertionError("the private destination must never be contacted")

    resolver = _resolver(
        ("user.example.com", (PUBLIC,)), ("127.0.0.1", (LOOPBACK,))
    )
    p = _provider(ffprobe_bin, handler, resolver=resolver)
    with pytest.raises(InvalidUrlError):
        run(p.materialize("https://user.example.com/start.mp4", Path("/tmp/t14-b")))
    Path("/tmp/t14-b").unlink(missing_ok=True)


def test_materialize_redirect_loop_is_bounded(ffprobe_bin):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://a.example.com/loop.mp4"})

    p = _provider(
        ffprobe_bin, handler,
        resolver=_resolver(("a.example.com", (PUBLIC,))),
        config=DirectUrlConfig(max_redirects=3),
    )
    with pytest.raises(TransferInterruptedError):
        run(p.materialize("https://a.example.com/loop.mp4", Path("/tmp/t14-c")))
    Path("/tmp/t14-c").unlink(missing_ok=True)


def test_materialize_rejects_surprise_redirect_during_stream(ffprobe_bin, tmp_path):
    """HEAD says final; GET redirects → fail loudly (never follow unvalidated)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": "8"})
        return httpx.Response(302, headers={"location": "https://b.example.com/x.mp4"})

    p = _provider(ffprobe_bin, handler,
                  resolver=_resolver(("a.example.com", (PUBLIC,))))
    with pytest.raises(TransferInterruptedError) as exc:
        run(p.materialize("https://a.example.com/v.mp4", tmp_path / "out"))
    assert "redirect" in str(exc.value)


# =============================================================================
# materialize — budgets (size cap, deadline, status)
# =============================================================================

def test_materialize_enforces_size_cap_and_removes_partial(ffprobe_bin, tmp_path):
    big = b"x" * (2 * 1024 * 1024)
    p = _provider(
        ffprobe_bin, _video_handler(big),
        resolver=_resolver(("cdn.example.com", (PUBLIC,))),
        config=DirectUrlConfig(max_source_bytes=1024 * 1024),
    )
    dest = tmp_path / "out"
    with pytest.raises(TransferInterruptedError) as exc:
        run(p.materialize(FILE_URL, dest))
    assert "maximum size" in str(exc.value)
    assert list(dest.glob("source*")) == []  # partial file removed


class SlowStream(httpx.AsyncByteStream):
    """Slow-drip body: sleeps between small chunks (deadline probe)."""

    def __init__(self, payload: bytes, chunk: int = 4096, delay: float = 0.05) -> None:
        self._payload, self._chunk, self._delay = payload, chunk, delay
        self._idx = 0

    async def aclose(self) -> None:
        pass

    async def __aiter__(self):
        while self._idx < len(self._payload):
            await asyncio.sleep(self._delay)
            piece = self._payload[self._idx : self._idx + self._chunk]
            self._idx += self._chunk
            yield piece


def test_materialize_enforces_total_deadline_on_slow_drip(ffprobe_bin, tmp_path):
    payload = b"x" * (1024 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(payload))})
        return httpx.Response(200, stream=SlowStream(payload, delay=0.05))

    p = _provider(
        ffprobe_bin, handler,
        resolver=_resolver(("cdn.example.com", (PUBLIC,))),
        config=DirectUrlConfig(transfer_deadline_seconds=0.02),
    )
    dest = tmp_path / "out"
    with pytest.raises(TransferInterruptedError) as exc:
        run(p.materialize(FILE_URL, dest))
    assert "time budget" in str(exc.value)
    assert list(dest.glob("source*")) == []


def test_materialize_maps_transfer_statuses_to_typed_errors(ffprobe_bin, tmp_path):
    p = _provider(ffprobe_bin, _video_handler(b"x", status=403),
                  resolver=_resolver(("cdn.example.com", (PUBLIC,))))
    with pytest.raises(TransferInterruptedError) as exc:
        run(p.materialize(FILE_URL, tmp_path / "out"))
    assert "403" in str(exc.value)


def test_materialize_retries_transient_then_succeeds(ffprobe_bin, synthetic_videos, tmp_path):
    payload = synthetic_videos["valid"].read_bytes()
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(payload))})
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=payload)

    p = _provider(
        ffprobe_bin, handler,
        resolver=_resolver(("cdn.example.com", (PUBLIC,))),
        config=DirectUrlConfig(max_attempts=2, backoff_seconds=0.01),
    )
    destination = run(p.materialize(FILE_URL, tmp_path / "out"))
    assert destination.read_bytes() == payload
    destination.unlink(missing_ok=True)


# =============================================================================
# materialize — byte truth (FFprobe), not URL shape
# =============================================================================

def test_materialize_rejects_bytes_that_are_not_video(ffprobe_bin, synthetic_videos, tmp_path):
    """An HTML page wearing a .mp4 extension must fail BEFORE storage."""
    page = b"<html><body>definitely not a video</body></html>"
    p = _provider(ffprobe_bin, _video_handler(page),
                  resolver=_resolver(("cdn.example.com", (PUBLIC,))))
    dest = tmp_path / "out"
    with pytest.raises(MediaValidationError):
        run(p.materialize(FILE_URL, dest))
    assert list(dest.glob("source*")) == []


def test_materialize_happy_path_probes_real_bytes(ffprobe_bin, synthetic_videos, tmp_path):
    payload = synthetic_videos["valid"].read_bytes()
    p = _provider(ffprobe_bin, _video_handler(payload),
                  resolver=_resolver(("cdn.example.com", (PUBLIC,))))
    destination = run(p.materialize(FILE_URL, tmp_path / "out"))
    assert destination.is_file()
    assert destination.read_bytes() == payload
    assert destination.suffix == ".mp4"
    destination.unlink(missing_ok=True)


def test_materialize_honors_cancel_probe(ffprobe_bin, synthetic_videos, tmp_path):
    payload = synthetic_videos["valid"].read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(payload))})
        return httpx.Response(200, content=payload)

    p = _provider(ffprobe_bin, handler,
                  resolver=_resolver(("cdn.example.com", (PUBLIC,))))
    from app.providers.errors import SourceCancelledError

    with pytest.raises(SourceCancelledError):
        run(p.materialize(FILE_URL, tmp_path / "out",
                          should_cancel=lambda: True))
    assert list((tmp_path / "out").glob("source*")) == []


# =============================================================================
# config: disabled switch + operator host allowlist
# =============================================================================

def test_disabled_provider_is_not_configured(ffprobe_bin):
    p = DirectUrlProvider(ffprobe_bin, config=DirectUrlConfig(enabled=False))
    assert p.is_configured is False
    p2 = DirectUrlProvider(ffprobe_bin, config=DirectUrlConfig(enabled=True))
    assert p2.is_configured is True


def test_operator_host_allowlist_restricts_destinations(ffprobe_bin, synthetic_videos):
    payload = synthetic_videos["valid"].read_bytes()
    cfg = DirectUrlConfig(host_allowlist=("storage.example.com",))
    p = _provider(
        ffprobe_bin, _video_handler(payload),
        resolver=_resolver(("other.example.com", (PUBLIC,))),
        config=cfg,
    )
    with pytest.raises(InvalidUrlError):
        run(p.validate("https://other.example.com/v.mp4"))
    p2 = _provider(
        ffprobe_bin, _video_handler(payload),
        resolver=_resolver(("storage.example.com", (PUBLIC,))),
        config=cfg,
    )
    check = run(p2.validate("https://storage.example.com/v.mp4"))
    assert check.status == "available"


# =============================================================================
# secret hygiene
# =============================================================================

def test_error_messages_never_leak_signed_url_or_secrets(ffprobe_bin, tmp_path):
    secret = "SIGNED-URL-TOKEN-DO-NOT-LEAK"
    url = f"https://cdn.example.com/v.mp4?token={secret}"
    p = _provider(ffprobe_bin, _video_handler(b"x" * 10, status=403),
                  resolver=_resolver(("cdn.example.com", (PUBLIC,))))
    with pytest.raises(TransferInterruptedError) as exc:
        run(p.materialize(url, tmp_path / "out"))
    assert secret not in str(exc.value)
    assert "token=" not in str(exc.value)


def test_unhandled_transport_error_reports_unavailable_not_raw(ffprobe_bin):
    """A raw transport error (connection refused) surfaces as a SAFE,
    retryable 'unavailable' check — the raw exception is never leaked."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    p = _provider(ffprobe_bin, handler,
                  resolver=_resolver(("cdn.example.com", (PUBLIC,))))
    check = run(p.validate(FILE_URL))
    assert check.status == "unavailable"
    assert "ConnectError" not in (check.reason or "")
