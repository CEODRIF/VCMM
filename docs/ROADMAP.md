# ClipForge AI — Development Roadmap

Eight phases, in order. Each phase ends with a shippable increment.

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

## Phase 5A — Rendering foundation ✅ (this commit)

- **Queue** (migration `20261117000000`): the claim RPC now also claims
  PARKED `analyzing` jobs (render budget available) and moves them to
  `rendering`; expired-lease crash recovery covers `rendering`; a transient
  rendering failure requeues to the parked `analyzing` state and consumes
  one `render_attempts` (dedicated budget, bounded by `jobs.max_attempts`,
  since `attempt_count` is consumed by every claim); a new
  `complete_job_rendering` RPC is the ONLY path to `completed` in rendering —
  it requires ownership, status `rendering`, ≥1 clip and EVERY clip `ready`
  with a `storage_path`. All 7 queue RPCs stay service-role-only.
- **Renderer** (`worker/app/rendering`): one managed FFmpeg subprocess per
  clip (wall-clock timeout → kill + await, partial output removed on every
  failure path, cancellation-safe). Source geometry is measured from the
  raw bytes: FFprobe plus a direct MP4 box scan for the `rot `/`dtrt`
  rotation atoms (version-independent ground truth). A detected clockwise
  rotation is baked into the pixels with an explicit `transpose`/flip filter
  (deterministic — this build's `-autorotate` ignores MP4 rotation boxes).
  Aspect ratio is never distorted: uniform scale + center-crop (cover) or
  black-pad (contain) to exact 1080x1920, H.264 (CRF 20, veryfast), 30 fps,
  yuv420p, AAC 128k, fast-start.
- **Output contract** (FFprobe on the rendered bytes, never trusted):
  MP4 container, H.264 video, AAC audio required, EXACT 1080x1920, yuv420p,
  stored rotation ≤ 0.5°, duration within ±15% of the clip window. Any
  violation is a permanent `render_invalid` failure — a non-conforming clip
  is never uploaded.
- **Processor** (`worker/app/processors/rendering.py`): download verified
  source → re-probe → render the remaining clips (skipping clips already
  `ready` — resume after crash/retry) under a per-job concurrency bound
  (`WORKER_RENDER_CONCURRENCY`) → validate each output → upsert-upload to
  `clipforge-clips` at `clips/{job_id}/{clip_id}.mp4` (path-shape validated,
  size-capped, idempotent) → mark `ready` with the storage path →
  `complete_job_rendering`. First clip error cancels siblings; same
  lease/ownership/temp-cleanup contract as Phases 3-4; the worker can never
  mark a job `completed` on its own (the DB guard enforces it).
- Tests: render profiles, FFmpeg run lifecycle (timeout/cancel/cleanup),
  output validation, **renderer integration with REAL FFmpeg/FFprobe**
  (portrait/landscape/square/rotated → exact 1080x1920, zero output
  rotation, corrupt, timeout, cancellation), full processor lifecycle
  (fakes + real FFmpeg: success, resume, no-clips, every failure category,
  lease loss, refusal paths, sibling cancellation, idempotent retry),
  Phase 5 queue semantics on a real PostgreSQL engine (pglite WASM),
  worker dispatch (rendering → renderer, missing renderer releases safely).
- Note: applied to a real Supabase project remains an explicit live step
  (verified development project required; none was provisioned in this
  phase).

## Phase 5B — Animated subtitles ✅ (this commit)

- **Persistence** (migration `20261120000000`, additive): `clips.captions
  jsonb` carries the REAL word-level transcription of the clip window
  (`{"words":[{"w","s","e"}]}`, absolute seconds, clipped to the window),
  written by the analysis stage — the renderer never re-transcribes and
  never invents words or timings. The job-level `captions_enabled` toggle
  already existed (Phase 2).
- **Engine** (`worker/app/rendering/captions.py`): pure, deterministic ASS
  generation for libass — timestamp sanitization (missing/invalid/
  overlapping/out-of-range entries dropped or resolved, never fabricated),
  line segmentation (words-per-line chunk + silence-gap breaks), per-word
  karaoke (`\k`) timing with the verified libass semantics (spoken word in
  the base color, upcoming words in the highlight color), full escaping
  (ASS markup in text, filtergraph path), and the persisted-payload parser
  (any corrupt payload degrades to a plain render).
- **Renderer** (`FFmpegRenderer`): the `ass=` filter is appended to the
  SAME filter chain (after the reframe plan, PlayRes == 1080x1920) — one
  command, no second pipeline. The ASS file lives in the per-job temp dir,
  is filtergraph-escaped, and is removed on success, failure, timeout and
  cancellation. With no words the command line is byte-for-byte Phase 5A.
- **Style** (config, deterministic defaults): font family (DejaVu Sans),
  size 88, base `FFFFFF`, highlight `00D7FF`, black outline 6px,
  MarginV 520 (safe area), 3 words/line, Latin uppercase. All configurable
  via `WORKER_CAPTION_*` env vars.
- **Languages**: Unicode-safe; Arabic RTL (bidi by libass/fribidi, logical
  order preserved in the file), English/German; uppercase never applied to
  non-Latin scripts. Glyph coverage/shaping depend on the system's
  fontconfig (documented); no bundled/paid fonts.
- **Tests**: 35 unit (timing/segmentation/escaping/styles/RTL/Unicode),
  11 real-FFmpeg integration (subtitles PROVEN in output pixels —
  active-word progression at 5 timestamps, portrait + rotated sources,
  Arabic/German, no-injection, style colors, timeout/cancellation cleanup,
  contract preserved), 7 processor wiring (spec conversion, disabled,
  missing, corrupt, resume, failure), +4 detector/store/config/extensions,
  +7 pglite (column, round-trip, idempotency, RLS, no 5A regression).
- Note: applied to a real Supabase project remains an explicit live step
  (verified development project required; none was provisioned in this
  phase).

## Phase 6 — Product completion ✅ (this commit)

- **Results API** (session + ownership verified on EVERY request;
  anti-enumeration extended to clip ids — non-UUID ⇒ 404 before any
  session/DB round trip; foreign/missing/not-ready/object-missing are all
  the same generic 404):
  - `GET /api/v1/jobs/[id]/results` — the job + ONLY its clips
    (`{id,title,duration,sortOrder,status,downloadable}`); `downloadable`
    requires `ready` AND a canonical `clips/{job_id}/{clip_id}.mp4` path
    cross-checked against the OWNING job; storage paths/ownership internals
    never leave the server; completed jobs additionally get
    `zip:{available,totalBytes,limitBytes}` (metadata-only) so the UI can
    decide the Download-all UX before the click.
  - `GET .../clips/[clipId]/preview` — 300s signed URL (inline), minted only
    after session + ownership + clip/job + `ready` + canonical path +
    object-existence verification; buckets stay private, no public URLs.
  - `GET .../clips/[clipId]/download` — 900s signed URL (attachment) with a
    server-sanitized single-segment filename (no traversal, no control
    chars, length-capped, empty-title fallback).
  - `GET .../download-all` — STREAMED STORE-method ZIP (zero new
    dependencies): JSON guards first (409 not-completed / no clips / missing
    objects, 413 over `MAX_ZIP_SIZE_BYTES` default 1 GiB), then an async
    generator pipes each clip from Storage chunk by chunk (concurrency 1,
    sort order, CRC in data descriptors) — server memory stays at one chunk,
    no temp files; entry names sanitized + index-prefixed; client cancel
    aborts the in-flight fetch and tears the response down (partial archives
    are discarded; per-clip URLs remain the fallback).
- **Results UI (live)**: `/results?job=<uuid>` renders a pure, unit-tested
  state machine (loading → processing (auto-poll 2.5s) / ready / failed /
  not-found / auth-required / error; ready splits into expired / empty /
  clip grid). Real `<video>` previews from lazy per-clip signed URLs with
  per-card retry, per-clip download buttons, Download-all anchor,
  accessible (roles, `aria-live`/`aria-busy`, labels) and responsive. The
  live processing panel gained a "View your clips" CTA on completion.
- **Retention & cleanup (explicit policy BEFORE any deletion)**:
  - Migration `20261123000000` (additive): `jobs.purged_at` + trigger — a
    job transitioning to `completed`/`failed` gets `expires_at =
    now() + 30 days`, only when NULL (operator deadlines win; the interval
    is a constant in one trigger function).
  - `worker/app/cleanup.py` — MANUAL `python -m app.cleanup` (NO scheduler
    wired): terminal + expired + un-purged jobs only; per job re-verified
    server-side, then job-scoped storage deletes (source directory +
    canonical clip objects — foreign/malformed `storage_path` skipped and
    logged), THEN the guarded `purged_at` update (at-least-once, crash-safe,
    idempotent; never marks before deletion; jobs without a deadline are
    never touched). 18 tests: scope, idempotency, partial-failure retry,
    cross-job refusal, non-terminal protection, CLI exit codes.
- **Tests**: results/preview/download API (authorization, cross-user
  denial, anti-enumeration, signed-URL params, missing objects, hostile
  filenames), streamed-ZIP route (valid archive parse-back, sanitized
  names, determinism, 409/413 guards, cancellation, mid-stream failure),
  view-state machine (all phases + expiry boundary), pglite retention
  trigger (9), ZIP engine (10) — plus the full Phase 1-5 suites green.
- Note: applied to a real Supabase project remains an explicit live step
  (verified development project required; none was provisioned in this
  phase), and the purge is an operator action by design until then.

## Phase 7 — Stream Intelligence Foundation ✅ (TASK 007, this branch)

Long-form gaming-stream highlight extraction: the system analyzes the
ENTIRE recording, whatever its duration, in bounded, resumable chunks, and
ranks candidate clips from measured multimodal evidence. Full audit +
design: [`docs/stream-intelligence.md`](stream-intelligence.md).

- **No product duration cap** (the old 8 h infrastructure limit is removed;
  the size cap stays as the disk bound). Operational limits only: chunk
  size, per-claim budget, decode timeouts — configurable via
  `WORKER_INTEL_*`.
- **Chunk engine** (`worker/app/intel/`): deterministic overlap planner,
  midpoint ownership (a boundary event is seen by both neighbours and
  persisted by exactly one), per-chunk `-ss/-t` bounded decodes (audio 16 kHz
  mono WAV ≈ 19 MB at 600 s; visual passes at 2 fps, null muxer, no disk),
  sequential execution, cancellation + temp cleanup.
- **Checkpoints + exact dedup**: migration `20261124000000` (additive;
  `processing_checkpoints` PK (job,chunk) + `candidate_events`
  PK (job,event_key) + `jobs.source_type` += 'twitch'; deny-by-default RLS,
  no policies). Events are upserted BEFORE the checkpoint, so a crash in
  between is a safe re-detect + dedup — proven by tests (zero reprocessing,
  final state identical to a clean run).
- **Signals (measured, never fabricated)**: audio energy spikes, bright
  transients, sustained voice-activity shifts, silence gaps; visual scene
  cuts (ffmpeg `select='gt(scene,…)'`) and motion bursts (tblend
  difference). A loud sound alone CANNOT qualify (single-family discount;
  SFX-only transients ×0.2 → never over the bar). Facecam / OCR / HUD are
  explicit, documented non-goals of this phase (interfaces reserved).
- **Fusion + selection**: weighted independent-family scoring (2 families
  → diversity bonus; confidence = fraction of families), context windows
  (pre 4 s / post 8 s), silence-gap boundary snap (speech-safe), merge +
  IoU-0.5 near-duplicate removal, 60–180 s product bounds, 8 heuristic
  categories (always labeled heuristic) + `unclassified`, ranked by
  (score, confidence, start).
- **Rendering**: source-aware frame rate — a measured ≥55 fps source renders
  at 60 (`-r 60`), everything else keeps the 30 profile default (no frame
  duplication; the output fps is ffprobe-verified in tests). The rest of
  the Phase 5A/5B pipeline is untouched.
- **Providers** (`worker/app/providers/`): protocol + registry, no
  guessing. YouTube (keyless oEmbed) and Twitch (keyless Helix listing)
  are VALIDATION-ONLY and refuse materialization explicitly
  (`MaterializationUnavailable`) — support is not claimed until an
  authorized materialization path exists. Upload is fully implemented
  (probe + injected bounded download). No yt-dlp, no DRM bypass.
- **Benchmarks** (`worker/benchmarks/`): deterministic synthetic fixture
  with 6 ground-truth events (4 boundary-straddling); JSON report with
  `measured` vs `targets` strictly separated. Synthetic results are labeled
  as such; **human-annotated real-VOD evaluation remains OUTSTANDING**
  (framework ready, no real annotated fixtures exist in the repo).
- **Tests**: +145 worker tests (chunk plan, signals, audio, visual,
  detectors, fusion, selection, checkpoints, pipeline recovery, providers,
  benchmarks, config/media/render updates) — full worker suite 477 passed;
  +9 pglite schema tests; frontend regression 279 passed.
- **Integration seam (next task, deliberately NOT wired here)**: the
  in-loop `AnalysisProcessor` hook needs a queue-side "incomplete, continue
  later" transition (distinct from the Phase 5A parked state) —
  `docs/stream-intelligence.md` §11. Until then Phases 4-6 behave exactly
  as before; this phase adds capability without changing any live path.
- Note: migration applied to a real Supabase project remains an explicit
  live step (verified development project; never production).

## Phase 8 — Hardening & launch

- Load testing and queue backpressure.
- Observability: structured metrics, alerting, request tracing.
- Security review: rate limits, secret rotation, dependency audits.
- Production deploy: Vercel (frontend), worker on dedicated compute with
  volumes; production Supabase migration applied with review.
- Launch checklist: docs, access-code distribution, monitoring on call.
