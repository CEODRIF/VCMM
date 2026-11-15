# Video Analysis — Full-Timeline Coverage, Gemini, Clip Planning

What happens between `uploading` and `rendering`: the **entire source
timeline** is analyzed (not a sampled head), per-window semantic
assessment goes through Gemini (optional, never fatal), and the clip set
is planned from the requested generation mode.

## Pipeline

1. **Probe & transcribe** — ffprobe report + Whisper transcription
   (chunked, `WORKER_WHISPER_*`) produce the timeline ground truth
   (duration, streams, word timings for captions).
2. **Timeline windows** (`worker/app/ai/timeline.py`) — the source is
   tiled into analysis windows (`WORKER_GEMINI_TIMELINE_WINDOW_SECONDS`,
   bounded by `WORKER_GEMINI_TIMELINE_MAX_WINDOWS`). Every window gets a
   row in `public.analysis_timeline` (TASK 015 P2 migration) with a
   status that is **written, not assumed**: `analyzed`, `failed`, or
   `not_configured`.
3. **Gemini semantic assessment** (`worker/app/ai/gemini.py`) — one call
   per window (text; optionally a global File-API video pass, off by
   default via `WORKER_GEMINI_VIDEO_UNDERSTANDING_ENABLED`).
4. **Local signals** — stream-intelligence chunks (TASK 007), facecam
   (TASK 008), game vision (TASK 009), multimodal fusion (TASK 010) run
   regardless of Gemini and feed the transparent scorer
   (`worker/app/ai/scoring.py`: every clip carries `score` + a
   human-readable `scoring` breakdown persisted to `clips.scoring`).
5. **Clip planning** (`worker/app/ai/clip_planning.py`) — turns analysis
   into the `clips` rows for the requested mode (see
   [video-generation.md](./video-generation.md)).

## Gemini key management & cooldown (exact behavior)

- Keys: `WORKER_GEMINI_API_KEYS` (comma-separated pool; legacy
  `WORKER_GEMINI_API_KEY` accepted). Model/base/timeout/retries:
  `WORKER_GEMINI_MODEL`, `WORKER_GEMINI_API_BASE`,
  `WORKER_GEMINI_TIMEOUT_SECONDS`, `WORKER_GEMINI_MAX_RETRIES`,
  `WORKER_GEMINI_BACKOFF_SECONDS`.
- **Round-robin KeyPool.** A 429/quota response puts that key in cooldown
  for `WORKER_GEMINI_COOLDOWN_SECONDS` (default **120 s**); 400/401/404
  mark the key **permanently** unusable for the process lifetime. The
  pool always tries the next key.
- **All keys cooling** → `GeminiRateLimitedError("all Gemini keys are in
  cooldown (quota)")`. **No keys configured** →
  `GeminiNotConfiguredError("no Gemini key configured
  (WORKER_GEMINI_API_KEYS)")` raised **before any network call**.
- **Job impact: never fatal on its own.** A per-window Gemini failure is
  recorded in the `analysis_timeline` row (`not_configured` / `failed`)
  and the analysis continues; "a Gemini failure NEVER marks the timeline
  as fully analyzed, and never fails the job on its own". Only
  timeline-invariant violations or storage failures fail the job
  (`ERROR_TIMELINE`, transient → retried within the attempt budget).
- **Mode B (Viral Highlights) works without Gemini**: local-signal
  windows with confidence ≥ 0.6 qualify, so unconfigured deployments
  still produce clips (fewer, and without the semantic layer).

## Coverage guarantee (P2)

The timeline tiling covers 100% of the source duration by construction,
and the DB enforces the invariants (window bounds inside the source,
no gaps in the full-coverage bookkeeping). Clips may only reference
verified timeline windows; the completion guard
(`complete_job_rendering`, RPC) refuses `completed` unless every clip is
`ready` **and** has a verified `storage_path`.

## Testing evidence

- Worker: timeline tiling/coverage unit tests, Gemini key-pool/cooldown
  tests (deterministic fakes — no live Gemini calls), clip-planning unit
  tests (incl. IoU ≤ 0.3 dedup invariant, remainder keep/merge policy).
- Database-engine: `tests/database/task15-analysis-timeline.test.ts`
  (PGlite, real migration) proves the timeline schema + invariants.

No live-external-provider verification is performed in CI; provider and
Gemini behavior is covered by deterministic adapters and the error/
cooldown contract tests.
