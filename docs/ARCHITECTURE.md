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
                         │  (short request/response    │
                         │   only — no heavy work)     │
                         └──────┬───────────────┬──────┘
                                │ service role  │ enqueues
                                ▼               ▼
                     ┌──────────────────┐  ┌─────────────────────────────┐
                     │  Supabase (PG)   │  │      Python worker          │
                     │  sessions/jobs/  │◄─┤  queue → source fetch →     │
                     │  clips (+ RLS)   │  │  transcribe → analyze →     │
                     └──────────────────┘  │  render → store             │
                                           │  FFmpeg · Whisper           │
                                           └──────────────┬──────────────┘
                                                          ▼
                                           ┌─────────────────────────────┐
                                           │  Storage (local FS now,     │
                                           │  object storage later)      │
                                           │  uploads/ · clips/ · zip/   │
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

- `POST /api/access` — verifies the shared access code against
  `ACCESS_CODE_HASH` (SHA-256, constant-time compare) and issues a short-lived
  session token as an `httpOnly` cookie (Phase 2).
- `POST /api/v1/jobs` — validates the payload, creates a `jobs` row and
  enqueues the worker (Phase 2/3).
- `GET /api/v1/jobs/[id]` — returns status/progress for the polling UI (Phase 2).
- `GET /api/v1/jobs/[id]/clips` — returns the clip list once complete (Phase 6).
- `GET /api/v1/clips/[id]` and ZIP export endpoints (Phase 6).
- Centralized error handling: typed errors mapped to stable HTTP responses;
  stack traces and internal details never leak to the client.

**The API layer must stay fast and stateless.** Anything that touches video
bytes (downloading, audio extraction, transcription, rendering, zipping)
belongs to the worker.

### Database (Supabase / Postgres)

- `processing_sessions` — access sessions (token hash + expiry).
- `jobs` — one row per creation request; carries the state machine.
- `clips` — generated clips per job, with source in/out timestamps.
- **Row Level Security is enabled on every table with no anonymous policies**:
  the only writer/reader in normal operation is the service-role client in the
  API layer (server code, `src/lib/supabase/server.ts`).
- Schema is version-controlled in `supabase/migrations/` and applied to
  development environments explicitly (never from CI, never to production
  without review).

### Storage

- Phase 1-3: local filesystem under `WORKER_STORAGE_ROOT`, laid out per job:
  `jobs/<job_id>/uploads/...`, `jobs/<job_id>/clips/clip-NN.mp4`.
  Path helpers in `worker/app/storage/paths.py` sanitize user-supplied names.
- Later: object storage for durability and direct streaming of clip
  previews/downloads.

### Worker (Python 3.12, separate service)

Modules and their phases:

| Module | Responsibility | Phase |
| --- | --- | --- |
| `app/api` | Health endpoint now; job control later | 1 / 3 |
| `app/queue` | Job models now; queue backend later | 1 / 3 |
| `app/processors` | Pipeline stage order now; execution later | 1 / 3 |
| `app/transcription` | Whisper transcription of the audio track | 4 |
| `app/ai` | Clip detection/scoring from the transcription | 4 |
| `app/rendering` | FFmpeg: reframe to 9:16, cut, animated subtitles, encode 1080x1920 | 5 |
| `app/storage` | Path layout now; object storage later | 1 / 3 |
| `app/config` | Pydantic settings (env-validated) + JSON structured logging | 1 |

The worker reports state transitions and progress back to `jobs`; the UI polls
the API. A failed job records `error_message` and stops.

## Job state machine

```
queued → uploading → transcribing → analyzing → rendering → completed
   └──────────────────┴──────────────────────────┴──────────────► failed
```

States are defined once in `src/config/app.ts` (frontend) and
`worker/app/queue/models.py` (worker) and kept in sync by a worker test.

## Security model

- **Single shared access code**, verified only server-side:
  `ACCESS_CODE_HASH` (SHA-256) in server env; constant-time comparison;
  success issues an httpOnly session cookie with a short expiry
  (`processing_sessions.expires_at`).
- **No user accounts** in Phase 1-2 design; rate limiting on `/api/access`
  to make brute force impractical (Phase 2).
- **Service-role key isolation**: `SUPABASE_SERVICE_ROLE_KEY` is read only in
  `server-only` modules (`src/lib/env.ts`, `src/lib/supabase/server.ts`) and
  is never prefixed `NEXT_PUBLIC_`.
- **RLS deny-by-default** on all tables.
- **Input validation** at every boundary: zod (API layer), Pydantic (worker),
  shared validators (UI). YouTube URLs are shape-validated and then fetched
  server-side by an authorized provider — the browser never talks to
  YouTube download endpoints.
- **Secrets** live in environment variables only; `.env*` is gitignored;
  `.env.example` contains placeholders.
- **Uploads**: size/type limits enforced server-side; file names sanitized to
  basenames; per-job storage isolation.

## Integration points (marked in code)

| Location | Marker | Lands in |
| --- | --- | --- |
| `src/components/access/access-form.tsx` | `INTEGRATION POINT (Phase 2)` | Access API |
| `src/components/create/creation-form.tsx` | `INTEGRATION POINT (Phase 2/3)` | Job submission |
| `src/components/create/upload-area.tsx` | `INTEGRATION POINT (Phase 3)` | Upload transfer |
| `src/app/processing/page.tsx` | `DEVELOPMENT PLACEHOLDER DATA` | Job polling |
| `src/app/results/page.tsx` | `DEVELOPMENT PLACEHOLDER DATA` | Results API |
| `worker/app/*` module docstrings | Phase markers | Phases 3-5 |
