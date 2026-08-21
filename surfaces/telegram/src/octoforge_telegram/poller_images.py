"""Describe Telegram images and submit one combined dialog message."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

from octoforge_core.domain import Attachment, AttachmentKind, MessageKind
from octoforge_core.media.api import MediaResult, MediaUnderstanding, none_understood

from octoforge_telegram.gateway import DialogSubmission, GatewayRegistry
from octoforge_telegram.images import REF_PREFIX
from octoforge_telegram.models import TelegramImageRef, TelegramMessage
from octoforge_telegram.poller_activity import IngestionActivity
from octoforge_telegram.poller_format import MediaNarrative, compose_images


@dataclass(frozen=True, slots=True)
class ImageSubmission:
    anchor: TelegramMessage
    images: Sequence[TelegramImageRef]
    user_id: str
    chat_id: int
    kind: MessageKind
    caption: str = ""


@dataclass(frozen=True, slots=True)
class ImageServices:
    registry: GatewayRegistry
    media: MediaUnderstanding
    slots: asyncio.Semaphore
    activity: IngestionActivity


class ImageSubmitter:
    def __init__(self, services: ImageServices) -> None:
        self._registry = services.registry
        self._media = services.media
        self._slots = services.slots
        self._activity = services.activity

    async def submit(self, request: ImageSubmission) -> tuple[MediaResult, ...]:
        refs = tuple(f"{REF_PREFIX}{image.file_id}" for image in request.images)
        async with self._slots, self._activity.showing(request.chat_id, request.kind):
            results = await self._media.describe(request.user_id, refs)
        if none_understood(results):
            return results
        forwarded = request.anchor.forward_origin is not None
        origin = (
            request.anchor.forward_origin.display_name
            if request.anchor.forward_origin is not None
            else ""
        )
        bridge = await self._registry.gateway_for(request.user_id, request.chat_id)
        await bridge.handle_text(
            DialogSubmission(
                compose_images(
                    results,
                    MediaNarrative(request.caption or request.anchor.body or "", origin, forwarded),
                ),
                client_message_id=str(request.anchor.message_id),
                kind=request.kind,
                origin=(origin or None) if forwarded else None,
                attachments=tuple(Attachment(AttachmentKind.IMAGE, ref) for ref in refs),
            )
        )
        return results
