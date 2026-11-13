# ClipForge AI

ClipForge AI is a web application that transforms long-form videos into short-form vertical clips using AI.

- No user registration — one shared access code.
- YouTube URL input (authorized source provider) or direct video file uploads.
- AI-powered clip detection, clips between 60 and 180 seconds.
- Vertical 9:16 output at 1080x1920 (1080p) with modern animated subtitles.
- Individual downloads and ZIP batch downloads.

> **Status: Phase 3 — video ingestion, Supabase Storage & worker queue.**
> Real direct uploads to private Supabase Storage (server-signed upload URLs,
> verified completion), FFprobe-based media validation in the Python worker,
> and a Postgres-backed job queue with atomic claiming, leases and crash
> recovery. On top of Phase 2's access-code authentication and session-scoped
> job APIs. AI transcription/analysis and clip rendering arrive in Phases 4-5
> (see `docs/ROADMAP.md`).

## Technology stack

 | Layer     | Technology                                                        |
 | --------- | ----------------------------------------------------------------- |
 | Frontend  | Next.js (App Router), TypeScript (strict), Tailwind CSS, shadcn/ui-compatible components, Lucide icons |
 | Hosting   | Vercel (frontend + API only — no video processing in serverless)  |
 | Database  | Supabase (Postgres) with Row Level Security                       |
 | Storage   | Supabase Storage (private buckets, signed upload URLs)            |
 | Worker    | Python 3.12, FastAPI, Pydantic, FFprobe, structured JSON logging  |
 | Pipeline  | Whisper (Phase 4), FFmpeg rendering (Phase 5)                     |
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
                                                  │  ingestion (Phase 3);   │
                                                  │  transcribe/analyze/    │
                                                  │  render: Phases 4-5     │
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

Server-side validation is implemented in `src/lib/env.ts` (zod) and
`worker/app/config/settings.py` (Pydantic).

## Supabase setup

1. Create a **development** Supabase project (free tier is fine).
2. Store the project URL and keys in your environment (see table above).
 3. Apply the four migrations to the development project (Supabase dashboard
    SQL editor or Supabase CLI), in order:
    - `supabase/migrations/20261113000000_initial_schema.sql` (Phase 1)
    - `supabase/migrations/20261114000000_phase2_job_settings_and_session_lookup.sql` (Phase 2)
    - `supabase/migrations/20261115000000_phase3_queue_and_ingestion.sql` (Phase 3 — queue columns, 9-state machine, restricted worker RPCs)
    - `supabase/migrations/20261115000001_phase3_storage_buckets.sql` (Phase 3 — private storage buckets; Supabase-specific)

All four are executed and asserted against a real PostgreSQL 16 engine (WASM)
in the test suite (`tests/database/rls.test.ts`, `tests/database/queue.test.ts`,
`tests/database/storage-buckets.test.ts`), including deny-by-default RLS, the
queue RPCs' atomicity/lease semantics, and the private-bucket configuration.

Tables: `processing_sessions`, `jobs`, `clips` — all with RLS enabled and no
anonymous policies (access is mediated exclusively by the server-side API).
Storage buckets: `clipforge-sources` (original uploads) and `clipforge-clips`
(rendered output, Phases 5-6) — both **private**, no client policies,
service-role-only access. No migrations are applied to any production
database by this project, and none of the live-verification above was run
against a real Supabase project in this repository (no credentials provided).

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
expiry/recovery, bounded retries, ownership scoping, anon denial) — all against
a real PostgreSQL engine** (frontend); worker health + heartbeat, worker env
configuration, queue client, **FFprobe media validation with synthetic
FFmpeg-generated videos (valid / no-audio / no-video / corrupt / unsupported
container / duration & size limits)**, ingestion lifecycle (download failure,
permanent vs transient errors, lease-loss abort, temp cleanup), worker loop
(claim, backoff, graceful shutdown) (worker).

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
 | AI transcription / clip detection | **Not implemented** (Phase 4) |
 | Clip rendering / subtitles / downloads | **Not implemented** (Phases 5-6) |

## Known limitations

- **No live Supabase project was used in this phase.** The migrations and the
  queue/storage semantics are fully tested against a real PostgreSQL engine
  (WASM), but nothing has been applied to a live project — none was provided.
  End-to-end upload → queue → worker ingestion therefore still requires:
  creating/applying the four migrations to a verified development project,
  setting the service-role credentials, and running the worker. Until then,
  the app runs Phase 1-2 behavior and upload creation returns a clear error
  when Supabase is not configured.
- Jobs end ingestion at `ready`; nothing transcribes, analyzes or renders yet
  (Phases 4-5), so no clips are produced and the Results page shows
  placeholder data (Phase 6; downloads not implemented).
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
