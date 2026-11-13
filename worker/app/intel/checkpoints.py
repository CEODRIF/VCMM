"""Checkpoint + candidate-event persistence (TASK 007).

Service-role, job-scoped access to the Phase 7 tables — same safety
contract as ``SupabaseClipStore``:

- the job id is UUID-validated BEFORE it can widen any query scope;
- every read/write is filtered by ``job_id`` (a stale run can never
  touch another job's checkpoints or events);
- event writes are UPSERTS keyed on ``(job_id, event_key)`` — retrying a
  chunk (crash, re-claim) exact-deduplicates instead of duplicating;
- ``reset_for_job`` is the only delete and it is scoped to one job.

The tables are deny-by-default RLS (no policies) — only the service-role
worker client can reach them; the API/UI never reads them directly.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

from supabase import Client

from .signals import SignalEvent, from_row

logger = logging.getLogger("clipforge.intel.store")

CHECKPOINTS_TABLE = "processing_checkpoints"
EVENTS_TABLE = "candidate_events"

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class IntelStoreError(RuntimeError):
    """Checkpoint/event persistence failed (safe message, no internals)."""


def _as_job_uuid(job_id: str) -> str:
    value = str(job_id or "").lower()
    if not _UUID_RE.match(value):
        raise ValueError("intel store requires a UUID job id")
    UUID(value)
    return value


class IntelStore:
    """Durable, resumable state for one job's stream-intelligence run."""

    def __init__(self, client: Client) -> None:
        self._client = client

    # -- checkpoints -------------------------------------------------------------

    async def load_done_chunks(self, job_id: str) -> dict[int, dict[str, Any]]:
        """Returns ``{chunk_index: row}`` for all DONE chunks of the job."""
        uuid_job = _as_job_uuid(job_id)
        try:
            result = await (
                self._client.table(CHECKPOINTS_TABLE)
                .select("chunk_index,start_time,end_time,features")
                .eq("job_id", uuid_job)
                .execute()
            )
            return {int(r["chunk_index"]): r for r in (result.data or [])}
        except Exception as exc:  # noqa: BLE001 — transport boundary
            logger.warning("checkpoint load failed for job %s: %s", uuid_job, exc.__class__.__name__)
            raise IntelStoreError("checkpoints could not be loaded") from exc

    async def mark_chunk_done(
        self, job_id: str, chunk_index: int, start: float, end: float, features: dict[str, Any]
    ) -> None:
        """Upserts the DONE checkpoint for one chunk (idempotent)."""
        uuid_job = _as_job_uuid(job_id)
        row = {
            "job_id": uuid_job,
            "chunk_index": int(chunk_index),
            "start_time": round(float(start), 3),
            "end_time": round(float(end), 3),
            "features": features,
        }
        try:
            await (
                self._client.table(CHECKPOINTS_TABLE)
                .upsert(row, on_conflict="job_id,chunk_index", ignore_duplicates=False)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001 — transport boundary
            logger.warning("checkpoint write failed for job %s: %s", uuid_job, exc.__class__.__name__)
            raise IntelStoreError("checkpoint could not be stored") from exc

    # -- candidate events ------------------------------------------------------------

    async def upsert_events(self, job_id: str, events: list[SignalEvent]) -> int:
        """Upserts a chunk's events (exact-dedup on the stable event_key)."""
        uuid_job = _as_job_uuid(job_id)
        if not events:
            return 0
        rows = [e.to_row(uuid_job) for e in events]
        try:
            await (
                self._client.table(EVENTS_TABLE)
                .upsert(rows, on_conflict="job_id,event_key", ignore_duplicates=True)
                .execute()
            )
            return len(rows)
        except Exception as exc:  # noqa: BLE001 — transport boundary
            logger.warning("event upsert failed for job %s: %s", uuid_job, exc.__class__.__name__)
            raise IntelStoreError("candidate events could not be stored") from exc

    async def load_events(self, job_id: str) -> list[SignalEvent]:
        """Loads the job's persisted events in time order (fusion input)."""
        uuid_job = _as_job_uuid(job_id)
        try:
            result = await (
                self._client.table(EVENTS_TABLE)
                .select("start_time,end_time,evidence,score,confidence")
                .eq("job_id", uuid_job)
                .order("start_time")
                .execute()
            )
            return [from_row(r) for r in (result.data or [])]
        except Exception as exc:  # noqa: BLE001 — transport boundary
            logger.warning("event load failed for job %s: %s", uuid_job, exc.__class__.__name__)
            raise IntelStoreError("candidate events could not be loaded") from exc

    # -- reset (scoped) -----------------------------------------------------------------

    async def reset_for_job(self, job_id: str) -> None:
        """Deletes ONE job's intel state (full re-run). Never another job's."""
        uuid_job = _as_job_uuid(job_id)
        try:
            await self._client.table(EVENTS_TABLE).delete().eq("job_id", uuid_job).execute()
            await self._client.table(CHECKPOINTS_TABLE).delete().eq("job_id", uuid_job).execute()
        except Exception as exc:  # noqa: BLE001 — transport boundary
            logger.warning("intel reset failed for job %s: %s", uuid_job, exc.__class__.__name__)
            raise IntelStoreError("intel state could not be reset") from exc
