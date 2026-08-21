"""Assemble draft, delivery, event and typing collaborators for one bridge."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from octoforge_core.agent.runner import ConversationRunner

from octoforge_telegram.bridge_delivery import DeliveryServices, DraftDelivery
from octoforge_telegram.bridge_drafts import DraftBook
from octoforge_telegram.bridge_events import EventRenderer, RenderServices
from octoforge_telegram.bridge_state import TelegramBridgeOptions
from octoforge_telegram.bridge_typing import TypingIndicator
from octoforge_telegram.client import TelegramClient

RunnerProvider = Callable[[str, str], Awaitable[ConversationRunner]]


@dataclass(frozen=True, slots=True)
class TelegramBridgeServices:
    user_id: str
    chat_id: int
    runner_provider: RunnerProvider
    client: TelegramClient


@dataclass(frozen=True, slots=True)
class BridgeRuntime:
    drafts: DraftBook
    typing: TypingIndicator
    delivery: DraftDelivery
    renderer: EventRenderer


def build_runtime(
    services: TelegramBridgeServices,
    options: TelegramBridgeOptions,
    record_reply: Callable[[int, str], None],
) -> BridgeRuntime:
    drafts = DraftBook(services.user_id, services.chat_id, options.drafts)
    typing = TypingIndicator(services.client, services.chat_id)
    delivery = DraftDelivery(
        DeliveryServices(
            services.user_id,
            services.chat_id,
            services.client,
            drafts,
            record_reply,
        ),
        options.edit_throttle_seconds,
    )
    renderer = EventRenderer(RenderServices(services.user_id, drafts, delivery, typing))
    return BridgeRuntime(drafts, typing, delivery, renderer)
