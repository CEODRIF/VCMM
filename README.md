# ClipForge AI

ClipForge AI is a web application that transforms long-form videos into short-form vertical clips using AI.

- No user registration — one shared access code.
- YouTube URL input (authorized source provider) or direct video file uploads.
- AI-powered clip detection, clips between 60 and 180 seconds.
- Vertical 9:16 output at 1080x1920 (1080p) with modern animated subtitles.
- Individual downloads and ZIP batch downloads.

> **Status: Phase 5B — animated word-synchronized subtitles.**
> On top of Phase 5A (clip rendering pipeline: download, cut/reframe to
> 1080x1920, FFprobe output contract, upload, DB-guarded completion), the
> worker now burns modern animated subtitles into every rendered clip — in
> the SAME FFmpeg command (libass `ass=` filter): the analysis stage
> persists each clip's REAL word timings (`clips.captions`), the renderer
> generates a deterministic ASS file (karaoke per-word progression, 3 words
> per line, bottom safe area, outline; Arabic RTL / English / German,
> configurable font and colors), and a clip without usable words renders
> exactly as Phase 5A. (Phase 5A recap: the worker claims parked
> `analyzing` jobs into a `rendering` stage, re-downloads the verified
> source, cuts each detected clip window with a managed FFmpeg subprocess
> — rotation-correct, aspect-ratio-preserving reframe to exact 1080x1920 —
> validates the output (MP4 / H.264 / AAC / yuv420p / zero rotation /
> duration tolerance), uploads to the private `clipforge-clips` bucket, and
> completes the job ONLY through the database guard that requires every
> clip `ready` with a stored object path.) Rendered output is NOT yet
> displayed or downloadable (Phase 6). See `docs/ROADMAP.md`.

## Technology stack

 | Layer     | Technology                                                        |
 | --------- | ----------------------------------------------------------------- |
 | Frontend  | Next.js (App Router), TypeScript (strict), Tailwind CSS, shadcn/ui-compatible components, Lucide icons |
 | Hosting   | Vercel (frontend + API only — no video processing in serverless)  |
 | Database  | Supabase (Postgres) with Row Level Security                       |
 | Storage   | Supabase Storage (private buckets, signed upload URLs)            |
 | Worker    | Python 3.12, FastAPI, Pydantic, FFprobe, structured JSON logging  |
  | Pipeline  | Local Whisper (faster-whisper, optional), FFmpeg rendering (Phase 5A) + animated subtitle burn-in (Phase 5B, libass) |
 | Tests/CI  | Vitest, pytest, GitHub Actions                                    |

## Architecture overview

 ```
  1  create job          2  authorize upload     3  verify completion
 ┌────────────┐  ─────────►  ┌──────────────┐   ┌────────────────────────────┐
 │            │              │              │   │                            │
 │  Browser   │              │ Next.js API  │   │       Supabase             │
 │  (UI only) │ ◄─────────── │ (Vercel; no  │──►│  Postgres: sessions · jobs │
 │            │  signed URL  │  video bytes │   │  clips + queue RPCs        │
 │            │              │  proxied)    │   │  (service-role only)       │
 │            │              └──────────────┘   │  Storage: private buckets  │
 │            │  4  PUT bytes DIRECTLY ─────────►│  (signed URLs only)        │
 └─────┬──────┘        (real progress,          │                            │
       │            cancel, retry)              └──────────────▲─────────────┘
       │                                                        │
        │  5  poll status (2.5s)      atomic claim → download → │
        └─────────────────────────────  FFprobe validate → ready│
                                                   ┌─────────────┴───────────┐
                                                    │  Python worker          │
                                                    │  (separate service)     │
                                                    │  ingestion (Phase 3) +  │
                                                    │  analysis (Phase 4) →   │
                                                    │  parked at analyzing →  │
                                                    │  rendering (Phase 5A):  │
                                                    │  cut/reframe/encode,    │
                                                    │  verify, upload clips,  │
                                                    │  DB-guarded completed   │
                                                    └─────────────────────────┘
 ```

Responsibilities of each component are detailed in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

**Key rule: all video processing happens in the separate Python worker — never inside Vercel serverless functions.**

## Repository structure

```
├── src/
│   ├── app/            # Next.js App Router pages (/, /access, /create, /processing, /results)
│   ├── components/     # ui/ (shadcn-compatible kit) + feature components
│   ├── lib/            # utils, env validation, supabase clients, validators
│   ├── hooks/          # client hooks
│   ├── types/          # shared TypeScript types
│   └── config/         # centralized app configuration (brand, product rules)
├── worker/
│   ├── app/            # api, queue, processors, transcription, ai, rendering, storage, config
│   ├── tests/          # pytest suite
│   ├── Dockerfile
│   └── requirements.txt
├── supabase/
│   └── migrations/     # version-controlled SQL (not yet applied anywhere)
├── tests/              # frontend unit tests (vitest)
├── docs/               # ARCHITECTURE.md, ROADMAP.md
├── .github/workflows/  # CI
├── .env.example
└── package.json
```

## Local development

### Prerequisites

- Node.js ≥ 18.17 (tested with Node 20/22)
- Python 3.12 (worker)

### Frontend

```bash
cp .env.example .env.local   # fill in what you have (all optional in Phase 1)
npm install
npm run dev                  # http://localhost:3000
```

Useful scripts:

```bash
npm run typecheck            # tsc --noEmit
npm run lint                 # ESLint (next/core-web-vitals + prettier)
npm run build                # production build
npm test                     # vitest unit tests
npm run format               # prettier
```

### Worker

```bash
cd worker
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
curl -s http://localhost:8000/health
```

See [`worker/README.md`](worker/README.md) for Docker and configuration details.

## Environment variables

Copy `.env.example` to `.env.local` and fill in what applies. **Never commit real values.**

| Variable | Scope | Purpose |
| --- | --- | --- |
| `NEXT_PUBLIC_APP_URL` | public | Canonical app URL |
| `NEXT_PUBLIC_SUPABASE_URL` | public | Supabase project URL |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | public | Supabase anon key (RLS applies) |
| `SUPABASE_URL` | server | Supabase project URL (server client) |
| `SUPABASE_SERVICE_ROLE_KEY` | **server only** | Bypasses RLS — must never reach the client |
 | `ACCESS_CODE_HASH` | **server only** | SHA-256 hash of the shared access code (Phase 2) |
 | `DATABASE_URL` | server | Optional direct Postgres connection |
 | `MAX_UPLOAD_SIZE_BYTES` | server | Upload size cap (default 2 GiB) |
 | `MAX_SOURCE_DURATION_SECONDS` | server | SOURCE duration cap (default 8h; not the 60-180s clip setting) |
 | `UPLOAD_AUTH_TTL_SECONDS` | server | Upload-authorization TTL (default 900s) |
 | `WORKER_ENVIRONMENT` | worker | `development` / `staging` / `production` |
 | `WORKER_LOG_LEVEL` | worker | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
 | `WORKER_STORAGE_ROOT` | worker | Scratch directory only (temp files; deleted after each job) |
 | `WORKER_SUPABASE_URL` | worker | Supabase project (starts the ingestion loop with the key) |
 | `WORKER_SUPABASE_SERVICE_ROLE_KEY` | **worker only** | Never exposed to clients or Vercel bundles |
 | `WORKER_POLL_INTERVAL` | worker | Idle poll interval seconds (default 2) |
 | `WORKER_CONCURRENCY` | worker | Concurrent jobs (default 2; 1-16) |
 | `WORKER_JOB_LEASE_SECONDS` | worker | Claimed-job lease (default 300; 30-3600) |
 | `WORKER_MAX_ATTEMPTS` | worker | Reference retry budget (DB `jobs.max_attempts` is authoritative, default 3) |
  | `WORKER_REQUEST_TIMEOUT_SECONDS` | worker | Request timeout (0 < value ≤ 300) |
  | `WORKER_FFMPEG_PATH` / `WORKER_FFPROBE_PATH` | worker | FFmpeg/FFprobe binary locations (default `ffmpeg` / `ffprobe`) |
  | `WORKER_RENDER_CONCURRENCY` | worker | Clips rendered in parallel per job (default 2; 1-8) |
  | `WORKER_RENDER_TIMEOUT_SECONDS` | worker | Wall-clock budget for one clip render (default 900; ≤ 7200) |
  | `WORKER_MAX_CLIP_SIZE_BYTES` | worker | Reject a rendered clip larger than this (default 200 MiB) |
  | `WORKER_CAPTION_FONT_FAMILY` / `WORKER_CAPTION_FONT_SIZE` | worker | Subtitle font (default `DejaVu Sans`, 88px; 24-200) |
  | `WORKER_CAPTION_BASE_COLOR` / `WORKER_CAPTION_HIGHLIGHT_COLOR` / `WORKER_CAPTION_OUTLINE_COLOR` | worker | Subtitle colors, RRGGBB (default `FFFFFF` / `00D7FF` / `000000`) |
  | `WORKER_CAPTION_OUTLINE_WIDTH` / `WORKER_CAPTION_MARGIN_V` / `WORKER_CAPTION_WORDS_PER_LINE` / `WORKER_CAPTION_UPPERCASE` | worker | Subtitle layout (default 6 / 520 / 3 / true) |

Server-side validation is implemented in `src/lib/env.ts` (zod) and
`worker/app/config/settings.py` (Pydantic).

## Supabase setup

1. Create a **development** Supabase project (free tier is fine).
2. Store the project URL and keys in your environment (see table above).
  3. Apply the seven migrations to the development project (Supabase dashboard
    SQL editor or Supabase CLI), in order:
    - `supabase/migrations/20261113000000_initial_schema.sql` (Phase 1)
    - `supabase/migrations/20261114000000_phase2_job_settings_and_session_lookup.sql` (Phase 2)
    - `supabase/migrations/20261115000000_phase3_queue_and_ingestion.sql` (Phase 3 — queue columns, 9-state machine, restricted worker RPCs)
    - `supabase/migrations/20261115000001_phase3_storage_buckets.sql` (Phase 3 — private storage buckets; Supabase-specific)
    - `supabase/migrations/20261116000000_phase4_clip_analysis.sql` (Phase 4 — clip score/scoring columns, stage-aware queue RPCs, `analyzing` parked state)
    - `supabase/migrations/20261117000000_phase5_rendering.sql` (Phase 5A — `jobs.render_attempts` budget, `rendering` stage in the queue RPCs, `complete_job_rendering` guard, clips index)
    - `supabase/migrations/20261120000000_phase5b_captions.sql` (Phase 5B — `clips.captions jsonb`: the clip's real word-level timings for subtitle burn-in)

All seven are executed and asserted against a real PostgreSQL 16 engine (WASM)
in the test suite (`tests/database/rls.test.ts`, `tests/database/queue.test.ts`,
`tests/database/phase4-queue.test.ts`, `tests/database/storage-buckets.test.ts`,
`tests/database/phase5-rendering.test.ts`, `tests/database/phase5b-captions.test.ts`),
including deny-by-default RLS, the queue RPCs' atomicity/lease semantics, the
Phase 4 stage transitions (incl. crash recovery and the parked state), the
Phase 5 rendering lifecycle (parked claim → rendering, render-retry requeue,
crash recovery, the `complete_job_rendering` guard refusing early completion)
and the private-bucket configuration.

Tables: `processing_sessions`, `jobs`, `clips` — all with RLS enabled and no
anonymous policies (access is mediated exclusively by the server-side API).
Storage buckets: `clipforge-sources` (original uploads) and `clipforge-clips`
(rendered clips, `clips/{job_id}/{clip_id}.mp4`, written by the Phase 5A
worker) — both **private**, no client policies, service-role-only access.
No migrations are applied to any production database by this project, and
none of the live-verification above was run against a real Supabase project
in this repository (no credentials provided).

## Access code setup (Phase 2)

1. Choose a shared access code (keep it secret).
2. Store **only its SHA-256 hash** server-side in `ACCESS_CODE_HASH`
   (generate: `node -e "console.log(require('crypto').createHash('sha256').update('YOUR_CODE').digest('hex'))"`).
3. Without `ACCESS_CODE_HASH` set, `POST /api/access` returns 503.

Authentication flow (details in `docs/ARCHITECTURE.md`):
code → server compares SHA-256 hash in constant time (rate-limited: 5 / 15 min
per IP) → 256-bit random session token in an HttpOnly SameSite=Lax cookie (24h)
→ only the token's hash is stored in `processing_sessions`.

## Testing

```bash
# Frontend (vitest) — includes real-Postgres schema/RLS tests (pglite, WASM)
npm test

# Worker (pytest)
cd worker && python -m pytest
```

Coverage: application configuration, clip duration/count validation,
environment variable validation, **access-code authentication API, session
security (tokens, cookies, expiry, constant-time), rate limiting, job API
authorization (session scoping, anti-enumeration), upload authorization
(extension/MIME-spoof/size/path-traversal/rate-limit guards), upload completion
(object verification, size mismatch, idempotency, state guards), database
migrations, RLS and the queue RPCs (atomic claiming, concurrent workers, lease
expiry/recovery, bounded retries, ownership scoping, anon denial, **Phase 4
stage-aware claiming, crash recovery in every active stage, the parked
`analyzing` end state, and the explainable clip-score columns**) — all against
a real PostgreSQL engine** (frontend); worker health + heartbeat, worker env
configuration, queue client (RPCs + Phase 4 guarded transitions), **FFprobe
media validation with synthetic FFmpeg-generated videos (valid / no-audio /
no-video / corrupt / unsupported container / duration & size limits), FFmpeg
audio extraction (mono 16 kHz WAV, corrupt/missing/timeout paths), transcription
providers (honest unavailability, factory routing, guarded real-Whisper smoke
test, word-level timestamps, chunk splitting + midpoint-ownership merge), clip
detection (60-180s band, sentence boundaries, overlap control, determinism,
honest no-clip failures) and explainable scoring (5 weighted criteria with
reasons), rendering profiles (rotation normalization, cover/contain
geometry), **FFmpeg output contract (real FFmpeg/FFprobe: portrait /
landscape / square / rotated sources → exact 1080x1920 H.264+AAC+yuv420p,
rotation baked to zero, corrupt input, timeout kills the subprocess,
cancellation cleans up)**, clip store and clip uploads (path-shape
validation, size cap, upsert idempotency), ingestion lifecycle (download
failure, permanent vs transient errors, lease-loss abort, temp cleanup),
**analysis lifecycle (happy path to parked, crash recovery, no-audio /
no-speech / no-clips / unavailable-provider permanent failures, transient
storage/transcription/persistence failures, ownership-loss abort, temp
cleanup)** and **rendering lifecycle (full success + upload, resume skipping
`ready` clips, no-clips permanent failure, download / FFmpeg / validation /
upload / persistence failures, lease renewal loss, cancellation, progress
 and completion refusals, sibling cancellation on first clip error,
 idempotent retry without duplicate uploads)**, **captions (Phase 5B:
 deterministic ASS engine — timing sanitization, overlap resolution, line
 segmentation, escaping, RTL/Unicode; real-FFmpeg burn-in PROVEN in output
 pixels: active-word progression at multiple timestamps, portrait + rotated
 sources, Arabic/German, no override-block injection, style colors, timeout
 / cancellation cleanup; processor wiring — clip-relative conversion,
 disabled = Phase 5A, corrupt payload degrades to plain render, resume,
 transient failure)**, worker loop (stage-aware dispatch incl. Phase 5
 rendering, missing-renderer safe release, backoff, graceful shutdown)
 (worker).

The media tests generate their synthetic videos on the fly (no binary fixtures
committed). They use `ffprobe`/`ffmpeg` from PATH, or the npm statics
(`ffmpeg-static`, `ffprobe-static`) after `npm install`, or explicit
`FFPROBE_BIN` / `FFMPEG_BIN` environment overrides.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs on every push to `main` and on
pull requests:

- **frontend**: `npm ci` (lockfile) → typecheck → lint → build → tests
- **worker**: `pip install` → `compileall` (syntax) → import check → `pytest`

## Current implementation status

| Area | Status |
| --- | --- |
| Frontend foundation (Next.js, TS strict, Tailwind, shadcn-compatible UI kit) | Done |
| Landing / Access / Creation / Processing / Results pages | Done |
| Creation form validation (YouTube URL, clip count 1-10, duration 60-180s) | Done (client + server) |
| Python worker + `GET /health` + settings + JSON logging + Docker | Done |
| Supabase schema (3 tables, RLS deny-by-default) + Phase 2 columns | Version-controlled; tested against real Postgres (WASM); apply to a dev project before use |
| Access-code authentication (rate-limited, constant-time, hash-only) | **Done — `POST /api/access`** |
| Anonymous sessions (HttpOnly cookie, 24h, hash stored, sign-out) | **Done** |
 | Job creation API (`POST /api/v1/jobs`, session-scoped) | **Done** (upload → `uploading`; YouTube → 503 until an authorized provider is configured) |
 | Job status API (`GET /api/v1/jobs`, `GET /api/v1/jobs/[id]`) | **Done** (live polling UI, 9 states) |
 | Direct upload: authorize → browser PUT → verified completion | **Done** (signed URLs, private bucket, real progress + cancel + retry in the UI) |
 | Postgres job queue (atomic claim, leases, retry, crash recovery) | **Done** (restricted RPCs, tested on a real Postgres engine) |
 | Worker ingestion (claim → download → FFprobe validation → `ready`) | **Done** (bounded concurrency, lease renewal, graceful shutdown) |
 | YouTube source provider | **Interface only** — no authorized provider ships with the repo; creation returns a clear 503, uploads are unaffected. No unauthorized downloader exists or may be added |
 | Worker analysis: audio extraction + local Whisper transcription + clip detection/scoring | **Done** (optional `faster-whisper` dependency; honest `transcription_unavailable` failure when not installed; word-level timestamps; 60-180s windows; explainable scores persisted to `clips`) |
  | Jobs parked at `analyzing` with detected clips | **Done** (stage-aware queue; crash recovery; bounded retries) |
  | Clip rendering pipeline (Phase 5A): claim parked → render cut/reframe/encode to 1080x1920 H.264+AAC → validate → upload to `clipforge-clips` → DB-guarded `completed` | **Done** (rotation-correct, aspect-ratio-preserving; resume + bounded render retries; no false `completed` possible) |
  | Animated subtitles (Phase 5B): real word timings persisted at analysis → deterministic ASS (karaoke per-word highlight, RTL/Unicode-safe) burned into the SAME FFmpeg render; job-level `captions_enabled` toggle; no words / disabled = Phase 5A output | **Done** (burn-in proven in rendered pixels; corrupt payloads degrade to plain render, never crash, never fabricate) |
  | Subtitle burn-in / downloads | **Not implemented** (Phase 5b / Phase 6) |

## Known limitations

- **No live Supabase project has been used so far.** The migrations and the
  queue/storage semantics are fully tested against a real PostgreSQL engine
  (WASM), but nothing has been applied to a live project — none was provided.
  End-to-end upload → queue → worker ingestion → rendering therefore still
  requires: creating/applying the seven migrations to a verified development
  project, setting the service-role credentials, and running the worker.
  Until then, the app runs Phase 1-2 behavior and upload creation returns a
  clear error when Supabase is not configured.
- Phase 5B burns animated subtitles into the clips, but the Results page
  still shows placeholder data: rendered clips are stored in the private
  `clipforge-clips` bucket (object verified in tests, never exposed publicly)
  and there is no download API yet (Phase 6).
- Subtitle rendering quality depends on the worker host's fonts/fontconfig:
  the default font is DejaVu Sans (pinned in CI). Arabic contextual shaping
  additionally requires an FFmpeg build with libharfbuzz (the CI apt build
  has it; the local ffmpeg-static does not — Arabic then renders with
  isolated glyphs, still legible and correctly ordered). No fonts are
  bundled and no paid font service is used.
- Transcription runs locally with the **optional** `faster-whisper`
  dependency (`worker/requirements-optional.txt`). Without it the worker
  starts normally and analysis jobs fail with a clear permanent
  `transcription_unavailable` error — no fake output is ever produced. With
  it, the first run downloads the selected model weights from Hugging Face
  once (plain file download, no API/credentials/cost).
- YouTube sources: only the provider interface exists. Without an explicitly
  approved, authorized source provider the API returns a clear 503. No
  yt-dlp/cookies/DRM-bypass path exists in this codebase and none may be
  added. Direct file upload is fully functional and independent.
- Large uploads are not chunked/resumable at the storage protocol level; a
  browser interruption must re-PUT the whole file (the authorization can be
  re-requested and completion is idempotent, so a retry is always safe).
- Rate limiting is per-instance (in-memory). On multi-instance Vercel the
  effective limit is per-instance; a Redis-backed limiter is planned (Phase 7).
- Worker concurrency is per-process; running several worker replicas is safe
  (claims are atomic in Postgres) but the in-flight view in `/health` is
  per-replica.
- No monitoring/alerting yet (Phase 7).

## Roadmap

See [`docs/ROADMAP.md`](docs/ROADMAP.md) for the seven development phases.
