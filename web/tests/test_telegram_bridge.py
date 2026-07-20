"""Tests for the Telegram bridge rendering dialog events into a chat."""

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

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
from octoforge_core.agent.runner import RunnerConfig
from octoforge_core.context.compactor import NoopContextCompactor
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.domain import ToolCall
from octoforge_core.llm.events import StreamEvent, StreamFinished
from octoforge_core.llm.events import TextDelta as LlmTextDelta
from octoforge_core.ports import LLMClient
from octoforge_core.skills.base import SkillContext, SkillOrigin
from octoforge_core.tasks.store import InMemoryTaskStore
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_web.telegram.bridge import (
    CANCELLED_LINE,
    PARSE_MODE_HTML,
    TOOL_LINE_TEMPLATE,
    TelegramBridge,
)
from octoforge_web.telegram.client import MAX_MESSAGE_LENGTH, USER_ID_PREFIX

CHAT_ID = 12345
TELEGRAM_USER_ID = f"{USER_ID_PREFIX}{CHAT_ID}"
SYSTEM_PROMPT = "test prompt"
MAX_ITERATIONS = 3
MAX_PROCESSES = 5
NO_THROTTLE = 0.0
MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
WAIT_TIMEOUT_SECONDS = 5.0
POLL_SECONDS = 0.01
REPLY = "final answer"
PARTIAL = "partial"
ECHO_SKILL = "echo_skill"
ECHO_OUTPUT = "echo output"
CALL_ID = "call-1"
LONG_REPLY_TAIL = "x" * 100
EXPECTED_MESSAGE_COUNT = 2


class FakeTelegramClient:
    """TelegramClient stub recording the outbound calls."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str, str | None]] = []
        self.edited: list[tuple[int, int, str, str | None]] = []
        self.actions: list[tuple[int, str]] = []
        self._next_message_id = 0

    async def get_updates(self, offset: int | None, timeout_seconds: float) -> list[Any]:
        raise NotImplementedError

    async def send_message(self, chat_id: int, text: str, parse_mode: str | None = None) -> int:
        self._next_message_id += 1
        self.sent.append((chat_id, text, parse_mode))
        return self._next_message_id

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, parse_mode: str | None = None
    ) -> None:
        self.edited.append((chat_id, message_id, text, parse_mode))

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        self.actions.append((chat_id, action))

    def current_text(self) -> str:
        """The text of the single message rendered so far (last edit or first send)."""
        if self.edited:
            return self.edited[-1][2]
        assert self.sent, "no message rendered yet"
        return self.sent[-1][1]


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


class ChunkedLLM:
    """LLMClient stub streaming one reply chunk by chunk."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

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
        for chunk in self._chunks:
            yield LlmTextDelta(text=chunk)
        yield StreamFinished(
            message=ChatMessage(role=MessageRole.ASSISTANT, content="".join(self._chunks))
        )


class StallingLLM:
    """LLMClient stub emitting partial text and stalling until released."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

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
        yield LlmTextDelta(text=PARTIAL)
        await self.release.wait()
        yield StreamFinished(message=ChatMessage(role=MessageRole.ASSISTANT, content="full"))


class FailingLLM:
    """LLMClient stub raising mid-stream."""

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
        yield LlmTextDelta(text=PARTIAL)
        raise RuntimeError("boom")


class EchoSkill:
    """Skill stub returning a fixed output."""

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(name=ECHO_SKILL, description="echo", parameters_schema={})

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        return ECHO_OUTPUT


class PassthroughRouter:
    """MessageRouter stub always starting a new process."""

    async def route(
        self,
        processes: tuple[ProcessInfo, ...],
        message: str,
        max_processes: int,
    ) -> RouteDecision:
        return RouteDecision()


def reply(content: str = REPLY) -> ChatMessage:
    return ChatMessage(role=MessageRole.ASSISTANT, content=content)


def tool_call_reply() -> ChatMessage:
    call = ToolCall(id=CALL_ID, name=ECHO_SKILL, arguments={})
    return ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=(call,))


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


async def make_manager(
    llm_client: LLMClient,
    session_factory: async_sessionmaker[AsyncSession],
    registry: SkillRegistry | None = None,
) -> ConversationManager:
    loop = AgentLoop(
        llm_client=llm_client,
        registry=registry or SkillRegistry(),
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


def make_bridge(client: FakeTelegramClient, manager: ConversationManager) -> TelegramBridge:
    return TelegramBridge(
        user_id=TELEGRAM_USER_ID,
        chat_id=CHAT_ID,
        runner_provider=manager.get_or_create_runner,
        client=client,
        edit_throttle_seconds=NO_THROTTLE,
    )


async def wait_until(predicate: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(POLL_SECONDS)

    await asyncio.wait_for(poll(), WAIT_TIMEOUT_SECONDS)


async def test_single_delta_sends_one_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = FakeTelegramClient()
    manager = await make_manager(ScriptedLLM([reply()]), session_factory)
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi")
    await wait_until(lambda: client.current_text() == REPLY if client.sent else False)

    assert client.sent == [(CHAT_ID, REPLY, PARSE_MODE_HTML)]
    assert client.edited == []
    await bridge.aclose()


async def test_deltas_stream_into_one_edited_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = FakeTelegramClient()
    manager = await make_manager(ChunkedLLM(["hel", "lo"]), session_factory)
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi")
    await wait_until(lambda: client.current_text() == "hello" if client.sent else False)

    assert client.sent == [(CHAT_ID, "hel", PARSE_MODE_HTML)]
    assert client.edited == [(CHAT_ID, 1, "hello", PARSE_MODE_HTML)]
    await bridge.aclose()


async def test_long_reply_is_split_into_telegram_sized_messages(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = FakeTelegramClient()
    long_reply = "x" * MAX_MESSAGE_LENGTH + LONG_REPLY_TAIL
    manager = await make_manager(ScriptedLLM([reply(long_reply)]), session_factory)
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi")
    await wait_until(lambda: len(client.sent) == EXPECTED_MESSAGE_COUNT)

    head, tail = client.sent
    assert head == (CHAT_ID, "x" * MAX_MESSAGE_LENGTH, PARSE_MODE_HTML)
    assert tail == (CHAT_ID, LONG_REPLY_TAIL, PARSE_MODE_HTML)
    assert client.edited == []
    await bridge.aclose()


async def test_tool_call_renders_status_line_before_the_answer(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    registry = SkillRegistry()
    registry.register(EchoSkill(), SkillOrigin.BASIC)
    client = FakeTelegramClient()
    manager = await make_manager(
        ScriptedLLM([tool_call_reply(), reply()]), session_factory, registry
    )
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi")
    tool_line = TOOL_LINE_TEMPLATE.format(name=ECHO_SKILL)
    expected = f"{tool_line}\n{REPLY}"
    await wait_until(lambda: client.current_text() == expected if client.sent else False)

    assert client.sent == [(CHAT_ID, tool_line, PARSE_MODE_HTML)]
    assert client.edited == [(CHAT_ID, 1, expected, PARSE_MODE_HTML)]
    await bridge.aclose()


async def test_cancel_appends_the_cancelled_line(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = StallingLLM()
    client = FakeTelegramClient()
    manager = await make_manager(llm, session_factory)
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi")
    await wait_until(lambda: bool(client.sent))
    await bridge.cancel()
    llm.release.set()
    expected = f"{PARTIAL}\n{CANCELLED_LINE}"
    await wait_until(lambda: client.current_text() == expected if client.edited else False)

    assert client.current_text() == expected
    await bridge.aclose()


async def test_llm_failure_appends_the_error_line(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = FakeTelegramClient()
    manager = await make_manager(FailingLLM(), session_factory)
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi")
    await wait_until(lambda: "❌ Ошибка" in client.current_text() if client.sent else False)

    assert client.current_text().startswith(PARTIAL)
    assert "RuntimeError: boom" in client.current_text()
    await bridge.aclose()


async def test_markdown_reply_is_delivered_as_html(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = FakeTelegramClient()
    content = "**жирный**\n```\ncode\n```"
    expected_html = "<b>жирный</b>\n<pre>code</pre>"
    manager = await make_manager(ScriptedLLM([reply(content)]), session_factory)
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi")
    await wait_until(lambda: client.current_text() == expected_html if client.sent else False)

    assert client.sent == [(CHAT_ID, expected_html, PARSE_MODE_HTML)]
    await bridge.aclose()
