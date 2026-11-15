# Video Generation — Modes, Clip Rules, Render Contract

## Generation modes (TASK 015 P3)

| | **Full Video to Reels** (`full_coverage`) | **Viral Highlights** (`highlights`) |
|---|---|---|
| Input contract | `requested_clips` **required** (1..5000 in DB; API validates tighter) | `requested_clips` **required** (1..10 at the API) |
| Coverage | The **entire** timeline is tiled into ~`requested_clips` clips (ceil(t/duration) tiling; the DB invariant `jobs_mode_count_consistency` enforces the mode⇔count pairing, NULL-safe) | The **best** windows are selected (detector band 60–180 s scaled to source length, transparent scoring, pairwise IoU ≤ 0.3 dedup) |
| Works without Gemini | ✅ (tiling is arithmetic) | ✅ (local-signal windows, confidence ≥ 0.6) |
| Persisted per clip | `clips.mode = 'full_coverage'` | `clips.mode = 'highlights'` |

The UI exposes both modes with a mode selector; the job's
`generation_mode` and per-clip `mode` are shown in the results dashboard.

## Clip duration rules

- Per-clip target: `jobs.clip_duration_seconds` (job setting; DB bounds
  `clips.duration` 5..3600 s — a clip outside the band is rejected by
  the database, not just the code).
- Full coverage: clip count = `ceil(source_duration / clip_duration)`
  with an explicit remainder policy (short remainders are kept or merged,
  never silently dropped — expected-vs-actual counts are asserted in the
  pipeline integration tests).
- Highlights: detector windows are clamped to
  `max(60, 0.85·t)` … `min(180, 1.25·t)` seconds.

## Output FPS (30 / 60)

- `jobs.output_fps` ∈ {30, 60} (DB: `jobs_output_fps_check`, NULL =
  source-rate-preserving). The API rejects every other value (400);
  24/25/50 etc. are not accepted.
- Rendering writes the rate **explicitly** (`-r`/`-vsync` + ffprobe
  verification `avg_frame_rate`), so a 60 fps job produces 60 fps
  output even from 30 fps sources (frame-duplicated by design), and a
  30 fps job never outputs 59.94.
- Worker render defaults: `WORKER_RENDER_FPS_LOW` / `WORKER_RENDER_FPS_HIGH`
  with `WORKER_RENDER_FPS_HIGH_SOURCE_THRESHOLD` for the
  source-rate-preserving path.

## Render contract (verified, not assumed)

Every clip is rendered by FFmpeg to the fixed vertical contract and then
**ffprobe-verified before it is marked `ready`**:

- 1080×1920 (9:16), H.264, AAC 44.1 kHz;
- measured `width` / `height` / `output_fps` / `file_size_bytes` are
  persisted with the `ready` transition (`clips` P4 columns) — the
  dashboard displays only these measurements (or "—"), never guesses;
- verification failure → clip `failed`, never a bogus `ready`.

Real-render evidence (TASK 015 P3/P4): 3.000 s clips at 1080×1920,
H.264 + AAC, `avg_frame_rate` 30/1 (90 frames) and 60/1 (180 frames)
verified by ffprobe on actual outputs.

## Rendering & completion

- Concurrency: `WORKER_RENDER_CONCURRENCY`, timeout
  `WORKER_RENDER_TIMEOUT_SECONDS`, size cap `WORKER_MAX_CLIP_SIZE_BYTES`;
  render temp files live under the worker's `storage/` root and are
  cleaned on completion/failure.
- Each clip: `pending → processing → ready` (row scoped to
  `(id, job_id)`; a stale worker can never touch another job).
- **Job completion guard** — the `complete_job_rendering` RPC moves the
  job to `completed` **only when every clip is `ready` with a verified
  `storage_path`**; otherwise it refuses (the job stays rendering /
  fails through the normal paths). The Phase 6 retention trigger sets
  `jobs.expires_at = completed + 30 days` exactly once, at that
  terminal transition.
