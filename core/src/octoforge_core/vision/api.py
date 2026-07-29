"""Public boundary of the vision module: looking at images the user sent.

The main model is text-only (the provider answers "this model does not
support image input"), so images are understood by a SEPARATE model behind
this port and enter the dialog as text. Two tiers, because the price gap is
measured, not assumed: a cheap model describes every incoming image at
ingestion (~4-8s), and a stronger one is asked only when the user has a
specific question about a picture (~15-30s, four times the tokens).
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ImageData:
    """Raw image bytes with the media type the provider needs for the data URL."""

    content: bytes
    media_type: str = "image/jpeg"


class VisionClient(Protocol):
    """Describes images; one call, one answer."""

    async def look(self, images: tuple[ImageData, ...], prompt: str) -> str:
        """Answer `prompt` about the images (OCR included when asked for)."""
        ...


class ImageResolver(Protocol):
    """Turns an attachment reference back into bytes (transport-owned)."""

    async def fetch(self, ref: str) -> ImageData:
        """Download the referenced image; raises when the ref is unknown."""
        ...


class VisionUnavailableError(Exception):
    """Vision is not configured (no model) or the reference cannot be resolved."""
