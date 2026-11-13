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

    async def _rpc(self, name: str, params: dict[str, Any]) -> Any:
        try:
            return await self._client.rpc(name, params)
        except Exception as exc:  # noqa: BLE001 — transport boundary
            # Never log credentials/params; the RPC name is safe.
            raise QueueError(f"queue rpc '{name}' failed: {exc.__class__.__name__}") from exc
