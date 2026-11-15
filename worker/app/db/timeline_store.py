"""TASK 015 — full-timeline persistence (analysis_timeline).

Service-role, job-scoped access — same safety contract as
:class:`app.intel.checkpoints.IntelStore`:

- the job id is UUID-validated BEFORE it can widen any query scope;
- every read/write is filtered by ``job_id`` (a stale run can never
  touch another job's timeline);
- window writes are UPSERTS keyed on ``(job_id, window_index)`` — a
  re-claimed job resumes from persisted windows instead of re-paying
  the Gemini calls for already-assessed windows (idempotent, no
  duplicates, no cost leak);
- ``reset_for_job`` is the only delete and it is scoped to one job
  (job-level cleanup / failed-job cleanup use it).

The table is deny-by-default RLS (no policies) — only the service-role
worker client can reach it; the API/UI never reads it directly.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

from supabase import Client

logger = logging.getLogger("clipforge.db.timeline")

TIMELINE_TABLE = "analysis_timeline"

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class TimelineStoreError(RuntimeError):
    """Timeline persistence failed (safe message, no internals)."""


def _as_job_uuid(job_id: str) -> str:
    value = str(job_id or "").lower()
    if not _UUID_RE.match(value):
        raise ValueError("timeline store requires a UUID job id")
    UUID(value)
    return value


class TimelineStore:
    """Durable, resumable state for one job's full-timeline analysis."""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def load_rows(self, job_id: str) -> dict[int, dict[str, Any]]:
        """Returns ``{window_index: row}`` for the job's persisted windows
        (resume: skip windows whose Gemini assessment already completed)."""
        uuid_job = _as_job_uuid(job_id)
        try:
            result = await (
                self._client.table(TIMELINE_TABLE)
                .select("window_index,start_time,end_time,transcript,scene_description,"
                        "event_type,visual_evidence,audio_evidence,event_importance,"
                        "candidate_clip,confidence,gemini_assessment")
                .eq("job_id", uuid_job)
                .order("window_index")
                .execute()
            )
            return {int(r["window_index"]): r for r in (result.data or [])}
        except Exception as exc:  # noqa: BLE001 — transport boundary
            logger.warning("timeline load failed for job %s: %s", uuid_job, exc.__class__.__name__)
            raise TimelineStoreError("analysis timeline could not be loaded") from exc

    async def upsert_rows(self, job_id: str, rows: list[dict[str, Any]]) -> int:
        """Upserts window rows (exact-dedup on (job_id, window_index))."""
        uuid_job = _as_job_uuid(job_id)
        if not rows:
            return 0
        # Chunk to bound a single REST payload (rows are small metadata).
        written = 0
        for i in range(0, len(rows), 50):
            chunk = rows[i:i + 50]
            try:
                await (
                    self._client.table(TIMELINE_TABLE)
                    .upsert(chunk, on_conflict="job_id,window_index", ignore_duplicates=False)
                    .execute()
                )
                written += len(chunk)
            except Exception as exc:  # noqa: BLE001 — transport boundary
                logger.warning("timeline upsert failed for job %s: %s",
                               uuid_job, exc.__class__.__name__)
                raise TimelineStoreError("analysis timeline could not be stored") from exc
        return written

    async def reset_for_job(self, job_id: str) -> None:
        """Deletes ONE job's timeline (cleanup / full re-run). Never
        another job's."""
        uuid_job = _as_job_uuid(job_id)
        try:
            await self._client.table(TIMELINE_TABLE).delete().eq("job_id", uuid_job).execute()
        except Exception as exc:  # noqa: BLE001 — transport boundary
            logger.warning("timeline reset failed for job %s: %s", uuid_job, exc.__class__.__name__)
            raise TimelineStoreError("analysis timeline could not be reset") from exc
