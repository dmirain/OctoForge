"""Tests for the Telegram long-poll loop and the bridge registry."""

import asyncio
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from octoforge_core import (
    AgentLoop,
    ChatMessage,
    ConversationManager,
    DialogRepository,
    MessageRepository,
    MessageRole,
    SkillRegistry,
    SkillSpec,
)
from octoforge_core.agent.prompts import SYSTEM_PROMPT_NAME, StaticPromptProvider
from octoforge_core.agent.router import ProcessInfo, RouteDecision
from octoforge_core.agent.runner import ConversationRunner, RunnerConfig
from octoforge_core.context.compactor import NoopContextCompactor
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.llm.events import StreamEvent, StreamFinished
from octoforge_core.llm.events import TextDelta as LlmTextDelta
from octoforge_core.tasks.store import InMemoryTaskStore
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_web.telegram.bridge import PARSE_MODE_HTML, RunnerProvider
from octoforge_web.telegram.client import USER_ID_PREFIX
from octoforge_web.telegram.models import (
    TelegramChat,
    TelegramChatType,
    TelegramMessage,
    TelegramUpdate,
    TelegramUser,
)
from octoforge_web.telegram.poller import (
    COMMAND_CANCEL,
    COMMAND_START,
    GREETING_TEXT,
    GROUP_NOTICE,
    TEXT_ONLY_NOTICE,
    TelegramBridgeRegistry,
    TelegramPoller,
    chat_id_from_user_id,
)

TELEGRAM_USER_ID = 12345
USER_ID = f"{USER_ID_PREFIX}{TELEGRAM_USER_ID}"
CHANNEL = "telegram"
SYSTEM_PROMPT = "test prompt"
MAX_ITERATIONS = 3
MAX_PROCESSES = 5
NO_THROTTLE = 0.0
NO_BACKOFF = 0.0
POLL_TIMEOUT = 30.0
MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
WAIT_TIMEOUT_SECONDS = 5.0
POLL_SECONDS = 0.01
IDLE_BATCH_SECONDS = 0.05
REPLY = "pong"
FIRST_UPDATE_ID = 41
SECOND_UPDATE_ID = 42
EXPECTED_GREETING_COUNT = 2
EXPECTED_CALLS_AFTER_DRAIN = 2
MIN_CALLS_AFTER_RECOVERY = 3


class FakeTelegramClient:
    """TelegramClient stub with scripted poll batches and recorded outbound calls."""

    def __init__(self, batches: list[list[TelegramUpdate]] | None = None) -> None:
        self._batches = list(batches) if batches is not None else []
        self.poll_calls: list[tuple[int | None, float]] = []
        self.failures: list[Exception] = []
        self.sent: list[tuple[int, str, str | None]] = []
        self.edited: list[tuple[int, int, str, str | None]] = []
        self._next_message_id = 0

    async def get_updates(self, offset: int | None, timeout_seconds: float) -> list[TelegramUpdate]:
        self.poll_calls.append((offset, timeout_seconds))
        if self.failures:
            raise self.failures.pop(0)
        if self._batches:
            return self._batches.pop(0)
        await asyncio.sleep(IDLE_BATCH_SECONDS)
        return []

    async def send_message(self, chat_id: int, text: str, parse_mode: str | None = None) -> int:
        self._next_message_id += 1
        self.sent.append((chat_id, text, parse_mode))
        return self._next_message_id

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, parse_mode: str | None = None
    ) -> None:
        self.edited.append((chat_id, message_id, text, parse_mode))

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        pass


class ScriptedLLM:
    """LLMClient stub replaying scripted replies as whole-delta streams."""

    def __init__(self, replies: list[ChatMessage]) -> None:
        self._replies = list(replies)

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> ChatMessage:
        raise NotImplementedError

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        reply = self._replies.pop(0)
        if reply.content:
            yield LlmTextDelta(text=reply.content)
        yield StreamFinished(message=reply)


class PassthroughRouter:
    """MessageRouter stub always starting a new process."""

    async def route(
        self,
        processes: tuple[ProcessInfo, ...],
        message: str,
        max_processes: int,
    ) -> RouteDecision:
        return RouteDecision()


def make_update(
    update_id: int,
    text: str | None = "hi",
    chat_type: TelegramChatType = TelegramChatType.PRIVATE,
) -> TelegramUpdate:
    return TelegramUpdate(
        update_id=update_id,
        message=TelegramMessage(
            message_id=update_id,
            from_user=TelegramUser(id=TELEGRAM_USER_ID),
            chat=TelegramChat(id=TELEGRAM_USER_ID, type=chat_type),
            text=text,
        ),
    )


async def forbidden_provider(user_id: str, channel: str) -> ConversationRunner:
    raise AssertionError("runner should not be requested")


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


async def make_manager(
    replies: list[ChatMessage],
    session_factory: async_sessionmaker[AsyncSession],
) -> ConversationManager:
    loop = AgentLoop(
        llm_client=ScriptedLLM(replies),
        registry=SkillRegistry(),
        max_iterations=MAX_ITERATIONS,
    )
    return ConversationManager(
        config=RunnerConfig(
            loop=loop,
            prompts=StaticPromptProvider({SYSTEM_PROMPT_NAME: SYSTEM_PROMPT}),
            router=PassthroughRouter(),
            max_processes=MAX_PROCESSES,
            compactor=NoopContextCompactor(),
        ),
        dialogs=DialogRepository(session_factory),
        messages=MessageRepository(session_factory),
        tasks=InMemoryTaskStore(),
    )


def make_poller(
    client: FakeTelegramClient,
    provider: RunnerProvider = forbidden_provider,
) -> TelegramPoller:
    registry = TelegramBridgeRegistry(
        runner_provider=provider,
        client=client,
        edit_throttle_seconds=NO_THROTTLE,
    )
    return TelegramPoller(
        client=client,
        registry=registry,
        poll_timeout_seconds=POLL_TIMEOUT,
        error_backoff_seconds=NO_BACKOFF,
    )


async def wait_until(predicate: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(POLL_SECONDS)

    await asyncio.wait_for(poll(), WAIT_TIMEOUT_SECONDS)


async def test_start_command_greets_without_runner() -> None:
    client = FakeTelegramClient()
    poller = make_poller(client)

    await poller.dispatch(make_update(FIRST_UPDATE_ID, text=COMMAND_START))

    assert client.sent == [(TELEGRAM_USER_ID, GREETING_TEXT, None)]


async def test_group_chat_gets_a_notice() -> None:
    client = FakeTelegramClient()
    poller = make_poller(client)

    await poller.dispatch(make_update(FIRST_UPDATE_ID, chat_type=TelegramChatType.GROUP))

    assert client.sent == [(TELEGRAM_USER_ID, GROUP_NOTICE, None)]


async def test_non_text_message_gets_a_notice() -> None:
    client = FakeTelegramClient()
    poller = make_poller(client)

    await poller.dispatch(make_update(FIRST_UPDATE_ID, text=None))

    assert client.sent == [(TELEGRAM_USER_ID, TEXT_ONLY_NOTICE, None)]


async def test_text_message_reaches_the_dialog_and_renders_the_reply(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reply = ChatMessage(role=MessageRole.ASSISTANT, content=REPLY)
    manager = await make_manager([reply], session_factory)
    client = FakeTelegramClient()
    poller = make_poller(client, manager.get_or_create_runner)

    await poller.dispatch(make_update(FIRST_UPDATE_ID, text="ping"))
    await wait_until(lambda: bool(client.sent))

    assert client.sent[0] == (TELEGRAM_USER_ID, REPLY, PARSE_MODE_HTML)


async def test_cancel_command_is_accepted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = await make_manager([], session_factory)
    client = FakeTelegramClient()
    poller = make_poller(client, manager.get_or_create_runner)

    await poller.dispatch(make_update(FIRST_UPDATE_ID, text=COMMAND_CANCEL))

    assert client.sent == []


async def test_run_advances_the_offset_per_update() -> None:
    updates = [
        make_update(FIRST_UPDATE_ID, text=COMMAND_START),
        make_update(SECOND_UPDATE_ID, text=COMMAND_START),
    ]
    client = FakeTelegramClient(batches=[[], updates])
    poller = make_poller(client)
    task = asyncio.create_task(poller.run_forever())
    try:
        await wait_until(lambda: len(client.sent) == EXPECTED_GREETING_COUNT)
    finally:
        task.cancel()

    assert client.poll_calls[0] == (-1, 0.0)
    assert client.poll_calls[1] == (None, POLL_TIMEOUT)
    assert client.poll_calls[-1][0] == SECOND_UPDATE_ID + 1


async def test_backlog_is_drained_on_start() -> None:
    client = FakeTelegramClient(batches=[[make_update(FIRST_UPDATE_ID)]])
    poller = make_poller(client)
    task = asyncio.create_task(poller.run_forever())
    try:
        await wait_until(lambda: len(client.poll_calls) >= EXPECTED_CALLS_AFTER_DRAIN)
    finally:
        task.cancel()

    assert client.poll_calls[0] == (-1, 0.0)
    assert client.poll_calls[1] == (FIRST_UPDATE_ID + 1, POLL_TIMEOUT)
    assert client.sent == []


async def test_poller_recovers_from_poll_errors() -> None:
    client = FakeTelegramClient()
    client.failures.append(httpx.ConnectError("boom"))
    poller = make_poller(client)
    task = asyncio.create_task(poller.run_forever())
    try:
        await wait_until(lambda: len(client.poll_calls) >= MIN_CALLS_AFTER_RECOVERY)
    finally:
        task.cancel()

    assert len(client.poll_calls) >= MIN_CALLS_AFTER_RECOVERY


def test_chat_id_from_user_id() -> None:
    assert chat_id_from_user_id(USER_ID) == TELEGRAM_USER_ID
    assert chat_id_from_user_id("alice") is None
    assert chat_id_from_user_id(f"{USER_ID_PREFIX}abc") is None


async def test_warm_starts_bridges_for_known_telegram_dialogs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = await make_manager([], session_factory)
    requested: list[tuple[str, str]] = []

    async def provider(user_id: str, channel: str) -> ConversationRunner:
        requested.append((user_id, channel))
        return await manager.get_or_create_runner(user_id, channel)

    client = FakeTelegramClient()
    registry = TelegramBridgeRegistry(
        runner_provider=provider, client=client, edit_throttle_seconds=NO_THROTTLE
    )

    await registry.warm([USER_ID, "alice", f"{USER_ID_PREFIX}not-a-number"])

    assert requested == [(USER_ID, CHANNEL)]
    await registry.aclose()
