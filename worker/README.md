# ClipForge AI — Worker

Video processing runs **exclusively** in this worker service, never inside
Vercel serverless functions.

Phase 3 delivers **ingestion**: the worker claims jobs atomically from the
Supabase Postgres queue, downloads the source from private Supabase Storage,
validates the real bytes with FFprobe, and marks the job `ready` (with
measured media metadata) — or fails it with a safe, categorized error
(bounded retries for transient errors; immediate terminal failure for
unrecoverable media). AI transcription/analysis (Phase 4) and rendering
(Phase 5) are not yet implemented.

## Storage model — read this first

**Supabase Storage is the durable source of truth.** The worker's local disk
(`WORKER_STORAGE_ROOT`) is a processing scratchpad only: each job downloads
its source to `tmp/{job_id}/`, and that directory is deleted on every path —
success, failure or cancellation. Nothing durable is written to local disk.
(Phases 4-5 write rendered clips to the `clipforge-clips` bucket, not to the
volume.)

## Requirements

- Python 3.12
- `ffprobe` (and `ffmpeg`, used by tests to generate synthetic fixtures) on
  the host or in the image — or set `WORKER_FFPROBE_PATH` /
  `WORKER_FFMPEG_PATH`. The Docker image installs them via apt.
- The Phase 3 database migration applied to the same Supabase project
  (queue RPCs are granted to `service_role`).

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

## Queue contract (Phase 3)

All queue traffic goes through the restricted, `SECURITY DEFINER` RPCs from
migration `20261115000000` (`claim_next_job`, `renew_job_lease`,
`update_job_progress`, `complete_job_ingestion`, `fail_job`, `release_job`).
They are revoked from PUBLIC and granted to `service_role` only; every update
is scoped to the calling worker's lease, so two workers can never own the
same active job. Expired leases make crashed jobs reclaimable (crash
recovery). The worker renews each job's lease while processing and aborts
safely (no stale state writes) when a renewal is rejected.

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
├── rendering/     # FFmpeg renderer contract (Phase 5)
├── storage/       # Private Supabase Storage download + local path utilities
├── validation/    # FFprobe media validation (container/streams/limits)
├── worker.py      # Ingestion loop (claim, concurrency, backoff, shutdown)
└── config/        # Settings + structured logging
```
