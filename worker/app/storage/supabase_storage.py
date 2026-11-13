"""Private Supabase Storage access for the worker (Phase 3).

The worker holds the service-role key server-side and downloads source
objects from the PRIVATE ``clipforge-sources`` bucket into ephemeral local
storage. Local disk is a processing scratchpad only — Supabase Storage
remains the durable source of truth, and temp files are always cleaned up.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from supabase import Client

logger = logging.getLogger("clipforge.storage")

SOURCES_BUCKET = "clipforge-sources"

#: Exactly the path shape the API generates:
#:   sources/{job_id:uuid}/original/{generated_filename}
_SOURCE_PATH_PATTERN = re.compile(r"^sources/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/original/[^/]+$")


class StorageDownloadError(RuntimeError):
    """Download failed (network, missing object, bad path, size cap)."""


class SupabaseStorage:
    def __init__(self, client: Client, max_download_bytes: int) -> None:
        self._client = client
        self._max_bytes = max_download_bytes

    async def download_to(self, object_path: str, destination: Path) -> int:
        """Streams ``object_path`` to ``destination``; returns bytes written.

        Raises :class:`StorageDownloadError` on invalid path shape, transport
        failure, missing object, or when the payload exceeds the configured
        size cap (the transfer is aborted and the partial file removed).
        """
        if not _SOURCE_PATH_PATTERN.match(object_path or ""):
            logger.warning("refusing download: object path does not match the server-generated shape")
            raise StorageDownloadError("invalid object path")

        destination.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            response = await self._client.storage.from_(SOURCES_BUCKET).download(object_path)
            with destination.open("wb") as handle:
                try:
                    async for chunk in response.aiter_bytes():  # type: ignore[attr-defined]
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > self._max_bytes:
                            raise StorageDownloadError(
                                f"download exceeds maximum size ({self._max_bytes} bytes)"
                            )
                        handle.write(chunk)
                except AttributeError:
                    # Fallback for non-streaming responses.
                    content = await response.aread()  # type: ignore[attr-defined]
                    if len(content) > self._max_bytes:
                        raise StorageDownloadError(
                            f"download exceeds maximum size ({self._max_bytes} bytes)"
                        ) from None
                    handle.write(content)
                    written = len(content)
            return written
        except StorageDownloadError:
            destination.unlink(missing_ok=True)
            raise
        except Exception as exc:  # noqa: BLE001 — transport boundary
            destination.unlink(missing_ok=True)
            logger.warning(
                "storage download failed (job dir %s): %s",
                object_path.split("/")[1] if "/" in object_path else "unknown",
                exc.__class__.__name__,
            )
            raise StorageDownloadError("source download failed") from exc
