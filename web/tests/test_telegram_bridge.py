"""Tests for the Telegram bridge rendering dialog events into a chat."""

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from octoforge_core import (
    AgentLoop,
    ChatMessage,
    ConversationManager,
    MessageRole,
    ToolRegistry,
    ToolSpec,
)
from octoforge_core.agent.events import ProcessStarted, TextDelta
from octoforge_core.agent.prompts import SYSTEM_PROMPT_NAME, StaticPromptProvider
from octoforge_core.agent.router import ExchangeInfo, RouteDecision
from octoforge_core.agent.runner import RunnerConfig
from octoforge_core.context.compactor import NoopContextCompactor
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.dialogs.store import (
    SqlAlchemyDialogRepository,
    SqlAlchemyExchangeRepository,
    SqlAlchemyMessageRepository,
)
from octoforge_core.domain import ToolCall
from octoforge_core.llm.events import StreamEvent, StreamFinished
from octoforge_core.llm.events import TextDelta as LlmTextDelta
from octoforge_core.llm.usage import Completion
from octoforge_core.ports import LLMClient
from octoforge_core.tasks.api import Task, TaskKind, TaskStatus
from octoforge_core.tasks.store import InMemoryTaskStore
from octoforge_core.time import utc_now
from octoforge_core.tools.base import ToolContext
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_web.telegram.bridge import (
    CANCELLED_LINE,
    PARSE_MODE_HTML,
    TOOL_LINE_TEMPLATE,
    TelegramBridge,
    TelegramBridgeOptions,
)
from octoforge_web.telegram.client import (
    MAX_MESSAGE_LENGTH,
    TELEGRAM_CHANNEL,
    USER_ID_PREFIX,
    TelegramApiError,
)

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
ECHO_TOOL = "echo_tool"
ECHO_OUTPUT = "echo output"
CALL_ID = "call-1"
LONG_REPLY_TAIL = "x" * 100
CRON_TITLE = "morning report"
CRON_RESULT = "the report is ready"
EXPECTED_MESSAGE_COUNT = 2
QUESTION_MESSAGE_ID = 777


class FakeTelegramClient:
    """TelegramClient stub recording the outbound calls."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str, str | None]] = []
        # reply targets, one per send (None = plain message)
        self.replies: list[int | None] = []
        self.edited: list[tuple[int, int, str, str | None]] = []
        self.rich_edited: list[tuple[int, int, str]] = []
        self.actions: list[tuple[int, str]] = []
        self.rich_edit_error: TelegramApiError | None = None
        self._next_message_id = 0

    async def get_updates(self, offset: int | None, timeout_seconds: float) -> list[Any]:
        raise NotImplementedError

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> int:
        self._next_message_id += 1
        self.sent.append((chat_id, text, parse_mode))
        self.replies.append(reply_to_message_id)
        return self._next_message_id

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, parse_mode: str | None = None
    ) -> None:
        self.edited.append((chat_id, message_id, text, parse_mode))

    async def edit_message_rich(self, chat_id: int, message_id: int, markdown: str) -> None:
        if self.rich_edit_error is not None:
            raise self.rich_edit_error
        self.rich_edited.append((chat_id, message_id, markdown))

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
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        raise NotImplementedError

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
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
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        raise NotImplementedError

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
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
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        raise NotImplementedError

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield LlmTextDelta(text=PARTIAL)
        await self.release.wait()
        yield StreamFinished(message=ChatMessage(role=MessageRole.ASSISTANT, content="full"))


class FailingLLM:
    """LLMClient stub raising mid-stream."""

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        raise NotImplementedError

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield LlmTextDelta(text=PARTIAL)
        raise RuntimeError("boom")


class EchoTool:
    """Tool stub returning a fixed output."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=ECHO_TOOL, description="echo", parameters_schema={})

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        return ECHO_OUTPUT


class PassthroughRouter:
    """MessageRouter stub always starting a new process."""

    async def route(
        self,
        exchanges: tuple[ExchangeInfo, ...],
        message: str,
        max_exchanges: int,
    ) -> RouteDecision:
        return RouteDecision()


def reply(content: str = REPLY) -> ChatMessage:
    return ChatMessage(role=MessageRole.ASSISTANT, content=content)


def tool_call_reply() -> ChatMessage:
    call = ToolCall(id=CALL_ID, name=ECHO_TOOL, arguments={})
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
    registry: ToolRegistry | None = None,
    tasks: InMemoryTaskStore | None = None,
) -> ConversationManager:
    loop = AgentLoop(
        llm_client=llm_client,
        registry=registry or ToolRegistry(),
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
        dialogs=SqlAlchemyDialogRepository(session_factory),
        messages=SqlAlchemyMessageRepository(session_factory),
        tasks=tasks if tasks is not None else InMemoryTaskStore(),
        exchanges=SqlAlchemyExchangeRepository(session_factory),
    )


def make_bridge(
    client: FakeTelegramClient,
    manager: ConversationManager,
    rich_messages_enabled: bool = True,
) -> TelegramBridge:
    return TelegramBridge(
        user_id=TELEGRAM_USER_ID,
        chat_id=CHAT_ID,
        runner_provider=manager.get_or_create_runner,
        client=client,
        options=TelegramBridgeOptions(
            edit_throttle_seconds=NO_THROTTLE,
            rich_messages_enabled=rich_messages_enabled,
        ),
    )


async def wait_until(predicate: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(POLL_SECONDS)

    await asyncio.wait_for(poll(), WAIT_TIMEOUT_SECONDS)


async def test_warmed_bridge_gets_the_result_that_finished_while_it_was_down(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A result redelivered before the bridge subscribes must still reach the chat.

    `runtime()` sweeps undelivered results before the surfaces start, so the
    redelivery finds no subscriber; the outbox has to hold it until the bridge
    is warmed, otherwise a cron answer that landed while the bot was down is
    silently stamped delivered and never shown.
    """
    client = FakeTelegramClient()
    tasks = InMemoryTaskStore()
    manager = await make_manager(ScriptedLLM([]), session_factory, tasks=tasks)
    dialog = await SqlAlchemyDialogRepository(session_factory).get_or_create(
        TELEGRAM_USER_ID, TELEGRAM_CHANNEL
    )
    task = Task(
        dialog_id=dialog.id,
        user_id=dialog.user_id,
        channel=dialog.channel,
        title=CRON_TITLE,
        kind=TaskKind.RUN,
        input={"prompt": CRON_TITLE},
        status=TaskStatus.DONE,
        result=CRON_RESULT,
        started_at=utc_now(),
    )
    await tasks.add(task)

    await manager.recover_interrupted()  # no bridge is attached yet
    await asyncio.sleep(POLL_SECONDS * 5)

    assert client.sent == []
    assert task.delivered_at is None

    bridge = make_bridge(client, manager)
    await bridge.start()  # what TelegramBridgeRegistry.warm() does at startup

    await wait_until(lambda: client.sent != [])
    assert client.sent == [(CHAT_ID, CRON_RESULT, PARSE_MODE_HTML)]
    await wait_until(lambda: task.delivered_at is not None)
    await bridge.aclose()


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


async def test_answer_replies_to_its_question(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The draft is created as a reply to the Telegram message it answers."""
    client = FakeTelegramClient()
    manager = await make_manager(ScriptedLLM([reply()]), session_factory)
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi", client_message_id="777")
    await wait_until(lambda: client.current_text() == REPLY if client.sent else False)

    assert client.replies == [777]
    await bridge.aclose()


async def test_empty_final_renders_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A deliberately silent answer must not produce an empty Telegram message."""
    client = FakeTelegramClient()
    manager = await make_manager(ScriptedLLM([reply("")]), session_factory)
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi")
    runner = await manager.get_or_create_runner(TELEGRAM_USER_ID, TELEGRAM_CHANNEL)
    await wait_until(lambda: not runner._processes)

    assert client.sent == []
    assert client.edited == []
    await bridge.aclose()


async def test_process_started_does_not_retarget_a_live_draft(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """White-box: once the draft message exists, a later ProcessStarted must
    not change what it replies to (a reply is fixed at creation time)."""
    client = FakeTelegramClient()
    manager = await make_manager(ScriptedLLM([]), session_factory)
    bridge = make_bridge(client, manager)

    await bridge._render(
        ProcessStarted(process_id="p1", title="one", source_client_message_id="777")
    )
    await bridge._render(TextDelta(text="draft text"))
    assert client.replies == [QUESTION_MESSAGE_ID]
    await bridge._render(
        ProcessStarted(process_id="p2", title="two", source_client_message_id="888")
    )

    assert bridge._draft.reply_to == QUESTION_MESSAGE_ID  # unchanged


async def test_keyless_submit_sends_a_plain_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = FakeTelegramClient()
    manager = await make_manager(ScriptedLLM([reply()]), session_factory)
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi")
    await wait_until(lambda: client.current_text() == REPLY if client.sent else False)

    assert client.replies == [None]
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

    await bridge.handle_text("hi", client_message_id="555")
    await wait_until(lambda: len(client.sent) == EXPECTED_MESSAGE_COUNT)

    head, tail = client.sent
    assert head == (CHAT_ID, "x" * MAX_MESSAGE_LENGTH, PARSE_MODE_HTML)
    assert tail == (CHAT_ID, LONG_REPLY_TAIL, PARSE_MODE_HTML)
    assert client.edited == []
    # only the head of a split answer replies; continuations are plain
    assert client.replies == [555, None]
    await bridge.aclose()


async def test_tool_call_renders_status_line_before_the_answer(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    client = FakeTelegramClient()
    manager = await make_manager(
        ScriptedLLM([tool_call_reply(), reply()]), session_factory, registry
    )
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi")
    tool_line = TOOL_LINE_TEMPLATE.format(name=ECHO_TOOL)
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


async def test_table_reply_is_upgraded_to_a_rich_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = FakeTelegramClient()
    content = "| col | val |\n| --- | --- |\n| a | 1 |"
    manager = await make_manager(ScriptedLLM([reply(content)]), session_factory)
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi", client_message_id="777")
    await wait_until(lambda: bool(client.rich_edited))

    assert client.rich_edited == [(CHAT_ID, 1, content)]
    assert client.sent[0][2] == PARSE_MODE_HTML  # the draft streamed as HTML first
    # the in-place rich upgrade edits the same message: threading survives
    assert client.replies[0] == QUESTION_MESSAGE_ID
    await bridge.aclose()


async def test_plain_reply_stays_on_the_legacy_path(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = FakeTelegramClient()
    manager = await make_manager(ScriptedLLM([reply()]), session_factory)
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi")
    await wait_until(lambda: client.current_text() == REPLY if client.sent else False)

    assert client.rich_edited == []
    await bridge.aclose()


async def test_split_reply_is_not_upgraded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = FakeTelegramClient()
    content = "| a |\n| --- |\n" + "x" * MAX_MESSAGE_LENGTH
    manager = await make_manager(ScriptedLLM([reply(content)]), session_factory)
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi")
    await wait_until(lambda: len(client.sent) == EXPECTED_MESSAGE_COUNT)

    assert client.rich_edited == []
    await bridge.aclose()


async def test_rich_upgrade_can_be_disabled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = FakeTelegramClient()
    content = "| col |\n| --- |\n| a |"
    manager = await make_manager(ScriptedLLM([reply(content)]), session_factory)
    bridge = make_bridge(client, manager, rich_messages_enabled=False)

    await bridge.handle_text("hi")
    await wait_until(lambda: bool(client.sent) and content in client.current_text())

    assert client.rich_edited == []
    await bridge.aclose()


async def test_a_failing_rich_upgrade_keeps_the_html_version(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = FakeTelegramClient()
    client.rich_edit_error = TelegramApiError("Bad Request: can't parse rich message")
    content = "| col |\n| --- |\n| a |"
    manager = await make_manager(ScriptedLLM([reply(content)]), session_factory)
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi")
    await wait_until(lambda: bool(client.sent) and content in client.current_text())

    assert client.rich_edited == []
    assert client.current_text() == content  # the HTML draft survived
    await bridge.aclose()
