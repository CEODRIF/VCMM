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
├── rendering/     # FFmpeg renderer (Phase 5A): profiles, subprocess lifecycle,
│                  #   rotation-aware cut/reframe, FFprobe output-contract validation
├── storage/       # Private Supabase Storage download + clip upload + local path utilities
├── validation/    # FFprobe media validation (container/streams/limits)
├── worker.py      # Ingestion loop (claim, concurrency, backoff, shutdown)
└── config/        # Settings + structured logging
```
