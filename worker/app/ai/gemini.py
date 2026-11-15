"""TASK 015 — Gemini provider abstraction (semantic understanding only).

Product rules this module enforces:

- **Multi-key, workload distribution**: keys come from the environment
  (``WORKER_GEMINI_API_KEYS`` comma list, legacy ``WORKER_GEMINI_API_KEY``).
  Keys are rotated round-robin with PER-KEY cooldowns after rate-limit /
  quota errors. Multiple keys share the project quota — this is for
  distribution and availability, NOT a quota bypass (documented).
- **Secrets**: a key travels ONLY in the ``x-goog-api-key`` header of
  allowlisted API requests; it is never logged, never placed in a URL,
  and never echoed in an error message.
- **Bounded retries**: transient failures (5xx / network / timeout) and
  rate limits (429 / quota 403) retry with exponential backoff across
  keys, bounded by ``max_retries``. PERMANENT errors (400 / 401 / 404)
  raise immediately — never an infinite retry loop.
- **Model via config**: ``WORKER_GEMINI_MODEL`` selects the model string;
  nothing here assumes a specific model name.
- **Responsibility boundary**: Gemini NEVER downloads or renders video.
  This client performs (a) per-window text-semantic assessment (the
  full-coverage mechanism) and (b) the optional File-API video pass
  (upload → poll → generateContent). Video understanding is enrichment
  and context — it is never accepted as proof of timeline coverage (the
  window tile check in :mod:`app.ai.timeline` is the proof).
- **Unconfigured = honest degradation**: with no keys, ``is_configured``
  is False and every call raises :class:`GeminiNotConfiguredError`
  without touching the network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

logger = logging.getLogger("clipforge.ai.gemini")

DEFAULT_API_BASE = "https://generativelanguage.googleapis.com/v1beta"


# --------------------------------------------------------------------------
# Errors (typed: permanent vs transient vs rate-limited — the caller's
# retry decision derives from the TYPE, never from message parsing)
# --------------------------------------------------------------------------

class GeminiError(RuntimeError):
    """Base for all Gemini provider failures (safe message, no keys)."""


class GeminiNotConfiguredError(GeminiError):
    """No API keys configured — honest unavailability, no network call."""


class GeminiPermanentError(GeminiError):
    """400/401/404-class failures: retrying will not help."""


class GeminiRateLimitedError(GeminiError):
    """429 / quota exhaustion: cooldown applies; a DIFFERENT key may
    still have budget."""


class GeminiTransientError(GeminiError):
    """5xx / network / timeout: bounded exponential-backoff retry."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class GeminiConfig:
    api_keys: tuple[str, ...] = ()
    model: str = "gemini-2.0-flash"
    api_base: str = DEFAULT_API_BASE
    timeout_seconds: float = 60.0
    max_retries: int = 2          # transient/rate-limited retry budget
    backoff_seconds: float = 1.5  # exponential base
    cooldown_seconds: float = 120.0  # per-key cooldown after rate limit
    max_prompt_chars: int = 8000  # per-request text bound (cost control)
    file_poll_attempts: int = 60  # File API ACTIVE-wait budget
    file_poll_interval: float = 2.0


def gemini_config_from_settings(settings: Any) -> GeminiConfig:
    """Builds the config from worker Settings (env-backed). Empty key
    env vars mean 'not configured' (the settings validators normalize
    them to None)."""
    keys: list[str] = []
    raw_list = getattr(settings, "gemini_api_keys", None)
    if raw_list:
        keys.extend(k.strip() for k in str(raw_list).split(",") if k.strip())
    single = getattr(settings, "gemini_api_key", None)
    if single and single not in keys:
        keys.append(single)
    return GeminiConfig(
        api_keys=tuple(keys),
        model=str(getattr(settings, "gemini_model", "gemini-2.0-flash")),
        api_base=str(getattr(settings, "gemini_api_base", DEFAULT_API_BASE)),
        timeout_seconds=float(getattr(settings, "gemini_timeout_seconds", 60.0)),
        max_retries=int(getattr(settings, "gemini_max_retries", 2)),
        backoff_seconds=float(getattr(settings, "gemini_backoff_seconds", 1.5)),
        cooldown_seconds=float(getattr(settings, "gemini_cooldown_seconds", 120.0)),
        max_prompt_chars=int(getattr(settings, "gemini_max_prompt_chars", 8000)),
        file_poll_attempts=int(getattr(settings, "gemini_file_poll_attempts", 60)),
        file_poll_interval=float(getattr(settings, "gemini_file_poll_interval", 2.0)),
    )


# --------------------------------------------------------------------------
# Key pool: round-robin rotation + per-key cooldowns
# --------------------------------------------------------------------------

class KeyPool:
    """Deterministic key selection (injectable clock for tests).

    A key in cooldown is skipped; if ALL keys are in cooldown the
    caller gets a rate-limited error (bounded wait, never a hang)."""

    def __init__(self, keys: tuple[str, ...], cooldown_seconds: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._keys = tuple(keys)
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._until: dict[str, float] = {}
        self._cursor = 0

    @property
    def size(self) -> int:
        return len(self._keys)

    def pick(self) -> str | None:
        """Next eligible key (round-robin among non-cooldown keys)."""
        if not self._keys:
            return None
        now = self._clock()
        for offset in range(len(self._keys)):
            key = self._keys[(self._cursor + offset) % len(self._keys)]
            if self._until.get(key, 0.0) <= now:
                self._cursor = (self._cursor + offset + 1) % len(self._keys)
                return key
        return None

    def report_success(self, key: str) -> None:
        self._until.pop(key, None)

    def report_rate_limited(self, key: str) -> None:
        self._until[key] = self._clock() + self._cooldown

    def report_permanent(self, key: str) -> None:
        # A key rejected outright (401) is poisoned for the whole run:
        # it will not work later in the same job either.
        self._until[key] = float("inf")


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

#: The window-assessment JSON contract (the prompt constrains to this).
ASSESSMENT_EVENT_TYPES = (
    "gameplay", "highlight", "combat", "victory", "defeat", "reaction",
    "story", "intro", "outro", "transition", "miscellaneous",
)

ASSESSMENT_SYSTEM = (
    "You are ClipForge's video-segment analyst. You receive ONE window of a "
    "longer video with the transcribed speech and the measured local "
    "signals (audio/visual event summaries). Assess it for short-form clip "
    "potential. Return ONLY valid JSON with exactly these keys: "
    "\"scene_description\" (string, 1-3 sentences), "
    f"\"event_type\" (one of: {', '.join(ASSESSMENT_EVENT_TYPES)}), "
    "\"event_importance\" (number 0.0-1.0), "
    "\"candidate_clip\" (boolean: true only if this segment would make a "
    "strong standalone short video), \"reasoning\" (string, 1-2 sentences). "
    "Use ONLY the provided evidence; when evidence is thin, say so and "
    "score conservatively. Never invent events, dialogue, or timestamps."
)


#: The optional global video pass (File API). Its output is CONTEXT for
#: the per-window prompts — never proof of coverage (the window tile
#: check is the proof) and never trusted timestamps.
GLOBAL_VIDEO_PROMPT = (
    "You are analyzing a COMPLETE video for a clip-repurposing platform. "
    "Return ONLY valid JSON: {\"summary\": string (2-4 sentences about the "
    "whole video), \"key_moments\": array of {\"start_seconds\": number, "
    "\"end_seconds\": number, \"why\": string} with the 3-5 most important "
    "moments. Use timestamps you can actually locate in the video; never "
    "invent moments or timestamps that are not there."
)


def build_window_prompt(window: dict[str, Any], global_context: str | None = None) -> str:
    """The strict per-window prompt (bounded by max_prompt_chars at the
    client; the window dict already carries capped transcript/evidence)."""
    payload = {"window": window}
    if global_context:
        payload["global_context"] = global_context
    return (
        ASSESSMENT_SYSTEM
        + "\n\nWindow data (JSON):\n"
        + json.dumps(payload, ensure_ascii=False, default=str)
    )


def parse_assessment(raw_text: str) -> dict[str, Any]:
    """Parses + validates the model's JSON answer. Raises
    GeminiPermanentError on malformed output (a permanent contract
    violation for THIS attempt — the caller records an honest failure)."""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        # Tolerate (but never require) a markdown fence around the JSON.
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise GeminiPermanentError("gemini returned non-JSON output") from exc
    if not isinstance(data, dict):
        raise GeminiPermanentError("gemini JSON must be an object")
    out: dict[str, Any] = {"status": "ok"}
    desc = data.get("scene_description")
    out["scene_description"] = str(desc)[:500] if isinstance(desc, str) and desc.strip() else None
    etype = data.get("event_type")
    out["event_type"] = etype if etype in ASSESSMENT_EVENT_TYPES else "miscellaneous"
    imp = data.get("event_importance")
    if isinstance(imp, (int, float)) and not isinstance(imp, bool) \
            and 0.0 <= float(imp) <= 1.0:
        out["event_importance"] = round(float(imp), 4)
    else:
        out["event_importance"] = 0.5  # honest neutral default (never > evidence)
    out["candidate_clip"] = bool(data.get("candidate_clip", False))
    reason = data.get("reasoning")
    out["reasoning"] = str(reason)[:500] if isinstance(reason, str) else ""
    return out


class GeminiClient:
    """Bounded, key-pooled Gemini access (transport-injectable).

    The httpx transport is injectable (``httpx.MockTransport`` in tests —
    no network, no real keys anywhere in the test suite). The sleep hook
    is injectable too (deterministic backoff tests)."""

    def __init__(
        self,
        config: GeminiConfig,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._pool = KeyPool(config.api_keys, config.cooldown_seconds, clock=clock)
        self._sleep = sleep
        self._external_client = client
        self._client = client

    # -- capabilities -----------------------------------------------------------

    @property
    def is_configured(self) -> bool:
        return self._pool.size > 0

    @property
    def model(self) -> str:
        return self._config.model

    async def close(self) -> None:
        if self._external_client is not None:
            await self._external_client.aclose()

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._config.timeout_seconds)
            self._external_client = self._client
        return self._client

    # -- core generate (bounded retries, typed errors) ---------------------------

    async def generate_json(self, prompt: str, *, system: str | None = None) -> dict[str, Any]:
        """One generateContent call with JSON response mode; retries
        transient/rate-limited failures across the key pool (bounded);
        permanent failures raise immediately."""
        if not self.is_configured:
            raise GeminiNotConfiguredError(
                "no Gemini API key configured (WORKER_GEMINI_API_KEYS)")
        if len(prompt) > self._config.max_prompt_chars:
            prompt = prompt[: self._config.max_prompt_chars]
        url = f"{self._config.api_base.rstrip('/')}/models/{self._config.model}:generateContent"
        body: dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "temperature": 0.1},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system[:2000]}]}

        attempts = self._config.max_retries + 1
        last_error: GeminiError | None = None
        for attempt in range(attempts):
            key = self._pool.pick()
            if key is None:
                # Every key is in cooldown: a bounded backoff, then one
                # more pick — the loop stays bounded (never an infinite
                # retry on permanent errors).
                if attempt + 1 < attempts:
                    await self._sleep(self._backoff(attempt))
                    continue
                raise GeminiRateLimitedError(
                    "all Gemini keys are in cooldown (quota)") from None
            headers = {"x-goog-api-key": key}
            try:
                response = await self._http().post(url, json=body, headers=headers)
            except httpx.TimeoutException as exc:
                # Transient: a server/network problem, NOT a per-key budget —
                # the pool is left untouched (bounded backoff + retry).
                last_error = GeminiTransientError("gemini request timed out")
                logger.warning("gemini timeout attempt=%d model=%s", attempt, self._config.model)
                if attempt + 1 < attempts:
                    await self._sleep(self._backoff(attempt))
                continue
            except httpx.HTTPError as exc:
                last_error = GeminiTransientError(
                    f"gemini network error: {exc.__class__.__name__}")
                if attempt + 1 < attempts:
                    await self._sleep(self._backoff(attempt))
                continue

            status = response.status_code
            if 200 <= status < 300:
                self._pool.report_success(key)
                text = self._extract_text(response)
                return parse_assessment(text)
            if status in (400, 401, 404):
                self._pool.report_permanent(key)
                raise GeminiPermanentError(
                    f"gemini request rejected (HTTP {status})") from None
            if status == 429 or (status == 403 and self._is_quota(response)):
                # Quota/rate budget is PER KEY — cool this key down and let
                # the pool rotate to another key with budget.
                self._pool.report_rate_limited(key)
                last_error = GeminiRateLimitedError(
                    "gemini quota/rate limit (HTTP %d)" % status)
                logger.warning("gemini rate-limited attempt=%d status=%d", attempt, status)
            else:
                # 5xx-class: server problem, not a key problem — retry with
                # backoff WITHOUT poisoning the pool.
                last_error = GeminiTransientError(
                    f"gemini server error (HTTP {status})")
                logger.warning("gemini server error attempt=%d status=%d", attempt, status)
            if attempt + 1 < attempts:
                await self._sleep(self._backoff(attempt))
        raise (last_error or GeminiTransientError("gemini retries exhausted"))

    async def _generate_text(self, prompt: str, *, system: str | None = None,
                             parts: list[dict[str, Any]] | None = None) -> str:
        """Raw text generateContent (the File-API video pass uses file
        parts). Same retry discipline as :meth:`generate_json`."""
        if not self.is_configured:
            raise GeminiNotConfiguredError(
                "no Gemini API key configured (WORKER_GEMINI_API_KEYS)")
        if parts is None:
            prompt = prompt[: self._config.max_prompt_chars]
            parts = [{"text": prompt}]
        url = f"{self._config.api_base.rstrip('/')}/models/{self._config.model}:generateContent"
        body: dict[str, Any] = {"contents": [{"parts": parts}]}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system[:2000]}]}
        attempts = self._config.max_retries + 1
        last_error: GeminiError | None = None
        for attempt in range(attempts):
            key = self._pool.pick()
            if key is None:
                if attempt + 1 < attempts:
                    await self._sleep(self._backoff(attempt))
                    continue
                raise GeminiRateLimitedError("all Gemini keys are in cooldown (quota)") from None
            headers = {"x-goog-api-key": key}
            try:
                response = await self._http().post(url, json=body, headers=headers)
            except (httpx.TimeoutException, httpx.HTTPError) as exc:
                last_error = GeminiTransientError(
                    f"gemini network error: {exc.__class__.__name__}")
                if attempt + 1 < attempts:
                    await self._sleep(self._backoff(attempt))
                continue
            status = response.status_code
            if 200 <= status < 300:
                self._pool.report_success(key)
                return self._extract_text(response)
            if status in (400, 401, 404):
                self._pool.report_permanent(key)
                raise GeminiPermanentError(f"gemini request rejected (HTTP {status})") from None
            if status == 429 or (status == 403 and self._is_quota(response)):
                self._pool.report_rate_limited(key)
                last_error = GeminiRateLimitedError(
                    "gemini quota/rate limit (HTTP %d)" % status)
            else:
                # 5xx-class: server problem, not a key problem.
                last_error = GeminiTransientError(f"gemini server error (HTTP {status})")
            if attempt + 1 < attempts:
                await self._sleep(self._backoff(attempt))
        raise (last_error or GeminiTransientError("gemini retries exhausted"))

    # -- public semantic surface ---------------------------------------------------

    async def assess_window(self, window: dict[str, Any],
                            *, global_context: str | None = None) -> dict[str, Any]:
        """Per-window semantic assessment (the full-coverage mechanism).
        Returns the validated assessment dict (status 'ok'). Raises the
        typed GeminiError hierarchy on failure — the caller records the
        honest per-window failure state."""
        prompt = build_window_prompt(window, global_context=global_context)
        return await self.generate_json(prompt, system=ASSESSMENT_SYSTEM)

    async def upload_video(self, file_path: Any) -> str:
        """File API upload (resumable protocol, bounded chunks). Returns
        the file NAME; the caller must clean up via delete_file. Never
        called for metadata or rendering — Gemini does not own the
        media pipeline."""
        if not self.is_configured:
            raise GeminiNotConfiguredError(
                "no Gemini API key configured (WORKER_GEMINI_API_KEYS)")
        from pathlib import Path

        path = Path(str(file_path))
        size = path.stat().st_size
        key = self._pool.pick()
        if key is None:
            raise GeminiRateLimitedError("all Gemini keys are in cooldown (quota)") from None
        headers = {"x-goog-api-key": key}
        # 1) initiate
        response = await self._http().post(
            f"{self._config.api_base.rstrip('/')}/upload/v1beta/files",
            json={"file": {"display_name": path.name}},
            headers=headers,
        )
        if response.status_code != 200:
            raise self._error_for(response, key, initiate=True)
        self._pool.report_success(key)
        data = response.json()
        name = str(data.get("name") or "")
        upload_uri = str(data.get("upload_uri") or "")
        if not name or not upload_uri:
            raise GeminiPermanentError("gemini File API returned no upload target")
        # 2) chunked PUT of the raw bytes (bounded memory)
        with path.open("rb") as fh:
            offset = 0
            while True:
                chunk = fh.read(2 * 1024 * 1024)
                if not chunk:
                    break
                put = await self._http().put(
                    upload_uri, content=chunk,
                    headers={"Content-Length": str(len(chunk))},
                )
                if put.status_code not in (200, 201):
                    raise GeminiTransientError(
                        f"gemini file upload failed (HTTP {put.status_code})")
                offset += len(chunk)
        # 3) poll until ACTIVE (bounded)
        for _ in range(self._config.file_poll_attempts):
            get = await self._http().get(
                f"{self._config.api_base.rstrip('/')}/files/{name}", headers=headers)
            if get.status_code == 200:
                state = str((get.json() or {}).get("file", {}).get("state") or "")
                if state == "ACTIVE":
                    return name
                if state == "FAILED":
                    raise GeminiPermanentError("gemini File API processing failed")
            await self._sleep(self._config.file_poll_interval)
        raise GeminiTransientError("gemini File API did not reach ACTIVE in budget")

    async def understand_video(self, file_name: str, prompt: str) -> str:
        """The optional global video-understanding pass (enrichment +
        context for the window prompts — NEVER proof of coverage)."""
        mime = "video/mp4"
        parts = [
            {"file_data": {"mime_type": mime,
                           "file_uri": f"gs://{file_name}"}},
            {"text": prompt[: self._config.max_prompt_chars]},
        ]
        return await self._generate_text("Analyze this video.", parts=parts)

    async def delete_file(self, file_name: str) -> None:
        """Best-effort File API cleanup (never raises — the File API
        garbage-collects within ~48h anyway)."""
        key = self._pool.pick()
        if key is None:
            return
        try:
            await self._http().delete(
                f"{self._config.api_base.rstrip('/')}/files/{file_name}",
                headers={"x-goog-api-key": key})
        except httpx.HTTPError:
            pass

    # -- internals ------------------------------------------------------------------

    def _backoff(self, attempt: int) -> float:
        return self._config.backoff_seconds * (2 ** attempt)

    @staticmethod
    def _is_quota(response: httpx.Response) -> bool:
        try:
            text = response.text or ""
        except Exception:  # noqa: BLE001 — body may be unreadable
            return False
        return ("429" in text or "quota" in text.lower() or "resource exhausted" in text.lower())

    @staticmethod
    def _extract_text(response: httpx.Response) -> str:
        try:
            data = response.json()
            parts = data["candidates"][0]["content"]["parts"]
            return "".join(str(p.get("text") or "") for p in parts)
        except (ValueError, KeyError, IndexError, TypeError):
            raise GeminiPermanentError("gemini returned an unexpected response shape") from None

    def _error_for(self, response: httpx.Response, key: str, *, initiate: bool) -> GeminiError:
        status = response.status_code
        if status in (400, 401, 404):
            self._pool.report_permanent(key)
            return GeminiPermanentError(f"gemini file API rejected (HTTP {status})")
        if status == 429 or (status == 403 and self._is_quota(response)):
            self._pool.report_rate_limited(key)
            return GeminiRateLimitedError(f"gemini file API rate limit (HTTP {status})")
        self._pool.report_rate_limited(key)
        return GeminiTransientError(f"gemini file API error (HTTP {status})")
