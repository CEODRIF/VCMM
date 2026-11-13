# ClipForge AI

ClipForge AI is a web application that transforms long-form videos into short-form vertical clips using AI.

- No user registration — one shared access code.
- YouTube URL input (authorized source provider) or direct video file uploads.
- AI-powered clip detection, clips between 60 and 180 seconds.
- Vertical 9:16 output at 1080x1920 (1080p) with modern animated subtitles.
- Individual downloads and ZIP batch downloads.

> **Status: Phase 2 — secure access control & job management.** Server-side
> access-code authentication (rate-limited, constant-time, hash-only), temporary
> anonymous sessions in HttpOnly cookies, session-scoped job creation/status
> APIs, and the Supabase schema with RLS — tested against a real PostgreSQL
> engine in CI. Video download/processing/rendering still arrives in
> Phases 3-5 and is clearly marked in the code and in `docs/ROADMAP.md`.

## Technology stack

| Layer     | Technology                                                        |
| --------- | ----------------------------------------------------------------- |
| Frontend  | Next.js (App Router), TypeScript (strict), Tailwind CSS, shadcn/ui-compatible components, Lucide icons |
| Hosting   | Vercel (frontend only — no video processing in serverless)        |
| Database  | Supabase (Postgres) with Row Level Security                       |
| Worker    | Python 3.12, FastAPI, Pydantic, structured JSON logging           |
| Pipeline  | FFmpeg (Phase 5), Whisper (Phase 4)                               |
| Tests/CI  | Vitest, pytest, GitHub Actions                                    |

## Architecture overview

```
┌────────────┐      ┌─────────────────┐      ┌──────────────────────────────┐
│  Browser   │ ───► │  Next.js (Vercel)│ ───► │  Supabase (Postgres + RLS)  │
│  (UI only) │      │  routing + API   │      │  sessions · jobs · clips     │
└────────────┘      └────────┬────────┘      └──────────────────────────────┘
                             │ enqueues jobs
                             ▼
                  ┌─────────────────────┐
                  │  Python worker      │  download → transcribe → analyze
                  │  (separate service) │  → render 9:16 → store clips
                  └─────────────────────┘
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
| `WORKER_ENVIRONMENT` | worker | `development` / `staging` / `production` |
| `WORKER_LOG_LEVEL` | worker | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `WORKER_STORAGE_ROOT` | worker | Base directory for uploads and rendered clips |
| `WORKER_REQUEST_TIMEOUT_SECONDS` | worker | Request timeout (0 < value ≤ 300) |

Server-side validation is implemented in `src/lib/env.ts` (zod) and
`worker/app/config/settings.py` (Pydantic).

## Supabase setup

1. Create a **development** Supabase project (free tier is fine).
2. Store the project URL and keys in your environment (see table above).
3. Apply both migrations to the development project (Supabase dashboard SQL
   editor or Supabase CLI), in order:
   - `supabase/migrations/20261113000000_initial_schema.sql` (Phase 1)
   - `supabase/migrations/20261114000000_phase2_job_settings_and_session_lookup.sql` (Phase 2)

Both migrations are executed and asserted against a real PostgreSQL 16 engine
(WASM) in the test suite (`tests/database/rls.test.ts`), including the
deny-by-default RLS behavior.

Tables: `processing_sessions`, `jobs`, `clips` — all with RLS enabled and no
anonymous policies (access is mediated exclusively by the server-side API).
No migrations are applied to any production database by this project.

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
authorization (session scoping, anti-enumeration), database migrations and RLS
against a real PostgreSQL engine** (frontend); worker health endpoint, worker
env configuration, module imports/contracts (worker).

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
| Job creation API (`POST /api/v1/jobs`, session-scoped) | **Done** |
| Job status API (`GET /api/v1/jobs`, `GET /api/v1/jobs/[id]`) | **Done** (live polling UI) |
| Queue consumption / video processing | **Not implemented** (Phase 3+) — jobs stay `queued` |
| Clip rendering / subtitles / downloads | **Not implemented** (Phases 5-6) |

## Known limitations

- No video is downloaded, transcribed, analyzed or rendered anywhere yet —
  created jobs remain `queued` until the Phase 3 queue integration.
- Upload-source jobs are created with a reserved storage path, but the file
  transfer itself arrives in Phase 3.
- Rate limiting is per-instance (in-memory). On multi-instance Vercel the
  effective limit is per-instance; a Redis-backed limiter is planned (Phase 7).
- The migrations are executed and asserted in tests against a real PostgreSQL
  engine (WASM), but not yet applied to a live Supabase project — apply both
  migrations to a development project before end-to-end use (see above).
- The Results page still shows placeholder data (Phase 6); downloads are not
  implemented.
- YouTube ingestion will use an authorized source provider (Phase 3); no
  downloader is implemented and none should be added ad hoc.
- No monitoring/alerting yet (Phase 7).

## Roadmap

See [`docs/ROADMAP.md`](docs/ROADMAP.md) for the seven development phases.
