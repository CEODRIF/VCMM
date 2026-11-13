"""Rendering abstraction.

Phase 1 fixes the output spec (vertical 9:16, 1080x1920) and the renderer
contract. Phase 5 implements the FFmpeg foundation: cut + reframe to the
fixed vertical profile (crop/pad, rotation-corrected, no distortion) with
output validation. Animated subtitle burn-in arrives later (Phase 5b) and is
NOT part of this foundation — ``spec.captions`` is carried for the future
but ignored by the current renderer.

The contract is ASYNC: rendering shells out to FFmpeg subprocesses that are
managed (timeout/cancel/cleanup) inside the event loop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class RenderSpec:
    """Fixed product output specification."""

    width: int = 1080
    height: int = 1920
    aspect_ratio: str = "9:16"
    fps: int = 30
    captions: bool = True


class BaseRenderer(ABC):
    """Interface for clip rendering backends."""

    @abstractmethod
    async def render(
        self,
        source_path: str,
        start: float,
        end: float,
        spec: RenderSpec,
        output_path: str,
    ) -> str:
        """Cut and re-render a vertical clip; return the output path.

        Raises a rendering error (with a safe category/message) when the
        clip cannot be produced. The output file is removed on failure.
        """
        raise NotImplementedError
