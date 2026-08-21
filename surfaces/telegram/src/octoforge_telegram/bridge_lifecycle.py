"""Runner lifecycle and incoming submission of one Telegram chat bridge."""

import asyncio
from collections import OrderedDict
from contextlib import suppress

from octoforge_core.agent.events import LoopEvent
from octoforge_core.agent.runner import (
    STREAM_CLOSED,
    ConversationRunner,
    SubscriberQueue,
)
from octoforge_core.domain import MessageKind

from octoforge_telegram.bridge_runtime import TelegramBridgeServices, build_runtime
from octoforge_telegram.bridge_state import REPLY_TARGET_MAP_SIZE, Draft, TelegramBridgeOptions
from octoforge_telegram.client import CHAT_ACTION_TYPING, TELEGRAM_CHANNEL
from octoforge_telegram.gateway_types import DialogSubmission


class TelegramBridge:
    def __init__(self, services: TelegramBridgeServices, options: TelegramBridgeOptions) -> None:
        self._user_id = services.user_id
        self._chat_id = services.chat_id
        self._runner_provider = services.runner_provider
        self._client = services.client
        self._runner: ConversationRunner | None = None
        self._forwarder: asyncio.Task[None] | None = None
        self._starting = False
        self._reply_targets: OrderedDict[int, str] = OrderedDict()
        runtime = build_runtime(services, options, self._record_reply_target)
        self._draft_book = runtime.drafts
        self._typing = runtime.typing
        self._delivery = runtime.delivery
        self._renderer = runtime.renderer

    async def start(self) -> None:
        if self._starting or (self._forwarder is not None and not self._forwarder.done()):
            return
        self._starting = True
        try:
            await self._draft_book.restore()
            runner = await self._ensure_runner()
            self._forwarder = asyncio.create_task(self._forward(runner, runner.subscribe()))
        finally:
            self._starting = False

    async def handle_text(self, submission: DialogSubmission) -> None:
        runner = await self._ensure_runner()
        await self.start()
        if submission.kind is MessageKind.OWN:
            await self._client.send_chat_action(self._chat_id, CHAT_ACTION_TYPING)
        reply_exchange = (
            self._reply_targets.get(submission.reply_to_message_id)
            if submission.reply_to_message_id is not None
            else None
        )
        await runner.submit(submission.to_core(reply_exchange))

    async def aclose(self) -> None:
        self._typing.stop_all()
        for draft in self._draft_book.values():
            self._delivery.cancel_timer(draft)
        if self._forwarder is not None:
            self._forwarder.cancel()
            with suppress(asyncio.CancelledError):
                await self._forwarder

    async def _ensure_runner(self) -> ConversationRunner:
        if self._runner is None:
            self._runner = await self._runner_provider(self._user_id, TELEGRAM_CHANNEL)
        return self._runner

    async def _restore_drafts(self) -> None:
        await self._draft_book.restore()

    async def _forward(self, runner: ConversationRunner, queue: SubscriberQueue) -> None:
        try:
            while True:
                event = await queue.get()
                if event is STREAM_CLOSED:
                    self._runner = None
                    return
                await self._renderer.render_safely(event.payload, event.exchange_id)
        finally:
            self._typing.stop_all()
            for draft in self._draft_book.values():
                self._delivery.cancel_timer(draft)
            runner.unsubscribe(queue)

    async def _render(self, event: LoopEvent, exchange_id: str | None) -> None:
        await self._renderer._render(event, exchange_id)

    async def _render_safely(self, event: LoopEvent, exchange_id: str | None) -> None:
        await self._renderer.render_safely(event, exchange_id)

    @property
    def _typing_pulse(self) -> asyncio.Task[None] | None:
        return self._typing.pulse

    @property
    def _typing_exchanges(self) -> set[str | None]:
        return self._typing.exchanges

    @property
    def _drafts(self) -> dict[str | None, Draft]:
        return self._draft_book.entries

    def _record_reply_target(self, message_id: int, exchange_id: str) -> None:
        self._reply_targets[message_id] = exchange_id
        if len(self._reply_targets) > REPLY_TARGET_MAP_SIZE:
            self._reply_targets.popitem(last=False)
