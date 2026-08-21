"""Plain text, forwarded material and media-refusal fallback paths."""

from dataclasses import dataclass

from octoforge_core.domain import MessageKind

from octoforge_telegram.client import SendMessage, TelegramClient
from octoforge_telegram.gateway import DialogSubmission, GatewayRegistry
from octoforge_telegram.models import TelegramMessage
from octoforge_telegram.poller_commands import PollerCommands, TextDispatch
from octoforge_telegram.poller_text import (
    MATERIAL_ATTRIBUTION_ANONYMOUS,
    MATERIAL_ATTRIBUTION_TEMPLATE,
    MATERIAL_PLACEHOLDER,
    TEXT_ONLY_NOTICE,
    VISION_PLAN_NOTICE,
)


@dataclass(frozen=True, slots=True)
class MaterialServices:
    client: TelegramClient
    registry: GatewayRegistry
    commands: PollerCommands


class MaterialDispatcher:
    def __init__(self, services: MaterialServices) -> None:
        self._client = services.client
        self._registry = services.registry
        self._commands = services.commands

    async def plain(self, message: TelegramMessage, user_id: str, chat_id: int) -> None:
        if message.body is None:
            await self._client.send_message(SendMessage(chat_id, TEXT_ONLY_NOTICE))
            return
        await self._commands.dispatch(
            TextDispatch(
                message.message_id,
                user_id,
                chat_id,
                message.body,
                message.reply_to_message.message_id if message.reply_to_message else None,
            )
        )

    async def material(self, message: TelegramMessage, user_id: str, chat_id: int) -> None:
        origin = message.forward_origin.display_name if message.forward_origin else ""
        attribution = (
            MATERIAL_ATTRIBUTION_TEMPLATE.format(origin=origin)
            if origin
            else MATERIAL_ATTRIBUTION_ANONYMOUS
        )
        bridge = await self._registry.gateway_for(user_id, chat_id)
        await bridge.handle_text(
            DialogSubmission(
                f"{attribution} {message.body or MATERIAL_PLACEHOLDER}",
                client_message_id=str(message.message_id),
                kind=MessageKind.MATERIAL,
                origin=origin or None,
            )
        )

    async def refuse_vision(
        self,
        message: TelegramMessage,
        user_id: str,
        chat_id: int,
    ) -> None:
        await self._client.send_message(SendMessage(chat_id, VISION_PLAN_NOTICE))
        if message.forward_origin is not None:
            await self.material(message, user_id, chat_id)
        elif message.body is not None:
            await self.plain(message, user_id, chat_id)

    async def without_vision(
        self,
        message: TelegramMessage,
        user_id: str,
        chat_id: int,
    ) -> None:
        if message.forward_origin is not None:
            await self.material(message, user_id, chat_id)
        else:
            await self.plain(message, user_id, chat_id)
