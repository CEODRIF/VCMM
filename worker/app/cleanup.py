"""Artifact cleanup (Phase 6) — MANUAL, idempotent purge of expired job artifacts.

Retention policy (explicit — see
``supabase/migrations/20261123000000_phase6_results_retention.sql``):

- When a job transitions to ``completed`` OR ``failed``, the DB trigger sets
  ``expires_at = now() + 30 days`` (only when NULL — an operator-set
  deadline always wins and is never overwritten).
- A job is purge-eligible ONLY when it is TERMINAL (completed/failed),
  EXPIRED (``expires_at < now``) and NOT YET PURGED (``purged_at IS NULL``).
- In-progress jobs are never touched, regardless of any marker.
- Jobs without an explicit ``expires_at`` (e.g. created before the
  migration) are never purged — no guessed retention period.

Deletion order (at-least-once, crash-safe):

1. Delete the job's SOURCE objects (``sources/{job_id}/original/*``).
2. Delete the job's CLIP objects — only canonical
   ``clips/{job_id}/{clip_id}.mp4`` paths whose stored ``storage_path``
   matches the shape AND the owning job (same cross-check as the API).
3. Set ``purged_at`` with a GUARDED update (terminal + still expired +
   still un-purged).

If the process dies between (2) and (3), the next run simply retries: the
storage listings are empty (or the deletes are no-ops), nothing is deleted
twice, and the marker is finally set. A job is therefore NEVER marked
purged while its objects still exist, and deleting an already-deleted
object is harmless.

Known residual race (documented): a deadline extended by an operator
WITHIN the small window between the candidate check and the guarded mark
may be lost for one job. Pause purging around manual deadline extensions.

NOT WIRED INTO ANY SCHEDULER — run it manually:

    cd worker && python -m app.cleanup [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from supabase import Client

from .storage.supabase_storage import (
    CLIPS_BUCKET,
    SOURCES_BUCKET,
    _CLIP_PATH_PATTERN,
    _SOURCE_PATH_PATTERN,
)

logger = logging.getLogger("clipforge.cleanup")

JOBS_TABLE = "jobs"
CLIPS_TABLE = "clips"

#: The only job statuses that may ever be purged.
TERMINAL_STATUSES = ("completed", "failed")

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class PurgeError(RuntimeError):
    """The purgeable-job list itself could not be fetched."""


@dataclass(frozen=True)
class PurgeResult:
    """Outcome of one job's purge attempt (never raises — CLI-friendly)."""

    job_id: str
    sources_deleted: int = 0
    clips_deleted: int = 0
    marked_purged: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _as_uuid(job_id: str) -> str:
    """Strict UUID gate — the whole deletion scope derives from this value."""
    value = str(job_id or "").lower()
    if not _UUID_RE.match(value):
        raise ValueError("cleanup requires a UUID job id")
    UUID(value)
    return value


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def _parse_iso(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class PurgeService:
    """Purges one job's artifacts when — and only when — the policy allows it."""

    def __init__(self, client: Client) -> None:
        self._client = client

    # -- discovery -----------------------------------------------------------------

    async def list_purgeable_jobs(
        self, *, limit: int = 10, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Terminal + expired + un-purged jobs, oldest deadline first."""
        now = now or _utcnow()
        try:
            result = await (
                self._client.table(JOBS_TABLE)
                .select("id,status,source_type,source_path,expires_at,purged_at")
                .in_("status", list(TERMINAL_STATUSES))
                .lt("expires_at", _iso(now))
                .is_("purged_at", None)
                .order("expires_at")
                .limit(max(1, limit))
                .execute()
            )
            return list(result.data or [])
        except Exception as exc:  # noqa: BLE001 — transport boundary
            logger.warning("purgeable job list failed: %s", exc.__class__.__name__)
            raise PurgeError("purgeable job list failed") from exc

    # -- per-job purge ----------------------------------------------------------------

    async def purge_job(self, job_id: str, *, now: datetime | None = None) -> PurgeResult:
        """Deletes one expired job's artifacts and marks it purged.

        Safe to call repeatedly: a job that is not (or no longer) a purge
        candidate is left completely untouched, and storage deletes are
        idempotent. Storage is ALWAYS deleted before the DB marker is set.
        """
        try:
            job_id = _as_uuid(job_id)
        except ValueError:
            return PurgeResult(str(job_id), error="invalid job id")
        now = now or _utcnow()

        sources_deleted = 0
        clips_deleted = 0
        try:
            # Re-verify the row server-side — the list may be stale, and this
            # fetch is the safety gate for every deletion below.
            candidate = await self._fetch_purge_candidate(job_id, now)
            if candidate is None:
                return PurgeResult(job_id, 0, 0, False, "not a purge candidate (terminal, expired, un-purged)")

            sources_deleted = await self._delete_source_objects(job_id)
            clips_deleted = await self._delete_clip_objects(job_id)
            marked = await self._mark_purged(job_id, now)
            return PurgeResult(job_id, sources_deleted, clips_deleted, marked)
        except Exception as exc:  # noqa: BLE001 — manual CLI boundary
            # Partial progress is reported as-is; the job stays un-purged,
            # so a re-run finishes it (already-deleted objects are no-ops).
            logger.warning("purge of job %s incomplete: %s", job_id, exc)
            return PurgeResult(
                job_id, sources_deleted, clips_deleted, False,
                f"{exc.__class__.__name__}: {exc}",
            )

    # -- steps -----------------------------------------------------------------------

    async def _fetch_purge_candidate(self, job_id: str, now: datetime) -> dict[str, Any] | None:
        result = await (
            self._client.table(JOBS_TABLE)
            .select("id,status,expires_at,purged_at")
            .eq("id", job_id)
            .execute()
        )
        rows = list(result.data or [])
        if not rows:
            return None
        row = rows[0]
        if row.get("status") not in TERMINAL_STATUSES:
            return None
        if row.get("purged_at") is not None:
            return None
        expires = _parse_iso(row.get("expires_at"))
        if expires is None or expires >= now:
            return None
        return row

    async def _delete_source_objects(self, job_id: str) -> int:
        """Deletes ``sources/{job}/original/*`` — nothing else, ever."""
        directory = f"sources/{job_id}/original"
        entries = await self._client.storage.from_(SOURCES_BUCKET).list(directory)
        paths: list[str] = []
        for entry in entries or []:
            name = str(entry.get("name") or "")
            full = f"{directory}/{name}"
            # Defense in depth: the path must match the server-generated
            # shape AND carry THIS job's id before it may be deleted.
            if not _SOURCE_PATH_PATTERN.match(full) or full.split("/")[1] != job_id:
                logger.warning("skipping unexpected source entry %r for job %s", name, job_id)
                continue
            paths.append(full)
        if not paths:
            return 0
        await self._client.storage.from_(SOURCES_BUCKET).remove(paths)
        return len(paths)

    async def _delete_clip_objects(self, job_id: str) -> int:
        """Deletes the job's clip objects — canonical, job-scoped paths only."""
        result = await (
            self._client.table(CLIPS_TABLE)
            .select("id,storage_path")
            .eq("job_id", job_id)
            .execute()
        )
        paths: list[str] = []
        for row in result.data or []:
            path = row.get("storage_path")
            if not path:
                continue
            if not _CLIP_PATH_PATTERN.match(str(path)):
                logger.warning("skipping clip %s: malformed storage_path %r", row.get("id"), path)
                continue
            if str(path).split("/")[1] != job_id:
                logger.warning("skipping clip %s: storage_path points at another job", row.get("id"))
                continue
            paths.append(str(path))
        if not paths:
            return 0
        await self._client.storage.from_(CLIPS_BUCKET).remove(paths)
        return len(paths)

    async def _mark_purged(self, job_id: str, now: datetime) -> bool:
        """Guarded update: terminal + still expired + still un-purged.

        Returns False when the guard did not match (concurrent purger won,
        or the deadline was just extended) — the objects are already gone
        either way, so this is not a failure.
        """
        result = await (
            self._client.table(JOBS_TABLE)
            .update({"purged_at": _iso(now)})
            .eq("id", job_id)
            .in_("status", list(TERMINAL_STATUSES))
            .lt("expires_at", _iso(now))
            .is_("purged_at", None)
            .execute()
        )
        return bool(result.data)


# -- CLI ---------------------------------------------------------------------------------


async def run_cleanup(*, limit: int = 10) -> int:
    """Runs one manual cleanup pass; returns the process exit code."""
    from .config.settings import get_settings

    settings = get_settings()
    if not settings.supabase_configured:
        print(
            "Supabase is not configured (set WORKER_SUPABASE_URL and "
            "WORKER_SUPABASE_SERVICE_ROLE_KEY). Nothing to do."
        )
        return 0

    from supabase import create_async_client

    client = create_async_client(settings.supabase_url, settings.supabase_service_role_key)
    service = PurgeService(client)

    try:
        jobs = await service.list_purgeable_jobs(limit=limit)
    except PurgeError as exc:
        print(f"error: {exc}")
        return 1

    if not jobs:
        print("No expired, un-purged jobs found. Nothing to do.")
        return 0

    print(f"Purging {len(jobs)} expired job(s) ...")
    exit_code = 0
    for row in jobs:
        result = await service.purge_job(str(row["id"]))
        if result.ok and result.marked_purged:
            print(
                f"  {result.job_id}: deleted {result.sources_deleted} source object(s) "
                f"and {result.clips_deleted} clip object(s); marked purged."
            )
        elif result.ok and not result.marked_purged:
            print(
                f"  {result.job_id}: deleted {result.sources_deleted} source object(s) "
                f"and {result.clips_deleted} clip object(s); marker already set (concurrent run)."
            )
        else:
            exit_code = 1
            print(
                f"  {result.job_id}: INCOMPLETE — sources={result.sources_deleted} "
                f"clips={result.clips_deleted} marked={result.marked_purged} "
                f"error={result.error}. Re-run to finish."
            )
    return exit_code


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="app.cleanup",
        description="Manually purges artifacts of EXPIRED terminal jobs "
        "(see the retention policy in the module docstring).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of jobs to purge in this run (default: 10).",
    )
    args = parser.parse_args(argv)
    raise SystemExit(asyncio.run(run_cleanup(limit=args.limit)))


if __name__ == "__main__":
    main()
