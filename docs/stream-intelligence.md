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
5. **Fixed 30 FPS output** — `RenderProfile.fps=30` (`-r 30`) downsamples
   60 FPS gaming sources (quality loss; the task asks to preserve the
   source rate where practical).

### 1.3 What this task deliberately does NOT do (scope honesty)

- **No in-loop claim-integration yet.** The intel pipeline is a complete,
  standalone, checkpointed orchestrator with a clean seam (a progress
  reporter + the existing stores). Wiring it into `AnalysisProcessor`
  needs a park/requeue design for "incomplete, continue later" that must
  not collide with the Phase 5A parked-`analyzing`→rendering claim. That
  integration is the documented next step (§8); half-wiring it now would
  risk the working Phase 4-6 paths, which the task forbids without
  demonstrated need.
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
- **Facecam presence / expression**: `FacecamSignal` protocol; the
  `NullFacecamSignal` returns "absent" (the fusion weights simply skip it
  — a clip WITHOUT facecam can still qualify, per the task).
- **HUD/kill-feed OCR**: `GameplayOcrSignal` protocol, optional, off.
  No game is assumed to expose the same indicators.

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

## 7. Rendering (one targeted fix + contract)

- `probe_media`/renderer now measures the source **frame rate**;
  `select_output_fps(source_fps)` → 60 when source ≥ 55 fps, else 30
  (configurable `WORKER_RENDER_FPS_HIGH/LOW`). A 30 fps source is never
  upsampled; a 60 fps source is no longer downsampled. Output contract
  (FFprobe) accepts 30 or 60 accordingly.
- No upscale of low-res footage is claimed as 1080p native (the cover-crop
  scale remains geometric; documentation only).
- Everything else (rotation, captions, audio sync, upload, completion
  guard) is untouched.

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

## 11. Integration seam (next step, out of scope here)

`AnalysisProcessor` wiring requires a queue-side "incomplete, continue
later" transition (distinct from `park_after_analysis`, which Phase 5A
claims for rendering). The orchestrator already exposes exactly what the
hook needs: `StreamIntelPipeline.run(job_id, source_path, total_duration,
temp_dir, should_stop) -> PipelineResult {status: complete|incomplete,
processed, remaining, events, candidates}`. When the transition RPC is
designed (follow-up task), the processor calls the pipeline with the same
lease/ownership/temp contract it already enforces; the top-N selected
clips feed the existing clip store (60–180 s, `clips_duration_bounds`),
and the Phase 4 transcript detector remains the fallback for jobs where the
intel pass yields nothing.
