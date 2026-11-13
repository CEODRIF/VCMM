# ClipForge AI — Development Roadmap

Seven phases, in order. Each phase ends with a shippable increment.

## Phase 1 — Project foundation ✅ (this commit)

- Next.js App Router + strict TypeScript + Tailwind + ESLint/Prettier.
- shadcn/ui-compatible component kit, Lucide icons, path aliases, centralized
  configuration, env validation (zod).
- Initial UI: Landing, Access (UI only), Creation (validated form),
  Processing (7 typed states, placeholder data), Results (placeholder layout).
- Python 3.12 worker: FastAPI `GET /health`, Pydantic settings, JSON logging,
  modular scaffolding, Dockerfile, requirements.
- Supabase: version-controlled initial schema (3 tables, RLS deny-by-default)
  — reviewed, not applied.
- Security baseline: `.env.example`, gitignored `.env*`, server-only modules,
  no secrets in frontend.
- Tests (vitest + pytest) and GitHub Actions CI.
- Documentation (README, ARCHITECTURE, ROADMAP).

## Phase 2 — Access control & job API ✅ (this commit)

- `POST /api/access`: verify shared access code against `ACCESS_CODE_HASH`
  (constant-time SHA-256 compare), issue 256-bit session token in an HttpOnly
  SameSite=Lax (Secure-in-prod) 24h cookie; only the token's hash stored in
  `processing_sessions`; sliding-window rate limit (5 / 15 min / IP).
- `DELETE /api/access`: sign-out (destroy session row, clear cookie).
- `POST /api/v1/jobs`: session-gated, zod-validated, session-owned job rows
  (adds `clip_duration_seconds` + `captions_enabled` via Phase 2 migration).
- `GET /api/v1/jobs` + `GET /api/v1/jobs/[id]`: session-scoped list/status,
  anti-enumeration (foreign ids ≡ 404, non-UUID ids rejected pre-DB).
- UI wired: Access form → real auth; Creation form → real job creation;
  Processing page → live polling panel; header → session-aware sign-out.
- Centralized API error handling (no stack/DB/storage leakage).
- Migrations 1+2 executed and asserted against a real PostgreSQL 16 engine
  (pglite WASM) in CI: schema, constraints, and RLS deny-by-default
  (anon blocked, service_role BYPASSRLS verified).
- Test suites: auth API, session security, rate limiter, jobs API
  authorization, database/RLS.

## Phase 3 — Ingestion ✅ (this commit)

- **Real direct uploads**: create job (`uploading`, reserved per-job location)
  → `POST /api/v1/jobs/[id]/upload/authorize` (session + ownership + state
  guards, file-name traversal check, extension/MIME whitelist with spoofing
  cross-check, declared-size cap, rate limit, server-generated object path,
  signed upload URL, short-lived authorization TTL) → browser PUT **directly**
  to private Supabase Storage (real progress %, cancellation, retry) →
  `POST /api/v1/jobs/[id]/upload/complete` (object existence + exact-size
  verification, idempotent, guarded atomic `uploading → queued`).
- **Private Supabase Storage** (migration `20261115000001`): `clipforge-sources`
  and `clipforge-clips`, both private, no client policies, service-role only;
  no public source-video URLs.
- **Video validation** (worker, FFprobe on the actual bytes — never the
  browser's MIME/extension): container whitelist (MP4/MOV/WEBM family), video
  stream required, audio presence reported, codecs, resolution, duration vs
  the configured SOURCE limit (`MAX_SOURCE_DURATION_SECONDS`, distinct from
  the 60-180s clip setting), size vs `MAX_UPLOAD_SIZE_BYTES`, corruption and
  unsupported-container rejection with safe error categories.
- **Postgres job queue** (migration `20261115000000`): queue/lease columns,
  nine-state machine with a DB-level transition trigger, `FOR UPDATE SKIP
  LOCKED` atomic claiming, expired-lease crash recovery, bounded retries,
  owner-scoped renew/progress/complete/fail/release RPCs — EXECUTE revoked
  from PUBLIC, granted to `service_role` only.
- **Python worker ingestion loop**: poll + claim (bounded concurrency),
  lease renewal with ownership-loss abort, download to scratch-only temp
  storage (always cleaned up), progress reporting, exponential backoff on
  transport errors, graceful shutdown, `/health` heartbeat. Ingestion ends at
  `ready`; the worker never marks jobs `completed`.
- **YouTube**: authorized source-provider interface (`src/lib/providers/
  youtube.ts`). No provider ships with the repo → creation returns a clear
  503 explaining that direct upload is available. No unauthorized downloader
  (yt-dlp/cookies/DRM) exists or is added.
- Tests: upload API (auth, ownership, state, extension, MIME spoofing,
  oversize, expiry, idempotent completion), queue RPCs + RLS + bucket
  migration on a real PostgreSQL engine (pglite WASM), FFprobe validation and
  ingestion lifecycle with synthetic FFmpeg-generated videos (no binary
  fixtures committed), worker loop (claim, backoff, shutdown).
- Note: applied to a real Supabase project remains an explicit live step
  (verified development project required; none was provisioned in this phase).

## Phase 4 — AI pipeline ✅ (this commit)

- **Audio extraction** (`worker/app/processors/audio.py`): FFmpeg decodes the
  verified source to a mono 16 kHz PCM WAV in the job's temp dir (guaranteed
  cleanup, timeouts, corrupt/empty-input handling). Missing audio STREAMS are
  caught by the probe before extraction and fail permanently (`no_audio`).
- **Provider-based transcription** (`worker/app/transcription`): `BaseTranscriber`
  contract with word-level timestamps, language and segment metadata.
  `LocalWhisperTranscriber` runs **faster-whisper locally** (optional
  `requirements-optional.txt`; `av<16` pin) — no cloud, no credentials, no
  paid APIs. Without the dependency (or with no model configured) the factory
  returns an honest `UnavailableTranscriber` and analysis jobs fail permanently
  with `transcription_unavailable` — nothing is ever fabricated. Long
  recordings are split into overlapping chunks (`-ss/-t`) and merged with
  midpoint-of-overlap ownership (no duplicated or dropped segments); inference
  runs in worker threads so lease renewal stays responsive.
- **Clip detection** (`worker/app/ai/clip_detection.py`): the transcript is
  grouped into timed sentences (punctuation + pause-based boundaries, with a
  proportional-timing fallback), then complete-idea windows are searched inside
  `[max(60, 0.85·target), min(180, 1.25·target)]` — always within the DB
  60-180s bound. Candidates start/end on sentence boundaries, and a greedy
  selection keeps the requested count while enforcing <50% pairwise overlap.
  No suitable window → honest `no_clips_detected` permanent failure.
- **Explainable scoring** (`worker/app/ai/scoring.py`): five weighted criteria
  (opening hook 0.30, semantic completeness 0.22, context & coherence 0.18,
  engagement potential 0.20, duration suitability 0.10), each with a 0-1 value
  and a human-readable reason; total 0-100.
- **Persistence** (`worker/app/storage/clip_store.py`): delete-then-insert
  **scoped to the job id** into the existing `clips` table (title, start/end,
  DB-generated duration, `score`, `scoring` jsonb, `sort_order`,
  `status='pending'`) — idempotent on retry, never touches other jobs.
- **Queue integration** (migration `20261116000000`): stage-aware claim
  (`ready → transcribing`, `queued → validating`, expired-lease crash recovery
  in every active stage), stage-aware renew/progress/fail/release (a transient
  analysis failure returns to `ready`, not `queued`), transition map extended
  with `transcribing → ready` and `analyzing → ready`, and a **parked
  `analyzing`** end state (lock cleared, not claimable) that Phase 5's renderer
  will pick up and move to `rendering`. The worker never marks a job
  `completed` in this phase.
- **Frontend**: job status API now reports `clipsCount`; the live panel stops
  polling once the job is parked with detected clips.
- Tests: extraction (real FFmpeg), providers + chunking (real split, scripted
  merge, real-Whisper smoke), detection + scoring (band, boundaries, overlap,
  determinism, honest failures), analysis lifecycle (all failure/ownership
  paths), Phase 4 queue semantics on a real PostgreSQL engine (pglite WASM).

## Phase 5 — Rendering

- FFmpeg pipeline: reframe to vertical 9:16, cut to start/end, encode
  1080x1920 H.264 (30fps) (`worker/app/rendering`).
- Modern animated subtitles burned in from the transcription (word-level).
- Health-checked render workers; retry and failure paths update `jobs`/`clips`.

## Phase 6 — Product completion

- Results page wired to the clips API: previews, titles, duration labels.
- Individual MP4 downloads and "Download all (ZIP)".
- Job/clip expiry + cleanup job for expired sessions and artifacts.
- Real error surfaces (retries, clear failure messages) end to end.

## Phase 7 — Hardening & launch

- Load testing and queue backpressure.
- Observability: structured metrics, alerting, request tracing.
- Security review: rate limits, secret rotation, dependency audits.
- Production deploy: Vercel (frontend), worker on dedicated compute with
  volumes; production Supabase migration applied with review.
- Launch checklist: docs, access-code distribution, monitoring on call.
