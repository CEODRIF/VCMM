# Provider Capabilities — Source-by-Source Matrix

Accurate, capability-by-capability status of every video source ClipForge
accepts (TASK 011 provider layer → TASK 014 universal link ingestion →
TASK 015 Kick + generation). **A source is only "supported" for a stage
when the worker performs that stage for real — metadata retrieval alone is
never claimed as media support.** No fake adapters or simulated downloads
exist in the codebase; every limitation below is enforced by an explicit
error (503 at job creation on the API side, `MaterializationUnavailable`
on the worker side).

## The matrix

| Capability | YouTube | Twitch VOD | Twitch Clips | Kick VOD | Direct video URL | Manual upload |
|---|---|---|---|---|---|---|
| URL classification | ✅ | ✅ | ✅ | ✅ | ✅ | n/a (file) |
| Metadata retrieval | ✅ (keyless oEmbed; Data API v3 optional) | ✅ (Helix `GET /videos`) | ✅ (Helix `GET /clips`) | ✅ (Kick public API) | ⚠️ (HEAD size probe only — no metadata API) | ✅ (local ffprobe of the stored object) |
| **Actual media acquisition** | ❌ **not available** | ❌ **not available** | ✅ **official `GET /clips/downloads`** (creator authorization) | ❌ **not available** | ✅ (SSRF-guarded HTTP stream, redirect chain resolved) | ✅ (browser upload, already in storage) |
| Storage materialization (`sources/{job}/original/*`) | ❌ | ❌ | ✅ | ❌ | ✅ | ✅ |
| Full timeline analysis (P2) | ❌ (blocked at acquisition) | ❌ | ✅ | ❌ | ✅ | ✅ |
| Full Video to Reels (mode A) | ❌ | ❌ | ✅ | ❌ | ✅ | ✅ |
| Viral Highlights (mode B) | ❌ | ❌ | ✅ | ❌ | ✅ | ✅ |
| Output download + permanent deletion (P4) | n/a (no clips) | n/a | ✅ | n/a | ✅ | ✅ |

Legend: ✅ implemented and tested · ❌ deliberately unavailable (typed error) ·
⚠️ partial (see note) · n/a not applicable.

## Current limitations, exactly

### YouTube — metadata only
- oEmbed works with **no key** (title/author/type). Optional Data API v3
  (`WORKER_YOUTUBE_API_KEY`, or OAuth client + refresh token) enriches
  metadata; it is NOT required for classification.
- **Media acquisition is intentionally impossible here.** YouTube exposes
  no official API for downloading video media. The deployment uses no
  yt-dlp, no browser cookies, no DRM/geo bypass — ever. Creating a job
  from a YouTube URL answers **503** with the actionable message:
  *"YouTube sources cannot be materialized on this deployment: the
  official YouTube APIs expose metadata but no authorized media-download
  endpoint. Direct video file upload is available."*
  UI: the create form shows a client-side hint on the YouTube tab:
  *"YouTube link — the official YouTube APIs expose metadata only (no
  authorized media download). To import a YouTube video, upload the file
  directly."*
- No YouTube job row is ever created, and no YouTube object ever reaches
  storage. The worker-side gate (`MaterializationUnavailable`) is a
  second, defense-in-depth line if a row did appear.

### Twitch VOD — metadata only
- Helix `GET /videos` (requires `WORKER_TWITCH_CLIENT_ID` +
  `WORKER_TWITCH_CLIENT_SECRET`) gives title/channel/length; live-stream
  URLs are rejected with *"This Twitch URL is a live stream, not a
  completed recording — wait for the VOD, or upload the file directly."*
- **VOD media is not acquirable through official, authorized endpoints.**
  Job creation answers **503**: *"Twitch VOD media cannot be imported:
  the official Twitch APIs expose no authorized VOD media-download
  endpoint. Export the VOD from the Twitch dashboard and upload the file
  directly."*
- Actionable path: export from the Twitch creator dashboard → manual
  upload (fully supported end-to-end).

### Twitch Clips — the one platform source that materializes
- Metadata: Helix `GET /clips?id=` (app token or creator token).
- Media: official **`GET /clips/downloads`**, which requires **creator
  authorization** (OAuth `refresh_token` flow, scope
  `channel:manage:clips`) via `WORKER_TWITCH_CLIENT_ID/SECRET` +
  `WORKER_TWITCH_OAUTH_REFRESH_TOKEN`. The CDN URL returned is validated
  against `WORKER_TWITCH_MEDIA_HOST_SUFFIXES` before a single byte is
  fetched, then streamed through the SSRF-safe HTTP client and verified
  with ffprobe. Without creator authorization the clip is
  metadata-only, exactly like VODs.
- This is the only remote-platform source that produces
  `sources/{job}/original/*` objects today.

### Kick VOD — metadata only
- Kick public API (`api.kick.com/public/v1`), gated by
  `WORKER_KICK_API_KEY` (base URL overridable via `WORKER_KICK_API_BASE`).
  Channel + VOD listing/metadata works when configured; unconfigured
  deployments answer 503 (*"Kick API key is not configured on this
  deployment (WORKER_KICK_API_KEY)…"*).
- **Kick exposes no official VOD media-download endpoint**, and Kick VODs
  expire 14–30 days after the broadcast. Job creation answers **503**:
  *"Kick VOD media cannot be imported: the official Kick API exposes
  metadata but no authorized VOD media-download endpoint (Kick VODs
  expire 14-30 days after the broadcast ends). Export the recording from
  Kick Studio and upload the file directly."*
- Kick URLs arrive through the generic **"Video URL"** tab (Kick is
  classified from the URL; there is no dedicated Kick tab).

### Direct video URL — full support
- Strict shape: http(s) on ports 80/443, no userinfo, path ends in
  `.mp4` / `.webm` / `.mov`; platform hosts (youtube.com, youtu.be,
  twitch.tv, twitchcdn.net, jtvnw.net) are never claimed by this provider.
- SSRF guards: redirect chain resolved with host re-validation at every
  hop, optional allowlist (`WORKER_DIRECT_URL_HOST_ALLOWLIST`), size cap
  (`WORKER_MATERIALIZE_MAX_SOURCE_BYTES`), transfer deadline
  (`WORKER_DIRECT_URL_TRANSFER_DEADLINE_SECONDS`), whole feature toggle
  (`WORKER_DIRECT_URLS_ENABLED`).
- Streams to `sources/{job}/original/{uuid}.mp4`, ffprobe-verified.

### Manual upload — full support
- Browser upload (chunked, resumable) into the private
  `clipforge-sources` bucket; the worker ffprobe-validates the stored
  object before analysis. No network provider call at all.

## What is NOT in this product

- No third-party download services, no cookie-based scraping, no
  region/DRM circumvention for any platform.
- No "metadata → future download" promise: a job that cannot materialize
  media is rejected **before** any job row or worker work exists, so no
  job is ever reported as processed without actual video media.

## Deployment prerequisites (optional features)

| Feature | Required env (worker) | Without it |
|---|---|---|
| YouTube/Twitch/Kick metadata classification | provider keys as above | 503 with a named-setting message |
| All URL sources | `WORKER_SOURCE_PROVIDERS_ENABLED` (default on) | every URL job → 503 "use direct video upload" |
| Twitch clip materialization | creator OAuth refresh token (`channel:manage:clips`) | clips = metadata only, 503 |
| Direct URL source | `WORKER_DIRECT_URLS_ENABLED=true` (+ allowlist for public exposure) | direct URL → 503 |
| Gemini semantic enrichment | `WORKER_GEMINI_API_KEYS` | analysis degrades to local signals; Mode B still works |

Manual upload and the full P2–P4 pipeline (timeline → modes → 30/60 FPS
1080×1920 renders → dashboard → download → deletion → cleanup) work with
**none** of the provider keys configured.
