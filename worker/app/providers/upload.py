"""Uploaded-file adapter (TASK 007) — the fully implemented provider.

The user-authorized file already exists in the PRIVATE ``clipforge-sources``
bucket (Phase 3 ingestion: signed upload URL, server-generated path,
verified object). Validation = local FFprobe of the downloaded file;
materialization = the existing bounded storage download. No network
provider calls, no authorization gaps.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..queue.models import ERROR_STORAGE_DOWNLOAD
from ..validation.media import MediaValidationError, probe_media
from .base import STATUS_AVAILABLE, ProviderCheck


class UploadProvider:
    name = "upload"
    supports_validation = True
    supports_materialization = True  # the file IS the source (private bucket)

    def __init__(self, ffprobe_path: str, probe_timeout_seconds: float = 60.0) -> None:
        self._ffprobe = ffprobe_path
        self._probe_timeout = probe_timeout_seconds

    def can_handle(self, url: str) -> bool:
        # Uploads are addressed by their (server-generated) object path,
        # not a public URL.
        return bool(url) and "://" not in url and url.startswith("sources/")

    async def validate(self, url: str) -> ProviderCheck:
        # A bare object path has no remote availability to check — the
        # ingestion stage already verified the object exists. The probe of
        # the downloaded file happens in materialize().
        return ProviderCheck(STATUS_AVAILABLE, self.name, title=None)

    async def materialize(self, url: str, destination_dir: Path, **kwargs: Any) -> Path:
        """Downloads the verified object (bounded, existing Phase 3 path)
        and probes it. ``kwargs['download']`` is an async callable
        ``(object_path, destination) -> None`` (the SupabaseStorage
        download) so this adapter stays transport-agnostic and testable."""
        download = kwargs.get("download")
        if download is None:
            raise ValueError("materialize requires the 'download' callable (private-bucket download)")
        destination_dir = Path(destination_dir)
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / "source.bin"
        await download(url, destination)
        if not destination.is_file() or destination.stat().st_size == 0:
            destination.unlink(missing_ok=True)
            raise MediaValidationError(
                ERROR_STORAGE_DOWNLOAD, "The stored file could not be downloaded."
            )
        await probe_media(self._ffprobe, destination, self._probe_timeout)
        return destination
