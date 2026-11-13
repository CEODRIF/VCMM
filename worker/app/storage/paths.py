"""Storage path helpers (local filesystem layout, object storage later).

All user-controlled file names are sanitized to their basename component so
path traversal is impossible.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID


def job_dir(storage_root: Path, job_id: UUID) -> Path:
    """Per-job storage directory."""
    return storage_root / "jobs" / str(job_id)


def upload_path(storage_root: Path, job_id: UUID, filename: str) -> Path:
    """Destination for an uploaded source file."""
    safe_name = Path(filename).name  # strip any directory components
    return job_dir(storage_root, job_id) / "uploads" / safe_name


def output_path(storage_root: Path, job_id: UUID, clip_index: int) -> Path:
    """Destination for a rendered clip (0-based index within the job)."""
    return job_dir(storage_root, job_id) / "clips" / f"clip-{clip_index:02d}.mp4"
