"""Supabase-backed job queue (Phase 3).

All queue-control operations go through the restricted, security-definer RPCs
created in migration 20261115000000 (``claim_next_job``, ``renew_job_lease``,
``update_job_progress``, ``complete_job_ingestion``, ``fail_job``,
``release_job``). Those RPCs are revocable-from-PUBLIC and granted only to
``service_role``; every update is scoped to the claimed job and lease inside
the database, so two workers can never own the same active job.
"""

from __future__ import annotations

from typing import Any, Optional

from supabase import Client


class QueueError(RuntimeError):
    """Raised when a queue RPC fails at the transport/HTTP level."""


class SupabaseQueue:
    def __init__(self, client: Client) -> None:
        self._client = client

    async def claim_next_job(self, worker_id: str, lease_seconds: int) -> Optional[dict[str, Any]]:
        """Atomically claim the oldest claimable job (or None).

        Claimable = queued, or validating with an expired lease (crash
        recovery of a job abandoned by a dead worker).
        """
        return await self._rpc("claim_next_job", {"p_worker_id": worker_id, "p_lease_seconds": lease_seconds})

    async def renew_job_lease(self, job_id: str, worker_id: str, lease_seconds: int) -> bool:
        return bool(await self._rpc("renew_job_lease", {"p_job_id": job_id, "p_worker_id": worker_id, "p_lease_seconds": lease_seconds}))

    async def update_job_progress(self, job_id: str, worker_id: str, progress: int) -> bool:
        return bool(await self._rpc("update_job_progress", {"p_job_id": job_id, "p_worker_id": worker_id, "p_progress": progress}))

    async def complete_job_ingestion(self, job_id: str, worker_id: str, source_metadata: dict[str, Any]) -> bool:
        return bool(
            await self._rpc(
                "complete_job_ingestion",
                {"p_job_id": job_id, "p_worker_id": worker_id, "p_source_metadata": source_metadata},
            )
        )

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
        return str(result) if result is not None else "not_owner"

    async def release_job(self, job_id: str, worker_id: str) -> bool:
        return bool(await self._rpc("release_job", {"p_job_id": job_id, "p_worker_id": worker_id}))

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
