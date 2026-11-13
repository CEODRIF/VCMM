# ClipForge AI — Worker

Video processing runs **exclusively** in this worker service, never inside
Vercel serverless functions.

Phase 1 delivers the foundation only: a FastAPI app with `GET /health`,
validated environment configuration, structured JSON logging, and the module
scaffolding for the pipeline. **No videos are downloaded or rendered here yet.**

## Requirements

- Python 3.12
- (Phase 5 adds) FFmpeg on the host or in the image

## Local development

```bash
cd worker
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# run the API (health endpoint)
uvicorn app.main:app --reload --port 8000
```

Check it:

```bash
curl -s http://localhost:8000/health | python -m json.tool
```

## Tests

```bash
cd worker
python -m pytest
```

## Configuration

All settings use the `WORKER_` env prefix and are validated by Pydantic at
startup (see `app/config/settings.py`):

| Variable | Default | Notes |
| --- | --- | --- |
| `WORKER_ENVIRONMENT` | `development` | `development` \| `staging` \| `production` |
| `WORKER_LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL` |
| `WORKER_STORAGE_ROOT` | `storage` | Base directory for uploads and rendered clips |
| `WORKER_REQUEST_TIMEOUT_SECONDS` | `30` | Must be > 0 and ≤ 300 |

## Docker

```bash
docker build -t clipforge-worker .
docker run --rm -p 8000:8000 -v clipforge-storage:/data clipforge-worker
```

## Module map (Phase 1)

```
worker/app/
├── api/           # HTTP endpoints (health today; job control later)
├── queue/         # Job queue models and (later) backend integration
├── processors/    # Pipeline stage order and (later) execution
├── transcription/ # Whisper adapter contract (Phase 4)
├── ai/            # Clip detection contract (Phase 4)
├── rendering/     # FFmpeg renderer contract (Phase 5)
├── storage/       # Local path helpers (object storage later)
└── config/        # Settings + structured logging
```
