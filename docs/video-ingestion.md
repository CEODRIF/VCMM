# Video Ingestion — Sources, Classification, Materialization

How a video gets from a user's paste (or file picker) to a
ffprobe-verified object in the private `clipforge-sources` bucket.
Capability status per source: [provider-capabilities.md](./provider-capabilities.md).

## Endpoints & flow

1. **Client** — the create form offers four tabs: `YouTube URL`,
   `Twitch VOD`, `Video URL` (Kick + direct file links), `Upload file`.
   Client-side schema validation (`src/lib/jobs/validate.ts`) rejects
   malformed links before submission with actionable messages
   ("must be an http(s) URL", "must be a Twitch VOD URL", …).
2. **API** — `POST /api/v1/jobs` (session required). The service layer
   (`src/lib/jobs/service.ts`) performs the **capability gate BEFORE any
   job row is written**:
   - provider unconfigured → `503` naming the missing deployment setting;
   - platform source without an authorized media path (YouTube, Twitch
     VOD, Kick) → `503` with the export/upload instruction;
   - live-stream URLs → `503` "wait for the VOD";
   - accepted sources (upload / direct URL / authorized Twitch clip) →
     job row created (`status: uploading|queued`).
   A job is therefore **never created, and never reported as
   processing, when its media can never be acquired**.
3. **Worker ingestion** — `worker/app/ingestion.py` runs the provider
   chain in priority order: `upload → youtube → twitch → kick →
   direct_url` (direct URL last so platform URLs win). Every provider
   implements the universal contract (`worker/app/providers/base.py`):
   `classify(url)`, `fetch_metadata()`, `materialize()` →
   `MaterializedSource` (bytes in storage + ffprobe report).
4. **Materialization** — `worker/app/providers/materialization.py`
   (`store_source`) uploads via the service-role client to
   `sources/{job_id}/original/{uuid}.mp4` (private bucket,
   `clipforge-sources`) and ffprobe-verifies duration/streams before the
   job leaves `uploading`.

## Provider notes (exact behavior)

- **Manual upload** — no provider call; the worker ffprobe-validates the
  object the browser already uploaded.
- **YouTube** — oEmbed (keyless) for classification metadata; Data API v3
  optional (`WORKER_YOUTUBE_API_KEY` / OAuth). `materialize()` **always
  raises `MaterializationUnavailable`** — no yt-dlp, no cookies, no
  bypass. The API gate makes this a 503 at creation time.
- **Twitch** — Helix metadata for VODs and clips (client id/secret).
  VOD `materialize()` raises `MaterializationUnavailable`. **Clips**
  download through the official `GET /clips/downloads` endpoint with
  creator authorization (`WORKER_TWITCH_OAUTH_REFRESH_TOKEN`, scope
  `channel:manage:clips`); the CDN URL is checked against
  `WORKER_TWITCH_MEDIA_HOST_SUFFIXES` before streaming.
- **Kick** — `api.kick.com/public/v1` (channel + VOD metadata) with
  `WORKER_KICK_API_KEY`. `materialize()` raises
  `MaterializationUnavailable` (no official VOD download; VODs expire
  14–30 days).
- **Direct video URL** — strict shape (http(s) 80/443, no userinfo,
  `.mp4/.webm/.mov` suffix, platform hosts excluded). The SSRF-safe HTTP
  client (`http_guard.py`) resolves the redirect chain re-validating each
  hop, optionally against `WORKER_DIRECT_URL_HOST_ALLOWLIST`, streams
  under `WORKER_MATERIALIZE_MAX_SOURCE_BYTES` +
  `WORKER_DIRECT_URL_TRANSFER_DEADLINE_SECONDS`.

## Worker configuration (all `WORKER_`-prefixed, `worker/app/config/settings.py`)

| Setting | Default | Purpose |
|---|---|---|
| `WORKER_SOURCE_PROVIDERS_ENABLED` | `true` | Master toggle for all URL sources |
| `WORKER_YOUTUBE_API_KEY`, `WORKER_YOUTUBE_OAUTH_*` | unset | Optional YouTube metadata enrichment |
| `WORKER_TWITCH_CLIENT_ID` / `_SECRET` | unset | Helix metadata (VOD + clips) |
| `WORKER_TWITCH_OAUTH_REFRESH_TOKEN` | unset | Creator authorization for clip downloads |
| `WORKER_TWITCH_MEDIA_HOST_SUFFIXES` | Twitch CDN suffixes | Allowlist for clip CDN URLs |
| `WORKER_TWITCH_API_BASE` / `_OAUTH_BASE` | official | Test overrides |
| `WORKER_KICK_API_KEY` / `WORKER_KICK_API_BASE` | unset / official | Kick public API |
| `WORKER_DIRECT_URLS_ENABLED` | `true` | Master switch for direct-URL imports (OFF → permanent actionable failure) |
| `WORKER_DIRECT_URL_HOST_ALLOWLIST` | unset | Comma list; empty = any non-platform host (still SSRF-guarded) |
| `WORKER_DIRECT_URL_TRANSFER_DEADLINE_SECONDS` | 1800 | Whole-download deadline |
| `WORKER_MATERIALIZE_MAX_SOURCE_BYTES` | 2 GiB | Source size cap |
| `WORKER_MATERIALIZE_MAX_REDIRECTS` / `_CHUNK_BYTES` / `_MAX_DURATION_SECONDS` | 4 / 1 MiB / 6 h | Stream safety |
| `WORKER_PROVIDER_REQUEST_TIMEOUT_SECONDS` / `_MAX_RETRIES` / `_RETRY_BACKOFF_SECONDS` | 30 / 3 / 1.0 | API call behavior |

## Failure semantics

Worker error categories (`worker/app/providers/errors.py`) map to typed
job failures with actionable messages; a missing adapter or disabled
providers produce a **permanent** failure ("unsupported source type",
"source providers are disabled on this deployment — use direct video
upload"), while network/quota errors are **transient** and retried within
the job's attempt budget. No provider error ever touches another job's
objects or rows.
