# ClipForge AI — Worker

Video processing runs **exclusively** in this worker service, never inside
Vercel serverless functions.

Phase 3 delivers **ingestion**: the worker claims jobs atomically from the
Supabase Postgres queue, downloads the source from private Supabase Storage,
validates the real bytes with FFprobe, and marks the job `ready` (with
measured media metadata) — or fails it with a safe, categorized error
(bounded retries for transient errors; immediate terminal failure for
unrecoverable media).

Phase 4 adds the **AI processing engine**: claimed `ready` jobs move to
`transcribing` (the claim RPC is stage-aware), the worker extracts the audio
track (FFmpeg → mono 16 kHz WAV), transcribes it with the **local**
Whisper-compatible engine (word-level timestamps, controlled chunking for
long recordings), detects complete-idea clip windows within the requested
60-180s duration, scores them with an explainable 5-criterion ranking, and
persists the clips (score + per-criterion reasons) into `clips`. The job then
**parks at `analyzing`** (lock cleared, not claimable as analysis).

Phase 5A adds the **rendering pipeline**: the stage-aware claim also picks
up parked `analyzing` jobs (render budget available) and moves them to
`rendering`. The worker re-downloads the verified source, cuts each detected
window with a managed FFmpeg subprocess (rotation-correct,
aspect-ratio-preserving reframe to exact 1080x1920 H.264 + AAC), validates
every rendered file against a strict output contract (container, codecs,
resolution, pixel format, zero rotation, duration tolerance), upserts it to
the private `clipforge-clips` bucket at `clips/{job_id}/{clip_id}.mp4`,
marks the clip `ready` with its storage path, and completes the job ONLY via
the `complete_job_rendering` RPC — which the database enforces (it refuses
unless every clip is `ready` with a stored object). Transient rendering
failures requeue to the parked state (dedicated `render_attempts` budget);
resumes skip clips already `ready`. The worker never marks a job `completed`
on its own.

Phase 5B burns **animated, word-synchronized subtitles** into the clips in
the SAME FFmpeg command (libass `ass=` filter appended after the reframe
plan — no second pipeline):

- The analysis stage persists each clip's REAL word timings
  (`clips.captions`, absolute seconds, clipped to the window); the renderer
  converts them to clip-relative time and generates a deterministic ASS
  file in the per-job temp dir. It never re-transcribes and never invents
  text or timings — missing/invalid/overlapping/out-of-range entries are
  dropped or resolved, and a corrupt payload degrades to a plain render.
- Visual: karaoke per-word progression (a word settles into the base color
  as it is spoken; upcoming words glow in the highlight color), 3 words per
  line, bottom safe area, outline for readability — all configurable via
  `WORKER_CAPTION_*` (see table below). The job-level `captions_enabled`
  flag (Phase 2 column, default `true`) gates the feature per job; with it
  off — or with no words — the render is byte-for-byte Phase 5A.
- Language: Unicode-safe, Arabic RTL (bidi handled by libass/fribidi) and
  mixed Arabic/Latin, English and German. Uppercase applies to Latin words
  only. Rendering quality depends on the system's fontconfig: the default
  font is DejaVu Sans (pinned in CI); Arabic contextual shaping requires an
  FFmpeg build with libharfbuzz (the CI apt build has it; the local
  ffmpeg-static does not — isolated glyphs). No fonts are bundled.

Phase 7 (TASK 007) adds the **stream-intelligence foundation** — long-form
analysis with NO product-level source-duration cap (the old 8 h limit is
gone; the size cap remains as the disk bound):

- `app/intel/` — the chunk engine: deterministic overlap planning, midpoint
  ownership (boundary events seen by both neighbours, persisted by exactly
  one), bounded per-chunk decodes (audio: mono 16 kHz WAV ≈ 19 MB at 600 s;
  visual: two 2 fps null-muxer passes, no disk), numpy signal features,
  detectors, multimodal fusion (a loud sound alone CANNOT qualify —
  single-family discount, SFX-only transients ×0.2) and context-aware clip
  selection (silence-snapped boundaries, merge, IoU dedup, 60–180 s bounds,
  8 heuristic categories, ranked).
- **Resumable & crash-safe**: checkpoints + candidate events persist in the
  Phase 7 tables (migration `20261124000000`); events are upserted BEFORE
  the chunk checkpoint, so a crash in between re-detects and exact-dedups.
  `chunks_per_claim` bounds each invocation; a finished job's re-run is a
  no-op. In-loop wiring into `AnalysisProcessor` is a documented next step
  (`docs/stream-intelligence.md` §11) — Phases 4-6 are unchanged.
- `app/providers/` — source adapters OUTSIDE the pipeline: YouTube/Twitch
  validation-only (keyless oEmbed / Helix; they REFUSE materialization
  explicitly — no support is claimed without an authorized path), upload
  fully implemented (probe + injected bounded download). No yt-dlp, no DRM
  bypass, no network guessing.
- **Source-preserved frame rate**: the renderer measures the source's
  average fps and renders at that rate — fractional NTSC included
  (23.976/29.97/55/59.94) — capped at `WORKER_RENDER_FPS_HIGH` (60); the
  output never exceeds the measured rate, so frames are never duplicated
  to fake a rate and nothing in 23.976…60 is silently clamped. Unknown
  rates keep the 30 fps profile default.
- **Benchmarks** (`worker/benchmarks/`, `python -m benchmarks.run`): a
  deterministic synthetic fixture with ground-truth events; JSON report
  separating measured from targets. Synthetic ≠ real-world accuracy —
  human-annotated real-VOD evaluation is an outstanding follow-up.

## Storage model — read this first

**Supabase Storage is the durable source of truth.** The worker's local disk
(`WORKER_STORAGE_ROOT`) is a processing scratchpad only: each job downloads
its source (and renders its clip files) into `tmp/{job_id}/`, and that
directory is deleted on every path — success, failure or cancellation.
Nothing durable is written to local disk. Phase 5A writes each rendered clip
to the `clipforge-clips` bucket (`clips/{job_id}/{clip_id}.mp4`, upsert)
before the temp file is removed.

## Requirements

- Python 3.12
- `ffprobe` (and `ffmpeg`, used by tests to generate synthetic fixtures) on
  the host or in the image — or set `WORKER_FFPROBE_PATH` /
  `WORKER_FFMPEG_PATH`. The Docker image installs them via apt.
- The Phase 3, 4 **and 5** database migrations applied to the same Supabase
  project (queue RPCs are granted to `service_role`). Phase 5B adds one more
  migration (`20261120000000`, `clips.captions jsonb`) — without it, clips
  simply render without subtitles (the column is read defensively).
  Phase 7 adds `20261124000000` (checkpoint + candidate-event tables,
  additive `twitch` source type) — without it, the intel pipeline has no
  store to resume from (the job loop does not call it yet).
- For subtitles: an FFmpeg build with **libass** (the CI apt build and the
  bundled ffmpeg-static both include it) and a fontconfig-resolvable font
  (DejaVu Sans by default; pinned via `fonts-dejavu-core` in CI).

### Transcription engine (Phase 4, optional but required to produce clips)

The base `requirements.txt` starts the worker WITHOUT any AI dependency.
To enable transcription, install the optional engine (local inference only —
no cloud, no credentials, no paid APIs):

```bash
pip install -r requirements.txt -r requirements-optional.txt
```

> `requirements-optional.txt` pins `faster-whisper==1.2.1` and `av>=11,<16`
> — `av` must stay below 16 (faster-whisper 1.2.x uses a PyAV argument that
> version 16 removed).

Then select a model (`tiny` is the smallest and is enough to validate the
pipeline; `small`/`medium` give better accuracy at a CPU cost):

```bash
export WORKER_WHISPER_MODEL=small        # empty/absent = transcription disabled
export WORKER_TRANSCRIPTION_LANGUAGE=fr  # optional; default = auto-detect
```

On first use the model weights are downloaded once from Hugging Face to the
local cache (a plain file download — no account, no API key, no cost).
**Without the optional packages (or with an empty `WORKER_WHISPER_MODEL`)
the worker still runs, and analysis jobs fail honestly** with the permanent
category `transcription_unavailable` — no fake output is ever produced.

Other Phase 4 knobs (defaults in brackets): `WORKER_TRANSCRIPTION_CHUNK_SECONDS`
[300], `WORKER_TRANSCRIPTION_OVERLAP_SECONDS` [15], `WORKER_WHISPER_DEVICE`
[cpu], `WORKER_WHISPER_COMPUTE_TYPE` [int8], `WORKER_AUDIO_SAMPLE_RATE` [16000],
`WORKER_AUDIO_EXTRACT_TIMEOUT_SECONDS` [600].

## Local development

```bash
cd worker
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# API only (no queue consumption):
uvicorn app.main:app --reload --port 8000

# With the ingestion loop (requires Supabase credentials):
export WORKER_SUPABASE_URL=https://<project>.supabase.co
export WORKER_SUPABASE_SERVICE_ROLE_KEY=<service-role key>
uvicorn app.main:app --reload --port 8000
```

Check it:

```bash
curl -s http://localhost:8000/health | python -m json.tool
```

`/health` includes a `worker` heartbeat (worker id, active jobs, uptime) when
the ingestion loop is configured; `"worker": null` means API-only mode.
Without `WORKER_SUPABASE_URL` + `WORKER_SUPABASE_SERVICE_ROLE_KEY` the loop
does not start (and never fails the API).

## Tests

```bash
cd worker
python -m pytest
```

The media tests generate synthetic videos with FFmpeg (lavfi testsrc/sine) at
run time — no binary fixtures are committed. Binaries are resolved from
`FFMPEG_BIN`/`FFPROBE_BIN` env overrides, then PATH, then the repo's npm
statics (`ffmpeg-static`/`ffprobe-static` after `npm install` in the repo
root); tests skip cleanly when no binary is available.

## Artifact cleanup (Phase 6) — manual only

Rendered artifacts are retained for **30 days after a job reaches a terminal
state** (`completed` or `failed`): the migration
`20261123000000_phase6_results_retention.sql` sets
`jobs.expires_at = now() + 30 days` via trigger when a job TRANSITIONS to a
terminal state (only when NULL — an operator-set deadline always wins).

The purge itself is a **manual command** — nothing destructive is scheduled
automatically:

```bash
cd worker
python -m app.cleanup            # purge up to 10 expired jobs
python -m app.cleanup --limit 50 # bigger batch
```

Per job it does, in order (at-least-once, crash-safe):

1. Re-verifies the row server-side: TERMINAL + EXPIRED + un-purged
   (`purged_at IS NULL`) — in-progress jobs are never touched.
2. Deletes `sources/{job_id}/original/*` (listed, then removed).
3. Deletes the job's clip objects — only canonical
   `clips/{job_id}/{clip_id}.mp4` paths whose stored `storage_path` matches
   the shape AND the owning job (foreign/malformed paths are skipped and
   logged).
4. Sets `purged_at` with a guarded update (terminal + still expired + still
   un-purged).

If the process dies between (3) and (4), re-running is safe: already-deleted
objects are no-ops and the marker is finally set. A job is never marked
purged while its objects still exist. Jobs without an `expires_at` (created
before the migration) are never purged — no guessed retention period.

## Configuration

All settings use the `WORKER_` env prefix and are validated by Pydantic at
startup (see `app/config/settings.py`):

| Variable | Default | Notes |
| --- | --- | --- |
| `WORKER_ENVIRONMENT` | `development` | `development` \| `staging` \| `production` |
| `WORKER_LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL` |
| `WORKER_STORAGE_ROOT` | `storage` | Scratch space only (temp files; deleted per job) |
| `WORKER_SUPABASE_URL` | – | Supabase project; ingestion loop needs URL **and** key |
| `WORKER_SUPABASE_SERVICE_ROLE_KEY` | – | Server-side only; never logged |
| `WORKER_POLL_INTERVAL` | `2` | Seconds between idle claim attempts (0 < x ≤ 60) |
| `WORKER_CONCURRENCY` | `2` | Jobs processed in parallel (1-16) |
| `WORKER_JOB_LEASE_SECONDS` | `300` | Lease per claimed job (30-3600; matches the RPC rule) |
| `WORKER_MAX_ATTEMPTS` | `3` | Reference retry budget; DB `jobs.max_attempts` is authoritative |
| `WORKER_FFMPEG_PATH` / `WORKER_FFPROBE_PATH` | `ffmpeg` / `ffprobe` | Binary locations (static builds, custom installs) |
| `WORKER_FFPROBE_TIMEOUT_SECONDS` | `60` | Probe timeout (0 < x ≤ 600) |
| `WORKER_REQUEST_TIMEOUT_SECONDS` | `30` | Must be > 0 and ≤ 300 |
| `WORKER_WORKER_ENABLED` | `true` | Set `false` for API-only deployments |
| `WORKER_RENDER_CONCURRENCY` | `2` | Clips rendered in parallel per job (1-8); each clip is one FFmpeg subprocess |
| `WORKER_RENDER_TIMEOUT_SECONDS` | `900` | Wall-clock budget for one clip render (0 < x ≤ 7200); the subprocess is killed and awaited after it |
 | `WORKER_MAX_CLIP_SIZE_BYTES` | `209715200` (200 MiB) | A rendered clip larger than this is rejected (not uploaded) |
 | `WORKER_RENDER_FPS_LOW` / `WORKER_RENDER_FPS_HIGH` | `30` / `60` | Source-preserved output rate: the measured source rate is kept (fractional included) and capped at HIGH; LOW is the fallback only when the rate is unmeasurable (no frame duplication) |
 | `WORKER_RENDER_FPS_HIGH_SOURCE_THRESHOLD` | `55` | Retained for settings compatibility; the source-preserved selection no longer uses it |
 | `WORKER_INTEL_CHUNK_SECONDS` | `600` | Analysis chunk length (operational bound, NOT a duration cap) (60-3600) |
 | `WORKER_INTEL_OVERLAP_SECONDS` | `30` | Chunk overlap keeping boundary events whole (0 ≤ x < chunk) |
 | `WORKER_INTEL_CHUNKS_PER_CLAIM` | `20` | Chunks one invocation processes before returning `incomplete` (resume next claim) (1-1000) |
 | `WORKER_INTEL_SCENE_THRESHOLD` | `0.3` | ffmpeg scene-score cut threshold (0.05 < x ≤ 0.95) |
 | `WORKER_INTEL_CHUNK_TIMEOUT_SECONDS` | `300` | Per-chunk decode / visual-pass budget; the subprocess is killed after it |
 | `WORKER_INTEL_REQUESTED_CLIPS` | `5` | Default number of ranked candidate clips (1-25) |
 | `WORKER_INTEL_MAX_RESUMES` | `100000` | Resume requeues allowed before a permanent `intel_resume_exhausted` failure (resumes never consume the job's `attempt_count` retry budget) |
| `WORKER_FACECAM_ENABLED` | `false` | Phase 8 facecam modality. OFF by default; requires the optional extra (`pip install -r requirements-facecam.txt`) AND a detector model path below. Missing model/dependency → honest "unavailable" (recorded, no events, pipeline otherwise unchanged) |
| `WORKER_FACECAM_SAMPLE_FPS` | `1.0` | Sampled frames per second for face analysis (0 < x ≤ 4) |
| `WORKER_FACECAM_MIN_CONFIDENCE` | `0.5` | Minimum detector confidence for a face to count (0 < x ≤ 1) |
| `WORKER_FACECAM_MAX_FACES` | `3` | Max faces considered per frame (1-8); persistence — not size — selects the region |
| `WORKER_FACECAM_MAX_CONCURRENCY` | `1` | Inference concurrency bound (1-8; 1 = strict sequential, CPU-safe) |
| `WORKER_FACECAM_DETECTOR_MODEL_PATH` | *(unset)* | EXPLICIT path to `blaze_face_short_range.tflite` (Apache-2.0, ~225 KB, `https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/latest/blaze_face_short_range.tflite`). Weights are NEVER downloaded during job execution |
| `WORKER_FACECAM_LANDMARKER_MODEL_PATH` | *(unset)* | Optional `face_landmarker.task` (Apache-2.0, ~3.6 MB, `https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task`) — enables facial-geometry metrics (mouth/smile/brow/head); without it only face presence is reported |
| `WORKER_FACECAM_FRAME_TIMEOUT_SECONDS` | `120` | Wall-clock budget for the per-chunk frame-extraction pass (the subprocess is killed after it) |
| `WORKER_CAPTION_FONT_FAMILY` | `DejaVu Sans` | libass font family (fontconfig-resolved; no markup characters) |
| `WORKER_CAPTION_FONT_SIZE` | `88` | Glyph size in PlayRes pixels (24-200) |
| `WORKER_CAPTION_BASE_COLOR` | `FFFFFF` | Spoken-word color (RRGGBB) — ASS PrimaryColour |
| `WORKER_CAPTION_HIGHLIGHT_COLOR` | `00D7FF` | Upcoming-word glow color (RRGGBB) — ASS SecondaryColour |
| `WORKER_CAPTION_OUTLINE_COLOR` | `000000` | Outline color (RRGGBB) |
| `WORKER_CAPTION_OUTLINE_WIDTH` | `6` | Outline thickness (0-24) |
| `WORKER_CAPTION_MARGIN_V` | `520` | Distance of the caption block from the bottom edge (60-1500) |
| `WORKER_CAPTION_WORDS_PER_LINE` | `3` | Consecutive words per on-screen line (1-8) |
| `WORKER_CAPTION_UPPERCASE` | `true` | Uppercase Latin words (Arabic/CJK never transformed) |

## Queue contract (Phases 3-5)

All queue traffic goes through the restricted, `SECURITY DEFINER` RPCs
(`claim_next_job`, `renew_job_lease`, `update_job_progress`,
`complete_job_ingestion`, `complete_job_rendering`, `fail_job`,
`release_job`), extended stage by stage in migrations
`20261115000000` / `20261116000000` / `20261117000000` with **unchanged
signatures** (backward compatible). They are revoked from PUBLIC and granted
to `service_role` only; every update is scoped to the calling worker's
lease, so two workers can never own the same active job. Expired leases make
crashed jobs reclaimable (crash recovery). The worker renews each job's lease
while processing and aborts safely (no stale state writes) when a renewal is
rejected. Since Phase 5, the claim also picks up parked `analyzing` jobs
(→ `rendering`), rendering retries consume the dedicated
`jobs.render_attempts` budget (bounded by `max_attempts`), and
`complete_job_rendering` is the ONLY path to `completed` in rendering — it
refuses unless every clip is `ready` with a `storage_path`.

## Docker

```bash
docker build -t clipforge-worker .
docker run --rm -p 8000:8000 \
  -e WORKER_SUPABASE_URL=... -e WORKER_SUPABASE_SERVICE_ROLE_KEY=... \
  clipforge-worker
```

The image installs FFmpeg/FFprobe via apt and uses `/data` as the scratch
root (ephemeral; no durable state lives there).

## Module map

```
worker/app/
├── api/           # /health + ingestion heartbeat
├── queue/         # Job models (9-state machine) + Supabase queue client (RPCs)
├── processors/    # Ingestion processor (Phase 3) + pipeline stage order (4-5)
├── transcription/ # Whisper adapter contract (Phase 4)
├── ai/            # Clip detection contract (Phase 4)
├── rendering/     # FFmpeg renderer (Phase 5A/5B): profiles, subprocess lifecycle,
│                  #   rotation-aware cut/reframe, ASS caption burn-in, output contract
├── storage/       # Private Supabase Storage download + clip upload + local path utilities
├── validation/    # FFprobe media validation (container/streams; no source-duration cap since TASK 007)
├── intel/         # Stream intelligence (Phase 7): chunk plan, audio/visual features,
│                  #   detectors, fusion, selection, checkpoints store, pipeline, testing double
├── providers/     # Source adapters (Phase 7): protocol, upload (full), youtube/twitch (validation-only)
├── worker.py      # Ingestion loop (claim, concurrency, backoff, shutdown)
├── cleanup.py     # Manual artifact purge CLI (Phase 6; retention policy, idempotent)
└── config/        # Settings + structured logging

worker/benchmarks/  # Deterministic benchmark fixture + runner (python -m benchmarks.run)
```
