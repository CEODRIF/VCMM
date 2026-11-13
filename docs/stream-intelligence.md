# Stream Intelligence Foundation — Audit & Design (TASK 007)

Goal: turn ClipForge AI from a fixed-window clip generator into a
long-form **gaming stream intelligence** system — analyze an entire
recording of any duration, incrementally and with bounded resources, and
rank the most engaging moments (especially genuine streamer reactions) as
context-preserving vertical clips. **Extraction quality is the objective,
not UI.**

This document records the mandatory repository audit (section 1) and the
chosen architecture (sections 2-8) BEFORE any major code change.

---

## 1. Repository audit (main @ f79e772, Phase 6 merged)

### 1.1 What already exists and is reused (NOT rewritten)

| Component | State | Reuse decision |
| --- | --- | --- |
| Job lifecycle + queue RPCs (claim, lease, stage transitions, park, fail/requeue) | Done, tested (pglite) | Reused as-is; intel must respect the same ownership/lease contract |
| Transcription (`transcribe_chunked`) | **Already chunked**: overlapping `-ss/-t` splits, midpoint-ownership merge, provider-agnostic, disk-based (~30 MB/h WAV, never whole-file RAM) | Its chunking + midpoint-ownership design is the template for the intel chunk engine |
| Audio extraction (`processors/audio.py`) | Streaming FFmpeg decode → mono 16 kHz WAV on disk | Reused; intel reads bounded sub-ranges of the SAME file |
| FFmpeg rendering (Phase 5A/5B) | Rotation-aware cut/reframe to 1080x1920, FFprobe output contract, captions, upload | Reused; ONE targeted fix — fixed `-r 30` drops 60 FPS sources (see §7) |
| Results API + storage security (Phase 6) | Session/ownership, signed URLs, ZIP, retention | Untouched; clip rows it serves are unchanged in shape |
| Clip persistence (`SupabaseClipStore`) | Job-scoped delete-then-insert (idempotent retries) | Pattern reused for the new checkpoint/event stores |
| pglite test harness, CI (FFmpeg installed, media tests executed) | Green | Extended |

### 1.2 Actual limitations found for long recordings

1. **Product-level duration cap** — `worker/app/validation/media.py`
   rejects sources > `max_source_duration_seconds` (default **8 h**) with
   `source_too_long`. This is exactly the "arbitrary product-level maximum
   duration" the task prohibits. The frontend has NO duration cap.
   **Fix**: remove the duration rejection (keep the size cap as a
   storage-infrastructure limit) — long recordings are handled by chunking
   + checkpoints instead.
2. **No analysis checkpoints** — a worker crash mid-analysis loses the
   whole run (transcription included). Nothing persists per-chunk
   progress.
3. **No signal features** — clip selection is transcript-scored only
   (Phase 4 `TranscriptClipDetector`). Non-verbal reactions (screams,
   laughter, transients, scene changes) are invisible to the pipeline.
4. **No provider abstraction** — `source_type in ('youtube','upload')`;
   YouTube materialization is a marked integration point; no URL
   validation/availability layer on the worker.
   *(Phase 8 / TASK 011 update: the worker now has the provider layer —
   official-API metadata validation with typed normalized sources,
   SSRF-guarded HTTP, an honest materialization refusal for platform URLs,
   and a provider-independent materialization service; ingestion dispatches
   `youtube`/`twitch` jobs through it while uploads keep the identical path.
   The intel pipeline itself is unchanged: it still consumes normalized
   source files.)*
5. **Fixed 30 FPS output** — `RenderProfile.fps=30` (`-r 30`) downsamples
   60 FPS gaming sources (quality loss; the task asks to preserve the
   source rate where practical).

### 1.3 What this task deliberately does NOT do (scope honesty)

- **In-loop claim integration — DELIVERED (Phase 7b, §11).** Originally
  deferred here because it needed a queue-side "incomplete, continue
  later" transition that must not collide with the Phase 5A
  parked-`analyzing`→rendering claim. That design is now implemented
  (`requeue_incomplete_analysis` + the dedicated `analysis_resumes`
  counter, following the Phase 5 `render_attempts` precedent) and
  integration-tested.
- **No facecam/expression ML, no OCR, no HUD parsers** — these require
  model weights/paid or heavy dependencies. They exist as typed,
  tested **interfaces** with honest null implementations (the scorer
  treats missing modalities as absent evidence, never as fabrication).
- **No YouTube/Twitch materialization** — adapters validate URL +
  availability via official keyless endpoints (oEmbed, Helix public
  videos); `supports_materialization=False` until an explicitly
  authorized provider is approved. No yt-dlp, no DRM bypass, ever.

---

## 2. Long-form processing foundation (chunk engine)

`worker/app/intel/chunk_plan.py` (pure, deterministic):

- `plan_chunks(total_duration, chunk_seconds, overlap_seconds)` →
  `ChunkSpan(index, start, end)` with `step = chunk - overlap`, no tiny
  tail chunk (same tail rule as transcription chunking). **No duration
  upper bound exists anywhere in the planner** — a 100 h VOD yields a
  plan; the planner itself is O(duration/chunk) integers.
- **Boundary ownership**: chunk `i` owns events with absolute time in
  `[offset_i + overlap/2, offset_{i+1} + overlap/2)` (midpoint rule,
  copied from the proven transcription merge) → an event straddling a
  chunk boundary is processed exactly once (by the earlier chunk's
  overlap side) — never lost, never duplicated.
- Configurable chunk/overlap via `Settings`
  (`WORKER_INTEL_CHUNK_SECONDS` default 600,
  `WORKER_INTEL_OVERLAP_SECONDS` default 30).

## 3. Bounded-resource execution

Per chunk (and per run), resources are hard-bounded:

- **RAM**: a chunk's audio is decoded to a bounded WAV
  (`chunk_seconds × 16000 × 2 B` ≈ 19 MB at 600 s); features reduce it to
  a **1 Hz RMS/ZCR downsample + events** before anything is persisted.
  Fusion/selection consume ONLY the stored event lists + 1 Hz
  downsamples (O(chunks), not O(file size) — a 24 h VOD's fusion input is
  < 1 MB). The video file is never fully read: every FFmpeg call is a
  bounded `-ss/-t` range decode or a decimated (`fps=2`) metadata pass.
- **Disk**: per-chunk temp files under the job's temp dir, deleted in a
  `finally` (same contract as ingestion/analysis). The extracted full
  WAV remains the one durable temp (already ~30 MB/h).
- **Concurrency**: chunk processing is strictly sequential within a job
  (deterministic order, one FFmpeg-bound step at a time); the pipeline
  exposes `intel_chunks_per_claim` (default 20) — an execution budget.
  When remaining chunks exceed the budget the run returns
  `PipelineResult(status="incomplete", processed=…)` instead of failing:
  the work is checkpointed and simply continues on the next invocation
  (crash or budget — same code path).
- **Cancellation**: an `asyncio.CancelledError` (or the provided
  `should_stop` callback) aborts without writing state and after cleaning
  temps; completed chunks stay checkpointed.

## 4. Checkpoints + idempotency (DB)

New migration `20261124000000_phase7_stream_intelligence.sql` (additive,
repository-only, NOT applied to any live project):

- `processing_checkpoints(job_id uuid → jobs ON DELETE CASCADE,
  chunk_index int, start_time numeric, end_time numeric, status text
  check (status='done'), features jsonb not null, updated_at,
  primary key (job_id, chunk_index))` — a chunk is `done` once its
  features + events are durably stored; resume = plan minus done
  (no reprocessing, no duplicate work).
- `candidate_events(job_id uuid → jobs ON DELETE CASCADE, event_key text,
  start_time numeric, end_time numeric, category text, score numeric,
  confidence numeric, evidence jsonb not null, created_at,
  primary key (job_id, event_key))` — `event_key` = stable hash
  (`sha256(job_id|round(start,2)|family|kind)[:32]`) so upserts on retry
  are exact-dedup (same event twice = one row; different event = new row).
- `jobs.source_type` check extended with `'twitch'` (additive; the
  column already exists; enables future Twitch adapters without another
  migration).

Retention: both tables are `ON DELETE CASCADE` from `jobs` — the Phase 6
purge (which deletes job rows… note: it currently deletes storage
objects + marks `purged_at`; row deletion is out of its scope, so these
tables are cleaned when a job row is removed; the purge contract is
unchanged in this task).

`worker/app/intel/checkpoints.py` — `IntelStore` (service-role client,
job-scoped, UUID-validated like `SupabaseClipStore`):
`load_done_chunks`, `mark_chunk_done` (upsert on PK), `upsert_events`
(`on_conflict=(job_id,event_key) do nothing`-style upsert),
`load_events`, `reset_for_job` (scoped delete-then-reinsert for a full
re-run of a job's intel data).

## 5. Signal extraction (no fabricated evidence)

### Audio (`intel/audio_features.py`, numpy)
From the mono 16 kHz WAV (window 250 ms, hop 50 ms, vectorized):
- **RMS envelope** (vocal energy) → 1 Hz downsample stored.
- **Zero-crossing rate** (brightness — screams/hits vs. speech).
- **Peak**, **silence-ratio**, **silence gaps** (boundary snapping input).
- **Envelope deltas** (sudden energy change) — the "reaction spike" raw.

### Visual (`intel/visual_features.py`, FFmpeg-only, zero ML deps)
Two decimated passes (`fps=2`, null muxer, bounded ranges):
- **Scene changes**: `select='gt(scene,T)',showinfo` → parsed `pts_time`
  (real, measured cuts).
- **Motion**: `tblend=all_mode=difference,signalstats,metadata=print` →
  per-frame YAVG difference → motion bursts.

### Not implemented (interfaces only, documented)
- **Facecam presence / expression — IMPLEMENTED in TASK 008 (Phase 8, §13).**
  The `FacecamSignal`/`NullFacecamSignal` fallback remains: when facecam is
  disabled/unconfigured the fusion weights simply skip it (a clip WITHOUT
  facecam still qualifies).
- **HUD/kill-feed OCR — IMPLEMENTED in TASK 009 (Phase 8, §14)** as the
  game-vision modality (local tesseract OCR of configurable regions;
  no game is assumed to expose the same indicators — regions are
  configuration, not code).

### Detectors (`intel/detectors.py`) — pure functions over features
Each emits `SignalEvent(t, family, kind, strength 0..1, evidence)`:
- `audio.energy_spike` (delta > k × local baseline)
- `audio.transient` (short, high-peak, high-ZCR burst)
- `audio.voice_activity_change` (heuristic: sustained RMS/ZCR regime
  shift — labeled heuristic in evidence)
- `visual.scene_change`, `visual.motion_burst`

**No timestamps, energies or events are invented** — every `SignalEvent`
carries its measured evidence (window stats, ffprobe/ffmpeg source).

## 6. Fusion + selection (evidence-based, configurable)

`intel/fusion.py` — `FusionConfig` weights (dataclass, env-configurable,
all defaults documented):
- A candidate window forms when **≥2 independent signal families**
  co-occur within `fusion_window` (default 2.5 s), OR a single family
  exceeds a high "solo" bar (default: a very strong spike alone can seed
  a LOW-confidence candidate — a loud sound alone never QUALIFIES:
  family-diversity multiplier + `min_score` gate; explicit unit test).
- `score = 100 × (Σ family_weight × family_strength × diversity)`;
  `confidence` = fraction of expected families present (audio+visual vs
  audio-only). Temporal ordering (event → reaction → aftermath) is
  preserved by construction: windows carry their member events in time
  order, and selection expands AROUND the event core (see below).

`intel/selection.py`:
- **Context**: pre-roll (default 4 s) + reaction core + aftermath
  (default 8 s), clamped to [0, duration].
- **Speech-safe boundaries**: start/end snap OUTWARD to the nearest
  silence gap (from `audio.silence` features) so a sentence or a kill
  shout is not cut mid-word.
- **Merging**: overlapping/adjacent candidate windows merge (union).
- **Dedup**: near-duplicates (IoU ≥ 0.5) keep the higher score.
- **Categories** (configurable mapping from the strongest evidence,
  always labeled heuristic): `crazy_reaction`, `funny_moment`,
  `unexpected_moment`, `clutch_moment`, `epic_kill`, `rage_moment`,
  `victory_moment`, `unexpected_failure` — plus `unclassified` when
  evidence is mixed (honest fallback).
- **Ranking**: (score desc, confidence desc, start asc). `requested_clips`
  cap. **No virality claims** — scores are "estimated engagement
  potential" from measured evidence only.

## 7. Rendering (source-rate preservation + contract)

- `probe_media`/the renderer measure the source **frame rate**;
  `select_output_fps(source_fps)` PRESERVES the measured rate — fractional
  NTSC included (23.976, 29.97, 55, 59.94) — capped at
  `WORKER_RENDER_FPS_HIGH` (default 60). The output rate never exceeds the
  measured source rate, so frames are **never duplicated to fake a higher
  rate**, and nothing in the 23.976…60 window is silently clamped
  (e.g. a 24 fps source renders at 24, not 30). Unknown rates fall back to
  `WORKER_RENDER_FPS_LOW` (30). Fractional rates are passed to FFmpeg
  exactly (`-r 59.94` → ffprobe `avg_frame_rate` = 2997/50).
- Verified with REAL FFmpeg/ffprobe (worker integration tests over
  23.976/24/30/55/59.94/60 fps fixtures): output `avg_frame_rate` matches
  the source within ±0.011, **decoded frame counts** match
  `rate × duration` (catches bulk duplication/dropping), and A/V stream
  durations agree (≤ 50 ms) with the 1080x1920 contract intact.
- No upscale of low-res footage is claimed as 1080p native (the cover-crop
  scale remains geometric; documentation only).
- Everything else (rotation, captions, upload, completion guard) is
  untouched.

## 8. Providers (`worker/app/providers/`)

- `base.py`: `StreamProvider` protocol — `name`, `can_handle(url)`,
  `validate(url) -> ProviderCheck(status ∈ available|private|deleted|
  restricted|rate_limited|unavailable, title?, duration?, reason)`,
  capability flags `supports_validation` / `supports_materialization`,
  and `materialize(...)` that raises `MaterializationUnavailable` when
  the adapter does not implement an **authorized** download path.
- `upload.py`: implemented end-to-end (the file already exists in the
  private bucket via Phase 3 — validation = probe; materialization = the
  existing download).
- `youtube.py`: URL validation (watch/shorts/youtu.be + VOD paths),
  availability via the official **oEmbed** endpoint (keyless, no
  auth, no DRM), mapped: HTTP 404 → deleted/unavailable, `isLive`/type
  rules for VOD vs live. **`supports_materialization = False`** — the
  download path remains the Phase 2 explicit-provider integration point.
- `twitch.py`: URL validation (`/videos/{id}`, `/channels/{name}/videos/{id}`;
  live URLs rejected as "not a completed recording"), availability via
  the keyless **Helix `GET /videos`** public listing (404 → deleted/
  removed; 429 → rate_limited with safe retry guidance). **`supports_
  materialization = False`**.
- `registry.py`: dispatch by URL; unknown platform → `ProviderNotSupported`
  (never guessed).
- Tests use `httpx.MockTransport` (deterministic, offline, no paid
  services): available/private/deleted/rate-limited/invalid-URL matrix per
  provider; materialization refusal asserted.

## 9. Benchmarks (measured ≠ target — always labeled)

`worker/benchmarks/`:
- `fixtures.py`: a deterministic synthetic VOD (real FFmpeg lavfi, no
  network): 30 s with 6 ground-truth events at exact timestamps —
  burst + hard cut at 8.5 s (straddles the chunk 0/1 boundary), audio-only
  burst at 20.0 s, cut at 25.0 s (straddles chunk 2/3). 4 of the 6 events
  are boundary-straddlers — the exact case that loses/duplicates events in
  naive chunking.
- `run.py` (`python -m benchmarks.run --workdir <dir>`): interrupted run
  (budget 2 of 4 chunks) → resume → clean reference run → render the top
  candidate → JSON report. The report keeps `measured` (facts from the run)
  and `targets` (labeled aspirations) in separate blocks; secondary
  heuristic events are listed, never hidden.
- `tests/test_benchmarks.py`: executes the report and asserts the honest
  contract + the measurable targets (full recall on the scripted fixture,
  zero duplicates, straddled events persisted exactly once, zero
  reprocessed chunks on resume, valid render with A/V sync < 150 ms).

**Honesty rules (enforced in the report itself):**
- The fixture is **synthetic and scripted**. Its precision/recall measures
  pipeline correctness on KNOWN inputs — it is **NOT** a claim of
  real-world extraction accuracy and is labeled `"human_annotated": false`.
- Targets that require real footage (precision ≥ 0.9 / recall ≥ 0.8 on
  human-annotated VODs, median boundary error vs human annotation) are
  listed as **unmeasurable on this fixture** — the report never fills them
  with invented numbers.
- **OUTSTANDING (not delivered here): human-annotated real-VOD evaluation.**
  The benchmark framework is ready to ingest such fixtures (same
  ground-truth schema: `t / family / kind`), but no real, human-annotated
  gaming VODs exist in this repository and none were fabricated. That
  evaluation is a follow-up task; until it happens, all accuracy numbers in
  this project refer to the synthetic fixture only.

## 10. New dependency (justified)

- `numpy` (worker): windowed audio feature extraction over hours of PCM.
  Without it, pure-Python per-sample loops make a 1 h VOD take minutes of
  CPU per chunk; numpy keeps the same math deterministic and fast, is
  free, has no service, and is pinned like the rest of `requirements.txt`.
  No other runtime dependency is added (the ZIP engine precedent:
  hand-rolled where possible).

## 11. Job-pipeline integration (Phase 7b — implemented & tested)

The pipeline is now the **PRIMARY analysis path** of every claimed job:
`build_worker` (`app/main.py`) constructs `IntelStore` and passes it to
`AnalysisProcessor`, which runs `StreamIntelPipeline.run(job_id,
source_path, total_duration, temp_dir)` after the re-probe / audio check.
The Phase 4 transcript path remains (a) the fallback for a COMPLETED intel
run that yields no signal candidates, and (b) the entire path when
`intel_store` is not injected (existing behavior, byte-identical).

Lifecycle (every transition used was already legal in the Phase 4/5 map —
no transition-map or lock-constraint changes):

    ready ─claim─► transcribing ─► [intel run]
        complete     ─► analyzing ─► park_after_analysis (Phase 5 renders)
        incomplete   ─► requeue_incomplete_analysis ─► ready (resume)
        chunk error  ─► fail_job (transient, `intel_analysis_failed`)
                       ─► ready (next claim resumes; retries the chunk)
        resume cap   ─► fail_job (permanent, `intel_resume_exhausted`) ─► failed

- **Incomplete is not an error.** With the per-claim budget
  (`WORKER_INTEL_CHUNKS_PER_CLAIM`), a long VOD finishes its budget,
  checkpoints, and requeues to `ready`. The next claim resumes from
  `processing_checkpoints` — zero reprocessing (chunks skip when `done`;
  events upsert on `event_key`). A job is never parked/completed while
  chunks remain.
- **Resumes do not consume the failure budget.** The new
  `jobs.analysis_resumes` counter (Phase 5 `render_attempts` precedent)
  counts resume requeues; `claim_next_job`'s added branch claims a
  `ready` job with `analysis_resumes > 0` **without** incrementing
  `attempt_count`, so a 24 h VOD is not stranded after a few claims.
  The cap (`WORKER_INTEL_MAX_RESUMES`, default 100 000) is enforced only
  in `requeue_incomplete_analysis` → `resume_exhausted`, and the processor
  then fails the job PERMANENTLY — no silent stall, no silent success.
- **Crash recovery.** A job that dies mid-analysis is recovered by the
  existing expired-lease claim as `analyzing`; the idempotent pipeline
  replays only what is missing, and a job whose checkpoints are all
  `done` resumes to "complete" with no further decoding.
- **Ownership/lease.** Requeue/park/fail are owner-scoped; `not_owner`
  aborts without writes; cancellation (shutdown/lease loss) performs no
  state writes; temps are removed on every path.

New migration `20261125000000_phase7b_intel_integration.sql` (additive,
repository-only, **NOT applied to any live project**): `analysis_resumes`
column (default 0, check ≥ 0), the `requeue_incomplete_analysis` RPC, and
a backward-compatible `claim_next_job` re-creation (resume branch +
conditional `attempt_count` increment; service-role-only privileges kept).
All Phase 3–5 queue behavior is re-verified by the pglite suite (134 tests
green, including the re-created RPC).

Integration tests (real orchestration path, not isolated units):
`worker/tests/test_intel_integration.py` (7 — candidates→persist+park,
no-candidates→transcript fallback, incomplete→requeue→resume→park on a
shared client, failed chunk→transient fail (never silent success),
resume exhaustion→permanent fail, crash recovery at `analyzing`,
`not_owner`→abandon) and `tests/database/phase7b-integration.test.ts`
(16 — column semantics, requeue outcomes, claim resume branch incl.
no-attempt-burn and exhausted-budget-claimable, backward compatibility,
anon/service_role privileges).

## 12. Operational limits, configuration, migrations

**Limits** — no product-level duration cap; everything below is an
explicit, configurable operational limit:

| Setting (`WORKER_` prefix) | Default | Meaning |
| --- | --- | --- |
| `INTEL_CHUNK_SECONDS` | 600 | chunk length (s) |
| `INTEL_OVERLAP_SECONDS` | 30 | chunk overlap (s) |
| `INTEL_CHUNKS_PER_CLAIM` | 20 | chunks per claim — splits long VODs across claims |
| `INTEL_CHUNK_TIMEOUT_SECONDS` | 300 | per-chunk audio decode / visual pass budget |
| `INTEL_SCENE_THRESHOLD` | 0.3 | scene-change sensitivity |
| `INTEL_REQUESTED_CLIPS` | 5 | clip cap fallback (the job's `requested_clips` wins) |
| `INTEL_MAX_RESUMES` | 100000 | resume cap before a permanent failure |
| `RENDER_FPS_LOW` | 30 | fallback rate when the source rate is unmeasurable |
| `RENDER_FPS_HIGH` | 60 | cap for the preserved source rate |
| `FACECAM_ENABLED` | false | Phase 8 facecam modality master switch (§13; OFF by default) |
| `FACECAM_SAMPLE_FPS` | 1.0 | facecam frame sampling rate (0 < x ≤ 4) |
| `FACECAM_MIN_CONFIDENCE` | 0.5 | min detector confidence for a face to count (0 < x ≤ 1) |
| `FACECAM_MAX_FACES` | 3 | max faces considered per frame (1-8) |
| `FACECAM_MAX_CONCURRENCY` | 1 | inference concurrency bound (1-8; 1 = strict sequential) |
| `FACECAM_DETECTOR_MODEL_PATH` | *(unset)* | EXPLICIT detector model file (required to enable) |
| `FACECAM_LANDMARKER_MODEL_PATH` | *(unset)* | optional landmarker (enables geometry metrics) |
| `FACECAM_FRAME_TIMEOUT_SECONDS` | 120 | per-chunk frame-extraction wall budget |
| `GAME_VISION_ENABLED` | false | Phase 8 game-vision modality master switch (§14; OFF by default) |
| `GAME_VISION_SAMPLE_FPS` | 1.0 | sampled frames per second for OCR (0 < x ≤ 4) |
| `GAME_VISION_MIN_OCR_CONFIDENCE` | 60.0 | min tesseract line confidence (0-100) |
| `GAME_VISION_MAX_FRAMES_PER_CHUNK` | 1200 | hard cap on sampled frames per chunk |
| `GAME_VISION_MAX_OCR_OPERATIONS_PER_CHUNK` | 800 | hard cap on OCR subprocess runs per chunk |
| `GAME_VISION_MAX_CHUNK_SECONDS` | 240 | wall budget for the whole per-chunk game-vision pass |
| `GAME_VISION_FRAME_TIMEOUT_SECONDS` | 120 | per-chunk frame-extraction budget (killed after) |
| `GAME_VISION_OCR_TIMEOUT_SECONDS` | 30 | per-OCR-subprocess budget (killed after) |
| `GAME_VISION_OCR_LANGUAGE` | eng | tesseract language (pack must be installed explicitly) |
| `GAME_VISION_TESSDATA_DIR` | *(unset)* | explicit tessdata dir (never downloaded at runtime) |
| `GAME_VISION_TESSERACT_BINARY` | *(unset)* | explicit tesseract binary (None = PATH) |
| `GAME_VISION_KILL_FEED_ROI` | 0.80,0.02,0.18,0.12 | 'x,y,w,h' normalized kill-feed region (empty = full frame) |

Resources: RAM bounded per chunk (audio WAV ≈ 19 MB at 600 s; fusion/
selection input < 1 MB even for a 24 h VOD); per-chunk temp files deleted
in `finally`; the extracted full WAV remains ≈ 30 MB/h on disk. Chunks run
sequentially within a job; the worker semaphore bounds parallel jobs.

**Migrations pending on live databases** (repository-only so far):
`20261124000000_phase7_stream_intelligence.sql` (checkpoints/events
tables, twitch source_type) and
`20261125000000_phase7b_intel_integration.sql` (resume column/RPC/claim
branch). **Neither has been applied to any live Supabase project.**
Operational note: on a database without those tables the worker must run
WITHOUT the `IntelStore` injection (Phase 4–6 behavior); otherwise intel
store calls error and analysis jobs fail. Apply the migrations before
turning on the integrated path in any environment.

## 13. Facecam Intelligence (TASK 008 — Phase 8)

A real, local-first facecam modality for the intel pipeline. **OFF by
default**; enabling it requires the optional extra + explicit model files.
When disabled or unavailable the pipeline is exactly Phase 7 — zero
overhead, zero behavioral change, no fabricated data.

### What it measures (visual patterns only — never emotion/identity)
- **Facecam presence + region**: faces detected per sampled frame
  (bbox + confidence, multi-face, NO largest-face assumption). Tracks are
  linked by bbox IoU; the facecam region is the track with the highest
  **temporal persistence** (≥ `region_persistence_ratio` of sampled
  frames) AND **spatial consistency** (mean pairwise IoU ≥
  `region_min_iou`) AND overlay-scale area (≤ `region_max_area_ratio`) —
  so a persistent small overlay wins over a large, briefly-present
  in-game face, and a full-frame webcam is reported as presence, not an
  "overlay region".
- **Facial activity (measured geometry, heuristic labels)** on the
  primary face: mouth-opening ratio vs chunk median (bursts), smile-like
  geometry score (blendshapes + mouth-width/interocular ratio, sustained
  runs), brow lift (blendshapes + brow-gap geometry, bursts), head
  rotation delta between consecutive samples (sudden pose changes), and
  per-frame visibility/tracking confidence (scales event strength).
  Every event's evidence states it is a visual pattern, **NOT** an
  emotion, state, or identity classification.

### Implementation (`worker/app/intel/facecam/`)
- `frames.py` — bounded FFmpeg extraction per chunk window: `-ss/-t` +
  `fps=<sample_fps>` + `-frames:v <cap>` → JPEG grid on disk (default
  1 fps; 1200-frame hard cap; wall-clock timeout + kill). Only sampled
  frames are ever decoded — never the whole VOD.
- `model.py` — MediaPipe **Tasks API** (CPU delegate by policy;
  per-job instance, no cross-job native state; model init ≈ 35 ms).
  `blaze_face_short_range.tflite` (229,746 B) detects;
  `face_landmarker.task` (3,758,596 B) provides 478 landmarks + 52
  blendshapes + the 4×4 facial transformation matrix (pose delta from
  convention-independent rotation comparison).
- `geometry.py` — pure-numpy measures (mediapipe-free, unit-testable).
- `region.py` / `signals.py` — track linking + region selection;
  `detect_facecam_events` → `SignalEvent`s (family `facecam`) with
  ABSOLUTE timestamps (chunk offset applied exactly once), measured
  evidence + detector metadata, and the same 0.1 s `event_key` bucketing
  — so checkpoint/dedup/midpoint-ownership semantics from §2/§4 apply
  UNCHANGED (an event in the overlap is persisted exactly once).
- `__init__.py` — `FacecamAnalyzer` interface + `NullFacecamAnalyzer`
  (kept as the honest fallback) + `MediaPipeFacecamAnalyzer`. **Honest
  unavailability at every boundary**: disabled / no model path /
  unreadable model / missing dependency → sticky `unavailable` (no retry
  storm), recorded per chunk in the checkpoint summary
  (`features.facecam`, nested — no schema change, NO migration) and in
  `PipelineResult.facecam`. Frame-extraction failure → per-chunk
  unavailable; audio/visual events still persist.
- **Integration**: `PipelineConfig.facecam` (None = disabled); the
  analyzer is owned by the pipeline and `close()`d on EVERY exit path
  (complete / incomplete / cancelled / error); cancellation maps
  `FacecamCancelled` → `PipelineCancelled` (no state written);
  `analysis.py` builds it only when `FACECAM_ENABLED` + detector path are
  set. Fusion: `facecam_weight` 0.5 with a solo factor of 0.4 — a
  facecam-only low-confidence observation can NEVER qualify alone.
  Selection categories for facecam evidence are labeled heuristic.

### Model + dependency policy
- Weights: official MediaPipe model files, **Apache-2.0** (model card:
  identity recognition explicitly out of scope). Explicitly configured
  via settings and validated at first use — **never downloaded during
  job execution**. No cloud API, no paid service, CPU-only.
- `mediapipe==1.2.3` lives in `worker/requirements-facecam.txt` — NOT in
  the base install (no silent large dependency).
- Runtime note: the mediapipe CPU inference path links EGL/GLES shared
  libraries (mesa on Linux); where those are absent the modality
  degrades to honest `unavailable` instead of crashing.

### Tests (`worker/tests/test_facecam.py`, 50)
Synthetic fixtures only (AI-generated face images, no real people, no
accuracy claims): region persistence vs in-game false positives vs
multi-face vs occlusion vs full-frame; signals (burst/smile/brow/head,
absolute timestamps, evidence, key stability, confidence scaling,
detector-only mode fabricates nothing); frames (bounded grid, caps,
timeouts, honest errors); missing model / missing dependency → honest
unavailable; pipeline-level (disabled = Phase 7 unchanged;
persist/resume/dedup across the overlap; unavailable recorded honestly;
cancellation writes nothing + cleans frames); and 5 **real-model**
smoke tests that skip cleanly when the extra/models are absent
(CI runs them in a best-effort step).

## 14. Game Vision Intelligence (TASK 009 — Phase 8)

A real, local-first **OCR-based** gameplay modality for the intel
pipeline. **OFF by default**; enabling it requires the system
`tesseract-ocr` package (plus the optional Pillow extra). When
disabled or unavailable the pipeline is exactly as before — zero
overhead, zero behavioral change, no fabricated data.

**What it measures (OCR-observed content only — never verified game
state):**
- **OCR (A)**: visible text + numeric values from sampled frames, with
  timestamps and per-line confidence; conservative normalization
  (case/punctuation preserved); missing binary/tessdata/language →
  honest `unavailable`.
- **HUD (B)**: stable interface text (lines persisting in ≥ 50% of a
  chunk's frames — region stability measured, game-agnostic) + a
  frame-to-frame region change metric (Pillow; honestly skipped when
  absent). Regions of interest are **configuration** (normalized
  `x,y,w,h`), not hardcoded game assumptions; full-frame scan is
  supported.
- **Kill feed (C)**: transient text in the configured region across
  consecutive frames — new vs persistent distinguished, fade-jitter
  merged (the same physical line is never counted twice),
  disappearance recorded, raw text + confidence kept as evidence.
- **Scoreboard (D)**: visibility via a strict text-density heuristic
  (labeled as such) + `label value` pairs extracted ONLY where OCR read
  them; value changes reported only when both readings were observed —
  an unreadable frame breaks the chain rather than fabricating a change.
- **Gameplay events (E)**: conservative candidates — `kill_feed_entry`,
  `scoreboard_appearance`, `score_change`, `match_result_text`
  (generic tokens, EXACT full-line match). Every candidate is flagged
  `directly_observed` and its interpretation is recorded as heuristic.

**Implementation (`worker/app/intel/game_vision/`)**: `config.py`
(validated bounds), `ocr.py` (engine interface + direct tesseract-CLI
TSV engine with per-op timeout + kill, no wrapper dependency),
`frames.py` (bounded extraction + FFmpeg `crop`/`scale` ROIs — the VOD
is never decoded whole), `hud.py`, `kill_feed.py`, `scoreboard.py`,
`events.py`, `signals.py` (family `game_vision`), `__init__.py`
(analyzer + Null + builder + job summary).

**Integration**: events enter `raw_events` BEFORE midpoint ownership —
existing `event_key`/checkpoint/dedup semantics apply unchanged (overlap
events persisted exactly once). Per-chunk summary nested in
`features.game_vision` (jsonb — **no schema change, NO migration**).
Fusion: `game_vision_weight` 0.5, solo factor 0.4 — low-confidence
OCR-only observations can never qualify a clip alone (strength ≥ 0.625
needed). Selection: categories for game-vision evidence are labeled
heuristic. Cancellation → `PipelineCancelled` (no writes); temp dirs +
subprocesses released on EVERY exit path; analyzer closed on every
pipeline exit.

**Model/dependency policy**: tesseract is the public, **Apache-2.0**
system package (Debian/Ubuntu: `tesseract-ocr` + `tesseract-ocr-eng`);
the binary + language packs are explicitly configured and validated
(binary → tessdata → version probe, deterministic order), **never
downloaded during job execution**. No cloud OCR, no paid API, CPU-only.
Pillow (MIT) lives in `worker/requirements-game-vision.txt` — NOT in
the base install.

**Resource limits (enforced)**: frames/chunk ≤ 1200; OCR ops/chunk ≤
800; wall budget 240 s/chunk (checked between ops — on budget, the
report records `budget_exceeded` + `frames_remaining` and keeps the
events already measured); per-OCR subprocess 30 s (killed); images
capped at 1280 px; consecutive OCR errors ≥ 10 → sticky honest
unavailable.

**Tests (`worker/tests/test_game_vision.py`)**: 64 tests — stub-OCR
logic (OCR success/unavailable/bad binary/bad tessdata; TSV parsing;
HUD stability + change metric; kill-feed appear/persist/disappear/
fade-jitter dedup/distinct re-appearance/exclusions/bounds; scoreboard
detection/change tracking/no-fabrication; event kinds; absolute
timestamps at nonzero offsets; op cap / wall budget / sticky
degradation; cancellation + cleanup; pipeline disabled-unchanged /
persist-resume-dedup / honest unavailable / nested summary / facecam
coexistence; fusion + selection integration) plus 4 **real-OCR** smoke
tests (PIL-rendered text) that skip cleanly without tesseract (CI runs
them in a best-effort step). **Synthetic fixtures only — no real-world
detection accuracy is claimed; there is no annotated real-VOD
evaluation (known limitation).**

## 15. Advanced Multimodal Fusion (TASK 010 — Phase 8)

A temporal **multimodal intelligence engine** over the four signal
families (audio, visual, facecam, game_vision) that correlates
independently measured evidence across time, evaluates its quality,
detects event sequences, and produces **explainable candidate moments**
for the EXISTING selection. It is an **optional layer — not a
replacement**: disabled (the default, `WORKER_MULTIMODAL_ENABLED=false`)
the completion phase runs Phase 7 `fuse()` **exactly** as before
(verified by an A/B pipeline test against an independent
`fuse()` + `select_clips()` recomputation); enabled, the same
qualifying candidates (the UNCHANGED `score_cluster` bar — identical
scores) gain a conservative `heuristic_confidence` and size-bounded
evidence provenance.

**Architecture (`worker/app/intel/multimodal/`)** — five conceptual
layers, one module each concern:

1. **Normalization** (`timeline.py`) — a validated re-projection of the
   existing `SignalEvent` contract (no competing schema): stable
   `event_key`, absolute timestamps, family/kind, ORIGINAL strength,
   original evidence (referenced, never mutated), detector identity,
   DERIVED quality (OCR `ocr_confidence`/100 where measured; strength
   elsewhere). Malformed events are **quarantined** (counted + recorded
   with a safe reason), never crash the job. Deterministic ordering;
   time-ordered input truncation at `max_events`.
2. **Temporal correlation** (`correlation.py`) — deterministic
   sweep-line clustering (one pass over time-ordered signals; the same
   union-chain rule as Phase 7, `correlation_window_seconds` 2.5 s)
   with a **three-tier duplicate-suppression** that implements
   evidence independence:
   - tier 1: same stable `event_key` → exact duplicate (one kept);
   - tier 2: same family + kind within `same_kind_merge_seconds` (1 s)
     AND, for OCR content (text / score values), the SAME observed
     content → repeat of the SAME physical signal (corroboration only,
     max strength kept). Two DIFFERENT OCR readings are never the same
     physical signal — they may be contradictory;
   - tier 3: same family, different kind (or same kind beyond the
     merge gap) → RELATED: still ONE independent modality.
   An independent modality is a distinct family — a loud burst and an
   audio-derived excitement event from the same detector are never two
   modalities; repeated face landmarks of one continuous reaction never
   inflate; repeated OCR samples of one persistent kill-feed line never
   inflate. Cross-family proximity is weighted by
   `exp(-gap / decay_tau_seconds)`.
3. **Interpretation** — `event_graph.py`: a bounded graph (nodes =
   kept observations ≤ 64; edges = overlap/precedes/follows relations
   within the window, ≤ 8 per node; a one-line temporal explanation) +
   distinct-kind sequences (length ≤ 4, per-step start tolerance 2.5 s —
   overlapping reactions still chain; persistent same-kind runs extend
   a stage, never repeat it). `sequences.py`: five configurable,
   conservative cross-modal PATTERNS (candidate interpretations, never
   verified game state): `gameplay_elimination` (kill-feed anchor, ≥ 2
   modalities), `high_impact_gameplay` (visual-change anchor, ≥ 2),
   `match_victory` (match-result-text anchor, ≥ 1),
   `unexpected_reaction` (facecam anchor, ≥ 2), `strong_audio_only`
   (vocal anchor, ≥ 1, strength ≥ 0.80). Patterns carry a bounded
   confidence bonus (0.10) and a **score bonus of 0.0 by default —
   matching a pattern never inflates a score**; operators may opt in
   explicitly.
4. **Confidence & evidence** — `confidence.py`:
   `heuristic_confidence` (version `mv1`), a bounded, deterministic
   aggregate:

   ```
   base         = Σ w_f·max_strength_f / Σ w_f  (PRESENT families;
                the existing FusionConfig weights — detector strengths
                are PRESERVED)
   independence = 1 + diversity_bonus·max(0, min(n_mods,3)−1)/2
                (1 modality → 1.0 — a single modality gets NO credit;
                3+ → 1 + bonus — bounded)
   proximity    = mean cross-family exp(-gap/τ) (1.0 when nothing
                measured — absence of data is NOT a penalty)
   quality      = mean quality over ALL kept evidence (duplicates
                included: a repeatedly low-quality read stays low)
   pattern      = 1 + pattern.confidence_bonus when matched
   penalties    = (1 − contradiction_penalty) per contradiction
                · (1 − low_quality_penalty) if quality < 0.40
   value        = clamp(base·independence·proximity·quality·pattern
                ·penalties, 0, 1)
   ```

   **TERMINOLOGY (mandatory until calibration exists): this is
   `heuristic_confidence` — NOT a statistically calibrated probability
   of correctness.** No annotated real-VOD evaluation exists yet;
   building one is TASK 013's scope. Every result exposes its FULL
   calculation (base, per-family max strengths, independence,
   proximity weights, quality mean, pattern, each penalty) plus the
   version, so the number is always reproducible and auditable.
   Detector confidence and fusion confidence are distinct fields
   (quality vs heuristic_confidence). Contradictions are a deliberately
   SMALL, explicit rule set (conflicting win/loss match-result readings
   in one cluster); missing data / unavailable detectors are NEVER
   negative evidence. `evidence.py`: size-bounded provenance per
   candidate (≤ 8 KiB serialized — hard byte cap with a deterministic
   shrink cascade, `provenance_truncated` flag when the cascade runs):
   supporting event keys/families/kinds/timestamps/original strengths/
   qualities/detectors (total always exact), independent observations
   and modalities, temporal relationships, sequences, the matched
   pattern, excluded/duplicate evidence (exact/repeated/overflow
   counts), contradictions, availability snapshot, and the confidence
   calc + version. Nothing that materially affects ranking is
   silently discarded.
5. **Candidate ranking** (`ranking.py`, `signals.py`) — measured
   boundaries (cluster span ± padding, clamped to `[0, total_duration]`
   — never negative, never beyond the source, never empty) with
   deterministic suppression (recorded, never silent):
   `below_min_score` (the SAME Phase 7 bar),
   `insufficient_independent_modalities` (configurable floor, default
   1), `below_confidence_threshold` (default 0.30 — enabled mode may
   select LESS than Phase 7, never arbitrarily more),
   `single_source_repetition` (one family below max strength 0.60),
   `empty_interval` (defensive). Candidates capped at 200
   (deterministic: strongest first). The adapter emits the SAME
   `CandidateWindow` type Phase 7 returns (identical score/
   qualification; upgraded confidence; `reasons["multimodal"]`
   provenance), so selection, merge, dedup, categories, ranking and the
   renderer are UNTOUCHED; `select_clips` passes the provenance through
   into the clip evidence as a nested `multimodal` key (absent in
   disabled mode — byte-compatible).

**Integration**: `PipelineConfig.multimodal` (None = disabled) +
`PipelineResult.multimodal` (the job-level engine summary). The engine
is a **pure function of the persisted events** — chunk-compatible and
resumable exactly like Phase 7 (re-running completion re-derives the
same candidates from the same rows). Availability states are derived
from the MEASURED modality summaries (never from import success):
`disabled` / `unavailable` / `available` / `available_no_events` /
`partial` / `failed` / `cancelled`, recorded per family in the summary
and in each candidate's provenance. A single unavailable modality never
fails the analysis.

**Performance budgets (ENFORCED — deterministic truncation + honest
reporting in the summary, never silent)**: input events ≤ 5000
(time-ordered cut, `input_truncated`); clusters ≤ 500
(`clusters_dropped` = earliest kept); signals/cluster ≤ 64 (overflow
kept for scoring, never independent); graph edges/node ≤ 8; sequences
≤ 8/cluster, length ≤ 4; candidates ≤ 200 (`candidates_capped`);
processing budget 30 s wall (checked BETWEEN clusters —
`status="budget_exceeded"`, `clusters_unprocessed` counted; what was
analyzed stays valid). Correlation/graph are sweep-line single passes —
no unbounded all-pairs, no unbounded in-memory timelines.

**Configuration** (`WORKER_MULTIMODAL_*`, all validated at startup; see
`worker/README.md` for the full table): enable/disable, correlation
window, decay τ, same-kind merge gap, confidence threshold, min
independent modalities, strong-audio-only threshold, boundary padding,
single-source floor, and every hard budget above.

**Persistence**: nested JSON only — the provenance rides inside the
clip's existing `evidence` jsonb (size-bounded to ≤ 8 KiB per clip);
the engine summary is job-level metadata. **No schema change, no
migration.** No large internal graphs are exposed to ordinary frontend
endpoints (the clip evidence carries the bounded provenance record only).

**Known limitations**: heuristic confidence is NOT calibrated (see
terminology above); patterns are conservative candidate
interpretations, not verified game state; the contradiction rule set is
deliberately minimal (one explicit rule); the wall budget check
introduces machine-speed-dependent truncation in extreme cases (the
report stays honest); synthetic-fixture tests only — no real-world
accuracy claims.
