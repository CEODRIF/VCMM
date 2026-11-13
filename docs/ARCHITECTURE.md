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

Implemented in Phase 2:

- `POST /api/access` — verifies the shared access code against
  `ACCESS_CODE_HASH` (SHA-256, constant-time compare), rate-limited
  (5 attempts / 15 min / IP), and issues a 256-bit session token as an
  HttpOnly, SameSite=Lax cookie (Secure in production, 24h lifetime).
- `DELETE /api/access` — destroys the session row and clears the cookie.
- `POST /api/v1/jobs` — session required; re-validates the payload server-side
  (zod); inserts a `jobs` row owned by the verified session. Worker enqueue
  lands in Phase 3.
- `GET /api/v1/jobs` — session required; lists only the session's jobs.
- `GET /api/v1/jobs/[id]` — session required; returns the job only if it is
  owned by the session; otherwise the same 404 as a missing id
  (anti-enumeration).

Coming: `GET /api/v1/jobs/[id]/clips`, `GET /api/v1/clips/[id]`, ZIP export
(Phase 6).

Centralized error handling (`src/lib/api/errors.ts`): typed `ApiError` mapped
to stable `{ "error": string }` responses; zod errors → 400; everything else →
generic 500 with server-side logging. Stack traces, database internals and
storage paths never leak to the client.

**The API layer must stay fast and stateless.** Anything that touches video
bytes (downloading, audio extraction, transcription, rendering, zipping)
belongs to the worker.

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
  success issues an httpOnly session cookie with a 24h expiry
  (`processing_sessions.expires_at`). Implemented and tested in Phase 2.
- **No user accounts**; rate limiting on `/api/access` (5 / 15 min / IP)
  makes brute force impractical. Implemented in Phase 2 (per-instance;
  Redis-backed global limit in Phase 7).
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

## Completed integrations (Phase 2)

- `src/components/access/access-form.tsx` → `POST /api/access` (real auth).
- `src/components/create/creation-form.tsx` → `POST /api/v1/jobs` (real job
  creation; 401 routes to the access page).
- `src/app/processing/page.tsx` → live polling of `GET /api/v1/jobs/[id]`
  via `?job=<id>` (placeholder overview kept for development).
- `src/components/site-header.tsx` → session-aware sign-out (`DELETE /api/access`).

## Remaining integration points (marked in code)

| Location | Marker | Lands in |
| --- | --- | --- |
| `src/components/create/upload-area.tsx` | `INTEGRATION POINT (Phase 3)` | Upload transfer |
| `src/lib/jobs/service.ts` | `INTEGRATION POINT (Phase 3)` | Worker enqueue / reserved upload path |
| `src/app/results/page.tsx` | `DEVELOPMENT PLACEHOLDER DATA` | Results API |
| `worker/app/*` module docstrings | Phase markers | Phases 3-5 |
