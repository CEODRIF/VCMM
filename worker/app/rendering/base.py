"""Rendering abstraction.

Phase 1 fixes the output spec (vertical 9:16, 1080x1920) and the renderer
contract. Phase 5A implements the FFmpeg foundation: cut + reframe to the
fixed vertical profile (crop/pad, rotation-corrected, no distortion) with
output validation. Phase 5B adds animated, word-synchronized subtitle
burn-in in the SAME pipeline: ``spec.captions`` enables it and
``spec.caption_words`` / ``spec.caption_style`` carry the per-clip content
(clip-relative real transcription words) and the visual style. With
``caption_words`` empty the render is byte-for-byte the Phase 5A command
line.

The contract is ASYNC: rendering shells out to FFmpeg subprocesses that are
managed (timeout/cancel/cleanup) inside the event loop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CaptionWord:
    """One real transcribed word, in CLIP-RELATIVE seconds."""

    word: str
    start: float
    end: float


@dataclass(frozen=True)
class CaptionStyle:
    """Visual style for burned-in captions (deterministic defaults).

    Colors are ``RRGGBB`` hex. ``base_color`` is what a word SETTLES into as
    it is spoken (ASS PrimaryColour); ``highlight_color`` is what upcoming
    words glow in (ASS SecondaryColour) — the verified libass ``\\k``
    progression (see app/rendering/captions.py).
    """

    font_family: str = "DejaVu Sans"
    font_size: int = 88
    base_color: str = "FFFFFF"
    highlight_color: str = "00D7FF"
    outline_color: str = "000000"
    outline_width: int = 6
    margin_v: int = 520
    words_per_line: int = 3
    uppercase: bool = True


@dataclass(frozen=True)
class RenderSpec:
    """Fixed product output specification."""

    width: int = 1080
    height: int = 1920
    aspect_ratio: str = "9:16"
    fps: int = 30
    #: Burn animated captions when words are present (job-level
    #: ``jobs.captions_enabled``, Phase 2 column).
    captions: bool = True
    #: Clip-relative caption words (Phase 5B); empty = Phase 5A render.
    caption_words: tuple[CaptionWord, ...] = field(default_factory=tuple)
    #: Caption style; None = CaptionStyle() defaults.
    caption_style: CaptionStyle | None = None


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
