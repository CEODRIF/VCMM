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
- `app/providers/` — source adapters OUTSIDE the pipeline (Phase 8, TASK 011):
  YouTube/Twitch metadata via OFFICIAL APIs only (oEmbed keyless / Data API v3
  with API key or OAuth refresh-token; Helix with Client-Id or app-access
  token) + typed `NormalizedSource` metadata + availability states; they
  REFUSE media materialization explicitly (the official APIs expose no
  media-download endpoint — no support is claimed, no workaround exists),
  upload fully implemented (probe + injected bounded download).
  `http_guard.py` = SSRF-safe HTTP (allowlist, DNS revalidation, private-IP
  rejection, bounded redirects, header allowlist, chunked streaming).
  `materialization.py` = provider-independent authorized-transfer service
  (streaming → private bucket → lease-guarded pin → shared tail).
  `errors.py` = typed, auto-redacted provider errors. No yt-dlp, no cookies,
  no DRM bypass, no scraping, no network guessing.
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
| `WORKER_SHUTDOWN_GRACE_SECONDS` | `600` | On SIGTERM/SIGINT: seconds to wait for in-flight jobs before ABANDONING them (30-86400). Abandoned jobs are never marked failed — their lease expires and another worker recovers them |
| `WORKER_TMP_STALE_SECONDS` | `172800` | Age (hours-scale) after which a `storage/tmp/<job-id>` directory from a CRASHED worker is removed on next startup (≥3600). Directories of live jobs are never touched |
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
| `WORKER_GAME_VISION_ENABLED` | `false` | Phase 8 game-vision modality (local OCR). OFF by default; requires the system `tesseract-ocr` package (`sudo apt-get install tesseract-ocr tesseract-ocr-eng`) and the optional extra (`pip install -r requirements-game-vision.txt`). Missing binary/tessdata → honest "unavailable" (recorded, no events, pipeline otherwise unchanged) |
| `WORKER_GAME_VISION_SAMPLE_FPS` | `1.0` | Sampled frames per second for OCR (0 < x ≤ 4) |
| `WORKER_GAME_VISION_MIN_OCR_CONFIDENCE` | `60.0` | Minimum tesseract line confidence (0-100); below = unreadable, filtered (never fabricated) |
| `WORKER_GAME_VISION_MAX_FRAMES_PER_CHUNK` | `1200` | Hard cap on sampled frames per chunk |
| `WORKER_GAME_VISION_MAX_OCR_OPERATIONS_PER_CHUNK` | `800` | Hard cap on OCR subprocess runs per chunk (on budget: `budget_exceeded` recorded, measured events kept) |
| `WORKER_GAME_VISION_MAX_CHUNK_SECONDS` | `240` | Wall-clock budget for the whole per-chunk game-vision pass |
| `WORKER_GAME_VISION_FRAME_TIMEOUT_SECONDS` | `120` | Per-chunk frame-extraction budget (subprocess killed after) |
| `WORKER_GAME_VISION_OCR_TIMEOUT_SECONDS` | `30` | Per-OCR-subprocess budget (killed after) |
| `WORKER_GAME_VISION_OCR_LANGUAGE` | `eng` | Tesseract language code; the matching language pack must be installed explicitly (never downloaded at runtime) |
| `WORKER_GAME_VISION_TESSDATA_DIR` | *(unset)* | EXPLICIT directory containing `<language>.traineddata` (None = system default) |
| `WORKER_GAME_VISION_TESSERACT_BINARY` | *(unset)* | EXPLICIT tesseract binary path (None = resolved from PATH) |
| `WORKER_GAME_VISION_KILL_FEED_ROI` | `0.80,0.02,0.18,0.12` | Kill-feed region 'x,y,w,h' normalized (default top-right strip — a DEFAULT, not a game assumption; empty = full-frame scan) |
| `WORKER_MULTIMODAL_ENABLED` | `false` | Phase 8 advanced multimodal fusion ENGINE (optional layer over Phase 7 fusion). OFF by default → the completion phase is byte-identical to Phase 7 (A/B pipeline test). ON → the same qualifying candidates (unchanged score bar) gain `heuristic_confidence` + size-bounded provenance. Pure over persisted events: no schema change, no new dependency, resumable |
| `WORKER_MULTIMODAL_CORRELATION_WINDOW_SECONDS` | `2.5` | Temporal clustering window (0 < x ≤ 30; matches the Phase 7 default) |
| `WORKER_MULTIMODAL_DECAY_TAU_SECONDS` | `2.5` | Temporal-decay constant for cross-family proximity weights, exp(−gap/τ) (0 < x ≤ 30) |
| `WORKER_MULTIMODAL_SAME_KIND_MERGE_SECONDS` | `1.0` | Same family + kind within this gap (and same OCR content, where observable) = one physical signal (corroboration, never independence; 0 ≤ x ≤ 30) |
| `WORKER_MULTIMODAL_CONFIDENCE_THRESHOLD` | `0.30` | Floor for a candidate's heuristic confidence (below → suppressed, recorded; 0 ≤ x ≤ 1) |
| `WORKER_MULTIMODAL_MIN_INDEPENDENT_MODALITIES` | `1` | Minimum INDEPENDENT modality count for any candidate (1-4) |
| `WORKER_MULTIMODAL_STRONG_AUDIO_ONLY_THRESHOLD` | `0.80` | Strength a strong audio-only solo anchor must reach (0 ≤ x ≤ 1) |
| `WORKER_MULTIMODAL_PRE_PADDING_SECONDS` | `1.0` | Candidate boundary padding before the measured cluster span, clamped to the source (0 ≤ x ≤ 60) |
| `WORKER_MULTIMODAL_POST_PADDING_SECONDS` | `2.0` | Candidate boundary padding after the measured cluster span, clamped (0 ≤ x ≤ 60) |
| `WORKER_MULTIMODAL_SINGLE_SOURCE_MIN_STRENGTH` | `0.60` | One-family candidates below this max strength are suppressed (single-source repetition; a strong single-source moment still qualifies; 0 ≤ x ≤ 1) |
| `WORKER_MULTIMODAL_MAX_EVENTS` | `5000` | Hard cap on fusion input events (time-ordered deterministic cut; `input_truncated` reported; 1-100000) |
| `WORKER_MULTIMODAL_MAX_CLUSTERS` | `500` | Hard cap on correlation clusters (earliest kept; `clusters_dropped` reported; 1-10000) |
| `WORKER_MULTIMODAL_MAX_SIGNALS_PER_CLUSTER` | `64` | Graph-node cap per cluster (overflow kept for scoring, never independent; 1-512) |
| `WORKER_MULTIMODAL_MAX_EDGES_PER_NODE` | `8` | Graph-edge cap per node (1-64) |
| `WORKER_MULTIMODAL_MAX_SEQUENCES_PER_CLUSTER` | `8` | Sequence-extraction cap per cluster (1-64) |
| `WORKER_MULTIMODAL_MAX_SEQUENCE_LENGTH` | `4` | Longest multi-stage sequence (2-16) |
| `WORKER_MULTIMODAL_MAX_CANDIDATES` | `200` | Output candidate cap (strongest first; `candidates_capped` reported; 1-1000) |
| `WORKER_MULTIMODAL_PROCESSING_BUDGET_SECONDS` | `30` | Wall budget checked BETWEEN clusters (on budget: `status="budget_exceeded"` + `clusters_unprocessed` reported, analyzed work kept; 0 < x ≤ 600) |
| `WORKER_CAPTION_FONT_FAMILY` | `DejaVu Sans` | libass font family (fontconfig-resolved; no markup characters) |
| `WORKER_CAPTION_FONT_SIZE` | `88` | Glyph size in PlayRes pixels (24-200) |
| `WORKER_CAPTION_BASE_COLOR` | `FFFFFF` | Spoken-word color (RRGGBB) — ASS PrimaryColour |
| `WORKER_CAPTION_HIGHLIGHT_COLOR` | `00D7FF` | Upcoming-word glow color (RRGGBB) — ASS SecondaryColour |
| `WORKER_CAPTION_OUTLINE_COLOR` | `000000` | Outline color (RRGGBB) |
| `WORKER_CAPTION_OUTLINE_WIDTH` | `6` | Outline thickness (0-24) |
| `WORKER_CAPTION_MARGIN_V` | `520` | Distance of the caption block from the bottom edge (60-1500) |
| `WORKER_CAPTION_WORDS_PER_LINE` | `3` | Consecutive words per on-screen line (1-8) |
| `WORKER_CAPTION_UPPERCASE` | `true` | Uppercase Latin words (Arabic/CJK never transformed) |
| `WORKER_SOURCE_PROVIDERS_ENABLED` | `true` | Phase 8 (TASK 011): master switch for provider jobs; OFF → youtube/twitch jobs fail permanently with a clear "disabled" error, uploads unaffected |
| `WORKER_YOUTUBE_API_KEY` | – | Phase 8: official YouTube Data API v3 key (server-side; sent in the `X-Goog-Api-Key` header, NEVER the URL); optional — without it, keyless oEmbed metadata (partial) still works |
| `WORKER_YOUTUBE_OAUTH_CLIENT_ID` / `_CLIENT_SECRET` / `_REFRESH_TOKEN` | – | Phase 8: OAuth user-credential flow (lets the USER's authorized context read unlisted/private metadata). All three must be set together (validated) or none. Server-side only |
| `WORKER_YOUTUBE_OAUTH_SCOPES` | `openid` | Phase 8: minimum scopes for metadata-only access (0-500 chars, validated) |
| `WORKER_TWITCH_CLIENT_ID` / `_CLIENT_SECRET` | – | Phase 8: official Twitch application credentials. Client id alone = Client-Id header mode (official minimum for PUBLIC VOD metadata); id + secret = app-access-token flow (client_credentials) |
| `WORKER_TWITCH_API_BASE` / `WORKER_TWITCH_OAUTH_BASE` | official endpoints | Phase 8: overridable for tests/private mirrors |
| `WORKER_PROVIDER_REQUEST_TIMEOUT_SECONDS` | `10` | Phase 8: provider API + token-exchange timeout (0-120) |
| `WORKER_PROVIDER_MAX_RETRIES` / `WORKER_PROVIDER_RETRY_BACKOFF_SECONDS` | `2` / `1.5` | Phase 8: bounded retries for TRANSIENT provider failures only (0-5 / 0-60) |
| `WORKER_MATERIALIZE_MAX_SOURCE_BYTES` | `2 GiB` | Phase 8: hard cap on materialized sources (≥ 1 MiB) |
| `WORKER_MATERIALIZE_MAX_REDIRECTS` | `3` | Phase 8: cap on transfer redirects (each hop re-validated; 0-8) |
| `WORKER_MATERIALIZE_CHUNK_BYTES` | `1 MiB` | Phase 8: streaming chunk size (64 KiB-16 MiB) — never the whole file in memory |
| `WORKER_MATERIALIZE_MAX_DURATION_SECONDS` | – (no cap) | Phase 8: optional duration gate for materialized sources (≥ 0); default preserves the TASK 007 no-duration-cap decision |

## Source providers (Phase 8, TASK 011)

Provider-independent source layer: the pipeline receives NORMALIZED source
files and never learns where a source came from. Ingestion dispatches
`upload` jobs exactly as before and routes `youtube`/`twitch` jobs through
this layer: gate → official validation/metadata → typed failure or the
authorized materialization path → the SAME download/probe/complete tail as
uploads.

### Supported URL formats (strict; lookalikes rejected)

| Provider | Accepted forms | Rejected |
| --- | --- | --- |
| YouTube | `https://(www./m./music.)youtube.com/(watch?v=\|shorts/\|live/\|embed/)ID`, `https://youtu.be/ID` (ID: 11 chars `[A-Za-z0-9_-]`) | channels (`@name`), playlists, bare `/ID`, other hosts/schemes |
| Twitch | `https://(www./m.)twitch.tv/videos/{vodId}`, `https://(www./m.)twitch.tv/{channel}/videos/{vodId}`; live/channel URLs (`/live/{channel}`, `/{channel}`) are validated (online → restricted "wait for the VOD", offline → unavailable) | non-VOD paths, lookalike hosts, other schemes |

### Capabilities — what is officially supported (be precise)

| Provider | Metadata | States distinguished | Materialization |
| --- | --- | --- | --- |
| YouTube, unconfigured | **oEmbed keyless** (official): title, author, type → partial (`NormalizedSource` with unknowns honestly unknown; no duration) | available / live (restricted) / deleted (404) / rate-limited (429) / unavailable (5xx, malformed) | **NO** — official APIs expose no media-download endpoint → typed permanent `materialization_unsupported`, "upload the file directly" |
| YouTube + `WORKER_YOUTUBE_API_KEY` | **Data API v3** `videos` (snippet + contentDetails + liveStreamingDetails): duration (IS-8601), publication, creator, thumbnail, definition | completed / live / scheduled / private / rejected / processing / deleted + HTTP mapping (403→restricted, 404→deleted, 429→rate-limited, 5xx→unavailable) | **NO** (same refusal) |
| YouTube + OAuth trio | Data API v3 under the USER's authorized context (unlisted/private readable) | as above | **NO** (same refusal) |
| Twitch + `WORKER_TWITCH_CLIENT_ID` | **Helix** `/videos` (Client-Id header — the official minimum for public VODs): duration, created, creator, thumbnail, vod_type (archive/highlight) | completed / deleted / private (401/403) / rate-limited (429) / unavailable (5xx, malformed); live online/offline via `/streams` | **NO** — typed permanent refusal |
| Twitch + id + secret | Helix with cached **app-access token** (client_credentials, POST body only) | as above | **NO** (same refusal) |
| Twitch, unconfigured | **Honest unavailability, zero network calls** (never a misleading 401-based guess) | unavailable ("not configured on this deployment") | **NO** |
| Upload | FFprobe on the stored object | — | **YES** (the fully implemented reference path) |

**Mocked tests vs live operations.** All provider tests are deterministic and
offline (httpx.MockTransport + injected DNS resolvers; no live credentials).
They prove the contract, state mapping, OAuth flow shape, and security
guards — they do NOT prove live YouTube/Twitch API behavior. Live metadata
calls work when an operator configures official credentials (free-tier, no
spend); **live materialization of platform URLs has NOT been and cannot be
verified by this codebase** — no official mechanism exists, so it is exposed
as unsupported, not simulated.

### OAuth / credential setup (server-side only)

- **YouTube API key** (easiest, public metadata): create an API key for the
  YouTube Data API v3 in Google Cloud; set `WORKER_YOUTUBE_API_KEY`. Sent via
  the official `X-Goog-Api-Key` header — never in URLs, never logged.
- **YouTube OAuth** (unlisted/private metadata): an OAuth client (web) with
  the user flow + a refresh token for the user; scopes minimum `openid`. Set
  all three `WORKER_YOUTUBE_OAUTH_*` variables (validated as a trio — partial
  configuration fails startup). Token exchange is form-encoded to the
  official token endpoint, cached with safe expiry (60 s margin),
  single-flight; `invalid_grant` and other failures surface as a typed
  PERMANENT `provider_unauthorized` with a redacted message (a retry cannot
  fix credentials).
- **Twitch**: create a Twitch app (no redirect flow needed).
  `WORKER_TWITCH_CLIENT_ID` alone = Client-Id header mode (public VOD
  metadata only). Add `WORKER_TWITCH_CLIENT_SECRET` for the app-access-token
  flow (client_credentials, cached). NO OAuth application is created or
  configured by this codebase — that is an operator action, explicit, out of
  scope.
- Secrets are env-only, validated by Pydantic, empty string = not configured,
  and NEVER logged (credential-shaped text is redacted in every provider
  message; see `errors.redact`).

### Materialization service (provider-independent, ready for a licensed path)

`MaterializationService` — the single place an AUTHORIZED media transfer
becomes a verified source object:

1. provider `materialize()` streams media through the SSRF guard into temp
   disk (chunked `iter_bytes`, hard size cap, progress + per-chunk cancel
   probe, bounded retries with `Retry-After`, partial-file cleanup);
2. `SupabaseStorage.upload_source` streams the file handle (never whole-file
   bytes) into the private `clipforge-sources` bucket at a SERVER-GENERATED
   path `sources/{job}/original/{uuid}.mp4` (UUID-validated job id — no path
   traversal; upsert disabled — no duplicate objects);
3. lease-guarded `set_job_source` pins the path on the owned `validating`
   job (ownership lost → abandon, bounded orphan documented);
4. the shared ingestion tail downloads the stored object, probes the ACTUAL
   bytes with FFprobe, and completes — identical to direct uploads.
   Idempotent re-claim: an already-pinned job skips the transfer and
   re-verifies (duplicate detection + interrupted-transfer recovery).

### Security boundaries (SSRF and secrets)

`http_guard.SafeHttpClient` is the ONLY network path for provider calls:

- host allowlist per provider (official endpoints only); http(s) only; no
  userinfo; standard ports (80/443) only; control characters rejected;
- literal-IP normalization (decimal/octal/hex encodings fail closed) and DNS
  revalidation BEFORE EVERY request and EVERY redirect hop — every resolved
  answer must be public (private/loopback/link-local incl. the
  cloud-metadata address/reserved/multicast/unspecified/site-local all
  refused; a mixed public+private answer poisons the set);
- redirects are manual, bounded (`WORKER_MATERIALIZE_MAX_REDIRECTS`), each
  hop re-validated, RFC 9110 method switching (301/302/303 → GET);
- header allowlist only (`accept`, `authorization`, `client-id`,
  `x-goog-api-key`) — no arbitrary user-supplied headers;
- tokens/keys travel in headers or form bodies only — never in URLs;
- `redact()` scrubs credential-shaped text (authorization/bearer,
  access/refresh tokens, client secrets, cookies, signed-URL tokens) from
  every message that can reach logs or job error records.

### Error categories (typed, safe, categorized)

Provider codes → queue categories (permanent vs retryable): invalid URL /
unsupported provider → `provider_invalid_url` (permanent); unauthorized →
`provider_unauthorized` (permanent); metadata unavailable →
`provider_metadata_unavailable` (retryable); materialization unsupported →
`materialization_unsupported` (permanent); rate limited →
`provider_rate_limited` (retryable); provider unavailable/disabled →
`provider_unavailable` (permanent); source restricted/expired →
`source_rejected` (permanent); size/duration exceeded → `source_too_large` /
`source_too_long` (permanent); transfer interrupted → `transfer_interrupted`
(retryable); storage failure → `source_upload_failed` (retryable); cancelled →
no failure write (lease-safe abandon).

## Operational endpoints & observability (TASK 012)

### Endpoints (FastAPI, port 8000)

| Endpoint | Purpose | HTTP |
| --- | --- | --- |
| `GET /health` | **Liveness** — process is up (Docker `HEALTHCHECK` target). Never probes dependencies. | always 200 |
| `GET /ready` | **Readiness** — real, bounded dependency probes (one `select` on `jobs`, one storage list, 5s each, 30s result cache). `200 {status:"ready"}` iff database ok **and** storage ok **and** ffmpeg enabled; otherwise `503 {status:"degraded", components:{...}}` naming every failing component. Missing OPTIONAL features (transcription/facecam/game_vision) are reported as `enabled`/`disabled`/`degraded` but never gate readiness. With `WORKER_WORKER_ENABLED=false` every component reports `disabled` and the endpoint is `200 ready` (API-only deployment). | 200 / 503 |
| `GET /metrics` | Prometheus text format (stdlib registry, `text/plain; version=0.0.4`). | 200 |

Never report "ready" merely because the process started — `/ready` performs real
probes, and its results are cached for 30s so load balancers can poll freely.

### Structured logging

Every log line is a single JSON object on stdout:
`{timestamp, level, logger, message}` plus, inside a claimed job's processing
context, `job_id`, `worker_id`, `correlation_id` (16-hex, one per claim),
`stage`, `attempt`, `provider`, `error_code` (when set) and whitelisted
extra facts (`duration_ms`, `bytes`, `object_count`). Tracebacks are
redacted with the same rules as messages: OAuth/refresh tokens,
`Authorization`/`Cookie` headers (to end-of-line), `service_role`/
`api_key`/`password`/`token`/`secret` values, and signed storage URLs
(including short signatures) are masked as `***`. Job ids are UUIDs, so
they are never treated as sensitive.

### Metrics

In-process, stdlib-only, **bounded cardinality** (fixed label domains —
stage, provider, outcome, operation — plus a per-family series cap that
drops and warns on overflow; no label ever carries a raw job id or URL):

- `clipforge_jobs_claimed_total{stage}`, `clipforge_jobs_completed_total{stage}`,
  `clipforge_jobs_failed_total{stage,error_category}`,
  `clipforge_jobs_released_total{stage}`, `clipforge_job_cancellations_total{stage}`
- `clipforge_queue_wait_seconds_{sum,count}{stage}` (first claim only),
  `clipforge_job_processing_seconds_{sum,count}{stage}`
- `clipforge_lease_renewals_failed_total`, `clipforge_claim_errors_total`,
  `clipforge_storage_failures_total{operation}`, `clipforge_provider_validations_total{provider,outcome}`
- gauges: `clipforge_worker_active_jobs`, `clipforge_worker_uptime_seconds`,
  `clipforge_worker_consecutive_claim_errors`

`error_category` is normalized to the closed set of queue-model error codes;
anything else becomes `other`.

### Shutdown & recovery

On SIGTERM/SIGINT: the worker stops claiming, waits up to
`WORKER_SHUTDOWN_GRACE_SECONDS` for in-flight jobs, then **abandons** any
still-running job (task cancelled, subprocesses killed in their `finally`
blocks, **no failure or completion written** — the lease expires and another
worker recovers the job from its stage entry). Queue, storage and provider
HTTP clients are then closed. On the next startup, `storage/tmp/<job-id>`
directories older than `WORKER_TMP_STALE_SECONDS` are removed (live-job
directories are never touched).

### Claim backoff

Claim-path failures (RPC/transport errors) back off exponentially with
jitter: delay ∈ [base/2, base], base = min(60s, poll_interval × 2^errors,
exponent capped at 5). A successful claim resets the counter.

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
├── providers/     # Source adapters (Phase 7-8): protocol, models, typed errors, SSRF-safe HTTP,
                  #   materialization service, registry; upload (full), youtube/twitch (official-API
                  #   metadata, explicit materialization refusal)
├── worker.py      # Ingestion loop (claim, concurrency, backoff, shutdown)
├── cleanup.py     # Manual artifact purge CLI (Phase 6; retention policy, idempotent)
└── config/        # Settings + structured logging

worker/benchmarks/  # Deterministic benchmark fixture + runner (python -m benchmarks.run)
```
