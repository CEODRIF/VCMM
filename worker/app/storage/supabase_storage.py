"""Private Supabase Storage access for the worker (Phase 3 + Phase 5).

The worker holds the service-role key server-side and:

- downloads source objects from the PRIVATE ``clipforge-sources`` bucket
  (Phase 3) into ephemeral local storage,
- uploads rendered clips to the PRIVATE ``clipforge-clips`` bucket (Phase 5)
  under the fixed convention ``clips/{job_id}/{clip_id}.mp4``.

Local disk is a processing scratchpad only — Supabase Storage remains the
durable source of truth, and temp files are always cleaned up. Every
object path must match the server-generated shape (UUIDs, no traversal)
before any transfer, and transfers are size-capped.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from supabase import Client

logger = logging.getLogger("clipforge.storage")

SOURCES_BUCKET = "clipforge-sources"
CLIPS_BUCKET = "clipforge-clips"

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

#: Exactly the path shape the API generates:
#:   sources/{job_id:uuid}/original/{generated_filename}
_SOURCE_PATH_PATTERN = re.compile(rf"^sources/{_UUID}/original/[^/]+$")

#: Exactly the path shape the renderer generates (Phase 5):
#:   clips/{job_id:uuid}/{clip_id:uuid}.mp4
_CLIP_PATH_PATTERN = re.compile(rf"^clips/{_UUID}/{_UUID}\.mp4$")


class StorageDownloadError(RuntimeError):
    """Download failed (network, missing object, bad path, size cap)."""


class StorageUploadError(RuntimeError):
    """Upload failed (bad path shape, missing/oversized file, transport)."""


class SupabaseStorage:
    def __init__(self, client: Client, max_download_bytes: int, max_clip_bytes: int | None = None) -> None:
        self._client = client
        self._max_bytes = max_download_bytes
        self._max_clip_bytes = max_clip_bytes or max_download_bytes

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

    async def upload_from(self, object_path: str, source_file: Path, max_bytes: int | None = None) -> int:
        """Uploads a rendered clip from ``source_file``; returns bytes sent.

        Raises :class:`StorageUploadError` on invalid path shape, a missing/
        empty file, a file over the size cap, or a transport failure. The
        upload is an UPSERT, so a re-render of the same clip after a crash
        is idempotent (the object is replaced, never duplicated).
        """
        if not _CLIP_PATH_PATTERN.match(object_path or ""):
            logger.warning("refusing upload: object path does not match the renderer-generated shape")
            raise StorageUploadError("invalid object path")

        source = Path(source_file)
        if not source.is_file():
            raise StorageUploadError("rendered file is missing")
        size = source.stat().st_size
        limit = max_bytes if max_bytes is not None else self._max_clip_bytes
        if size == 0:
            raise StorageUploadError("rendered file is empty")
        if size > limit:
            raise StorageUploadError(f"rendered file exceeds maximum size ({limit} bytes)")

        content = source.read_bytes()
        try:
            await self._client.storage.from_(CLIPS_BUCKET).upload(
                object_path,
                content,
                {"content-type": "video/mp4", "upsert": True},
            )
        except Exception as exc:  # noqa: BLE001 — transport boundary
            logger.warning("clip upload failed for %s: %s", object_path, exc.__class__.__name__)
            raise StorageUploadError("clip upload failed") from exc
        return size
