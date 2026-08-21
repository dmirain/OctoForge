"""On-demand reinspection of the latest dialog images."""

import asyncio
import logging
from typing import TYPE_CHECKING

from octoforge_core.domain import Attachment, AttachmentKind
from octoforge_core.vision.api import VisionUnavailableError

from .runner_constants import MAX_LOOK_IMAGES

if TYPE_CHECKING:
    from .runner import ConversationRunner

logger = logging.getLogger(__name__)


class DialogVision:
    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    @property
    def available(self) -> bool:
        config = self._runner._config
        return config.vision is not None and config.image_resolver is not None

    async def look(self, question: str) -> str:
        config = self._runner._config
        if config.vision is None or config.image_resolver is None:
            raise VisionUnavailableError("vision is not configured")
        attachments = self.latest_images()
        if not attachments:
            raise VisionUnavailableError("no image in this dialog")
        logger.info(
            "looking at images again: dialog=%s refs=%s",
            self._runner.dialog_id,
            [item.ref for item in attachments],
        )
        images = tuple(
            await asyncio.gather(
                *(config.image_resolver.fetch(attachment.ref) for attachment in attachments)
            )
        )
        return await config.vision.look(images, question)

    def latest_images(self) -> tuple[Attachment, ...]:
        for message in reversed(self._runner._runtime.narrative):
            images = tuple(
                item for item in message.attachments if item.kind is AttachmentKind.IMAGE
            )
            if images:
                return images[:MAX_LOOK_IMAGES]
        return ()
