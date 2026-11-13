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

## Phase 2 — Access control & job API

- `POST /api/access`: verify shared access code against `ACCESS_CODE_HASH`
  (constant-time SHA-256 compare), issue httpOnly session cookie, store session
  in `processing_sessions`; rate limiting on the endpoint.
- Apply the Phase 1 migration to a **development** Supabase project and verify
  RLS behavior; add session-scoped policies where needed.
- `POST /api/v1/jobs` + `GET /api/v1/jobs/[id]`: create jobs, poll status;
  session middleware protecting job routes; centralized error handling.
- Wire the Access page to the real endpoint (no client-side auth).

## Phase 3 — Ingestion

- YouTube source via an **authorized source provider** (server-side fetch,
  never in the browser); upload path with size/type validation.
- Queue backend for jobs (worker consumes jobs; state transitions written to
  `jobs` with progress updates).
- Storage: per-job layout on local FS, object-storage abstraction behind
  `worker/app/storage`.
- Replace upload-area and creation-form placeholders with real transfers.

## Phase 4 — AI pipeline

- Audio extraction (FFmpeg) + Whisper transcription with word-level timings
  (`worker/app/transcription`).
- Clip detection/scoring selecting the requested count at the requested
  duration within 60-180s (`worker/app/ai`).
- Persist detected clips to `clips` (title, start/end, sort order).

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
