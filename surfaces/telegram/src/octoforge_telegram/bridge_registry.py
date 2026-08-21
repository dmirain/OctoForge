"""One Telegram bridge per person and DialogSurface lifecycle."""

import logging
from dataclasses import dataclass

from octoforge_core.agent.runner import ConversationRunner
from octoforge_core.identity.api import IdentityStore

from octoforge_telegram.bridge import (
    RunnerProvider,
    TelegramBridge,
    TelegramBridgeOptions,
    TelegramBridgeServices,
)
from octoforge_telegram.client import TELEGRAM_CHANNEL, USER_ID_PREFIX, TelegramClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BridgeRegistryServices:
    runner_provider: RunnerProvider
    client: TelegramClient
    identities: IdentityStore | None = None


class TelegramBridgeRegistry:
    def __init__(self, services: BridgeRegistryServices, options: TelegramBridgeOptions) -> None:
        self._services = services
        self._identities = services.identities
        self._options = options
        self._bridges: dict[str, TelegramBridge] = {}

    async def person_of(self, external_id: str) -> str:
        if self._identities is None:
            return f"{USER_ID_PREFIX}{external_id}"
        return await self._identities.resolve_or_create(TELEGRAM_CHANNEL, external_id)

    async def gateway_for(self, handle: str, chat_id: int) -> TelegramBridge:
        return self.get_or_create(
            await self.person_of(handle.removeprefix(USER_ID_PREFIX)),
            chat_id,
        )

    def get_or_create(self, user_id: str, chat_id: int) -> TelegramBridge:
        bridge = self._bridges.get(user_id)
        if bridge is None:
            bridge = TelegramBridge(
                TelegramBridgeServices(
                    user_id,
                    chat_id,
                    self._services.runner_provider,
                    self._services.client,
                ),
                self._options,
            )
            self._bridges[user_id] = bridge
        return bridge

    async def attach(self, runner: ConversationRunner) -> None:
        if runner.channel != TELEGRAM_CHANNEL:
            return
        chat_id = await self._chat_of(runner.user_id)
        if chat_id is None:
            logger.warning("no telegram account on record for user %r", runner.user_id)
            return
        await self.get_or_create(runner.user_id, chat_id).start()

    async def detach(self, runner: ConversationRunner) -> None:
        if runner.channel != TELEGRAM_CHANNEL:
            return
        bridge = self._bridges.pop(runner.user_id, None)
        if bridge is not None:
            await bridge.aclose()

    async def aclose(self) -> None:
        for bridge in self._bridges.values():
            await bridge.aclose()

    async def _chat_of(self, user_id: str) -> int | None:
        if self._identities is None:
            return None
        for identity in await self._identities.identities_of(user_id):
            if identity.surface == TELEGRAM_CHANNEL and identity.active:
                try:
                    return int(identity.external_id)
                except ValueError:
                    return None
        return None
