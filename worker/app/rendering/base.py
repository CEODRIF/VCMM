"""Rendering abstraction.

Phase 1 fixes the output spec (vertical 9:16, 1080x1920) and the renderer
contract. The FFmpeg implementation (crop/reframe + animated subtitle burn-in)
arrives in Phase 5. No video is rendered in this phase.
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
    def render(
        self,
        source_path: str,
        start: float,
        end: float,
        spec: RenderSpec,
        output_path: str,
    ) -> str:
        """Cut and re-render a vertical clip; return the output path."""
        raise NotImplementedError
