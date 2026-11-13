"""Supabase-backed job queue (Phase 3).

All queue-control operations go through the restricted, security-definer RPCs
created in migration 20261115000000 (``claim_next_job``, ``renew_job_lease``,
``update_job_progress``, ``complete_job_ingestion``, ``fail_job``,
``release_job``) and extended in Phase 5 (``complete_job_rendering``, the
only terminal-completion path during rendering). Those RPCs are
revocable-from-PUBLIC and granted only to ``service_role``; every update is
scoped to the claimed job and lease inside the database, so two workers can
never own the same active job.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional

from supabase import Client

from ..observability import context as log_context
from ..observability import metrics as m


class QueueError(RuntimeError):
    """Raised when a queue RPC fails at the transport/HTTP level."""


class SupabaseQueue:
    def __init__(self, client: Client) -> None:
        self._client = client

    async def ping(self) -> bool:
        """Readiness probe (TASK 012): one bounded metadata read. True when
        the database (and therefore the queue RPCs) is reachable. Never
        raises — a probe failure is an 'error' status, not a crash."""
        try:
            rows = await self._client.table("jobs").select("id", count="exact").limit(1).execute()
            return rows is not None
        except Exception:  # noqa: BLE001 — probe boundary
            return False

    async def aclose(self) -> None:
        """TASK 012: release the queue's HTTP client on shutdown.
        supabase-py's AsyncClient has no top-level close; the underlying
        postgrest client does. Best-effort — never raises."""
        try:
            await self._client.postgrest.aclose()
        except Exception:  # noqa: BLE001
            pass

    async def claim_next_job(self, worker_id: str, lease_seconds: int) -> Optional[dict[str, Any]]:
        """Atomically claim the oldest claimable job (or None).

        Claimable = queued, or validating with an expired lease (crash
        recovery of a job abandoned by a dead worker).
        """
        job = await self._rpc("claim_next_job", {"p_worker_id": worker_id, "p_lease_seconds": lease_seconds})
        if job:
            stage = m.stage_for_status(str(job.get("status") or ""))
            m.jobs_claimed_total.inc({"stage": stage})
            # First claim: the job's queue-wait time (created_at → now).
            # Resume/recovery claims (attempt > 1) are NOT "queue wait".
            if int(job.get("attempt_count") or 0) == 1:
                wait = self._queue_wait_seconds(job.get("created_at"))
                if wait is not None:
                    m.observe_queue_wait(stage, wait)
        return job

    @staticmethod
    def _queue_wait_seconds(created_at: Any) -> float | None:
        try:
            parsed = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
        except (ValueError, TypeError):
            return None

    async def renew_job_lease(self, job_id: str, worker_id: str, lease_seconds: int) -> bool:
        renewed = bool(await self._rpc("renew_job_lease", {"p_job_id": job_id, "p_worker_id": worker_id, "p_lease_seconds": lease_seconds}))
        if not renewed:
            # A rejected renewal means we lost ownership — the most
            # operationally significant event in the queue.
            m.lease_renewals_failed_total.inc()
        return renewed

    async def update_job_progress(self, job_id: str, worker_id: str, progress: int) -> bool:
        return bool(await self._rpc("update_job_progress", {"p_job_id": job_id, "p_worker_id": worker_id, "p_progress": progress}))

    async def complete_job_ingestion(self, job_id: str, worker_id: str, source_metadata: dict[str, Any]) -> bool:
        completed = bool(
            await self._rpc(
                "complete_job_ingestion",
                {"p_job_id": job_id, "p_worker_id": worker_id, "p_source_metadata": source_metadata},
            )
        )
        if completed:
            m.jobs_completed_total.inc({"stage": m.stage_for_status(log_context.stage_var.get() or "validating")})
        return completed

    async def fail_job(
        self,
        job_id: str,
        worker_id: str,
        error_category: str,
        error_message: str,
        permanent: bool = False,
    ) -> str:
        """Returns the new status ('queued' | 'failed') or 'not_owner'."""
        result = await self._rpc(
            "fail_job",
            {
                "p_job_id": job_id,
                "p_worker_id": worker_id,
                "p_error_category": error_category,
                "p_error_message": error_message,
                "p_permanent": permanent,
            },
        )
        new_status = str(result) if result is not None else "not_owner"
        if new_status != "not_owner":
            # A real failure write (re-queue or terminal). Stage comes from
            # the bound job context; the category is bounded to the known
            # set (anything else → 'other').
            m.jobs_failed_total.inc({
                "stage": m.stage_for_status(log_context.stage_var.get() or ""),
                "error_category": m._error_category(error_category),
            })
        return new_status

    async def release_job(self, job_id: str, worker_id: str) -> bool:
        released = bool(await self._rpc("release_job", {"p_job_id": job_id, "p_worker_id": worker_id}))
        if released:
            m.jobs_released_total.inc({"stage": m.stage_for_status(log_context.stage_var.get() or "")})
        return released

    async def complete_job_rendering(self, job_id: str, worker_id: str) -> bool:
        """Phase 5 terminal completion — the ONLY path to ``completed``.

        The DB refuses (returns False) unless the caller owns the job, it is
        in ``rendering``, and EVERY clip is ``ready`` WITH a ``storage_path``
        — a stale or buggy worker can never mark a job completed early.
        """
        completed = bool(await self._rpc("complete_job_rendering", {"p_job_id": job_id, "p_worker_id": worker_id}))
        if completed:
            m.jobs_completed_total.inc({"stage": "rendering"})
        return completed

    async def requeue_incomplete_analysis(
        self, job_id: str, worker_id: str, max_resumes: int
    ) -> str:
        """Phase 7b: return an owned INCOMPLETE analysis job to ``ready``.

        The job's per-claim chunk budget was exhausted (it is making progress,
        not failing): it is re-queued to resume from its checkpoints on the
        next claim. This does NOT consume the failure ``attempt_count`` budget
        — a long recording legitimately needs many resume claims.

        Returns ``'ready'`` (re-queued), ``'resume_exhausted'`` (the resume cap
        was reached — the caller must record a failure; NO terminal write was
        made here), or ``'not_owner'`` (ownership lost — the caller aborts).
        """
        result = await self._rpc(
            "requeue_incomplete_analysis",
            {
                "p_job_id": job_id,
                "p_worker_id": worker_id,
                "p_max_resumes": max_resumes,
            },
        )
        return str(result) if result is not None else "not_owner"

    # -- Phase 4: guarded direct updates (no RPC needed) -----------------------
    # These use the service-role REST client directly. Ownership is enforced
    # by scoping EVERY update to (id, status, locked_by = caller), and the
    # database transition trigger + lock-consistency constraint re-validate
    # the result, so a stale owner can never move the job.

    async def transition_stage(
        self, job_id: str, worker_id: str, from_status: str, to_status: str
    ) -> str:
        """Moves a job ``from_status`` → ``to_status`` if we still own it.

        Returns ``'transitioned'`` (we did it), ``'already'`` (crash
        recovery: the job was already in ``to_status`` and owned by us), or
        ``'not_owner'`` (someone else owns/moved it — the caller must abort
        without further writes).
        """
        rows = await self._table_update(
            "jobs",
            {"status": to_status},
            {"id": job_id, "status": from_status, "locked_by": worker_id},
            select="id,status,locked_by",
        )
        if rows:
            return "transitioned"
        current = await self._table_select(
            "jobs", {"id": job_id}, select="id,status,locked_by"
        )
        if (
            current is not None
            and current.get("status") == to_status
            and current.get("locked_by") == worker_id
        ):
            return "already"
        return "not_owner"

    async def park_after_analysis(self, job_id: str, worker_id: str) -> bool:
        """Phase 4 end state: stay in ``analyzing`` with the lock CLEARED.

        A parked job is no longer claimable (claiming requires an EXPIRED
        lease on active stages) — it waits for the Phase 5 renderer.
        Returns False when we no longer own the job (caller aborts).
        """
        rows = await self._table_update(
            "jobs",
            {"locked_by": None, "locked_at": None, "lease_expires_at": None, "progress": 100},
            {"id": job_id, "status": "analyzing", "locked_by": worker_id},
            select="id",
        )
        return bool(rows)

    async def set_job_source(self, job_id: str, worker_id: str, source_path: str) -> bool:
        """TASK 011: pin a materialized source object on an owned job.

        The path is written ONLY if the job is still in ``validating`` and
        still locked by ``worker_id`` — a stale worker can never attach a
        source to a job someone else now owns. Returns False when the
        guarded update matched no row (ownership lost; the caller must stop
        and clean up its temporary object).
        """
        rows = await self._table_update(
            "jobs",
            {"source_path": source_path},
            {"id": job_id, "status": "validating", "locked_by": worker_id},
            select="id",
        )
        return bool(rows)

    async def _table_update(self, table: str, values: dict[str, Any], where: dict[str, Any], select: str) -> list[dict[str, Any]]:
        try:
            builder = self._client.table(table).update(values)
            for column, value in where.items():
                builder = builder.eq(column, value)
            return list(await builder.select(select))
        except Exception as exc:  # noqa: BLE001 — transport boundary
            raise QueueError(f"queue update '{table}' failed: {exc.__class__.__name__}") from exc

    async def _table_select(self, table: str, where: dict[str, Any], select: str) -> dict[str, Any] | None:
        try:
            builder = self._client.table(table).select(select)
            for column, value in where.items():
                builder = builder.eq(column, value)
            rows = list(await builder.limit(1))
            return rows[0] if rows else None
        except Exception as exc:  # noqa: BLE001 — transport boundary
            raise QueueError(f"queue select '{table}' failed: {exc.__class__.__name__}") from exc

    async def _rpc(self, name: str, params: dict[str, Any]) -> Any:
        try:
            return await self._client.rpc(name, params)
        except Exception as exc:  # noqa: BLE001 — transport boundary
            # Never log credentials/params; the RPC name is safe.
            raise QueueError(f"queue rpc '{name}' failed: {exc.__class__.__name__}") from exc
