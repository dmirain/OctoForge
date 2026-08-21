"""Surface commands and plain-text submission into a Telegram dialog gateway."""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from octoforge_telegram.client import USER_ID_PREFIX, SendMessage, TelegramClient
from octoforge_telegram.gateway import DialogSubmission, GatewayRegistry
from octoforge_telegram.invites.api import ReferralStore
from octoforge_telegram.membership import COMMAND_START, REFERRAL_PREFIX, start_code
from octoforge_telegram.poller_text import (
    COMMAND_INVITE,
    COMMAND_SECRETS,
    GREETING_TEXT,
    REFERRAL_CODE_TEXT,
    REFERRAL_LINK_TEXT,
    REFERRALS_DISABLED_TEXT,
    SECRETS_DISABLED_TEXT,
    SECRETS_LINK_TEXT,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TextDispatch:
    message_id: int
    user_id: str
    chat_id: int
    text: str
    reply_to_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class CommandServices:
    client: TelegramClient
    registry: GatewayRegistry
    secrets_link: Callable[[str], str] | None
    referrals: ReferralStore | None
    bot_username: str | None


class PollerCommands:
    def __init__(self, services: CommandServices) -> None:
        self._client = services.client
        self._registry = services.registry
        self._secrets_link = services.secrets_link
        self._referrals = services.referrals
        self._bot_username = services.bot_username

    async def dispatch(self, request: TextDispatch) -> None:
        if request.text == COMMAND_START or start_code(request.text) is not None:
            if request.text == COMMAND_START:
                await self._client.send_message(SendMessage(request.chat_id, GREETING_TEXT))
            return
        if request.text.strip() == COMMAND_SECRETS:
            await self._send_secrets_link(request.user_id, request.chat_id)
            return
        if request.text.strip() == COMMAND_INVITE:
            await self._send_referral_link(request.user_id, request.chat_id)
            return
        bridge = await self._registry.gateway_for(request.user_id, request.chat_id)
        await bridge.handle_text(
            DialogSubmission(
                request.text,
                client_message_id=str(request.message_id),
                reply_to_message_id=request.reply_to_message_id,
            )
        )

    async def _send_secrets_link(self, user_id: str, chat_id: int) -> None:
        if self._secrets_link is None:
            await self._client.send_message(SendMessage(chat_id, SECRETS_DISABLED_TEXT))
            return
        external_id = user_id.removeprefix(USER_ID_PREFIX)
        await self._client.send_message(
            SendMessage(chat_id, SECRETS_LINK_TEXT.format(url=self._secrets_link(external_id)))
        )

    async def _send_referral_link(self, user_id: str, chat_id: int) -> None:
        if self._referrals is None:
            await self._client.send_message(SendMessage(chat_id, REFERRALS_DISABLED_TEXT))
            return
        code = f"{REFERRAL_PREFIX}{await self._referrals.code_of(user_id)}"
        if self._bot_username:
            link = f"https://t.me/{self._bot_username}?start={code}"
            await self._client.send_message(
                SendMessage(chat_id, REFERRAL_LINK_TEXT.format(link=link))
            )
        else:
            await self._client.send_message(
                SendMessage(chat_id, REFERRAL_CODE_TEXT.format(code=code))
            )
