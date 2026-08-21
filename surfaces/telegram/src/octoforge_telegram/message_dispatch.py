"""Route one gated Telegram message or album into commands, media or material."""

import logging
from dataclasses import dataclass

from octoforge_core.domain import MessageKind
from octoforge_core.media.api import none_understood, refused_by_plan

from octoforge_telegram.client import SendMessage, TelegramClient
from octoforge_telegram.gateway import GatewayRegistry
from octoforge_telegram.models import TelegramChatType, TelegramMessage
from octoforge_telegram.poller_commands import PollerCommands
from octoforge_telegram.poller_images import ImageSubmission, ImageSubmitter
from octoforge_telegram.poller_material import MaterialDispatcher
from octoforge_telegram.poller_text import GROUP_NOTICE
from octoforge_telegram.poller_voice import VoiceSubmission
from octoforge_telegram.poller_voice_outcome import VoiceOutcomeHandler

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DispatchServices:
    client: TelegramClient
    registry: GatewayRegistry
    commands: PollerCommands
    material: MaterialDispatcher
    images: ImageSubmitter | None
    voice: VoiceOutcomeHandler | None


class MessageDispatcher:
    def __init__(self, services: DispatchServices) -> None:
        self._client = services.client
        self._registry = services.registry
        self._commands = services.commands
        self._material = services.material
        self._images = services.images
        self._voice = services.voice

    async def dispatch(self, batch: list[TelegramMessage], user_id: str) -> None:
        message = batch[0]
        chat_id = message.chat.id
        if message.chat.type is not TelegramChatType.PRIVATE:
            await self._client.send_message(SendMessage(chat_id, GROUP_NOTICE))
        elif len(batch) > 1 or message.media_group_id is not None:
            await self._album(batch, user_id, chat_id)
        elif message.forward_origin is not None:
            await self._forward(message, user_id, chat_id)
        elif message.best_image is not None and self._images is not None:
            await self._own_image(message, user_id, chat_id)
        elif message.best_audio is not None and self._voice is not None:
            await self._voice_message(
                VoiceSubmission(message, user_id, chat_id, message.best_audio, MessageKind.OWN)
            )
        else:
            await self._material.plain(message, user_id, chat_id)

    async def _forward(self, message: TelegramMessage, user_id: str, chat_id: int) -> None:
        if message.best_audio is not None and self._voice is not None:
            await self._voice_message(
                VoiceSubmission(message, user_id, chat_id, message.best_audio, MessageKind.MATERIAL)
            )
            return
        if message.best_image is not None and self._images is not None:
            results = await self._images.submit(
                ImageSubmission(
                    message,
                    (message.best_image,),
                    user_id,
                    chat_id,
                    MessageKind.MATERIAL,
                )
            )
            if refused_by_plan(results):
                await self._material.refuse_vision(message, user_id, chat_id)
            elif none_understood(results):
                await self._material.material(message, user_id, chat_id)
            return
        await self._material.material(message, user_id, chat_id)

    async def _own_image(self, message: TelegramMessage, user_id: str, chat_id: int) -> None:
        assert self._images is not None and message.best_image is not None
        kind = MessageKind.OWN if message.body else MessageKind.MATERIAL
        results = await self._images.submit(
            ImageSubmission(message, (message.best_image,), user_id, chat_id, kind)
        )
        if refused_by_plan(results):
            await self._material.refuse_vision(message, user_id, chat_id)
        elif none_understood(results):
            await self._material.plain(message, user_id, chat_id)

    async def _album(self, batch: list[TelegramMessage], user_id: str, chat_id: int) -> None:
        anchor = batch[0]
        images = tuple(image for image in (item.best_image for item in batch) if image is not None)
        caption = next((item.body for item in batch if item.body), "")
        if self._images is None or not images:
            await self._material.without_vision(anchor, user_id, chat_id)
            return
        kind = MessageKind.MATERIAL if anchor.forward_origin or not caption else MessageKind.OWN
        results = await self._images.submit(
            ImageSubmission(anchor, images, user_id, chat_id, kind, caption)
        )
        if refused_by_plan(results):
            await self._material.refuse_vision(anchor, user_id, chat_id)
        elif none_understood(results):
            await self._material.without_vision(anchor, user_id, chat_id)

    async def _voice_message(self, request: VoiceSubmission) -> None:
        assert self._voice is not None
        await self._voice.submit(request)
