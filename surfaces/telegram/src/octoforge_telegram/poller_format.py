"""Narrative text composed from understood Telegram media."""

from collections.abc import Sequence
from dataclasses import dataclass

from octoforge_core.media.api import MediaResult

VOICE_TAG = "[голосовое]"
IMAGE_TAG = "[изображение]"
IMAGE_TAG_NUMBERED = "[изображение {index}/{total}]"
IMAGE_FAILED_PLACEHOLDER = "(не удалось распознать это изображение)"
CAPTION_LABEL = "Подпись:"
MATERIAL_ATTRIBUTION_TEMPLATE = "[переслано от {origin}]"
MATERIAL_ATTRIBUTION_ANONYMOUS = "[переслано]"


@dataclass(frozen=True, slots=True)
class MediaNarrative:
    caption: str = ""
    origin: str = ""
    forwarded: bool = False


def compose_voice(transcript: str, context: MediaNarrative) -> str:
    text = f"{VOICE_TAG} {transcript}"
    text = _attribute(text, context)
    if context.caption:
        text = f"{text}\n\n{CAPTION_LABEL} {context.caption}"
    return text


def compose_images(
    descriptions: Sequence[MediaResult],
    context: MediaNarrative,
) -> str:
    total = len(descriptions)
    blocks = [
        f"{IMAGE_TAG if total == 1 else IMAGE_TAG_NUMBERED.format(index=index, total=total)} "
        f"{described.text if described.ok else IMAGE_FAILED_PLACEHOLDER}"
        for index, described in enumerate(descriptions, start=1)
    ]
    text = _attribute("\n\n".join(blocks), context)
    if context.caption:
        text = f"{text}\n\n{CAPTION_LABEL} {context.caption}"
    return text


def _attribute(text: str, context: MediaNarrative) -> str:
    if not context.forwarded:
        return text
    attribution = (
        MATERIAL_ATTRIBUTION_TEMPLATE.format(origin=context.origin)
        if context.origin
        else MATERIAL_ATTRIBUTION_ANONYMOUS
    )
    return f"{attribution} {text}"
