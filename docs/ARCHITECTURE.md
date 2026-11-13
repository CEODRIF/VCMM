# ClipForge AI — Architecture

## System overview

```
                         ┌─────────────────────────────┐
                         │           Browser           │
                         │  Next.js UI (App Router)    │
                         │  landing · access · create  │
                         │  processing · results       │
                         └──────────────┬──────────────┘
                                        │ HTTPS (REST)
                                        ▼
                          ┌─────────────────────────────┐
                          │   Next.js server (Vercel)   │
                          │  route handlers / API layer │
                          │  access-code verification   │
                          │  job CRUD · input validation│
                          │  signed upload authorization│
                          │  (short request/response    │
                          │   only — NO video bytes are │
                          │   proxied through Vercel)   │
                          └──────┬───────────────────┬──┘
                                 │ service role      │ service role
                                 ▼                   ▼
                      ┌─────────────────────┐  ┌─────────────────────────────┐
                      │  Supabase (PG)      │  │  Supabase Storage          │
                      │  sessions/jobs/     │  │  (PRIVATE buckets — the    │
                      │  clips (+ RLS)      │  │   durable source of truth) │
                      │  + queue RPCs       │  │  clipforge-sources:        │
                      │  (service-role only)│  │   sources/{job}/original/* │
                      └─────────▲───────────┘  │  clipforge-clips:          │
                                │              │   clips/{job}/{clip}.mp4   │
                                │ atomic claim └─────────────▲──────────────┘
                                │ + lease + RPCs             │ signed-URL PUT
                                │ (service role)             │ (browser, direct)
                      ┌─────────┴──────────────────┐  ┌──────┴───────────────┐
                       │      Python worker         │  │        Browser       │
                       │  claim → download →        │  │  real progress,      │
                       │  FFprobe validate → ready  │  │  cancel, retry       │
                       │  (ingestion, Phase 3)      │  └──────────────────────┘
                       │  transcribe → analyze      │
                       │  (Phase 4) → render        │
                       │  (Phase 5A: cut/reframe/   │
                       │  encode/verify/upload)     │
                       │  FFmpeg · Whisper          │
                      │  local disk = SCRATCH ONLY │
                      │  (temp files, always       │
                      │   deleted after the job)   │
                      └─────────────────────────────┘
 ```

## Component responsibilities

### Frontend (Next.js on Vercel)

- Renders the five pages: Landing, Access, Creation, Processing, Results.
- Validates user input client-side for immediate feedback (duration 60-180s,
  count 1-10, YouTube URL shape) using the shared rules in
  `src/lib/validation/clips.ts` and `src/config/app.ts`.
- Holds the shared access-code UI (password-style input). **No secrets and no
  auth logic in the browser** — verification happens server-side (Phase 2).
- Polls job status (Phase 2) and renders the results grid (Phase 6).

### API layer (Next.js route handlers)

Implemented in Phase 2:

- `POST /api/access` — verifies the shared access code against
  `ACCESS_CODE_HASH` (SHA-256, constant-time compare), rate-limited
  (5 attempts / 15 min / IP), and issues a 256-bit session token as an
  HttpOnly, SameSite=Lax cookie (Secure in production, 24h lifetime).
- `DELETE /api/access` — destroys the session row and clears the cookie.
- `POST /api/v1/jobs` — session required; re-validates the payload server-side
  (zod); inserts a `jobs` row owned by the verified session. Upload jobs start
  in `uploading` with a reserved per-job storage location; YouTube jobs
  require an authorized source provider and return a clear 503 when none is
  configured (no job row is created).
- `GET /api/v1/jobs` — session required; lists only the session's jobs.
- `GET /api/v1/jobs/[id]` — session required; returns the job only if it is
  owned by the session; otherwise the same 404 as a missing id
  (anti-enumeration).

Implemented in Phase 3 (ingestion):

- `POST /api/v1/jobs/[id]/upload/authorize` — session + ownership required;
  rate-limited (10 / min / session+IP). Validates file name (no traversal),
  extension whitelist (mp4/mov/webm), MIME whitelist AND extension/MIME
  cross-check (spoofing guard), and declared size against
  `MAX_UPLOAD_SIZE_BYTES`. Generates the storage object path
  (`sources/{job_id}/original/{uuid}.{ext}`) **server-side**, creates a
  Supabase signed upload URL, and records the path + a short
  authorization expiry (`UPLOAD_AUTH_TTL_SECONDS`) on the job (state-guarded
  `uploading` update). Returns only `{uploadUrl, method, contentType,
  maxBytes, expiresInSeconds, jobId}` — never the object layout.
- `POST /api/v1/jobs/[id]/upload/complete` — session + ownership required;
  **idempotent**. The browser can never complete a job by calling this alone:
  the server verifies the authorization is unexpired, that the object exists
  in the private bucket at the exact server-generated path (storage list,
  metadata only), and that the real size matches the declared and reported
  sizes — then performs the guarded atomic `uploading → queued` transition.
  Duplicate calls after finalization return 200 `alreadyCompleted`.

Implemented in Phase 6 (results & downloads — every route re-verifies session
+ ownership on EVERY request; anti-enumeration extends to clip ids — a
non-UUID clip id is 404 before any session/DB round trip, and a foreign or
missing clip is indistinguishable from "not ready" or "object missing"):

- `GET /api/v1/jobs/[id]/results` — the session's own job plus ONLY its
  clips: `{id, title, duration, sortOrder, status, downloadable}`. A clip is
  `downloadable` only when it is `ready` AND its stored `storage_path`
  matches the canonical `clips/{job_id}/{clip_id}.mp4` shape for THIS job
  (server cross-check — a client can never inject a path). For a completed
  job the response also carries `zip: {available, totalBytes, limitBytes}`
  (metadata-only storage listing) so the UI can offer, explain-disable or
  hide "Download all" before the click. Storage paths, service-role material
  and ownership internals never leave the server.
- `GET /api/v1/jobs/[id]/clips/[clipId]/preview` — mints a SHORT-LIVED
  server-side signed URL (default 300s, `CLIP_PREVIEW_URL_SECONDS`) for
  inline playback, after verifying session + job ownership + clip/job
  association + `ready` + canonical path + object existence (one metadata
  listing per job). No URL is ever minted for a missing/foreign/invalid
  clip — all failure shapes return the same generic 404.
- `GET /api/v1/jobs/[id]/clips/[clipId]/download` — same check chain, but
  the URL (default 900s, `CLIP_DOWNLOAD_URL_SECONDS`) is issued in
  attachment flavour with a server-sanitized filename (single path segment;
  path separators, forbidden characters and control characters removed;
  length capped; empty titles fall back to `clip.mp4`).
- `GET /api/v1/jobs/[id]/download-all` — "Download all" as a STREAMED ZIP
  (see Storage below). JSON guards BEFORE the first archive byte: job must
  be `completed` (else 409), at least one ready+canonical clip, ALL those
  objects must exist (else 409), and the total size must fit the configured
  cap (else 413 with a per-clip fallback message).

Centralized error handling (`src/lib/api/errors.ts`): typed `ApiError` mapped
to stable `{ "error": string }` responses; zod errors → 400; missing backend
configuration → clear 503; everything else → generic 500 with server-side
logging. Stack traces, database internals and storage paths never leak to the
client.

**The API layer must stay fast and stateless.** Video bytes travel
browser → Supabase Storage directly (signed URL); the worker downloads them
with the service role. Anything that touches video bytes (downloading,
audio extraction, transcription, rendering, zipping) belongs to the worker.

### Authentication & session model (Phase 2)

```
Browser                          Next.js server (Vercel)                Supabase
   │  POST /api/access {code}        │                                       │
   │────────────────────────────────►│ 1. rate-limit check (per IP)          │
   │                                 │ 2. sha256(code) vs ACCESS_CODE_HASH   │
   │                                 │    (constant-time)                    │
   │                                 │ 3. token = randomBytes(32)            │
   │                                 │──────── sha256(token) ───────────────►│ processing_sessions
   │◄────────── 200 + Set-Cookie ────│                                       │ (hash only, 24h)
   │  clipforge_session=<token>      │                                       │
   │  (HttpOnly — JS cannot read)    │                                       │
   │                                 │                                       │
   │  POST /api/v1/jobs (cookie)     │                                       │
   │────────────────────────────────►│ 1. read cookie → sha256(token)        │
   │                                 │──────── lookup + expiry ─────────────►│
   │                                 │ 2. zod-validate payload               │
   │                                 │ 3. insert job (session_id = verified) │
   │◄────────── 201 { job.id } ──────│                                       │
   │                                 │                                       │
   │  GET /api/v1/jobs/[id] (cookie) │  session-scoped lookup only           │
   │────────────────────────────────►│  foreign id ⇒ 404 (same as missing)   │
```

Security properties:

- The **plaintext access code exists only in the client's memory and one
  server request**; the server environment stores its SHA-256 hash
  (`ACCESS_CODE_HASH`), the database stores session **token hashes**, never
  codes or raw tokens.
- Comparison is **constant-time** (SHA-256 both sides + `timingSafeEqual`).
- Session tokens are **256-bit** from `crypto.randomBytes`.
- Cookie: `HttpOnly` (no XSS exfiltration), `SameSite=Lax` (CSRF posture),
  `Secure` in production, `Path=/`, 24h `Max-Age`.
- **Authorization is server-side only**: every protected route re-verifies the
  cookie → token hash → `processing_sessions` (expiry checked); job queries are
  always filtered by the verified `session_id`; job ids are UUIDs and foreign
  ids are indistinguishable from missing ones.
- **Rate limiting** on `POST /api/access` (sliding window, per IP; in-memory —
  per-instance on Vercel, Redis-backed planned in Phase 7).
- Session lifecycle: created on successful auth; verified on every protected
  request; expired rows are deleted on first use; `DELETE /api/access` signs
  out.

### Database (Supabase / Postgres)

- `processing_sessions` — access sessions (token hash + expiry).
- `jobs` — one row per creation request; carries the state machine and, since
  Phase 3, the ingestion queue state: `attempt_count` / `max_attempts`,
  `locked_at` / `locked_by` / `lease_expires_at`, `last_error` (safe error
  category + message, ≤1000 chars), `upload_completed_at`,
  `upload_auth_expires_at`, `source_metadata` (jsonb: declared upload facts,
  later merged with FFprobe-measured media facts), `updated_at`.
- `clips` — generated clips per job, with source in/out timestamps.
- **Row Level Security is enabled on every table with no anonymous policies**:
  the only writer/reader in normal operation is the service-role client in the
  API layer (server code, `src/lib/supabase/server.ts`).
- **Job state machine is enforced by the database** (Phase 3 migration
  `20261115000000`): the `jobs_status_check` constraint allows exactly the
  nine states, a BEFORE-UPDATE trigger rejects any transition outside the
  legal map (arbitrary client-driven status changes are impossible at the DB
  level), and a `jobs_lock_consistency` check constraint guarantees lock
  fields are set exactly while a job is `validating`.
- **Worker queue = restricted RPCs** (same migration, `SECURITY DEFINER`,
  EXECUTE revoked from PUBLIC, granted to `service_role` only):
  - `claim_next_job(worker_id, lease_seconds)` — oldest claimable job via
    `FOR UPDATE SKIP LOCKED`; claimable = `queued`, OR `validating` with an
    **expired lease** (crash recovery); attempt budget not exhausted.
  - `renew_job_lease(job_id, worker_id, lease_seconds)` — owner only.
  - `update_job_progress(job_id, worker_id, 0-100)` — owner only.
  - `complete_job_ingestion(job_id, worker_id, metadata?)` — owner only;
    `validating → ready`, merges measured metadata, clears the lock.
  - `fail_job(job_id, worker_id, category, message, permanent)` — owner only;
    requeues while attempts remain, `failed` at the limit or immediately for
    permanent errors (corrupt/unsupported media).
  - `release_job(job_id, worker_id)` — owner only; graceful `validating → queued`.
- Schema is version-controlled in `supabase/migrations/` and applied to
  development environments explicitly (never from CI, never to production
  without review).

### Storage (Supabase Storage — the durable source of truth)

- Two **private** buckets (Phase 3 migration `20261115000001`, Supabase-
  specific):
  - `clipforge-sources` — original uploads, `sources/{job_id}/original/{uuid}.{ext}`.
    - `clipforge-clips` — rendered output, `clips/{job_id}/{clip_id}.mp4`
      (written by the Phase 5A renderer; upsert, so re-renders are
      idempotent).
- **No client policies** on either bucket: with RLS deny-by-default, browsers
  can only ever act on (a) a signed upload URL issued server-side for exactly
  one job-owned path, or (b) a SHORT-LIVED signed download URL minted
  server-side (Phase 6: 300s preview / 900s attachment, both only after
  session + ownership + clip/job + object verification). There are no public
  source-video URLs — and no public clip URLs — anywhere in this system.
- **Clip delivery is streamed, never buffered whole** (Phase 6). Individual
  clips are never proxied: the browser holds a direct signed URL to Storage.
  "Download all" is a STORE-method ZIP written by an async generator
  (`src/lib/zip/store-zip.ts` — zero new dependencies): each entry's local
  header carries no size/CRC (data-descriptor flag), the clip bytes are
  fetched with the service key and piped chunk by chunk (concurrency exactly
  1, sort order — bounded and deterministic), and the CRC32 + sizes are
  emitted in the 0x08074b50 descriptor after the data; the central directory
  and EOCD follow at the end. Server memory stays at a single fetch chunk
  regardless of archive size; no temp files, no compression pass (MP4 is
  already compressed). Entry names are sanitized server-side (index-prefixed,
  single path segment — traversal impossible). Client cancellation (or a
  mid-stream Storage failure) aborts the in-flight fetch and tears down the
  response — a partially received archive is discarded by the client, and
  the per-clip signed URLs remain the reliable fallback. Documented hosting
  limit: serverless execution time/bandwidth are bounded, which is why the
  total-size cap (`MAX_ZIP_SIZE_BYTES`, default 1 GiB) makes oversized sets
  fail fast with a clear 413 instead of aborting mid-download.
- **Retention & cleanup** (Phase 6): terminal jobs get
  `expires_at = now() + 30 days` from the migration 20261123000000 trigger
  (only when NULL — operator deadlines win). The worker's MANUAL
  `python -m app.cleanup` (see worker/README.md) purges only
  terminal + expired + un-purged jobs: job-scoped storage deletes
  (sources + canonical clip objects) BEFORE the guarded `purged_at` marker —
  idempotent, crash-safe, never scheduled automatically.
- **Every object path is generated server-side**; the browser supplies only a
  file name (validated: no traversal, extension whitelist, MIME cross-check).
- Uploads go **browser → Storage directly** (signed-URL PUT with real
  progress and cancellation); large video bytes are never proxied through
  Vercel. The API only issues authorizations and verifies completion (object
  existence + exact declared size via the storage list API — metadata only).
- **Worker local disk is a processing scratchpad, NOT durable storage.**
  `WORKER_STORAGE_ROOT/tmp/{job_id}/` holds the downloaded source during
  validation and is always removed (success, failure or cancellation). The
  durable copy remains in `clipforge-sources`. The old Phase 1
  `jobs/<id>/uploads/...` local layout (`worker/app/storage/paths.py`) is
  retained only as a path-safety utility; no durable data is written to local
  disk.

### Worker (Python 3.12, separate service)

Modules and their phases:

| Module | Responsibility | Phase |
| --- | --- | --- |
| `app/api` | Health endpoint + worker heartbeat | 1 / 3 (done) |
| `app/queue` | Job models + Supabase queue client (RPC wrappers incl. `complete_job_rendering` + Phase 4 guarded stage transition / parking) | 1 / 3 / 4 / 5 (done) |
| `app/processors` | Ingestion (download → FFprobe → ready) + audio extraction + analysis (transcribe → detect → score + word timings → persist → park) + **rendering (download → cut/reframe/encode → burn subtitles → verify → upload → DB-guarded complete)** | 3 / 4 / 5A / 5B (done) |
| `app/transcription` | Provider base (word-level contract) + local faster-whisper adapter (optional) + overlap chunking/merge + honest unavailability | 4 (done) |
| `app/ai` | Sentence extraction, complete-idea window detection, explainable 5-criterion scoring, per-clip word attachment (5B) | 4 / 5B (done) |
| `app/rendering` | FFmpeg subprocess lifecycle (timeout/cancel/cleanup), rotation-aware reframe + cut to exact 1080x1920, **deterministic ASS caption engine + libass `ass=` burn-in in the same command (5B)**, FFprobe output-contract validation, render profiles | 5A / 5B (done) |
| `app/storage` | Private Supabase Storage download + clip upload (path-shape validated, size-capped, upsert) + clip persistence (job-scoped, incl. captions payload) + local path utilities | 3 / 4 / 5 (done) |
| `app/validation` | FFprobe media validation (container, streams, codecs, duration, size) | 3 (done) |
| `app/config` | Pydantic settings (env-validated) + JSON structured logging | 1 / 4 / 5 |
| `app/cleanup.py` | Manual artifact purge CLI (Phase 6): retention-policy candidates → job-scoped storage deletes → guarded `purged_at` marker; idempotent, NOT scheduled | 6 (done) |

Ingestion loop (Phase 3): poll → `claim_next_job` (atomic) → bounded-
concurrency processing (semaphore) → download to temp → FFprobe validate →
progress reporting → `complete_job_ingestion` (`ready`) or `fail_job` (retry /
permanent). A per-job lease-renewal task extends the lease while processing;
if renewal is rejected (ownership lost), the processing task is cancelled and
no stale state write is attempted. Consecutive claim transport errors back off
exponentially (capped at 60s). Shutdown is graceful: stop claiming, let
in-flight jobs finish or expire their leases (recovered by other workers),
clean temp files. `/health` exposes a worker heartbeat (id, active jobs,
uptime).

The worker reports state transitions and progress through the RPCs; the UI
polls the API. The worker NEVER marks a job `completed` by direct write —
since Phase 5A, `completed` is reachable only through the
`complete_job_rendering` RPC, which the database enforces.

Analysis lifecycle (Phase 4): the claim RPC is stage-aware — a `ready` job is
claimed directly as `transcribing` (a `queued` job as `validating`; an active
job with an EXPIRED lease is recovered in place, crash recovery for every
active stage). The worker dispatches by the claimed stage: `validating` →
ingestion processor, `transcribing`/`analyzing` → analysis processor, which
runs: re-download the verified source → re-probe (require an audio stream,
else permanent `no_audio`) → FFmpeg audio extraction (mono 16 kHz WAV) →
local Whisper transcription (chunked for long recordings, word-level
timestamps) → guarded `transcribing → analyzing` transition → complete-idea
clip detection within `[max(60, 0.85·target), min(180, 1.25·target)]` with
<50% pairwise overlap → explainable 5-criterion scoring → job-scoped
delete-then-insert into `clips` → **park** (lock cleared, progress 100,
status stays `analyzing`). A parked job is NOT claimable; the Phase 5
renderer will extend the claim to parked jobs and move them to `rendering`.
Every step keeps the Phase 3 safety contract: per-job lease renewal with
ownership-loss abort (no stale writes), cancellation without state writes,
temp-dir cleanup on every path, bounded retries with stage-aware requeue
(a transient analysis failure returns to `ready`, never `queued`), and safe,
categorized errors (`no_audio`, `transcription_unavailable`, `no_speech`,
`no_clips_detected` are permanent; download/extract/transcribe/persist
errors are transient).

Transcription is provider-based and honest by construction: the
`BaseTranscriber` contract carries word/segment timings, language and
metadata. The only shipped engine is **local** faster-whisper (optional
dependency, CPU, no cloud/credentials); when it is absent or unconfigured
the factory returns an `UnavailableTranscriber` whose calls raise
`TranscriptionUnavailableError` — jobs fail permanently with a clear message
instead of producing fabricated output.

Rendering lifecycle (Phase 5A): the claim RPC is stage-aware for parked
jobs too — a PARKED `analyzing` job whose `render_attempts < max_attempts`
is claimed as `rendering` (an expired-lease `rendering` job is recovered in
place, stage kept; `render_attempts` is the dedicated rendering retry budget
because `attempt_count` is consumed by every claim). The worker dispatches
`rendering` → rendering processor, which runs: re-download the verified
source → re-probe → list the job's clips (resume skips clips already
`ready`) → render the remaining windows in parallel (bounded by
`WORKER_RENDER_CONCURRENCY`, first clip error cancels the siblings) →
validate EVERY rendered file against the output contract (MP4 / H.264 /
AAC / exact 1080x1920 / yuv420p / stored rotation ≤ 0.5° / duration within
±15%) → upsert-upload to `clips/{job_id}/{clip_id}.mp4` in the private
`clipforge-clips` bucket (path shape validated, size-capped) → mark the
clip `ready` with its storage path → report progress (15 → 100) →
`complete_job_rendering`. The completion RPC — the only way a job becomes
`completed` during rendering — succeeds only when the caller owns the
lease, the job is `rendering`, at least one clip exists and EVERY clip is
`ready` with a non-null `storage_path`; any refusal is treated as
"abandon, the lease recovers it". A transient rendering failure requeues to
the parked `analyzing` state (render_attempts + 1) until the budget is
exhausted, then `failed`; `render_invalid` (contract violation) and
`no_clips_to_render` are permanent. Rotation is read from the MP4 `rot
`/`dtrt` boxes directly (version-independent — old ffprobe cannot expose
side data, recent ffmpeg ignores the boxes in `-autorotate`) and baked into
the pixels with an explicit filter, so output always stores zero rotation.
Same safety contract as before: per-job lease renewal with ownership-loss
abort, cancellation without state writes, temp-dir cleanup on every path,
one managed FFmpeg subprocess per clip (timeout → kill + await, partial
output removed).

Captions (Phase 5B): the analysis stage persists each clip's REAL word
timings into `clips.captions` (`{"words":[{"w","s","e"}]}`, absolute
seconds, clipped to the window; the detector attaches the transcribed
tokens itself — nothing is ever invented). The rendering stage converts
them to clip-relative time, sanitizes them deterministically (missing /
invalid / overlapping / out-of-range entries are dropped or resolved; a
corrupt payload degrades to a plain render), and generates an ASS document
in the per-job temp dir: lines of `caption_words_per_line` words (broken
earlier by >1.2s silence gaps), per-word karaoke (`\k`) timing — verified
libass semantics: a word settles into `PrimaryColour` (base, default white)
as it is spoken while upcoming words glow in `SecondaryColour` (highlight,
default `00D7FF`) — bottom-center placement in the safe area (MarginV 520,
PlayRes 1080x1920 = output size, so no rescaling). The `ass=PATH` filter
(appended AFTER the reframe plan) burns the subtitles into the SAME FFmpeg
command; the path is filtergraph-escaped and the ASS file is removed on
every path. Text is escaped so transcribed `{`/`}`/`\` can never open an
override block; uppercase applies to Latin words only; RTL text is stored
in logical order (libass/fribidi render the bidi). The job-level
`captions_enabled` flag (Phase 2 column) gates the feature; when it is off,
or a clip has no usable words, the command line is byte-for-byte Phase 5A.
The output contract (MP4/H.264/AAC/1080x1920/yuv420p/zero rotation/
 duration) is unchanged — burn-in alters pixels only. Font rendering is
delegated to fontconfig (default `DejaVu Sans`, pinned in CI); Arabic
contextual shaping additionally needs libharfbuzz in the FFmpeg build
(deterministic fallback: the font's coverage / isolated glyphs).

Retention & cleanup (Phase 6): the worker hosts a MANUAL purge service
(`python -m app.cleanup`) — the only component that deletes storage
objects, because Postgres cannot reach Storage. The policy is explicit in
the migration (30 days after `completed`/`failed`, trigger-gated,
operator-overridable, `jobs.purged_at` as the idempotency marker) and the
purge re-verifies every precondition server-side before each deletion:
terminal status, `expires_at < now`, `purged_at IS NULL`. Deletions are
strictly job-scoped (source directory listing + clip rows whose
`storage_path` matches the canonical shape AND the owning job — foreign or
malformed paths are skipped and logged), storage is cleared BEFORE the
guarded marker update (at-least-once: a crash between the two is resolved
by a safe re-run), and no scheduler of any kind is wired — deletion is an
explicit operator action until the policy is proven in a live environment.

## Job state machine

```
ingestion (Phase 3):   uploading → queued → validating → ready
                       (any active) ──────────────────► failed

product pipeline:      ready → transcribing → analyzing → rendering → completed
                       (any active) ─────────────────────────────────► failed
                       (Phase 4 end state: PARKED analyzing = lock cleared,
                        not claimable; the Phase 5 claim moves it → rendering)
                       rendering ──transient fail──► analyzing (PARKED,
                                                    render_attempts + 1)
                       rendering → completed ONLY via complete_job_rendering
                       (DB guard: every clip ready + stored)
```

Legal transitions are enforced by the database trigger
(`validate_job_status_transition`), so no API or worker bug can move a job to
an arbitrary state. Phase 4 extends the map with `transcribing → ready`
(transient analysis retry) and `analyzing → ready` (graceful release); Phase 5
adds `rendering → completed` (guarded RPC) and `rendering → analyzing`
(rendering retry to the parked state). The lock-consistency constraint admits
a parked `analyzing` row (lock cleared) and requires a live lease for an
ACTIVE `rendering` row. States are defined in `src/config/app.ts` (frontend),
`worker/app/queue/models.py` (worker) and the `jobs_status_check` constraint
(Postgres) and kept in sync by tests on both sides.

## Security model

- **Single shared access code**, verified only server-side:
  `ACCESS_CODE_HASH` (SHA-256) in server env; constant-time comparison;
  success issues an httpOnly session cookie with a 24h expiry
  (`processing_sessions.expires_at`). Implemented and tested in Phase 2.
- **No user accounts**; rate limiting on `/api/access` (5 / 15 min / IP)
  makes brute force impractical. Implemented in Phase 2 (per-instance;
  Redis-backed global limit in Phase 7).
- **Service-role key isolation**: `SUPABASE_SERVICE_ROLE_KEY` is read only in
  `server-only` modules (`src/lib/env.ts`, `src/lib/supabase/server.ts`) and
  is never prefixed `NEXT_PUBLIC_`; the worker uses its own
  `WORKER_SUPABASE_*` credentials and is a separate service — worker secrets
  never appear in Vercel client bundles.
- **RLS deny-by-default** on all tables; **queue-control RPCs are revoked from
  PUBLIC and granted to `service_role` only** — anonymous sessions cannot
  claim, renew, fail or release jobs.
- **Storage**: private buckets, no client policies, server-generated paths
  only, signed upload authorization with a short TTL (default 900s), object
  existence + exact-size verification before a job may enter the queue,
  idempotent completion (guarded atomic state transition — a browser can
  never mark a job completed without a verified object).
- **Input validation** at every boundary: zod (API layer), Pydantic (worker),
  shared validators (UI). File names: no traversal, extension whitelist,
  MIME whitelist with extension/MIME cross-check (spoofing guard). YouTube
  URLs are shape-validated; fetching requires an authorized source provider —
  the browser never talks to YouTube download endpoints, and no unauthorized
  downloader (yt-dlp, cookies, DRM bypass) exists in this codebase.
- **Upload size limits**: declared size checked at authorization (413) and
  re-checked against the real object at completion; the worker enforces the
  same cap on downloads (aborts + cleans up partial files).
- **Media trust**: browser-declared container/MIME is never trusted; the
  worker probes the actual bytes with FFprobe (container whitelist, video
  stream required, duration vs the source limit — not the 60-180s clip
  setting — resolution, codecs, corruption detection).
- **Worker lease ownership**: every queue RPC update is scoped to
  `locked_by = caller`; crash recovery is via expired leases, never via
  client action. Failed jobs store a safe error category, never stack traces.
- **Terminal completion is DB-enforced**: `completed` is reachable only via
  the `complete_job_rendering` RPC, which refuses unless the caller owns the
  lease, the job is `rendering`, and every clip is `ready` WITH a
  `storage_path` — a stale or buggy worker can never mark a job completed
  early (no code path writes `completed` directly).
- **Clip object safety**: uploads to `clipforge-clips` are rejected unless
  the path matches the renderer-generated shape exactly
  (`clips/{uuid}/{uuid}.mp4` — no traversal), the file exists, is non-empty
  and is under the configured size cap; uploads are upserts (idempotent on
  retry).
- **Caption safety**: subtitles are generated ONLY from persisted,
  transcribed words (no synthesis, no fabrication); ASS markup characters
  in text are escaped (an override block can never be opened from
  transcribed content), the `ass=` path is filtergraph-escaped, the ASS
  file exists only in the per-job temp dir and is removed on every path;
  the font family is validated against markup characters at startup.
- **Clip delivery is verification-gated** (Phase 6): a signed preview/
  download URL is minted only after session verification, job ownership,
  the job-scoped clip lookup, `ready` status, the canonical-path cross-check
  against the OWNING job, and an object-existence check — and the URL points
  at the server-derived path only (client-supplied paths are never trusted).
  All failure shapes collapse to the same generic 404, so clip ids cannot be
  enumerated. Signed URLs are short-lived (300s / 900s, configurable) and
  the buckets remain private with no client policies.
- **Destructive operations are policy-gated and job-scoped** (Phase 6):
  artifact deletion happens ONLY through the manual worker purge, which
  requires terminal status + explicit expiry + a null `purged_at` marker,
  re-verified at execution time; every deleted object path must match the
  server-generated shape for the job being purged; the DB marker is set only
  AFTER the storage deletions (crash-safe at-least-once); nothing is
  scheduled automatically, and jobs without an expiry marker are never
  deleted.
- **Secrets** live in environment variables only; `.env*` is gitignored;
  `.env.example` contains placeholders. Logs are structured JSON and never
  include credentials, signed URLs or raw file names beyond job ids.

## Completed integrations

Phase 2:

- `src/components/access/access-form.tsx` → `POST /api/access` (real auth).
- `src/components/create/creation-form.tsx` → `POST /api/v1/jobs` (real job
  creation; 401 routes to the access page).
- `src/app/processing/page.tsx` → live polling of `GET /api/v1/jobs/[id]`
  via `?job=<id>`.
- `src/components/site-header.tsx` → session-aware sign-out (`DELETE /api/access`).

Phase 3 (ingestion):

- `src/components/create/creation-form.tsx` → real upload workflow: create
  job → `POST .../upload/authorize` → direct signed-URL PUT (XHR: real
  progress %, cancellation, retry of the same job on failure) →
  `POST .../upload/complete` → navigate to processing. YouTube submission
  shows the provider-unavailable 503 message when no authorized provider is
  configured.
- `src/components/processing/*` → nine-state rendering (uploading, queued,
  validating, ready, transcribing, analyzing, rendering, completed, failed);
  polling stops at a final state or when the job is parked at `analyzing`
  with detected clips (`clipsCount > 0`).
- `src/lib/storage/objects.ts` → signed upload URL creation + object
  verification (private bucket, service role).
- `src/lib/providers/youtube.ts` → authorized-provider interface; the
  unconfigured provider returns the clear 503. Integration point for an
  explicitly approved provider is marked in `src/lib/jobs/service.ts`.
- `worker/app/worker.py` + `app/processors/ingestion.py` +
  `app/queue/supabase_queue.py` + `app/storage/supabase_storage.py` +
  `app/validation/media.py` → the ingestion loop (see above), started via
  FastAPI lifespan.

Phase 4 (AI processing engine):

- `src/app/api/v1/jobs/[jobId]/route.ts` → job status now includes
  `clipsCount` (detected-clip count; 0 until analysis persists clips).
- `worker/app/processors/analysis.py` + `app/processors/audio.py` +
  `app/transcription/*` + `app/ai/*` + `app/storage/clip_store.py` → the
  analysis lifecycle (see above), dispatched by the stage-aware claim
  (`ready → transcribing`), parked at `analyzing`.
- `worker/app/queue/supabase_queue.py` → guarded direct updates
  (`transition_stage`, `park_after_analysis`) scoped to
  `(id, status, locked_by = caller)`; the DB trigger remains the authority.
- `supabase/migrations/20261116000000_phase4_clip_analysis.sql` → clip
  score/scoring columns, stage-aware queue RPCs, transition map extension,
  parked-`analyzing` lock-consistency branch.

Phase 5A (rendering foundation):

- `worker/app/rendering/*` → `profiles.py` (rotation normalization +
  cover/contain geometry plan), `ffmpeg.py` (managed subprocess: timeout /
  cancellation / partial-output cleanup), `renderer.py` (rotation-aware
  cut + reframe to exact 1080x1920), `validation.py` (FFprobe output
  contract + MP4 rotation box scanner), `base.py` (`BaseRenderer` /
  `RenderSpec` — `captions` carried for Phase 5b).
- `worker/app/processors/rendering.py` + `worker/app/worker.py` → the
  rendering lifecycle (see above), stage-aware dispatch
  (`rendering` → RenderProcessor; a build without a renderer releases the
  job back to the parked state instead of failing it).
- `worker/app/storage/supabase_storage.py` → `upload_from` for
  `clips/{job_id}/{clip_id}.mp4` (path-shape validated, size-capped,
  upsert); `worker/app/storage/clip_store.py` → `list_clips` / `mark_clip`
  scoped to `(id, job_id)` with `pending → processing → ready`.
- `worker/app/queue/supabase_queue.py` → `complete_job_rendering` wrapper.
- `supabase/migrations/20261117000000_phase5_rendering.sql` →
  `jobs.render_attempts`, `rendering` stage in all queue RPCs (same
  signatures — backward compatible), `rendering → analyzing` retry
  transition, `complete_job_rendering` guard RPC, `clips_job_status_idx`.
- `tests/database/phase5-rendering.test.ts` → the full Phase 5 queue
  semantics asserted on a real PostgreSQL engine (pglite WASM).

Phase 5B (animated subtitles):

- `worker/app/rendering/captions.py` → the deterministic ASS engine
  (timestamp sanitization, line segmentation, `\k` karaoke timing, escaping,
  persisted-payload parser).
- `worker/app/rendering/renderer.py` → `ass=` filter appended to the SAME
  filter chain (temp-dir ASS file, removed on every path).
- `worker/app/rendering/base.py` → `CaptionWord` / `CaptionStyle` +
  `RenderSpec.caption_words` / `caption_style` (empty words = Phase 5A).
- `worker/app/ai/clip_detection.py` + `worker/app/storage/clip_store.py`
  → `ScoredClip.words` attached from the real tokens, persisted to
  `clips.captions`, read back for rendering.
- `worker/app/processors/analysis.py` / `rendering.py` → the wiring
  (analysis persists words; rendering consumes them per clip, gated by the
  Phase 2 `jobs.captions_enabled` flag).
- `supabase/migrations/20261120000000_phase5b_captions.sql` → additive
  `clips.captions jsonb` (no RLS/RPC changes).
- `tests/database/phase5b-captions.test.ts` → column, round-trip,
  idempotency, RLS and no-5A-regression asserted on a real PostgreSQL
  engine (pglite WASM).

Phase 6 (results API & product completion):

- `src/app/api/v1/jobs/[jobId]/results/route.ts` +
  `.../clips/[clipId]/preview/route.ts` + `.../clips/[clipId]/download/route.ts`
  + `.../download-all/route.ts` → the session-protected results API
  (safe metadata, 300s/900s signed URLs, streamed ZIP; all verification-
  gated and anti-enumeration-safe).
- `src/lib/storage/clips.ts` → clip storage access (canonical path
  derivation + owning-job cross-check, signed URL minting, metadata
  existence listing, raw service-key object streaming for the ZIP) —
  `server-only`, service-role only.
- `src/lib/zip/store-zip.ts` + `src/lib/zip/filename.ts` → the streaming
  STORE-ZIP engine (zero dependencies, chunk-at-a-time, deterministic) and
  the single-segment filename sanitizer.
- `src/lib/api/owned-job.ts` + `src/lib/jobs/results.ts` → the shared
  ownership gate (incl. the clip-level 404-uniform variant) and job-scoped
  clip data access.
- `src/app/results/page.tsx` + `src/components/results/*` → the live results
  page (`?job=<uuid>`): loading / auth / not-found / error / processing
  (auto-poll) / failed / expired / empty / ready states, native `<video>`
  previews from lazy signed URLs, per-clip downloads, Download-all anchor;
  the state machine itself is pure (`src/lib/results/view.ts`) and unit-
  tested. `src/components/processing/live-job-panel.tsx` → "View your clips"
  CTA for completed jobs.
- `supabase/migrations/20261123000000_phase6_results_retention.sql` →
  additive: `jobs.purged_at` + the retention trigger (30 days after a
  terminal transition, only when `expires_at IS NULL`).
- `worker/app/cleanup.py` + `worker/tests/test_cleanup.py` → the manual,
  idempotent, job-scoped purge (policy, scope, partial-failure safety, CLI).
- `tests/database/phase6-retention.test.ts` → the retention trigger + marker
  semantics asserted on a real PostgreSQL engine (pglite WASM).

## Remaining integration points (marked in code)

| Location | Marker | Lands in |
| --- | --- | --- |
| `src/lib/jobs/service.ts` | `INTEGRATION POINT (authorized provider only)` | YouTube source materialization by an explicitly approved provider |
