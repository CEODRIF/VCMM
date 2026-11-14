# Universal Video Link Ingestion (TASK 014)

The create form accepts pasted links for **four** source classes. Each
class is handled through an explicit, authorized mechanism — or refused
with a clear, actionable error. **Metadata is never presented as
ingestion**: a job is only created when a real media path exists (or when
the worker will attempt one through an authorized endpoint).

```
User pastes URL
   │
   ▼
Client classification (src/lib/validation/clips.ts — shape only, no
network) → tab hints ("direct file link will be imported", "YouTube:
metadata only — upload instead", …)
   │
   ▼
POST /api/v1/jobs  (server re-classifies authoritatively —
src/lib/validation/clips.ts is shared, the API gate wins)
   │
   ├── youtube          → 503 honest refusal (no official media API)
   ├── twitch VOD/live  → 503 actionable refusal (no official VOD media API)
   ├── twitch CLIP      → 201 queued (worker imports via official
   │                       Get Clips Download — needs creator OAuth)
   ├── direct file URL  → 201 queued as source_type "direct_url"
   │                       (.mp4/.webm/.mov on any public host)
   └── unrecognized     → 400 validation error
```

## Capability matrix (source by source)

| Pasted URL class | Import media? | Mechanism | Authorization needed | Failure mode when unauthorized |
|---|---|---|---|---|
| YouTube (`youtube.com/watch`, `youtu.be`) | **No** — metadata only | Official oEmbed / Data API v3 (metadata) | none / API key (metadata) | API 503: "YouTube has no official media-download API — upload the file directly" |
| Twitch VOD (`twitch.tv/videos/{id}`, `/{channel}/videos/{id}`) | **No** — metadata only | Official Helix `/videos` (metadata) | client id (metadata) | API 503: "no authorized VOD media-download endpoint — export from the Twitch dashboard, or upload" |
| Twitch LIVE (`/live/{channel}`, `/{channel}`) | **No** | Official Helix `/streams` (status) | client id | API 503: "live streams cannot be imported — wait for the VOD" |
| Twitch **CLIP** (`twitch.tv/clips/{slug}`, `/{channel}/clip/{slug}`, `clips.twitch.tv/{slug}`) | **Yes** | Official Helix `GET /clips/downloads` (the only authorized media endpoint) | **creator OAuth**: `WORKER_TWITCH_OAUTH_REFRESH_TOKEN` (the clip owner's refresh token, scope `channel:manage:clips`) + client id/secret | Job fails PERMANENT with the actionable `provider_unauthorized` message naming the exact settings; no partial state |
| Direct file URL (`https://…/…mp4/.webm/.mov`, any non-platform host, standard port) | **Yes** | SSRF-guarded streaming download → FFprobe validation → private bucket | none (public file) | typed permanent/retryable errors per cause (see below) |
| Upload (file picker) | **Yes** (unchanged) | Signed URL → Supabase Storage | user's session | unchanged |

## Direct video file URLs — security model

`worker/app/providers/direct_url.py` + `worker/app/providers/http_guard.py`:

- **Claim** is shape-only: `http(s)`, standard port (80/443), no embedded
  credentials, path ends in `.mp4`/`.webm`/`.mov`, host is not a
  platform host (youtube.com / youtu.be / twitch.tv families / twitchcdn /
  jtvnw). Unsafe URLs are REJECTED with typed `InvalidUrlError` before any
  network traffic — never merely "unrecognized".
- **SSRF guard (every request, every hop)**: literal-IP + DNS revalidation
  with fail-closed "ALL answers must be public" (loopback / private /
  link-local / reserved / multicast / unspecified / site-local all
  refused — a single private answer poisons the name; decimal/octal IP
  encodings are normalized before the class check).
- **Redirects are pre-resolved** with `resolve_redirect_chain`: a HEAD
  probe per hop, each hop re-validated (shape + public IP; hop 0 on the
  pasted host, later hops public-only — the safe form of "the host may
  sign the file on a CDN domain"), bounded by `materialize_max_redirects`.
  The download client is then built with the FINAL host only, and a
  redirect appearing during the stream fails loudly (never followed).
- **Bounded transfer**: chunked reads (default 1 MiB), hard size cap
  (default 2 GiB, checked DURING the transfer — partial file removed on
  breach), per-request timeout, bounded retries with Retry-After, and a
  total wall-clock deadline (`WORKER_DIRECT_URL_TRANSFER_DEADLINE_SECONDS`,
  default 1800) that defeats slow-drip sources.
- **Byte truth**: the downloaded file is FFprobe-validated BEFORE storage
  (a 200-OK HTML page is rejected with `corrupt_media`, bytes removed).
- **Storage**: private `sources/{job_id}/original/` object, per-extension
  content type, lease-guarded idempotent pin; re-claim never re-downloads.
- **Operator controls**: `WORKER_DIRECT_URLS_ENABLED` (default on),
  `WORKER_DIRECT_URL_HOST_ALLOWLIST` (optional comma-separated list;
  when set, hop 0 must match — for deployments that want imports only
  from known storage hosts).
- **Secrets**: error messages and logs pass through the redactor
  (`token=…` masks to end-of-URL, bearer/access/refresh/client_secret
  patterns); signed URLs never appear in job records or logs (tested).

## Twitch clips — authorization model

`worker/app/providers/twitch.py`:

1. `WORKER_TWITCH_OAUTH_REFRESH_TOKEN` must be the clip owner's refresh
   token, granted against THIS deployment's Twitch app
   (`WORKER_TWITCH_CLIENT_ID` + `_CLIENT_SECRET`) with scope
   **`channel:manage:clips`** (or `editor:manage:clips` for editors).
   The refresh happens through the official `refresh_token` grant;
   refresh token + client secret travel ONLY in the token-exchange POST
   body (never in URLs or logs), the access token is cached single-flight.
2. `GET /users` identifies the token's user → `editor_id`;
   `GET /clips?id={slug}` → `broadcaster_id`; then the official
   `GET /clips/downloads` (max 10 ids per call, 1 here).
3. The returned CDN URL must be **https** and its host must match
   `WORKER_TWITCH_MEDIA_HOST_SUFFIXES` (default `twitchcdn.net`,
   `twitch.tv`, `jtvnw.net`) — the URL is then fetched through the full
   SSRF guard and FFprobe-validated before storage.
4. Every refusal is per-cause: not configured → `provider_unauthorized`
   (names the settings + scope); 403 → `source_rejected` (token is not
   the owner/editor of this clip); 404 → `source_expired`; null download
   URL → `materialization_unsupported` (media no longer available).
   None of these are retried into a loop.

**VODs remain un-importable by design**: no official Twitch endpoint
serves VOD media to third-party applications, and no unofficial path
(GraphQL VideoAccessToken, session cookies, HLS scraping) is used or may
be added. The API refuses VOD jobs with the export/upload guidance.

## What is NOT implemented (honest boundary)

- YouTube media download: no official API exists; nothing ships that
  works around YouTube's terms (no yt-dlp, no cookie theft, no scraping).
- Twitch VOD/live media: no official endpoint (see above).
- Streaming playlists (`m3u8`/`mpd`): classified as unrecognized —
  ClipForge imports container files, not live stream protocols.
- Non-standard ports, credentials-in-URL, non-http(s) schemes: rejected.

## Tests

- `worker/tests/test_direct_url_provider.py` — SSRF matrix (loopback /
  private / link-local / IPv6 / decimal-octal IP / rebinding / ports /
  credentials / schemes / control chars), redirect chains (public ok,
  private refused, loop refused, surprise redirect refused), size cap +
  cleanup, wall-clock deadline, status mapping, HTML rejection, secret
  hygiene.
- `worker/tests/test_twitch_clips.py` — classification (incl. VOD-id-wins),
  authorized happy path (full official flow, token/body hygiene),
  unauthorized/expired-token (permanent + actionable), Helix failure
  mapping, CDN hardening (suffix/https/redirect/rebinding/size/empty/
  corrupt), no secret in messages or log records.
- `worker/tests/test_ingestion_direct_url.py` — real provider through the
  real pipeline: success + dispatch, idempotent re-claim (no re-download),
  disabled provider, SSRF refusal, 404, corrupt bytes, size cap (no
  partial storage).
- `tests/jobs-provider-api.test.ts` — API boundary per class (201/503/400),
  lookalike-domain classifier, regression of upload/youtube/twitch-VOD.
- `tests/database/task14-universal-link.test.ts` — PGlite: `direct_url`
  constraint branches + legacy branches unchanged + idempotency.

## Migration

`supabase/migrations/20261126000000_task14_universal_link_source.sql` is
**repository-only**: it extends `jobs_source_type_check` and
`jobs_source_present` with the `direct_url` branches. It is idempotent and
does not touch existing rows. Per the TASK 014 delivery rules it is NOT
applied to any production database — apply it manually after the PR
review (see the file header).
