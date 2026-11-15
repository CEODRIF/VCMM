"""TASK 015 — Gemini provider abstraction (deterministic unit tests).

httpx.MockTransport only: no network, no real keys. Covers: success +
parse, malformed response, transient vs permanent failure classes, bounded
retries, key rotation + per-key cooldown, the no-key-leak guarantee, and
the File-API upload/video pass shapes.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from app.ai.gemini import (
    GeminiClient,
    GeminiConfig,
    GeminiNotConfiguredError,
    GeminiPermanentError,
    GeminiRateLimitedError,
    GeminiTransientError,
    KeyPool,
    parse_assessment,
)

HOST = "gemini.test"
BASE = f"https://{HOST}/v1beta"
MODEL = "test-model"
KEY_A = "key-a-sentinel"
KEY_B = "key-b-sentinel"


def run(coro: Any) -> Any:
    return asyncio.new_event_loop().run_until_complete(coro)


def _config(**over: Any) -> GeminiConfig:
    base: dict[str, Any] = dict(
        api_keys=(KEY_A, KEY_B),
        model=MODEL,
        api_base=BASE,
        timeout_seconds=5.0,
        max_retries=2,
        backoff_seconds=0.01,
        cooldown_seconds=60.0,
        max_prompt_chars=8000,
    )
    base.update(over)
    return GeminiConfig(**base)


def assessment_json(**over: Any) -> str:
    data = {
        "scene_description": "a clutch teamfight",
        "event_type": "combat",
        "event_importance": 0.87,
        "candidate_clip": True,
        "reasoning": "measured energy + reactions",
    }
    data.update(over)
    return json.dumps(data)


def make_client(handler: Any, **cfg_over: Any) -> tuple[GeminiClient, list, list]:
    """Client + captured request states: (client, requests, sleeps)."""
    state: dict[str, Any] = {"requests": [], "sleeps": []}
    transport = httpx.MockTransport(_wrap(handler, state))
    http = httpx.AsyncClient(transport=transport)

    async def fake_sleep(s: float) -> None:
        state["sleeps"].append(s)

    client = GeminiClient(_config(**cfg_over), client=http, sleep=fake_sleep)
    return client, state["requests"], state["sleeps"]


def _wrap(handler: Any, state: dict[str, Any]) -> Any:
    def wrapped(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        return handler(request)

    return wrapped


def close(client: GeminiClient) -> None:
    run(client.close())


# --------------------------------------------------------------------------
# Key pool (deterministic clock)
# --------------------------------------------------------------------------

class TestKeyPool:
    def test_round_robin_and_cooldown_skip(self):
        t = [0.0]
        pool = KeyPool(("a", "b"), 10.0, clock=lambda: t[0])
        assert pool.pick() == "a"
        pool.report_rate_limited("a")  # a in cooldown until t=10
        assert pool.pick() == "b"
        assert pool.pick() == "b"  # still only b eligible
        t[0] = 11.0
        assert pool.pick() in ("a", "b")
        pool.report_rate_limited("a")
        pool.report_rate_limited("b")
        assert pool.pick() is None  # ALL in cooldown

    def test_permanent_poisons_key_for_the_run(self):
        t = [0.0]
        pool = KeyPool(("a", "b"), 10.0, clock=lambda: t[0])
        pool.report_permanent("a")
        picked = {pool.pick() for _ in range(4)}
        assert picked == {"b"}

    def test_success_clears_cooldown(self):
        t = [0.0]
        pool = KeyPool(("a",), 10.0, clock=lambda: t[0])
        pool.report_rate_limited("a")
        assert pool.pick() is None
        pool.report_success("a")
        assert pool.pick() == "a"


# --------------------------------------------------------------------------
# generate_json: success / parse / failure classes
# --------------------------------------------------------------------------

class TestGenerateJson:
    def test_success_parses_and_never_puts_key_in_url(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("x-goog-api-key") in (KEY_A, KEY_B)
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [{"text": assessment_json()}]}}]
            })

        client, requests, _ = make_client(handler)
        try:
            out = run(client.generate_json("prompt"))
            assert out["status"] == "ok"
            assert out["event_importance"] == 0.87
            assert out["candidate_clip"] is True
            for r in requests:
                assert KEY_A not in str(r.url) and KEY_B not in str(r.url)
                assert r.url.path == f"/v1beta/models/{MODEL}:generateContent"
        finally:
            close(client)

    def test_malformed_json_is_permanent(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [{"text": "not json at all"}]}}]
            })

        client, requests, _ = make_client(handler)
        try:
            with pytest.raises(GeminiPermanentError, match="non-JSON"):
                run(client.generate_json("prompt"))
            # permanent-class parse failure: the request itself succeeded;
            # the retry budget is for transport/quota classes — exactly one
            # attempt was made for THIS response.
            assert len(requests) == 1
        finally:
            close(client)

    def test_unexpected_shape_is_permanent(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"no_candidates": True})

        client, _, _ = make_client(handler)
        try:
            with pytest.raises(GeminiPermanentError, match="unexpected"):
                run(client.generate_json("prompt"))
        finally:
            close(client)

    def test_http_401_is_permanent_no_retry(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"message": "bad key"}})

        client, requests, sleeps = make_client(handler)
        try:
            with pytest.raises(GeminiPermanentError, match="401"):
                run(client.generate_json("prompt"))
            assert len(requests) == 1  # NO retry on permanent
            assert sleeps == []
        finally:
            close(client)

    def test_http_500_retries_then_succeeds(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(500)
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [{"text": assessment_json()}]}}]
            })

        client, requests, sleeps = make_client(handler)
        try:
            out = run(client.generate_json("prompt"))
            assert out["status"] == "ok"
            assert len(requests) == 2
            assert len(sleeps) == 1 and sleeps[0] > 0  # bounded backoff
        finally:
            close(client)

    def test_http_429_rotates_to_second_key(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.headers.get("x-goog-api-key") == KEY_A:
                return httpx.Response(429, json={"error": {"message": "quota"}})
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [{"text": assessment_json()}]}}]
            })

        client, requests, _ = make_client(handler)
        try:
            out = run(client.generate_json("prompt"))
            assert out["status"] == "ok"
            keys_used = [r.headers.get("x-goog-api-key") for r in requests]
            assert keys_used == [KEY_A, KEY_B]  # rotation after cooldown
        finally:
            close(client)

    def test_all_keys_rate_limited_raises_bounded(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": {"message": "quota"}})

        client, requests, sleeps = make_client(handler, max_retries=1,
                                                cooldown_seconds=60.0)
        try:
            with pytest.raises(GeminiRateLimitedError):
                run(client.generate_json("prompt"))
            # bounded: max_retries+1 attempts total, no infinite loop
            assert len(requests) <= 2
        finally:
            close(client)

    def test_network_error_retries_bounded(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        client, _, sleeps = make_client(handler, max_retries=1)
        try:
            with pytest.raises(GeminiTransientError):
                run(client.generate_json("prompt"))
            assert len(sleeps) <= 1
        finally:
            close(client)

    def test_unconfigured_raises_without_network(self):
        state: dict[str, Any] = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            state["n"] += 1
            return httpx.Response(200, json={})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = GeminiClient(GeminiConfig(api_keys=(), api_base=BASE), client=http)
        try:
            with pytest.raises(GeminiNotConfiguredError):
                run(client.generate_json("prompt"))
            assert state["n"] == 0
            assert client.is_configured is False
        finally:
            close(client)


# --------------------------------------------------------------------------
# parse_assessment validation/clamping
# --------------------------------------------------------------------------

class TestParseAssessment:
    def test_valid(self):
        out = parse_assessment(assessment_json())
        assert out == {
            "status": "ok", "scene_description": "a clutch teamfight",
            "event_type": "combat", "event_importance": 0.87,
            "candidate_clip": True, "reasoning": "measured energy + reactions",
        }

    def test_markdown_fence_tolerated(self):
        out = parse_assessment("```json\n" + assessment_json() + "\n```")
        assert out["status"] == "ok"

    def test_importance_out_of_range_defaults_neutral(self):
        assert parse_assessment(assessment_json(event_importance=2.5))["event_importance"] == 0.5
        assert parse_assessment(assessment_json(event_importance="high"))["event_importance"] == 0.5
        assert parse_assessment(assessment_json(event_importance=True))["event_importance"] == 0.5

    def test_event_type_whitelist(self):
        assert parse_assessment(assessment_json(event_type="viral"))["event_type"] == "miscellaneous"
        assert parse_assessment(assessment_json(event_type="victory"))["event_type"] == "victory"

    def test_candidate_coerced_to_bool(self):
        assert parse_assessment(assessment_json(candidate_clip="yes"))["candidate_clip"] is True
        assert parse_assessment(assessment_json(candidate_clip=0))["candidate_clip"] is False

    def test_non_object_rejected(self):
        with pytest.raises(GeminiPermanentError):
            parse_assessment("[1,2,3]")

    def test_fence_without_json_marker(self):
        with pytest.raises(GeminiPermanentError):
            parse_assessment("```\ngarbage\n```")


# --------------------------------------------------------------------------
# File API (upload / video pass) — shape + honesty, mocked
# --------------------------------------------------------------------------

def test_upload_video_happy_path(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * (3 * 1024 * 1024 + 123))  # forces 2 chunks

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and "/upload/v1beta/files" in url:
            return httpx.Response(200, json={
                "name": "files/abc123",
                "upload_uri": f"{BASE}/upload/files/abc123",
            })
        if request.method == "PUT":
            return httpx.Response(200)
        if request.method == "GET" and "/files/files%2Fabc123" in url or \
           request.method == "GET" and "/files/" in url:
            return httpx.Response(200, json={"file": {"state": "ACTIVE"}})
        return httpx.Response(404)

    state: dict[str, Any] = {"requests": []}

    def wrapped(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        return handler(request)

    async def fake_sleep(_s: float) -> None:
        return None

    client = GeminiClient(_config(), client=httpx.AsyncClient(
        transport=httpx.MockTransport(wrapped)), sleep=fake_sleep)
    try:
        name = run(client.upload_video(video))
        assert name == "files/abc123"
        puts = [r for r in state["requests"] if r.method == "PUT"]
        assert len(puts) == 2  # bounded 2MB chunks
        assert sum(len(r.content) for r in puts) == video.stat().st_size
    finally:
        close(client)


def test_upload_video_failed_state_is_permanent(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"y" * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={
                "name": "files/x", "upload_uri": f"{BASE}/upload/files/x"})
        if request.method == "PUT":
            return httpx.Response(200)
        return httpx.Response(200, json={"file": {"state": "FAILED"}})

    async def fake_sleep(_s: float) -> None:
        return None

    client = GeminiClient(_config(), client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)), sleep=fake_sleep)
    try:
        with pytest.raises(GeminiPermanentError, match="processing failed"):
            run(client.upload_video(video))
    finally:
        close(client)


def test_understand_video_sends_file_part(tmp_path):
    state: dict[str, Any] = {"body": None}

    def handler(request: httpx.Request) -> httpx.Response:
        state["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "the video summary"}]}}]
        })

    async def fake_sleep(_s: float) -> None:
        return None

    client = GeminiClient(_config(), client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)), sleep=fake_sleep)
    try:
        out = run(client.understand_video("files/abc", "analyze"))
        assert out == "the video summary"
        parts = state["body"]["contents"][0]["parts"]
        assert parts[0]["file_data"]["file_uri"] == "gs://files/abc"
        assert parts[0]["file_data"]["mime_type"] == "video/mp4"
    finally:
        close(client)
